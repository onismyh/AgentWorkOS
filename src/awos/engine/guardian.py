"""Project guardian: once a day, look at every project's health and hand the troubled ones to an agent.

The board shows the user where to look (api/timeline.py computes each project's health). The guardian makes sure somebody
actually looks: for a project that is at risk or stalled it queues one analysis task (L2, read-only: what is it stuck on, what
is the smallest next step, what does it need from the user). Bounded so it never becomes noise: at most 3 new tasks per run,
one open guardian task per project, a week of quiet after the last one (three weeks if the user dismissed it).
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from .. import db, repo
from ..config import load_config
from ..models import TERMINAL, TaskStatus
from ..timeutil import local_now

log = logging.getLogger("awos.guardian")

WATCH = ("risk", "stalled")           # health levels that get an agent's attention ("attention" = it is waiting for the user: that is the inbox's job)
MAX_PER_RUN = 3
QUIET_DAYS = 7
QUIET_DAYS_AFTER_DISMISS = 21
UNASSIGNED = "__none__"
SOURCE = "guardian"


def _blocked(key: str, now: datetime) -> str | None:
    """Why this project must not get a new guardian task right now (None = it may)."""
    rows = db.q("SELECT status, created_at FROM tasks WHERE source = ? AND project = ? ORDER BY created_at DESC LIMIT 5", (SOURCE, key))
    if any(TaskStatus(r["status"]) not in TERMINAL for r in rows):
        return "上一次巡检任务还没处理完"
    if rows:
        last = db.parse_ts(rows[0]["created_at"])
        days = QUIET_DAYS_AFTER_DISMISS if rows[0]["status"] in (TaskStatus.DISMISSED, TaskStatus.CANCELLED) else QUIET_DAYS
        if now - last < timedelta(days=days):
            return f"{days} 天内已经查过"
    return None


def _create(lane: dict, week: str) -> str:
    from ..brain.summarize import basis_for_task
    h = lane.get("health") or {}
    name, label, reason = lane.get("name") or lane["key"], h.get("label", ""), h.get("reason", "")
    risk = h.get("level") == "risk"
    desc = (f"系统每天检查所有项目。**{name}**：{label}（{reason}）。\n\n现状：{lane.get('state') or '（暂无摘要）'}\n\n"
            "请只做分析，不要改动任何文件：\n1. 查明它卡在哪（看项目目录、相关任务、最近进展和截止日期）；\n"
            "2. 给出最小的下一步；\n3. 需要用户拍板或补充的事写进 decisions_needed，没有就不写。")
    tid = repo.create_task(
        f"项目巡检：{name}（{label}）", description=desc, kind="todo", category="pm", priority=1 if risk else 2, level=2,
        level_reason="项目巡检只做分析，不改任何东西", status=TaskStatus.READY, project=lane["key"], source=SOURCE,
        dedupe_key=repo.fingerprint(SOURCE, lane["key"], week), acceptance="说明卡点、最小的下一步，以及需要用户决定的事（如有）",
        meta={"level_locked": True, "category_locked": True, "guardian": {"level": h.get("level"), "reason": reason}}, actor="system")
    t = repo.get_task(tid)
    repo.set_digest("task", tid, f"{name}{label}：查明卡点"[:26], [name, label], gist=reason[:50], basis=basis_for_task(t), source=SOURCE)
    return tid


def review_projects(now: datetime | None = None) -> dict:
    """Queue analysis tasks for troubled projects; returns what was created and what was skipped (and why)."""
    from ..api import timeline
    now = now or datetime.now(timezone.utc)
    week = now.astimezone(local_now().tzinfo).strftime("%G-W%V")
    created: list[dict] = []
    skipped: list[dict] = []
    for lane in timeline.lanes()["projects"]:          # already in attention order: risk first
        level = (lane.get("health") or {}).get("level")
        if lane["key"] == UNASSIGNED or level not in WATCH:
            continue
        why = _blocked(lane["key"], now) if len(created) < MAX_PER_RUN else "今天已经派了 3 个"
        if why:
            skipped.append({"project": lane["key"], "why": why})
            continue
        created.append({"project": lane["key"], "task": _create(lane, week), "level": level})
    repo.kv_set("guardian.last_run", {"ts": db.utcnow(), "created": len(created), "skipped": len(skipped)})
    repo.log_event("system", None, "guardian_review", {"created": created, "skipped": skipped}, actor="system")
    return {"created": created, "skipped": skipped}


def enabled() -> bool:
    return bool(load_config().get("guardian.enabled", True))


def due(now_local: datetime) -> bool:
    """Once a day, from `guardian.hour` (Beijing time) on."""
    return enabled() and now_local.hour >= int(load_config().get("guardian.hour", 11)) and repo.kv_get("guardian.last_day") != now_local.strftime("%Y-%m-%d")
