"""Task -> executor routing: who should do this, given what it is, how hard it is and how much it matters.

Three questions decide the order of executors; the answer comes with plain-language reasons (kept in task.meta.routing and
shown on the task page):

  importance  high | normal | low    P0, a deadline within 3 days, or a result that reaches other people (P3 = low)
  difficulty  easy | medium | hard   triage's own estimate, else autonomy level / write access / size; bumped after a failed
                                     attempt or a rework (escalation)
  trust       which executors may see the content: mail and private categories stay with codex/claude

importance x difficulty gives a TIER (economy | standard | premium); each tier has its own executor order for read-only and
for write work (config [routing.tiers.*]). On top of that the executor that is best at the category goes first (affinity),
a triage hint counts as a soft preference, an explicit choice by the user always goes first, and executors whose daily budget
is (nearly) used up move to the back. If no executor of the tier is usable we fall back to the category route.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import llm
from ..config import load_config
from ..timeutil import local_now

log = logging.getLogger("awos.router")

DEFAULT_TIERS: dict[str, dict[str, list[str]]] = {
    "economy": {"readonly": ["dsh", "pi", "codex", "claude"], "write": ["dsh", "codex", "claude"]},
    "standard": {"readonly": ["codex", "dsh", "pi", "claude"], "write": ["codex", "dsh", "claude"]},
    "premium": {"readonly": ["claude", "codex", "dsh", "pi"], "write": ["claude", "codex", "dsh"]},
}
DEFAULT_AFFINITY = {"codex": ["dev", "data", "system", "research"],
                    "claude": ["writing", "doc", "comms", "career", "pm", "schedule", "admin"]}
DEFAULT_PRIVATE = ("comms", "career", "admin", "schedule")          # about other people / personal matters: codex and claude only
DEFAULT_HIGH_STAKES = ("comms", "career", "admin", "schedule")      # the result reaches other people
DEEPSEEK = ("pi", "dsh")                                             # mail / private content is never sent to these (codex and claude are trusted with it)
NEAR_FULL = 0.85                                                     # daily budget share after which an executor is used last (unless important)

TIER_OF = {  # (importance, difficulty) -> tier
    ("low", "easy"): "economy", ("normal", "easy"): "economy", ("high", "easy"): "standard",
    ("low", "medium"): "economy", ("normal", "medium"): "standard", ("high", "medium"): "premium",
    ("low", "hard"): "standard", ("normal", "hard"): "premium", ("high", "hard"): "premium",
}
TIER_ZH = {"economy": "经济档", "standard": "标准档", "premium": "高级档"}
IMPORTANCE_ZH = {"high": "重要", "normal": "一般", "low": "不急"}
DIFFICULTY_ZH = {"easy": "简单", "medium": "中等", "hard": "困难"}
EXEC_ZH = {"codex": "Codex", "claude": "Claude", "pi": "Pi", "dsh": "DSH"}
LEVEL_BASE = {1: 0, 2: 1, 3: 2, 4: 3, 5: 3}
DIFFICULTY_BASE = {"easy": 0, "medium": 2, "hard": 4}


@dataclass
class Route:
    tier: str
    importance: str
    difficulty: str
    order: list[str]
    reasons: list[str] = field(default_factory=list)
    trusted_only: bool = False   # mail / private content: neither the executor nor the verifier may be a DeepSeek one

    def meta(self) -> dict[str, Any]:
        return {"tier": self.tier, "importance": self.importance, "difficulty": self.difficulty, "order": self.order,
                "reasons": self.reasons, "trusted_only": self.trusted_only}


def _cfg_list(key: str, default: Any) -> list[str]:
    return list(load_config().get(key, None) or default)


def _meta(task: dict) -> dict:
    return task.get("meta") or {}


def pinned_executor(task: dict) -> str | None:
    """An executor the USER chose (new task dialog, task editor). A triage/planner suggestion is not a choice."""
    ex = task.get("executor")
    if not ex:
        return None
    return ex if _meta(task).get("executor_pinned") or task.get("source") == "manual" else None


def hinted_executor(task: dict) -> str | None:
    """The soft suggestion by triage/planner (new tasks keep it in meta; older tasks kept it in the executor column)."""
    return _meta(task).get("executor_hint") or (task.get("executor") if not pinned_executor(task) else None) or None


def importance(task: dict) -> tuple[str, str]:
    pr = int(task.get("priority") if task.get("priority") is not None else 2)
    if pr <= 0:   # P1 is "this week": routine for this system, not a reason to spend the premium executors
        return "high", "优先级 P0（今天必须处理）"
    due = str(task.get("due_at") or "")[:10]
    if due:
        try:
            left = (date.fromisoformat(due) - local_now().date()).days
            if left <= 3:
                return "high", "已过截止日期" if left < 0 else f"{left} 天内截止"
        except ValueError:
            pass
    if task.get("category") in _cfg_list("routing.high_stakes_categories", DEFAULT_HIGH_STAKES) and int(task.get("level") or 1) >= 3:
        return "high", "结果会交到别人手里"
    if pr >= 3:
        return "low", "优先级 P3"
    return "normal", "常规优先级"


def difficulty(task: dict, sandbox: str) -> tuple[str, str]:
    meta = _meta(task)
    level = int(task.get("level") or 1)
    est = meta.get("difficulty") if meta.get("difficulty") in DIFFICULTY_BASE else None
    if est:
        score, why = DIFFICULTY_BASE[est], f"分诊判断{DIFFICULTY_ZH[est]}"
    else:
        score = LEVEL_BASE.get(level, 1)
        why = f"L{level} 任务"
        if sandbox != "read-only" and task.get("category") in ("dev", "data", "system"):
            score, why = score + 1, why + "，要改代码或数据"
        if len(str(task.get("description") or "")) > 2000:
            score, why = score + 1, why + "，材料多"
    if int(task.get("attempts") or 0) >= 2:
        score, why = score + 1, why + f"，已失败 {int(task['attempts']) - 1} 次，升档"
    if meta.get("last_feedback"):
        score, why = score + 1, why + "，返工过，升档"
    return ("easy" if score <= 1 else "medium" if score <= 3 else "hard"), why


def filter_candidates(candidates: list[str], *, sandbox: str, n_mail: int, private: bool = False) -> list[str]:
    """Who may be handed this task: mail and private content stays with the executors the user trusts with it (codex/claude),
    and Pi (no sandbox of its own) only gets read-only work unless executors.pi.allow_write is on."""
    cfg = load_config()
    out = []
    for name in candidates:
        if (n_mail or private) and name in DEEPSEEK:
            continue
        if name == "pi" and sandbox != "read-only" and not cfg.get("executors.pi.allow_write", False):
            continue
        out.append(name)
    return out


def _tier_order(tier: str, mode: str) -> list[str]:
    return _cfg_list(f"routing.tiers.{tier}.{mode}", DEFAULT_TIERS[tier][mode])


def route_task(task: dict, *, sandbox: str, n_mail: int = 0) -> Route:
    cfg = load_config()
    category = task.get("category") or "other"
    imp, imp_why = importance(task)
    dif, dif_why = difficulty(task, sandbox)
    tier = TIER_OF[(imp, dif)]
    mode = "readonly" if sandbox == "read-only" else "write"
    reasons = [f"重要性：{IMPORTANCE_ZH[imp]}（{imp_why}）", f"难度：{DIFFICULTY_ZH[dif]}（{dif_why}）",
               f"据此走{TIER_ZH[tier]}（{'分析' if mode == 'readonly' else '动手'}类）"]
    order = _tier_order(tier, mode)

    front: list[str] = []
    pin = pinned_executor(task)
    if pin:
        front.append(pin)
        reasons.append(f"你指定了 {EXEC_ZH.get(pin, pin)}，优先用它")
    elif tier != "economy":   # economy work is routine: a suggestion for a pricier executor is not followed there
        hint = hinted_executor(task)
        if hint in order:
            front.append(hint)
            reasons.append(f"分诊建议 {EXEC_ZH.get(hint, hint)}")
        for name, cats in (cfg.get("routing.affinity", None) or DEFAULT_AFFINITY).items():
            if category in cats and name in order and name not in front:
                front.append(name)
                reasons.append(f"{category} 类任务 {EXEC_ZH.get(name, name)} 更擅长")
                break
    order = front + [n for n in order if n not in front]

    private = category in _cfg_list("routing.private_categories", DEFAULT_PRIVATE)
    if n_mail:
        reasons.append("含邮件原文：只交给 Codex / Claude")
    elif private:
        reasons.append(f"{category} 属个人事务：只交给 Codex / Claude")
    order = filter_candidates(order, sandbox=sandbox, n_mail=n_mail, private=private)
    if mode == "write" and "pi" in _tier_order(tier, "readonly") and not cfg.get("executors.pi.allow_write", False):
        reasons.append("Pi 没有沙箱，只做只读分析")

    order = llm.usable(order)
    if not order:   # nothing of this tier is enabled/available: the category's own route still decides
        order = filter_candidates(llm.executor_candidates(category), sandbox=sandbox, n_mail=n_mail, private=private)
        reasons.append("该档位没有可用执行器，改用类别默认路线")
    else:
        order, notes = _by_budget(order, important=(imp == "high"))
        reasons += notes
    return Route(tier=tier, importance=imp, difficulty=dif, order=order, reasons=reasons, trusted_only=bool(n_mail) or private)


def _by_budget(order: list[str], *, important: bool) -> tuple[list[str], list[str]]:
    """Executors whose daily budget is used up go last; nearly used up ones go behind the rest unless the task is important."""
    ok, soft, hard, notes = [], [], [], []
    for name in order:
        frac = llm.budget_fraction(name)
        if frac >= 1.0:
            hard.append(name)
            notes.append(f"{EXEC_ZH.get(name, name)} 今日额度已用完，排到最后")
        elif frac >= NEAR_FULL and not important:
            soft.append(name)
            notes.append(f"{EXEC_ZH.get(name, name)} 今日额度已用 {int(frac * 100)}%，后移")
        else:
            ok.append(name)
    return ok + soft + hard, notes


def describe(meta: dict | None) -> str:
    """One line for logs / tooltips: 标准档 · 重要 · 中等 → codex > dsh > claude."""
    if not meta:
        return ""
    return (f"{TIER_ZH.get(meta.get('tier'), '')} · {IMPORTANCE_ZH.get(meta.get('importance'), '')} · "
            f"{DIFFICULTY_ZH.get(meta.get('difficulty'), '')} → {' > '.join(meta.get('order') or [])}")


__all__ = ["Route", "route_task", "importance", "difficulty", "filter_candidates", "pinned_executor", "hinted_executor", "describe",
           "TIER_ZH", "IMPORTANCE_ZH", "DIFFICULTY_ZH"]
