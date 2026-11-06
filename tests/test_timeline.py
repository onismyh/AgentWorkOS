"""Project timeline + milestones: task -> project assignment, plain-language events (agent vs human), media, flow graph, milestone CRUD."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from tests.test_api import H, client, env  # noqa: F401  (fixtures)


def _project(name: str, summary: str = "测试项目") -> None:
    from awos import repo
    repo.upsert_fact("project", name, summary, source="user", actor="user", pinned=True)


def _task(title: str, **kw) -> str:
    from awos import repo
    kw.setdefault("source", "manual")
    return repo.create_task(title, **kw)


def _run(task_id: str, executor: str = "codex", *, status: str = "succeeded", purpose: str = "execute", summary: str | None = "做完了", cost: float = 0.0,
         error: str | None = None, error_class: str | None = None) -> str:
    from awos import db, repo
    rid = repo.create_run(purpose, executor, task_id=task_id)
    repo.update_run(rid, status=status, started_at=db.utcnow(), ended_at=db.utcnow(), cost_usd=cost, error=error, error_class=error_class,
                    structured={"summary": summary} if summary else None)
    return rid


def _tl(client, key: str) -> dict:
    r = client.get(f"/api/projects/{key}/timeline")
    assert r.status_code == 200, r.text
    return r.json()


def _events(tl: dict) -> list[dict]:
    return [e for d in tl["days"] for e in d["events"]]


def test_tasks_are_assigned_to_projects_by_topic_text_and_parent(client):
    from awos import db, repo
    _project("电厂改造模型")
    _project("ConfX 出访与汇报")
    _project("学位论文 thuthesis")
    a = _task("整理仓库", project="电厂改造")                                    # tasks.project text matches the canonical name
    b = _task("准备 ConfX 报销材料清单")                                         # a distinctive word of the project name appears in the title
    c = _task("子任务：核对数据", parent_id=a)                                      # subtasks follow their root
    d = _task("某个无关的杂事")                                                    # nothing matches
    e = _task("话题里的任务")
    db.insert("topic_items", {"entity_type": "task", "entity_id": e, "topic_key": "thesis", "updated_at": db.utcnow()})
    repo.link_topic_project("thesis", "学位论文 thuthesis", "user")                # the topic link wins over text
    lanes = {p["key"]: p for p in client.get("/api/timeline").json()["projects"]}
    assert lanes["电厂改造模型"]["stats"]["tasks"] == 2 and lanes["ConfX 出访与汇报"]["stats"]["tasks"] == 1
    assert lanes["学位论文 thuthesis"]["stats"]["tasks"] == 1 and lanes["__none__"]["stats"]["tasks"] == 1
    assert {n["id"] for n in _tl(client, "电厂改造模型")["flow"]["nodes"]} == {a, c}
    assert _tl(client, "__none__")["flow"]["nodes"][0]["id"] == d


def test_events_tell_what_the_agent_did_and_what_the_human_changed(client):
    from awos import repo
    from awos.engine import actions
    _project("电厂改造模型")
    t = _task("整理文件", project="电厂改造模型", level=4)
    _run(t, "codex", status="failed", summary=None, error="429 Too Many Requests", error_class="transient")
    _run(t, "claude", summary="已整理 12 个文件，附清单", cost=0.31)
    ask = repo.create_approval(title="提交整理结果", kind="action", action_type="git_commit", task_id=t,
                               options=[{"key": "approve", "label": "批准"}, {"key": "reject", "label": "拒绝"}])
    repo.decide_approval(ask, "approve", note="先别推送", via="dashboard")
    auto = repo.create_approval(title="独立提交脚本", kind="action", action_type="git_commit", task_id=t, options=[{"key": "approve", "label": "批准"}])
    repo.decide_approval(auto, "approve", via="auto", actor="system")
    repo.set_approval_status(auto, "executed", result={"done": True, "details": "ok"})
    actions.retry_task(t, note="再核对一遍")
    actions.cancel_task(_task("不要了", project="电厂改造模型"), reason="不需要了")
    tl = _tl(client, "电厂改造模型")
    ev = _events(tl)
    assert any(e["actor"] == "agent:claude" and e["outcome"] == "done" and "已整理 12 个文件" in e["detail"] and e["cost"] == 0.31 for e in ev)
    rec = next(e for e in ev if e["actor"] == "agent:codex")
    assert rec["outcome"] == "recovered" and "限流" in rec["title"]                       # the later success makes the failure a recovered one
    mine = next(e for e in ev if e["human"] and e["title"].startswith("你批准了（提交代码）"))
    assert mine["detail"] == "先别推送" and mine["refs"]["approval"] == ask
    assert any(e["actor"] == "system" and "按白板规则自动放行" in e["title"] for e in ev)
    assert any(e["human"] and e["title"].startswith("你让 Agent 重新来过") and e["detail"] == "再核对一遍" for e in ev)
    assert any(e["human"] and e["title"].startswith("你取消了任务") and e["detail"] == "不需要了" for e in ev)
    assert tl["stats"]["interventions"] >= 4 and tl["stats"]["agent_runs"] == 2 and tl["stats"]["agent_ok"] == 1
    assert tl["stats"]["executors"] == {"codex": {"runs": 1, "ok": 0}, "claude": {"runs": 1, "ok": 1}} and tl["days"][0]["label"] == "今天"
    assert all(not e["detail"].startswith("{") for e in ev)                                # excerpts, never raw JSON / logs


def test_images_ride_on_the_run_that_produced_them_and_nothing_outside_var_is_served(client, env):
    from awos import db
    _project("电厂改造模型")
    t = _task("画图", project="电厂改造模型")
    rid = _run(t, "claude")
    png = env["var"] / "workspaces" / "fig.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = env["var"] / "workspaces" / "notes.md"
    doc.write_text("# n", encoding="utf-8")
    outside = env["tmp"] / "secret.png"
    outside.write_bytes(b"\x89PNG")
    for title, p, run in (("图 1", png, rid), ("说明", doc, rid), ("泄露", outside, rid)):
        db.insert("artifacts", {"id": db.new_id("art"), "task_id": t, "run_id": run, "kind": "file", "title": title, "path": str(p), "created_at": db.utcnow()})
    ev = next(e for e in _events(_tl(client, "电厂改造模型")) if e["kind"] == "run")
    assert [m["title"] for m in ev["media"]] == ["图 1"] and ev["media"][0]["image"] and ev["media"][0]["url"].startswith("/api/artifacts/art_")
    assert [f["title"] for f in ev["files"]] == ["说明"]
    assert client.get(ev["media"][0]["url"]).status_code == 200


def test_milestones_crud_effective_status_and_derived_markers(client):
    from awos import repo
    _project("电厂改造模型")
    done = _task("交付稿件", project="电厂改造模型", level=4, status="done")
    repo.db.update("tasks", "id", done, {"finished_at": "2026-11-05T10:00:00Z"})
    repo.upsert_fact("deadline", "电厂改造模型投稿", "2099-05-10 前提交修回稿", source="user", actor="user", pinned=True)
    assert client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "x"}).status_code in (401, 403)        # token protected
    assert client.post("/api/milestones", json={"project_key": "没有这个项目", "title": "x"}, headers=H).status_code == 422
    assert client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "x", "target_date": "明天"}, headers=H).status_code == 422
    past = (date.today() - timedelta(days=3)).isoformat()
    m1 = client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "初稿完成", "target_date": past, "summary": "第 1-3 章"}, headers=H).json()["id"]
    m2 = client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "投出去", "target_date": "2099-01-01"}, headers=H).json()["id"]
    tl = _tl(client, "电厂改造模型")
    by = {m["title"]: m for m in tl["milestones"]}
    assert by["初稿完成"]["status"] == "missed" and by["初稿完成"]["stored_status"] == "planned" and by["投出去"]["status"] == "planned"
    assert by["电厂改造模型投稿"]["source"] == "auto" and by["电厂改造模型投稿"]["target_date"] == "2099-05-10" and not by["电厂改造模型投稿"]["editable"]
    assert by["交付：交付稿件"]["source"] == "auto" and by["交付：交付稿件"]["status"] == "done"
    assert tl["progress"] == {**tl["progress"], "pct": 0, "basis": "milestones"}
    r = client.patch(f"/api/milestones/{m1}", json={"status": "done"}, headers=H).json()["milestone"]
    assert r["status"] == "done" and r["done_at"]
    tl = _tl(client, "电厂改造模型")
    assert tl["progress"]["pct"] == 50 and any(e["human"] and e["title"].startswith("你把里程碑标为完成") for e in _events(tl))
    lane = next(p for p in client.get("/api/timeline").json()["projects"] if p["key"] == "电厂改造模型")
    assert lane["next_milestone"]["title"] == "投出去" and lane["progress"]["milestones"] == {"done": 1, "total": 2}
    assert client.patch(f"/api/milestones/{m2}", json={"target_date": None, "title": "投稿"}, headers=H).json()["milestone"]["target_date"] is None
    assert client.patch("/api/milestones/auto:task:x", json={"title": "x"}, headers=H).status_code == 404
    assert client.delete(f"/api/milestones/{m2}", headers=H).status_code == 200 and client.delete(f"/api/milestones/{m2}", headers=H).status_code == 404


def test_flow_graph_has_dependencies_rollups_and_human_gates(client):
    from awos import repo
    _project("电厂改造模型")
    root = _task("总任务", project="电厂改造模型")
    a = _task("第一步", parent_id=root)
    b = _task("第二步", parent_id=root)
    repo.add_dependency(b, a)
    gate = repo.create_approval(title="选方案", kind="decision", task_id=a, options=[{"key": "x", "label": "方案 X"}])
    repo.decide_approval(gate, "x", via="dashboard")
    pending = repo.create_approval(title="批准写入", kind="action", task_id=b, options=[{"key": "approve", "label": "批准"}])
    flow = _tl(client, "电厂改造模型")["flow"]
    kinds = {(e["type"]) for e in flow["edges"]}
    assert {e["type"] for e in flow["edges"] if e["from"] == a and e["to"] == b} == {"dep"} and kinds == {"dep", "rollup", "gate"}
    gates = {n["id"]: n for n in flow["nodes"] if n["type"] == "gate"}
    assert gates[gate]["state"] == "decided" and gates[gate]["decision"] == "方案 X" and gates[pending]["state"] == "waiting"
    assert {n["id"] for n in flow["nodes"] if n["type"] == "task"} == {root, a, b}


def test_unknown_project_is_a_404_and_the_pile_of_unassigned_tasks_has_its_own_timeline(client):
    assert client.get("/api/projects/没有/timeline").status_code == 404
    _task("没人认领的事")
    tl = _tl(client, "__none__")
    assert tl["name"] == "未归类" and tl["stats"]["tasks"] == 1 and _events(tl)[0]["title"].startswith("你新建了任务")


# --------------------------------------------------------------------------- the portfolio review: health, state, last, asks, highlights, archive

TODAY = date(2026, 11, 6)
BASE = dict(idle_days=1, tasks=3, tasks_active=1, tasks_failed=0, open_asks=0, milestones=[], pct=40)


def _ms(days: int | None, status: str = "planned", title: str = "投稿") -> dict:
    return {"title": title, "status": status, "target_date": None if days is None else (TODAY + timedelta(days=days)).isoformat()}


HEALTH_CASES = [
    # (changes to BASE, expected level, a phrase the reason must contain)
    ({}, "ok", "1 个任务在推进"),
    ({"tasks_active": 0}, "ok", "1 天前有动静"),
    ({"idle_days": 0, "tasks_active": 0}, "ok", "今天有动静"),
    ({"milestones": [_ms(-3, "missed", "初稿")]}, "risk", "「初稿」已逾期 3 天"),
    ({"milestones": [_ms(-1)]}, "risk", "已逾期 1 天"),                                   # past date, not yet flagged by the caller
    ({"milestones": [_ms(-3, "missed")], "open_asks": 2}, "risk", "已逾期"),               # overdue beats open asks
    ({"tasks_failed": 2}, "risk", "有 2 个任务没做成"),
    ({"milestones": [_ms(3)], "idle_days": 6}, "risk", "11月9日截止，6 天没动静"),            # <=3 days away while stalled
    ({"milestones": [_ms(3)], "open_asks": 2}, "risk", "还有 2 项等你处理"),                  # <=3 days away with open asks
    ({"milestones": [_ms(0)], "idle_days": None}, "risk", "今天截止，还没有动静"),
    ({"milestones": [_ms(3)]}, "attention", "11月9日截止，进度 40%"),                         # close but being worked on
    ({"open_asks": 2}, "attention", "有 2 项等你处理"),
    ({"milestones": [_ms(7)]}, "attention", "进度 40%"),
    ({"milestones": [_ms(8)]}, "ok", "1 个任务在推进"),                                    # >7 days away, nothing wrong
    ({"idle_days": 5}, "stalled", "5 天没动静"),                                         # open work and 5 quiet days
    ({"idle_days": 4}, "ok", "1 个任务在推进"),
    ({"idle_days": 9, "tasks_active": 0, "milestones": [_ms(20)]}, "stalled", "9 天没动静，11月26日截止"),   # no open work but a deadline within 30 days
    ({"idle_days": 9, "tasks_active": 0, "milestones": [_ms(40)]}, "ok", "9 天前有动静"),
    ({"idle_days": 9, "tasks_active": 0}, "ok", "9 天前有动静"),                          # nothing open, nothing due: not an alarm
    ({"idle_days": None, "tasks_active": 1}, "stalled", "还没有动静"),
    ({"milestones": [_ms(-5, "done")], "tasks_active": 0}, "ok", "天前有动静"),            # finished milestones are ignored
    ({"tasks": 0, "tasks_active": 0, "idle_days": 20}, "quiet", "没有任务，也没有里程碑"),
    ({"tasks": 0, "tasks_active": 0, "idle_days": None}, "quiet", "没有任务"),
    ({"tasks": 0, "tasks_active": 0, "idle_days": 3}, "ok", "3 天前有动静"),               # recent life is not quiet
    ({"tasks": 0, "tasks_active": 0, "idle_days": 20, "milestones": [_ms(60)]}, "ok", "20 天前有动静"),
]


@pytest.mark.parametrize("change, level, phrase", HEALTH_CASES, ids=[f"{i}-{c[1]}" for i, c in enumerate(HEALTH_CASES)])
def test_health_rules_table(change, level, phrase):
    from awos.api import timeline
    h = timeline.health_of(TODAY, **{**BASE, **change})
    assert h["level"] == level and h["label"] == timeline.HEALTH_LABEL[level] and phrase in h["reason"], h
    assert set(h) == {"level", "label", "reason"} and len(h["reason"]) <= 40


def test_health_pile_thresholds_and_labels():
    from awos.api import timeline
    assert (timeline.STALLED_DAYS, timeline.QUIET_DAYS, timeline.RISK_SOON_DAYS, timeline.ATTENTION_SOON_DAYS, timeline.DEADLINE_WINDOW_DAYS) == (5, 14, 3, 7, 30)
    assert [timeline.HEALTH_LABEL[k] for k in timeline.HEALTH_LEVELS] == ["有风险", "要你处理", "停滞", "正常", "暂无动静"]
    pile = lambda **kw: timeline.health_of(TODAY, **{**BASE, "pile_open": 3, **kw})        # noqa: E731
    assert pile()["level"] == "attention" and "3 个任务还没归到项目" in pile()["reason"]
    assert pile(pile_open=0)["level"] == "ok" and pile(pile_open=0, tasks=0)["level"] == "quiet"


def test_lanes_follow_attention_order_and_carry_the_review_fields(client):
    from awos import repo
    names = ["风险项目", "关注项目", "停滞项目", "正常项目", "安静项目"]
    for n in names:
        _project(n, f"{n}的说明")
    past = (date.today() - timedelta(days=3)).isoformat()
    client.post("/api/milestones", json={"project_key": "风险项目", "title": "初稿", "target_date": past}, headers=H)
    t = _task("等拍板的任务", project="关注项目", status="waiting_approval")
    ask = repo.create_approval(title="选哪个方案", kind="decision", task_id=t, options=[{"key": "a", "label": "方案 A"}])
    _task("没人管的任务", project="停滞项目", status="ready", source="sensor:git")           # open work, and nobody has touched it
    _task("在推进的任务", project="正常项目", status="running")
    repo.upsert_fact("note", "进展·正常项目", "（正常项目）11-05 18:37 基线：已完成第一章；其余待办。", source="t", actor="agent")
    lanes = client.get("/api/timeline").json()["projects"]
    assert [p["key"] for p in lanes if p["key"] in names] == names                       # risk, attention, stalled, ok, quiet; every project is listed
    by = {p["key"]: p for p in lanes}
    assert [by[n]["health"]["level"] for n in names] == ["risk", "attention", "stalled", "ok", "quiet"]
    assert by["风险项目"]["health"] == {"level": "risk", "label": "有风险", "reason": "「初稿」已逾期 3 天"}
    assert by["关注项目"]["asks"] == [{"kind": "decision", "label": "拍板", "title": "选哪个方案", "ts": by["关注项目"]["asks"][0]["ts"], "ref": {"approval": ask}}]
    assert by["停滞项目"]["health"]["reason"] == "还没有动静" and by["安静项目"]["health"]["label"] == "暂无动静"
    assert by["正常项目"]["state"] == "基线：已完成第一章" and by["风险项目"]["state"] == "风险项目的说明"       # newest progress note, else the summary
    for p in lanes:
        assert len(p["state"]) <= 60 and set(p["health"]) == {"level", "label", "reason"} and len(p["asks"]) <= 3
        assert {"key", "name", "progress", "next_milestone", "last_activity", "stats", "last"} <= set(p)
        assert {"tasks_active", "open_asks"} <= set(p["stats"])
    assert by["风险项目"]["next_milestone"]["days"] == -3
    first = by["关注项目"]["last"]
    assert first is None or {"ts", "text", "actor"} == set(first)


def test_asks_are_capped_newest_first_and_point_at_the_inbox_item(client):
    from awos import repo
    _project("忙项目")
    t = _task("很多请求", project="忙项目", status="waiting_approval")
    ids = [repo.create_approval(title=f"批准{i}", kind="action", action_type="git_commit", task_id=t, options=[{"key": "approve", "label": "批准"}]) for i in range(1, 7)]
    review = _task("待验收", project="忙项目", status="review")
    lane = next(p for p in client.get("/api/timeline").json()["projects"] if p["key"] == "忙项目")
    assert len(lane["asks"]) == 3
    detail = _tl(client, "忙项目")
    assert len(detail["asks"]) == 5 and detail["stats"]["open_asks"] == 7                    # shown capped, counted in full
    assert [a["title"] for a in detail["asks"]][:2] == ["待验收", "批准6"] or detail["asks"][0]["ref"] in ({"task": review}, {"approval": ids[-1]})
    assert all(a["label"] and (set(a["ref"]) == {"approval"} or set(a["ref"]) == {"task"}) for a in detail["asks"])
    assert {a["kind"] for a in detail["asks"]} <= {"approval", "decision", "budget", "review", "handback"}


def test_highlights_collapse_duplicates_into_one_plain_sentence_and_cap_at_six(client):
    from awos import repo
    from awos.engine import actions
    _project("要点项目")
    t = _task("整理文件", project="要点项目", level=4, executor="codex")
    rid = _run(t, "codex", summary="整理好了")
    ask = repo.create_approval(title="提交整理结果", kind="action", action_type="git_commit", task_id=t, run_id=rid, options=[{"key": "approve", "label": "批准"}])
    repo.decide_approval(ask, "approve", note="先别推送", via="dashboard")
    repo.set_approval_status(ask, "executed", result={"done": True, "details": "ok"})
    auto = repo.create_approval(title="独立提交脚本", kind="action", action_type="git_commit", task_id=t, run_id=rid, options=[{"key": "approve", "label": "批准"}])
    repo.decide_approval(auto, "approve", via="auto", actor="system")
    repo.set_approval_status(auto, "executed", result={"done": True, "details": "ok"})
    repo.set_task_status(t, "done", "完成", actor="system", force=True)
    broken = _task("会失败的活", project="要点项目", status="ready")
    _run(broken, "claude", status="failed", summary=None, error="boom", error_class="permanent")
    d = _tl(client, "要点项目")
    hl = d["highlights"]
    texts = [h["text"] for h in hl]
    assert any(x.startswith("你批准了（提交代码）：提交整理结果") and x.endswith("，已执行") for x in texts)        # request + decision + "executed" = one sentence
    assert sum(1 for h in hl if h["refs"].get("approval") == ask) == 1 and not any(x.startswith(("请你批准", "已执行")) for x in texts)
    assert any(x == "Agent 自动完成了：独立提交脚本" for x in texts) and any(x.startswith("Agent 完成了：整理文件") for x in texts)
    assert any(h["outcome"] == "failed" and "没成功" in h["text"] for h in hl)                                 # an unrecovered failure is a highlight
    assert not any(x.startswith("Agent 做了一轮：整理文件") for x in texts)                                    # run noise of a finished task is dropped
    for i in range(8):
        actions.cancel_task(_task(f"不要了{i}", project="要点项目"), reason="不需要了")
    hl = _tl(client, "要点项目")["highlights"]
    assert len(hl) == 6 and sum(h["human"] for h in hl) >= 4 and [h["ts"] for h in hl] == sorted((h["ts"] for h in hl), reverse=True)   # same-second events may interleave
    assert all({"ts", "day", "actor", "human", "text", "detail", "outcome", "refs"} <= set(h) for h in hl)
    assert next(p for p in client.get("/api/timeline").json()["projects"] if p["key"] == "要点项目")["last"]["ts"] == hl[0]["ts"]                  # `last` is the newest curated line
    assert {"flow", "days", "milestones", "stats", "health", "state", "asks", "highlights"} <= set(_tl(client, "要点项目"))     # the folded record keeps its data


def test_archived_projects_leave_the_board_but_keep_their_history(client):
    from awos import repo
    _project("旧项目", "已经结束的项目")
    _project("在盯的项目")
    old = _task("旧任务", project="旧项目", status="ready")
    done = _task("旧的完成", project="旧项目", status="done")
    _task("别的", project="在盯的项目", status="running")
    repo.upsert_fact("project", "旧项目", "已经结束的项目", source="user", actor="user", pinned=True, status="archived")
    body = client.get("/api/timeline").json()
    keys = [p["key"] for p in body["projects"]]
    assert "旧项目" not in keys and "在盯的项目" in keys and "__none__" not in keys          # not listed, not in any group, not dumped on the 未归类 pile
    arch = body["archived"]
    assert [(a["key"], a["name"], a["summary"], a["tasks"], a["open"]) for a in arch] == [("旧项目", "旧项目", "已经结束的项目", 2, 1)] and arch[0]["archived_at"]
    d = _tl(client, "旧项目")                                                              # still readable, read-only history
    assert d["archived"] is True and {n["id"] for n in d["flow"]["nodes"] if n["type"] == "task"} == {old, done} and _tl(client, "在盯的项目")["archived"] is False
    repo.upsert_fact("project", "旧项目", "已经结束的项目", source="user", actor="user", pinned=True, status="active")   # restored
    body = client.get("/api/timeline").json()
    assert "旧项目" in [p["key"] for p in body["projects"]] and body["archived"] == [] and _tl(client, "旧项目")["archived"] is False


# --------------------------------------------------------------------------- front end (no browser; node is used when available)

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

DASH = Path(__file__).resolve().parents[1] / "src" / "awos" / "dashboard"
NODE = shutil.which("node")


def test_review_board_is_wired_into_the_board_and_the_dead_ui_is_gone():
    imports = re.findall(r'@import url\("([^"]+)"\)', (DASH / "style.css").read_text(encoding="utf-8"))
    assert imports.index("css/timeline.css") < imports.index("css/glass.css")                      # glass overrides, so it stays last
    board = (DASH / "js" / "views" / "board.js").read_text(encoding="utf-8")
    assert "mode') === 'timeline'" in board and "modeSwitch('board')" in board and "d?.__timeline" in board and "timeline.mount(el, d)" in board
    tl = (DASH / "js" / "views" / "project-timeline.js").read_text(encoding="utf-8")
    assert "api('/timeline')" in tl and "/timeline`" in tl and "'/milestones'" in tl and "#/inbox?sel=" in tl
    for needle in ("需要关注", "进展正常", "暂无动静", "要你处理", "近期要点", "里程碑", "完整记录", "任务关系图", "去处理"):      # the review's vocabulary
        assert needle in tl, needle
    body = tl[tl.index("function reviewHTML"):]
    assert body.index('class="tl-full"') < body.index("graphHTML(d)") < body.index('id="tl-chips"')       # the diagram is folded INSIDE the full record
    assert "tl-archive" in tl and "tl-restore" in tl and "/archive`" in tl and "/restore`" in tl and "已归档" in tl and "close_open" in tl
    assert "key === PILE" in tl and "d.key !== PILE" in tl                                          # the 未归类 pile cannot be archived
    assert ".tl-row" in (DASH / "js" / "shell.js").read_text(encoding="utf-8")                      # joins the navigation-only entrance cascade
    css = (DASH / "css" / "timeline.css").read_text(encoding="utf-8")
    assert "@media (max-width: 900px)" in css and "prefers-reduced-motion" in css and ".tl-group.arch" in css
    for dead in ("tl-stats", "tl-stat-", "tl-exs", "tl-lane", "ms-rail", "tl-main", "tl-head"):                   # tiles, executor line, side list and rail were removed
        assert dead not in tl and dead not in css, dead


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_flow_layout_orders_ranks_wraps_tall_columns_and_survives_cycles():
    flow = (DASH / "js" / "ui" / "flow.js").as_uri()
    code = f"""
import {{ layoutFlow, wrapText, flowSVG }} from {json.dumps(flow)};
const t = (id, extra = {{}}) => ({{ id, type: 'task', title: id, status: 'done', level: 2, ts: id, ...extra }});
const L = layoutFlow({{ nodes: [t('a'), t('b'), t('c'), {{ id: 'g', type: 'gate', title: 'ask', state: 'waiting', task: 'b', ts: 'z' }}],
  edges: [{{ from: 'a', to: 'b', type: 'dep' }}, {{ from: 'b', to: 'c', type: 'dep' }}, {{ from: 'b', to: 'g', type: 'gate' }}] }});
const x = Object.fromEntries(L.nodes.map((n) => [n.id, n.x]));
const many = layoutFlow({{ nodes: Array.from({{ length: 20 }}, (_, i) => t('n' + String(i).padStart(2, '0'))), edges: [] }});
const cyc = layoutFlow({{ nodes: [t('p'), t('q')], edges: [{{ from: 'p', to: 'q', type: 'dep' }}, {{ from: 'q', to: 'p', type: 'dep' }}] }});
const svg = String(flowSVG({{ nodes: [t('<img onerror=1>')], edges: [] }}));
console.log(JSON.stringify({{ order: x.a < x.b && x.b < x.c && x.g > x.b, paths: L.edges.every((e) => e.d.startsWith('M')),
  wrapped: new Set(many.nodes.map((n) => n.x)).size, rows: Math.max(...many.nodes.map((n) => n.y)) < 8 * 80,
  cycle: cyc.nodes.length === 2, w: wrapText('一二三四五六七八九十一二三四五六七八九十', 60, 2), safe: !svg.includes('<img') }}));
"""
    r = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:800]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["order"] and out["paths"] and out["wrapped"] == 3 and out["rows"] and out["cycle"] and out["safe"]
    assert len(out["w"]) == 2 and out["w"][1].endswith("…")
