"""Pi coding agent adapter (`@earendil-works/pi-coding-agent`, run as `node cli.js -p --mode json`).

Pi has no sandbox/permission system: AgentWorkOS limits its tool allowlist per sandbox level and only
routes workspace-bounded work to it. Structured output is requested in the prompt and parsed from the
final assistant message.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .base import EventSink, Executor, RunResult, RunSpec
from .noderuntime import extract_json, node_exe, runtime_dir, runtime_secrets, schema_prompt

PI_ENTRY = Path("node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js")
READ_TOOLS = "read,grep,find,ls"
WRITE_TOOLS = "read,bash,edit,write,grep,find,ls"


def entry() -> Path:
    return runtime_dir("pi") / PI_ENTRY


def build_argv(spec: RunSpec, model: str | None) -> list[str]:
    tools = READ_TOOLS if spec.sandbox == "read-only" else WRITE_TOOLS
    argv = [str(node_exe()), str(entry()), "-p", "--mode", "json", "--no-approve", "--no-session",
            "--no-extensions", "--no-prompt-templates", "--no-themes", "--tools", tools]
    if model:
        argv += ["--model", model]
    if spec.system_prompt:
        argv += ["--append-system-prompt", spec.system_prompt]
    return argv


def build_env(spec: RunSpec) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWOS_")}
    env.update({"PI_CODING_AGENT_DIR": str(runtime_dir("pi-home")), "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0",
                "NO_PROXY": "localhost,127.0.0.1"})
    for k, v in (runtime_secrets("pi").get("env") or {}).items():
        env[str(k)] = str(v)
    env.update(spec.env or {})
    return env


class PiParser:
    """Incremental parser for Pi JSONL events."""

    def __init__(self, on_event: EventSink) -> None:
        self.on_event = on_event
        self.final_text = ""
        self.stop_reason: str | None = None
        self.error: str | None = None
        self.settled = False
        self.aborted = False
        self.tokens_in = self.tokens_out = 0
        self.cost = 0.0
        self.model: str | None = None
        self.session_id: str | None = None

    def feed(self, line: str) -> None:
        line = line.strip("\r\n")
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        t = ev.get("type")
        if t == "session":
            self.session_id = ev.get("id")
        elif t == "tool_execution_start":
            self.on_event("tool_call", ev.get("toolName"), {"args": ev.get("args")})
        elif t == "tool_execution_end":
            res = ev.get("result")
            self.on_event("tool_result", ev.get("toolName"), {"is_error": ev.get("isError"),
                                                             "result": str(res)[:1500] if res is not None else None})
        elif t == "message_end":
            msg = ev.get("message") or {}
            if msg.get("role") == "assistant":
                text = "".join(b.get("text", "") for b in msg.get("content") or [] if isinstance(b, dict) and b.get("type") == "text")
                if text:
                    self.final_text = text
                    self.on_event("message", None, {"text": text[:4000]})
                self.stop_reason = msg.get("stopReason")
                self.error = msg.get("errorMessage") or self.error
                self.model = msg.get("model") or self.model
                usage = msg.get("usage") or {}
                self.tokens_in += int(usage.get("input") or usage.get("inputTokens") or 0)
                self.tokens_out += int(usage.get("output") or usage.get("outputTokens") or 0)
                cost = usage.get("cost") or {}
                self.cost += float(cost.get("total") or 0) if isinstance(cost, dict) else float(cost or 0)
        elif t == "agent_settled":
            self.settled = True
            self.aborted = bool(ev.get("aborted"))
        elif t in ("auto_retry_start", "compaction_start"):
            self.on_event("status", t, {k: ev.get(k) for k in ("attempt", "reason") if k in ev})

    def succeeded(self) -> bool:
        return self.settled and not self.aborted and self.stop_reason not in ("error", "aborted", "length")


class PiExecutor(Executor):
    name = "pi"
    _avail: tuple[bool, str] | None = None

    def available(self) -> tuple[bool, str]:
        if node_exe() is None:
            return False, "portable node missing (runtime/node)"
        if not entry().exists():
            return False, "pi not installed (runtime/pi)"
        sec = runtime_secrets("pi")
        auth = runtime_dir("pi-home") / "auth.json"
        has_oauth = auth.exists() and auth.stat().st_size > 2
        if not sec.get("env") and not has_oauth:
            return False, "pi has no model credentials (var/secrets.toml [pi] env, or run /login once)"
        return True, f"pi {entry().parent.parent.parent.name}"

    def run(self, spec: RunSpec, on_event: EventSink, cancel: threading.Event) -> RunResult:
        from .proc import classify_error, run_process

        model = spec.model or runtime_secrets("pi").get("model")
        parser = PiParser(on_event)
        prompt = schema_prompt(spec.prompt, spec.output_schema)
        pr = run_process(build_argv(spec, model), cwd=spec.cwd, stdin_text=prompt, env=build_env(spec),
                         timeout_s=spec.timeout_s, cancel=cancel, log_dir=spec.log_dir, on_stdout=parser.feed,
                         on_stderr=lambda l: None, on_pid=spec.on_pid)
        base: dict[str, Any] = dict(exit_code=pr.exit_code, final_text=parser.final_text, tokens_in=parser.tokens_in,
                                    tokens_out=parser.tokens_out, cost_usd=parser.cost, model=parser.model or model,
                                    session_id=parser.session_id)
        if pr.cancelled:
            return RunResult(status="cancelled", error="cancelled", error_class="cancelled", **base)
        if pr.timed_out:
            return RunResult(status="timeout", error=f"timeout after {spec.timeout_s}s", error_class="transient", **base)
        if not parser.succeeded():
            err = parser.error or f"pi stop={parser.stop_reason} settled={parser.settled} exit={pr.exit_code} {pr.stderr_tail[-500:]}"
            return RunResult(status="failed", error=err, error_class=classify_error(err, pr.exit_code), **base)
        structured = extract_json(parser.final_text) if spec.output_schema else None
        if spec.output_schema and structured is None:
            return RunResult(status="failed", error="no JSON object in final message", error_class="transient", **base)
        return RunResult(status="succeeded", structured=structured, **base)


EXECUTOR = PiExecutor()
