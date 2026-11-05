"""Board / dashboard aggregations, card digests (headline, gist, keywords) and the unified inbox feed."""
from __future__ import annotations

import json

import pytest

from tests.test_api import H, TOKEN, client, env, _approval, _task  # noqa: F401  (fixtures)


def _digest_task(title="整理仓库", **kw) -> str:
    from awos import repo
    tid = repo.create_task(title, **kw)
    return tid


def test_digest_fallback_and_stored_digest_are_attached(client):
    from awos import repo
    a = _task("检查 thuthesis 仓库未提交改动并决定提交", status="triaged", category="pm", level=2, project="thuthesis")
    b = _task("回复 JournalA 作者授权邮件", status="triaged", category="comms", level=3)
    repo.set_digest("task", b, "回复 JournalA 论文作者授权邮件", ["JournalA", "作者确认", "逾期"], gist="王工已代为确认", source="llm")
    rows = {t["id"]: t for t in client.get("/api/tasks?status=triaged").json()["tasks"]}
    assert rows[b]["headline"] == "回复 JournalA 论文作者授权邮件" and rows[b]["keywords"] == ["JournalA", "作者确认", "逾期"]
    assert rows[b]["digest_auto"] is False and rows[b]["gist"] == "王工已代为确认"
    assert rows[a]["digest_auto"] is True and rows[a]["headline"] and "thuthesis" in rows[a]["keywords"] and "L2" in rows[a]["keywords"]


def test_board_columns_and_groups(client):
    ids = {s: _task(f"task-{s}", status=s, priority=1 if s == "running" else 2) for s in
           ("triaged", "ready", "blocked", "running", "waiting_approval", "review", "failed", "dismissed")}
    kid = _task("child", status="running", parent_id=ids["blocked"])
    done = _task("old done", status="done")
    from awos import repo
    repo.update_task(done, priority=3)
    body = client.get("/api/board").json()
    cols = {c["key"]: c for c in body["columns"]}
    assert [c["key"] for c in body["columns"]] == ["suggested", "queued", "running", "needs_you", "done", "failed", "dismissed"]
    got = {k: {i["title"] for i in c["items"]} for k, c in cols.items()}
    assert "task-triaged" in got["suggested"] and "task-ready" in got["queued"]
    assert {"task-running", "task-blocked"} <= got["running"]      # a parent with a running child counts as in progress
    assert {"task-waiting_approval", "task-review"} <= got["needs_you"]
    assert "task-failed" in got["failed"] and "task-dismissed" in got["dismissed"] and "old done" in got["done"]
    assert "child" not in {i["title"] for c in cols.values() for i in c["items"]}   # roots only
    blocked = next(i for i in cols["running"]["items"] if i["title"] == "task-blocked")
    assert blocked["children"] == {"total": 1, "done": 0, "running": 1}
    assert body["totals"]["open"] >= 5 and all("headline" in i and "keywords" in i for c in cols.values() for i in c["items"])


def test_dashboard_shape(client):
    from awos import repo
    t = _task("x", status="triaged", level=3, category="dev")
    _task("y", status="running", level=5, category="writing")
    repo.add_signal("git", "s1", "fp-1")
    repo.record_cost(None, "claude", "sonnet", 0.5, 10, 5)
    _approval(task_id=t)
    repo.set_digest("task", t, "做点事", ["Git", "论文", "L3"], source="llm")
    d = client.get("/api/dashboard?days=7").json()
    assert set(d) >= {"kpis", "series", "levels", "categories", "funnel", "executors", "sensors", "keywords", "activity"}
    assert len(d["series"]["days"]) == 7 and all(len(d["series"][k]) == 7 for k in ("tasks_created", "tasks_done", "signals", "runs", "cost"))
    assert d["kpis"]["approvals"] == 1 and d["kpis"]["running"] == 1 and d["kpis"]["inbox"] == 1
    assert d["levels"]["3"] == 1 and d["levels"]["5"] == 1 and sum(d["series"]["tasks_created"]) == 2
    assert d["series"]["cost"][-1]["claude"] == 0.5 and d["series"]["cost"][-1]["total"] == 0.5
    assert d["funnel"]["signals"] == 1 and d["funnel"]["tasks"] == 2
    assert {"word": "Git", "count": 1} in d["keywords"] and not any(k["word"] == "L3" for k in d["keywords"])
    assert any(a["type"] == "task.created" for a in d["activity"])


def test_inbox_feed_is_flat_and_urgency_ordered(client):
    from awos import repo
    t1 = _task("待验收", status="review", priority=1)
    t2 = _task("新建议", status="triaged", priority=0)
    t3 = _task("失败的", status="failed")
    _approval(task_id=t2, kind="decision", title="选哪个")
    _approval(task_id=t2, kind="action", title="发邮件？", action_type="send_email", risk="high")
    feed = client.get("/api/inbox").json()["feed"]
    assert [f["section"] for f in feed] == ["decision", "approval", "review", "suggested", "failed"]
    assert feed[1]["risk"] == "high" and feed[1]["kind"] == "approval" and feed[0]["task_id"] == t2
    assert all(f["headline"] and isinstance(f["keywords"], list) and f["section_title"] for f in feed)
    assert {f["id"] for f in feed if f["kind"] == "task"} == {t1, t2, t3}


def test_ensure_digests_writes_and_refreshes_only_when_text_changes(client, monkeypatch):
    from awos import repo
    from awos.brain import summarize
    from awos.executors.base import RunResult

    t = _task("整理 thuthesis 仓库改动", status="triaged", category="pm", level=2,
              description="69 个未提交文件，需要判断哪些是正式版本")
    a = _approval(task_id=t, title="先怎么处理？")
    calls = []

    def fake_run_agent(prompt, **kw):
        calls.append(kw["purpose"])
        ids = [x.split("id=")[1].split()[0] for x in prompt.splitlines() if x.startswith("[")]
        return "run_x", RunResult(status="succeeded", structured={"items": [
            {"id": i, "headline": f"摘要-{i[-4:]}", "gist": "一句话", "keywords": ["仓库", "论文", "Git"]} for i in ids]})

    import awos.llm as llm
    monkeypatch.setattr(llm, "run_agent", fake_run_agent)
    r = summarize.ensure_digests()
    assert r["written"] == 2 and calls == ["digest"]
    assert repo.get_digests("task", [t])[t]["keywords"] == ["仓库", "论文", "Git"] and repo.get_digests("approval", [a])[a]["gist"] == "一句话"
    assert summarize.ensure_digests() == {"pending": 0, "written": 0}          # unchanged text -> no new call
    repo.update_task(t, summary="已分类：正式版本 40 个，中间产物 29 个")
    assert summarize.ensure_digests()["written"] == 1                          # changed text -> refreshed once
    repo.set_digest("task", t, "我自己写的标题", ["手写"], source="user")
    repo.update_task(t, summary="再变一次")
    assert summarize.ensure_digests()["pending"] == 0                          # user-written digests are never overwritten


def test_evidence_and_mail_endpoints_need_the_token(client, monkeypatch):
    from awos import mailstore, repo
    sid = repo.add_signal("git", "仓库有 69 个未提交改动", "fp-ev", body="最早已 194 天\n涉及 data/chap02.tex")
    tid = repo.create_task("整理仓库", status="triaged", source="sensor:git", source_signal_id=sid, meta={"signals": [sid]})
    assert client.get(f"/api/tasks/{tid}/evidence").status_code == 401           # raw source text: token even for GET
    assert client.get("/api/mail/read?ref=mail:abc:1").status_code == 401
    body = client.get(f"/api/tasks/{tid}/evidence", headers=H).json()
    assert body["mail_count"] == 0 and body["items"][0]["type"] == "signal" and "data/chap02.tex" in body["items"][0]["body"]

    mails = {"mail:aaaaaaaaaaaa:2": dict(ref="mail:aaaaaaaaaaaa:2", subject="Re: URGENT", received_ms=2.0),
             "mail:aaaaaaaaaaaa:1": dict(ref="mail:aaaaaaaaaaaa:1", subject="URGENT: approval", received_ms=1.0)}
    for m in mails.values():
        m.update({"from": "a <a@x.org>", "to": ["me"], "cc": [], "received": "2026-11-04 14:39", "body": "正文" * 3,
                  "truncated": False, "body_available": True, "has_attachments": False, "account_key": "aaaaaaaaaaaa", "local_id": 1})
    monkeypatch.setattr(mailstore, "refs_for_task", lambda t: ["mail:aaaaaaaaaaaa:2"])
    monkeypatch.setattr(mailstore, "thread_refs", lambda ref, **kw: ["mail:aaaaaaaaaaaa:1"])
    monkeypatch.setattr(mailstore, "read_mail", lambda ref, max_chars=0: mails[ref])
    ev = client.get(f"/api/tasks/{tid}/evidence", headers=H).json()
    got = [i for i in ev["items"] if i["type"] == "mail"]
    assert [m["subject"] for m in got] == ["URGENT: approval", "Re: URGENT"]          # thread, oldest first
    assert [m["is_source"] for m in got] == [False, True] and ev["mail_count"] == 2
    assert client.get("/api/mail/read?ref=mail:aaaaaaaaaaaa:2", headers=H).json()["subject"] == "Re: URGENT"
    assert client.get("/api/tasks/nope/evidence", headers=H).status_code == 404


def test_meta_timezone_and_failure_series(client):
    from awos import repo
    rid = repo.create_run("execute", "codex")
    repo.update_run(rid, status="failed", started_at=repo.utcnow())
    assert client.get("/api/meta").json()["timezone"] == "Asia/Shanghai"
    d = client.get("/api/dashboard?days=5").json()
    assert len(d["series"]["failures"]) == 5 and sum(d["series"]["failures"]) == 1


def test_mail_people_parsing_handles_all_stored_shapes():
    from awos.mailstore import _people
    one = '{"mail":"a@x.org","name":"Amy"}'
    many = '{"mail":"a@x.org","name":"Amy"},{"mail":"b@x.org","name":"b@x.org"},{"mail":"c@x.org","name":""}'
    assert _people(one) == ["Amy <a@x.org>"]
    assert _people(many) == ["Amy <a@x.org>", "b@x.org", "c@x.org"]      # unbracketed list, name==mail and empty name
    assert _people('[{"mail":"a@x.org","name":"Amy"}]') == ["Amy <a@x.org>"]
    assert _people(b'{"mail":"a@x.org","name":"Amy"}') == ["Amy <a@x.org>"] and _people(None) == [] and _people("plain text") == ["plain text"]


def test_mail_people_parsing_handles_double_encoded_arrays():
    import json
    from awos.mailstore import _people
    raw = json.dumps([json.dumps({"mail": "a@x.org", "name": "王工 Amy"}, ensure_ascii=False), json.dumps({"mail": "b@x.org", "name": ""})],
                     ensure_ascii=False)
    assert _people(raw) == ["王工 Amy <a@x.org>", "b@x.org"]
