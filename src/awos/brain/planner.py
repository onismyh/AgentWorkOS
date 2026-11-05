"""Planner: decompose complex tasks into a dependency DAG of sub-tasks that run in parallel."""
from __future__ import annotations

import logging

from .. import repo
from ..models import TaskStatus
from . import prompts
from .context import context_brief
from .schemas import PLAN_SCHEMA

log = logging.getLogger("awos.planner")


def notes_md(task: dict) -> str:
    meta = task.get("meta") or {}
    lines = [f"- [{n.get('by')}] {n.get('text')}" for n in meta.get("notes", [])]
    lines += [f"- [决策] {d.get('question')} → {d.get('choice')} {d.get('note') or ''}" for d in meta.get("decisions", [])]
    lines += [f"- [更新] {u.get('note')}" for u in meta.get("updates", [])[-5:]]
    return "\n".join(lines) or "（无）"


def has_cycle(subtasks: list[dict]) -> bool:
    graph = {s["key"]: [d for d in s.get("depends_on", []) if d != s["key"]] for s in subtasks}
    state: dict[str, int] = {}

    def visit(k: str) -> bool:
        if state.get(k) == 1:
            return True
        if state.get(k) == 2 or k not in graph:
            return False
        state[k] = 1
        if any(visit(d) for d in graph[k]):
            return True
        state[k] = 2
        return False

    return any(visit(k) for k in graph)


def create_subtasks(parent: dict, subtasks: list[dict], run_id: str | None = None) -> list[str]:
    """Materialize a sub-task DAG under parent; returns child ids. Parent becomes BLOCKED until children finish."""
    subtasks = [s for s in subtasks if s.get("key") and s.get("title")][:8]
    if not subtasks:
        return []
    if has_cycle(subtasks):
        for s in subtasks:
            s["depends_on"] = []
    key_to_id: dict[str, str] = {}
    for s in subtasks:
        key_to_id[s["key"]] = repo.create_task(
            s["title"], description=s.get("description"), kind="todo", category=s.get("category") or parent["category"],
            priority=parent["priority"], level=min(int(s.get("level", parent["level"])), int(parent["level"])),
            status=TaskStatus.READY, parent_id=parent["id"], executor=s.get("executor_hint"), project=parent.get("project"),
            source="subtask", acceptance=s.get("acceptance"), meta={"plan_key": s["key"], "plan_run": run_id},
            dedupe_key=repo.fingerprint("subtask", parent["id"], s["key"], (parent.get("meta") or {}).get("plan_round", 0)))
    for s in subtasks:
        for dep in s.get("depends_on", []):
            if dep in key_to_id and dep != s["key"]:
                repo.add_dependency(key_to_id[s["key"]], key_to_id[dep])
    return list(key_to_id.values())


def plan_task(task: dict, attempt: int = 1) -> dict:
    """Ask the planner whether to decompose. On decompose: create children and block the parent."""
    from ..llm import run_agent

    prompt = prompts.PLAN.format(context=context_brief(2000), title=task["title"], category=task["category"],
                                 level=task["level"], priority=task["priority"], description=task.get("description") or "",
                                 acceptance=task.get("acceptance") or "（未给出）", notes=notes_md(task))
    run_id, res = run_agent(prompt, purpose="plan", route="plan", task_id=task["id"], attempt=attempt,
                            output_schema=PLAN_SCHEMA, sandbox="read-only", allow_network=False, timeout_s=900)
    if res.status != "succeeded" or not res.structured:
        return {"ok": False, "run_id": run_id, "error": res.error, "error_class": res.error_class}
    plan = res.structured
    meta = task.get("meta") or {}
    repo.update_task(task["id"], plan=plan)
    repo.merge_task_meta(task["id"], planned=True, plan_round=int(meta.get("plan_round", 0)) + 1)
    if not plan.get("decompose") or len(plan.get("subtasks") or []) < 2:
        return {"ok": True, "run_id": run_id, "decomposed": False}
    ids = create_subtasks(repo.get_task(task["id"]), plan["subtasks"], run_id)
    return {"ok": True, "run_id": run_id, "decomposed": True, "children": ids}
