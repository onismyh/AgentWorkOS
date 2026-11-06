"""Codex CLI adapter: `codex exec --json --output-schema S -o last.txt -s <sandbox> -C <cwd> -` (prompt on stdin).

Verified on codex-cli 0.159.2 (see docs/playbook/executors.md):
- JSONL events: thread.started, turn.started, item.started/item.completed (agent_message, reasoning,
  command_execution, mcp_tool_call, web_search, file_change, todo_list, error), turn.completed{usage}, turn.failed, error.
- `--disable apps --disable browser_use --disable computer_use --disable plugins` removes the ChatGPT connectors
  (Gmail/Notion/Drive...) and desktop automation from a run; only shell/patch/web tools remain.
- `--output-schema` requires OpenAI strict schemas -> strictify().
"""
from __future__ import annotations

import copy
import json
import os
import threading
from typing import Any

from ..config import load_config
from .base import EventSink, Executor, RunResult, RunSpec
from .proc import classify_error, extract_json, resolve_command, run_process, trim, version_check

NO_CONNECTOR_FLAGS = ["--disable", "apps", "--disable", "plugins", "-c", "mcp_servers.node_repl.enabled=false"]
NO_DESKTOP_FLAGS = ["--disable", "browser_use", "--disable", "computer_use"]


def strictify(schema: dict) -> dict:
    """Convert a normal JSON Schema to OpenAI strict mode: every object closed, all properties required,
    optional properties made nullable; unsupported keywords dropped."""
    s = copy.deepcopy(schema)

    def walk(node: Any) -> Any:
        if isinstance(node, list):
            return [walk(n) for n in node]
        if not isinstance(node, dict):
            return node
        for k in ("minimum", "maximum", "minLength", "maxLength", "format", "default", "pattern", "minItems", "maxItems"):
            node.pop(k, None)
        t = node.get("type")
        if t == "object" or "properties" in node:
            props = node.setdefault("properties", {})
            required = set(node.get("required", []))
            for name, sub in list(props.items()):
                sub = walk(sub)
                if name not in required:
                    sub = _nullable(sub)
                props[name] = sub
            node["required"] = list(props.keys())
            node["additionalProperties"] = False
        if "items" in node:
            node["items"] = walk(node["items"])
        for key in ("anyOf", "oneOf", "allOf"):
            if key in node:
                node[key] = walk(node[key])
        if "enum" in node and isinstance(node.get("type"), list) and "null" in node["type"] and None not in node["enum"]:
            node["enum"] = list(node["enum"]) + [None]
        return node

    return walk(s)


def _nullable(sub: dict) -> dict:
    t = sub.get("type")
    if isinstance(t, list):
        if "null" not in t:
            sub["type"] = t + ["null"]
    elif isinstance(t, str):
        sub["type"] = [t, "null"]
    if "enum" in sub and None not in sub["enum"]:
        sub["enum"] = list(sub["enum"]) + [None]
    return sub


class CodexParser:
    def __init__(self, on_event: EventSink) -> None:
        self.on_event = on_event
        self.last_message = ""
        self.tokens_in = self.tokens_out = 0
        self.thread_id: str | None = None
        self.failed: str | None = None

    def feed(self, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        t = ev.get("type")
        if t == "thread.started":
            self.thread_id = ev.get("thread_id")
        elif t in ("item.started", "item.completed"):
            self._item(t, ev.get("item") or {})
        elif t == "turn.completed":
            u = ev.get("usage") or {}
            self.tokens_in += int(u.get("input_tokens") or 0)
            self.tokens_out += int(u.get("output_tokens") or 0)
            self.on_event("usage", None, u)
        elif t in ("turn.failed", "error"):
            err = ev.get("error") or ev.get("message") or ev
            self.failed = json.dumps(err, ensure_ascii=False)[:2000] if not isinstance(err, str) else err[:2000]
            self.on_event("error", t, {"error": self.failed})

    def _item(self, phase: str, it: dict) -> None:
        kind = it.get("type")
        done = phase == "item.completed"
        if kind == "agent_message" and done:
            self.last_message = it.get("text") or self.last_message
            self.on_event("message", None, {"text": trim(it.get("text"), 4000)})
        elif kind == "reasoning" and done:
            self.on_event("reasoning", None, {"text": trim(it.get("text"), 1500)})
        elif kind == "command_execution":
            if done:
                self.on_event("tool_result", "shell", {"command": trim(it.get("command"), 500), "exit_code": it.get("exit_code"),
                                                       "output": trim(it.get("aggregated_output"), 1500)})
            else:
                self.on_event("tool_call", "shell", {"command": trim(it.get("command"), 1000)})
        elif kind == "mcp_tool_call":
            name = f"{it.get('server', '')}.{it.get('tool', '')}".strip(".")
            if done:
                self.on_event("tool_result", name, {"status": it.get("status"), "error": trim(str(it.get("error") or ""), 500)})
            else:
                self.on_event("tool_call", name, {"arguments": trim(json.dumps(it.get("arguments"), ensure_ascii=False), 1500)})
        elif kind == "web_search" and done:
            self.on_event("tool_call", "web_search", {"query": trim(it.get("query"), 300)})
        elif kind == "file_change" and done:
            self.on_event("tool_call", "file_change", {"changes": trim(json.dumps(it.get("changes"), ensure_ascii=False), 1500)})
        elif kind == "error":
            self.on_event("error", "item", {"message": trim(it.get("message"), 1000)})


KEEP_ENV = ("OPENAI_", "CODEX_", "AZURE_OPENAI_")  # credential-like env Codex itself may need; all others are scrubbed
def sandbox_env(spec: RunSpec) -> dict[str, str]:
    from . import sandbox
    return sandbox.cache_env(spec.log_dir / "tmp")


PROFILES_SINCE = (0, 167)  # permission profiles (filesystem deny / per-path grants) verified from this Codex version on


def profiles_supported(version_text: str) -> bool:
    """`codex-cli 0.167.0` -> True. Older or unparsable: fall back to the coarse -s modes (never to no sandbox)."""
    import re
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", version_text or "")
    return bool(m) and (int(m.group(1)), int(m.group(2))) >= PROFILES_SINCE


class CodexExecutor(Executor):
    name = "codex"

    def _argv(self) -> list[str] | None:
        return resolve_command(load_config().get("executors.codex.command", "codex"))

    def available(self) -> tuple[bool, str]:
        return version_check(self._argv(), "codex")

    def build_argv(self, spec: RunSpec, schema_path: str | None) -> list[str]:
        base = self._argv() or ["codex"]
        argv = base + ["exec", "--json", "--skip-git-repo-check", "--ephemeral", *self.sandbox_args(spec), "-C", str(spec.cwd),
                       "-o", str(spec.log_dir / "last_message.txt")]
        if schema_path:
            argv += ["--output-schema", schema_path]
        if spec.model:
            argv += ["-m", spec.model]
        # spec.add_dirs are read-only roots: Codex can already read the whole disk in both sandboxes, and
        # `--add-dir` would grant WRITE access, so it is deliberately never passed.
        if not spec.allow_connectors:
            argv += NO_CONNECTOR_FLAGS
        argv += NO_DESKTOP_FLAGS
        if not spec.allow_network:
            argv += ["-c", 'web_search="disabled"']
        return argv + ["-"]

    @staticmethod
    def sandbox_args(spec: RunSpec) -> list[str]:
        """macOS: a Codex permission profile (Seatbelt) for the agent's commands: read the disk except credential files,
        write only the workspace (write runs) and a private per-run TMPDIR, no network. Elsewhere: Codex's -s modes."""
        from . import sandbox
        mode = "read-only" if spec.sandbox == "read-only" else "workspace-write"
        if not sandbox.enabled() or not profiles_supported(version_check(EXECUTOR._argv(), "codex")[1]):
            return ["-s", mode]
        fs = sandbox.codex_filesystem(cwd=spec.cwd, log_dir=spec.log_dir, readable=list(spec.add_dirs), write=mode != "read-only")
        body = ", ".join(f"{json.dumps(k, ensure_ascii=False)} = {json.dumps(v)}" for k, v in fs.items())
        return ["-c", f"permissions.awos.filesystem={{{body}}}", *sandbox.codex_network(sandbox.allowed_domains(spec)),
                "-c", 'default_permissions="awos"']

    def run(self, spec: RunSpec, on_event: EventSink, cancel: threading.Event) -> RunResult:
        (spec.log_dir / "tmp").mkdir(parents=True, exist_ok=True)
        schema_path = None
        if spec.output_schema:
            p = spec.log_dir / "schema.json"
            p.write_text(json.dumps(strictify(spec.output_schema), ensure_ascii=False), encoding="utf-8")
            schema_path = str(p)
        prompt = spec.prompt if not spec.system_prompt else f"{spec.system_prompt}\n\n{spec.prompt}"
        parser = CodexParser(on_event)
        pr = run_process(self.build_argv(spec, schema_path), cwd=spec.cwd, stdin_text=prompt, timeout_s=spec.timeout_s,
                         cancel=cancel, log_dir=spec.log_dir, env={**sandbox_env(spec), **(spec.env or {})},
                         on_stdout=parser.feed, on_pid=spec.on_pid, keep_secrets=KEEP_ENV)
        last_file = spec.log_dir / "last_message.txt"
        final = last_file.read_text(encoding="utf-8", errors="replace") if last_file.exists() else parser.last_message
        base = dict(exit_code=pr.exit_code, final_text=final or "", tokens_in=parser.tokens_in, tokens_out=parser.tokens_out,
                    cost_usd=0.0, model=spec.model, session_id=parser.thread_id)
        if pr.spawn_error:
            return RunResult(status="failed", error=pr.spawn_error, error_class="permanent", **base)
        if pr.cancelled:
            return RunResult(status="cancelled", error="cancelled", error_class="cancelled", **base)
        if pr.timed_out:
            return RunResult(status="timeout", error=f"timeout after {spec.timeout_s}s", error_class="transient", **base)
        if pr.exit_code != 0 or parser.failed:
            err = parser.failed or pr.stderr_tail[-1500:] or f"exit {pr.exit_code}"
            return RunResult(status="failed", error=err, error_class=classify_error(err, pr.exit_code), **base)
        structured = extract_json(final) if spec.output_schema else None
        if spec.output_schema and structured is None:
            return RunResult(status="failed", error="no structured JSON in final message", error_class="transient", **base)
        return RunResult(status="succeeded", structured=structured, **base)


EXECUTOR = CodexExecutor()
