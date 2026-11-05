"""Plain-language diagnosis of failed runs for the 异常 page: what went wrong, whether it already recovered, what to do."""
from __future__ import annotations

from typing import Any, Iterable

from .. import db

# (kind, label, hint) - the first matching rule wins.
_RULES: list[tuple[str, str, str]] = [
    ("restart", "守护进程重启时被中断", "重启不会丢任务：下一轮调度会自动重排。"),
    ("budget", "触到预算上限", "当日或单任务花费达到上限时的拒绝。放宽预算（config/awos.toml 的 [budget]）或批准“追加预算”后会重新排队。"),
    ("queue", "排队等预算额度超时", "只是别的运行占着额度；这种等待不消耗重试次数，会自动再排队。"),
    ("upstream", "模型服务限流或繁忙", "上游（OpenAI / Anthropic）暂时拥挤，不是本机的问题；任务会自动换执行器或稍后重试。"),
    ("network", "与模型服务的连接超时", "网络波动（常见于代理/VPN 抖动）；网络恢复后会自动重试，反复出现就检查代理设置。"),
    ("killed", "进程被异常终止", "日志里没有模型报错，多发生在守护进程被强制重启或进程树被结束时。"),
    ("timeout", "运行超时", "超过了该执行器的超时上限；任务太大时可拆分，或在配置里调大 timeout_seconds。"),
    ("permanent", "执行器报错（需要看日志）", "这类错误不会自动重试：打开运行详情，看 stderr 与输出定位原因。"),
    ("other", "未分类的失败", "打开运行详情查看日志。"),
]
_BY_KIND = {k: (label, hint) for k, label, hint in _RULES}


def classify(run: dict[str, Any]) -> str:
    err = str(run.get("error") or "").lower()
    cls = str(run.get("error_class") or "")
    if run.get("status") == "interrupted" or "daemon restarted" in err:
        return "restart"
    if cls == "budget" or "budget exhausted" in err:
        return "budget"
    if cls == "capacity" or "waited for budget reservations" in err:
        return "queue"
    if any(s in err for s in ("429", "too many requests", "at capacity", "rate limit", "overloaded", "usage limit")):
        return "upstream"
    if any(s in err for s in ("timed out", "reconnecting", "routing discovery", "connection", "network", "econnreset")):
        return "network"
    if run.get("status") == "timeout" or cls == "timeout":
        return "timeout"
    if cls == "permanent":
        return "permanent"
    if err.strip() in ("exit 1", "exit -1", "") and cls in ("transient", ""):
        return "killed"
    return "other"


def _later_successes() -> dict[tuple[str, str], str]:
    """(purpose, task_id) -> id of the newest succeeded run. Ids sort in creation order."""
    rows = db.q("SELECT purpose, COALESCE(task_id, '') t, MAX(id) mx FROM runs WHERE status = 'succeeded' GROUP BY purpose, t")
    return {(r["purpose"], r["t"]): r["mx"] for r in rows}


_CLOSED_TASK = ("done", "cancelled", "dismissed")


def annotate(runs: Iterable[dict[str, Any]]) -> None:
    """Attach `diagnosis` = {kind, label, hint, resolved, why} to every failed run dict (in place)."""
    runs = list(runs)
    if not runs:
        return
    latest_ok = _later_successes()
    for r in runs:
        kind = classify(r)
        label, hint = _BY_KIND[kind]
        resolved, why = False, None
        newest = latest_ok.get((r.get("purpose") or "", r.get("task_id") or ""))
        if newest and newest > r["id"]:
            resolved, why = True, "之后同类运行已成功"
        elif r.get("task_status") in _CLOSED_TASK:
            resolved, why = True, {"done": "任务已完成", "cancelled": "任务已取消", "dismissed": "任务已忽略"}[r["task_status"]]
        r["diagnosis"] = {"kind": kind, "label": label, "hint": hint, "resolved": resolved, "why": why}


def open_failure_count(since_utc: str) -> int:
    """Failed/timed-out runs since `since_utc` that have not recovered (what the red badge should count)."""
    rows = db.q("SELECT r.*, t.status AS task_status FROM runs r LEFT JOIN tasks t ON t.id = r.task_id "
                "WHERE r.status IN ('failed', 'timeout') AND COALESCE(r.ended_at, r.started_at, '') >= ?", (since_utc,))
    annotate(rows)
    return sum(1 for r in rows if not r["diagnosis"]["resolved"])
