"""SQLite access: one connection per thread, WAL mode, schema bootstrap, small helpers."""
from __future__ import annotations

import json
import secrets
import sqlite3
import threading
import time
from datetime import datetime, timezone
from importlib import resources
from pathlib import Path
from typing import Any, Iterable

from .config import load_config

SCHEMA_VERSION = 1
_local = threading.local()
_init_lock = threading.Lock()
_initialized: set[str] = set()


def utcnow() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_ts(s: str | None) -> datetime | None:
    if not s:
        return None
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def ts_after(seconds: float) -> str:
    return datetime.fromtimestamp(time.time() + seconds, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_id(prefix: str) -> str:
    """Sortable id: <prefix>_<yyyymmddHHMMSS><6 hex>."""
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%y%m%d%H%M%S')}{secrets.token_hex(3)}"


def dumps(obj: Any) -> str | None:
    if obj is None:
        return None
    return json.dumps(obj, ensure_ascii=False, default=str)


def loads(s: str | None, default: Any = None) -> Any:
    if not s:
        return default
    try:
        return json.loads(s)
    except (TypeError, ValueError):
        return default


def _connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), timeout=30, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def init_db(path: Path) -> None:
    key = str(path.resolve())
    with _init_lock:
        if key in _initialized:
            return
        conn = _connect(path)
        try:
            ddl = resources.files("awos").joinpath("schema.sql").read_text(encoding="utf-8")
            conn.executescript(ddl)
            row = conn.execute("SELECT version FROM schema_version").fetchone()
            if row is None:
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (SCHEMA_VERSION,))
            elif row["version"] > SCHEMA_VERSION:
                raise RuntimeError(f"database schema v{row['version']} is newer than this code (v{SCHEMA_VERSION})")
        finally:
            conn.close()
        _initialized.add(key)


# One connection per database file shared by all threads, serialized by a re-entrant lock.
# This avoids multi-connection WAL write/checkpoint races (see SQLite WAL-reset advisory for 3.50.x) and lets
# transactions nest: inner tx() blocks simply join the outer transaction.
_conns: dict[str, sqlite3.Connection] = {}
_conns_lock = threading.Lock()
LOCK = threading.RLock()


def get_conn() -> sqlite3.Connection:
    path = load_config().db_path
    key = str(path)
    conn = _conns.get(key)
    if conn is None:
        with _conns_lock:
            conn = _conns.get(key)
            if conn is None:
                init_db(path)
                conn = _connect(path)
                _conns[key] = conn
    return conn


def close_thread_conns() -> None:
    """Close all shared connections (tests / shutdown)."""
    with _conns_lock, LOCK:
        for c in _conns.values():
            try:
                c.close()
            except sqlite3.Error:
                pass
        _conns.clear()


def after_commit(fn) -> None:
    """Run fn after the current transaction commits (immediately when not inside tx())."""
    pending = getattr(_local, "after_commit", None)
    if getattr(_local, "depth", 0) > 0 and pending is not None:
        pending.append(fn)
    else:
        fn()


class tx:
    """`with tx() as c:` -> BEGIN IMMEDIATE ... COMMIT/ROLLBACK. Re-entrant: nested blocks join the outer tx."""

    def __enter__(self) -> sqlite3.Connection:
        LOCK.acquire()
        self.c = get_conn()
        depth = getattr(_local, "depth", 0)
        if depth == 0:
            _local.after_commit = []
            try:
                self.c.execute("BEGIN IMMEDIATE")
            except BaseException:
                LOCK.release()
                raise
        _local.depth = depth + 1
        return self.c

    def __exit__(self, exc_type, exc, tb) -> None:
        _local.depth -= 1
        try:
            if _local.depth == 0:
                callbacks = _local.after_commit or []
                _local.after_commit = None
                if exc_type is None:
                    try:
                        self.c.execute("COMMIT")
                    except BaseException:
                        try:
                            self.c.execute("ROLLBACK")
                        except sqlite3.Error:
                            pass
                        raise
                    for fn in callbacks:
                        try:
                            fn()
                        except Exception:  # pragma: no cover - callbacks are best effort (bus)
                            pass
                else:
                    try:
                        self.c.execute("ROLLBACK")
                    except sqlite3.Error:
                        pass
        finally:
            LOCK.release()


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    with LOCK:
        return [dict(r) for r in get_conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    with LOCK:
        r = get_conn().execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    with LOCK:
        return get_conn().execute(sql, tuple(params)).rowcount


def insert(table: str, row: dict[str, Any], conn: sqlite3.Connection | None = None, or_ignore: bool = False) -> int:
    cols = list(row.keys())
    sql = f"INSERT {'OR IGNORE ' if or_ignore else ''}INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    with LOCK:
        return (conn or get_conn()).execute(sql, tuple(row[c] for c in cols)).rowcount


def update(table: str, key: str, key_value: Any, fields: dict[str, Any], conn: sqlite3.Connection | None = None) -> int:
    if not fields:
        return 0
    sets = ", ".join(f"{k} = ?" for k in fields)
    with LOCK:
        return (conn or get_conn()).execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*fields.values(), key_value)).rowcount


def backup(dest: Path) -> Path:
    """Online consistent backup using the SQLite backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        with LOCK:
            get_conn().backup(target)
    finally:
        target.close()
    return dest


def sqlite_version() -> str:
    return sqlite3.sqlite_version
