"""DSH (DeepSeek Harness) adapter: `node lib/bin.js --profile headless --json ... -` (NDJSON, `final` event).

DSH confines its own commands (`--permission-mode workspace-write|read-only`: bwrap/Landlock on Linux, ACL restricted
token on Windows; it fails closed when no sandbox is available). Its macOS Seatbelt profile only limits writes (and allows
the temp areas), so on macOS the whole DSH process runs under AgentWorkOS's stricter profile instead (executors/sandbox.py:
workspace-only writes, credential files unreadable, private TMPDIR) and DSH's `runnerCommand` hook is pointed at a
pass-through runner (macOS refuses nested Seatbelt sandboxes; the outer one already confines every descendant).
Session-log upload to DeepSeek and telemetry are disabled. `--add-dir` breaks tool calls on Windows, so extra readable
dirs are not passed. Credentials: a DeepSeek API key (var/secrets.toml [dsh] env) or the DeepSeek Harness account login
([dsh] credentials = path of its .credentials.yaml), linked into the isolated DSH_HOME.
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
DSH_DATA_DIRS = ("sessions", "storages", "cache", "attachments", "llm-deepseek", "tmp")
# bwrap-style profile args come first, then `--`, then the command: drop the former, exec the latter.
PASSTHROUGH_RUNNER = ["/bin/sh", "-c", 'while [ "$#" -gt 0 ] && [ "$1" != "--" ]; do shift; done; '
                      '[ "$#" -gt 0 ] || { echo "awos-runner: no command" >&2; exit 125; }; shift; exec "$@"', "awos-runner"]


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
    if account_credentials() is not None:
        # The headless profile ships without the account platform and the local credential store: mount both so the
        # deepseek-account provider can read the login linked into DSH_HOME (no browser sign-in is ever started here).
        base += ("\n- id: credentials\n  disabled: false\n- id: authorization\n  disabled: false\n"
                 "- id: deepseek-account\n  disabled: false\n")
    from . import sandbox
    if sandbox.enabled():
        base += ("\n- id: sandbox\n  config:\n"
                 f"    runnerCommand: {json.dumps(PASSTHROUGH_RUNNER)}\n"
                 '    runnerFailureSignatures: ["awos-runner:"]\n')
    p = spec.log_dir / "dsh.patch.yml"
    p.write_text(base, encoding="utf-8")
    return p


def build_argv(spec: RunSpec, model: str | None) -> list[str]:
    mode = "read-only" if spec.sandbox == "read-only" else "workspace-write"
    return [str(node_exe()), str(entry()), "--profile", "headless", "--patch", str(patch_file(spec, model)),
            "--json", "--permission-mode", mode, "--approval-policy", "never", "-"]


def account_credentials() -> Path | None:
    p = runtime_secrets("dsh").get("credentials")
    if not p or not runtime_secrets("dsh").get("provider"):
        return None
    p = Path(os.path.expanduser(str(p)))
    return p if p.is_file() else None


def prepare_home() -> Path:
    """runtime/dsh-home, with the account login linked in (by reference: a re-login in the app is picked up)."""
    home = runtime_dir("dsh-home")
    home.mkdir(parents=True, exist_ok=True)
    src = account_credentials()
    if src is not None:
        dst = home / ".credentials.yaml"
        if not (dst.is_symlink() and os.path.realpath(dst) == os.path.realpath(src)):
            if dst.is_symlink() or dst.exists():
                dst.unlink()
            try:
                dst.symlink_to(src)
            except OSError:  # Windows without symlink rights
                import shutil
                shutil.copy2(src, dst)
                os.chmod(dst, 0o600)
    return home


def build_env(spec: RunSpec) -> dict[str, str]:
    """Only what DSH needs on top of the scrubbed inherited env (proc.child_env drops credential-looking variables)."""
    env = {"DSH_HOME": str(runtime_dir("dsh-home")), "DSH_TELEMETRY_DISABLED": "1", "DSH_TELEMETRY_MODE": "DISABLED",
           "NO_PROXY": "localhost,127.0.0.1"}
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
        self.tokens_in = self.tokens_out = self.tokens_cached = 0
        self._tools: dict[str, str] = {}

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
            self._tools[ev.get("callId", "")] = ev.get("tool", "")
            self.on_event("tool_call", ev.get("tool"), {"input": ev.get("input")})
        elif t == "tool_result":
            self.on_event("tool_result", self._tools.get(ev.get("callId", ""), ev.get("callId")),
                          {"status": ev.get("status"), "error": ev.get("error"), "result": str(ev.get("result"))[:1500]})
        elif t == "text":
            self.texts.append(ev.get("text") or "")
            self.on_event("message", None, {"text": (ev.get("text") or "")[:4000]})
        elif t == "status":
            usage = ev.get("usage") or {}
            if ev.get("phase") == "step_end" and usage:  # usage is reported per step: sum them
                self.tokens_in += int(usage.get("inputTokens") or 0)
                self.tokens_out += int(usage.get("outputTokens") or 0)
                self.tokens_cached += int(usage.get("cacheReadTokens") or 0)
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
        if runtime_secrets("dsh").get("env"):
            return True, "dsh (API key)"
        if account_credentials() is not None:
            return True, "dsh (DeepSeek account login)"
        return False, "dsh has no model credentials (run `awos setup`: DEEPSEEK_API_KEY or the DeepSeek Harness account login)"

    def run(self, spec: RunSpec, on_event: EventSink, cancel: threading.Event) -> RunResult:
        from .proc import classify_error, run_process

        model = spec.model or runtime_secrets("dsh").get("model")
        from . import egress, sandbox
        parser = DshParser(on_event)
        home = prepare_home()
        cred = account_credentials()
        # DSH may write its data (sessions, storages, caches, attachments); not its profile / patch layers (plugins,
        # `!!js`), instructions (AGENTS.md ...), skills or .env: a run must not plant anything the next run would load.
        data = [home / d for d in DSH_DATA_DIRS]
        for d in data:
            d.mkdir(parents=True, exist_ok=True)
        writable = data + ([] if spec.sandbox == "read-only" else [spec.cwd])
        with egress.EgressProxy([*sandbox.allowed_domains(spec), *sandbox.AGENT_DOMAINS["dsh"]]) as px:
            argv, sb_env = sandbox.wrap(build_argv(spec, model), log_dir=spec.log_dir, writable=writable,
                                        readable=[spec.cwd, *spec.add_dirs],
                                        # DSH rewrites its profile's constant root file on every boot
                                        writable_files=[home / "profiles" / "headless" / "cordis.yml", home / ".anonymous-user-id"],
                                        own_files=[cred] if cred else [], proxy=px)
            pr = run_process(argv, cwd=spec.cwd, stdin_text=schema_prompt(spec.prompt, spec.output_schema),
                             env={**build_env(spec), **sb_env}, timeout_s=spec.timeout_s, cancel=cancel, log_dir=spec.log_dir,
                             on_stdout=parser.feed, on_stderr=lambda l: None, on_pid=spec.on_pid,
                             proxy=False)  # network only through px
        egress.report(px, on_event)
        from ..config import load_config
        price = load_config().section("executors.dsh")  # USD per million tokens; DSH reports tokens but no cost
        cost = (parser.tokens_in * float(price.get("usd_per_mtok_in", 0.5)) + parser.tokens_cached * float(price.get("usd_per_mtok_cached", 0.05))
                + parser.tokens_out * float(price.get("usd_per_mtok_out", 2.0))) / 1e6
        base: dict[str, Any] = dict(exit_code=pr.exit_code, final_text=parser.text,
                                    tokens_in=parser.tokens_in + parser.tokens_cached, tokens_out=parser.tokens_out,
                                    cost_usd=round(cost, 6), model=model or "deepseek-flash", session_id=parser.session_id)
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
