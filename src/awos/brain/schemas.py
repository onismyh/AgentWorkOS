"""JSON Schemas for every structured agent output. Executors convert them to strict mode where required."""
from __future__ import annotations

from ..models import CATEGORIES, KINDS

_CATS = list(CATEGORIES)
_KINDS = list(KINDS)
_EXECS = ["codex", "claude", "pi", "dsh"]

TRIAGE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "items": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "ref_ids": {"type": "array", "items": {"type": "string"},
                                "description": "ids of the signals/tasks this item covers (merge duplicates)"},
                    "action": {"type": "string", "enum": ["create_task", "update_existing", "ignore", "context_only"]},
                    "existing_task_id": {"type": ["string", "null"]},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "kind": {"type": "string", "enum": _KINDS},
                    "category": {"type": "string", "enum": _CATS},
                    "priority": {"type": "integer", "minimum": 0, "maximum": 3},
                    "level": {"type": "integer", "minimum": 1, "maximum": 5},
                    "level_reason": {"type": "string"},
                    "complexity": {"type": "string", "enum": ["simple", "complex"]},
                    "autostart": {"type": "boolean"},
                    "project": {"type": ["string", "null"]},
                    "due_at": {"type": ["string", "null"], "description": "YYYY-MM-DD or null"},
                    "acceptance": {"type": "string"},
                    "executor_hint": {"type": ["string", "null"], "enum": _EXECS + [None]},
                    "difficulty": {"type": ["string", "null"], "enum": ["easy", "medium", "hard", None],
                                   "description": "how hard the work itself is (used to pick the executor tier)"},
                    "reason": {"type": "string", "description": "why create/ignore; for ignore keep it short"},
                    "headline": {"type": "string", "description": "<=26 chars, verb-first card title"},
                    "gist": {"type": "string", "description": "<=50 chars one-line status/why-you"},
                    "keywords": {"type": "array", "items": {"type": "string"}, "description": "3-5 short keywords"},
                },
                "required": ["ref_ids", "action", "title", "kind", "category", "priority", "level", "level_reason",
                             "complexity", "autostart", "acceptance", "reason", "headline", "gist", "keywords"],
            },
        },
    },
    "required": ["items"],
}

PLAN_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "decompose": {"type": "boolean"},
        "rationale": {"type": "string"},
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string", "description": "short unique key like s1"},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "category": {"type": "string", "enum": _CATS},
                    "level": {"type": "integer", "minimum": 1, "maximum": 5},
                    "executor_hint": {"type": ["string", "null"], "enum": _EXECS + [None]},
                    "difficulty": {"type": ["string", "null"], "enum": ["easy", "medium", "hard", None]},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {"type": "string"},
                },
                "required": ["key", "title", "description", "category", "level", "depends_on", "acceptance"],
            },
        },
        "synthesis": {"type": "string", "description": "how the parent combines sub-results into the final deliverable"},
    },
    "required": ["decompose", "rationale", "subtasks", "synthesis"],
}

EXECUTE_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "status": {"type": "string", "enum": ["completed", "partial", "blocked", "failed"]},
        "summary": {"type": "string", "description": "markdown summary of what was done / found, for the human"},
        "deliverables": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "file path relative to the working directory"},
                    "title": {"type": "string"},
                    "kind": {"type": "string", "enum": ["report", "code", "draft", "data", "patch", "file"]},
                },
                "required": ["path", "title", "kind"],
            },
        },
        "verification": {
            "type": "object",
            "properties": {
                "performed": {"type": "boolean"},
                "method": {"type": "string"},
                "passed": {"type": "boolean"},
                "details": {"type": "string"},
            },
            "required": ["performed", "method", "passed", "details"],
        },
        "proposed_actions": {
            "type": "array",
            "description": "external / irreversible actions you want to take; they are executed only after human approval",
            "items": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": ["send_email", "post_message", "publish", "git_push", "git_commit", "open_mr", "apply_to_project",
                                                        "calendar_write", "notion_write", "delete", "spend", "submit_form", "grant_network", "other"]},
                    "title": {"type": "string"},
                    "description": {"type": "string"},
                    "risk": {"type": "string", "enum": ["low", "medium", "high"]},
                    "payload_json": {"type": "string", "description": "JSON object string with exact parameters"},
                },
                "required": ["type", "title", "description", "risk", "payload_json"],
            },
        },
        "decisions_needed": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "question": {"type": "string"},
                    "context": {"type": "string"},
                    "options": {"type": "array", "items": {
                        "type": "object",
                        "properties": {"key": {"type": "string"}, "label": {"type": "string"}, "consequence": {"type": "string"}},
                        "required": ["key", "label", "consequence"]}},
                    "recommendation": {"type": "string", "description": "option key + one-line reason"},
                },
                "required": ["question", "context", "options", "recommendation"],
            },
        },
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"}, "title": {"type": "string"}, "description": {"type": "string"},
                    "category": {"type": "string", "enum": _CATS}, "level": {"type": "integer", "minimum": 1, "maximum": 5},
                    "depends_on": {"type": "array", "items": {"type": "string"}}, "acceptance": {"type": "string"},
                },
                "required": ["key", "title", "description", "category", "level", "depends_on", "acceptance"],
            },
        },
        "followups": {"type": "array", "items": {"type": "string"}},
        "context_updates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "kind": {"type": "string", "enum": ["role", "project", "goal", "interest", "habit", "preference", "person", "deadline", "note"]},
                    "key": {"type": "string"}, "value": {"type": "string"},
                },
                "required": ["kind", "key", "value"],
            },
        },
    },
    "required": ["status", "summary", "deliverables", "verification", "proposed_actions", "decisions_needed",
                 "subtasks", "followups", "context_updates"],
}

VERIFY_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "issues": {"type": "array", "items": {"type": "string"}},
        "feedback": {"type": "string", "description": "concrete rework instructions if not passed"},
    },
    "required": ["passed", "score", "issues", "feedback"],
}

CONTEXT_SCHEMA: dict = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "op": {"type": "string", "enum": ["upsert", "stale"]},
                    "kind": {"type": "string", "enum": ["role", "project", "goal", "interest", "habit", "preference", "person", "deadline", "note"]},
                    "key": {"type": "string", "description": "short stable identifier, e.g. project name"},
                    "value": {"type": "string", "description": "one or two sentences"},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string"},
                },
                "required": ["op", "kind", "key", "value", "confidence", "evidence"],
            },
        },
        "focus_summary": {"type": "string", "description": "what the user is focused on right now (markdown, <= 12 lines)"},
        "opportunities": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"title": {"type": "string"}, "why": {"type": "string"},
                               "category": {"type": "string", "enum": _CATS}, "level": {"type": "integer", "minimum": 1, "maximum": 5}},
                "required": ["title", "why", "category", "level"],
            },
        },
    },
    "required": ["facts", "focus_summary", "opportunities"],
}
