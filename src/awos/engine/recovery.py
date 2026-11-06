"""Crash recovery: orphaned runs, expired leases, half-executed actions."""
from __future__ import annotations

import ctypes
import logging
import sys
from datetime import datetime, timedelta, timezone

from .. import repo
from ..config import load_config
from ..db import utcnow
from ..models import ApprovalStatus, TaskStatus
from ..osutil import kill_process_tree

log = logging.getLogger("awos.recovery")


def pid_alive(pid: int | None) -> bool:
    if not pid:
        return False
    if sys.platform == "win32":
        k32 = ctypes.windll.kernel32
        h = k32.OpenProcess(0x1000, False, int(pid))  # PROCESS_QUERY_LIMITED_INFORMATION
        if not h:
            return False
        try:
            code = ctypes.c_ulong()
            k32.GetExitCodeProcess(h, ctypes.byref(code))
            return code.value == 259  # STILL_ACTIVE
        finally:
            k32.CloseHandle(h)
    import os
    try:
        os.kill(int(pid), 0)
        return True
    except OSError:
        return False


def kill_tree(pid: int) -> None:
    kill_process_tree(int(pid))


def recover_task(task: dict, reason: str) -> None:
    """Put an orphaned RUNNING task back in the queue (or fail it when attempts are exhausted)."""
    for r in repo.list_runs(task_id=task["id"], status="running"):
        if r.get("pid") and pid_alive(r["pid"]):
            kill_tree(r["pid"])
        repo.update_run(r["id"], status="interrupted", ended_at=utcnow(), error=f"interrupted: {reason}", error_class="transient")
    t = repo.get_task(task["id"])
    if not t or t["status"] != TaskStatus.RUNNING:
        return
    if t["attempts"] >= t["max_attempts"]:
        repo.set_task_status(t["id"], TaskStatus.FAILED, f"{reason}; attempts exhausted")
        return
    seq = load_config().get("scheduler.retry_backoff_seconds", [60, 300, 1800])
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=int(seq[0]))).strftime("%Y-%m-%dT%H:%M:%SZ")
    repo.set_task_status(t["id"], TaskStatus.READY, f"recovered: {reason}", extra={"next_attempt_at": nxt})
    repo.log_event("task", t["id"], "recovered", {"reason": reason})


def startup_recovery(owner: str) -> dict:
    stats = {"runs": 0, "tasks": 0, "actions": 0}
    for r in repo.list_runs(status="running", limit=1000) + repo.list_runs(status="queued", limit=1000):
        if r.get("pid") and pid_alive(r["pid"]):
            kill_tree(r["pid"])
        repo.update_run(r["id"], status="interrupted", ended_at=utcnow(), error="daemon restarted", error_class="transient")
        stats["runs"] += 1
    for t in repo.list_tasks(status=TaskStatus.RUNNING, limit=1000):
        if t.get("lease_owner") != owner:
            recover_task(t, "daemon restarted")
            stats["tasks"] += 1
    # Never re-run an action that may have half-executed: fail it and hand back to the human.
    for a in repo.list_approvals(status=ApprovalStatus.EXECUTING, limit=200):
        repo.set_approval_status(a["id"], ApprovalStatus.FAILED,
                                 result={"done": False, "error": "daemon restarted while executing; please verify manually"})
        stats["actions"] += 1
        if a.get("task_id"):
            t = repo.get_task(a["task_id"])
            if t and t["status"] == TaskStatus.WAITING_APPROVAL:
                repo.set_task_status(t["id"], TaskStatus.WAITING_USER, "动作执行被中断，请人工核实")
    # Approved-but-not-started actions are safe to start again.
    for a in repo.list_approvals(status=ApprovalStatus.APPROVED, limit=200):
        if a["kind"] == "action":
            from ..hitl.outbox import execute_action
            import threading
            threading.Thread(target=execute_action, args=(a,), daemon=True).start()
    repo.log_event("system", None, "startup_recovery", stats)
    log.info("startup recovery: %s", stats)
    return stats
