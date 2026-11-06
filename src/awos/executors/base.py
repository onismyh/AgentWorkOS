"""Executor contract shared by every agent backend (codex, claude, pi, dsh, internal)."""
from __future__ import annotations

import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal

Sandbox = Literal["read-only", "workspace-write", "full"]

# on_event(kind, name, data): kind in message | tool_call | tool_result | reasoning | stderr | error | status | usage
EventSink = Callable[[str, str | None, Any], None]


@dataclass
class RunSpec:
    run_id: str
    prompt: str
    cwd: Path                                   # working directory (task workspace); the only writable dir for workspace-write
    log_dir: Path                               # var/runs/<run_id>/ : prompt.md, stdout.jsonl, stderr.log, last_message.txt
    purpose: str = "execute"                    # triage | plan | execute | verify | sensor | context | action | digest
    sandbox: Sandbox = "read-only"
    output_schema: dict | None = None           # JSON Schema for the final answer (structured output)
    timeout_s: int = 1200
    model: str | None = None
    add_dirs: list[Path] = field(default_factory=list)   # extra dirs the agent may READ (and write if sandbox allows)
    allow_network: bool = True                  # web search / fetch, and shell access to the allowed domains
    net_allow: list[str] = field(default_factory=list)    # extra domains the user approved for this task (grant_network)
    allow_connectors: bool = False              # codex apps (gmail/notion/drive...) allowed in this run
    max_budget_usd: float | None = None
    env: dict[str, str] | None = None
    system_prompt: str | None = None            # appended system instructions
    on_pid: Callable[[int], None] | None = None


@dataclass
class RunResult:
    status: Literal["succeeded", "failed", "timeout", "cancelled"]
    exit_code: int | None = None
    final_text: str = ""
    structured: dict | None = None
    cost_usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    model: str | None = None
    error: str | None = None
    error_class: Literal["transient", "permanent", "policy", "budget", "cancelled"] | None = None
    session_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)


class Executor(ABC):
    name: str = "base"

    @abstractmethod
    def available(self) -> tuple[bool, str]:
        """(ok, detail). Must be cheap (no model call): checks binary presence/version/config."""

    @abstractmethod
    def run(self, spec: RunSpec, on_event: EventSink, cancel: threading.Event) -> RunResult:
        """Run to completion (blocking). Must honour spec.timeout_s and `cancel`, kill the whole process tree,
        write raw stdout/stderr under spec.log_dir, emit normalized events through on_event, parse the final
        answer into RunResult.structured when spec.output_schema is given, and never raise for agent failures
        (return status='failed' with error/error_class instead)."""
