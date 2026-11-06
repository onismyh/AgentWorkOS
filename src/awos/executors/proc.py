"""Subprocess plumbing shared by CLI executors: Windows-safe spawn, stdin prompt, line streaming, tee to log
files, timeout/cancel with whole-tree kill, child-env hygiene, npm-shim resolution and error classification."""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

log = logging.getLogger("awos.executors.proc")

IS_WINDOWS = sys.platform == "win32"
_FLAGS = (subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW) if IS_WINDOWS else 0
LineSink = Callable[[str], None]


@dataclass
class ProcResult:
    exit_code: int | None
    timed_out: bool = False
    cancelled: bool = False
    duration_s: float = 0.0
    stderr_tail: str = ""
    pid: int | None = None
    spawn_error: str | None = None


# --------------------------------------------------------------------------- environment / command resolution


# Env vars that look like credentials (tokens, keys, ssh-agent / gh auth ...) are not inherited by agents: each executor
# keeps only the prefixes it needs for its own login (see `keep_secrets`); everything else it needs is passed explicitly.
_SECRETISH = re.compile(r"KEY|TOKEN|SECRET|PASSW|CREDENTIAL|AUTH|COOKIE|SESSION", re.I)


def child_env(extra: dict[str, str] | None = None, *, keep_secrets: tuple[str, ...] = ()) -> dict[str, str]:
    """Inherited env minus our own AWOS_* settings and minus credential-looking variables (except the `keep_secrets`
    prefixes), forced UTF-8 / no colour, then caller overrides."""
    keep = tuple(p.upper() for p in keep_secrets)
    env = {k: v for k, v in os.environ.items()
           if not k.upper().startswith("AWOS_") and (not _SECRETISH.search(k) or k.upper().startswith(keep))}
    env.update(PYTHONUTF8="1", NO_COLOR="1")
    from ..config import load_config
    from ..osutil import system_proxy
    if load_config().get("network.use_system_proxy", True):
        env.update(system_proxy())
    env.update({str(k): str(v) for k, v in (extra or {}).items()})
    return env


_SHIM_TARGET = re.compile(r'"%dp0%\\([^"%]+?\.(?:exe|js|cjs|mjs))"', re.I)


def resolve_command(cmd: str | list[str]) -> list[str] | None:
    """argv prefix for a configured command, bypassing npm .cmd shims (cmd.exe re-parses arguments, which
    breaks JSON / multi-line / CJK args). Returns None if the binary is not found."""
    parts = [cmd] if isinstance(cmd, str) else list(cmd)
    path = shutil.which(parts[0])
    if not path:
        return _runtime_command(parts)
    p = Path(path)
    if p.suffix.lower() in (".cmd", ".bat"):
        target = _npm_shim_target(p)
        if target:
            return target + parts[1:]
    return [str(p)] + parts[1:]


def _runtime_command(parts: list[str]) -> list[str] | None:
    """A CLI installed by `awos setup` under runtime/<name> (not on PATH): run its JS entry with the runtime Node."""
    name = parts[0]
    if os.sep in name or "/" in name:
        return None
    from ..config import load_config
    bin_dir = load_config().root / "runtime" / name / "node_modules" / ".bin"
    for cand in ([bin_dir / f"{name}.cmd"] if IS_WINDOWS else []) + [bin_dir / name]:
        if not cand.exists():
            continue
        if cand.suffix.lower() == ".cmd":
            target = _npm_shim_target(cand)
            return target + parts[1:] if target else None
        real = Path(os.path.realpath(cand))
        if real.suffix.lower() in (".js", ".cjs", ".mjs"):
            from .noderuntime import node_exe
            node = node_exe()
            return [str(node), str(real)] + parts[1:] if node else None
        return [str(real)] + parts[1:]
    return None


def _npm_shim_target(shim: Path) -> list[str] | None:
    try:
        text = shim.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    names = [m for m in _SHIM_TARGET.findall(text) if m.lower() != "node.exe"]
    if not names:
        return None
    target = shim.parent / names[-1].replace("\\", os.sep)
    if not target.exists():
        return None
    if target.suffix.lower() == ".exe":
        return [str(target)]
    node = shim.parent / "node.exe"
    node_path = str(node) if node.exists() else shutil.which("node")
    return [node_path, str(target)] if node_path else None


_version_cache: dict[tuple[str, ...], tuple[float, tuple[bool, str]]] = {}


def version_check(argv: list[str] | None, name: str, ttl_s: float = 600) -> tuple[bool, str]:
    """Cheap `<cli> --version` probe, cached for ttl_s."""
    if not argv:
        return False, f"{name} not found on PATH"
    key = tuple(argv)
    hit = _version_cache.get(key)
    if hit and time.monotonic() - hit[0] < ttl_s:
        return hit[1]
    try:
        cp = subprocess.run(argv + ["--version"], capture_output=True, timeout=30, creationflags=_FLAGS,
                            env=child_env(), stdin=subprocess.DEVNULL)
        out = (cp.stdout or cp.stderr).decode("utf-8", "replace").strip().splitlines()
        res = (cp.returncode == 0, (out[0] if out else f"exit {cp.returncode}")[:200])
    except (OSError, subprocess.TimeoutExpired) as e:
        res = (False, f"{name} --version failed: {e}")
    _version_cache[key] = (time.monotonic(), res)
    return res


# --------------------------------------------------------------------------- Windows job object (belt and braces)


class _Job:
    """Job object with KILL_ON_JOB_CLOSE: descendants die with the run (or with the daemon), even if orphaned."""

    def __init__(self) -> None:
        self.handle = None
        if not IS_WINDOWS:
            return
        try:
            import ctypes
            from ctypes import wintypes

            class Basic(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class Extended(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", Basic), ("IoInfo", ctypes.c_uint64 * 6),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = (ctypes.c_void_p, wintypes.LPCWSTR)
            k32.SetInformationJobObject.argtypes = (wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD)
            k32.AssignProcessToJobObject.argtypes = (wintypes.HANDLE, wintypes.HANDLE)
            k32.TerminateJobObject.argtypes = (wintypes.HANDLE, wintypes.UINT)
            k32.CloseHandle.argtypes = (wintypes.HANDLE,)
            h = k32.CreateJobObjectW(None, None)
            if not h:
                return
            info = Extended()
            info.BasicLimitInformation.LimitFlags = 0x2000 | 0x800  # KILL_ON_JOB_CLOSE | BREAKAWAY_OK
            if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                k32.CloseHandle(h)
                return
            self.k32, self.handle = k32, h
        except Exception:  # pragma: no cover - best effort
            log.debug("job object unavailable", exc_info=True)

    def assign(self, proc: subprocess.Popen) -> None:
        if self.handle and not self.k32.AssignProcessToJobObject(self.handle, int(proc._handle)):  # type: ignore[attr-defined]
            log.debug("AssignProcessToJobObject failed")

    def terminate(self) -> None:
        if self.handle:
            self.k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self.k32.CloseHandle(self.handle)
            self.handle = None


def kill_tree(pid: int) -> None:
    """Kill a process and all its descendants."""
    if IS_WINDOWS:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=20,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        except (OSError, subprocess.TimeoutExpired):
            pass
    else:  # pragma: no cover - POSIX fallback (new session => process group)
        import signal

        try:
            os.killpg(pid, signal.SIGKILL)
        except OSError:
            pass


# --------------------------------------------------------------------------- run


def _pump(stream, sink_file, callback: LineSink | None, tail: deque | None) -> None:
    with sink_file:
        for raw in iter(stream.readline, b""):
            sink_file.write(raw)
            sink_file.flush()
            line = raw.decode("utf-8", "replace").rstrip("\r\n")
            if tail is not None:
                tail.append(line)
            if callback and line:
                try:
                    callback(line)
                except Exception:  # never let a parser bug kill the pump
                    log.exception("line callback failed")
    stream.close()


def run_process(argv: list[str], *, cwd: Path, stdin_text: str | None, timeout_s: float,
                cancel: threading.Event | None = None, log_dir: Path | None = None, env: dict[str, str] | None = None,
                on_stdout: LineSink | None = None, on_stderr: LineSink | None = None,
                on_pid: Callable[[int], None] | None = None, stdout_name: str = "stdout.jsonl",
                stderr_name: str = "stderr.log", keep_secrets: tuple[str, ...] = ()) -> ProcResult:
    """Run argv to completion. stdin_text is written as UTF-8 then stdin is closed; stdout/stderr are streamed
    line by line to the callbacks and tee'd raw to log_dir. Timeout or cancel kills the whole process tree."""
    cancel = cancel or threading.Event()
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
    t0 = time.monotonic()
    try:
        proc = subprocess.Popen(argv, cwd=str(cwd), env=child_env(env, keep_secrets=keep_secrets), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, creationflags=_FLAGS, start_new_session=not IS_WINDOWS)
    except OSError as e:
        return ProcResult(exit_code=None, spawn_error=f"spawn failed: {e}", stderr_tail=str(e))
    job = _Job()
    job.assign(proc)
    if on_pid:
        try:
            on_pid(proc.pid)
        except Exception:
            log.exception("on_pid callback failed")

    tail: deque[str] = deque(maxlen=200)
    out_f = open(log_dir / stdout_name if log_dir else os.devnull, "wb")
    err_f = open(log_dir / stderr_name if log_dir else os.devnull, "wb")
    readers = [threading.Thread(target=_pump, args=(proc.stdout, out_f, on_stdout, None), daemon=True),
               threading.Thread(target=_pump, args=(proc.stderr, err_f, on_stderr, tail), daemon=True)]
    for t in readers:
        t.start()

    def feed() -> None:
        try:
            if stdin_text:
                proc.stdin.write(stdin_text.encode("utf-8"))
            proc.stdin.close()
        except OSError:
            pass

    threading.Thread(target=feed, daemon=True).start()

    timed_out = cancelled = False
    deadline = t0 + timeout_s
    while True:
        try:
            proc.wait(timeout=0.25)
            break
        except subprocess.TimeoutExpired:
            pass
        if cancel.is_set():
            cancelled = True
        elif time.monotonic() > deadline:
            timed_out = True
        if cancelled or timed_out:
            kill_tree(proc.pid)
            job.terminate()
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                log.warning("pid %s did not exit after kill", proc.pid)
            break

    job.terminate()  # reap orphaned descendants still holding our pipes
    job.close()
    for t in readers:
        t.join(timeout=10)
    return ProcResult(exit_code=proc.returncode, timed_out=timed_out, cancelled=cancelled,
                      duration_s=round(time.monotonic() - t0, 3), stderr_tail="\n".join(tail)[-8000:], pid=proc.pid)


# --------------------------------------------------------------------------- results


_BUDGET = re.compile(r"max[_ ]budget|budget[_ ]exhausted|budget exceeded|usage limit|insufficient[_ ]quota|"
                     r"quota exceeded|exceeded your current quota|credit balance is too low|out of credits", re.I)
_AUTH = re.compile(r"\b401\b|\b403\b|unauthori[sz]ed|forbidden|not logged in|please (?:run )?/?login|log ?in again|"
                   r"invalid (?:api[ _-]?key|x-api-key|token|bearer)|authentication(?:_error| failed| required)|"
                   r"token (?:has )?expired|refresh token|permission_error", re.I)
_REQUEST = re.compile(r"invalid_json_schema|invalid_request_error|invalid schema|unknown option|unexpected argument|"
                      r"unrecognized (?:option|argument)|error: unexpected|model .{0,40}not (?:found|exist|supported)|"
                      r"not_found_error", re.I)
_TRANSIENT = re.compile(r"\b429\b|rate[_ -]?limit|too many requests|overloaded|\b5\d\d\b|internal server error|"
                        r"bad gateway|service unavailable|gateway time-?out|timed? ?out|econnreset|econnrefused|"
                        r"etimedout|enotfound|eai_again|socket hang up|connection (?:reset|refused|error|closed|aborted)|"
                        r"network|stream (?:disconnected|error)|temporar|try again|api_error|upstream", re.I)


def classify_error(text: str, exit_code: int | None = None) -> str:
    """Failure class for retry policy: budget | permanent | transient. (Timeout/cancel are decided by callers.)"""
    t = text or ""
    if _BUDGET.search(t):
        return "budget"
    if _AUTH.search(t) or _REQUEST.search(t):
        return "permanent"
    if _TRANSIENT.search(t):
        return "transient"
    if exit_code in (126, 127) or "spawn failed" in t or "not found on PATH" in t:
        return "permanent"
    return "transient"


_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.S)


def extract_json(text: str | None) -> dict | None:
    """Parse a JSON object from a final message: whole text, else last ```json fence, else outermost {...}."""
    if not text:
        return None
    s = text.strip()
    candidates = [s] + list(reversed(_FENCE.findall(s)))
    a, b = s.find("{"), s.rfind("}")
    if 0 <= a < b:
        candidates.append(s[a:b + 1])
    for c in candidates:
        try:
            v = json.loads(c)
        except ValueError:
            continue
        if isinstance(v, dict):
            return v
    return None


def trim(value, limit: int = 2000):
    """Shorten long strings for event payloads (raw output stays in stdout.jsonl)."""
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + f"…[+{len(value) - limit} chars]"
    return value
