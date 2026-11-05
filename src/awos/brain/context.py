"""Personal context: brief for prompts, periodic refresh from sensors + activity, profile.md rendering."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta

from .. import repo
from ..config import load_config
from ..db import q, utcnow
from ..timeutil import local_now
from . import prompts
from .schemas import CONTEXT_SCHEMA

log = logging.getLogger("awos.context")

KIND_TITLES = {"role": "身份与职责", "goal": "长期目标", "project": "正在推进的项目", "deadline": "截止日期",
               "interest": "技术兴趣", "habit": "日常习惯", "preference": "偏好", "person": "协作者", "note": "备注"}
KIND_ORDER = ["role", "goal", "project", "deadline", "preference", "habit", "interest", "person", "note"]


def context_brief(max_chars: int = 3500) -> str:
    """Compact markdown description of the user for prompts."""
    facts = repo.list_facts(status="active")
    focus = repo.kv_get("context.focus")
    lines = [f"当前时间（北京时间）：{local_now().strftime('%Y-%m-%d %H:%M %A')}"]
    if focus:
        lines += ["### 近期工作重心", focus.strip()]
    by_kind: dict[str, list[dict]] = {}
    for f in facts:
        by_kind.setdefault(f["kind"], []).append(f)
    limits = {"project": 10, "person": 5, "note": 5, "interest": 6}
    for kind in KIND_ORDER:
        items = by_kind.get(kind) or []
        if not items:
            continue
        lines.append(f"### {KIND_TITLES.get(kind, kind)}")
        for f in items[: limits.get(kind, 8)]:
            lines.append(f"- **{f['key']}**：{f['value']}")
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[: max_chars - 20] + "\n…（已截断）"
    return out if facts or focus else out + "\n（个人上下文尚在建立中）"


def render_profile() -> str:
    facts = repo.list_facts(status="active")
    focus = repo.kv_get("context.focus") or ""
    md = [f"# 个人工作上下文（自动维护，更新于 {local_now().strftime('%Y-%m-%d %H:%M')}）", "",
          "> 在 Dashboard「上下文」页面可以编辑；用户编辑过的条目会被标记为 pinned，Agent 不会覆盖。", ""]
    if focus:
        md += ["## 近期工作重心", focus, ""]
    by_kind: dict[str, list[dict]] = {}
    for f in facts:
        by_kind.setdefault(f["kind"], []).append(f)
    for kind in KIND_ORDER:
        if by_kind.get(kind):
            md.append(f"## {KIND_TITLES.get(kind, kind)}")
            for f in by_kind[kind]:
                pin = " 📌" if f["pinned"] else ""
                md.append(f"- **{f['key']}**{pin}：{f['value']}  <sub>({f['source'] or '-'}, {f['confidence']:.1f})</sub>")
            md.append("")
    text = "\n".join(md)
    path = load_config().path("context") / "profile.md"
    path.write_text(text, encoding="utf-8")
    return text


def _observations() -> str:
    from ..sensors import all_sensors
    from ..sensors.base import SensorContext

    chunks: list[str] = []
    for name, sensor in all_sensors().items():
        cfg = load_config().get(f"sensors.{name}", {}) or {}
        if not cfg.get("enabled", False):
            continue
        try:
            st = repo.sensor_state(name)
            snap = sensor.context_snapshot(SensorContext(config=cfg, cursor=dict(st.get("cursor") or {}),
                                                         log=logging.getLogger(f"awos.sensors.{name}")))
        except Exception as e:  # a broken sensor must not block context refresh
            log.warning("context_snapshot %s failed: %s", name, e)
            continue
        if snap:
            chunks.append(f"### 来源：{name}\n{snap.strip()[:3000]}")
    since = (datetime.utcnow() - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
    sigs = q("SELECT source, title, occurred_at FROM signals WHERE observed_at >= ? AND source IN "
             "('gmail','notion','mailmaster','zotero','inbox_drop') ORDER BY observed_at DESC LIMIT 60", (since,))
    if sigs:
        chunks.append("### 最近的邮件/Notion/文献标题\n" + "\n".join(f"- [{s['source']}] {s['title']}" for s in sigs))
    return "\n\n".join(chunks)[:16000] or "（暂无观测）"


def refresh_context() -> dict:
    """Run the context builder agent and apply its updates. Returns a summary."""
    from ..llm import run_agent

    facts = repo.list_facts(status="active")
    facts_md = "\n".join(f"- [{f['kind']}] {f['key']}{' (pinned)' if f['pinned'] else ''}: {f['value']}" for f in facts) or "（无）"
    tasks = repo.list_tasks(status=["running", "review", "waiting_user", "waiting_approval", "done", "ready", "triaged"], limit=40)
    tasks_md = "\n".join(f"- [{t['status']}] {t['title']}" + (f"：{(t.get('summary') or '')[:120]}" if t.get("summary") else "")
                         for t in tasks) or "（无）"
    prompt = prompts.CONTEXT_REFRESH.format(facts=facts_md, observations=_observations(), tasks=tasks_md)
    run_id, res = run_agent(prompt, purpose="context", route="pm", output_schema=CONTEXT_SCHEMA, sandbox="read-only",
                            allow_network=False, timeout_s=900)
    if res.status != "succeeded" or not res.structured:
        repo.log_event("context", None, "refresh_failed", {"run_id": run_id, "error": res.error})
        return {"ok": False, "run_id": run_id, "error": res.error}
    data = res.structured
    upserts = stale = 0
    for f in data.get("facts", []):
        if f.get("op") == "stale":
            existing = [x for x in facts if x["kind"] == f["kind"] and x["key"] == f["key"]]
            if existing and not existing[0]["pinned"]:
                repo.upsert_fact(f["kind"], f["key"], existing[0]["value"], source="context-agent",
                                 confidence=float(f.get("confidence", 0.5)), status="stale", actor="agent:context")
                stale += 1
            continue
        if repo.upsert_fact(f["kind"], f["key"][:80], f["value"][:600], source=f"context-agent: {f.get('evidence', '')[:80]}",
                            confidence=float(f.get("confidence", 0.6)), actor="agent:context"):
            upserts += 1
    if data.get("focus_summary"):
        repo.kv_set("context.focus", data["focus_summary"][:2500])
    created = 0
    for opp in data.get("opportunities", [])[:5]:
        tid = repo.create_task(opp["title"], description=f"{opp.get('why', '')}\n\n（由个人上下文分析主动发现的机会）",
                               kind="opportunity", category=opp.get("category", "other"), level=int(opp.get("level", 2)),
                               status="triaged", source="context", priority=2,
                               dedupe_key=repo.fingerprint("opportunity", opp["title"]))
        created += 1 if tid else 0
    repo.kv_set("context.last_refresh", utcnow())
    render_profile()
    repo.log_event("context", None, "refreshed", {"run_id": run_id, "upserts": upserts, "stale": stale, "opportunities": created})
    return {"ok": True, "run_id": run_id, "upserts": upserts, "stale": stale, "opportunities": created}


def apply_updates(updates: list[dict], source: str) -> int:
    n = 0
    for u in updates or []:
        if u.get("kind") and u.get("key") and u.get("value"):
            if repo.upsert_fact(u["kind"], u["key"][:80], u["value"][:600], source=source, confidence=0.6, actor="agent"):
                n += 1
    return n
