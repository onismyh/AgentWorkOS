"""Core vocabulary: statuses, levels, categories and the task state machine."""
from __future__ import annotations

from enum import StrEnum


class TaskStatus(StrEnum):
    INBOX = "inbox"                  # captured, not yet triaged
    TRIAGED = "triaged"              # classified; waiting for the human to accept/handle (not auto-started)
    READY = "ready"                  # queued for the scheduler
    BLOCKED = "blocked"              # waiting on dependencies (sub-tasks)
    RUNNING = "running"              # an agent run holds the lease
    WAITING_APPROVAL = "waiting_approval"  # paused on an Outbox item
    WAITING_USER = "waiting_user"    # AI did its part (per level); the human must take over
    REVIEW = "review"                # deliverable ready; human acceptance requested
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DISMISSED = "dismissed"          # triage/human decided it is noise
    SNOOZED = "snoozed"


TERMINAL = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DISMISSED}
ACTIVE = {TaskStatus.READY, TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL}

# Allowed transitions. Anything not listed is rejected by repo.set_task_status (unless force=True).
TRANSITIONS: dict[TaskStatus, set[TaskStatus]] = {
    TaskStatus.INBOX: {TaskStatus.TRIAGED, TaskStatus.READY, TaskStatus.DISMISSED, TaskStatus.CANCELLED, TaskStatus.SNOOZED, TaskStatus.BLOCKED},
    TaskStatus.TRIAGED: {TaskStatus.READY, TaskStatus.DISMISSED, TaskStatus.CANCELLED, TaskStatus.SNOOZED, TaskStatus.WAITING_USER, TaskStatus.DONE, TaskStatus.BLOCKED},
    TaskStatus.READY: {TaskStatus.RUNNING, TaskStatus.BLOCKED, TaskStatus.CANCELLED, TaskStatus.SNOOZED, TaskStatus.TRIAGED},
    TaskStatus.BLOCKED: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.RUNNING: {TaskStatus.READY, TaskStatus.BLOCKED, TaskStatus.WAITING_APPROVAL, TaskStatus.WAITING_USER,
                         TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED},
    TaskStatus.WAITING_APPROVAL: {TaskStatus.READY, TaskStatus.REVIEW, TaskStatus.DONE, TaskStatus.WAITING_USER,
                                  TaskStatus.CANCELLED, TaskStatus.FAILED},
    TaskStatus.WAITING_USER: {TaskStatus.READY, TaskStatus.DONE, TaskStatus.CANCELLED, TaskStatus.DISMISSED, TaskStatus.SNOOZED},
    TaskStatus.REVIEW: {TaskStatus.DONE, TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.WAITING_USER},
    TaskStatus.SNOOZED: {TaskStatus.INBOX, TaskStatus.TRIAGED, TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.DISMISSED},
    TaskStatus.FAILED: {TaskStatus.READY, TaskStatus.CANCELLED, TaskStatus.DISMISSED},
    TaskStatus.DONE: {TaskStatus.READY},          # reopen
    TaskStatus.CANCELLED: {TaskStatus.READY},
    TaskStatus.DISMISSED: {TaskStatus.INBOX, TaskStatus.TRIAGED},
}


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    INTERRUPTED = "interrupted"      # daemon died / process vanished


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"
    CANCELLED = "cancelled"
    EXECUTING = "executing"
    EXECUTED = "executed"
    FAILED = "failed"


# Autonomy levels (how far the AI may push a task on its own).
LEVELS: dict[int, dict[str, str]] = {
    1: {"name": "L1 理解整理", "en": "Understand & organize",
        "deliverable": "背景摘要、待办提取、关键信息整理（briefing）", "handoff": "waiting_user"},
    2: {"name": "L2 分析建议", "en": "Analyze & advise",
        "deliverable": "问题分析、可选方案、推荐方案与执行计划", "handoff": "decision"},
    3: {"name": "L3 部分完成", "en": "Partial execution",
        "deliverable": "可交付的中间成果（草稿、代码骨架、数据整理）", "handoff": "review"},
    4: {"name": "L4 大部分自主", "en": "Mostly autonomous",
        "deliverable": "完成大部分工作，关键环节进入 Outbox 由人确认", "handoff": "review"},
    5: {"name": "L5 端到端", "en": "End-to-end",
        "deliverable": "端到端完成、自主验证并交付；对外动作仍走 Outbox", "handoff": "done"},
}

CATEGORIES: dict[str, str] = {
    "dev": "研发 / 编码 / 调试",
    "research": "信息检索 / 文献调研",
    "writing": "论文 / 文档写作与修改",
    "doc": "文档处理（格式转换、整理、表格）",
    "data": "数据分析 / 模型计算",
    "pm": "项目管理 / 进度跟踪",
    "schedule": "日程 / 会议 / 截止日期",
    "comms": "沟通协作（邮件、消息回复）",
    "admin": "行政事务（报销、申请、表格）",
    "career": "求职 / 职业发展",
    "learning": "学习 / 技术兴趣",
    "system": "AgentWorkOS 自身维护",
    "other": "其他",
}

KINDS = {"todo": "待办事项", "issue": "潜在问题", "opportunity": "项目机会", "decision": "需要决策", "info": "信息"}

# Action types that ALWAYS require human approval through the Outbox, whatever the level.
SENSITIVE_ACTIONS = {
    "send_email", "post_message", "publish", "git_push", "calendar_write", "notion_write",
    "delete", "spend", "apply_to_project", "submit_form", "other",
}

EXECUTORS = ("codex", "claude", "pi", "dsh", "internal")
