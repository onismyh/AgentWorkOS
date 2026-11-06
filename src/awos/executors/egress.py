"""Per-run egress proxy: the only way out to the network for agents AgentWorkOS confines itself (Pi, DSH; git hooks).

The Seatbelt profile lets such a process connect to nothing but this proxy's port (plus DNS), and its environment points
HTTP(S)_PROXY / NODE_USE_ENV_PROXY at it, so every request is checked here:

* only hosts on the run's allowlist (exact, or `*.example.com` for subdomains): HTTPS as CONNECT to port 443 whose TLS
  ClientHello names the same host (SNI), plain HTTP to port 80 whose Host header names the URL's host, so a tunnel
  opened for an allowed name cannot be pointed at another site on the same CDN;
* never non-global addresses (loopback, private, link-local, CGNAT, ...), whatever a name resolves to (the dashboard
  API and the LAN stay out of reach); behind an upstream proxy in fake-IP mode (198.18/15) names are left to it;
* chained through the user's upstream proxy when one is configured (HTTPS_PROXY or the macOS system proxy), else direct;
* bounded: request head size, header count and concurrent connections.

Denied hosts are recorded, so the run can turn them into a human-in-the-loop request (outbox `grant_network`).
"""
from __future__ import annotations

import ipaddress
import logging
import re
import select
import socket
import socketserver
import threading
from urllib.parse import urlsplit

log = logging.getLogger("awos.egress")

ALLOWED_PORTS = (80, 443)
MAX_HEAD = 32 * 1024
MAX_HEADERS = 100
MAX_CONNECTIONS = 64
FAKE_IP = ipaddress.ip_network("198.18.0.0/15")   # Clash / Surge fake-IP DNS answers: the upstream proxy resolves for real
_LABEL = r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?"
_NAME_RE = re.compile(rf"^{_LABEL}(\.{_LABEL})*\.[a-z][a-z0-9-]*[a-z0-9]$")
# Never allowed as a `*.` wildcard: anyone can get a subdomain there (public suffixes, user-content and app hosting).
SHARED_SUFFIXES = {
    "co.uk", "org.uk", "ac.uk", "com.cn", "net.cn", "org.cn", "edu.cn", "gov.cn", "com.au", "co.jp", "co.kr", "com.br", "com.tw",
    "com.hk", "github.io", "githubusercontent.com", "gitlab.io", "workers.dev", "pages.dev", "vercel.app", "netlify.app",
    "herokuapp.com", "appspot.com", "web.app", "firebaseapp.com", "azurewebsites.net", "cloudfront.net", "amazonaws.com",
    "blogspot.com", "glitch.me", "repl.co", "ngrok.io", "ngrok-free.app", "trycloudflare.com", "onrender.com", "fly.dev",
}


def valid_host(h: str) -> bool:
    """A DNS name (letters in the last label, so no IPv4 in any notation), lower-case-able, nothing else."""
    h = (h or "").strip().lower().rstrip(".")
    return len(h) <= 253 and bool(_NAME_RE.match(h))


def valid_domain(d: str) -> bool:
    """An allowlist entry: a DNS name with at least one dot, or `*.` + such a name that is not a public / shared suffix."""
    d = (d or "").strip().lower().rstrip(".")
    if d.startswith("*."):
        base = d[2:]
        return base.count(".") >= 1 and valid_host(base) and base not in SHARED_SUFFIXES
    return "." in d and valid_host(d)


def host_allowed(host: str, allow: list[str]) -> bool:
    if not valid_host(host):
        return False
    host = host.lower().rstrip(".")
    for a in allow:
        a = a.lower().rstrip(".")
        if a.startswith("*."):
            if host.endswith(a[1:]) and host != a[2:]:
                return True
        elif host == a:
            return True
    return False


def _internal(ip: str, *, fake_ip_ok: bool = False) -> bool:
    a = ipaddress.ip_address(ip.split("%")[0])
    if fake_ip_ok and a.version == 4 and a in FAKE_IP:
        return False
    return not a.is_global


def resolves_internal(host: str, *, behind_upstream: bool = False) -> bool:
    """True if `host` is, or resolves to, a non-global address. Unresolvable names are left to the upstream proxy, and so
    are fake-IP answers when there is one (it resolves the real name itself)."""
    try:
        return _internal(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(_internal(i[4][0], fake_ip_ok=behind_upstream) for i in infos)


def sni_of(data: bytes) -> str | None:
    """server_name from a TLS ClientHello record (None if `data` is not one, or names nothing)."""
    try:
        if len(data) < 5 or data[0] != 0x16:
            return None
        end = 5 + int.from_bytes(data[3:5], "big")
        p = 5
        if data[p] != 0x01:
            return None
        p += 4 + 2 + 32                                    # handshake header, client version, random
        p += 1 + data[p]                                   # session id
        p += 2 + int.from_bytes(data[p:p + 2], "big")      # cipher suites
        p += 1 + data[p]                                   # compression methods
        ext_end = p + 2 + int.from_bytes(data[p:p + 2], "big")
        p += 2
        while p + 4 <= min(ext_end, end):
            etype, elen = int.from_bytes(data[p:p + 2], "big"), int.from_bytes(data[p + 2:p + 4], "big")
            if etype == 0:                                 # server_name: list len, name type, name len, name
                q = p + 4 + 2
                if data[q] == 0:
                    n = int.from_bytes(data[q + 1:q + 3], "big")
                    return data[q + 3:q + 3 + n].decode("ascii").lower()
                return None
            p += 4 + elen
    except (IndexError, UnicodeDecodeError):
        return None
    return None


def _client_hello(conn: socket.socket, timeout: float = 15) -> bytes:
    """The first TLS record from the client (the ClientHello), read in full."""
    conn.settimeout(timeout)
    buf = b""
    while len(buf) < 5 or len(buf) < 5 + int.from_bytes(buf[3:5], "big"):
        chunk = conn.recv(16384)
        if not chunk:
            break
        buf += chunk
        if len(buf) > 64 * 1024:
            break
    return buf


def upstream_proxy() -> tuple[str, int, str | None] | None:
    """(host, port, Proxy-Authorization value or None) of the user's HTTP proxy, if any."""
    import base64
    import os
    from urllib.parse import unquote

    from ..osutil import system_proxy
    for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        u = os.environ.get(v)
        if u:
            break
    else:
        u = system_proxy().get("HTTPS_PROXY")
    if not u:
        return None
    p = urlsplit(u if "://" in u else f"http://{u}")
    if not p.hostname:
        return None
    auth = None
    if p.username:
        cred = f"{unquote(p.username)}:{unquote(p.password or '')}".encode()
        auth = "Basic " + base64.b64encode(cred).decode()
    return p.hostname, p.port or 80, auth


class _Handler(socketserver.StreamRequestHandler):
    timeout = 120
    rbufsize = 0   # unbuffered: after the request head nothing sits in a Python buffer, the socket pipe sees every byte

    def handle(self) -> None:
        srv: EgressProxy = self.server.owner  # type: ignore[attr-defined]
        if not srv.slots.acquire(blocking=False):
            return                                         # too many connections from this run
        try:
            self._handle(srv)
        finally:
            srv.slots.release()

    def _handle(self, srv: "EgressProxy") -> None:
        try:
            self.connection.settimeout(30)
            line = self.rfile.readline(8192).decode("latin-1").strip()
            if not line:
                return
            method, target, _ = (line.split(" ", 2) + ["", ""])[:3]
            headers, size = [], len(line)
            while True:
                h = self.rfile.readline(8192)
                size += len(h)
                if h in (b"\r\n", b"\n", b"") or size > MAX_HEAD or len(headers) >= MAX_HEADERS:
                    break
                headers.append(h)
            if size > MAX_HEAD or len(headers) >= MAX_HEADERS:
                return self._deny()
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port or 443)
                if port != 443:
                    srv.note_denied(f"{host}:{port}")
                    return self._deny()
                if not srv.check(host, port):
                    return self._deny()
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                hello = _client_hello(self.connection)
                if sni_of(hello) != host.lower().rstrip("."):   # the TLS session must be for the host that was allowed
                    srv.note_denied(f"{host} (TLS for {sni_of(hello) or 'unnamed host'})")
                    return
                upstream = srv.connect(host, port)
                upstream.sendall(hello)
                self.connection.settimeout(None)
                self._pipe(upstream)
            else:
                u = urlsplit(target)
                host, port = u.hostname or "", u.port or 80
                hosthdr = next((h.split(b":", 1)[1].strip().decode("latin-1").lower() for h in headers
                                if h.lower().startswith(b"host:")), "")
                if u.scheme != "http" or hosthdr.split(":")[0] != host.lower() or not srv.check(host, port):
                    return self._deny()
                upstream = srv.connect_plain(host, port)
                if srv.upstream is None:  # direct: origin-form request line
                    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                    line = f"{method} {path} HTTP/1.1"
                kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:"))]
                auth = [f"Proxy-Authorization: {srv.upstream[2]}\r\n".encode()] if srv.upstream and srv.upstream[2] else []
                upstream.sendall(line.encode("latin-1") + b"\r\n" + b"".join(kept) + b"".join(auth) + b"Connection: close\r\n\r\n")
                self.connection.settimeout(None)
                self._pipe(upstream)
        except (OSError, ValueError) as e:
            log.debug("egress: %s", e)

    def _deny(self) -> None:
        self.wfile.write(b"HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\n\r\n"
                         b"blocked by AgentWorkOS sandbox: host not on this task's network allowlist "
                         b"(propose a grant_network action to ask the user)\n")

    def _pipe(self, other: socket.socket) -> None:
        a = self.connection
        socks = [a, other]
        try:
            while True:
                r, _, x = select.select(socks, [], socks, self.timeout)
                if x or not r:
                    return
                for s in r:
                    data = s.recv(65536)
                    if not data:
                        return
                    (other if s is a else a).sendall(data)
        finally:
            other.close()


class _Server(socketserver.ThreadingTCPServer):
    daemon_threads = True
    allow_reuse_address = True


class EgressProxy:
    """`with EgressProxy(allow) as p:` -> p.url is http://127.0.0.1:<port>; p.denied lists refused hosts."""

    def __init__(self, allow: list[str], *, upstream: tuple[str, int, str | None] | None | bool = True) -> None:
        self.allow = sorted({a.lower() for a in allow if valid_domain(a)})
        self.upstream = upstream_proxy() if upstream is True else (upstream or None)
        self.denied: list[str] = []
        self.allowed: list[str] = []
        self._lock = threading.Lock()
        self.slots = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self._srv = _Server(("127.0.0.1", 0), _Handler)
        self._srv.owner = self  # type: ignore[attr-defined]
        self.port = self._srv.server_address[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self._thread = threading.Thread(target=self._srv.serve_forever, daemon=True, name=f"egress-{self.port}")

    def __enter__(self) -> "EgressProxy":
        self._thread.start()
        return self

    def __exit__(self, *exc) -> None:
        self._srv.shutdown()
        self._srv.server_close()

    def check(self, host: str, port: int) -> bool:
        ok = (port in ALLOWED_PORTS and host_allowed(host, self.allow)
              and not resolves_internal(host, behind_upstream=self.upstream is not None))
        name = host if port in (80, 443) else f"{host}:{port}"
        with self._lock:
            lst = self.allowed if ok else self.denied
            if name not in lst and len(lst) < 200:
                lst.append(name)
        return ok

    def note_denied(self, what: str) -> None:
        with self._lock:
            if what not in self.denied and len(self.denied) < 200:
                self.denied.append(what)

    def _open(self, host: str, port: int) -> socket.socket:
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=30)
        s = socket.create_connection(self.upstream[:2], timeout=30)
        auth = f"Proxy-Authorization: {self.upstream[2]}\r\n" if self.upstream[2] else ""
        s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n{auth}\r\n".encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = s.recv(4096)
            if not chunk:
                break
            resp += chunk
        if b" 200" not in resp.split(b"\r\n", 1)[0]:
            s.close()
            raise OSError(f"upstream proxy refused CONNECT {host}:{port}")
        return s

    def connect(self, host: str, port: int) -> socket.socket:
        return self._open(host, port)

    def connect_plain(self, host: str, port: int) -> socket.socket:
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=30)
        return socket.create_connection(self.upstream[:2], timeout=30)   # absolute-form request goes to the upstream as is

    def env(self) -> dict[str, str]:
        """Environment for the confined process: everything through this proxy, nothing exempt."""
        return {"HTTPS_PROXY": self.url, "HTTP_PROXY": self.url, "ALL_PROXY": self.url, "https_proxy": self.url,
                "http_proxy": self.url, "all_proxy": self.url, "NO_PROXY": "", "no_proxy": "", "NODE_USE_ENV_PROXY": "1"}


def report(proxy: "EgressProxy", on_event) -> None:
    """Record what the run was refused (it can become a `grant_network` request for the user)."""
    if proxy.denied:
        try:
            on_event("network_denied", None, {"hosts": proxy.denied[:50], "allowed": proxy.allowed[:50]})
        except Exception:  # pragma: no cover - never let reporting break a run
            log.exception("failed to record network denials")
