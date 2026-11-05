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
