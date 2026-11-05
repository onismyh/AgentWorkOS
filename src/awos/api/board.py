"""Aggregations for the board and the dashboard (pure read-side queries; routes live in app.py)."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from .. import repo
from ..brain import summarize
from ..db import loads, q
from ..models import CATEGORIES, EXECUTORS
from ..timeutil import local_now, local_today, tz

OPEN = ("inbox", "triaged", "ready", "blocked", "running", "waiting_approval", "waiting_user", "review", "snoozed")

COLUMNS: list[dict] = [
    {"key": "suggested", "title": "建议", "hint": "AI 发现的事项，等你决定做不做", "statuses": ["inbox", "triaged", "snoozed"]},
    {"key": "queued", "title": "排队", "hint": "已接受，等待调度或依赖", "statuses": ["ready", "blocked"]},
    {"key": "running", "title": "执行中", "hint": "Agent 正在推进", "statuses": ["running"]},
    {"key": "needs_you", "title": "等你", "hint": "需要你审批、验收或接手", "statuses": ["waiting_approval", "waiting_user", "review"]},
    {"key": "done", "title": "已完成", "hint": "最近完成的", "statuses": ["done"]},
    {"key": "failed", "title": "失败", "hint": "重试耗尽或不可恢复", "statuses": ["failed"]},
    {"key": "dismissed", "title": "已忽略", "hint": "你忽略或取消的", "statuses": ["dismissed", "cancelled"]},
]


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _marks(n: int) -> str:
    return ",".join("?" for _ in range(n))


def _eff_level(t: dict) -> int:
    lvl = int(t.get("level") or 1)
    cap = t.get("level_cap")
    return min(lvl, int(cap)) if cap else lvl


def _snippet(t: dict, n: int = 180) -> str:
    s = (t.get("summary") or t.get("description") or "").replace("\n", " ").strip()
    return s[:n] + ("…" if len(s) > n else "")


def _children(ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    rows = q(f"SELECT parent_id, COUNT(*) total, COALESCE(SUM(status = 'done'), 0) done, COALESCE(SUM(status = 'running'), 0) running "
             f"FROM tasks WHERE parent_id IN ({_marks(len(ids))}) GROUP BY parent_id", ids)
    return {r["parent_id"]: {"total": r["total"], "done": int(r["done"]), "running": int(r["running"])} for r in rows}


def _pending(ids: list[str]) -> dict[str, int]:
    if not ids:
        return {}
    rows = q(f"SELECT task_id, COUNT(*) n FROM approvals WHERE status = 'pending' AND task_id IN ({_marks(len(ids))}) GROUP BY task_id", ids)
    return {r["task_id"]: r["n"] for r in rows}


def task_brief(t: dict, kids: dict | None = None, pending: int = 0) -> dict:
    return {"id": t["id"], "title": t["title"], "status": t["status"], "level": t["level"], "effective_level": _eff_level(t),
            "priority": t["priority"], "category": t["category"], "kind": t["kind"], "executor": t.get("executor"),
            "project": t.get("project"), "due_at": t.get("due_at"), "updated_at": t["updated_at"], "created_at": t["created_at"],
            "cost_usd": round(float(t.get("cost_usd") or 0), 4), "attempts": t["attempts"], "max_attempts": t["max_attempts"],
            "source": t.get("source"), "children": kids or {"total": 0, "done": 0, "running": 0},
            "pending_approvals": pending, "snippet": _snippet(t)}


def board(days: int = 7, limit_per_column: int = 60) -> dict:
    since = _utc(datetime.now(timezone.utc) - timedelta(days=days))
    statuses = [s for c in COLUMNS for s in c["statuses"]]
    rows = q(f"SELECT * FROM tasks WHERE parent_id IS NULL AND status IN ({_marks(len(statuses))}) "
             f"AND (status NOT IN ('done','dismissed','cancelled') OR COALESCE(finished_at, updated_at) >= ?) "
             f"ORDER BY priority ASC, updated_at DESC", [*statuses, since])
    for r in rows:
        r["meta"] = loads(r.get("meta"), {}) or {}
    ids = [r["id"] for r in rows]
    kids, pend = _children(ids), _pending(ids)
    col_of = {s: c["key"] for c in COLUMNS for s in c["statuses"]}
    cols = {c["key"]: [] for c in COLUMNS}
    for r in rows:
        key = col_of[r["status"]]
        if r["status"] == "blocked" and kids.get(r["id"], {}).get("running"):
            key = "running"  # a parent whose sub-tasks are executing is, for the human, "in progress"
        cols[key].append(task_brief(r, kids.get(r["id"]), pend.get(r["id"], 0)))
    out = []
    for c in COLUMNS:
        items = cols[c["key"]]
        total = len(items)
        items = summarize.attach(items[:limit_per_column], "task")
        out.append({**c, "count": total, "items": items})
    return {"columns": out, "updated_at": _utc(datetime.now(timezone.utc)),
            "totals": {"open": sum(len(cols[k]) for k in ("suggested", "queued", "running", "needs_you")), "done_7d": len(cols["done"])}}


# --------------------------------------------------------------------------- dashboard


def dashboard(days: int = 14) -> dict:
    days = max(3, min(int(days), 60))
    today = local_today()
    day_keys = [(today - timedelta(days=days - 1 - i)).isoformat() for i in range(days)]
    labels = [d[5:] for d in day_keys]
    start_local = datetime.combine(today - timedelta(days=days - 1), datetime.min.time(), tzinfo=tz())
    start = _utc(start_local)
    off = local_now().utcoffset() or timedelta(0)
    offset = f"{int(off.total_seconds() // 3600):+d} hours"

    def series(table: str, col: str, where: str = "1=1") -> list[int]:
        rows = q(f"SELECT date({col}, '{offset}') d, COUNT(*) n FROM {table} WHERE {col} >= ? AND {where} GROUP BY d", (start,))
        by = {r["d"]: r["n"] for r in rows}
        return [int(by.get(d, 0)) for d in day_keys]

    cost_rows = q("SELECT day, executor, COALESCE(SUM(cost_usd),0) usd FROM cost_ledger WHERE day >= ? GROUP BY day, executor",
                  (day_keys[0],))
    cost = []
    for d, label in zip(day_keys, labels):
        row = {"day": label, **{e: 0.0 for e in EXECUTORS}}
        for r in cost_rows:
            if r["day"] == d:
                row[r["executor"]] = round(float(r["usd"]), 4)
        row["total"] = round(sum(row[e] for e in EXECUTORS if e in row), 4)
        cost.append(row)

    marks = _marks(len(OPEN))
    open_tasks = q(f"SELECT level, level_cap, category FROM tasks WHERE parent_id IS NULL AND status IN ({marks})", list(OPEN))
    levels = {str(i): 0 for i in range(1, 6)}
    cats: dict[str, int] = {}
    for t in open_tasks:
        levels[str(_eff_level(t))] += 1
        cats[t["category"]] = cats.get(t["category"], 0) + 1

    counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM tasks WHERE parent_id IS NULL GROUP BY status")}
    pending = (q("SELECT COUNT(*) n FROM approvals WHERE status = 'pending'") or [{"n": 0}])[0]["n"]
    since24 = _utc(datetime.now(timezone.utc) - timedelta(hours=24))
    midnight = _utc(datetime.combine(today, datetime.min.time(), tzinfo=tz()))
    failures = (q("SELECT COUNT(*) n FROM runs WHERE status IN ('failed','timeout') AND COALESCE(ended_at, started_at,'') >= ?", (since24,)) or [{"n": 0}])[0]["n"]
    today_cost = repo.cost_today()
    from ..config import load_config
    kpis = {"inbox": sum(counts.get(s, 0) for s in ("review", "waiting_user", "triaged", "failed")), "approvals": pending,
            "running": counts.get("running", 0) + (q("SELECT COUNT(*) n FROM tasks WHERE status = 'blocked' AND id IN (SELECT parent_id FROM tasks WHERE status = 'running')")[0]["n"]),
            "done_today": (q("SELECT COUNT(*) n FROM tasks WHERE status = 'done' AND finished_at >= ?", (midnight,)) or [{"n": 0}])[0]["n"],
            "failures_24h": failures, "cost_today": round(float(today_cost["usd"]), 4),
            "budget_daily": float(load_config().get("budget.daily_usd", 8.0))}

    funnel = {
        "signals": (q("SELECT COUNT(*) n FROM signals WHERE observed_at >= ?", (start,)) or [{"n": 0}])[0]["n"],
        "triaged": (q("SELECT COUNT(*) n FROM signals WHERE observed_at >= ? AND status = 'triaged'", (start,)) or [{"n": 0}])[0]["n"],
        "tasks": (q("SELECT COUNT(*) n FROM tasks WHERE parent_id IS NULL AND created_at >= ?", (start,)) or [{"n": 0}])[0]["n"],
        "executed": (q("SELECT COUNT(DISTINCT task_id) n FROM runs WHERE purpose = 'execute' AND started_at >= ?", (start,)) or [{"n": 0}])[0]["n"],
        "delivered": (q("SELECT COUNT(*) n FROM tasks WHERE parent_id IS NULL AND status IN ('done','review','waiting_user') AND finished_at >= ?", (start,)) or [{"n": 0}])[0]["n"],
    }

    execs = []
    for r in q("SELECT executor, COUNT(*) runs, SUM(status = 'succeeded') ok, SUM(status IN ('failed','timeout','interrupted')) failed, "
               "COALESCE(SUM(cost_usd),0) cost, AVG(CASE WHEN status = 'succeeded' AND ended_at IS NOT NULL AND started_at IS NOT NULL "
               "THEN (julianday(ended_at) - julianday(started_at)) * 86400 END) avg_s FROM runs WHERE started_at >= ? GROUP BY executor", (start,)):
        execs.append({"name": r["executor"], "runs": r["runs"], "ok": int(r["ok"] or 0), "failed": int(r["failed"] or 0),
                      "cost": round(float(r["cost"]), 4), "avg_s": round(float(r["avg_s"]), 1) if r["avg_s"] else None})

    sensors = []
    for s in repo.list_sensor_states():
        enabled = load_config().get(f"sensors.{s['name']}.enabled", False)
        if enabled:
            sensors.append({"name": s["name"], "enabled": bool(s.get("enabled", 1)), "last_ok": s.get("last_ok_at"),
                            "next": s.get("next_run_at"), "new": s.get("last_count") or 0,
                            "fails": s.get("consecutive_failures") or 0, "error": (s.get("last_error") or None)})

    return {
        "kpis": kpis,
        "series": {"days": labels, "tasks_created": series("tasks", "created_at", "parent_id IS NULL"),
                   "tasks_done": series("tasks", "finished_at", "status = 'done' AND parent_id IS NULL"),
                   "signals": series("signals", "observed_at"), "runs": series("runs", "started_at"),
                   "failures": series("runs", "started_at", "status IN ('failed','timeout')"), "cost": cost},
        "levels": levels,
        "categories": [{"key": k, "label": CATEGORIES.get(k, k).split(" / ")[0].split("（")[0], "count": n}
                       for k, n in sorted(cats.items(), key=lambda x: -x[1])],
        "funnel": funnel, "executors": execs, "sensors": sensors,
        "keywords": summarize.keyword_ranking(24), "activity": activity(30),
    }


_TASK_EVENT = {"created": "新建任务", "retried": "重试了任务", "recovered": "任务被恢复"}
_STATUS_WORD = {"inbox": "收件箱", "triaged": "待你决定", "ready": "排队", "blocked": "等待子任务", "running": "开始执行",
                "waiting_approval": "等你审批", "waiting_user": "等你接手", "review": "待你验收", "done": "已完成",
                "failed": "失败", "cancelled": "已取消", "dismissed": "已忽略", "snoozed": "已推迟"}


def activity(limit: int = 30) -> list[dict]:
    """Human-readable recent happenings (task lifecycle, approvals, deliverables, sensors with news)."""
    events = repo.list_events(limit=limit * 5)
    wanted = []
    for e in events:
        et, ty = e["entity_type"], e["type"]
        if (et == "task" and ty in ("created", "status_changed", "retried", "recovered")) or (et == "approval" and ty in ("created", "decided")) \
                or (et == "artifact" and ty == "created") or (et == "sensor" and ty == "collected" and (e.get("data") or {}).get("new")):
            wanted.append(e)
    titles: dict[tuple[str, str], str] = {}
    for kind, table in (("task", "tasks"), ("approval", "approvals"), ("artifact", "artifacts")):
        ids = sorted({e["entity_id"] for e in wanted if e["entity_type"] == kind and e.get("entity_id")})
        if ids:
            for r in q(f"SELECT id, title FROM {table} WHERE id IN ({_marks(len(ids))})", ids):
                titles[(kind, r["id"])] = r["title"]
    out = []
    for e in wanted:
        et, ty, d = e["entity_type"], e["type"], e.get("data") or {}
        title = titles.get((et, e.get("entity_id") or ""), e.get("entity_id") or "")
        sub = e["actor"]
        if et == "task" and ty == "status_changed":
            if d.get("from") == d.get("to"):
                continue
            text = f"{title} → {_STATUS_WORD.get(d.get('to'), d.get('to'))}"
        elif et == "task":
            text = f"{_TASK_EVENT.get(ty, ty)}：{title}"
        elif et == "approval" and ty == "created":
            text = f"待你确认：{title}"
        elif et == "approval":
            text = f"{'你' if e['actor'] == 'user' else '系统'}处理了审批（{d.get('decision')}）：{title}"
        elif et == "artifact":
            text = f"新交付物：{title}"
        else:
            text = f"传感器 {e.get('entity_id')} 发现 {d.get('new')} 条新信号"
        out.append({"ts": e["ts"], "type": f"{et}.{ty}", "text": text[:140], "entity_type": et, "entity_id": e.get("entity_id"), "actor": sub})
        if len(out) >= limit:
            break
    return out


# --------------------------------------------------------------------------- unified inbox feed

SECTION_ORDER = ["decision", "approval", "review", "handoff", "suggested", "failed"]
SECTION_TITLES = {"decision": "需你决定", "approval": "需你批准", "review": "待验收", "handoff": "待接手",
                  "suggested": "AI 的新建议", "failed": "失败"}


def inbox_feed(approvals: list[dict], review: list[dict], handoff: list[dict], suggested: list[dict], failed: list[dict]) -> list[dict]:
    """One flat, urgency-ordered list for the split-view inbox. Inputs already carry digests."""
    feed: list[dict] = []

    def add(section: str, kind: str, it: dict, ts: str | None) -> None:
        feed.append({"section": section, "section_title": SECTION_TITLES[section], "kind": kind, "id": it["id"],
                     "task_id": it.get("task_id") if kind == "approval" else it["id"], "headline": it.get("headline") or it.get("title"),
                     "gist": it.get("gist") or "", "keywords": it.get("keywords") or [], "digest_auto": it.get("digest_auto", True),
                     "title": it.get("title"), "level": it.get("effective_level") or it.get("task_level") or it.get("level"),
                     "priority": it.get("priority"), "risk": it.get("risk"), "action_type": it.get("action_type"),
                     "status": it.get("status"), "ts": ts, "expires_at": it.get("expires_at")})

    for a in approvals:
        add("decision" if a["kind"] == "decision" else "approval", "approval", a, a.get("created_at"))
    for sec, rows in (("review", review), ("handoff", handoff), ("suggested", suggested), ("failed", failed)):
        for t in rows:
            add(sec, "task", t, t.get("updated_at"))
    order = {s: i for i, s in enumerate(SECTION_ORDER)}
    feed.sort(key=lambda x: (order[x["section"]], x.get("priority") if x.get("priority") is not None else 9, -(datetime.fromisoformat((x["ts"] or "1970-01-01T00:00:00Z").replace("Z", "+00:00")).timestamp())))
    return feed
