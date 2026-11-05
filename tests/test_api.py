"""HTTP API tests (FastAPI TestClient) against an isolated temp var/ + SQLite DB."""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

TOKEN = "test-token-123"


@pytest.fixture()
def env(tmp_path, monkeypatch):
    var = tmp_path / "var"
    cfg = tmp_path / "test.toml"
    cfg.write_text(f"""
[server]
host = "127.0.0.1"
port = 8765
[paths]
var = "{var.as_posix()}"
workspaces = "{(var / 'workspaces').as_posix()}"
artifacts = "{(var / 'artifacts').as_posix()}"
logs = "{(var / 'logs').as_posix()}"
runs = "{(var / 'runs').as_posix()}"
backups = "{(var / 'backups').as_posix()}"
context = "{(var / 'context').as_posix()}"
inbox_drop = "{(var / 'inbox-drop').as_posix()}"
[executors.codex]
enabled = false
[executors.claude]
enabled = false
[notify]
digest_times = []
""" + "".join(f"[sensors.{n}]\nenabled = false\n" for n in ("git", "files", "agent_history", "zotero", "inbox_drop",
                                                              "gmail", "notion", "mailmaster")), encoding="utf-8")
    # env first, then (re)load awos config so nothing touches the real var/ or DB
    monkeypatch.setenv("AWOS_CONFIG", str(cfg))
    monkeypatch.setenv("AWOS_DB", str(tmp_path / "awos.db"))
    monkeypatch.setenv("AWOS_NOTIFY_DISABLE", "1")
    from awos.config import reset_config_cache
    reset_config_cache()
    import awos.api.app as app_mod
    app_mod._exec_cache.update(at=0.0, data=None)
    yield {"tmp": tmp_path, "var": var, "app_mod": app_mod}
    reset_config_cache()


@pytest.fixture()
def client(env):
    from fastapi.testclient import TestClient
    with TestClient(env["app_mod"].create_app(token=TOKEN)) as c:
        yield c


H = {"X-AWOS-Token": TOKEN}


def _task(title="t", **kw) -> str:
    from awos import repo
    return repo.create_task(title, **kw)


def _approval(task_id=None, kind="decision", title="选哪个方案？", **kw) -> str:
    from awos import repo
    return repo.create_approval(title=title, kind=kind, task_id=task_id,
                                options=[{"key": "approve", "label": "批准"}, {"key": "reject", "label": "拒绝"}], **kw)


def _raw_artifact(title: str, path) -> str:
    from awos import db
    aid = db.new_id("art")
    db.insert("artifacts", {"id": aid, "kind": "file", "title": title, "path": str(path), "created_at": db.utcnow()})
    return aid


# --------------------------------------------------------------------------- security


def test_token_required_on_mutations(client):
    assert client.post("/api/tasks", json={"title": "x"}).status_code == 401
    assert client.post("/api/tasks", json={"title": "x"}, headers={"X-AWOS-Token": "wrong"}).status_code == 401
    assert client.patch("/api/tasks/tsk_x", json={"priority": 1}).status_code == 401
    assert client.delete("/api/context/ctx_x").status_code == 401
    assert client.post("/api/approvals/apv_x/decide", json={"option": "approve"}).status_code == 401
    assert client.post("/api/runs/run_x/cancel").status_code == 401
    # reads are open (still Host-guarded); a valid token passes
    assert client.get("/api/tasks").status_code == 200
    assert client.post("/api/tasks", json={"title": "x"}, headers=H).status_code == 201


def test_host_header_guard(client):
    assert client.get("/api/health", headers={"Host": "evil.example:8765"}).status_code == 403
    assert client.get("/api/health", headers={"Host": "127.0.0.1:9999"}).status_code == 403
    assert client.get("/", headers={"Host": "attacker.test"}).status_code == 403
    for host in ("127.0.0.1:8765", "localhost:8765", "testserver"):
        assert client.get("/api/health", headers={"Host": host}).status_code == 200


def test_api_token_file_created(env):
    from awos.config import load_config
    tok = env["app_mod"].load_api_token()
    assert len(tok) >= 32
    assert (load_config().path("var") / "api_token").read_text(encoding="utf-8").strip() == tok
    assert env["app_mod"].load_api_token() == tok  # stable across restarts


def test_index_injects_token_and_static(client):
    r = client.get("/")
    assert r.status_code == 200
    assert f'<meta name="awos-token" content="{TOKEN}">' in r.text
    assert "__AWOS_TOKEN__" not in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]
    js = client.get("/static/app.js")
    assert js.status_code == 200 and "javascript" in js.headers["content-type"]
    assert client.get("/static/style.css").headers["content-type"].startswith("text/css")


# --------------------------------------------------------------------------- tasks


def test_task_create_accept_cancel_flow(client):
    r = client.post("/api/tasks", json={"title": "整理开题报告", "description": "见 D:/x", "priority": 1}, headers=H)
    assert r.status_code == 201
    tid = r.json()["id"]
    assert r.json()["task"]["status"] == "inbox"

    r = client.post(f"/api/tasks/{tid}/actions/accept", json={"note": "先做第一章"}, headers=H)
    assert r.status_code == 200 and r.json()["task"]["status"] == "ready"
    r = client.post(f"/api/tasks/{tid}/actions/cancel", json={"note": "不需要了"}, headers=H)
    assert r.status_code == 200 and r.json()["task"]["status"] == "cancelled"

    d = client.get(f"/api/tasks/{tid}").json()
    assert d["task"]["status"] == "cancelled" and d["task"]["status_reason"] == "不需要了"
    assert [n["text"] for n in d["notes"]] == ["先做第一章"]
    transitions = [(e["data"]["from"], e["data"]["to"]) for e in d["events"] if e["type"] == "status_changed"]
    assert ("inbox", "ready") in transitions and ("ready", "cancelled") in transitions

    listed = client.get("/api/tasks", params={"status": "cancelled,done", "search": "开题"}).json()
    assert [t["id"] for t in listed["tasks"]] == [tid]


def test_task_action_errors(client):
    tid = _task("noise")
    assert client.post(f"/api/tasks/{tid}/actions/dismiss", headers=H).status_code == 200
    r = client.post(f"/api/tasks/{tid}/actions/accept", headers=H)  # dismissed -> ready is not a legal transition
    assert r.status_code == 409 and "not allowed" in r.json()["detail"]
    assert client.post(f"/api/tasks/{tid}/actions/explode", headers=H).status_code == 404
    assert client.post("/api/tasks/tsk_missing/actions/accept", headers=H).status_code == 404
    assert client.post(f"/api/tasks/{tid}/actions/rework", json={}, headers=H).status_code == 422
    assert client.get("/api/tasks/tsk_missing").status_code == 404


def test_task_snooze_complete_rework(client):
    from awos import repo
    tid = _task("draft", status="review", level=3)
    r = client.post(f"/api/tasks/{tid}/actions/rework", json={"feedback": "第二节太长"}, headers=H)
    assert r.json()["task"]["status"] == "ready"
    assert "第二节太长" in repo.get_task(tid)["meta"]["notes"][-1]["text"]
    assert client.post(f"/api/tasks/{tid}/actions/snooze", json={"hours": 2}, headers=H).json()["task"]["status"] == "snoozed"
    assert repo.get_task(tid)["snoozed_until"]
    assert client.post(f"/api/tasks/{tid}/actions/complete", headers=H).json()["task"]["status"] == "done"


def test_patch_task_and_validation(client):
    from awos import repo
    tid = _task("p")
    r = client.patch(f"/api/tasks/{tid}", json={"level": 4, "priority": 0, "category": "dev", "title": "p2"}, headers=H)
    assert r.status_code == 200
    t = repo.get_task(tid)
    assert (t["level"], t["priority"], t["category"], t["title"]) == (4, 0, "dev", "p2")
    assert t["meta"]["level_locked"] is True
    assert client.patch(f"/api/tasks/{tid}", json={"category": "nope"}, headers=H).status_code == 422
    assert client.patch(f"/api/tasks/{tid}", json={"level": 9}, headers=H).status_code == 422
    assert client.patch(f"/api/tasks/{tid}", json={}, headers=H).status_code == 422
    assert client.post("/api/tasks", json={"title": "x", "kind": "weird"}, headers=H).status_code == 422


def test_task_detail_tree_and_deps(client):
    from awos import repo
    root = _task("root", status="blocked")
    a = _task("child a", parent_id=root, status="done")
    b = _task("child b", parent_id=root, status="ready")
    leaf = _task("leaf", parent_id=b)
    repo.add_dependency(b, a)
    d = client.get(f"/api/tasks/{root}").json()
    assert [c["id"] for c in d["children"]] == [a, b]
    assert d["children"][1]["deps"] == [{"id": a, "title": "child a", "status": "done"}]
    assert d["children"][1]["children"][0]["id"] == leaf
    assert d["children_progress"] == {"total": 3, "done": 1}
    listed = {t["id"]: t for t in client.get("/api/tasks", params={"root_only": 1}).json()["tasks"]}
    assert set(listed) == {root} and listed[root]["children_total"] == 2 and listed[root]["children_done"] == 1
    assert client.get(f"/api/tasks/{leaf}").json()["root"]["id"] == root


# --------------------------------------------------------------------------- inbox / approvals


def test_inbox_buckets(client):
    from awos import repo
    review = _task("review me", status="review")
    handoff = _task("handoff", status="waiting_user")
    suggested = _task("suggested", status="triaged")
    failed = _task("failed", status="failed")
    _task("running", status="running")
    rid = repo.create_run("execute", "codex", task_id=failed)
    repo.update_run(rid, status="failed", error="boom: rate limited", ended_at="2026-01-01T00:00:00Z")
    dec = _approval(review, kind="decision")
    act = _approval(review, kind="action", action_type="send_email", risk="high")
    repo.add_artifact("report", path=None, task_id=review)

    d = client.get("/api/inbox").json()
    ids = {k: [x["id"] for x in d[k]] for k in ("decisions", "actions", "review", "handoff", "suggested", "failed")}
    assert ids == {"decisions": [dec], "actions": [act], "review": [review], "handoff": [handoff],
                   "suggested": [suggested], "failed": [failed]}
    assert d["failed"][0]["last_error"]["error"] == "boom: rate limited"
    assert d["decisions"][0]["task_title"] == "review me"
    assert [a["title"] for a in d["review"][0]["artifacts"]] == ["report"]
    assert d["counts"]["review"] == 1
    o = client.get("/api/overview").json()
    assert o["inbox"]["total"] == 6 and o["approvals_pending"] == 2 and o["inbox"]["actions"] == 1


def test_approval_decide_once(client):
    from awos import repo
    tid = _task("needs decision", status="waiting_approval", level=2)
    aid = _approval(tid, context_md="**背景**", recommendation="approve：更稳妥")
    assert client.get(f"/api/approvals/{aid}").json()["approval"]["status"] == "pending"
    r = client.post(f"/api/approvals/{aid}/decide", json={"option": "approve", "note": "就这样"}, headers=H)
    assert r.status_code == 200
    a = repo.get_approval(aid)
    assert a["decision"] == "approve" and a["decided_via"] == "dashboard" and a["decision_note"] == "就这样"
    r2 = client.post(f"/api/approvals/{aid}/decide", json={"option": "reject"}, headers=H)
    assert r2.status_code == 409 and "not pending" in r2.json()["detail"]
    other = _approval(tid)
    assert client.post(f"/api/approvals/{other}/decide", json={"option": "maybe"}, headers=H).status_code == 409
    assert client.post("/api/approvals/apv_missing/decide", json={"option": "approve"}, headers=H).status_code == 404
    pending = client.get("/api/approvals", params={"status": "pending"}).json()["approvals"]
    assert [x["id"] for x in pending] == [other]


def test_signed_approval_link(client, env):
    from awos import repo
    from awos.hitl import tokens
    aid = _approval(_task("t"), title="<script>alert(1)</script> 发邮件？", context_md="<img src=x onerror=alert(1)>")
    tok = tokens.sign(aid)
    page = client.get(f"/approve/{tok}")
    assert page.status_code == 200
    assert "<script>alert(1)" not in page.text and "&lt;script&gt;" in page.text and "<img src=x" not in page.text
    assert repo.get_approval(aid)["status"] == "pending"  # GET never decides
    r = client.post(f"/approve/{tok}", content="option=approve&note=%E5%A5%BD", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert r.status_code == 200
    a = repo.get_approval(aid)
    assert (a["decision"], a["decided_via"], a["decision_note"]) == ("approve", "link", "好")
    replay = client.post(f"/approve/{tok}", content="option=reject", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert replay.status_code in (403, 409) and repo.get_approval(aid)["decision"] == "approve"
    assert client.get("/approve/not-a-token").status_code == 403

    bound_aid = _approval(_task("t2"))
    bound = tokens.sign(bound_aid, option="reject")
    wrong = client.post(f"/approve/{bound}", content="option=approve", headers={"Content-Type": "application/x-www-form-urlencoded"})
    assert wrong.status_code == 403 and repo.get_approval(bound_aid)["status"] == "pending"


def test_approval_link_module_missing(client, env, monkeypatch):
    monkeypatch.setattr(env["app_mod"], "_tokens_module", lambda: None)
    assert client.get("/approve/whatever").status_code == 503


# --------------------------------------------------------------------------- runs / artifacts


def test_run_endpoints(client, env):
    from awos import repo
    from awos.config import load_config
    tid = _task("run me", status="running")
    rid = repo.create_run("execute", "codex", task_id=tid)
    log_dir = load_config().path("runs") / rid
    log_dir.mkdir(parents=True)
    (log_dir / "stdout.jsonl").write_text("\n".join(json.dumps({"n": i}) for i in range(10)) + "\n", encoding="utf-8")
    (log_dir / "stderr.log").write_text("warn 1\nwarn 2\n", encoding="utf-8")
    (log_dir / "prompt.md").write_text("# do it", encoding="utf-8")
    repo.update_run(rid, status="succeeded", started_at="2026-01-01T00:00:00Z", ended_at="2026-01-01T00:01:30Z", log_path=str(log_dir))
    repo.add_run_event(rid, "message", None, {"text": "hello"})
    repo.add_run_event(rid, "tool_call", "shell", {"command": "git status"})
    repo.add_run_event(rid, "tool_result", "shell", {"output": "clean"})

    runs = client.get("/api/runs", params={"task_id": tid, "purpose": "execute"}).json()["runs"]
    assert [r["id"] for r in runs] == [rid] and runs[0]["duration_s"] == 90 and runs[0]["task_title"] == "run me"
    assert client.get("/api/runs", params={"purpose": "triage"}).json()["runs"] == []
    det = client.get(f"/api/runs/{rid}").json()
    assert det["event_counts"] == {"message": 1, "tool_call": 1, "tool_result": 1} and det["cancellable"] is False
    evs = client.get(f"/api/runs/{rid}/events").json()
    first = evs["events"][0]["id"]
    after = client.get(f"/api/runs/{rid}/events", params={"after": first}).json()
    assert [e["kind"] for e in after["events"]] == ["tool_call", "tool_result"] and after["run_status"] == "succeeded"
    assert [e["kind"] for e in client.get(f"/api/runs/{rid}/events", params={"kind": "tool_call"}).json()["events"]] == ["tool_call"]
    lg = client.get(f"/api/runs/{rid}/log", params={"tail": 2}).json()
    assert lg["stdout"]["lines"] == ['{"n": 8}', '{"n": 9}'] and lg["stdout"]["truncated"] is True
    assert lg["stderr"]["lines"] == ["warn 1", "warn 2"] and lg["prompt"] == "# do it"
    assert client.post(f"/api/runs/{rid}/cancel", headers=H).status_code == 409
    calls = client.get("/api/tool-calls").json()["tool_calls"]
    assert [c["name"] for c in calls] == ["shell"] and calls[0]["task_title"] == "run me"


def test_run_log_outside_var_blocked(client, env):
    from awos import repo
    outside = env["tmp"] / "elsewhere"
    outside.mkdir()
    (outside / "stdout.jsonl").write_text("secret", encoding="utf-8")
    rid = repo.create_run("execute", "codex")
    repo.update_run(rid, log_path=str(outside))
    assert client.get(f"/api/runs/{rid}/log").status_code == 403


def test_artifact_content_path_traversal(client, env):
    from awos import repo
    from awos.config import load_config
    arts = load_config().path("artifacts")
    good = arts / "report.md"
    good.write_bytes("# 报告\n内容".encode("utf-8"))
    secret = env["tmp"] / "secret.txt"
    secret.write_text("TOP SECRET", encoding="utf-8")
    ok = repo.add_artifact("report", path=good)
    # repo.add_artifact refuses paths outside var/; the API must still guard legacy / tampered rows
    outside = _raw_artifact("secret", secret)
    sneaky = _raw_artifact("sneaky", str(arts / ".." / ".." / "secret.txt"))
    missing = repo.add_artifact("missing", path=arts / "nope.md")
    link_only = repo.add_artifact("link", kind="link", uri="https://example.com")

    r = client.get(f"/api/artifacts/{ok}/content")
    assert r.status_code == 200 and r.text == "# 报告\n内容"
    assert r.headers["content-type"].startswith("text/markdown")
    assert r.headers["content-security-policy"].startswith("sandbox")
    for aid in (outside, sneaky):
        r = client.get(f"/api/artifacts/{aid}/content")
        assert r.status_code == 403 and "TOP SECRET" not in r.text
    assert client.get(f"/api/artifacts/{missing}/content").status_code == 404
    assert client.get(f"/api/artifacts/{link_only}/content").status_code == 404
    assert client.get("/api/artifacts/art_nope/content").status_code == 404
    dl = client.get(f"/api/artifacts/{ok}/content", params={"download": 1})
    assert dl.status_code == 200 and "attachment" in dl.headers["content-disposition"]
    listed = {a["id"]: a for a in client.get("/api/artifacts").json()["artifacts"]}
    assert listed[ok]["previewable"] and not listed[outside]["servable"] and not listed[missing]["exists"]


# --------------------------------------------------------------------------- stream / history / misc


def test_sse_stream_open_close(client):
    from awos.bus import bus
    before = len(bus._subs)
    with client.stream("GET", "/api/stream") as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        chunks = r.iter_text()
        assert "retry:" in next(chunks)
        bus.publish({"entity_type": "task", "entity_id": "tsk_1", "type": "created", "actor": "user"})
        msg = next(chunks)
        assert msg.startswith("data: ") and json.loads(msg[6:].strip())["entity_id"] == "tsk_1"
        assert len(bus._subs) == before + 1
    deadline = time.time() + 5
    while len(bus._subs) != before and time.time() < deadline:
        time.sleep(0.05)
    assert len(bus._subs) == before  # unsubscribed on disconnect


def test_events_pagination(client):
    from awos import repo
    tid = _task("history")
    for i in range(7):
        repo.log_event("task", tid, "updated", {"i": i}, actor="user")
    seen, before, pages = [], None, 0
    while True:
        params = {"entity_type": "task", "entity_id": tid, "limit": 3}
        if before:
            params["before"] = before
        d = client.get("/api/events", params=params).json()
        pages += 1
        seen += [e["id"] for e in d["events"]]
        assert all(e["entity_title"] == "history" for e in d["events"])
        if not d["has_more"]:
            assert d["next_before"] is None
            break
        before = d["next_before"]
    assert pages == 3 and len(seen) == 8 and seen == sorted(seen, reverse=True) and len(set(seen)) == 8
    only_user = client.get("/api/events", params={"actor": "user", "entity_id": tid}).json()["events"]
    assert len(only_user) == 7


def test_context_crud(client, env):
    from awos.config import load_config
    (load_config().path("context") / "profile.md").write_bytes("# 我\n博士".encode("utf-8"))
    r = client.post("/api/context", json={"kind": "project", "key": "论文", "value": "第三章实验"}, headers=H)
    assert r.status_code == 201
    fid = r.json()["id"]
    assert r.json()["fact"]["pinned"] == 1
    d = client.get("/api/context").json()
    assert [f["id"] for f in d["groups"]["project"]] == [fid] and d["profile"]["content"] == "# 我\n博士"
    from awos import repo
    assert repo.upsert_fact("project", "论文", "agent rewrite", actor="agent:codex") is None  # pinned: agents can't overwrite
    r = client.post("/api/context", json={"kind": "project", "key": "论文", "value": "第四章"}, headers=H)  # user edit wins
    assert r.json()["id"] == fid and r.json()["fact"]["value"] == "第四章" and r.json()["fact"]["pinned"] == 1
    assert client.delete(f"/api/context/{fid}", headers=H).status_code == 200
    assert "project" not in client.get("/api/context").json()["groups"]
    assert client.get("/api/context", params={"status": "all"}).json()["groups"]["project"][0]["status"] == "rejected"
    assert client.delete("/api/context/ctx_nope", headers=H).status_code == 404
    assert client.post("/api/context", json={"kind": "Bad Kind!", "key": "k", "value": "v"}, headers=H).status_code == 422


def test_health_overview_meta_errors_sensors(client):
    from awos import repo
    from awos.db import utcnow
    repo.kv_set("daemon.heartbeat", {"ts": utcnow(), "pid": 1})
    repo.record_cost(None, "claude", "sonnet", 0.25, 100, 50)
    hl = client.get("/api/health").json()
    assert hl["daemon"]["alive"] is True and hl["executors"]["codex"]["ok"] is False
    assert hl["budget"]["today_usd"] == 0.25 and "tasks" in hl
    o = client.get("/api/overview").json()
    assert len(o["cost"]["days"]) == 14 and o["cost"]["days"][-1]["by_executor"]["claude"]["usd"] == 0.25
    m = client.get("/api/meta").json()
    assert set(m["levels"]) == {"1", "2", "3", "4", "5"} and "dev" in m["categories"]
    rid = repo.create_run("triage", "claude")
    repo.update_run(rid, status="timeout", error="took too long", ended_at=utcnow())
    errs = client.get("/api/errors").json()
    assert [r["id"] for r in errs["runs"]] == [rid]
    assert isinstance(client.get("/api/sensors").json()["sensors"], list)
    assert client.post("/api/sensors/no_such_sensor/run", headers=H).status_code == 404
    assert client.get("/api/signals").json() == {"signals": []}
    assert client.get("/api/notifications").json() == {"notifications": []}
