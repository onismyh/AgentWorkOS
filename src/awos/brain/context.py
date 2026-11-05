"""Personal context: brief for prompts, periodic refresh from sensors + activity, profile.md rendering."""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

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


def _latest_progress(facts: list[dict]) -> dict[str, str]:
    """project name -> its newest progress note (`进展·…`, value starts with （project name）)."""
    out: dict[str, tuple[str, str]] = {}
    for f in facts:
        if not f["key"].startswith("进展·"):
            continue
        m = re.match(r"^（(.+?)）(.*)$", f["value"], re.S)
        if m and (m.group(1) not in out or f["updated_at"] > out[m.group(1)][0]):
            out[m.group(1)] = (f["updated_at"], m.group(2).strip())
    return {k: v[1] for k, v in out.items()}


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
    limits = {"project": 12, "person": 5, "note": 5, "interest": 6}
    progress = _latest_progress(by_kind.get("note", []))
    by_kind["note"] = [f for f in by_kind.get("note", []) if not f["key"].startswith("进展·")]   # shown under their project
    feedback = [f for f in by_kind.get("note", []) if f["key"].startswith("反馈·")]
    if feedback:
        by_kind["note"] = [f for f in by_kind["note"] if not f["key"].startswith("反馈·")]
        lines.append("### 用户对任务的反馈（强信号：用户说不用管/已处理的主题，不要再建同类任务）")
        for f in sorted(feedback, key=lambda x: x["updated_at"], reverse=True)[:20]:
            lines.append(f"- {f['value']}")
    for kind in KIND_ORDER:
        items = by_kind.get(kind) or []
        if not items:
            continue
        lines.append(f"### {KIND_TITLES.get(kind, kind)}")
        for f in items[: limits.get(kind, 8)]:
            lines.append(f"- **{f['key']}**：{f['value']}")
            if kind == "project" and progress.get(f["key"]):
                lines.append(f"  - 最新进展：{progress[f['key']][:140]}")
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
    progress = _latest_progress(by_kind.get("note", []))
    by_kind["note"] = [f for f in by_kind.get("note", []) if not f["key"].startswith(("进展·", "反馈·"))]
    for kind in KIND_ORDER:
        if by_kind.get(kind):
            md.append(f"## {KIND_TITLES.get(kind, kind)}")
            for f in by_kind[kind]:
                pin = " 📌" if f["pinned"] else ""
                conf = f"（置信度 {f['confidence']:.1f}）" if f["confidence"] < 1 else ""
                md.append(f"- **{f['key']}**{pin}：{f['value']}{conf}")
                if kind == "project" and progress.get(f["key"]):
                    md.append(f"  - 最新进展：{progress[f['key']]}")
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
    since = (datetime.now(timezone.utc) - timedelta(days=4)).strftime("%Y-%m-%dT%H:%M:%SZ")
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
        if agent_write_fact(f["kind"], f["key"], f["value"], source=f"context-agent: {f.get('evidence', '')[:80]}",
                            confidence=float(f.get("confidence", 0.6)), actor="agent:context", may_define_projects=True):
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


def agent_write_fact(kind: str, key: str, value: str, *, source: str, confidence: float, actor: str, may_define_projects: bool) -> str | None:
    """Write an agent-provided fact. Projects are stable things the user owns: a task agent may only report PROGRESS on a
    project (kept as one rolling `进展·<project>` note), it cannot create or re-summarise projects; the daily context
    agent may maintain project summaries but a dated / kebab-case key is still just a progress note."""
    from . import projects
    if kind == "project":
        existing = projects.match_project(key)
        if existing and may_define_projects:
            return repo.upsert_fact("project", existing["key"], value[:400], source=source, confidence=confidence, actor=actor)
        if not existing and may_define_projects and not projects.looks_like_progress_note(key):
            return repo.upsert_fact("project", key[:60], value[:400], source=source, confidence=confidence, actor=actor)
        name = existing["key"] if existing else key
        return repo.upsert_fact("note", (projects.PROGRESS_PREFIX + name)[:80], (f"（{name}）{value}" if existing else value)[:600],
                                source=source, confidence=confidence, actor=actor)
    return repo.upsert_fact(kind, key[:80], value[:600], source=source, confidence=confidence, actor=actor)


def apply_updates(updates: list[dict], source: str) -> int:
    n = 0
    for u in updates or []:
        if u.get("kind") and u.get("key") and u.get("value"):
            if agent_write_fact(u["kind"], u["key"], u["value"], source=source, confidence=0.6, actor="agent", may_define_projects=False):
                n += 1
    return n
