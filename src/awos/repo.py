"""Domain data access. Every state transition goes through here and is written to the `events` audit log."""
from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import Path
from typing import Any

from . import db
from .db import dumps, loads, new_id, q, q1, tx, utcnow
from .models import TERMINAL, TRANSITIONS, ApprovalStatus, RunStatus, TaskStatus

# --------------------------------------------------------------------------- events / audit


def log_event(entity_type: str, entity_id: str | None, type_: str, data: Any = None, actor: str = "system", conn=None) -> None:
    db.insert("events", {"ts": utcnow(), "entity_type": entity_type, "entity_id": entity_id,
                         "type": type_, "actor": actor, "data": dumps(data)}, conn=conn)
    try:
        from .bus import bus
        bus.publish({"entity_type": entity_type, "entity_id": entity_id, "type": type_, "actor": actor})
    except Exception:  # pragma: no cover - bus is best effort
        pass


def list_events(entity_type: str | None = None, entity_id: str | None = None, limit: int = 200, before_id: int | None = None) -> list[dict]:
    sql, params = "SELECT * FROM events WHERE 1=1", []
    if entity_type:
        sql += " AND entity_type = ?"; params.append(entity_type)
    if entity_id:
        sql += " AND entity_id = ?"; params.append(entity_id)
    if before_id:
        sql += " AND id < ?"; params.append(before_id)
    sql += " ORDER BY id DESC LIMIT ?"; params.append(limit)
    return [_decode(r, "data") for r in q(sql, params)]


def _decode(row: dict | None, *json_cols: str) -> dict | None:
    if row is None:
        return None
    for c in json_cols:
        if c in row:
            row[c] = loads(row[c], row[c])
    return row


# --------------------------------------------------------------------------- signals


def fingerprint(*parts: Any) -> str:
    return hashlib.sha256("␟".join(str(p) for p in parts).encode("utf-8")).hexdigest()[:32]


def add_signal(source: str, title: str, fingerprint_: str, *, body: str | None = None, url: str | None = None,
               external_id: str | None = None, occurred_at: str | None = None, payload: Any = None) -> str | None:
    """Insert a signal; returns its id, or None if an identical fingerprint already exists (dedupe)."""
    sid = new_id("sig")
    n = db.insert("signals", {
        "id": sid, "source": source, "external_id": external_id, "fingerprint": fingerprint_,
        "title": title[:300], "body": (body or "")[:4000] or None, "url": url, "occurred_at": occurred_at,
        "observed_at": utcnow(), "payload": dumps(payload), "status": "new",
    }, or_ignore=True)
    if n:
        log_event("signal", sid, "created", {"source": source, "title": title[:120]}, actor=f"sensor:{source}")
        return sid
    return None


def pending_signals(limit: int = 50) -> list[dict]:
    return [_decode(r, "payload") for r in q("SELECT * FROM signals WHERE status = 'new' ORDER BY observed_at LIMIT ?", (limit,))]


def mark_signal(signal_id: str, status: str, task_id: str | None = None, note: str | None = None) -> None:
    db.update("signals", "id", signal_id, {"status": status, "task_id": task_id, "triage_note": note})


def list_signals(status: str | None = None, limit: int = 200) -> list[dict]:
    if status:
        rows = q("SELECT * FROM signals WHERE status = ? ORDER BY observed_at DESC LIMIT ?", (status, limit))
    else:
        rows = q("SELECT * FROM signals ORDER BY observed_at DESC LIMIT ?", (limit,))
    return [_decode(r, "payload") for r in rows]


# --------------------------------------------------------------------------- tasks

TASK_JSON = ("plan", "result", "meta")


def create_task(title: str, *, description: str | None = None, kind: str = "todo", category: str = "other",
                priority: int = 2, level: int = 1, level_reason: str | None = None, status: str = TaskStatus.INBOX,
                parent_id: str | None = None, executor: str | None = None, project: str | None = None,
                due_at: str | None = None, source: str = "manual", source_signal_id: str | None = None,
                dedupe_key: str | None = None, acceptance: str | None = None, meta: Any = None,
                max_attempts: int = 3, actor: str = "system") -> str:
    if dedupe_key:
        existing = q1("SELECT id FROM tasks WHERE dedupe_key = ?", (dedupe_key,))
        if existing:
            return existing["id"]
    tid = new_id("tsk")
    root_id = tid
    if parent_id:
        parent = q1("SELECT root_id FROM tasks WHERE id = ?", (parent_id,))
        root_id = (parent or {}).get("root_id") or parent_id
    now = utcnow()
    with tx() as c:
        db.insert("tasks", {
            "id": tid, "parent_id": parent_id, "root_id": root_id, "title": title[:300], "description": description,
            "kind": kind, "category": category, "priority": int(priority), "level": max(1, min(5, int(level))),
            "level_reason": level_reason, "status": str(status), "executor": executor, "project": project,
            "due_at": due_at, "source": source, "source_signal_id": source_signal_id, "dedupe_key": dedupe_key,
            "acceptance": acceptance, "meta": dumps(meta or {}), "max_attempts": max_attempts,
            "created_at": now, "updated_at": now,
        }, conn=c)
        log_event("task", tid, "created", {"title": title[:200], "status": str(status), "source": source,
                                           "parent_id": parent_id}, actor=actor, conn=c)
    return tid


def get_task(task_id: str) -> dict | None:
    return _decode(q1("SELECT * FROM tasks WHERE id = ?", (task_id,)), *TASK_JSON)


def list_tasks(status: str | list[str] | None = None, parent_id: str | None = None, root_only: bool = False,
               limit: int = 500, search: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM tasks WHERE 1=1", []
    if status:
        sts = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' for _ in sts)})"; params += sts
    if parent_id:
        sql += " AND parent_id = ?"; params.append(parent_id)
    if root_only:
        sql += " AND parent_id IS NULL"
    if search:
        sql += " AND (title LIKE ? OR description LIKE ?)"; params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY priority ASC, created_at DESC LIMIT ?"; params.append(limit)
    return [_decode(r, *TASK_JSON) for r in q(sql, params)]


class InvalidTransition(Exception):
    pass


def set_task_status(task_id: str, status: str, reason: str | None = None, *, actor: str = "system",
                    force: bool = False, extra: dict[str, Any] | None = None, conn=None) -> dict:
    """Transition a task; validates against models.TRANSITIONS and records an event."""
    def _do(c):
        row = c.execute("SELECT status FROM tasks WHERE id = ?", (task_id,)).fetchone()
        if row is None:
            raise KeyError(task_id)
        old = TaskStatus(row["status"])
        new = TaskStatus(status)
        if old != new and not force and new not in TRANSITIONS.get(old, set()):
            raise InvalidTransition(f"{task_id}: {old} -> {new} not allowed")
        fields: dict[str, Any] = {"status": str(new), "status_reason": reason, "updated_at": utcnow()}
        if new == TaskStatus.RUNNING:
            fields["started_at"] = utcnow()
        if new in TERMINAL or new in (TaskStatus.REVIEW, TaskStatus.WAITING_USER):
            fields["finished_at"] = utcnow()
        if new != TaskStatus.RUNNING:
            fields["lease_owner"] = None
            fields["lease_expires_at"] = None
        if extra:
            fields.update({k: (dumps(v) if k in TASK_JSON else v) for k, v in extra.items()})
        db.update("tasks", "id", task_id, fields, conn=c)
        log_event("task", task_id, "status_changed", {"from": str(old), "to": str(new), "reason": reason}, actor=actor, conn=c)
        return {"from": str(old), "to": str(new)}

    if conn is not None:
        return _do(conn)
    with tx() as c:
        return _do(c)


def update_task(task_id: str, actor: str = "system", **fields: Any) -> None:
    if not fields:
        return
    fields = {k: (dumps(v) if k in TASK_JSON else v) for k, v in fields.items()}
    fields["updated_at"] = utcnow()
    with tx() as c:
        db.update("tasks", "id", task_id, fields, conn=c)
        log_event("task", task_id, "updated", {k: v for k, v in fields.items() if k not in ("updated_at",) and k not in TASK_JSON},
                  actor=actor, conn=c)


def merge_task_meta(task_id: str, **items: Any) -> dict:
    t = get_task(task_id) or {}
    meta = t.get("meta") or {}
    meta.update(items)
    db.update("tasks", "id", task_id, {"meta": dumps(meta), "updated_at": utcnow()})
    return meta


def add_dependency(task_id: str, depends_on: str) -> None:
    db.insert("task_deps", {"task_id": task_id, "depends_on": depends_on}, or_ignore=True)


def dependencies(task_id: str) -> list[dict]:
    return q("SELECT t.id, t.title, t.status FROM task_deps d JOIN tasks t ON t.id = d.depends_on WHERE d.task_id = ?", (task_id,))


def children(task_id: str) -> list[dict]:
    return [_decode(r, *TASK_JSON) for r in q("SELECT * FROM tasks WHERE parent_id = ? ORDER BY created_at", (task_id,))]


def claim_next_task(owner: str, lease_seconds: int, executor_ok=None) -> dict | None:
    """Atomically claim the highest-priority READY task whose deps are done and backoff has elapsed."""
    now = utcnow()
    with tx() as c:
        rows = c.execute(
            """SELECT * FROM tasks t
               WHERE t.status = 'ready'
                 AND (t.next_attempt_at IS NULL OR t.next_attempt_at <= ?)
                 AND NOT EXISTS (SELECT 1 FROM task_deps d JOIN tasks u ON u.id = d.depends_on
                                 WHERE d.task_id = t.id AND u.status NOT IN ('done'))
               ORDER BY t.priority ASC, t.created_at ASC LIMIT 20""", (now,)).fetchall()
        for r in rows:
            row = dict(r)
            if executor_ok and not executor_ok(row):
                continue
            n = c.execute("UPDATE tasks SET status='running', lease_owner=?, lease_expires_at=?, started_at=?, updated_at=?, "
                          "attempts = attempts + 1 WHERE id=? AND status='ready'",
                          (owner, db.ts_after(lease_seconds), now, now, row["id"])).rowcount
            if n:
                log_event("task", row["id"], "status_changed", {"from": "ready", "to": "running", "owner": owner}, conn=c)
                return get_task(row["id"])
    return None


def renew_lease(task_id: str, owner: str, lease_seconds: int) -> bool:
    return db.execute("UPDATE tasks SET lease_expires_at = ? WHERE id = ? AND lease_owner = ? AND status = 'running'",
                      (db.ts_after(lease_seconds), task_id, owner)) > 0


def expired_leases() -> list[dict]:
    return q("SELECT * FROM tasks WHERE status = 'running' AND (lease_expires_at IS NULL OR lease_expires_at < ?)", (utcnow(),))


# --------------------------------------------------------------------------- runs


def create_run(purpose: str, executor: str, *, task_id: str | None = None, model: str | None = None,
               attempt: int = 1, cwd: str | None = None) -> str:
    rid = new_id("run")
    db.insert("runs", {"id": rid, "task_id": task_id, "purpose": purpose, "attempt": attempt, "executor": executor,
                       "model": model, "status": RunStatus.QUEUED, "cwd": cwd})
    log_event("run", rid, "created", {"task_id": task_id, "purpose": purpose, "executor": executor})
    return rid


def update_run(run_id: str, **fields: Any) -> None:
    for k in ("structured", "command"):
        if k in fields and not isinstance(fields[k], (str, type(None))):
            fields[k] = dumps(fields[k])
    db.update("runs", "id", run_id, fields)
    if "status" in fields:
        log_event("run", run_id, "status_changed", {"to": str(fields["status"]), "error": fields.get("error")})


def get_run(run_id: str) -> dict | None:
    return _decode(q1("SELECT * FROM runs WHERE id = ?", (run_id,)), "structured", "command")


def list_runs(task_id: str | None = None, status: str | None = None, limit: int = 200) -> list[dict]:
    sql, params = "SELECT * FROM runs WHERE 1=1", []
    if task_id:
        sql += " AND task_id = ?"; params.append(task_id)
    if status:
        sql += " AND status = ?"; params.append(status)
    sql += " ORDER BY COALESCE(started_at, '9999') DESC, id DESC LIMIT ?"; params.append(limit)
    return [_decode(r, "structured", "command") for r in q(sql, params)]


def add_run_event(run_id: str, kind: str, name: str | None = None, data: Any = None) -> None:
    s = dumps(data)
    if s and len(s) > 8000:
        s = dumps({"truncated": True, "head": s[:7800]})
    db.insert("run_events", {"run_id": run_id, "ts": utcnow(), "kind": kind, "name": name, "data": s})


def run_events(run_id: str, after_id: int = 0, limit: int = 1000, kind: str | None = None) -> list[dict]:
    sql, params = "SELECT * FROM run_events WHERE run_id = ? AND id > ?", [run_id, after_id]
    if kind:
        sql += " AND kind = ?"; params.append(kind)
    sql += " ORDER BY id LIMIT ?"; params.append(limit)
    return [_decode(r, "data") for r in q(sql, params)]


def recent_tool_calls(limit: int = 300) -> list[dict]:
    rows = q("""SELECT e.*, r.task_id, r.executor FROM run_events e JOIN runs r ON r.id = e.run_id
                WHERE e.kind = 'tool_call' ORDER BY e.id DESC LIMIT ?""", (limit,))
    return [_decode(r, "data") for r in rows]


def record_cost(run_id: str | None, executor: str, model: str | None, cost_usd: float, tokens_in: int = 0, tokens_out: int = 0) -> None:
    db.insert("cost_ledger", {"ts": utcnow(), "day": datetime.now().strftime("%Y-%m-%d"), "run_id": run_id,
                              "executor": executor, "model": model, "cost_usd": cost_usd or 0,
                              "tokens_in": tokens_in or 0, "tokens_out": tokens_out or 0})


def cost_today(executor: str | None = None) -> dict:
    day = datetime.now().strftime("%Y-%m-%d")
    if executor:
        r = q1("SELECT COALESCE(SUM(cost_usd),0) usd, COUNT(*) runs, COALESCE(SUM(tokens_in+tokens_out),0) tokens "
               "FROM cost_ledger WHERE day = ? AND executor = ?", (day, executor))
    else:
        r = q1("SELECT COALESCE(SUM(cost_usd),0) usd, COUNT(*) runs, COALESCE(SUM(tokens_in+tokens_out),0) tokens "
               "FROM cost_ledger WHERE day = ?", (day,))
    return r or {"usd": 0, "runs": 0, "tokens": 0}


# --------------------------------------------------------------------------- approvals (Outbox)

APPROVAL_JSON = ("options", "payload", "result")


def create_approval(*, title: str, kind: str, options: list[dict], task_id: str | None = None, run_id: str | None = None,
                    action_type: str | None = None, context_md: str | None = None, recommendation: str | None = None,
                    risk: str = "medium", payload: Any = None, expires_at: str | None = None,
                    default_on_expiry: str | None = None) -> str:
    aid = new_id("apv")
    with tx() as c:
        db.insert("approvals", {
            "id": aid, "task_id": task_id, "run_id": run_id, "kind": kind, "action_type": action_type,
            "title": title[:300], "context_md": context_md, "recommendation": recommendation,
            "options": dumps(options), "risk": risk, "payload": dumps(payload), "status": ApprovalStatus.PENDING,
            "created_at": utcnow(), "expires_at": expires_at, "default_on_expiry": default_on_expiry,
        }, conn=c)
        log_event("approval", aid, "created", {"task_id": task_id, "title": title[:200], "kind": kind,
                                               "action_type": action_type, "risk": risk}, conn=c)
    return aid


def get_approval(approval_id: str) -> dict | None:
    return _decode(q1("SELECT * FROM approvals WHERE id = ?", (approval_id,)), *APPROVAL_JSON)


def list_approvals(status: str | list[str] | None = None, task_id: str | None = None, limit: int = 200) -> list[dict]:
    sql, params = "SELECT * FROM approvals WHERE 1=1", []
    if status:
        sts = [status] if isinstance(status, str) else list(status)
        sql += f" AND status IN ({','.join('?' for _ in sts)})"; params += sts
    if task_id:
        sql += " AND task_id = ?"; params.append(task_id)
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    return [_decode(r, *APPROVAL_JSON) for r in q(sql, params)]


def decide_approval(approval_id: str, decision: str, *, note: str | None = None, via: str = "dashboard",
                    actor: str = "user") -> dict:
    """Record a human decision exactly once. Raises ValueError if not pending or option unknown."""
    with tx() as c:
        row = c.execute("SELECT * FROM approvals WHERE id = ?", (approval_id,)).fetchone()
        if row is None:
            raise KeyError(approval_id)
        row = dict(row)
        if row["status"] != ApprovalStatus.PENDING:
            raise ValueError(f"approval {approval_id} is {row['status']}, not pending")
        keys = [o.get("key") for o in (loads(row["options"], []) or [])]
        if decision not in keys:
            raise ValueError(f"unknown option {decision!r}; expected one of {keys}")
        status = ApprovalStatus.REJECTED if decision in ("reject", "no", "cancel") else ApprovalStatus.APPROVED
        db.update("approvals", "id", approval_id, {"status": status, "decision": decision, "decision_note": note,
                                                   "decided_via": via, "decided_at": utcnow()}, conn=c)
        log_event("approval", approval_id, "decided", {"decision": decision, "status": str(status), "via": via,
                                                       "note": (note or "")[:500]}, actor=actor, conn=c)
    return get_approval(approval_id)


def set_approval_status(approval_id: str, status: str, result: Any = None, *, expect: str | None = None) -> bool:
    """Compare-and-set approval status (used for executing/executed transitions, guarantees once-only)."""
    fields: dict[str, Any] = {"status": status}
    if result is not None:
        fields["result"] = dumps(result)
    if status in (ApprovalStatus.EXECUTED, ApprovalStatus.FAILED):
        fields["executed_at"] = utcnow()
    sets = ", ".join(f"{k} = ?" for k in fields)
    sql = f"UPDATE approvals SET {sets} WHERE id = ?" + (" AND status = ?" if expect else "")
    params = [*fields.values(), approval_id] + ([expect] if expect else [])
    ok = db.execute(sql, params) > 0
    if ok:
        log_event("approval", approval_id, "status_changed", {"to": str(status)})
    return ok


# --------------------------------------------------------------------------- artifacts


def add_artifact(title: str, *, kind: str = "file", path: str | Path | None = None, uri: str | None = None,
                 task_id: str | None = None, run_id: str | None = None) -> str:
    aid = new_id("art")
    sha, size = None, None
    if path:
        p = Path(path)
        if p.is_file():
            size = p.stat().st_size
            h = hashlib.sha256()
            with p.open("rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            sha = h.hexdigest()
    db.insert("artifacts", {"id": aid, "task_id": task_id, "run_id": run_id, "kind": kind, "title": title[:300],
                            "path": str(path) if path else None, "uri": uri, "sha256": sha, "bytes": size,
                            "created_at": utcnow()})
    log_event("artifact", aid, "created", {"task_id": task_id, "title": title[:200], "path": str(path) if path else None})
    return aid


def list_artifacts(task_id: str | None = None, limit: int = 300) -> list[dict]:
    if task_id:
        return q("SELECT * FROM artifacts WHERE task_id = ? ORDER BY created_at DESC LIMIT ?", (task_id, limit))
    return q("SELECT * FROM artifacts ORDER BY created_at DESC LIMIT ?", (limit,))


def get_artifact(artifact_id: str) -> dict | None:
    return q1("SELECT * FROM artifacts WHERE id = ?", (artifact_id,))


# --------------------------------------------------------------------------- sensors


def sensor_state(name: str) -> dict:
    row = q1("SELECT * FROM sensor_state WHERE name = ?", (name,))
    if row is None:
        db.insert("sensor_state", {"name": name}, or_ignore=True)
        row = q1("SELECT * FROM sensor_state WHERE name = ?", (name,))
    row["cursor"] = loads(row.get("cursor"), {}) or {}
    return row


def update_sensor_state(name: str, **fields: Any) -> None:
    sensor_state(name)
    if "cursor" in fields and not isinstance(fields["cursor"], (str, type(None))):
        fields["cursor"] = dumps(fields["cursor"])
    db.update("sensor_state", "name", name, fields)


def list_sensor_states() -> list[dict]:
    return [_decode(r, "cursor") for r in q("SELECT * FROM sensor_state ORDER BY name")]


# --------------------------------------------------------------------------- notifications


def add_notification(channel: str, title: str, body: str | None = None, ref_type: str | None = None,
                     ref_id: str | None = None, status: str = "pending", error: str | None = None) -> str:
    nid = new_id("ntf")
    db.insert("notifications", {"id": nid, "channel": channel, "ref_type": ref_type, "ref_id": ref_id, "title": title[:300],
                                "body": body, "status": status, "error": error, "created_at": utcnow(),
                                "sent_at": utcnow() if status == "sent" else None})
    return nid


def list_notifications(limit: int = 100) -> list[dict]:
    return q("SELECT * FROM notifications ORDER BY created_at DESC LIMIT ?", (limit,))


# --------------------------------------------------------------------------- context facts


def upsert_fact(kind: str, key: str, value: str, *, source: str | None = None, confidence: float = 0.6,
                status: str = "active", actor: str = "system", pinned: bool | None = None) -> str | None:
    """Insert/update a context fact. Agent writes never overwrite pinned (user-confirmed) facts."""
    now = utcnow()
    existing = q1("SELECT * FROM context_facts WHERE kind = ? AND key = ?", (kind, key))
    if existing:
        if existing["pinned"] and actor != "user":
            return None
        if existing["value"] == value and existing["status"] == status and pinned is None:
            return existing["id"]
        fields: dict[str, Any] = {"value": value, "source": source, "confidence": confidence, "status": status, "updated_at": now}
        if pinned is not None:
            fields["pinned"] = 1 if pinned else 0
        db.update("context_facts", "id", existing["id"], fields)
        log_event("context", existing["id"], "updated", {"kind": kind, "key": key, "old": existing["value"][:300],
                                                         "new": value[:300], "source": source}, actor=actor)
        return existing["id"]
    fid = new_id("ctx")
    db.insert("context_facts", {"id": fid, "kind": kind, "key": key, "value": value, "source": source,
                                "confidence": confidence, "status": status, "pinned": 1 if pinned else 0,
                                "created_at": now, "updated_at": now})
    log_event("context", fid, "created", {"kind": kind, "key": key, "value": value[:300], "source": source}, actor=actor)
    return fid


def list_facts(kind: str | None = None, status: str | None = "active") -> list[dict]:
    sql, params = "SELECT * FROM context_facts WHERE 1=1", []
    if kind:
        sql += " AND kind = ?"; params.append(kind)
    if status:
        sql += " AND status = ?"; params.append(status)
    sql += " ORDER BY kind, pinned DESC, confidence DESC, updated_at DESC"
    return q(sql, params)


def delete_fact(fact_id: str, actor: str = "user") -> None:
    db.update("context_facts", "id", fact_id, {"status": "rejected", "updated_at": utcnow()})
    log_event("context", fact_id, "rejected", None, actor=actor)


# --------------------------------------------------------------------------- kv


def kv_get(key: str, default: Any = None) -> Any:
    r = q1("SELECT value FROM kv WHERE key = ?", (key,))
    return loads(r["value"], default) if r else default


def kv_set(key: str, value: Any) -> None:
    db.get_conn().execute("INSERT INTO kv(key, value, updated_at) VALUES(?,?,?) ON CONFLICT(key) DO UPDATE SET "
                          "value = excluded.value, updated_at = excluded.updated_at", (key, dumps(value), utcnow()))
