"""Small cross-platform helpers (Windows / macOS / Linux) for subprocesses and bundled runtimes."""
from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

IS_WINDOWS = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"

# subprocess.run/Popen kwargs that keep a console window from flashing up on Windows; empty elsewhere
# (POSIX rejects any non-zero creationflags).
NO_WINDOW: dict = {"creationflags": subprocess.CREATE_NO_WINDOW} if IS_WINDOWS else {}


def exe_name(name: str) -> str:
    """`gh` -> `gh.exe` on Windows, unchanged elsewhere."""
    return f"{name}.exe" if IS_WINDOWS and not name.lower().endswith(".exe") else name


def bundled_or_path(bundled: Path, name: str) -> Path | None:
    """A runtime shipped under runtime/ (`.exe` added on Windows). On macOS/Linux, falls back to `name` on PATH
    (Homebrew etc.); on Windows only the bundled copy is used, as before."""
    p = bundled.with_name(exe_name(bundled.name))
    if p.exists():
        return p
    if IS_WINDOWS:
        return None
    found = shutil.which(name)
    return Path(found) if found else None


def path_basename(p: str) -> str:
    """Last component of a path string from any OS (`D:\\work\\projA` and `/Users/x/projA` both give `projA`)."""
    parts = [s for s in re.split(r"[\\/]+", str(p or "")) if s]
    return parts[-1] if parts else ""


def kill_process_tree(pid: int) -> None:
    """Kill a process and its descendants. POSIX: the whole process group when pid leads one (executors and the
    detached daemon are started in their own session), else just the process."""
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20, **NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
        return
    try:
        if os.getpgid(pid) == pid:
            os.killpg(pid, signal.SIGKILL)
            return
    except OSError:
        pass
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "https_proxy", "http_proxy", "all_proxy")
_proxy_cache: tuple[float, dict[str, str]] | None = None


def parse_scutil_proxy(text: str) -> dict[str, str]:
    kv = dict(re.findall(r"^\s*(\w+)\s*:\s*(\S+)\s*$", text, re.M))
    url = None
    for kind in ("HTTPS", "HTTP"):
        if kv.get(f"{kind}Enable") == "1" and kv.get(f"{kind}Proxy"):
            url = f"http://{kv[f'{kind}Proxy']}:{kv.get(f'{kind}Port', '80')}"
            break
    if not url:
        return {}
    no = "localhost,127.0.0.1,::1"
    return {"HTTPS_PROXY": url, "HTTP_PROXY": url, "https_proxy": url, "http_proxy": url, "NO_PROXY": no, "no_proxy": no}


def system_proxy() -> dict[str, str]:
    """macOS: the System Settings HTTP(S) proxy as env vars, for CLIs that only honour HTTPS_PROXY (GUI apps use it
    automatically). Empty when the environment already sets a proxy, off macOS, or when none is configured."""
    global _proxy_cache
    if not IS_MAC or any(os.environ.get(v) for v in PROXY_VARS):
        return {}
    if _proxy_cache and time.monotonic() - _proxy_cache[0] < 60:
        return _proxy_cache[1]
    try:
        out = subprocess.run(["/usr/sbin/scutil", "--proxy"], capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        out = ""
    _proxy_cache = (time.monotonic(), parse_scutil_proxy(out))
    return _proxy_cache[1]
