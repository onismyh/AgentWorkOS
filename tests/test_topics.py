"""Topics (grouping + deterministic history + AI brief) and coalesced reminders."""
from __future__ import annotations

import time

import pytest

from tests.test_api import H, client, env, _approval, _task  # noqa: F401  (fixtures)


def _fake_agent(monkeypatch, handler):
    from awos.executors.base import RunResult
    import awos.llm as llm
    calls = []

    def run_agent(prompt, **kw):
        calls.append(kw["purpose"])
        return "run_x", RunResult(status="succeeded", structured=handler(prompt, kw))
    monkeypatch.setattr(llm, "run_agent", run_agent)
    return calls


def _row(tid):
    from awos import repo
    return repo.get_task(tid)


def test_fallback_topics_group_by_project_then_category(client):
    from awos.brain import topics
    a = _task("检查仓库", project="thuthesis", status="triaged")
    b = _task("再看仓库", project="thuthesis", status="triaged")
    c = _task("回复邮件", category="comms", status="triaged")
    child = _task("子任务", parent_id=a, status="ready")
    r = topics.resolve([_row(x) for x in (a, b, c, child)])
    assert r[a] == r[b] == r[child] and r[a][1] == "thuthesis" and r[c][0] == "c:comms"


def test_clustering_assigns_reuses_keys_and_survives_bad_keys(client, monkeypatch):
    from awos import repo
    from awos.brain import topics
    t1 = _task("整理 thuthesis 改动", project="thuthesis", status="triaged")
    t2 = _task("电厂改造和 thuthesis 分批提交", status="triaged")
    t3 = _task("准备面试", category="career", status="triaged")
    repo.upsert_topic("thuthesis-repo", "thuthesis 论文仓库")
    repo.assign_topic("task", t1, "thuthesis-repo")

    def handler(prompt, kw):
        assert "thuthesis-repo" in prompt and t1 not in prompt.split("新事项（用户数据")[1]   # existing topics offered; assigned ones not re-sent
        return {"assignments": [{"id": t2, "topic_key": "thuthesis-repo", "topic_title": "别的名字"},     # reuse: the title must stay
                                {"id": t3, "topic_key": "求职 & 面试!!", "topic_title": "求职与面试"},        # bad key -> slugified
                                {"id": "ghost", "topic_key": "x", "topic_title": "x"}]}                   # unknown id ignored
    _fake_agent(monkeypatch, handler)
    assert topics.ensure_topics() == {"unassigned": 2, "assigned": 2}
    a = repo.topic_assignments("task", [t1, t2, t3])
    assert a[t1] == a[t2] == "thuthesis-repo" and repo.get_topic("thuthesis-repo")["title"] == "thuthesis 论文仓库"
    assert a[t3] and a[t3] != "thuthesis-repo" and repo.get_topic(a[t3])["title"] == "求职与面试"
    assert topics.ensure_topics() == {"unassigned": 0, "assigned": 0}                                     # nothing new -> no call


def test_history_is_plain_language_and_chronological(client):
    from awos import repo
    from awos.brain import topics
    t = _task("检查仓库状态", project="thuthesis", status="triaged", source="sensor:git")
    repo.set_digest("task", t, "整理 thuthesis 未提交改动", ["thuthesis"], source="llm")
    repo.set_task_status(t, "ready", "accepted by user", actor="user")
    repo.claim_next_task("w", 60)
    aid = repo.create_approval(title="先怎么处理 69 个文件？", kind="decision", task_id=t, recommendation="b：先分类",
                               options=[{"key": "a", "label": "全部提交"}, {"key": "b", "label": "先分类再提交"}])
    repo.decide_approval(aid, "b", note="别动 main.pdf")
    repo.set_task_status(t, "waiting_user", "L2：分析与方案已给出", force=True)
    hist = topics.history("p:thuthesis")
    texts = [h["text"] for h in hist]
    assert any(x.startswith("发现：整理 thuthesis 未提交改动（来自Git 仓库）") for x in texts)
    assert any("你接受了建议" in x for x in texts)
    assert any("AI 请你决定：先怎么处理 69 个文件？" in x and "AI 建议：b：先分类" in x for x in texts)
    assert any("你选择了「先分类再提交」" in x and "备注：别动 main.pdf" in x for x in texts)
    assert any("整理好了，交给你接手" in x for x in texts)
    stamps = [h["ts"] for h in hist]
    assert stamps == sorted(stamps)


def test_brief_generation_filters_asks_and_goes_stale_when_items_change(client, monkeypatch):
    from awos.brain import topics
    t = _task("检查仓库", project="thuthesis", status="waiting_user")
    a = _approval(task_id=t, kind="decision", title="先怎么处理？")
    seen = {}

    def handler(prompt, kw):
        seen["route"], seen["purpose"] = kw.get("route"), kw["purpose"]
        assert "前情提要" in prompt and "先怎么处理？" in prompt
        return {"state_now": "两个提交已完成，等你决定是否推送。",
                "recap": [{"ts": "2026-11-04T10:00:00Z", "text": "AI 检查了仓库"}, {"ts": "", "text": "   "}],
                "asks": [{"id": a, "question": "要不要推送？", "why_now": "3 天前提出", "stakes": "不推送则只在本地"},
                         {"id": "bogus", "question": "x", "why_now": "x", "stakes": "x"}]}
    _fake_agent(monkeypatch, handler)
    brief = topics.generate_brief("p:thuthesis")
    assert seen == {"route": "brief", "purpose": "brief"}
    assert [x["id"] for x in brief["asks"]] == [a] and len(brief["recap"]) == 1                           # bogus ask + blank recap dropped
    view = topics.topic_view("p:thuthesis", [])
    assert view["brief"]["state_now"].startswith("两个提交") and view["brief"]["stale"] is False
    _approval(task_id=t, kind="decision", title="第二个问题")
    assert topics.topic_view("p:thuthesis", [])["brief"]["stale"] is True                                  # new open item -> stale
    assert topics.refresh_briefs(min_age_min=0)["generated"] == 1
    assert topics.refresh_briefs(min_age_min=0) == {"stale": 0, "generated": 0}                           # unchanged -> no work


def test_refresh_is_throttled(client, monkeypatch):
    from awos.brain import topics
    t = _task("检查仓库", project="thuthesis", status="waiting_user")
    _approval(task_id=t, kind="decision", title="q1")
    n = []
    _fake_agent(monkeypatch, lambda p, kw: n.append(1) or {"state_now": "s", "recap": [], "asks": []})
    assert topics.refresh_briefs()["generated"] == 1
    _approval(task_id=t, kind="decision", title="q2")
    assert topics.refresh_briefs(min_age_min=20) == {"stale": 0, "generated": 0}                          # changed, but briefed <20 min ago
    assert len(n) == 1


def test_inbox_and_topic_endpoints(client, monkeypatch):
    t1 = _task("检查仓库", project="thuthesis", status="waiting_user")
    _task("别的事", category="comms", status="triaged")
    _approval(task_id=t1, kind="decision", title="先怎么处理？")
    _approval(task_id=t1, kind="action", title="提交这 3 个文件", action_type="git_commit")
    inbox = client.get("/api/inbox").json()
    assert {f["topic_key"] for f in inbox["feed"]} == {"p:thuthesis", "c:comms"}
    top = {t["key"]: t for t in inbox["topics"]}
    assert top["p:thuthesis"]["counts"]["decisions"] == 1 and top["p:thuthesis"]["counts"]["approvals"] == 1
    assert top["p:thuthesis"]["counts"]["total"] == 3 and inbox["topics"][0]["key"] == "p:thuthesis"      # pending decisions first
    assert top["p:thuthesis"]["has_brief"] is False and top["p:thuthesis"]["state_now"]
    detail = client.get("/api/topics/p:thuthesis").json()
    assert detail["title"] == "thuthesis" and detail["brief"] is None and len(detail["items"]) == 3 and isinstance(detail["history"], list)
    assert client.get("/api/topics/nope").status_code == 404
    assert client.post("/api/topics/p:thuthesis/refresh").status_code == 401                               # mutating -> token
    started = []
    import awos.brain.topics as tm
    monkeypatch.setattr(tm, "generate_brief", lambda key: started.append(key))
    assert client.post("/api/topics/p:thuthesis/refresh", headers=H).status_code == 202
    time.sleep(0.3)
    assert started == ["p:thuthesis"]
    assert client.get(f"/api/tasks/{t1}").json()["task"]["topic"]["key"] == "p:thuthesis"
    cols = {c["key"]: c for c in client.get("/api/board").json()["columns"]}
    assert cols["needs_you"]["items"][0]["topic_key"] == "p:thuthesis"


def test_reminders_are_coalesced_by_topic_and_respect_window_and_quiet_hours(client, monkeypatch):
    from awos import repo
    from awos.hitl import batcher, notify as notify_mod
    sent = []
    monkeypatch.setattr(notify_mod, "notify", lambda title, body="", **kw: sent.append((title, body, kw["level"])) or [])
    monkeypatch.setattr(notify_mod, "in_quiet_hours", lambda hm=None: False)
    t1 = _task("检查仓库", project="thuthesis", status="waiting_user")
    t2 = _task("准备面试", category="career", status="waiting_user")
    batcher.enqueue("待你确认：A", "a", ref_type="task", ref_id=t1)
    batcher.enqueue("待你确认：B", "b", ref_type="task", ref_id=t1)
    batcher.enqueue("待验收：C", "c", ref_type="task", ref_id=t2)
    assert sent == [] and batcher.flush() is False                                       # inside the window: nothing sent yet
    q = repo.kv_get(batcher.KEY)
    q[0]["ts"] = "2000-01-01T00:00:00Z"
    repo.kv_set(batcher.KEY, q)
    monkeypatch.setattr(notify_mod, "in_quiet_hours", lambda hm=None: True)
    assert batcher.flush() is False and len(repo.kv_get(batcher.KEY)) == 3               # quiet hours hold it back (and keep it)
    monkeypatch.setattr(notify_mod, "in_quiet_hours", lambda hm=None: False)
    assert batcher.flush() is True and len(sent) == 1 and repo.kv_get(batcher.KEY) == []
    title, body, level = sent[0]
    assert title == "有 3 件事等你（2 个话题）" and "thuthesis：2 项" in body and "求职：1 项" in body and level == "action"
    batcher.enqueue("风险高的动作", "x", level="high", ref_type="task", ref_id=t1)         # high priority goes out at once
    assert len(sent) == 2 and sent[1][2] == "high" and sent[1][0].startswith("有 1 件事等你")


def test_decision_notes_are_learned_as_feedback(client):
    from awos import repo
    from awos.engine import actions
    t = _task("整理论文仓库", project="paper", status="waiting_user")
    a1 = _approval(task_id=t, kind="decision", title="先怎么处理 810 个文件？")
    a2 = _approval(task_id=t, kind="decision", title="第二个问题")
    actions.decide(a1, "approve", note="这个项目不管，后面不用追踪")
    actions.decide(a2, "approve", note="ok")                                             # too short to be an instruction
    facts = [f["value"] for f in repo.list_facts(kind="note") if f["key"].startswith("反馈·答复·")]
    assert facts == ["用户在回答「先怎么处理 810 个文件？」时说：这个项目不管，后面不用追踪"]
    from awos.brain.context import context_brief
    assert "这个项目不管，后面不用追踪" in context_brief()                                  # triage / executors see it


def test_dismiss_topic_clears_everything_open_and_requires_a_reason(client):
    from awos import repo
    t1 = _task("整理仓库", project="paper", status="waiting_approval")
    kid = _task("子任务", parent_id=t1, status="running")
    t2 = _task("另一件", project="paper", status="triaged")
    other = _task("无关", project="other", status="triaged")
    a = _approval(task_id=t1, kind="decision", title="怎么处理？")
    assert client.post("/api/topics/p:paper/dismiss", json={"note": ""}, headers=H).status_code == 422
    assert client.post("/api/topics/p:paper/dismiss", json={"note": "这个项目不管了"}).status_code == 401
    r = client.post("/api/topics/p:paper/dismiss", json={"note": "这个项目不管了"}, headers=H).json()
    assert r["dismissed"] == 2
    assert repo.get_task(t1)["status"] == repo.get_task(t2)["status"] == "dismissed" and repo.get_task(kid)["status"] == "cancelled"
    assert repo.get_approval(a)["status"] == "cancelled" and repo.get_task(other)["status"] == "triaged"
    assert any("这个项目不管了" in f["value"] for f in repo.list_facts(kind="note"))
