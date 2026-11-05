"""Projects: canonical projects from the user's context, demoting agent working notes, linking topics, API."""
from __future__ import annotations

import pytest

from tests.test_api import H, client, env, _approval, _task  # noqa: F401  (fixtures)
from tests.test_topics import _fake_agent


def _facts(kind):
    from awos import repo
    return {f["key"]: f for f in repo.list_facts(kind=kind, status=None)}


def _seed_context():
    from awos import repo
    repo.upsert_fact("project", "学位论文 thuthesis", "博士学位论文修订", source="ctx", actor="agent")
    repo.upsert_fact("project", "电厂改造模型", "电厂改造研究代码与结果", source="ctx", actor="agent", pinned=True)
    repo.upsert_fact("project", "thuthesis-commit-state-2026-11-05", "HEAD 已提交，ahead/behind 0/0", source="task", actor="agent")
    repo.upsert_fact("project", "coal-retrofit-repositories-distinc", "两个不同仓库", source="task", actor="agent")
    repo.upsert_fact("project", "求职状态-2026-11-05", "某公司面试已参加", source="task", actor="agent")
    repo.upsert_fact("role", "身份-博士", "学校博士，能源系统建模", source="ctx", actor="agent")


def test_progress_note_detection_and_demotion_keep_real_projects(client):
    from awos.brain import projects
    _seed_context()
    assert [projects.looks_like_progress_note(k) for k in
            ("thuthesis-commit-state-2026-11-05", "coal-retrofit-repositories-distinc", "求职状态-2026-11-05",
             "学位论文 thuthesis", "Energy-Model 2.0", "Paper-A on Hydrogen", "电厂改造模型")] == [True, True, True, False, False, False, False]
    demoted = projects.demote_progress_notes()
    assert sorted(demoted) == ["coal-retrofit-repositories-distinc", "thuthesis-commit-state-2026-11-05", "求职状态-2026-11-05"]
    assert set(_facts("project")) == {"学位论文 thuthesis", "电厂改造模型"}
    notes = _facts("note")
    assert "进展·thuthesis-commit-state-2026-11-05" in notes and notes["进展·求职状态-2026-11-05"]["value"] == "某公司面试已参加"
    assert projects.demote_progress_notes() == []                                                           # idempotent


def test_pinned_facts_are_never_demoted(client):
    from awos import repo
    from awos.brain import projects
    repo.upsert_fact("project", "audit-of-the-model-2026-10-01", "用户自己起的名字", source="user", actor="user", pinned=True)
    assert projects.demote_progress_notes() == [] and "audit-of-the-model-2026-10-01" in _facts("project")
    assert [p["key"] for p in projects.canonical_projects()] == ["audit-of-the-model-2026-10-01"]


def test_match_project_by_name(client):
    from awos.brain import projects
    _seed_context()
    assert projects.match_project("thuthesis")["key"] == "学位论文 thuthesis"
    assert projects.match_project("电厂改造")["key"] == "电厂改造模型"
    assert projects.match_project("无关的事情") is None and projects.match_project("ab") is None


def test_task_agents_report_progress_but_cannot_create_projects(client):
    from awos.brain import context
    _seed_context()
    context.apply_updates([{"kind": "project", "key": "thuthesis", "value": "第 5 章图件已核对"},
                           {"kind": "project", "key": "brand-new-audit-2026-11-06", "value": "一次性检查结果"},
                           {"kind": "deadline", "key": "答辩", "value": "2027-05"}], source="task:x")
    assert set(_facts("project")) == {"学位论文 thuthesis", "电厂改造模型", "thuthesis-commit-state-2026-11-05",
                                      "coal-retrofit-repositories-distinc", "求职状态-2026-11-05"}      # nothing new (old ones untouched here)
    notes = _facts("note")
    assert notes["进展·学位论文 thuthesis"]["value"] == "（学位论文 thuthesis）第 5 章图件已核对"            # rolling note, attached to the project name
    assert "进展·brand-new-audit-2026-11-06" in notes and "答辩" in _facts("deadline")
    context.apply_updates([{"kind": "project", "key": "thuthesis", "value": "又有新进展"}], source="task:y")
    assert _facts("note")["进展·学位论文 thuthesis"]["value"] == "（学位论文 thuthesis）又有新进展"          # replaced, not accumulated


def test_context_agent_may_maintain_project_summaries_but_not_dated_projects(client):
    from awos.brain import context
    _seed_context()
    context.agent_write_fact("project", "学位论文 thuthesis", "新的概述", source="s", confidence=0.8, actor="agent:context", may_define_projects=True)
    context.agent_write_fact("project", "新的稳定项目", "概述", source="s", confidence=0.8, actor="agent:context", may_define_projects=True)
    context.agent_write_fact("project", "check-2026-11-05-run", "一次检查", source="s", confidence=0.8, actor="agent:context", may_define_projects=True)
    projects = _facts("project")
    assert projects["学位论文 thuthesis"]["value"] == "新的概述" and "新的稳定项目" in projects and "check-2026-11-05-run" not in projects
    assert "进展·check-2026-11-05-run" in _facts("note")


def test_link_topics_by_task_project_field_then_by_llm_and_user_links_win(client, monkeypatch):
    from awos import repo
    from awos.brain import projects
    _seed_context()
    projects.demote_progress_notes()
    t1 = _task("检查仓库", project="thuthesis", status="waiting_user")
    t2 = _task("电厂相关一", status="waiting_user", category="research")
    t3 = _task("无关的事", status="waiting_user", category="comms")
    repo.upsert_topic("coal", "电厂"); repo.assign_topic("task", t2, "coal")
    repo.upsert_topic("misc", "杂项"); repo.assign_topic("task", t3, "misc")

    def handler(prompt, kw):
        if not seen:   # first pass: both unlinked topics are offered, the rule-linked one is not
            assert "电厂改造模型" in prompt and "topic_key=coal" in prompt and "topic_key=misc" in prompt and "项目字段=thuthesis" not in prompt
        else:          # second pass: the user's link on `coal` must not even be offered to the AI again
            assert "topic_key=coal" not in prompt
        seen.append(1)
        return {"links": [{"topic_key": "coal", "project": "电厂改造模型"}, {"topic_key": "misc", "project": "不存在的项目"}]}
    seen: list[int] = []
    calls = _fake_agent(monkeypatch, handler)
    out = projects.link_topics()
    assert calls == ["digest"] and out["linked"] == 2
    links = repo.topic_project_links()
    assert links["p:thuthesis"]["project_key"] == "学位论文 thuthesis" and links["p:thuthesis"]["source"] == "rule"
    assert links["coal"]["project_key"] == "电厂改造模型" and links["coal"]["source"] == "llm" and "misc" not in links   # unknown project name ignored
    repo.link_topic_project("coal", "学位论文 thuthesis", "user")                                         # the user decides
    projects.link_topics()
    assert repo.topic_project_links()["coal"]["project_key"] == "学位论文 thuthesis"                         # never overridden by the AI


def test_project_endpoints_and_relinking(client):
    from awos import repo
    _seed_context()
    t = _task("检查仓库", project="thuthesis", status="waiting_user")
    _approval(task_id=t, kind="decision", title="怎么处理？")
    repo.upsert_fact("deadline", "thuthesis 预答辩", "2026-12-01 预答辩", source="ctx", actor="agent")
    repo.upsert_fact("note", "进展·学位论文 thuthesis", "第 5 章图件已核对", source="task", actor="agent")
    repo.upsert_topic("p:thuthesis", "thuthesis", "fallback")
    repo.link_topic_project("p:thuthesis", "学位论文 thuthesis", "rule")
    inbox = client.get("/api/inbox").json()
    topic = next(x for x in inbox["topics"] if x["key"] == "p:thuthesis")
    assert topic["project"] == {"key": "学位论文 thuthesis", "name": "学位论文 thuthesis", "summary": "博士学位论文修订", "link_source": "rule"}
    plist = client.get("/api/projects").json()["projects"]
    top = plist[0]
    assert top["name"] == "学位论文 thuthesis" and top["open_items"] == 2 and top["topic_keys"] == ["p:thuthesis"] and top["deadlines"] == 1 and top["notes"] == 1
    assert {p["name"] for p in plist} == {"学位论文 thuthesis", "电厂改造模型"}                              # working notes are not projects
    detail = client.get("/api/projects/学位论文 thuthesis").json()
    assert detail["related"]["deadline"][0]["key"] == "thuthesis 预答辩" and detail["role"]["key"] == "身份-博士"
    assert detail["topics"][0]["key"] == "p:thuthesis" and detail["recent_tasks"][0]["id"] == t
    assert client.get("/api/projects/nope").status_code == 404
    assert client.get("/api/topics/p:thuthesis").json()["project"]["name"] == "学位论文 thuthesis"
    # relink: token required, unknown project 422, user link recorded, unlink, create-and-link
    assert client.post("/api/topics/p:thuthesis/project", json={"project_key": "电厂改造模型"}).status_code == 401
    assert client.post("/api/topics/p:thuthesis/project", json={"project_key": "没有"}, headers=H).status_code == 422
    r = client.post("/api/topics/p:thuthesis/project", json={"project_key": "电厂改造模型"}, headers=H).json()
    assert r["project"]["name"] == "电厂改造模型" and r["project"]["link_source"] == "user"
    assert client.post("/api/topics/p:thuthesis/project", json={}, headers=H).json()["project"] is None
    assert client.get("/api/topics/p:thuthesis").json()["project"] is None
    from awos.brain import projects
    projects.link_topics()
    assert repo.topic_project_links()["p:thuthesis"]["source"] == "user" and repo.topic_project_links()["p:thuthesis"]["project_key"] == ""   # unlink sticks
    made = client.post("/api/topics/p:thuthesis/project", json={"new_name": "ConfX 报销", "new_summary": "整理发票"}, headers=H).json()
    assert made["project"]["name"] == "ConfX 报销" and _facts("project")["ConfX 报销"]["pinned"] == 1
    assert client.post("/api/topics/nope/project", json={}, headers=H).status_code == 404


def test_consolidation_renames_summarises_and_files_notes(client, monkeypatch):
    from awos import repo
    from awos.brain import projects
    _seed_context()
    _task("t", project="示例论文B", status="triaged")

    def handler(prompt, kw):
        assert "thuthesis-commit-state-2026-11-05" in prompt and "示例论文B" in prompt
        return {"projects": [{"name": "学位论文 thuthesis", "summary": "新概述"},
                             {"name": "示例论文B", "summary": "已投稿并接收的论文"}],
                "note_map": [{"note_key": "进展·thuthesis-commit-state-2026-11-05", "project": "学位论文 thuthesis"},
                             {"note_key": "进展·求职状态-2026-11-05", "project": ""}]}
    _fake_agent(monkeypatch, handler)
    out = projects.consolidate_projects()
    assert out["demoted"] == 3 and out["projects"] == 2 and out["notes_mapped"] == 1
    proj = _facts("project")
    assert proj["学位论文 thuthesis"]["value"] == "新概述" and proj["示例论文B"]["value"] == "已投稿并接收的论文"
    assert proj["电厂改造模型"]["value"] == "电厂改造研究代码与结果"                                          # pinned: never rewritten
    assert _facts("note")["进展·thuthesis-commit-state-2026-11-05"]["value"].startswith("（学位论文 thuthesis）")


def test_background_pack_and_brief_prompt_use_the_users_context(client, monkeypatch):
    from awos import repo
    from awos.brain import projects, topics
    _seed_context()
    repo.upsert_fact("goal", "毕业与就业", "推进学位论文和求职", source="ctx", actor="agent")
    repo.upsert_fact("deadline", "thuthesis 预答辩", "2026-12-01", source="ctx", actor="agent")
    t = _task("检查仓库", project="thuthesis", status="waiting_user")
    _approval(task_id=t, kind="decision", title="先怎么处理？")
    repo.link_topic_project("p:thuthesis", "学位论文 thuthesis", "rule")
    pack = projects.background_pack("学位论文 thuthesis")
    assert "学校博士" in pack and "毕业与就业" in pack and "所属项目「学位论文 thuthesis」" in pack and "thuthesis 预答辩" in pack
    seen = {}

    def handler(prompt, kw):
        seen["p"] = prompt
        return {"state_now": "s", "recap": [], "asks": []}
    _fake_agent(monkeypatch, handler)
    topics.generate_brief("p:thuthesis")
    assert "用户的整体背景" in seen["p"] and "毕业与就业" in seen["p"] and "thuthesis 预答辩" in seen["p"]
