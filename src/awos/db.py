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
    conn.execute("PRAGMA synchronous=NORMAL")
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
        finally:
            conn.close()
        _initialized.add(key)


def get_conn() -> sqlite3.Connection:
    path = load_config().db_path
    conns: dict[str, sqlite3.Connection] = getattr(_local, "conns", None) or {}
    _local.conns = conns
    key = str(path)
    if key not in conns:
        init_db(path)
        conns[key] = _connect(path)
    return conns[key]


def close_thread_conns() -> None:
    for c in (getattr(_local, "conns", None) or {}).values():
        try:
            c.close()
        except sqlite3.Error:
            pass
    _local.conns = {}


class tx:
    """`with tx() as c:` -> BEGIN IMMEDIATE ... COMMIT/ROLLBACK (serializes writers)."""

    def __enter__(self) -> sqlite3.Connection:
        self.c = get_conn()
        self.c.execute("BEGIN IMMEDIATE")
        return self.c

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type is None:
            self.c.execute("COMMIT")
        else:
            self.c.execute("ROLLBACK")


def q(sql: str, params: Iterable[Any] = ()) -> list[dict]:
    return [dict(r) for r in get_conn().execute(sql, tuple(params)).fetchall()]


def q1(sql: str, params: Iterable[Any] = ()) -> dict | None:
    r = get_conn().execute(sql, tuple(params)).fetchone()
    return dict(r) if r else None


def execute(sql: str, params: Iterable[Any] = ()) -> int:
    cur = get_conn().execute(sql, tuple(params))
    return cur.rowcount


def insert(table: str, row: dict[str, Any], conn: sqlite3.Connection | None = None, or_ignore: bool = False) -> int:
    cols = list(row.keys())
    sql = f"INSERT {'OR IGNORE ' if or_ignore else ''}INTO {table} ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})"
    cur = (conn or get_conn()).execute(sql, tuple(row[c] for c in cols))
    return cur.rowcount


def update(table: str, key: str, key_value: Any, fields: dict[str, Any], conn: sqlite3.Connection | None = None) -> int:
    if not fields:
        return 0
    sets = ", ".join(f"{k} = ?" for k in fields)
    cur = (conn or get_conn()).execute(f"UPDATE {table} SET {sets} WHERE {key} = ?", (*fields.values(), key_value))
    return cur.rowcount


def backup(dest: Path) -> Path:
    """Online consistent backup using the SQLite backup API."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    target = sqlite3.connect(str(dest))
    try:
        get_conn().backup(target)
    finally:
        target.close()
    return dest
