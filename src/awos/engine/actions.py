"""Human / API facing operations. The dashboard, CLI and signed links call ONLY these functions."""
from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any

from .. import repo
from ..db import utcnow
from ..models import TaskStatus


def add_manual_task(title: str, description: str | None = None, *, level: int | None = None, category: str | None = None,
                    priority: int = 2, kind: str = "todo", executor: str | None = None, triage: bool = True,
                    project: str | None = None, due_at: str | None = None, actor: str = "user") -> str:
    """Create a task from the human. If triage=True it goes through triage (classification + level);
    otherwise it is created READY with the given level/category."""
    status = TaskStatus.INBOX if triage else TaskStatus.READY
    tid = repo.create_task(title, description=description, kind=kind, category=category or "other", priority=priority,
                           level=level or 1, status=status, executor=executor, project=project, due_at=due_at,
                           source="manual", actor=actor,
                           meta={"manual": True, "level_locked": level is not None, "category_locked": category is not None})
    return tid


def accept_task(task_id: str, actor: str = "user", note: str | None = None) -> None:
    """Human accepts a triaged suggestion -> queue it for the agents."""
    if note:
        _append_note(task_id, note, actor)
    repo.set_task_status(task_id, TaskStatus.READY, "accepted by user", actor=actor)


def retry_task(task_id: str, actor: str = "user", note: str | None = None) -> None:
    t = repo.get_task(task_id)
    if t is None:
        raise KeyError(task_id)
    if note:
        _append_note(task_id, note, actor)
    if t["status"] == TaskStatus.RUNNING:
        cancel_task_runs(task_id)
    repo.update_task(task_id, actor=actor, next_attempt_at=None, max_attempts=max(t["max_attempts"], t["attempts"] + 1))
    repo.set_task_status(task_id, TaskStatus.READY, "manual retry", actor=actor, force=True)
    repo.log_event("task", task_id, "retried", {"note": note}, actor=actor)


def rework_task(task_id: str, feedback: str, actor: str = "user") -> None:
    """Send a reviewed deliverable back to the agents with feedback."""
    _append_note(task_id, f"[返工意见] {feedback}", actor)
    t = repo.get_task(task_id)
    repo.update_task(task_id, actor=actor, max_attempts=max(t["max_attempts"], t["attempts"] + 2))
    repo.set_task_status(task_id, TaskStatus.READY, "rework requested", actor=actor, force=True)


def complete_task(task_id: str, actor: str = "user", note: str | None = None) -> None:
    if note:
        _append_note(task_id, note, actor)
    repo.set_task_status(task_id, TaskStatus.DONE, note or "completed by user", actor=actor, force=True)
    from .scheduler import on_task_finished
    on_task_finished(task_id)


def cancel_task_runs(task_id: str) -> int:
    from ..llm import cancel_run
    n = 0
    for r in repo.list_runs(task_id=task_id, status="running"):
        n += 1 if cancel_run(r["id"]) else 0
    return n


def cancel_task(task_id: str, actor: str = "user", reason: str | None = None) -> None:
    cancel_task_runs(task_id)
    for ch in repo.children(task_id):
        if ch["status"] not in ("done", "failed", "cancelled", "dismissed"):
            cancel_task(ch["id"], actor=actor, reason="parent cancelled")
    for a in repo.list_approvals(status="pending", task_id=task_id):
        repo.set_approval_status(a["id"], "cancelled", expect="pending")
    repo.set_task_status(task_id, TaskStatus.CANCELLED, reason or "cancelled by user", actor=actor, force=True)


def dismiss_task(task_id: str, actor: str = "user", reason: str | None = None) -> None:
    repo.set_task_status(task_id, TaskStatus.DISMISSED, reason or "dismissed", actor=actor, force=True)


def snooze_task(task_id: str, hours: float = 24, actor: str = "user") -> None:
    until = (datetime.utcnow() + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo.update_task(task_id, actor=actor, snoozed_until=until)
    repo.set_task_status(task_id, TaskStatus.SNOOZED, f"snoozed until {until}", actor=actor, force=True)


def set_task_fields(task_id: str, actor: str = "user", **fields: Any) -> None:
    allowed = {"level", "priority", "category", "kind", "executor", "title", "description", "due_at", "project", "acceptance"}
    clean = {k: v for k, v in fields.items() if k in allowed and v is not None}
    if "level" in clean:
        clean["level"] = max(1, min(5, int(clean["level"])))
        repo.merge_task_meta(task_id, level_locked=True)
    if "priority" in clean:
        clean["priority"] = max(0, min(3, int(clean["priority"])))
    if clean:
        repo.update_task(task_id, actor=actor, **clean)


def decide(approval_id: str, option: str, note: str | None = None, via: str = "dashboard", actor: str = "user") -> dict:
    approval = repo.decide_approval(approval_id, option, note=note, via=via, actor=actor)
    from ..hitl.outbox import on_decision
    on_decision(approval)
    return repo.get_approval(approval_id)


def run_sensor_now(name: str) -> None:
    from ..sensors import run_sensor
    threading.Thread(target=run_sensor, args=(name, True), daemon=True, name=f"sensor-{name}").start()


def add_context_note(kind: str, key: str, value: str, actor: str = "user") -> str | None:
    return repo.upsert_fact(kind, key, value, source="user", confidence=1.0, actor="user", pinned=True)


def _append_note(task_id: str, note: str, actor: str) -> None:
    t = repo.get_task(task_id) or {}
    notes = list((t.get("meta") or {}).get("notes", []))
    notes.append({"ts": utcnow(), "by": actor, "text": note})
    repo.merge_task_meta(task_id, notes=notes)
