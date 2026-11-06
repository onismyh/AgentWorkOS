"""Claude Code adapter: `claude -p --output-format stream-json --verbose --json-schema S ...` (prompt on stdin).

Verified on claude 2.1.311 (see docs/playbook/executors.md):
- stream-json: system/init, assistant (text | thinking | tool_use), user (tool_result), result{subtype,
  is_error, total_cost_usd, usage, structured_output, permission_denials}. Structured output arrives through
  the built-in StructuredOutput tool and `result.structured_output`.
- `--permission-mode dontAsk` + `--allowedTools` never prompts: anything not allowed is denied and reported in
  permission_denials. `Write(./**)` / `Edit(./**)` confine writes to the working directory.
- `--bare` skips hooks/plugins/CLAUDE.md (cheapest; used for triage/plan/verify/context); execute runs keep
  skills but use `--settings {"disableAllHooks": true}` so the user's memory plugin never records automated runs.
"""
from __future__ import annotations

import json
import threading

from ..config import load_config
from .base import EventSink, Executor, RunResult, RunSpec
from .proc import classify_error, extract_json, resolve_command, run_process, trim, version_check

BARE_PURPOSES = {"triage", "sensor", "context", "plan", "verify", "digest"}
READ_TOOLS = ["Read", "Glob", "Grep"]
WEB_TOOLS = ["WebSearch", "WebFetch"]
# Read-only runs may inspect repos/files through these shell prefixes (everything else is denied by dontAsk).
# Claude Code on Windows exposes a PowerShell tool next to Bash; both get the same rules.
_SAFE_PREFIXES = ["cd", "pwd", "ls", "wc", "head", "tail", "du", "file", "Get-ChildItem", "Get-Content", "Select-String",
                  "Get-Item", "Test-Path", "Get-Location", "Set-Location", "Measure-Object", "Resolve-Path",
                  "git status", "git --no-optional-locks status", "git diff", "git log", "git show", "git ls-files",
                  "git rev-parse", "git rev-list", "git blame", "git shortlog", "git describe", "git cat-file",
                  "git branch --list", "git branch -vv", "git stash list", "git config --get"]
# Guard rails for every run: git writes and destructive commands go through the Outbox (git_commit / git_push / open_mr /
# apply_to_project actions executed by the system), never straight from an agent. Branch commits/pushes are executed
# without sign-off by the whiteboard rule (hitl/gitops.py checks the branch); MRs/PRs and protected branches wait for the human.
_DENY_PREFIXES = ["git push", "git commit", "git reset", "git clean", "git checkout", "git rebase", "git merge", "git stash drop",
                  "git diff --output", "git log --output", "git show --output", "gh pr", "gh api", "gh repo", "gh release", "rm -rf", "rm -r", "del /s", "rmdir /s",
                  "Remove-Item", "Clear-Content", "Format-Volume", "format"]


def _rules(prefixes: list[str], exact: tuple[str, ...] = ()) -> list[str]:
    out = []
    for p in prefixes:
        for tool in ("Bash", "PowerShell"):
            out.append(f"{tool}({p}{'' if p in exact else ':*'})")
            if p.startswith("git ") and not p.startswith("git --"):
                # agents usually write `git -C <repo> status`; verified: only the space-star wildcard matches this shape
                out.append(f"{tool}(git -C * {p[4:]} *)")
    return out


SAFE_SHELL = _rules(_SAFE_PREFIXES, exact=("pwd", "git remote -v")) + ["Bash(git remote -v)", "PowerShell(git remote -v)"]
DENY_SHELL = _rules(_DENY_PREFIXES)


class ClaudeParser:
    def __init__(self, on_event: EventSink) -> None:
        self.on_event = on_event
        self.result: dict | None = None
        self.texts: list[str] = []
        self.model: str | None = None
        self.session_id: str | None = None
        self._tool_names: dict[str, str] = {}

    def feed(self, line: str) -> None:
        if not line.startswith("{"):
            return
        try:
            ev = json.loads(line)
        except ValueError:
            return
        t = ev.get("type")
        if t == "system" and ev.get("subtype") == "init":
            self.model = ev.get("model")
            self.session_id = ev.get("session_id")
        elif t == "assistant":
            for b in (ev.get("message") or {}).get("content") or []:
                bt = b.get("type")
                if bt == "text" and b.get("text"):
                    self.texts.append(b["text"])
                    self.on_event("message", None, {"text": trim(b["text"], 4000)})
                elif bt == "tool_use":
                    self._tool_names[b.get("id", "")] = b.get("name", "")
                    if b.get("name") != "StructuredOutput":
                        self.on_event("tool_call", b.get("name"), {"input": trim(json.dumps(b.get("input"), ensure_ascii=False), 1500)})
                elif bt == "thinking" and b.get("thinking"):
                    self.on_event("reasoning", None, {"text": trim(b.get("thinking"), 1500)})
        elif t == "user":
            for b in (ev.get("message") or {}).get("content") or []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    name = self._tool_names.get(b.get("tool_use_id", ""), "")
                    if name != "StructuredOutput":
                        content = b.get("content")
                        self.on_event("tool_result", name, {"is_error": b.get("is_error", False),
                                                            "output": trim(content if isinstance(content, str) else json.dumps(content, ensure_ascii=False), 1500)})
        elif t == "result":
            self.result = ev
            u = ev.get("usage") or {}
            self.on_event("usage", None, {"cost_usd": ev.get("total_cost_usd"), "input_tokens": u.get("input_tokens"),
                                          "output_tokens": u.get("output_tokens"), "turns": ev.get("num_turns")})
            for d in ev.get("permission_denials") or []:
                self.on_event("error", "permission_denied", {"tool": d.get("tool_name"),
                                                             "input": trim(json.dumps(d.get("tool_input"), ensure_ascii=False), 500)})


class ClaudeExecutor(Executor):
    name = "claude"

    def _argv(self) -> list[str] | None:
        return resolve_command(load_config().get("executors.claude.command", "claude"))

    def available(self) -> tuple[bool, str]:
        return version_check(self._argv(), "claude")

    def build_argv(self, spec: RunSpec) -> list[str]:
        argv = (self._argv() or ["claude"]) + ["-p", "--output-format", "stream-json", "--verbose", "--no-session-persistence",
                                               "--permission-mode", "dontAsk"]
        if spec.purpose in BARE_PURPOSES:
            argv.append("--bare")
        else:
            argv += ["--settings", json.dumps({"disableAllHooks": True})]
        if spec.model:
            argv += ["--model", spec.model]
        if spec.max_budget_usd:
            argv += ["--max-budget-usd", f"{float(spec.max_budget_usd):.2f}"]
        if spec.output_schema:
            argv += ["--json-schema", json.dumps(spec.output_schema, ensure_ascii=False)]
        allowed = list(READ_TOOLS) + ["Write(./**)", "Edit(./**)"]
        if spec.sandbox == "read-only":
            allowed += SAFE_SHELL  # vetted read-only commands so the agent can inspect repos instead of giving up
        else:
            allowed += ["MultiEdit(./**)", "Bash", "PowerShell", "NotebookEdit(./**)", "TodoWrite", "Skill"]
        if spec.allow_network:
            allowed += WEB_TOOLS
        argv += ["--allowedTools", " ".join(allowed)]
        disallowed = ([] if spec.allow_network else list(WEB_TOOLS)) + list(DENY_SHELL)
        if disallowed:
            argv += ["--disallowedTools", " ".join(disallowed)]
        for d in spec.add_dirs:
            if d.exists():
                argv += ["--add-dir", str(d)]  # read access; writes stay limited by the ./** rules
        if spec.system_prompt:
            argv += ["--append-system-prompt", spec.system_prompt]
        return argv

    def run(self, spec: RunSpec, on_event: EventSink, cancel: threading.Event) -> RunResult:
        parser = ClaudeParser(on_event)
        pr = run_process(self.build_argv(spec), cwd=spec.cwd, stdin_text=spec.prompt, timeout_s=spec.timeout_s, cancel=cancel,
                         log_dir=spec.log_dir, env=spec.env, on_stdout=parser.feed, on_pid=spec.on_pid)
        res = parser.result or {}
        usage = res.get("usage") or {}
        final = res.get("result") if isinstance(res.get("result"), str) else "\n".join(parser.texts)
        (spec.log_dir / "last_message.txt").write_text(final or "", encoding="utf-8")
        base = dict(exit_code=pr.exit_code, final_text=final or "", cost_usd=float(res.get("total_cost_usd") or 0),
                    tokens_in=int(usage.get("input_tokens") or 0) + int(usage.get("cache_read_input_tokens") or 0)
                    + int(usage.get("cache_creation_input_tokens") or 0),
                    tokens_out=int(usage.get("output_tokens") or 0), model=parser.model or spec.model,
                    session_id=parser.session_id)
        if pr.spawn_error:
            return RunResult(status="failed", error=pr.spawn_error, error_class="permanent", **base)
        if pr.cancelled:
            return RunResult(status="cancelled", error="cancelled", error_class="cancelled", **base)
        if pr.timed_out:
            return RunResult(status="timeout", error=f"timeout after {spec.timeout_s}s", error_class="transient", **base)
        subtype = res.get("subtype") or ""
        if not res or res.get("is_error") or subtype != "success" or pr.exit_code not in (0, None):
            err = f"{subtype or 'no result'}: {(final or '')[:500]} {pr.stderr_tail[-800:]}".strip()
            cls = "budget" if "budget" in subtype else ("transient" if subtype == "error_max_turns" else classify_error(err, pr.exit_code))
            return RunResult(status="failed", error=err, error_class=cls, **base)
        structured = res.get("structured_output")
        if spec.output_schema and not isinstance(structured, dict):
            structured = extract_json(final)
        if spec.output_schema and structured is None:
            return RunResult(status="failed", error="no structured output", error_class="transient", **base)
        return RunResult(status="succeeded", structured=structured if spec.output_schema else None, **base)


EXECUTOR = ClaudeExecutor()
