"""HITL Outbox: turn agent results into approvals, react to human decisions, execute approved actions once."""
from __future__ import annotations

import hashlib
import json
import logging
import shutil
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import repo
from ..brain import policy
from ..config import load_config
from ..db import dumps, utcnow
from ..models import ApprovalStatus, TaskStatus
from ..timeutil import local_now
from . import gitops

log = logging.getLogger("awos.outbox")

# Approved actions are normally executed in a background thread right after the decision. Short-lived processes
# (the CLI) must set this to False: their threads die on exit, so the daemon's scheduler picks the action up instead.
EXECUTE_INLINE = True

ACTION_OPTIONS = [{"key": "approve", "label": "批准执行", "description": "按载荷原样执行（备注会作为补充指示）"},
                  {"key": "reject", "label": "拒绝", "description": "不执行，任务交还给你"}]
AGENT_ACTIONS = {"send_email", "notion_write", "calendar_write", "publish", "submit_form", "other", "post_message"}
ACTION_RESULT_SCHEMA = {
    "type": "object",
    "properties": {"done": {"type": "boolean"}, "details": {"type": "string"}, "evidence": {"type": "string"}},
    "required": ["done", "details", "evidence"],
}


def payload_hash(payload: Any) -> str:
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()


def _expiry(risk: str) -> str:
    return (datetime.now(timezone.utc) + timedelta(hours=policy.approval_expiry_hours(risk))).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_payload(raw: Any) -> dict:
    if isinstance(raw, dict):
        return raw
    try:
        v = json.loads(raw or "{}")
        return v if isinstance(v, dict) else {"value": v}
    except (TypeError, ValueError):
        return {"raw": str(raw)[:4000]}


def _task_context(task: dict) -> str:
    return (f"**任务**：{task['title']}（L{task['level']} · {task['category']} · P{task['priority']}）\n\n"
            f"**AI 进展**：\n{(task.get('summary') or '')[:1500]}")


def create_from_result(task: dict, run_id: str, out: dict) -> list[str]:
    ids: list[str] = []
    for d in out.get("decisions_needed") or []:
        opts = [{"key": o["key"], "label": o["label"], "description": o.get("consequence", "")} for o in d.get("options", []) if o.get("key")]
        if not opts:
            continue
        opts.append({"key": "defer", "label": "暂不决定", "description": "保持现状，任务交还给你"})
        ids.append(repo.create_approval(
            title=d["question"][:200], kind="decision", task_id=task["id"], run_id=run_id,
            context_md=f"{d.get('context', '')}\n\n---\n{_task_context(task)}", recommendation=d.get("recommendation"),
            options=opts, risk="medium", payload={"question": d["question"]}, expires_at=_expiry("medium")))
    for a in out.get("proposed_actions") or []:
        payload = _parse_payload(a.get("payload_json"))
        atype = a.get("type") or "other"
        risk = a.get("risk") or "medium"
        notes = []
        if atype == "apply_to_project":
            files = payload.get("files") or []
            bad = [f.get("dest") for f in files if not f.get("dest") or not policy.writable_destination(Path(f["dest"]))]
            if bad:
                risk = "high"
                notes.append(f"⚠️ 以下目标路径不在允许的工作目录内，将被拒绝执行：{bad}")
            elif files and all(policy.auto_apply_allowed(Path(f["dest"])) for f in files) and task["level"] >= 5:
                aid = repo.create_approval(title=a["title"], kind="action", action_type=atype, task_id=task["id"], run_id=run_id,
                                           context_md="（L5 + 白名单目录，自动执行）", options=ACTION_OPTIONS, risk="low",
                                           payload={**payload, "_hash": payload_hash(payload)})
                repo.decide_approval(aid, "approve", via="auto", actor="system")
                execute_action(repo.get_approval(aid))
                continue
        if atype in ("git_commit", "git_push"):
            ok, why = gitops.auto_allowed(atype, payload, task=task)
            if ok and ids:  # an earlier decision/action of this result still waits for the human: keep the order
                ok, why = False, "前面还有等你决定或批准的事项，这一步排在它们之后"
            if ok:  # whiteboard rule: commits/pushes of a non-protected branch need no sign-off (still recorded in the Outbox)
                aid = repo.create_approval(title=a["title"][:200], kind="action", action_type=atype, task_id=task["id"], run_id=run_id,
                                           context_md=("（白板规则：分支上的 commit / push 默认放行，已自动执行）\n\n```json\n"
                                                       f"{json.dumps(payload, ensure_ascii=False, indent=2)[:1500]}\n```"),
                                           options=ACTION_OPTIONS, risk="low", payload={**payload, "_hash": payload_hash(payload)})
                repo.decide_approval(aid, "approve", via="auto", actor="system")
                execute_action(repo.get_approval(aid))
                continue
            if why:
                risk = "high" if "受保护分支" in why else risk
                notes.append(f"⚠️ 不能自动执行，需要你批准：{why}")
        if not policy.needs_approval(atype):
            continue
        ctx = (f"{a.get('description', '')}\n\n" + ("\n".join(notes) + "\n\n" if notes else "") +
               f"**将执行的载荷**：\n```json\n{json.dumps(payload, ensure_ascii=False, indent=2)[:3000]}\n```\n\n---\n{_task_context(task)}")
        ids.append(repo.create_approval(
            title=a["title"][:200], kind="action", action_type=atype, task_id=task["id"], run_id=run_id, context_md=ctx,
            recommendation=(f"approve：按「批准后将执行的内容」原样执行，风险{'低' if risk == 'low' else '中'}；批准前请先核对其中的文件、收件人和说明。"
                            if risk != "high" else "请仔细核对后再批准：这是高风险动作，批准前务必逐项确认「批准后将执行的内容」。"),
            options=ACTION_OPTIONS, risk=risk,
            payload={**payload, "_hash": payload_hash(payload)}, expires_at=_expiry(risk)))
    return ids


def create_budget_approval(task: dict, error: str) -> str:
    extra = float(load_config().get("budget.per_task_usd", 3.0))
    return repo.create_approval(
        title=f"预算不足：{task['title'][:60]}", kind="budget", task_id=task["id"],
        context_md=f"{error}\n\n已花费 ${float(task.get('cost_usd') or 0):.2f}。批准后为该任务追加 ${extra:.2f} 预算并继续。",
        recommendation="approve" if task["priority"] <= 1 else "reject",
        options=[{"key": "approve", "label": f"追加 ${extra:.0f} 预算继续", "description": ""},
                 {"key": "reject", "label": "停止，交还给我", "description": ""}],
        risk="low", payload={"extra_usd": extra}, expires_at=_expiry("low"))


def notify_pending(approval_ids: list[str]) -> None:
    from .batcher import enqueue
    items = [repo.get_approval(a) for a in approval_ids]
    items = [i for i in items if i]
    if not items:
        return
    high = any(i["risk"] == "high" for i in items)
    title = f"待你确认（{len(items)}）：{items[0]['title'][:40]}"
    body = "\n".join(f"• {i['title'][:60]}（建议：{(i.get('recommendation') or '-')[:40]}）" for i in items[:5])
    try:
        enqueue(title, body, level="high" if high else "action", ref_type="approval", ref_id=items[0]["id"])
        for i in items:
            repo.db.update("approvals", "id", i["id"], {"notified_at": utcnow()})
    except Exception as e:
        log.warning("notify failed: %s", e)


# --------------------------------------------------------------------------- decisions


def on_decision(approval: dict) -> None:
    """Called right after repo.decide_approval(). Executes actions (in background) and resumes the task."""
    kind = approval["kind"]
    task_id = approval.get("task_id")
    if kind == "decision" and task_id:
        label = next((o["label"] for o in approval.get("options") or [] if o["key"] == approval["decision"]), approval["decision"])
        t = repo.get_task(task_id) or {}
        decisions = list((t.get("meta") or {}).get("decisions", []))
        decisions.append({"question": approval["title"], "choice": label, "key": approval["decision"],
                          "note": approval.get("decision_note"), "ts": utcnow()})
        repo.merge_task_meta(task_id, decisions=decisions)
        repo.set_approval_status(approval["id"], ApprovalStatus.EXECUTED, result={"recorded": label}, expect=ApprovalStatus.APPROVED)
        maybe_resume(task_id)
    elif kind == "budget" and task_id:
        if approval["status"] == ApprovalStatus.APPROVED:
            t = repo.get_task(task_id) or {}
            bonus = float((t.get("meta") or {}).get("budget_bonus", 0)) + float((approval.get("payload") or {}).get("extra_usd", 3))
            repo.merge_task_meta(task_id, budget_bonus=bonus)
            repo.set_approval_status(approval["id"], ApprovalStatus.EXECUTED, result={"budget_bonus": bonus}, expect=ApprovalStatus.APPROVED)
        maybe_resume(task_id)
    elif kind == "action":
        if approval["status"] == ApprovalStatus.APPROVED:
            if EXECUTE_INLINE:
                threading.Thread(target=execute_action, args=(approval,), daemon=True, name=f"action-{approval['id']}").start()
        else:
            _record_action_result(approval, {"rejected": True, "note": approval.get("decision_note")})
            maybe_resume(task_id)
    else:
        maybe_resume(task_id)


def maybe_resume(task_id: str | None) -> None:
    """When a task has no pending/executing approvals left, move it forward."""
    if not task_id:
        return
    t = repo.get_task(task_id)
    if not t or t["status"] != TaskStatus.WAITING_APPROVAL:
        return
    open_ = repo.list_approvals(status=["pending", "approved", "executing"], task_id=task_id)
    if open_:
        return
    meta = t.get("meta") or {}
    round_ = [a for a in repo.list_approvals(task_id=task_id, limit=50) if a.get("decided_at") and a["decided_at"] >= (t.get("finished_at") or t["updated_at"] or "")]
    if not round_:
        round_ = repo.list_approvals(task_id=task_id, limit=10)
    failed = [a for a in round_ if a["status"] == ApprovalStatus.FAILED]
    decisions = [a for a in round_ if a["kind"] == "decision" and a.get("decision") not in (None, "defer")]
    budget_ok = [a for a in round_ if a["kind"] == "budget" and a.get("decision") == "approve"]
    rejected_all = round_ and all(a["status"] in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED, ApprovalStatus.CANCELLED) for a in round_)
    if failed:
        repo.set_task_status(task_id, TaskStatus.WAITING_USER, f"{len(failed)} 个已批准动作执行失败，需要你处理")
    elif budget_ok or (decisions and t["level"] >= 3):
        repo.update_task(task_id, max_attempts=max(t["max_attempts"], t["attempts"] + 2))
        repo.set_task_status(task_id, TaskStatus.READY, "已获授权，继续推进")
    elif rejected_all:
        repo.set_task_status(task_id, TaskStatus.WAITING_USER, "审批被拒绝或过期，任务交还给你")
    else:
        after = meta.get("after_approval") or {"status": TaskStatus.REVIEW, "reason": "审批完成"}
        target = after["status"]
        if target == TaskStatus.DONE and any(a["status"] == ApprovalStatus.REJECTED for a in round_):
            target = TaskStatus.REVIEW
        repo.set_task_status(task_id, target, after.get("reason"), force=True)
        from ..engine.worker import on_task_finished
        on_task_finished(task_id)


def expire_approvals() -> int:
    n = 0
    for a in repo.list_approvals(status="pending", limit=500):
        if a.get("expires_at") and a["expires_at"] < utcnow():
            if a.get("default_on_expiry") and a["kind"] != "action":
                try:
                    repo.decide_approval(a["id"], a["default_on_expiry"], via="auto", actor="system", note="expired: default option")
                    on_decision(repo.get_approval(a["id"]))
                except ValueError:
                    pass
            elif repo.set_approval_status(a["id"], ApprovalStatus.EXPIRED, expect="pending"):
                maybe_resume(a.get("task_id"))
            n += 1
    return n


# --------------------------------------------------------------------------- action runner


def _record_action_result(approval: dict, result: dict) -> None:
    tid = approval.get("task_id")
    if not tid:
        return
    t = repo.get_task(tid) or {}
    results = list((t.get("meta") or {}).get("action_results", []))
    results.append({"approval_id": approval["id"], "title": approval["title"], "type": approval.get("action_type"),
                    "result": json.dumps(result, ensure_ascii=False)[:600], "ts": utcnow()})
    repo.merge_task_meta(tid, action_results=results[-30:])


def execute_action(approval: dict) -> dict:
    """Execute an APPROVED action exactly once (CAS approved -> executing -> executed/failed)."""
    if not repo.set_approval_status(approval["id"], ApprovalStatus.EXECUTING, expect=ApprovalStatus.APPROVED):
        return {"skipped": "not in approved state"}
    payload = dict(approval.get("payload") or {})
    expected = payload.pop("_hash", None)
    try:
        if expected and expected != payload_hash(payload):
            raise RuntimeError("payload hash mismatch: refusing to execute a modified action")
        atype = approval.get("action_type") or "other"
        if atype == "apply_to_project":
            result = _apply_to_project(approval, payload)
        elif atype in ("git_commit", "git_push", "open_mr"):
            if atype != "open_mr" and approval.get("decided_via") == "auto":
                task = repo.get_task(approval["task_id"]) if approval.get("task_id") else None
                ok, why = gitops.auto_allowed(atype, payload, task=task)   # the repo may have changed since the rule let it through
                if not ok:
                    raise RuntimeError(f"no longer allowed without approval: {why}")
            result = {"git_commit": gitops.commit, "git_push": gitops.push, "open_mr": gitops.open_mr}[atype](payload)
        elif atype == "delete":
            result = _soft_delete(payload)
        elif atype == "spend":
            result = {"done": False, "details": "涉及付款的动作需要你本人完成，系统不会代付。"}
        elif atype == "post_message" and payload.get("channel") in ("wecom", "feishu"):
            from .notify import post_webhook
            result = post_webhook(payload["channel"], payload.get("title", ""), payload.get("text", ""))
        elif atype in AGENT_ACTIONS:
            result = _agent_action(approval, payload)
        else:
            raise RuntimeError(f"unsupported action type {atype}")
        ok = bool(result.get("done", True))
        repo.set_approval_status(approval["id"], ApprovalStatus.EXECUTED if ok else ApprovalStatus.FAILED, result=result)
    except Exception as e:
        log.exception("action %s failed", approval["id"])
        result = {"done": False, "error": str(e)[:1000]}
        repo.set_approval_status(approval["id"], ApprovalStatus.FAILED, result=result)
    _record_action_result(approval, result)
    if not result.get("done", True):
        try:
            from .notify import dashboard_url, notify
            notify(f"动作执行失败：{approval['title'][:40]}", str(result.get("error") or result.get("details"))[:200],
                   level="action", ref_type="approval", ref_id=approval["id"], url=dashboard_url("outbox"))
        except Exception:
            pass
    maybe_resume(approval.get("task_id"))
    return result


def _apply_to_project(approval: dict, payload: dict) -> dict:
    task = repo.get_task(approval["task_id"]) if approval.get("task_id") else None
    ws = Path(task["workspace"]).resolve() if task and task.get("workspace") else None
    stamp = local_now().strftime("%Y%m%d-%H%M%S")
    done = []
    for f in payload.get("files") or []:
        src = Path(f["src"])
        if not src.is_absolute():
            if ws is None:
                raise RuntimeError("relative src without a task workspace")
            src = (ws / src).resolve()
        dest = Path(f["dest"])
        if ws and ws not in src.parents:
            raise RuntimeError(f"source outside task workspace: {src}")
        if not policy.writable_destination(dest):
            raise RuntimeError(f"destination not allowed: {dest}")
        if not src.exists():
            raise RuntimeError(f"source missing: {src}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        backup = None
        if dest.exists():
            backup = dest.with_name(f"{dest.stem}.awos-bak-{stamp}{dest.suffix}")
            shutil.copy2(dest, backup)
        shutil.copy2(src, dest)
        done.append({"dest": str(dest), "backup": str(backup) if backup else None})
    return {"done": True, "details": f"已写入 {len(done)} 个文件（原文件已备份）", "files": done}


def _soft_delete(payload: dict) -> dict:
    trash = load_config().path("var") / "trash" / local_now().strftime("%Y%m%d-%H%M%S")
    moved = []
    for p in payload.get("paths") or []:
        path = Path(p)
        if not policy.writable_destination(path):
            raise RuntimeError(f"path not in allowed roots: {path}")
        if path.exists():
            trash.mkdir(parents=True, exist_ok=True)
            target = trash / path.name
            shutil.move(str(path), str(target))
            moved.append({"from": str(path), "to": str(target)})
    return {"done": True, "details": f"已移入回收区 {trash}（可恢复）", "moved": moved}


def _agent_action(approval: dict, payload: dict) -> dict:
    from ..llm import run_agent

    note = approval.get("decision_note") or "（无）"
    prompt = f"""你是 AgentWorkOS 的动作执行器。用户刚刚在 Outbox 中**批准**了下面这一个动作。请准确执行它，且只执行它。

动作类型：{approval.get('action_type')}
标题：{approval['title']}
载荷（精确参数）：
```json
{json.dumps(payload, ensure_ascii=False, indent=2)}
```
用户批准时的备注（如有修改意见，以备注为准，但不得扩大动作范围）：{note}

规则：
- 只执行这一个动作。不要发送额外邮件、不要修改其它内容、不要删除任何东西。
- 发邮件时使用 Gmail 连接器；写 Notion 时使用 Notion 连接器；写日历时使用 Google Calendar 连接器。
- 如果载荷信息不足以安全执行，不要猜测，done=false 并说明缺什么。
- 完成后用 evidence 给出可核对的凭据（例如邮件 message id、Notion 页面链接）。
只输出符合 schema 的 JSON。"""
    run_id, res = run_agent(prompt, purpose="action", executor="codex", task_id=approval.get("task_id"),
                            output_schema=ACTION_RESULT_SCHEMA, sandbox="read-only", allow_connectors=True,
                            allow_network=False, timeout_s=900, check_budget=False)
    if res.status != "succeeded" or not res.structured:
        return {"done": False, "error": res.error or "no result", "run_id": run_id}
    return {**res.structured, "run_id": run_id}


__all__ = ["create_from_result", "on_decision", "maybe_resume", "expire_approvals", "execute_action",
           "create_budget_approval", "notify_pending", "dumps"]
