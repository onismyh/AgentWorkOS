"""Per-run egress proxy: the only way out to the network for agents AgentWorkOS confines itself (Pi, DSH; git hooks).

The Seatbelt profile lets such a process connect to nothing but this proxy's port (plus DNS), and its environment points
HTTP(S)_PROXY / NODE_USE_ENV_PROXY at it, so every request is checked here:

* only hosts on the run's allowlist (exact, or `*.example.com` for subdomains) and ports 80 / 443;
* never loopback, private, link-local or otherwise internal addresses, whatever a name resolves to (the dashboard API
  and the LAN stay out of reach, DNS rebinding included);
* chained through the user's upstream proxy when one is configured (HTTPS_PROXY or the macOS system proxy), else direct.

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
_HOST_RE = re.compile(r"^(\*\.)?[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]{0,62}[A-Za-z0-9])?)+$")


def valid_domain(d: str) -> bool:
    """A plain DNS name (optionally `*.` + name) with at least one dot; no IPs, no bare TLDs, no `*`."""
    d = (d or "").strip().lower().rstrip(".")
    if not _HOST_RE.match(d) or d.startswith("*.") and d.count(".") < 2:
        return False
    try:
        ipaddress.ip_address(d)
        return False
    except ValueError:
        return True


def host_allowed(host: str, allow: list[str]) -> bool:
    host = host.lower().rstrip(".")
    for a in allow:
        a = a.lower().rstrip(".")
        if a.startswith("*."):
            if host.endswith(a[1:]) and host != a[2:]:
                return True
        elif host == a:
            return True
    return False


def _internal(ip: str) -> bool:
    a = ipaddress.ip_address(ip)
    return a.is_loopback or a.is_private or a.is_link_local or a.is_multicast or a.is_reserved or a.is_unspecified


def resolves_internal(host: str) -> bool:
    """True if `host` is, or resolves to, an internal address. Unresolvable names are left to the upstream proxy."""
    try:
        return _internal(host)
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return False
    return any(_internal(i[4][0]) for i in infos)


def upstream_proxy() -> tuple[str, int] | None:
    import os

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
    return (p.hostname, p.port or 80) if p.hostname else None


class _Handler(socketserver.StreamRequestHandler):
    timeout = 120
    rbufsize = 0   # unbuffered: after the request head nothing sits in a Python buffer, the socket pipe sees every byte

    def handle(self) -> None:
        srv: EgressProxy = self.server.owner  # type: ignore[attr-defined]
        try:
            line = self.rfile.readline(8192).decode("latin-1").strip()
            if not line:
                return
            method, target, _ = (line.split(" ", 2) + ["", ""])[:3]
            headers = []
            while True:
                h = self.rfile.readline(8192)
                if h in (b"\r\n", b"\n", b""):
                    break
                headers.append(h)
            if method.upper() == "CONNECT":
                host, _, port = target.rpartition(":")
                host, port = host.strip("[]"), int(port or 443)
                if not srv.check(host, port):
                    return self._deny()
                upstream = srv.connect(host, port)
                self.wfile.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
                self.wfile.flush()
                self._pipe(upstream)
            else:
                u = urlsplit(target)
                host, port = u.hostname or "", u.port or 80
                if u.scheme != "http" or not srv.check(host, port):
                    return self._deny()
                upstream = srv.connect_plain(host, port)
                if srv.upstream is None:  # direct: origin-form request line
                    path = (u.path or "/") + (f"?{u.query}" if u.query else "")
                    line = f"{method} {path} HTTP/1.1"
                kept = [h for h in headers if not h.lower().startswith((b"proxy-", b"connection:"))]
                upstream.sendall(line.encode("latin-1") + b"\r\n" + b"".join(kept) + b"Connection: close\r\n\r\n")
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

    def __init__(self, allow: list[str], *, upstream: tuple[str, int] | None | bool = True) -> None:
        self.allow = sorted({a.lower() for a in allow if valid_domain(a)})
        self.upstream = upstream_proxy() if upstream is True else (upstream or None)
        self.denied: list[str] = []
        self.allowed: list[str] = []
        self._lock = threading.Lock()
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
        ok = (port in ALLOWED_PORTS and bool(host) and host_allowed(host, self.allow) and not resolves_internal(host))
        with self._lock:
            lst = self.allowed if ok else self.denied
            if host not in lst:
                lst.append(f"{host}" if port in (80, 443) else f"{host}:{port}")
        return ok

    def _open(self, host: str, port: int) -> socket.socket:
        if self.upstream is None:
            return socket.create_connection((host, port), timeout=30)
        s = socket.create_connection(self.upstream, timeout=30)
        s.sendall(f"CONNECT {host}:{port} HTTP/1.1\r\nHost: {host}:{port}\r\n\r\n".encode())
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
        return socket.create_connection(self.upstream, timeout=30)   # absolute-form request goes to the upstream as is

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
