"""Executor adapter tests: pure-unit (argv, parsing, strictify) plus process-tree behaviour and opt-in live runs."""
from __future__ import annotations

import json
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest

from awos.executors.base import RunSpec
from awos.executors.claude import ClaudeExecutor, ClaudeParser
from awos.executors.codex import CodexExecutor, CodexParser, strictify
from awos.executors.proc import classify_error, extract_json, run_process
from awos.brain.schemas import EXECUTE_SCHEMA, TRIAGE_SCHEMA


def spec(tmp_path: Path, **kw) -> RunSpec:
    d = dict(run_id="run_t", prompt="hi", cwd=tmp_path, log_dir=tmp_path / "log", purpose="execute", sandbox="read-only")
    d.update(kw)
    (tmp_path / "log").mkdir(exist_ok=True)
    return RunSpec(**d)


def test_strictify_closes_objects_and_nullables():
    s = strictify(TRIAGE_SCHEMA)
    item = s["properties"]["items"]["items"]
    assert item["additionalProperties"] is False
    assert set(item["required"]) == set(item["properties"])
    assert "null" in item["properties"]["existing_task_id"]["type"]
    assert "null" in item["properties"]["project"]["type"]
    assert "minimum" not in item["properties"]["priority"]
    e = strictify(EXECUTE_SCHEMA)
    assert e["additionalProperties"] is False and "verification" in e["required"]


def test_codex_argv_blocks_connectors_by_default(tmp_path):
    argv = CodexExecutor().build_argv(spec(tmp_path), None)
    assert argv[-1] == "-" and "--ephemeral" in argv and "apps" in argv and "plugins" in argv
    assert "danger-full-access" not in argv and "--add-dir" not in argv
    assert argv[argv.index("-s") + 1] == "read-only"
    on = CodexExecutor().build_argv(spec(tmp_path, allow_connectors=True, sandbox="workspace-write"), None)
    assert "apps" not in on and on[on.index("-s") + 1] == "workspace-write"
    assert "browser_use" in on  # desktop automation is never available


def test_claude_argv_scopes_tools(tmp_path):
    ro = ClaudeExecutor().build_argv(spec(tmp_path, allow_network=False, output_schema={"type": "object"}))
    assert "dontAsk" in ro and "--json-schema" in ro
    assert "Bash" in ro[ro.index("--disallowedTools") + 1] and "WebSearch" in ro[ro.index("--disallowedTools") + 1]
    assert "Bash" not in ro[ro.index("--allowedTools") + 1].split()
    assert "disableAllHooks" in " ".join(ro) and "--bare" not in ro
    rw = ClaudeExecutor().build_argv(spec(tmp_path, sandbox="workspace-write", purpose="triage"))
    assert "--bare" in rw and "Bash" in rw[rw.index("--allowedTools") + 1].split()


def test_codex_parser_events():
    ev = []
    p = CodexParser(lambda k, n, d: ev.append((k, n)))
    for e in [{"type": "thread.started", "thread_id": "t1"},
              {"type": "item.started", "item": {"type": "command_execution", "command": "ls"}},
              {"type": "item.completed", "item": {"type": "command_execution", "command": "ls", "exit_code": 0, "aggregated_output": "a"}},
              {"type": "item.completed", "item": {"type": "agent_message", "text": "{\"a\":1}"}},
              {"type": "turn.completed", "usage": {"input_tokens": 5, "output_tokens": 2}}]:
        p.feed(json.dumps(e))
    assert ev == [("tool_call", "shell"), ("tool_result", "shell"), ("message", None), ("usage", None)]
    assert p.tokens_in == 5 and p.last_message == '{"a":1}' and p.thread_id == "t1"


def test_claude_parser_hides_structured_output_tool():
    ev = []
    p = ClaudeParser(lambda k, n, d: ev.append((k, n)))
    for e in [{"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "1", "name": "Read", "input": {"file_path": "a"}}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "1", "content": "x"}]}},
              {"type": "assistant", "message": {"content": [{"type": "tool_use", "id": "2", "name": "StructuredOutput", "input": {}}]}},
              {"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": "2", "content": "ok"}]}},
              {"type": "result", "subtype": "success", "total_cost_usd": 0.01, "permission_denials": [{"tool_name": "Write", "tool_input": {}}]}]:
        p.feed(json.dumps(e))
    assert ev == [("tool_call", "Read"), ("tool_result", "Read"), ("usage", None), ("error", "permission_denied")]


def test_classify_and_extract():
    assert classify_error("429 Too Many Requests") == "transient"
    assert classify_error("401 Unauthorized") == "permanent"
    assert classify_error("error_max_budget_usd") == "budget"
    assert classify_error("invalid_json_schema") == "permanent"
    assert extract_json('x ```json\n{"a": 1}\n``` y') == {"a": 1}


def test_run_process_kills_whole_tree_on_timeout(tmp_path):
    child = tmp_path / "child.py"
    child.write_text(textwrap.dedent("""
        import subprocess, sys, time, pathlib
        p = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
        pathlib.Path(sys.argv[1]).write_text(str(p.pid))
        time.sleep(120)
    """), encoding="utf-8")
    pidfile = tmp_path / "gc.pid"
    t0 = time.monotonic()
    pr = run_process([sys.executable, str(child), str(pidfile)], cwd=tmp_path, stdin_text=None, timeout_s=3, log_dir=tmp_path / "l")
    assert pr.timed_out and time.monotonic() - t0 < 30
    from awos.engine.recovery import pid_alive
    time.sleep(1)
    assert not pid_alive(int(pidfile.read_text()))


def test_run_process_cancel_pid_callback_and_utf8_stdin(tmp_path):
    echo = tmp_path / "echo.py"
    echo.write_text("import sys; sys.stdout.buffer.write(sys.stdin.buffer.read()); sys.stdout.flush()", encoding="utf-8")
    lines, pids = [], []
    pr = run_process([sys.executable, str(echo)], cwd=tmp_path, stdin_text="中文 prompt\n第二行", timeout_s=20,
                     on_stdout=lines.append, on_pid=pids.append, log_dir=tmp_path / "l")
    assert pr.exit_code == 0 and lines == ["中文 prompt", "第二行"] and pids
    sleeper = tmp_path / "s.py"
    sleeper.write_text("import time; time.sleep(60)", encoding="utf-8")
    cancel = threading.Event()
    threading.Timer(1.0, cancel.set).start()
    pr = run_process([sys.executable, str(sleeper)], cwd=tmp_path, stdin_text=None, timeout_s=60, cancel=cancel)
    assert pr.cancelled and not pr.timed_out


@pytest.mark.live
def test_live_codex_structured(tmp_path):
    ex = CodexExecutor()
    ok, why = ex.available()
    if not ok:
        pytest.skip(f"codex unavailable: {why}")
    s = spec(tmp_path, prompt="Answer with ok=true.", output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]})
    res = ex.run(s, lambda *a: None, threading.Event())
    assert res.status == "succeeded" and res.structured == {"ok": True}


@pytest.mark.live
def test_live_claude_denies_write_outside_cwd(tmp_path):
    ex = ClaudeExecutor()
    ok, why = ex.available()
    if not ok:
        pytest.skip(f"claude unavailable: {why}")
    ws = tmp_path / "ws"
    ws.mkdir()
    events = []
    s = spec(ws, prompt="Create ../outside.txt containing BAD and ./inside.txt containing OK, then report booleans.", sandbox="workspace-write",
             model="haiku", max_budget_usd=0.3, output_schema={"type": "object", "properties": {"inside": {"type": "boolean"}, "outside": {"type": "boolean"}},
                                                               "required": ["inside", "outside"]})
    res = ex.run(s, lambda k, n, d: events.append((k, n)), threading.Event())
    assert res.status == "succeeded" and not (tmp_path / "outside.txt").exists() and (ws / "inside.txt").exists()
