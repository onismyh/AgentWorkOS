"""Policy engine: effective autonomy level, sandbox, readable dirs, autostart and approval rules."""
from __future__ import annotations

import tomllib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from ..config import ROOT, load_config
from ..models import LEVELS, SENSITIVE_ACTIONS


@lru_cache(maxsize=1)
def load_policy() -> dict[str, Any]:
    path = ROOT / "config" / "policy.toml"
    data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    override = load_config().get("policy", {}) or {}
    for section, values in override.items():
        data.setdefault(section, {}).update(values)
    return data


def reset_policy_cache() -> None:
    load_policy.cache_clear()


@dataclass
class Decision:
    level: int                 # effective level
    cap: int
    sandbox: str
    readable: list[Path]
    reasons: list[str]


def level_cap(task: dict) -> tuple[int, list[str]]:
    pol = load_policy()
    reasons: list[str] = []
    cap = int(pol.get("levels", {}).get("max_by_category", {}).get(task.get("category") or "other", 4))
    if cap < 5:
        reasons.append(f"类别 {task.get('category')} 上限 L{cap}")
    if task.get("level_cap"):
        cap = min(cap, int(task["level_cap"]))
        reasons.append(f"任务上限 L{task['level_cap']}")
    meta = task.get("meta") or {}
    if meta.get("missing_capability"):
        cap = min(cap, 2)
        reasons.append(f"缺少能力: {meta['missing_capability']}")
    return cap, reasons


def decide(task: dict) -> Decision:
    pol = load_policy()
    cap, reasons = level_cap(task)
    level = max(1, min(int(task.get("level") or 1), cap))
    sandbox = str(pol.get("sandbox", {}).get("by_level", {}).get(str(level), "read-only"))
    readable = [Path(p) for p in pol.get("permissions", {}).get("readable_roots", []) if Path(p).exists()]
    return Decision(level=level, cap=cap, sandbox=sandbox, readable=readable, reasons=reasons)


def should_autostart(task: dict, triage_autostart: bool) -> tuple[bool, str]:
    pol = load_policy()
    cfg = load_config()
    if (task.get("kind") or "") in pol.get("levels", {}).get("no_autostart_kinds", []):
        # decisions still get L2 analysis automatically; the decision itself goes to the Outbox
        return True, "decision: AI prepares analysis, human decides in Outbox"
    if not triage_autostart:
        return False, "triage suggested human acceptance first"
    if int(task.get("level") or 1) > int(cfg.get("scheduler.autostart_max_level", 5)):
        return False, "level above autostart_max_level"
    if int(task.get("priority") or 2) > int(cfg.get("scheduler.autostart_min_priority", 3)):
        return False, "priority below autostart threshold"
    return True, "policy allows autostart"


def needs_approval(action_type: str) -> bool:
    pol = load_policy()
    always = set(pol.get("actions", {}).get("always_approve", [])) | SENSITIVE_ACTIONS
    return action_type in always or action_type not in {"write_workspace", "create_subtask", "update_context"}


def approval_expiry_hours(risk: str) -> float:
    return float(load_policy().get("actions", {}).get("expiry_hours", {}).get(risk, 72))


def verify_levels() -> set[int]:
    return set(load_policy().get("verify", {}).get("levels", [5]))


def min_verify_score() -> int:
    return int(load_policy().get("verify", {}).get("min_score", 7))


def auto_apply_allowed(dest: Path) -> bool:
    roots = [Path(p).resolve() for p in load_policy().get("permissions", {}).get("auto_apply_roots", [])]
    d = dest.resolve()
    return any(d == r or r in d.parents for r in roots)


def writable_destination(dest: Path) -> bool:
    """apply_to_project destinations must be inside readable_roots (the user's own work dirs)."""
    roots = [Path(p).resolve() for p in load_policy().get("permissions", {}).get("readable_roots", [])]
    d = dest.resolve()
    return any(d == r or r in d.parents for r in roots)


def level_brief(level: int) -> str:
    info = LEVELS[level]
    return f"{info['name']}：{info['deliverable']}"
