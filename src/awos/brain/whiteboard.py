"""The whiteboard: standing rules from the user that every agent sees at the top of its prompt.

Rules are context facts of kind `rule` (pinned, written by the user from the dashboard's 上下文 page). Agents can read them
but never change them: `agent_write_fact` refuses the kind. They are injected untruncated, ahead of everything else.
"""
from __future__ import annotations

from .. import repo
from ..db import utcnow

RULE_KIND = "rule"
TITLE = "白板·所有 Agent 必须遵守的规则（用户制定，优先于其他上下文）"
SEEDED_KEY = "whiteboard.seeded"

DEFAULT_RULES: list[tuple[str, str]] = [
    ("Git 交付权限",
     "默认可以 commit 并 push 到本地分支和远端分支（用新的功能分支，如 agent/<主题>），不需要请示，系统会代为执行。"
     "不要直接动 main/master 等默认分支：不提交、不推送、不 force-push、不改写历史。"
     "要发起或合并 MR/PR 时必须先告诉用户（提议 open_mr，由用户确认），合并由用户决定。"),
    ("交付时清理中间产物",
     "交付时清理掉所有中间产物（临时脚本、草稿、调试日志、截图、缓存、重复或备份副本），只保留最终交付物；"
     "不要往工作目录或用户项目里留多余文件；提交只包含必要的改动。"),
]


def rules() -> list[dict]:
    return [f for f in repo.list_facts(kind=RULE_KIND, status="active")]


def rules_md() -> str:
    """The block injected into prompts; empty when the whiteboard is empty."""
    items = rules()
    if not items:
        return ""
    return "\n".join([f"### {TITLE}", *(f"- **{f['key']}**：{f['value']}" for f in items)])


def ensure_defaults() -> int:
    """Seed the built-in rules once. After that the user owns them: an edited rule stays, a deleted one is not re-added."""
    if repo.kv_get(SEEDED_KEY):
        return 0
    n = 0
    for key, value in DEFAULT_RULES:
        if repo.upsert_fact(RULE_KIND, key, value, source="user", confidence=1.0, actor="user", pinned=True):
            n += 1
    repo.kv_set(SEEDED_KEY, utcnow())
    return n
