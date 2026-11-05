"""Daemon scheduler: the single loop that senses, triages, dispatches, recovers, expires and digests."""
from __future__ import annotations

import logging
import os
import socket
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from .. import db, repo
from ..config import load_config
from ..db import utcnow
from ..models import TaskStatus
from ..timeutil import local_now
from .worker import on_task_finished, process_task  # noqa: F401  (re-exported for actions.complete_task)

log = logging.getLogger("awos.scheduler")


class Scheduler:
    def __init__(self) -> None:
        cfg = load_config()
        self.owner = f"{socket.gethostname()}:{os.getpid()}"
        self.tick_s = float(cfg.get("scheduler.tick_seconds", 15))
        self.max_runs = int(cfg.get("scheduler.max_concurrent_runs", 3))
        self.workers = ThreadPoolExecutor(max_workers=self.max_runs, thread_name_prefix="worker")
        self.bg = ThreadPoolExecutor(max_workers=3, thread_name_prefix="bg")
        self.active: dict[str, Future] = {}
        self.bg_jobs: dict[str, Future] = {}
        self.stop_event = threading.Event()
        self.started_at = utcnow()

    # ------------------------------------------------------------------ lifecycle
    def run_forever(self) -> None:
        from .recovery import startup_recovery
        startup_recovery(self.owner)
        log.info("scheduler started owner=%s tick=%ss max_runs=%s", self.owner, self.tick_s, self.max_runs)
        while not self.stop_event.is_set():
            t0 = time.monotonic()
            try:
                self.tick()
            except Exception:
                log.exception("tick failed")
            self.stop_event.wait(max(1.0, self.tick_s - (time.monotonic() - t0)))
        log.info("scheduler stopping; waiting for %d active runs", len(self.active))
        self.workers.shutdown(wait=False, cancel_futures=True)
        self.bg.shutdown(wait=False, cancel_futures=True)

    def stop(self) -> None:
        self.stop_event.set()

    # ------------------------------------------------------------------ tick
    def tick(self) -> dict:
        repo.kv_set("daemon.heartbeat", {"ts": utcnow(), "owner": self.owner, "pid": os.getpid(), "started_at": self.started_at,
                                         "active": list(self.active)})
        self._reap()
        self._sensors()
        self._background("triage", self._triage_job)
        self._digests()
        self._topics()
        self._flush_reminders()
        self._wake_snoozed()
        from ..hitl.outbox import expire_approvals
        expire_approvals()
        self._unblock()
        self._run_approved_actions()
        dispatched = self._dispatch()
        self._recover_expired()
        self._daily_jobs()
        return {"dispatched": dispatched, "active": len(self.active)}

    def _digests(self) -> None:
        """Card summaries for the inbox/board: at most one cheap batch every 5 minutes, only when something is missing."""
        last = repo.kv_get("digests.last_run")
        if last and db.parse_ts(last) > datetime.now(timezone.utc) - timedelta(minutes=5):
            return
        repo.kv_set("digests.last_run", utcnow())
        self._background("digests", _digest_cards_job)

    @staticmethod
    def _flush_reminders() -> None:
        from ..hitl.batcher import flush
        try:
            flush()
        except Exception:
            log.exception("reminder flush failed")

    def _topics(self) -> None:
        """Group new items into topics and refresh stale topic briefs (recaps): throttled to once per 5 minutes."""
        last = repo.kv_get("topics.last_run")
        if last and db.parse_ts(last) > datetime.now(timezone.utc) - timedelta(minutes=5):
            return
        repo.kv_set("topics.last_run", utcnow())
        self._background("topics", _topics_job)

    def _reap(self) -> None:
        for tid, fut in list(self.active.items()):
            if fut.done():
                self.active.pop(tid, None)
                if fut.exception():
                    log.error("worker for %s raised: %r", tid, fut.exception())
        for name, fut in list(self.bg_jobs.items()):
            if fut.done():
                self.bg_jobs.pop(name, None)
                if fut.exception():
                    log.error("bg job %s raised: %r", name, fut.exception())

    def _background(self, name: str, fn, *args) -> bool:
        if name in self.bg_jobs:
            return False
        self.bg_jobs[name] = self.bg.submit(fn, *args)
        return True

    def _sensors(self) -> None:
        from ..sensors import all_sensors, is_due, run_sensor
        for name in all_sensors():
            try:
                if is_due(name):
                    self._background(f"sensor:{name}", run_sensor, name)
            except Exception:
                log.exception("sensor scheduling failed for %s", name)

    @staticmethod
    def _triage_job() -> None:
        from ..brain.triage import triage_once
        for _ in range(4):  # drain up to 4 batches per tick
            stats = triage_once()
            if not stats.get("signals") and not stats.get("tasks"):
                break
            if not stats.get("ok", True):
                break

    def _wake_snoozed(self) -> None:
        for t in repo.list_tasks(status=TaskStatus.SNOOZED, limit=200):
            if t.get("snoozed_until") and t["snoozed_until"] <= utcnow():
                repo.set_task_status(t["id"], TaskStatus.TRIAGED, "snooze ended")

    def _unblock(self) -> None:
        for t in repo.list_tasks(status=TaskStatus.BLOCKED, limit=200):
            kids = repo.children(t["id"])
            if kids:
                if all(k["status"] in ("done", "review", "waiting_user", "failed", "cancelled", "dismissed") for k in kids):
                    on_task_finished(kids[-1]["id"])
            elif not repo.dependencies(t["id"]):
                repo.set_task_status(t["id"], TaskStatus.READY, "no remaining blockers")

    def _run_approved_actions(self) -> None:
        """Execute actions that were approved elsewhere (CLI) or whose inline thread never started."""
        from ..hitl.outbox import execute_action
        for a in repo.list_approvals(status="approved", limit=50):
            if a["kind"] == "action":
                self._background(f"action:{a['id']}", execute_action, a)

    def _executor_ok(self, task: dict) -> bool:
        return True

    def _dispatch(self) -> int:
        n = 0
        lease_s = int(load_config().get("scheduler.lease_seconds", 300))
        while len(self.active) < self.max_runs and not self.stop_event.is_set():
            task = repo.claim_next_task(self.owner, lease_s, executor_ok=self._executor_ok)
            if not task:
                break
            self.active[task["id"]] = self.workers.submit(process_task, task["id"], self.owner)
            n += 1
        return n

    def _recover_expired(self) -> None:
        from .recovery import recover_task
        for t in repo.expired_leases():
            if t["id"] in self.active:
                repo.renew_lease(t["id"], self.owner, int(load_config().get("scheduler.lease_seconds", 300)))
                continue
            recover_task(t, "lease expired")

    def _daily_jobs(self) -> None:
        cfg = load_config()
        now = local_now()
        today = now.strftime("%Y-%m-%d")
        if repo.kv_get("backup.last_day") != today:
            self._background("backup", _backup_job, today)
        last = repo.kv_get("context.last_refresh")
        hours = float(cfg.get("context.refresh_hours", 24))
        if not last or db.parse_ts(last) < datetime.now(timezone.utc) - timedelta(hours=hours):
            if repo.kv_get("context.auto", True):
                self._background("context", _context_job)
        for hm in cfg.get("notify.digest_times", []) or []:
            key = f"digest.{today}.{hm}"
            if now.strftime("%H:%M") >= hm and not repo.kv_get(key):
                repo.kv_set(key, utcnow())
                self._background(f"digest:{hm}", _digest_job, hm)


def _topics_job() -> None:
    from ..brain import projects
    from ..brain.topics import ensure_topics, refresh_briefs
    last = repo.kv_get("projects.last_consolidate")
    if not last or db.parse_ts(last) < datetime.now(timezone.utc) - timedelta(hours=24):
        repo.kv_set("projects.last_consolidate", utcnow())
        projects.consolidate_projects()  # demote working notes, name/summarise the canonical projects (once a day)
    ensure_topics()
    projects.link_topics()
    refresh_briefs()


def _digest_cards_job() -> None:
    from ..brain.summarize import ensure_digests
    ensure_digests()


def _backup_job(today: str) -> None:
    cfg = load_config()
    dest = cfg.path("backups") / f"awos-{today}.db"
    db.backup(dest)
    backups = sorted(cfg.path("backups").glob("awos-*.db"))
    for old in backups[:-14]:
        old.unlink(missing_ok=True)
    repo.kv_set("backup.last_day", today)
    repo.log_event("system", None, "backup", {"path": str(dest)})


def _context_job() -> None:
    from ..brain.context import refresh_context
    repo.kv_set("context.last_refresh", utcnow())  # avoid hammering on repeated failures
    refresh_context()


def _digest_job(hm: str) -> None:
    from .digest import send_digest
    send_digest(hm)
