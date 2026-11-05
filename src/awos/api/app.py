"""HTTP API + SSE + static dashboard + signed approval links (FastAPI).

Security (docs/architecture.md §10): bind to 127.0.0.1, reject foreign Host headers (DNS rebinding guard) and
require `X-AWOS-Token` (var/api_token) on every mutating /api/* request. The dashboard receives the token through
a <meta> tag injected into index.html; `/approve/{token}` is authorized by its HMAC-signed token instead.
All human operations go through `engine.actions`; everything else here is read-only.
"""
from __future__ import annotations

import asyncio
import hmac
import html
import importlib
import json
import mimetypes
import os
import queue
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import parse_qs

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from starlette.concurrency import run_in_threadpool

from .. import __version__, repo
from ..brain import summarize, topics as topics_mod
from ..bus import bus
from . import board as board_mod
from ..config import load_config
from ..db import loads, parse_ts, q, q1, utcnow
from ..engine import actions
from ..models import CATEGORIES, EXECUTORS, KINDS, LEVELS, SENSITIVE_ACTIONS, TRANSITIONS, ApprovalStatus, RunStatus, TaskStatus
from ..repo import InvalidTransition

DASHBOARD_DIR = Path(__file__).resolve().parent.parent / "dashboard"
STARTED_AT = time.time()
TOKEN_PLACEHOLDER = "__AWOS_TOKEN__"

# Windows' registry may map .js to text/plain, which breaks <script type="module">.
mimetypes.add_type("text/javascript", ".js")
mimetypes.add_type("text/css", ".css")
mimetypes.add_type("image/svg+xml", ".svg")

TASK_STATUS_LABELS = {
    "inbox": "待分拣", "triaged": "建议", "ready": "排队中", "blocked": "等待子任务", "running": "运行中",
    "waiting_approval": "待审批", "waiting_user": "待接手", "review": "待验收", "done": "已完成", "failed": "失败",
    "cancelled": "已取消", "dismissed": "已驳回", "snoozed": "已推迟",
}
RUN_STATUS_LABELS = {"queued": "排队", "running": "运行中", "succeeded": "成功", "failed": "失败", "timeout": "超时",
                     "cancelled": "已取消", "interrupted": "中断"}
APPROVAL_STATUS_LABELS = {"pending": "待决策", "approved": "已批准", "rejected": "已拒绝", "expired": "已过期",
                          "cancelled": "已取消", "executing": "执行中", "executed": "已执行", "failed": "执行失败"}
APPROVAL_KIND_LABELS = {"action": "动作", "decision": "决策", "review": "评审", "permission": "权限", "budget": "预算"}
FACT_KINDS = {"role": "角色", "project": "项目", "goal": "目标", "interest": "兴趣", "habit": "习惯",
              "preference": "偏好", "person": "人物", "deadline": "截止日期", "note": "备注"}
TASK_ACTIONS = ("accept", "retry", "rework", "complete", "cancel", "dismiss", "snooze")

# Artifact suffixes returned as text (anything else is streamed as a file).
TEXT_TYPES = {
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain", ".log": "text/plain",
    ".json": "application/json", ".jsonl": "text/plain", ".html": "text/html", ".htm": "text/html",
    ".csv": "text/csv", ".tsv": "text/tab-separated-values", ".tex": "text/plain", ".bib": "text/plain",
    ".py": "text/plain", ".yaml": "text/plain", ".yml": "text/plain", ".toml": "text/plain", ".xml": "text/plain",
    ".diff": "text/plain", ".patch": "text/plain", ".ps1": "text/plain", ".sh": "text/plain", ".sql": "text/plain",
}
MAX_TEXT_BYTES = 4 << 20
# Served artifacts are agent-generated (possibly derived from emails/web pages): render them in an opaque,
# script-less origin so they can never reach the API or the dashboard token.
SANDBOX_CSP = "sandbox; default-src 'none'; img-src data:; style-src 'unsafe-inline'"
DASHBOARD_CSP = ("default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; script-src 'self'; "
                 "connect-src 'self'; frame-src 'self'; object-src 'none'; base-uri 'none'; form-action 'self'; "
                 "frame-ancestors 'none'")
APPROVE_CSP = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"

# --------------------------------------------------------------------------- token / security


def load_api_token() -> str:
    """Return the dashboard/API token from var/api_token, creating it on first start."""
    path = load_config().path("var") / "api_token"
    if path.exists():
        tok = path.read_text(encoding="utf-8").strip()
        if tok:
            return tok
    tok = secrets.token_urlsafe(32)
    path.write_text(tok, encoding="utf-8")
    return tok


def allowed_hosts(port: int, extra: Iterable[str] = ()) -> set[str]:
    hosts = {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}", "testserver"}
    hosts |= {str(h) for h in (load_config().get("server.allowed_hosts") or [])}
    hosts |= set(extra)
    return {h.lower() for h in hosts}


class SecurityGuard:
    """Pure ASGI middleware (safe for streaming): Host allow-list + token on mutating /api/* requests."""

    MUTATING = {"POST", "PUT", "PATCH", "DELETE"}

    def __init__(self, app: Any, *, hosts: set[str], token: str) -> None:
        self.app, self.hosts, self.token = app, hosts, token.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers") or [])
        host = headers.get(b"host", b"").decode("latin-1").lower()
        if host not in self.hosts:
            await JSONResponse({"detail": f"host {host!r} not allowed"}, 403)(scope, receive, send)
            return
        if (scope["method"] in self.MUTATING and scope["path"].startswith("/api/")) or \
                (scope["method"] == "GET" and self._sensitive_get(scope["path"])):
            if not hmac.compare_digest(headers.get(b"x-awos-token", b""), self.token):
                await JSONResponse({"detail": "missing or invalid X-AWOS-Token"}, 401)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    @staticmethod
    def _sensitive_get(path: str) -> bool:
        """Raw mail text (source evidence, mail reader) needs the token even for GET."""
        return path.startswith("/api/mail/") or (path.startswith("/api/tasks/") and path.endswith("/evidence"))


# --------------------------------------------------------------------------- request bodies


class TaskCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str | None = Field(None, max_length=50000)
    level: int | None = Field(None, ge=1, le=5)
    category: str | None = None
    priority: int = Field(2, ge=0, le=3)
    kind: str = "todo"
    executor: str | None = None
    project: str | None = None
    due_at: str | None = None
    triage: bool = True


class TaskActionBody(BaseModel):
    note: str | None = Field(None, max_length=20000)
    feedback: str | None = Field(None, max_length=20000)
    hours: float | None = Field(None, gt=0, le=24 * 365)


class TaskPatch(BaseModel):
    level: int | None = Field(None, ge=1, le=5)
    priority: int | None = Field(None, ge=0, le=3)
    category: str | None = None
    kind: str | None = None
    executor: str | None = None
    title: str | None = Field(None, min_length=1, max_length=300)
    description: str | None = Field(None, max_length=50000)
    due_at: str | None = None
    project: str | None = None
    acceptance: str | None = Field(None, max_length=20000)


class DecisionBody(BaseModel):
    option: str = Field(min_length=1, max_length=100)
    note: str | None = Field(None, max_length=20000)


class FactBody(BaseModel):
    kind: str = Field(pattern=r"^[a-z_]{1,32}$")
    key: str = Field(min_length=1, max_length=200)
    value: str = Field(min_length=1, max_length=8000)


# --------------------------------------------------------------------------- small helpers


def _csv(s: str | None) -> list[str]:
    return [p.strip() for p in (s or "").split(",") if p.strip()]


def _in(values: list) -> str:
    return ",".join("?" for _ in values)


def _clamp(n: int | None, lo: int = 1, hi: int = 1000, default: int = 200) -> int:
    return max(lo, min(hi, int(n if n is not None else default)))


def _require(obj: dict | None, ident: str) -> dict:
    if obj is None:
        raise KeyError(ident)
    return obj


def _ts(s: Any) -> datetime | None:
    if not s or not isinstance(s, str):
        return None
    try:
        return parse_ts(s)
    except ValueError:
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _duration(start: str | None, end: str | None, running: bool = False) -> float | None:
    s = _ts(start)
    e = _ts(end) or (datetime.now(timezone.utc) if running else None)
    return round((e - s).total_seconds(), 1) if s and e else None


def _local_midnight_utc() -> str:
    return _iso(datetime.now().astimezone().replace(hour=0, minute=0, second=0, microsecond=0))


def _cut(s: Any, n: int) -> Any:
    return s[:n] + "…" if isinstance(s, str) and len(s) > n else s


def _inside(path: Path, root: Path) -> bool:
    try:
        a, b = os.path.normcase(str(path.resolve())), os.path.normcase(str(root.resolve()))
        return os.path.commonpath([a, b]) == b
    except (OSError, ValueError):
        return False


def _var_dir() -> Path:
    return load_config().path("var").resolve()


def _eff_level(t: dict) -> int:
    lvl = int(t.get("level") or 1)
    cap = t.get("level_cap")
    return min(lvl, int(cap)) if cap else lvl


# --------------------------------------------------------------------------- shaping rows for the UI


def _brief_task(t: dict | None, text_limit: int = 600) -> dict | None:
    if t is None:
        return None
    out = {k: v for k, v in t.items() if k not in ("plan", "result", "meta")}
    for k in ("description", "summary", "acceptance"):
        out[k] = _cut(out.get(k), text_limit)
    meta = t.get("meta") if isinstance(t.get("meta"), dict) else {}
    out["notes_count"] = len(meta.get("notes") or [])
    out["effective_level"] = _eff_level(t)
    return out


def _brief_run(r: dict) -> dict:
    out = {k: v for k, v in r.items() if k not in ("final_text", "structured", "command")}
    out["error"] = _cut(out.get("error"), 600)
    out["duration_s"] = _duration(r.get("started_at"), r.get("ended_at"), running=r.get("status") == RunStatus.RUNNING)
    return out


def _task_index(ids: Iterable[str | None]) -> dict[str, dict]:
    uniq = sorted({i for i in ids if i})
    if not uniq:
        return {}
    rows = q(f"SELECT id, title, status, level, level_cap, category, priority, kind, parent_id FROM tasks "
             f"WHERE id IN ({_in(uniq)})", uniq)
    return {r["id"]: r for r in rows}


def _attach_task(rows: list[dict], key: str = "task_id") -> list[dict]:
    idx = _task_index(r.get(key) for r in rows)
    for r in rows:
        t = idx.get(r.get(key) or "")
        r["task_title"] = t["title"] if t else None
        r["task_status"] = t["status"] if t else None
        r["task_level"] = _eff_level(t) if t else None
        r["task_category"] = t["category"] if t else None
    return rows


def _attach_task_context(tasks: list[dict]) -> list[dict]:
    """Child progress, pending approvals and parent title for a list of (brief) tasks."""
    ids = [t["id"] for t in tasks]
    if not ids:
        return tasks
    prog = {r["parent_id"]: r for r in q(
        f"SELECT parent_id, COUNT(*) n, SUM(status = 'done') done FROM tasks WHERE parent_id IN ({_in(ids)}) "
        f"GROUP BY parent_id", ids)}
    pend = {r["task_id"]: r["n"] for r in q(
        f"SELECT task_id, COUNT(*) n FROM approvals WHERE status = 'pending' AND task_id IN ({_in(ids)}) "
        f"GROUP BY task_id", ids)}
    parents = _task_index(t.get("parent_id") for t in tasks)
    for t in tasks:
        p = prog.get(t["id"])
        t["children_total"] = p["n"] if p else 0
        t["children_done"] = int(p["done"] or 0) if p else 0
        t["pending_approvals"] = pend.get(t["id"], 0)
        par = parents.get(t.get("parent_id") or "")
        t["parent_title"] = par["title"] if par else None
    return tasks


def _decorate_artifacts(rows: list[dict]) -> list[dict]:
    var = _var_dir()
    for a in rows:
        p = Path(a["path"]) if a.get("path") else None
        ext = p.suffix.lower() if p else ""
        a["ext"] = ext
        a["exists"] = bool(p and p.is_file())
        a["servable"] = bool(a["exists"] and _inside(p, var))  # type: ignore[arg-type]
        a["previewable"] = bool(a["servable"] and ext in TEXT_TYPES and (a.get("bytes") or 0) <= MAX_TEXT_BYTES)
    return rows


def _attach_artifacts(tasks: list[dict]) -> None:
    ids = [t["id"] for t in tasks]
    by: dict[str, list] = {}
    if ids:
        for a in _decorate_artifacts(q(f"SELECT * FROM artifacts WHERE task_id IN ({_in(ids)}) ORDER BY created_at DESC", ids)):
            by.setdefault(a["task_id"], []).append(a)
    for t in tasks:
        t["artifacts"] = by.get(t["id"], [])


def _attach_last_error(tasks: list[dict]) -> None:
    ids = [t["id"] for t in tasks]
    last: dict[str, dict] = {}
    if ids:
        for r in q(f"SELECT id, task_id, status, error, error_class, executor, ended_at FROM runs "
                   f"WHERE task_id IN ({_in(ids)}) AND error IS NOT NULL ORDER BY COALESCE(ended_at, started_at) DESC", ids):
            last.setdefault(r["task_id"], {**r, "error": _cut(r["error"], 1500)})
    for t in tasks:
        t["last_error"] = last.get(t["id"])


def _status_counts() -> dict[str, int]:
    counts = {s.value: 0 for s in TaskStatus}
    for r in q("SELECT status, COUNT(*) n FROM tasks GROUP BY status"):
        counts[r["status"]] = r["n"]
    return counts


def _inbox_counts() -> dict[str, int]:
    a = q1("SELECT COALESCE(SUM(kind = 'decision'), 0) d, COALESCE(SUM(kind != 'decision'), 0) o "
           "FROM approvals WHERE status = 'pending'") or {"d": 0, "o": 0}
    c = _status_counts()
    out = {"decisions": int(a["d"]), "actions": int(a["o"]), "review": c["review"], "handoff": c["waiting_user"],
           "suggested": c["triaged"], "failed": c["failed"]}
    out["tasks_total"] = out["review"] + out["handoff"] + out["suggested"] + out["failed"]
    out["approvals_total"] = out["decisions"] + out["actions"]
    out["total"] = out["tasks_total"] + out["approvals_total"]
    return out


def _tree(task_id: str, depth: int = 0, seen: set[str] | None = None) -> list[dict]:
    seen = seen if seen is not None else {task_id}
    nodes = []
    for ch in repo.children(task_id):
        if ch["id"] in seen:
            continue
        seen.add(ch["id"])
        node = _brief_task(ch, 300) or {}
        node["deps"] = repo.dependencies(ch["id"])
        node["children"] = _tree(ch["id"], depth + 1, seen) if depth < 8 else []
        nodes.append(node)
    return nodes


def _decode_events(rows: list[dict]) -> list[dict]:
    for r in rows:
        r["data"] = loads(r.get("data"), r.get("data"))
    return rows


def _task_timeline(task_id: str, runs: list[dict], approvals: list[dict], artifacts: list[dict], limit: int = 400) -> list[dict]:
    conds, params = ["(entity_type = 'task' AND entity_id = ?)"], [task_id]
    for etype, ids in (("run", [r["id"] for r in runs]), ("approval", [a["id"] for a in approvals]),
                       ("artifact", [a["id"] for a in artifacts])):
        if ids:
            conds.append(f"(entity_type = ? AND entity_id IN ({_in(ids)}))")
            params += [etype, *ids]
    return _decode_events(q(f"SELECT * FROM events WHERE {' OR '.join(conds)} ORDER BY id DESC LIMIT ?", [*params, limit]))


def _attach_event_titles(events: list[dict]) -> list[dict]:
    lookups = {"task": "SELECT id, title FROM tasks", "approval": "SELECT id, title FROM approvals",
               "artifact": "SELECT id, title FROM artifacts", "signal": "SELECT id, title FROM signals",
               "context": "SELECT id, kind || ' · ' || key AS title FROM context_facts",
               "run": "SELECT id, purpose || ' · ' || executor AS title FROM runs"}
    by_type: dict[str, set[str]] = {}
    for e in events:
        if e.get("entity_id") and e["entity_type"] in lookups:
            by_type.setdefault(e["entity_type"], set()).add(e["entity_id"])
    titles: dict[tuple[str, str], str] = {}
    for etype, ids in by_type.items():
        lst = sorted(ids)
        for r in q(f"{lookups[etype]} WHERE id IN ({_in(lst)})", lst):
            titles[(etype, r["id"])] = r["title"]
    for e in events:
        e["entity_title"] = titles.get((e["entity_type"], e.get("entity_id") or ""))
        if e["entity_type"] == "sensor":
            e["entity_title"] = e.get("entity_id")
    return events


# --------------------------------------------------------------------------- health / overview pieces

_exec_cache: dict[str, Any] = {"at": 0.0, "data": None}
_exec_lock = threading.Lock()


def executors_status(max_age: float = 60.0) -> dict[str, dict]:
    """llm.available_executors() cached for `max_age` seconds (it may spawn `--version` subprocesses)."""
    with _exec_lock:
        if _exec_cache["data"] is not None and time.monotonic() - _exec_cache["at"] < max_age:
            return _exec_cache["data"]
    try:
        from .. import llm
        data = {name: {"ok": bool(v[0]), "detail": str(v[1])} for name, v in llm.available_executors().items()}
    except Exception as e:  # never let a broken adapter take the health endpoint down
        data = {"_registry": {"ok": False, "detail": f"{type(e).__name__}: {e}"}}
    cfg = load_config()
    for name in EXECUTORS:
        if name not in data and name != "internal" and cfg.get(f"executors.{name}") is not None:
            data[name] = {"ok": False, "detail": "adapter not registered"
                          if cfg.get(f"executors.{name}.enabled") else "disabled in config"}
    with _exec_lock:
        _exec_cache.update(at=time.monotonic(), data=data)
    return data


def _daemon_status() -> dict:
    hb = repo.kv_get("daemon.heartbeat")
    raw_ts: Any = hb
    if isinstance(hb, dict):
        raw_ts = hb.get("ts") or hb.get("at") or hb.get("time")
    if isinstance(raw_ts, (int, float)):
        dt: datetime | None = datetime.fromtimestamp(raw_ts, timezone.utc)
    else:
        dt = _ts(raw_ts)
    age = (datetime.now(timezone.utc) - dt).total_seconds() if dt else None
    stale_after = max(3 * float(load_config().get("scheduler.tick_seconds", 15) or 15), 90.0)
    return {"heartbeat": hb, "ts": _iso(dt) if dt else None, "age_s": round(age, 1) if age is not None else None,
            "alive": age is not None and age < stale_after, "stale_after_s": stale_after}


def _budget() -> dict:
    cfg = load_config()
    today = repo.cost_today()
    return {"today_usd": round(float(today["usd"] or 0), 4), "today_runs": today["runs"], "today_tokens": today["tokens"],
            "daily_usd": float(cfg.get("budget.daily_usd", 8.0)), "per_task_usd": float(cfg.get("budget.per_task_usd", 3.0)),
            "codex_runs": repo.cost_today("codex")["runs"], "codex_daily_runs": int(cfg.get("budget.codex_daily_runs", 60))}


def _sensors(include_cursor: bool = False) -> list[dict]:
    cfg = load_config().section("sensors")
    states = {s["name"]: s for s in repo.list_sensor_states()}
    try:
        from ..sensors import all_sensors
        registry = all_sensors()
    except Exception:  # pragma: no cover - a broken sensor module must not break the dashboard
        registry = {}
    counts = {r["source"]: r for r in q("SELECT source, COUNT(*) n, MAX(observed_at) last FROM signals GROUP BY source")}
    out = []
    for name in sorted(set(cfg) | set(states) | set(registry)):
        c = cfg.get(name) if isinstance(cfg.get(name), dict) else {}
        st = states.get(name) or {}
        item = {
            "name": name, "registered": name in registry, "config_enabled": bool(c.get("enabled", False)),
            "enabled": bool(c.get("enabled", False)) and bool(st.get("enabled", 1)),
            "interval_minutes": c.get("interval_minutes"), "active_hours": c.get("active_hours"),
            "last_run_at": st.get("last_run_at"), "last_ok_at": st.get("last_ok_at"), "next_run_at": st.get("next_run_at"),
            "last_error": st.get("last_error"), "consecutive_failures": st.get("consecutive_failures") or 0,
            "last_count": st.get("last_count") or 0, "signals_total": (counts.get(name) or {}).get("n", 0),
            "last_signal_at": (counts.get(name) or {}).get("last"),
        }
        if include_cursor:
            item["cursor"] = st.get("cursor")
        out.append(item)
    return out


def _cost_days(days: int = 14) -> dict:
    start = datetime.now().date() - timedelta(days=days - 1)
    rows = q("SELECT day, executor, COALESCE(SUM(cost_usd), 0) usd, COUNT(*) runs, COALESCE(SUM(tokens_in + tokens_out), 0) tokens "
             "FROM cost_ledger WHERE day >= ? GROUP BY day, executor", (start.isoformat(),))
    order = {name: i for i, name in enumerate(EXECUTORS)}
    execs = sorted({r["executor"] for r in rows}, key=lambda n: (order.get(n, 99), n))
    out = []
    for i in range(days):
        day = (start + timedelta(days=i)).isoformat()
        by = {r["executor"]: {"usd": round(r["usd"], 4), "runs": r["runs"], "tokens": r["tokens"]} for r in rows if r["day"] == day}
        out.append({"day": day, "usd": round(sum(v["usd"] for v in by.values()), 4),
                    "runs": sum(v["runs"] for v in by.values()), "tokens": sum(v["tokens"] for v in by.values()),
                    "by_executor": by})
    return {"days": out, "executors": execs}


def _errors(limit: int = 100) -> dict:
    runs = [_brief_run(r) for r in q("SELECT * FROM runs WHERE status IN ('failed', 'timeout', 'interrupted') "
                                     "ORDER BY COALESCE(ended_at, started_at, '') DESC, id DESC LIMIT ?", (limit,))]
    tasks = [_brief_task(t) for t in repo.list_tasks(status="failed", limit=limit)]
    _attach_last_error(tasks)  # type: ignore[arg-type]
    return {
        "runs": _attach_task(runs),
        "tasks": tasks,
        "sensors": [s for s in _sensors() if s["last_error"]],
        "approvals": _attach_task(repo.list_approvals(status=ApprovalStatus.FAILED, limit=limit)),
        "notifications": q("SELECT * FROM notifications WHERE status = 'failed' ORDER BY created_at DESC LIMIT ?", (limit,)),
    }


# --------------------------------------------------------------------------- files


def _tail_file(path: Path, lines: int, max_bytes: int = 512 * 1024, line_cap: int = 4000) -> dict:
    if not path.is_file():
        return {"name": path.name, "exists": False, "bytes": 0, "truncated": False, "lines": []}
    size = path.stat().st_size
    with path.open("rb") as f:
        start = max(0, size - max_bytes)
        f.seek(start)
        data = f.read()
    text_lines = data.decode("utf-8", errors="replace").splitlines()
    if start > 0 and text_lines:
        text_lines = text_lines[1:]  # first line is partial
    tail = [_cut(ln, line_cap) for ln in text_lines[-lines:]]
    return {"name": path.name, "exists": True, "bytes": size, "truncated": start > 0 or len(text_lines) > lines, "lines": tail}


def _read_text(path: Path, limit: int = MAX_TEXT_BYTES) -> tuple[str, bool]:
    with path.open("rb") as f:
        data = f.read(limit + 1)
    return data[:limit].decode("utf-8", errors="replace"), len(data) > limit


def _profile() -> dict:
    p = load_config().path("context") / "profile.md"
    if not p.is_file():
        return {"path": str(p), "exists": False, "content": None, "updated_at": None}
    text, truncated = _read_text(p, 512 * 1024)
    return {"path": str(p), "exists": True, "content": text, "truncated": truncated,
            "updated_at": _iso(datetime.fromtimestamp(p.stat().st_mtime, timezone.utc))}


# --------------------------------------------------------------------------- signed approval pages

_PAGE_CSS = """
:root{color-scheme:light dark;--bg:#f9f9f7;--card:#fff;--ink:#0b0b0b;--ink2:#52514e;--line:#e1e0d9;--acc:#2a78d6;
--rec:#eef5fd;--bad:#d03b3b}
@media (prefers-color-scheme:dark){:root{--bg:#0d0d0d;--card:#1a1a19;--ink:#f0efec;--ink2:#c3c2b7;--line:#2c2c2a;
--acc:#3987e5;--rec:#16263a}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.55 system-ui,-apple-system,
"Segoe UI","Microsoft YaHei",sans-serif}main{max-width:640px;margin:0 auto;padding:20px 16px 48px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:18px;margin:12px 0}
h1{font-size:20px;margin:0 0 6px}.meta{color:var(--ink2);font-size:13px}.pill{display:inline-block;padding:1px 8px;
border-radius:999px;border:1px solid var(--line);font-size:12px;margin-right:6px}.risk-high{border-color:var(--bad);color:var(--bad)}
pre{white-space:pre-wrap;word-break:break-word;font:inherit;margin:0}.rec{background:var(--rec);border-left:3px solid var(--acc);
padding:10px 12px;border-radius:8px}label{display:block;font-size:13px;color:var(--ink2);margin:12px 0 4px}
textarea{width:100%;min-height:80px;border:1px solid var(--line);border-radius:8px;padding:8px;background:var(--bg);color:var(--ink);font:inherit}
.opts{display:flex;flex-direction:column;gap:8px;margin-top:14px}button{font:inherit;padding:12px 14px;border-radius:10px;
border:1px solid var(--line);background:var(--card);color:var(--ink);text-align:left;cursor:pointer}
button.primary{background:var(--acc);border-color:var(--acc);color:#fff}button small{display:block;opacity:.8;font-size:12px}
"""


def _page(title: str, body: str, status_code: int = 200) -> HTMLResponse:
    doc = (f"<!doctype html><html lang='zh-CN'><head><meta charset='utf-8'>"
           f"<meta name='viewport' content='width=device-width,initial-scale=1'><meta name='robots' content='noindex'>"
           f"<title>{html.escape(title)} · AgentWorkOS</title><style>{_PAGE_CSS}</style></head>"
           f"<body><main>{body}</main></body></html>")
    return HTMLResponse(doc, status_code, headers={"Content-Security-Policy": APPROVE_CSP, "Cache-Control": "no-store",
                                                   "Referrer-Policy": "no-referrer", "X-Content-Type-Options": "nosniff"})


def _message_page(title: str, msg: str, status_code: int) -> HTMLResponse:
    return _page(title, f"<div class='card'><h1>{html.escape(title)}</h1><p class='meta'>{html.escape(msg)}</p></div>", status_code)


def _tokens_module() -> Any | None:
    try:
        return importlib.import_module("awos.hitl.tokens")
    except ImportError:
        return None


def _approval_page(approval: dict, token: str, bound: str | None) -> HTMLResponse:
    e = html.escape
    task = repo.get_task(approval["task_id"]) if approval.get("task_id") else None
    risk = approval.get("risk") or "medium"
    head = (f"<div class='card'><div class='meta'><span class='pill risk-{e(risk)}'>风险 {e(risk)}</span>"
            f"<span class='pill'>{e(APPROVAL_KIND_LABELS.get(approval['kind'], approval['kind']))}</span>"
            f"{e(approval.get('action_type') or '')}</div><h1>{e(approval['title'])}</h1>"
            + (f"<div class='meta'>任务：{e(task['title'])}</div>" if task else "") + "</div>")
    ctx = f"<div class='card'><pre>{e(approval['context_md'])}</pre></div>" if approval.get("context_md") else ""
    rec = (f"<div class='card rec'><strong>AI 建议</strong><pre>{e(approval['recommendation'])}</pre></div>"
           if approval.get("recommendation") else "")
    if approval["status"] != ApprovalStatus.PENDING:
        state = APPROVAL_STATUS_LABELS.get(approval["status"], approval["status"])
        done = (f"<div class='card'><strong>该事项已处理：{e(state)}</strong><p class='meta'>决策：{e(approval.get('decision') or '—')}"
                f" · 渠道：{e(approval.get('decided_via') or '—')} · {e(approval.get('decided_at') or '')}</p></div>")
        return _page(approval["title"], head + ctx + rec + done)
    opts = [o for o in (approval.get("options") or []) if isinstance(o, dict) and o.get("key")]
    if bound:
        opts = [o for o in opts if o["key"] == bound]
    rec = (approval.get("recommendation") or "").strip().lower()
    buttons = "".join(
        f"<button type='submit' name='option' value='{e(o['key'])}' class='{'primary' if bound or rec.startswith(str(o['key']).lower()) else ''}'>"
        f"{e(o.get('label') or o['key'])}" + (f"<small>{e(o['description'])}</small>" if o.get("description") else "")
        + "</button>" for o in opts)
    form = (f"<form class='card' method='post' action='/approve/{e(token)}'><label for='note'>备注 / 修改意见（可选）</label>"
            f"<textarea id='note' name='note' maxlength='20000'></textarea><div class='opts'>{buttons}</div></form>")
    return _page(approval["title"], head + ctx + rec + form)


# --------------------------------------------------------------------------- app factory


def create_app(*, token: str | None = None, port: int | None = None, extra_hosts: Iterable[str] = ()) -> FastAPI:
    cfg = load_config()
    port = int(port or cfg.get("server.port", 8765))
    api_token = token or load_api_token()
    app = FastAPI(title="AgentWorkOS", version=__version__, docs_url="/api/docs", redoc_url=None, openapi_url="/api/openapi.json")
    app.state.api_token = api_token
    app.add_middleware(SecurityGuard, hosts=allowed_hosts(port, extra_hosts), token=api_token)

    async def not_found(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"not found: {exc.args[0] if exc.args else ''}"}, 404)

    async def conflict(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, 409)

    async def unavailable(_: Request, exc: Exception) -> JSONResponse:
        return JSONResponse({"detail": f"component not available: {exc}"}, 503)

    app.add_exception_handler(KeyError, not_found)
    app.add_exception_handler(InvalidTransition, conflict)
    app.add_exception_handler(ValueError, conflict)
    app.add_exception_handler(ImportError, unavailable)

    # ------------------------------------------------------------------ dashboard

    @app.get("/", include_in_schema=False)
    def index() -> HTMLResponse:
        doc = (DASHBOARD_DIR / "index.html").read_text(encoding="utf-8").replace(TOKEN_PLACEHOLDER, html.escape(api_token))
        return HTMLResponse(doc, headers={"Cache-Control": "no-store", "Content-Security-Policy": DASHBOARD_CSP,
                                          "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"})

    class _Static(StaticFiles):
        def file_response(self, *args: Any, **kwargs: Any) -> Response:
            resp = super().file_response(*args, **kwargs)
            resp.headers["Cache-Control"] = "no-cache"
            resp.headers["X-Content-Type-Options"] = "nosniff"
            return resp

    app.mount("/static", _Static(directory=DASHBOARD_DIR), name="static")

    # ------------------------------------------------------------------ health / overview / meta

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "version": __version__, "now": utcnow(), "pid": os.getpid(),
                "uptime_s": int(time.time() - STARTED_AT), "daemon": _daemon_status(), "executors": executors_status(),
                "sensors": _sensors(), "budget": _budget(), "tasks": _status_counts(), "db": str(cfg.db_path)}

    @app.get("/api/overview")
    def overview() -> dict:
        since = _iso(datetime.now(timezone.utc) - timedelta(hours=24))
        failed_runs = q1("SELECT COUNT(*) n FROM runs WHERE status IN ('failed', 'timeout') "
                         "AND COALESCE(ended_at, started_at, '') >= ?", (since,)) or {"n": 0}
        failed_tasks = q1("SELECT COUNT(*) n FROM tasks WHERE status = 'failed' AND updated_at >= ?", (since,)) or {"n": 0}
        done_today = q1("SELECT COUNT(*) n FROM tasks WHERE status = 'done' AND finished_at >= ?", (_local_midnight_utc(),)) or {"n": 0}
        running = [_brief_run(r) for r in q("SELECT * FROM runs WHERE status IN ('running', 'queued') ORDER BY started_at DESC LIMIT 20")]
        errs = _errors(limit=8)
        recent_errors = sorted(
            [{"type": "run", "id": r["id"], "title": r.get("task_title") or f"{r['purpose']} · {r['executor']}",
              "error": r.get("error"), "status": r["status"], "ts": r.get("ended_at") or r.get("started_at"), "task_id": r.get("task_id")}
             for r in errs["runs"]]
            + [{"type": "sensor", "id": s["name"], "title": f"传感器 {s['name']}", "error": _cut(s["last_error"], 600),
                "status": "failed", "ts": s.get("last_run_at")} for s in errs["sensors"]],
            key=lambda x: x.get("ts") or "", reverse=True)[:8]
        return {
            "tasks": _status_counts(), "inbox": _inbox_counts(),
            "approvals_pending": len(repo.list_approvals(status=ApprovalStatus.PENDING, limit=1000)),
            "runs_running": sum(1 for r in running if r["status"] == RunStatus.RUNNING), "running": _attach_task(running),
            "failures_24h": {"runs": failed_runs["n"], "tasks": failed_tasks["n"], "total": failed_runs["n"] + failed_tasks["n"]},
            "done_today": done_today["n"], "budget": _budget(), "cost": _cost_days(14),
            "deliverables": _attach_task(_decorate_artifacts(repo.list_artifacts(limit=8))),
            "recent_errors": recent_errors,
        }

    @app.get("/api/meta")
    def meta() -> dict:
        return {
            "version": __version__, "timezone": str(cfg.get("timezone", "Asia/Shanghai")), "levels": LEVELS, "categories": CATEGORIES, "kinds": KINDS,
            "task_statuses": [{"key": s.value, "label": TASK_STATUS_LABELS.get(s.value, s.value)} for s in TaskStatus],
            "run_statuses": [{"key": s.value, "label": RUN_STATUS_LABELS.get(s.value, s.value)} for s in RunStatus],
            "approval_statuses": [{"key": s.value, "label": APPROVAL_STATUS_LABELS.get(s.value, s.value)} for s in ApprovalStatus],
            "approval_kinds": APPROVAL_KIND_LABELS, "executors": list(EXECUTORS), "fact_kinds": FACT_KINDS,
            "transitions": {k.value: sorted(v.value for v in vs) for k, vs in TRANSITIONS.items()},
            "sensitive_actions": sorted(SENSITIVE_ACTIONS), "task_actions": list(TASK_ACTIONS),
            "priorities": {0: "P0 紧急", 1: "P1 重要", 2: "P2 常规", 3: "P3 有空再做"},
        }

    # ------------------------------------------------------------------ tasks

    def _validate(fields: dict[str, Any]) -> None:
        if fields.get("category") is not None and fields["category"] not in CATEGORIES:
            raise HTTPException(422, f"unknown category {fields['category']!r}")
        if fields.get("kind") is not None and fields["kind"] not in KINDS:
            raise HTTPException(422, f"unknown kind {fields['kind']!r}")
        if fields.get("executor") and fields["executor"] not in EXECUTORS:
            raise HTTPException(422, f"unknown executor {fields['executor']!r}")

    @app.get("/api/tasks")
    def tasks(status: str | None = None, search: str | None = None, root_only: bool = False, limit: int = 300,
              level: str | None = None, category: str | None = None, kind: str | None = None,
              parent_id: str | None = None) -> dict:
        term = (search or "").strip() or None
        rows = repo.list_tasks(status=_csv(status) or None, parent_id=parent_id, root_only=root_only,
                               limit=_clamp(limit, 1, 2000, 300), search=term)
        if term and not rows and (exact := repo.get_task(term)):
            rows = [exact]
        levels = {int(x) for x in _csv(level) if x.isdigit()}
        cats, kinds = set(_csv(category)), set(_csv(kind))
        rows = [t for t in rows if (not levels or _eff_level(t) in levels) and (not cats or t["category"] in cats)
                and (not kinds or t["kind"] in kinds)]
        out = _attach_task_context([_brief_task(t, 300) for t in rows])  # type: ignore[misc]
        summarize.attach(out, "task")
        return {"tasks": out, "total": len(out), "counts": _status_counts()}

    @app.get("/api/board")
    def board(days: int = 7) -> dict:
        return board_mod.board(days=_clamp(days, 1, 60, 7))

    @app.get("/api/dashboard")
    def dashboard(days: int = 14) -> dict:
        return board_mod.dashboard(days=_clamp(days, 3, 60, 14))

    @app.get("/api/tasks/{task_id}")
    def task_detail(task_id: str) -> dict:
        t = _require(repo.get_task(task_id), task_id)
        runs = [_brief_run(r) for r in repo.list_runs(task_id=task_id, limit=100)]
        approvals = repo.list_approvals(task_id=task_id)
        artifacts = _decorate_artifacts(repo.list_artifacts(task_id=task_id))
        meta_ = t.get("meta") if isinstance(t.get("meta"), dict) else {}
        signal = None
        if t.get("source_signal_id"):
            signal = q1("SELECT * FROM signals WHERE id = ?", (t["source_signal_id"],))
            if signal:
                signal["payload"] = loads(signal.get("payload"), signal.get("payload"))
        children = _tree(task_id)
        flat: list[dict] = []
        stack = list(children)
        while stack:
            n = stack.pop()
            flat.append(n)
            stack.extend(n.get("children") or [])
        return {
            "task": {**t, "effective_level": _eff_level(t), "topic": topics_mod.topic_for_task(task_id),
                     **{k: v for k, v in summarize.attach([dict(t)], "task")[0].items() if k in ("headline", "gist", "keywords", "digest_auto")}},
            "parent": _brief_task(repo.get_task(t["parent_id"])) if t.get("parent_id") else None,
            "root": _brief_task(repo.get_task(t["root_id"])) if t.get("root_id") and t["root_id"] not in (task_id, t.get("parent_id")) else None,
            "children": children,
            "children_progress": {"total": len(flat), "done": sum(1 for n in flat if n["status"] == TaskStatus.DONE)},
            "deps": repo.dependencies(task_id),
            "dependents": q("SELECT t.id, t.title, t.status FROM task_deps d JOIN tasks t ON t.id = d.task_id WHERE d.depends_on = ?", (task_id,)),
            "runs": runs, "approvals": approvals, "artifacts": artifacts,
            "events": _task_timeline(task_id, runs, approvals, artifacts),
            "notes": meta_.get("notes") or [], "signal": signal,
            "allowed_transitions": sorted(v.value for v in TRANSITIONS.get(TaskStatus(t["status"]), set())),
        }

    @app.get("/api/tasks/{task_id}/evidence")
    def task_evidence(task_id: str) -> dict:
        """Everything the task was built from, readable in full: source mails (with thread) and other signals.
        Token-protected GET (see SecurityGuard._sensitive_get)."""
        t = _require(repo.get_task(task_id), task_id)
        from .. import mailstore
        items: list[dict] = []
        mail_refs = mailstore.refs_for_task(t)
        meta_ = t.get("meta") if isinstance(t.get("meta"), dict) else {}
        sig_ids = list(dict.fromkeys(list(meta_.get("signals") or []) + ([t["source_signal_id"]] if t.get("source_signal_id") else [])))
        for sid in sig_ids[:20]:
            s = q1("SELECT * FROM signals WHERE id = ?", (sid,))
            if s and s["source"] != "mailmaster":
                items.append({"type": "signal", "source": s["source"], "title": s["title"], "body": s.get("body") or "",
                              "url": s.get("url"), "observed_at": s["observed_at"]})
        ordered: list[str] = []
        for r in mail_refs:
            for x in mailstore.thread_refs(r):
                if x not in ordered and x not in mail_refs:
                    ordered.append(x)
            ordered.append(r)
        mails = []
        for r in list(dict.fromkeys(ordered))[-8:]:
            try:
                m = mailstore.read_mail(r, max_chars=8000)
            except (KeyError, ValueError, OSError):
                continue
            mails.append({"type": "mail", "ref": m["ref"], "subject": m["subject"], "from": m["from"], "to": m["to"], "cc": m["cc"],
                          "received": m["received"], "received_ms": m["received_ms"], "body": m["body"], "truncated": m["truncated"],
                          "body_available": m["body_available"], "has_attachments": m["has_attachments"], "is_source": r in mail_refs})
        mails.sort(key=lambda m: m["received_ms"])
        return {"task_id": task_id, "items": items + mails, "mail_count": len(mails)}

    @app.get("/api/mail/read")
    def mail_read(ref: str, chars: int = 40000) -> dict:
        """Full text of one local mail (token-protected)."""
        from .. import mailstore
        return mailstore.read_mail(ref, max_chars=_clamp(chars, 500, 120000, 40000))

    @app.post("/api/tasks", status_code=201)
    def create_task(body: TaskCreate) -> dict:
        fields = body.model_dump()
        _validate(fields)
        title = body.title.strip()
        if not title:
            raise HTTPException(422, "title must not be blank")
        tid = actions.add_manual_task(title, (body.description or "").strip() or None, level=body.level, category=body.category,
                                      priority=body.priority, kind=body.kind, executor=body.executor or None,
                                      triage=body.triage, project=body.project or None, due_at=body.due_at or None)
        return {"id": tid, "task": _brief_task(repo.get_task(tid), 5000)}

    @app.post("/api/tasks/{task_id}/actions/{action}")
    def task_action(task_id: str, action: str, body: TaskActionBody | None = None) -> dict:
        if action not in TASK_ACTIONS:
            raise HTTPException(404, f"unknown action {action!r}; expected one of {list(TASK_ACTIONS)}")
        _require(repo.get_task(task_id), task_id)
        b = body or TaskActionBody()
        note = (b.note or "").strip() or None
        if action == "accept":
            actions.accept_task(task_id, note=note)
        elif action == "retry":
            actions.retry_task(task_id, note=note)
        elif action == "rework":
            feedback = (b.feedback or b.note or "").strip()
            if not feedback:
                raise HTTPException(422, "rework requires feedback")
            actions.rework_task(task_id, feedback)
        elif action == "complete":
            actions.complete_task(task_id, note=note)
        elif action == "cancel":
            actions.cancel_task(task_id, reason=note)
        elif action == "dismiss":
            actions.dismiss_task(task_id, reason=note)
        elif action == "snooze":
            actions.snooze_task(task_id, hours=b.hours or 24)
        return {"ok": True, "action": action, "task": _brief_task(repo.get_task(task_id))}

    @app.patch("/api/tasks/{task_id}")
    def patch_task(task_id: str, body: TaskPatch) -> dict:
        _require(repo.get_task(task_id), task_id)
        fields = body.model_dump(exclude_unset=True, exclude_none=True)
        _validate(fields)
        if not fields:
            raise HTTPException(422, "no editable fields given")
        actions.set_task_fields(task_id, **fields)
        return {"ok": True, "task": _brief_task(repo.get_task(task_id), 5000)}

    # ------------------------------------------------------------------ inbox / outbox

    def _inbox_data() -> dict:
        pending = _attach_task(repo.list_approvals(status=ApprovalStatus.PENDING, limit=500))

        def bucket(status: str, text_limit: int) -> list[dict]:
            return _attach_task_context([_brief_task(t, text_limit) for t in repo.list_tasks(status=status, limit=300)])  # type: ignore[misc]

        review, handoff = bucket("review", 4000), bucket("waiting_user", 4000)
        suggested, failed = bucket("triaged", 1200), bucket("failed", 1200)
        _attach_artifacts(review + handoff)
        _attach_last_error(failed)
        summarize.attach(pending, "approval")
        for rows in (review, handoff, suggested, failed):
            summarize.attach(rows, "task")
        out = {"decisions": [a for a in pending if a["kind"] == "decision"],
               "actions": [a for a in pending if a["kind"] != "decision"],
               "review": review, "handoff": handoff, "suggested": suggested, "failed": failed}
        out["counts"] = {k: len(v) for k, v in out.items()}
        out["feed"] = topics_mod.attach_to_feed(board_mod.inbox_feed(pending, review, handoff, suggested, failed))
        out["topics"] = topics_mod.topic_summaries(out["feed"])
        return out

    @app.get("/api/inbox")
    def inbox() -> dict:
        return _inbox_data()

    @app.get("/api/topics")
    def topics_list() -> dict:
        return {"topics": _inbox_data()["topics"]}

    @app.get("/api/topics/{key}")
    def topic_detail(key: str) -> dict:
        return _require(topics_mod.topic_view(key, _inbox_data()["feed"]), key)

    @app.post("/api/topics/{key}/dismiss")
    def topic_dismiss(key: str, body: TaskActionBody) -> dict:
        _require(topics_mod.ensure_topic_row(key), key)
        reason = (body.note or "").strip()
        if len(reason) < 2:
            raise HTTPException(422, "a reason is required (it is kept as feedback so the same thing is not suggested again)")
        return {"ok": True, **actions.dismiss_topic(key, reason)}

    @app.post("/api/topics/{key}/refresh", status_code=202)
    def topic_refresh(key: str) -> dict:
        _require(repo.get_topic(key), key)
        threading.Thread(target=topics_mod.generate_brief, args=(key,), daemon=True, name=f"brief-{key}").start()
        return {"ok": True, "queued": True}

    @app.get("/api/approvals")
    def approvals(status: str | None = None, task_id: str | None = None, limit: int = 200) -> dict:
        rows = repo.list_approvals(status=_csv(status) or None, task_id=task_id, limit=_clamp(limit, 1, 1000))
        return {"approvals": summarize.attach(_attach_task(rows), "approval")}

    @app.get("/api/approvals/{approval_id}")
    def approval_detail(approval_id: str) -> dict:
        a = summarize.attach(_attach_task([_require(repo.get_approval(approval_id), approval_id)]), "approval")[0]
        a["topic"] = topics_mod.topic_for_task(a["task_id"]) if a.get("task_id") else None
        return {"approval": a, "task": _brief_task(repo.get_task(a["task_id"]), 4000) if a.get("task_id") else None,
                "events": repo.list_events("approval", approval_id, limit=100)}

    @app.post("/api/approvals/{approval_id}/decide")
    def approval_decide(approval_id: str, body: DecisionBody) -> dict:
        _require(repo.get_approval(approval_id), approval_id)
        a = actions.decide(approval_id, body.option, note=(body.note or "").strip() or None, via="dashboard")
        return {"ok": True, "approval": a}

    # ------------------------------------------------------------------ runs / tool calls / errors

    @app.get("/api/runs")
    def runs(status: str | None = None, task_id: str | None = None, purpose: str | None = None,
             executor: str | None = None, limit: int = 200) -> dict:
        sql, params = "SELECT * FROM runs WHERE 1=1", []
        for col, vals in (("status", _csv(status)), ("purpose", _csv(purpose)), ("executor", _csv(executor))):
            if vals:
                sql += f" AND {col} IN ({_in(vals)})"
                params += vals
        if task_id:
            sql += " AND task_id = ?"
            params.append(task_id)
        sql += " ORDER BY COALESCE(started_at, '9999') DESC, id DESC LIMIT ?"
        params.append(_clamp(limit, 1, 2000))
        return {"runs": _attach_task([_brief_run(r) for r in q(sql, params)])}

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        r = _require(repo.get_run(run_id), run_id)
        counts = {x["kind"]: x["n"] for x in q("SELECT kind, COUNT(*) n FROM run_events WHERE run_id = ? GROUP BY kind", (run_id,))}
        run = {**r, "duration_s": _duration(r.get("started_at"), r.get("ended_at"), running=r["status"] == RunStatus.RUNNING)}
        return {"run": run, "task": _brief_task(repo.get_task(r["task_id"])) if r.get("task_id") else None,
                "event_counts": counts,
                "artifacts": _decorate_artifacts(q("SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,))),
                "cancellable": r["status"] in (RunStatus.RUNNING, RunStatus.QUEUED)}

    @app.get("/api/runs/{run_id}/events")
    def run_events(run_id: str, after: int = 0, kind: str | None = None, limit: int = 500) -> dict:
        r = _require(repo.get_run(run_id), run_id)
        evs = repo.run_events(run_id, after_id=max(0, after), limit=_clamp(limit, 1, 5000, 500), kind=kind or None)
        return {"events": evs, "last_id": evs[-1]["id"] if evs else max(0, after), "run_status": r["status"]}

    @app.get("/api/runs/{run_id}/log")
    def run_log(run_id: str, tail: int = 200) -> dict:
        r = _require(repo.get_run(run_id), run_id)
        n = _clamp(tail, 1, 5000, 200)
        empty = {"log_dir": r.get("log_path"), "available": False, "stdout": None, "stderr": None, "last_message": None,
                 "prompt": None, "files": []}
        if not r.get("log_path"):
            return empty
        log_dir = Path(r["log_path"])
        if not _inside(log_dir, _var_dir()):
            raise HTTPException(403, "run log directory is outside var/")
        if not log_dir.is_dir():
            return empty
        files = sorted(({"name": p.name, "bytes": p.stat().st_size} for p in log_dir.iterdir() if p.is_file()), key=lambda f: f["name"])
        last = log_dir / "last_message.txt"
        prompt = log_dir / "prompt.md"
        return {"log_dir": str(log_dir), "available": True, "files": files,
                "stdout": _tail_file(log_dir / "stdout.jsonl", n), "stderr": _tail_file(log_dir / "stderr.log", n),
                "last_message": _read_text(last, 64 * 1024)[0] if last.is_file() else None,
                "prompt": _read_text(prompt, 64 * 1024)[0] if prompt.is_file() else None}

    @app.post("/api/runs/{run_id}/cancel")
    def run_cancel(run_id: str) -> dict:
        r = _require(repo.get_run(run_id), run_id)
        if r["status"] not in (RunStatus.RUNNING, RunStatus.QUEUED):
            raise ValueError(f"run {run_id} is {r['status']}, not running")
        from .. import llm
        ok = llm.cancel_run(run_id)
        if ok:
            repo.log_event("run", run_id, "cancel_requested", None, actor="user")
        return {"ok": ok, "detail": "cancel signal sent" if ok else "run is not active in this process"}

    @app.get("/api/tool-calls")
    def tool_calls(limit: int = 300, name: str | None = None) -> dict:
        rows = repo.recent_tool_calls(limit=_clamp(limit, 1, 3000, 300))
        if name:
            rows = [r for r in rows if name.lower() in (r.get("name") or "").lower()]
        return {"tool_calls": _attach_task(rows)}

    @app.get("/api/errors")
    def errors(limit: int = 100) -> dict:
        return _errors(_clamp(limit, 1, 1000, 100))

    # ------------------------------------------------------------------ artifacts

    @app.get("/api/artifacts")
    def artifacts(task_id: str | None = None, limit: int = 300) -> dict:
        return {"artifacts": _attach_task(_decorate_artifacts(repo.list_artifacts(task_id=task_id, limit=_clamp(limit, 1, 2000, 300))))}

    @app.get("/api/artifacts/{artifact_id}/content")
    def artifact_content(artifact_id: str, download: bool = False) -> Response:
        a = _require(repo.get_artifact(artifact_id), artifact_id)
        if not a.get("path"):
            raise HTTPException(404, "artifact has no local file")
        p = Path(a["path"])
        if not p.is_absolute():
            p = cfg.root / p
        real = p.resolve()
        if not _inside(real, _var_dir()):
            raise HTTPException(403, "artifact path is outside the AgentWorkOS var/ directory")
        if not real.is_file():
            raise HTTPException(404, "artifact file is missing")
        ext = real.suffix.lower()
        headers = {"Content-Security-Policy": SANDBOX_CSP, "X-Content-Type-Options": "nosniff", "Cache-Control": "no-cache"}
        if ext in TEXT_TYPES and not download:
            text, truncated = _read_text(real)
            if truncated:
                headers["X-AWOS-Truncated"] = "1"
            return Response(text, media_type=f"{TEXT_TYPES[ext]}; charset=utf-8", headers=headers)
        media = mimetypes.guess_type(real.name)[0] or "application/octet-stream"
        return FileResponse(real, media_type=media, headers=headers, filename=real.name,
                            content_disposition_type="attachment" if download else "inline")

    # ------------------------------------------------------------------ signals / sensors

    @app.get("/api/signals")
    def signals(status: str | None = None, source: str | None = None, limit: int = 200) -> dict:
        sql, params = "SELECT * FROM signals WHERE 1=1", []
        for col, vals in (("status", _csv(status)), ("source", _csv(source))):
            if vals:
                sql += f" AND {col} IN ({_in(vals)})"
                params += vals
        sql += " ORDER BY observed_at DESC LIMIT ?"
        params.append(_clamp(limit, 1, 2000))
        rows = q(sql, params)
        for r in rows:
            r["payload"] = loads(r.get("payload"), r.get("payload"))
        return {"signals": _attach_task(rows)}

    @app.get("/api/sensors")
    def sensors() -> dict:
        return {"sensors": _sensors(include_cursor=True)}

    @app.post("/api/sensors/{name}/run", status_code=202)
    def sensor_run(name: str) -> dict:
        from ..sensors import all_sensors
        if name not in all_sensors():
            raise KeyError(f"sensor {name}")
        actions.run_sensor_now(name)
        return {"ok": True, "sensor": name, "detail": "started in background"}

    # ------------------------------------------------------------------ context

    @app.get("/api/context")
    def context(status: str = "active,proposed") -> dict:
        wanted = set(_csv(status))
        facts = [f for f in repo.list_facts(status=None) if "all" in wanted or f["status"] in wanted]
        groups: dict[str, list[dict]] = {}
        for f in facts:
            groups.setdefault(f["kind"], []).append(f)
        return {"kinds": FACT_KINDS, "groups": groups, "count": len(facts), "profile": _profile()}

    @app.post("/api/context", status_code=201)
    def context_add(body: FactBody) -> dict:
        key, value = body.key.strip(), body.value.strip()
        if not key or not value:
            raise HTTPException(422, "key and value must not be blank")
        fid = actions.add_context_note(body.kind, key, value)
        return {"id": fid, "fact": q1("SELECT * FROM context_facts WHERE id = ?", (fid,)) if fid else None}

    @app.delete("/api/context/{fact_id}")
    def context_delete(fact_id: str) -> dict:
        _require(q1("SELECT id FROM context_facts WHERE id = ?", (fact_id,)), fact_id)
        repo.delete_fact(fact_id, actor="user")
        return {"ok": True}

    # ------------------------------------------------------------------ history / notifications

    @app.get("/api/events")
    def events(entity_type: str | None = None, entity_id: str | None = None, type: str | None = None,
               actor: str | None = None, before: int | None = None, limit: int = 100) -> dict:
        n = _clamp(limit, 1, 1000, 100)
        sql, params = "SELECT * FROM events WHERE 1=1", []
        for col, val in (("entity_type", entity_type), ("entity_id", entity_id), ("type", type)):
            if val:
                sql += f" AND {col} = ?"
                params.append(val)
        if actor:
            sql += " AND actor LIKE ?"
            params.append(f"{actor}%")
        if before:
            sql += " AND id < ?"
            params.append(before)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(n + 1)
        rows = _decode_events(q(sql, params))
        has_more = len(rows) > n
        rows = _attach_event_titles(rows[:n])
        return {"events": rows, "has_more": has_more, "next_before": rows[-1]["id"] if has_more and rows else None}

    @app.get("/api/notifications")
    def notifications(limit: int = 100) -> dict:
        return {"notifications": repo.list_notifications(limit=_clamp(limit, 1, 1000, 100))}

    # ------------------------------------------------------------------ live stream (SSE)

    @app.get("/api/stream")
    async def stream(request: Request) -> StreamingResponse:
        async def gen():
            qu = bus.subscribe()
            try:
                yield "retry: 3000\n: connected\n\n"
                last_sent = time.monotonic()
                while not await request.is_disconnected():
                    batch = []
                    while len(batch) < 200:
                        try:
                            batch.append(qu.get_nowait())
                        except queue.Empty:
                            break
                    if batch:
                        yield "".join(f"data: {json.dumps(ev, ensure_ascii=False, default=str)}\n\n" for ev in batch)
                        last_sent = time.monotonic()
                    elif time.monotonic() - last_sent >= 15:
                        yield ": keep-alive\n\n"
                        last_sent = time.monotonic()
                    await asyncio.sleep(0.5)
            finally:
                bus.unsubscribe(qu)

        return StreamingResponse(gen(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # ------------------------------------------------------------------ signed approval links

    @app.get("/approve/{token}", include_in_schema=False)
    def approve_page(token: str) -> HTMLResponse:
        mod = _tokens_module()
        if mod is None:
            return _message_page("暂不可用", "签名链接模块（awos.hitl.tokens）尚未安装。", 503)
        try:  # GET never decides anything: chat apps pre-fetch links
            approval_id, bound = mod.verify(token)
        except ValueError as e:
            return _message_page("链接无效", f"该审批链接无效、已使用或已过期（{e}），请在 Dashboard 中处理。", 403)
        approval = repo.get_approval(approval_id)
        if approval is None:
            return _message_page("未找到", "该审批事项不存在。", 404)
        return _approval_page(approval, token, bound)

    @app.post("/approve/{token}", include_in_schema=False)
    async def approve_submit(token: str, request: Request) -> HTMLResponse:
        mod = _tokens_module()
        if mod is None:
            return _message_page("暂不可用", "签名链接模块（awos.hitl.tokens）尚未安装。", 503)
        try:
            approval_id, bound = mod.verify(token)
        except ValueError:
            return _message_page("链接无效", "该审批链接无效或已过期，请在 Dashboard 中处理。", 403)
        raw = await request.body()
        if len(raw) > 64 * 1024:
            return _message_page("请求过大", "表单内容过长。", 413)
        form = parse_qs(raw.decode("utf-8", errors="replace"), keep_blank_values=True)
        option = (form.get("option") or [""])[0].strip()
        note = (form.get("note") or [""])[0].strip() or None
        if not option or (bound and option != bound):
            return _message_page("选项无效", "该链接不允许此选项。", 403 if bound else 422)
        current = repo.get_approval(approval_id)
        if current is None:
            return _message_page("未找到", "该审批事项不存在。", 404)
        if option not in {o.get("key") for o in (current.get("options") or []) if isinstance(o, dict)}:
            return _message_page("选项无效", "该事项没有这个选项。", 422)
        try:  # burn the one-time token first so a second submit (or a replay) cannot decide again
            approval_id, _ = getattr(mod, "consume", mod.verify)(token)
        except ValueError as e:
            return _message_page("链接已失效", f"该链接已被使用或已过期：{e}", 409)
        try:
            approval = await run_in_threadpool(actions.decide, approval_id, option, note, "link")
        except KeyError:
            return _message_page("未找到", "该审批事项不存在。", 404)
        except ValueError as e:
            return _message_page("无法提交", f"该事项可能已被处理：{e}", 409)
        labels = {o.get("key"): o.get("label") for o in (approval.get("options") or []) if isinstance(o, dict)}
        return _message_page("已记录决策", f"「{approval['title']}」→ {labels.get(option) or option}。可以关闭此页面。", 200)

    return app


# --------------------------------------------------------------------------- server


def make_server(host: str | None = None, port: int | None = None, log_level: str = "warning") -> Any:
    """Build a uvicorn.Server (call .run() in a thread; set .should_exit = True to stop)."""
    import uvicorn

    cfg = load_config()
    host = host or cfg.get("server.host", "127.0.0.1")
    port = int(port or cfg.get("server.port", 8765))
    config = uvicorn.Config(create_app(port=port), host=host, port=port, log_level=log_level, access_log=False,
                            lifespan="off", timeout_graceful_shutdown=3)
    return uvicorn.Server(config)


def serve(host: str | None = None, port: int | None = None) -> None:
    """Blocking: run the API + dashboard (used by the daemon in a background thread)."""
    make_server(host, port).run()


if __name__ == "__main__":  # pragma: no cover - manual dev entry: python -m awos.api.app --port 8799
    import argparse

    ap = argparse.ArgumentParser(description="AgentWorkOS API server")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    args = ap.parse_args()
    serve(args.host, args.port)
