"""Coalesced reminders. Instead of one toast per event (and so one "thread" per topic), reminders are queued and sent as
ONE message that lists the topics waiting for the user. High-risk items still go out immediately (with everything that
was already queued); everything else waits for a short window and for the end of quiet hours."""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from .. import repo
from ..db import utcnow

log = logging.getLogger("awos.batcher")
KEY = "notify.queue"
WINDOW_S = 600


def _topic_title(ref_type: str | None, ref_id: str | None) -> str:
    try:
        from ..brain.topics import topic_for_task
        if ref_type == "approval" and ref_id:
            a = repo.get_approval(ref_id)
            ref_type, ref_id = "task", (a or {}).get("task_id")
        if ref_type == "task" and ref_id:
            t = topic_for_task(ref_id)
            if t:
                return t["title"]
    except Exception:
        log.debug("topic lookup failed", exc_info=True)
    return "其他"


def enqueue(title: str, body: str = "", *, level: str = "action", ref_type: str | None = None, ref_id: str | None = None) -> None:
    queue: list[dict[str, Any]] = list(repo.kv_get(KEY, []) or [])
    queue.append({"ts": utcnow(), "title": title[:120], "body": body[:300], "level": level, "ref_type": ref_type, "ref_id": ref_id,
                  "topic": _topic_title(ref_type, ref_id)})
    repo.kv_set(KEY, queue[-60:])
    if level == "high":
        flush(force=True)


def compose(queue: list[dict]) -> tuple[str, str, str]:
    """(title, body, level) for one message covering the queue, grouped by topic."""
    groups: dict[str, list[dict]] = {}
    for q_ in queue:
        groups.setdefault(q_["topic"], []).append(q_)
    n = len(queue)
    title = f"有 {n} 件事等你" + (f"（{len(groups)} 个话题）" if len(groups) > 1 else f"：{next(iter(groups))}")
    lines = []
    for topic, items in sorted(groups.items(), key=lambda kv: -len(kv[1]))[:5]:
        lines.append(f"• {topic}：{len(items)} 项 — {items[-1]['title'][:44]}")
    if len(groups) > 5:
        lines.append(f"…另有 {len(groups) - 5} 个话题")
    lines.append("打开收件箱按话题查看前情提要。")
    return title, "\n".join(lines), "high" if any(q_["level"] == "high" for q_ in queue) else "action"


def flush(force: bool = False) -> bool:
    """Send one combined reminder if the oldest queued item has waited WINDOW_S (or force). Quiet hours hold it back
    unless something is high-priority. Returns True when a message was sent."""
    queue: list[dict] = list(repo.kv_get(KEY, []) or [])
    if not queue:
        return False
    high = any(q_["level"] == "high" for q_ in queue)
    from .notify import dashboard_url, in_quiet_hours, notify
    if in_quiet_hours() and not high:
        return False
    oldest = datetime.strptime(queue[0]["ts"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    if not force and (datetime.now(timezone.utc) - oldest).total_seconds() < WINDOW_S:
        return False
    title, body, level = compose(queue)
    repo.kv_set(KEY, [])
    try:
        notify(title, body, level=level, ref_type="digest", ref_id=None, url=dashboard_url("inbox"))
    except Exception:
        log.exception("combined reminder failed")
        return False
    return True
