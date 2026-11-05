"""Worker: executes one claimed task end-to-end (plan -> execute -> verify -> deliver -> HITL handoff)."""
from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .. import db, repo
from ..brain import planner, policy, prompts
from ..brain.context import apply_updates, context_brief
from ..brain.schemas import EXECUTE_SCHEMA, VERIFY_SCHEMA
from ..config import load_config
from ..models import LEVELS, TaskStatus

log = logging.getLogger("awos.worker")

READONLY_ROUTE_CATEGORIES = {"dev", "research", "learning", "data", "pm"}
SETTLED = {"done", "review", "waiting_user", "failed", "cancelled", "dismissed"}


class LeaseKeeper:
    """Renews the task lease while the worker runs; stops when the task leaves RUNNING or ownership is lost."""

    def __init__(self, task_id: str, owner: str, lease_s: int) -> None:
        self.task_id, self.owner, self.lease_s = task_id, owner, lease_s
        self._stop = threading.Event()
        self._t = threading.Thread(target=self._loop, daemon=True, name=f"lease-{task_id}")

    def _loop(self) -> None:
        while not self._stop.wait(max(10, self.lease_s // 4)):
            try:
                if not repo.renew_lease(self.task_id, self.owner, self.lease_s):
                    return
            except Exception:  # pragma: no cover
                log.exception("lease renew failed")

    def __enter__(self) -> "LeaseKeeper":
        self._t.start()
        return self

    def __exit__(self, *exc) -> None:
        self._stop.set()


def _still_mine(task_id: str, owner: str) -> bool:
    t = repo.get_task(task_id)
    return bool(t and t["status"] == TaskStatus.RUNNING and t.get("lease_owner") == owner)


def _move(task_id: str, owner: str, status: str, reason: str, **extra) -> bool:
    """Fenced transition: succeeds only while we still own the running task (the human may have cancelled)."""
    try:
        repo.set_task_status(task_id, status, reason, extra=extra or None, expect_owner=owner)
        return True
    except repo.LeaseLost as e:
        log.info("fenced out: %s (wanted -> %s)", e, status)
        return False


def _notify(title: str, body: str, *, level: str = "action", ref_type: str = "task", ref_id: str | None = None,
            path: str = "") -> None:
    try:
        from ..hitl.batcher import enqueue  # one combined reminder per topic window, not one toast per event
        enqueue(title, body, level=level, ref_type=ref_type, ref_id=ref_id)
    except Exception as e:  # notifications are best effort
        log.warning("notify failed: %s", e)


def _backoff(attempt: int) -> int:
    seq = load_config().get("scheduler.retry_backoff_seconds", [60, 300, 1800])
    return int(seq[min(max(attempt - 1, 0), len(seq) - 1)])


def fail_or_retry(task: dict, owner: str, error: str | None, error_class: str | None, run_id: str | None = None) -> None:
    task = repo.get_task(task["id"]) or task
    err = (error or "unknown error")[:1500]
    repo.merge_task_meta(task["id"], last_error=err, last_error_class=error_class, last_run=run_id)
    if error_class == "cancelled":
        _move(task["id"], owner, TaskStatus.WAITING_USER, "运行被取消，等待人工处理")
        return
    if error_class == "capacity":
        # Only other runs' in-flight reservations were in the way: queue again WITHOUT using up a retry attempt.
        # After ~1 hour of that (30 x 2 min) the budget is genuinely tight, so ask the human instead.
        waits = int((task.get("meta") or {}).get("capacity_waits", 0)) + 1
        if waits <= 30:
            repo.merge_task_meta(task["id"], capacity_waits=waits)
            db.execute("UPDATE tasks SET attempts = MAX(0, attempts - 1) WHERE id = ?", (task["id"],))
            nxt = (datetime.now(timezone.utc) + timedelta(seconds=120)).strftime("%Y-%m-%dT%H:%M:%SZ")
            _move(task["id"], owner, TaskStatus.READY, f"等待预算额度释放（第 {waits} 次排队，不计入重试）", next_attempt_at=nxt)
            return
        error_class = "budget"
    if error_class == "budget":
        if not repo.list_approvals(status="pending", task_id=task["id"]):
            from ..hitl.outbox import create_budget_approval
            create_budget_approval(task, err)
        _move(task["id"], owner, TaskStatus.WAITING_APPROVAL, "预算不足，等待批准")
        return
    if error_class in ("permanent", "policy") or task["attempts"] >= task["max_attempts"]:
        if _move(task["id"], owner, TaskStatus.FAILED, f"{error_class or 'error'}: {err[:300]}"):
            _notify(f"任务失败：{task['title'][:40]}", f"{err[:200]}\n可在控制台重试或接手。", level="action",
                    ref_id=task["id"], path=f"task/{task['id']}")
        return
    delay = _backoff(task["attempts"])
    nxt = (datetime.now(timezone.utc) + timedelta(seconds=delay)).strftime("%Y-%m-%dT%H:%M:%SZ")
    _move(task["id"], owner, TaskStatus.READY, f"第 {task['attempts']} 次尝试失败（{error_class}），{delay}s 后重试",
          next_attempt_at=nxt)


def _related_results(task: dict) -> tuple[str, list[Path]]:
    """Results of children (synthesis) and dependencies, plus their workspaces for read access."""
    parts: list[str] = []
    dirs: list[Path] = []
    kids = repo.children(task["id"])
    deps = [repo.get_task(d["id"]) for d in repo.dependencies(task["id"])]
    for title, group in (("子任务成果（请汇总）", kids), ("前置任务成果", deps)):
        if not group:
            continue
        parts.append(f"## {title}")
        for k in group:
            if not k:
                continue
            arts = repo.list_artifacts(task_id=k["id"])
            parts.append(f"### [{k['status']}] {k['title']}\n{(k.get('summary') or '（无总结）')[:2500]}")
            if arts:
                parts.append("交付物：\n" + "\n".join(f"- {a['title']}: `{a['path']}`" for a in arts if a.get("path")))
            if k.get("workspace"):
                dirs.append(Path(k["workspace"]))
    return "\n".join(parts), dirs


def _extra_context(task: dict) -> tuple[str, list[Path]]:
    meta = task.get("meta") or {}
    parts = []
    notes = planner.notes_md(task)
    if notes != "（无）":
        parts.append(f"## 用户备注 / 决策记录（优先遵循）\n{notes}")
    if meta.get("last_feedback"):
        parts.append(f"## 上一轮的质检/返工意见（必须解决）\n{meta['last_feedback']}")
    if meta.get("last_error") and task["attempts"] > 1:
        parts.append(f"## 上一次尝试失败的原因\n{meta['last_error'][:800]}")
    if meta.get("action_results"):
        parts.append("## 已经批准并执行的动作\n" + "\n".join(f"- {a.get('title')}: {a.get('result')}" for a in meta["action_results"][-10:]))
    if task.get("summary") and task["attempts"] > 1:
        parts.append(f"## 之前的进展\n{task['summary'][:2000]}")
    rel, dirs = _related_results(task)
    if rel:
        parts.append(rel)
    return "\n\n".join(parts), dirs


def _register_deliverables(task: dict, run_id: str, workspace: Path, out: dict) -> list[str]:
    ids = []
    ws = workspace.resolve()
    for d in out.get("deliverables") or []:
        p = (ws / d.get("path", "")).resolve()
        if ws not in p.parents and p != ws:
            log.warning("deliverable outside workspace ignored: %s", p)
            continue
        if p.exists():
            ids.append(repo.add_artifact(d.get("title") or p.name, kind=d.get("kind", "file"), path=p, task_id=task["id"], run_id=run_id))
    summary_path = ws / f"_summary_{run_id}.md"
    summary_path.write_text(f"# {task['title']}\n\n{out.get('summary', '')}\n", encoding="utf-8")
    ids.append(repo.add_artifact(f"执行总结 · {task['title'][:40]}", kind="report", path=summary_path, task_id=task["id"], run_id=run_id))
    return ids


def _verify(task: dict, workspace: Path, out: dict, executed_by: str) -> dict | None:
    from ..llm import pick_executor, run_agent

    ex = None
    sensitive = (workspace / "inputs" / "mail").exists()  # mail-derived deliverables never go to the DeepSeek verifiers
    for cand in ("dsh", "pi", "codex", "claude"):  # cheapest independent opinion first, never the executor itself
        if cand == executed_by or (sensitive and cand in ("dsh", "pi")):
            continue
        try:
            if pick_executor("verify", preferred=cand) == cand:
                ex = cand
                break
        except RuntimeError:
            break
    if ex is None:
        return None
    deliv = "\n".join(f"- `{d.get('path')}`（{d.get('title')}）" for d in out.get("deliverables") or []) or "（无文件交付物）"
    prompt = prompts.VERIFY.format(title=task["title"], acceptance=task.get("acceptance") or "（未给出，按常识判断质量）",
                                   summary=out.get("summary", "")[:4000], workspace=str(workspace), deliverables=deliv)
    _, res = run_agent(prompt, purpose="verify", executor=ex, task_id=task["id"], attempt=task["attempts"], cwd=workspace,
                       sandbox="read-only", output_schema=VERIFY_SCHEMA, allow_network=False, timeout_s=900)
    return res.structured if res.status == "succeeded" else None


def process_task(task_id: str, owner: str) -> None:
    lease_s = int(load_config().get("scheduler.lease_seconds", 300))
    task = repo.get_task(task_id)
    if not task:
        return
    with LeaseKeeper(task_id, owner, lease_s):
        try:
            _process(task, owner)
        except Exception as e:
            log.exception("worker crashed on %s", task_id)
            fail_or_retry(task, owner, f"worker exception: {e!r}", "transient")


def _process(task: dict, owner: str) -> None:
    from ..hitl import outbox
    from ..llm import pick_executor, run_agent

    meta = task.get("meta") or {}
    dec = policy.decide(task)
    if dec.level != task["level"]:
        repo.update_task(task["id"], level_cap=dec.cap)
    kids = repo.children(task["id"])

    # 1) Plan complex root tasks once.
    if meta.get("complexity") == "complex" and not meta.get("planned") and not kids and not task.get("parent_id"):
        r = planner.plan_task(task, attempt=task["attempts"])
        if not r.get("ok"):
            return fail_or_retry(task, owner, r.get("error"), r.get("error_class") or "transient", r.get("run_id"))
        if r.get("decomposed"):
            _move(task["id"], owner, TaskStatus.BLOCKED, f"已拆分为 {len(r['children'])} 个并行子任务")
            return
        task = repo.get_task(task["id"])
    if kids and any(k["status"] not in SETTLED for k in kids):
        _move(task["id"], owner, TaskStatus.BLOCKED, "等待子任务完成")
        return

    # 2) Execute.
    workspace = load_config().path("workspaces") / task["id"]
    workspace.mkdir(parents=True, exist_ok=True)
    if task.get("workspace") != str(workspace):
        repo.update_task(task["id"], workspace=str(workspace))
    extra, extra_dirs = _extra_context(task)
    n_mail = 0
    try:
        from .. import mailstore
        mail_block, n_mail = mailstore.export_for_task(task, workspace)
        if mail_block:
            extra = "\n\n".join(x for x in (extra, mail_block) if x)
    except Exception:
        log.exception("mail export failed for %s", task["id"])
    readable = ", ".join(f"`{p}`" for p in dec.readable) or "（无）"
    prompt = prompts.EXECUTE.format(
        context=context_brief(), task_id=task["id"], title=task["title"], category=task["category"], kind=task["kind"],
        priority=task["priority"], level=dec.level, level_name=LEVELS[dec.level]["name"],
        level_deliverable=LEVELS[dec.level]["deliverable"], acceptance=task.get("acceptance") or "（未给出，请自行定义合理标准并写进 summary）",
        description=task.get("description") or task["title"], extra=extra, workspace=str(workspace), readable=readable,
        level_instructions=prompts.LEVEL_INSTRUCTIONS[dec.level], security=prompts.SECURITY_RULES)
    preferred = task.get("executor") if task.get("executor") in ("codex", "claude", "pi", "dsh") else None
    if n_mail and preferred in ("pi", "dsh"):
        preferred = None  # mail bodies only go to the executors the user already trusts with mail (codex/claude)
    route = task["category"]
    if preferred is None and not n_mail and dec.sandbox == "read-only" and task["category"] in READONLY_ROUTE_CATEGORIES:
        route = "readonly"  # analysis-only work on non-personal topics goes to the cheap DeepSeek executors first
    try:
        executor = pick_executor(route, preferred=preferred)
    except RuntimeError as e:
        return fail_or_retry(task, owner, str(e), "transient")
    remaining = (float(load_config().get("budget.per_task_usd", 3.0)) + float(meta.get("budget_bonus", 0))
                 - float(task.get("cost_usd") or 0))
    if executor != "codex" and remaining <= 0.05:
        return fail_or_retry(task, owner, f"per-task budget exhausted (${task.get('cost_usd'):.2f})", "budget")
    run_id, res = run_agent(prompt, purpose="execute", executor=executor, task_id=task["id"], attempt=task["attempts"],
                            cwd=workspace, sandbox=dec.sandbox, output_schema=EXECUTE_SCHEMA,
                            add_dirs=dec.readable + extra_dirs, allow_network=True, allow_connectors=False,
                            max_budget_usd=max(0.1, min(remaining, float(load_config().get("executors.claude.max_budget_usd_per_run", 1.5)))))
    if res.status != "succeeded" or not res.structured:
        return fail_or_retry(task, owner, res.error or "no structured result", res.error_class or "transient", run_id)
    out = res.structured
    if not _still_mine(task["id"], owner):
        return

    # 3) Deliverables, context, summary.
    _register_deliverables(task, run_id, workspace, out)
    apply_updates(out.get("context_updates") or [], source=f"task:{task['id']}")
    repo.update_task(task["id"], summary=out.get("summary", "")[:8000], result=out)
    repo.merge_task_meta(task["id"], last_run=run_id, last_executor=executor, last_feedback=None, last_error=None,
                         followups=(out.get("followups") or [])[:10])

    # 4) Dynamic decomposition requested by the executor.
    if out.get("subtasks") and out.get("status") == "partial" and not kids:
        ids = planner.create_subtasks(repo.get_task(task["id"]), out["subtasks"], run_id)
        if ids:
            _move(task["id"], owner, TaskStatus.BLOCKED, f"执行中拆分为 {len(ids)} 个子任务")
            return

    if out.get("status") in ("blocked", "failed"):
        if _move(task["id"], owner, TaskStatus.WAITING_USER, f"Agent 报告 {out['status']}：需要你接手"):
            _notify(f"需要你接手：{task['title'][:40]}", out.get("summary", "")[:300], ref_id=task["id"], path=f"task/{task['id']}")
        return

    # 5) Independent verification for high-autonomy work.
    is_sub = bool(task.get("parent_id"))
    if dec.level in policy.verify_levels() and not is_sub:
        v = _verify(repo.get_task(task["id"]), workspace, out, executor)
        if v is not None:
            repo.merge_task_meta(task["id"], verification=v)
            if not v.get("passed") or int(v.get("score", 0)) < policy.min_verify_score():
                t = repo.get_task(task["id"])
                if t["attempts"] < t["max_attempts"]:
                    repo.merge_task_meta(task["id"], last_feedback=v.get("feedback") or "; ".join(v.get("issues", [])))
                    _move(task["id"], owner, TaskStatus.READY, f"质检未通过（{v.get('score')}/10），自动返工")
                    return
                out.setdefault("verification", {})["passed"] = False

    # 6) HITL: decisions + proposed actions -> Outbox.
    approvals = outbox.create_from_result(repo.get_task(task["id"]), run_id, out)

    # 7) Handoff by level.
    if is_sub:
        target, why = TaskStatus.DONE, "子任务完成"
    elif dec.level == 1:
        target, why = (TaskStatus.DONE, "信息已整理") if task["kind"] == "info" else (TaskStatus.WAITING_USER, "L1：背景已整理，等待你接手")
    elif dec.level == 2:
        target, why = TaskStatus.WAITING_USER, "L2：分析与方案已给出"
    elif dec.level == 5 and (out.get("verification") or {}).get("passed"):
        target, why = TaskStatus.DONE, "L5：已端到端完成并通过验证"
    else:
        target, why = TaskStatus.REVIEW, f"L{dec.level}：交付物已就绪，等待验收"

    if approvals:
        repo.merge_task_meta(task["id"], after_approval={"status": str(target), "reason": why})
        if _move(task["id"], owner, TaskStatus.WAITING_APPROVAL, f"等待 {len(approvals)} 项审批"):
            outbox.notify_pending(approvals)
        return
    if _move(task["id"], owner, target, why):
        on_task_finished(task["id"])
        if target in (TaskStatus.REVIEW, TaskStatus.WAITING_USER) and not is_sub:
            _notify(f"{'待验收' if target == TaskStatus.REVIEW else '待接手'}：{task['title'][:40]}",
                    (out.get("summary") or "")[:300], ref_id=task["id"], path=f"task/{task['id']}")


def on_task_finished(task_id: str) -> None:
    """Unblock the parent when all its children are settled."""
    t = repo.get_task(task_id)
    if not t or not t.get("parent_id"):
        return
    parent = repo.get_task(t["parent_id"])
    if not parent or parent["status"] != TaskStatus.BLOCKED:
        return
    kids = repo.children(parent["id"])
    if kids and all(k["status"] in SETTLED for k in kids):
        repo.merge_task_meta(parent["id"], synthesis=True)
        repo.update_task(parent["id"], max_attempts=max(parent["max_attempts"], parent["attempts"] + 2))
        repo.set_task_status(parent["id"], TaskStatus.READY, "子任务全部完成，进入汇总")


__all__ = ["process_task", "on_task_finished", "fail_or_retry"]
