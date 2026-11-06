"""The project guardian: troubled projects get one read-only analysis task, bounded so it never turns into noise."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from tests.test_engine import env  # noqa: F401  (fixture)


def lane(key, level, reason="6 天没动静", name=None):
    return {"key": key, "name": name or key, "state": f"{key} 的现状", "health": {"level": level, "label": {"risk": "有风险", "stalled": "停滞", "attention": "要你处理", "ok": "正常", "quiet": "暂无动静"}[level], "reason": reason}}


@pytest.fixture()
def lanes(env, monkeypatch):
    box = {"projects": []}
    monkeypatch.setattr("awos.api.timeline.lanes", lambda: box)
    return box


def test_only_risk_and_stalled_projects_get_a_task(lanes):
    from awos import repo
    from awos.engine import guardian
    lanes["projects"] = [lane("论文", "risk", "11月20日截止，进度 40%"), lane("申请", "attention"), lane("模型", "stalled"), lane("杂项", "ok"), lane("旧事", "quiet"),
                         lane("__none__", "stalled")]
    out = guardian.review_projects()
    assert [c["project"] for c in out["created"]] == ["论文", "模型"] and out["skipped"] == []
    t = repo.get_task(out["created"][0]["task"])
    assert (t["source"], t["category"], t["level"], t["status"], t["project"], t["priority"]) == ("guardian", "pm", 2, "ready", "论文", 1)
    assert "11月20日截止" in t["description"] and "不要改动任何文件" in t["description"] and t["meta"]["level_locked"]
    assert repo.get_task(out["created"][1]["task"])["priority"] == 2                      # stalled is less urgent than at risk
    assert repo.get_digests("task", [t["id"]])[t["id"]]["headline"].startswith("论文有风险")   # the inbox row reads well without any LLM call


def test_at_most_three_per_run_and_never_two_open_ones_for_a_project(lanes):
    from awos.engine import guardian
    lanes["projects"] = [lane(f"项目{i}", "risk") for i in range(5)]
    first = guardian.review_projects()
    assert len(first["created"]) == 3 and {s["why"] for s in first["skipped"]} == {"今天已经派了 3 个"}
    second = guardian.review_projects()                                                    # three still open, the other two now fit
    assert [c["project"] for c in second["created"]] == ["项目3", "项目4"]
    assert {s["why"] for s in second["skipped"]} == {"上一次巡检任务还没处理完"}


def test_quiet_period_after_a_finished_or_dismissed_task(lanes):
    from awos import db
    from awos.engine import guardian
    lanes["projects"] = [lane("论文", "stalled")]
    now = datetime.now(timezone.utc)
    tid = guardian.review_projects(now)["created"][0]["task"]

    def later(days, status):                      # the task finished with `status`; the guardian runs again `days` days after it was made
        db.update("tasks", "id", tid, {"status": status})
        return guardian.review_projects(now + timedelta(days=days))

    assert later(3, "done")["skipped"][0]["why"] == "7 天内已经查过"
    again = later(8, "done")["created"]
    assert len(again) == 1 and again[0]["task"] != tid
    db.update("tasks", "id", tid, {"created_at": "2026-01-01T00:00:00Z"})      # real runs are days apart; keep this test's two rows ordered
    tid, now = again[0]["task"], now + timedelta(days=8)
    assert later(10, "dismissed")["skipped"][0]["why"] == "21 天内已经查过"               # the user waved it away: back off longer
    assert len(later(22, "dismissed")["created"]) == 1


def test_runs_once_a_day_from_the_configured_hour(env):
    from awos import repo
    from awos.engine import guardian
    at = lambda h: datetime(2026, 11, 7, h, 5)
    assert not guardian.due(at(9)) and guardian.due(at(11))
    repo.kv_set("guardian.last_day", "2026-11-07")
    assert not guardian.due(at(15))
    assert guardian.due(datetime(2026, 11, 8, 11, 0))


def test_analysis_tasks_are_routed_to_the_cheap_executors(lanes, monkeypatch):
    import awos.llm as llm
    from awos import repo
    from awos.brain import policy
    from awos.engine import guardian
    from awos.engine.router import route_task
    monkeypatch.setattr(llm, "usable", lambda names: [n for n in names if n in ("codex", "claude", "pi", "dsh")])
    monkeypatch.setattr(llm, "budget_fraction", lambda n: 0.0)
    lanes["projects"] = [lane("模型", "stalled")]
    t = repo.get_task(guardian.review_projects()["created"][0]["task"])
    r = route_task(t, sandbox=policy.decide(t).sandbox)
    assert r.tier == "economy" and r.order[0] == "dsh"
