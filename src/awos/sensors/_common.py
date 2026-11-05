"""Shared sensor helpers: time buckets (user's local zone), redaction, read-only SQLite, self-exclusion."""
from __future__ import annotations

import os
import re
import shutil
import sqlite3
import tempfile
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from ..config import ROOT
from ..timeutil import local_now, tz

# Directory names never worth descending into (build/env/cache/VCS internals).
SKIP_DIRS = frozenset({
    ".git", ".hg", ".svn", "node_modules", ".venv", "venv", "__pycache__", "site-packages",
    ".ipynb_checkpoints", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".tox", ".idea", ".vscode",
    ".cache", "$RECYCLE.BIN", "System Volume Information",
})


def norm(p: str | Path) -> str:
    s = str(p)
    if s.startswith("\\\\?\\"):
        s = s[4:]
    return os.path.normcase(os.path.normpath(s))


def is_under(path: str | Path, base: str | Path) -> bool:
    p, b = norm(path), norm(base)
    return p == b or p.startswith(b.rstrip("\\/") + os.sep)


def _self_roots() -> tuple[str, ...]:
    """AgentWorkOS root plus any configured data dirs that live elsewhere (runs/workspaces/...). No mkdir side effects."""
    out = {norm(ROOT)}
    try:
        from ..config import load_config
        cfg = load_config()
        for key in ("var", "runs", "workspaces", "artifacts", "inbox_drop"):
            raw = cfg.get(f"paths.{key}")
            if raw:
                p = Path(raw)
                out.add(norm(p if p.is_absolute() else cfg.root / p))
    except Exception:  # pragma: no cover - config problems must not break sensing
        pass
    return tuple(sorted(out))


def self_excluded(path: str | Path) -> bool:
    """True for AgentWorkOS' own tree (code, var/, runtime/, external/ clones, workspaces): sensing it would
    make the system observe its own generated output."""
    p = norm(path)
    return any(p == e or p.startswith(e + os.sep) for e in _self_roots())


# --------------------------------------------------------------------------- time (user's zone, not the system clock's)

def iso_week(dt: datetime | None = None) -> str:
    y, w, _ = (dt or local_now()).isocalendar()
    return f"{y}-W{w:02d}"


def utc_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def local_dt(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, tz())


def local_str(ts: float) -> str:
    return local_dt(ts).strftime("%Y-%m-%d %H:%M")


def parse_iso(s: str | None) -> float | None:
    """ISO-8601 (with Z/offset, optional fraction) -> epoch seconds."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.strip())  # py>=3.11: handles Z and 7-digit fractions (Codex)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return None


# --------------------------------------------------------------------------- text hygiene

_SECRET_RE = re.compile(
    r"(sk-[A-Za-z0-9_\-]{16,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[abpr]-[A-Za-z0-9-]{10,}"
    r"|AKIA[0-9A-Z]{16}|eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{6,}"
    r"|(?i:\b(?:password|passwd|pwd|token|secret|api[_-]?key|access[_-]?key|authorization|bearer)\b\s*[:=：]?\s*\S{6,})"
    r"|(?:授权码|密码|口令|密钥)\s*[:=：是为]?\s*\S{4,})"
)
_LONG_TOKEN_RE = re.compile(r"[A-Za-z0-9+/]{32,}={0,2}")
_EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE_RE = re.compile(r"(?<!\d)(?:\+?86[- ]?)?1[3-9]\d{9}(?!\d)")


def redact(s: str | None) -> str:
    """Mask obvious credentials and long high-entropy tokens."""
    if not s:
        return ""
    s = _SECRET_RE.sub("[REDACTED]", s)
    return _LONG_TOKEN_RE.sub(lambda m: "[REDACTED]" if (re.search(r"\d", m.group()) and re.search(r"[A-Za-z]", m.group()))
                              else m.group(), s)


def scrub_pii(s: str | None) -> str:
    """redact() + mask e-mail addresses and phone numbers (third-party text such as mail previews)."""
    return _PHONE_RE.sub("<phone>", _EMAIL_RE.sub("<email>", redact(s)))


def trunc(s: str | None, n: int) -> str:
    s = re.sub(r"\s+", " ", s or "").strip()
    return s if len(s) <= n else s[: max(0, n - 1)].rstrip() + "…"


# --------------------------------------------------------------------------- read-only sqlite

@contextmanager
def sqlite_ro(path: str | Path) -> Iterator[sqlite3.Connection]:
    """Open another app's SQLite strictly read-only: mode=ro, then immutable=1 (app holds an exclusive lock),
    then a private temp copy as last resort. Never writes to the source."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(str(path))
    conn: sqlite3.Connection | None = None
    tmpdir: str | None = None
    for q in ("mode=ro", "mode=ro&immutable=1"):
        try:
            conn = sqlite3.connect(f"{path.as_uri()}?{q}", uri=True, timeout=2)
            conn.execute("SELECT count(*) FROM sqlite_master").fetchone()
            break
        except sqlite3.Error:
            if conn is not None:
                conn.close()
            conn = None
    if conn is None:
        tmpdir = tempfile.mkdtemp(prefix="awos-sensor-")
        dst = Path(tmpdir) / path.name
        shutil.copy2(path, dst)
        wal = path.with_name(path.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, dst.with_name(dst.name + "-wal"))
        conn = sqlite3.connect(str(dst), timeout=2)  # private copy: rw is harmless and lets sqlite replay the WAL
    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
