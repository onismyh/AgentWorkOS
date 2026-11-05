"""Triage: signals (and un-triaged manual tasks) -> classified tasks with autonomy level + autostart decision."""
from __future__ import annotations

import logging

from .. import repo
from ..models import TaskStatus
from . import policy, prompts
from .context import context_brief
from .schemas import TRIAGE_SCHEMA

log = logging.getLogger("awos.triage")
MAX_SIGNAL_ATTEMPTS = 3


def _open_tasks_md(limit: int = 60) -> str:
    rows = repo.list_tasks(status=["inbox", "triaged", "ready", "running", "blocked", "waiting_approval",
                                   "waiting_user", "review", "snoozed"], root_only=True, limit=limit)
    return "\n".join(f"- {t['id']} [{t['status']}] {t['title']}" for t in rows) or "（无）"


def _fmt_signal(s: dict) -> str:
    body = (s.get("body") or "").strip()
    return (f"### id={s['id']}  source={s['source']}  time={s.get('occurred_at') or s['observed_at']}\n"
            f"title: {s['title']}\n" + (f"url: {s['url']}\n" if s.get("url") else "") + (body[:1500] if body else ""))


def _fmt_task(t: dict) -> str:
    return (f"### id={t['id']}  source=manual-task  time={t['created_at']}\n"
            f"title: {t['title']}\n{(t.get('description') or '')[:2000]}")


def _route_status(task: dict, autostart: bool) -> tuple[str, str]:
    ok, why = policy.should_autostart(task, autostart)
    return (TaskStatus.READY, why) if ok else (TaskStatus.TRIAGED, why)


def triage_once(limit: int = 25) -> dict:
    """Classify a batch of pending signals and inbox tasks. Returns stats."""
    from ..llm import run_agent

    signals = repo.pending_signals(limit)
    inbox_tasks = [t for t in repo.list_tasks(status=TaskStatus.INBOX, limit=limit)]
    # Fully specified manual tasks skip the LLM.
    llm_tasks = []
    for t in inbox_tasks:
        meta = t.get("meta") or {}
        if meta.get("level_locked") and meta.get("category_locked"):
            status, why = _route_status(t, True)
            repo.set_task_status(t["id"], status, why)
        else:
            llm_tasks.append(t)
    if not signals and not llm_tasks:
        return {"signals": 0, "tasks": 0}

    inputs = "\n\n".join([_fmt_signal(s) for s in signals] + [_fmt_task(t) for t in llm_tasks])
    prompt = prompts.TRIAGE.format(context=context_brief(2500), open_tasks=_open_tasks_md(), levels=prompts.levels_table(),
                                   categories=prompts.categories_list(), kinds=prompts.kinds_list(), inputs=inputs)
    run_id, res = run_agent(prompt, purpose="triage", route="triage", output_schema=TRIAGE_SCHEMA, sandbox="read-only",
                            allow_network=False, timeout_s=600)
    if res.status != "succeeded" or not res.structured:
        attempts = repo.kv_get("triage.failures", {}) or {}
        for s in signals:
            attempts[s["id"]] = attempts.get(s["id"], 0) + 1
            if attempts[s["id"]] >= MAX_SIGNAL_ATTEMPTS:
                repo.mark_signal(s["id"], "error", note=f"triage failed: {res.error}")
                attempts.pop(s["id"], None)
        repo.kv_set("triage.failures", attempts)
        log.warning("triage run %s failed: %s", run_id, res.error)
        return {"signals": len(signals), "tasks": len(llm_tasks), "ok": False, "run_id": run_id, "error": res.error}

    sig_ids = {s["id"]: s for s in signals}
    task_ids = {t["id"]: t for t in llm_tasks}
    covered: set[str] = set()
    stats = {"created": 0, "updated": 0, "ignored": 0, "classified": 0, "ready": 0}
    for item in res.structured.get("items", []):
        refs = [r for r in item.get("ref_ids", []) if (r in sig_ids or r in task_ids) and r not in covered]
        if not refs:
            continue
        covered.update(refs)
        action = item.get("action")
        reason = (item.get("reason") or "")[:500]
        ref_sigs = [r for r in refs if r in sig_ids]
        ref_tasks = [r for r in refs if r in task_ids]
        if action in ("ignore", "context_only"):
            for r in ref_sigs:
                repo.mark_signal(r, "ignored", note=f"{action}: {reason}")
            for r in ref_tasks:  # the human asked for it explicitly: never silently drop -> keep as suggestion
                repo.update_task(r, level_reason=f"triage 建议忽略：{reason}")
                repo.set_task_status(r, TaskStatus.TRIAGED, f"triage suggested {action}")
            stats["ignored"] += 1
            continue
        if action == "update_existing" and item.get("existing_task_id") and repo.get_task(item["existing_task_id"]):
            tid = item["existing_task_id"]
            t = repo.get_task(tid)
            updates = list((t.get("meta") or {}).get("updates", []))
            updates.append({"refs": refs, "note": item.get("description") or reason})
            repo.merge_task_meta(tid, updates=updates[-20:])
            for r in ref_sigs:
                repo.mark_signal(r, "triaged", task_id=tid, note="merged into existing task")
            for r in ref_tasks:
                repo.set_task_status(r, TaskStatus.DISMISSED, f"duplicate of {tid}")
            stats["updated"] += 1
            continue
        fields = dict(title=item["title"][:200], kind=item.get("kind", "todo"), category=item.get("category", "other"),
                      priority=int(item.get("priority", 2)), level=int(item.get("level", 1)),
                      level_reason=item.get("level_reason"), project=item.get("project"), due_at=item.get("due_at"),
                      acceptance=item.get("acceptance"), executor=item.get("executor_hint"))
        complexity = item.get("complexity", "simple")
        autostart = bool(item.get("autostart"))
        if ref_tasks:  # classify the existing manual task in place
            tid = ref_tasks[0]
            t = task_ids[tid]
            meta = t.get("meta") or {}
            upd = {k: v for k, v in fields.items() if v is not None and k != "title"}
            if meta.get("level_locked"):
                upd.pop("level", None)
            if meta.get("category_locked"):
                upd.pop("category", None)
            if not t.get("description"):
                upd["description"] = item.get("description")
            repo.update_task(tid, **upd)
            repo.merge_task_meta(tid, complexity=complexity, triage_run=run_id, triage_reason=reason)
            autostart = True  # explicitly requested by the human
            stats["classified"] += 1
        else:
            desc = item.get("description") or ""
            src_lines = "\n".join(f"- [{sig_ids[r]['source']}] {sig_ids[r]['title']}" + (f" ({sig_ids[r]['url']})" if sig_ids[r].get("url") else "")
                                  for r in ref_sigs)
            tid = repo.create_task(fields.pop("title"), description=f"{desc}\n\n**来源**：\n{src_lines}", status=TaskStatus.INBOX,
                                   source=f"sensor:{sig_ids[ref_sigs[0]]['source']}", source_signal_id=ref_sigs[0],
                                   meta={"complexity": complexity, "triage_run": run_id, "triage_reason": reason,
                                         "signals": ref_sigs}, **fields)
            stats["created"] += 1
        for r in ref_sigs:
            repo.mark_signal(r, "triaged", task_id=tid, note=reason)
        task = repo.get_task(tid)
        status, why = _route_status(task, autostart)
        repo.set_task_status(tid, status, why)
        if status == TaskStatus.READY:
            stats["ready"] += 1
    # Anything the model forgot stays 'new' for the next round, with an attempt counter.
    attempts = repo.kv_get("triage.failures", {}) or {}
    for s in signals:
        if s["id"] not in covered:
            attempts[s["id"]] = attempts.get(s["id"], 0) + 1
            if attempts[s["id"]] >= MAX_SIGNAL_ATTEMPTS:
                repo.mark_signal(s["id"], "ignored", note="not covered by triage after retries")
                attempts.pop(s["id"], None)
        else:
            attempts.pop(s["id"], None)
    for t in llm_tasks:
        if t["id"] not in covered:
            repo.set_task_status(t["id"], TaskStatus.TRIAGED, "triage did not classify; needs a human look")
    repo.kv_set("triage.failures", attempts)
    stats.update({"signals": len(signals), "tasks": len(llm_tasks), "ok": True, "run_id": run_id})
    repo.log_event("system", None, "triaged", stats)
    return stats
