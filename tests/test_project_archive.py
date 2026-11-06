"""Archiving a project: the user no longer wants to watch it. It leaves the board, the guardian and the agents' prompts, and its
open work can be closed in the same step; agents cannot revive it, the user can restore it."""
from __future__ import annotations

from tests.test_api import H, client, env  # noqa: F401  (fixtures)


def _project(key="论文 thuthesis"):
    from awos import repo
    repo.upsert_fact("project", key, "博士学位论文修订", source="user", actor="user", pinned=True)
    return key


def _task(key, status, title="t"):
    from awos import repo
    return repo.create_task(title, project=key, status=status, source="manual")


def test_archive_hides_the_project_from_agents_and_restore_brings_it_back(client):
    from awos import repo
    from awos.brain.context import agent_write_fact, context_brief
    from awos.brain.projects import canonical_projects
    from awos.engine import actions
    key = _project()
    assert key in context_brief() and any(p["key"] == key for p in canonical_projects())
    assert actions.archive_project(key) == {"ok": True, "key": key, "closed": 0}
    assert not any(p["key"] == key for p in canonical_projects())
    brief = context_brief()
    assert "已归档项目" in brief and "不要为它们创建任务" in brief and f"**{key}**" not in brief      # named only in the do-not-bother line
    # agents (the context refresh, task agents) cannot change or revive it, whatever they write
    assert repo.upsert_fact("project", key, "新摘要", source="agent", actor="agent:context") is None
    assert agent_write_fact("project", key, "新摘要", source="t", confidence=0.9, actor="agent:context", may_define_projects=True) is None
    assert repo.list_facts(kind="project", status="archived")[0]["value"] == "博士学位论文修订"
    actions.restore_project(key)
    assert any(p["key"] == key for p in canonical_projects()) and f"**{key}**" in context_brief()
    for bad in (lambda: actions.archive_project("不存在"), lambda: actions.restore_project(key)):
        try:
            bad()
        except KeyError:
            continue
        raise AssertionError("expected KeyError")


def test_closing_open_work_dismisses_tasks_and_withdraws_approvals_without_piling_up_feedback_notes(client):
    from awos import repo
    from awos.engine import actions
    key = _project()
    ready, waiting, done = _task(key, "ready", "排队的"), _task(key, "waiting_user", "等我的"), _task(key, "done", "做完的")
    other = repo.create_task("别的项目", project="另一个项目", status="ready", source="manual")
    aid = repo.create_approval(title="决定", kind="decision", task_id=waiting, options=[{"key": "a", "label": "A"}])
    out = actions.archive_project(key, close_open=True)
    assert out["closed"] == 2
    assert repo.get_task(ready)["status"] == "dismissed" and repo.get_task(waiting)["status"] == "dismissed"
    assert repo.get_task(done)["status"] == "done" and repo.get_task(other)["status"] == "ready"
    assert repo.get_approval(aid)["status"] == "cancelled"
    assert repo.list_facts(kind="note") == []                      # "项目已归档" is not a preference worth remembering per task
    assert repo.get_task(ready)["status_reason"] == "项目已归档"


def test_archive_endpoints(client):
    from awos import repo
    key = _project("电厂改造模型")
    tid = _task(key, "ready")
    assert client.post(f"/api/projects/{key}/archive", json={"close_open": True}).status_code in (401, 403)       # token required
    assert client.post("/api/projects/不存在/archive", json={}, headers=H).status_code == 404
    assert client.post("/api/projects/__none__/archive", json={}, headers=H).status_code == 422
    r = client.post(f"/api/projects/{key}/archive", json={"close_open": True}, headers=H)
    assert r.status_code == 200 and r.json() == {"ok": True, "key": key, "closed": 1}
    assert repo.get_task(tid)["status"] == "dismissed"
    assert key not in [p["key"] for p in client.get("/api/projects").json().get("projects", [])]
    assert client.get("/api/context?status=all").json()["groups"]["project"][0]["status"] == "archived"
    assert client.post(f"/api/projects/{key}/restore", headers=H).json() == {"ok": True, "key": key}
    assert client.post(f"/api/projects/{key}/restore", headers=H).status_code == 404                   # already active


def test_archive_closes_only_tasks_that_belong_to_the_project(client):
    from awos import repo
    from awos.api.timeline import Dataset
    from awos.engine import actions
    key = _project("电厂改造模型")
    mine = _task(key, "ready", "校准参数")
    child = repo.create_task("子任务", parent_id=mine, status="ready", source="manual")          # belongs through its parent
    guessed = repo.create_task("顺便看看电厂改造模型的那封邮件", status="ready", source="manual")  # only mentions the name
    ds = Dataset()
    assert {mine, child, guessed} <= {t["id"] for t in ds.project_tasks(key)}                    # the board still shows all three
    assert {t["id"] for t in ds.project_tasks(key, explicit=True)} == {mine, child}
    lane = next(p for p in client.get("/api/timeline").json()["projects"] if p["key"] == key)
    assert lane["stats"]["close_open"] == 2 and lane["stats"]["tasks_open"] == 3                 # the dialog counts what it closes
    assert actions.archive_project(key, close_open=True)["closed"] == 2
    assert repo.get_task(guessed)["status"] == "ready"
    assert repo.get_task(mine)["status"] == repo.get_task(child)["status"] == "dismissed"
