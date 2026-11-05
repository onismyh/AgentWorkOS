"""DSH (DeepSeek Harness) adapter: `node lib/bin.js --profile headless --json ... -` (NDJSON, `final` event).

DSH has a Windows ACL sandbox (`--permission-mode workspace-write|read-only`). Session-log upload to DeepSeek
and telemetry are disabled. `--add-dir` breaks tool calls on Windows, so extra readable dirs are not passed.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any

from .base import EventSink, Executor, RunResult, RunSpec
from .noderuntime import extract_json, node_exe, runtime_dir, runtime_secrets, schema_prompt

DSH_ENTRY = Path("node_modules/@deepseek-ai/dsh/lib/bin.js")


def entry() -> Path:
    return runtime_dir("dsh") / DSH_ENTRY


def patch_file(spec: RunSpec, model: str | None) -> Path:
    """Compose the worker overlay: no session-log upload + optional default model row."""
    from ..config import ROOT
    base = (ROOT / "config" / "runtimes" / "dsh-worker.patch.yml").read_text(encoding="utf-8")
    sec = runtime_secrets("dsh")
    provider = sec.get("provider")
    if model and provider:
        base += f"\n- id: agent-default-model\n  config:\n    provider: {provider}\n    model: {model}\n"
    p = spec.log_dir / "dsh.patch.yml"
    p.write_text(base, encoding="utf-8")
    return p


def build_argv(spec: RunSpec, model: str | None) -> list[str]:
    mode = "read-only" if spec.sandbox == "read-only" else "workspace-write"
    return [str(node_exe()), str(entry()), "--profile", "headless", "--patch", str(patch_file(spec, model)),
            "--json", "--permission-mode", mode, "--approval-policy", "never", "-"]


def build_env(spec: RunSpec) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("AWOS_")}
    env.update({"DSH_HOME": str(runtime_dir("dsh-home")), "DSH_TELEMETRY_DISABLED": "1", "DSH_TELEMETRY_MODE": "DISABLED",
                "NO_PROXY": "localhost,127.0.0.1"})
    for k, v in (runtime_secrets("dsh").get("env") or {}).items():
        env[str(k)] = str(v)
    env.update(spec.env or {})
    return env


class DshParser:
    def __init__(self, on_event: EventSink) -> None:
        self.on_event = on_event
        self.final: dict | None = None
        self.texts: list[str] = []
        self.error: str | None = None
        self.session_id: str | None = None
        self.tokens_in = self.tokens_out = 0

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
            self.session_id = ev.get("sessionId")
        elif t == "tool_call":
            self.on_event("tool_call", ev.get("tool"), {"input": ev.get("input")})
        elif t == "tool_result":
            self.on_event("tool_result", ev.get("callId"), {"status": ev.get("status"), "error": ev.get("error"),
                                                           "result": str(ev.get("result"))[:1500]})
        elif t == "text":
            self.texts.append(ev.get("text") or "")
            self.on_event("message", None, {"text": (ev.get("text") or "")[:4000]})
        elif t == "status":
            usage = ev.get("usage") or {}
            if ev.get("phase") == "turn_end" and usage:
                self.tokens_in = int(usage.get("inputTokens") or usage.get("input_tokens") or self.tokens_in)
                self.tokens_out = int(usage.get("outputTokens") or usage.get("output_tokens") or self.tokens_out)
        elif t == "final":
            self.final = ev
        elif t == "error":
            self.error = json.dumps(ev, ensure_ascii=False)[:1500]
            self.on_event("error", None, ev)

    @property
    def text(self) -> str:
        if self.final and self.final.get("text"):
            return self.final["text"]
        return "\n".join(self.texts)

    def succeeded(self, exit_code: int | None) -> bool:
        reason = (self.final or {}).get("reason") or {}
        return exit_code == 0 and (reason.get("kind") if isinstance(reason, dict) else reason) == "completed"


class DshExecutor(Executor):
    name = "dsh"

    def available(self) -> tuple[bool, str]:
        if node_exe() is None:
            return False, "portable node missing (runtime/node)"
        if not entry().exists():
            return False, "dsh not installed (runtime/dsh)"
        if not (runtime_secrets("dsh").get("env")):
            return False, "dsh has no model credentials (var/secrets.toml [dsh] env, e.g. DEEPSEEK_API_KEY)"
        return True, "dsh"

    def run(self, spec: RunSpec, on_event: EventSink, cancel: threading.Event) -> RunResult:
        from .proc import classify_error, run_process

        model = spec.model or runtime_secrets("dsh").get("model")
        parser = DshParser(on_event)
        pr = run_process(build_argv(spec, model), cwd=spec.cwd, stdin_text=schema_prompt(spec.prompt, spec.output_schema),
                         env=build_env(spec), timeout_s=spec.timeout_s, cancel=cancel, log_dir=spec.log_dir,
                         on_stdout=parser.feed, on_stderr=lambda l: None, on_pid=spec.on_pid)
        base: dict[str, Any] = dict(exit_code=pr.exit_code, final_text=parser.text, tokens_in=parser.tokens_in,
                                    tokens_out=parser.tokens_out, model=model, session_id=parser.session_id)
        if pr.cancelled:
            return RunResult(status="cancelled", error="cancelled", error_class="cancelled", **base)
        if pr.timed_out:
            return RunResult(status="timeout", error=f"timeout after {spec.timeout_s}s", error_class="transient", **base)
        if not parser.succeeded(pr.exit_code):
            err = parser.error or f"dsh final={json.dumps((parser.final or {}).get('reason'))} exit={pr.exit_code} {pr.stderr_tail[-500:]}"
            return RunResult(status="failed", error=err, error_class=classify_error(err, pr.exit_code), **base)
        structured = extract_json(parser.text) if spec.output_schema else None
        if spec.output_schema and structured is None:
            return RunResult(status="failed", error="no JSON object in final text", error_class="transient", **base)
        return RunResult(status="succeeded", structured=structured, **base)


EXECUTOR = DshExecutor()
