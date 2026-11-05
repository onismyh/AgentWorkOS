"""Unit tests for the Pi / DSH event parsers and JSON extraction (no processes, no model calls)."""
from __future__ import annotations

import json

from awos.executors.dsh import DshParser
from awos.executors.noderuntime import extract_json, schema_prompt
from awos.executors.pi import PiParser


def _sink(store):
    return lambda kind, name, data: store.append((kind, name))


def test_pi_parser_success_and_usage():
    ev = []
    p = PiParser(_sink(ev))
    lines = [
        {"type": "session", "version": 3, "id": "s1", "cwd": "x"},
        {"type": "tool_execution_start", "toolCallId": "c1", "toolName": "read", "args": {"path": "a"}},
        {"type": "tool_execution_end", "toolCallId": "c1", "toolName": "read", "result": "ok", "isError": False},
        {"type": "message_end", "message": {"role": "assistant", "stopReason": "stop", "model": "m",
                                             "content": [{"type": "text", "text": "```json\n{\"a\": 1}\n```"}],
                                             "usage": {"input": 10, "output": 5, "cost": {"total": 0.002}}}},
        {"type": "agent_settled", "aborted": False},
    ]
    for line in lines:
        p.feed(json.dumps(line) + "\r\n")
    assert p.succeeded() and p.session_id == "s1"
    assert extract_json(p.final_text) == {"a": 1}
    assert ("tool_call", "read") in ev and ("tool_result", "read") in ev
    assert p.tokens_in == 10 and abs(p.cost - 0.002) < 1e-9


def test_pi_parser_detects_error_stop():
    p = PiParser(_sink([]))
    p.feed(json.dumps({"type": "message_end", "message": {"role": "assistant", "stopReason": "error", "errorMessage": "401",
                                                          "content": []}}))
    p.feed(json.dumps({"type": "agent_settled", "aborted": False}))
    assert not p.succeeded() and p.error == "401"


def test_dsh_parser_final_completed():
    ev = []
    p = DshParser(_sink(ev))
    for line in [{"type": "session", "sessionId": "d1", "cwd": "x"},
                 {"type": "tool_call", "callId": "1", "tool": "shell", "input": {"cmd": "ls"}},
                 {"type": "tool_result", "callId": "1", "status": "completed", "result": "a"},
                 {"type": "text", "text": "partial"},
                 {"type": "final", "sessionId": "d1", "reason": {"kind": "completed"}, "text": "{\"ok\": true}"}]:
        p.feed(json.dumps(line))
    assert p.succeeded(0) and not p.succeeded(1)
    assert extract_json(p.text) == {"ok": True}
    assert ("tool_call", "shell") in ev


def test_extract_json_variants():
    assert extract_json('noise {"x": [1, 2]} trailing') == {"x": [1, 2]}
    assert extract_json("```json\n{\"a\":1}\n```\n```json\n{\"b\":2}\n```") == {"b": 2}
    assert extract_json("no json here") is None
    assert "JSON Schema" in schema_prompt("do it", {"type": "object"})
