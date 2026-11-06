"""Executor routing by importance x difficulty x trust (engine/router.py): who gets the work, and why."""
from __future__ import annotations

import pytest

from tests.test_engine import drain, env, exec_result  # noqa: F401  (fixtures/helpers)

ALL = ["codex", "claude", "pi", "dsh"]


def task(**kw) -> dict:
    t = {"category": "dev", "level": 2, "priority": 2, "due_at": None, "description": "d", "attempts": 0, "meta": {}, "executor": None,
         "source": "sensor:git"}
    t.update(kw)
    return t


@pytest.fixture()
def real_executors(env, monkeypatch):
    """Pretend all four real executors are enabled and have budget left (tests never run real ones)."""
    import awos.llm as llm
    monkeypatch.setattr(llm, "usable", lambda names: [n for n in names if n in ALL])
    monkeypatch.setattr(llm, "budget_fraction", lambda name: 0.0)
    return llm


def route(t, sandbox="workspace-write", n_mail=0):
    from awos.engine.router import route_task
    return route_task(t, sandbox=sandbox, n_mail=n_mail)


def test_importance_from_priority_deadline_and_stakes(env):
    from datetime import timedelta
    from awos.engine.router import importance
    from awos.timeutil import local_now
    soon = (local_now() + timedelta(days=2)).strftime("%Y-%m-%d")
    later = (local_now() + timedelta(days=30)).strftime("%Y-%m-%d")
    assert importance(task(priority=0))[0] == "high" and importance(task(priority=1))[0] == "normal"   # P1 = this week: routine
    assert importance(task(due_at=soon))[0] == "high" and importance(task(due_at="2020-01-01"))[1] == "已过截止日期"
    assert importance(task(due_at=later))[0] == "normal" and importance(task())[0] == "normal"
    assert importance(task(priority=3))[0] == "low"
    assert importance(task(category="career", level=3))[0] == "high"       # the result reaches other people
    assert importance(task(category="career", level=2))[0] == "normal"     # analysis for yourself does not


def test_difficulty_from_triage_estimate_level_size_and_escalation(env):
    from awos.engine.router import difficulty
    ro, rw = "read-only", "workspace-write"
    assert difficulty(task(level=1), ro)[0] == "easy" and difficulty(task(level=2, category="research"), ro)[0] == "easy"
    assert difficulty(task(level=3, category="writing"), rw)[0] == "medium"
    assert difficulty(task(level=4, category="dev"), rw)[0] == "hard"                    # L4 + code changes
    assert difficulty(task(level=1, meta={"difficulty": "hard"}), ro)[0] == "hard"       # triage's own estimate wins over the level
    assert difficulty(task(level=1, meta={"difficulty": "easy"}), ro)[0] == "easy"
    assert difficulty(task(level=2, category="research", description="x" * 2500), ro)[0] == "medium"
    base = task(level=2, category="research")
    assert difficulty({**base, "attempts": 3}, ro)[0] == "medium"                        # failed twice: step up
    assert difficulty({**base, "meta": {"last_feedback": "redo"}}, ro)[0] == "medium"    # reworked: step up


def test_tiers_decide_who_goes_first(real_executors):
    easy_ro = route(task(level=2, category="research"), sandbox="read-only")
    assert (easy_ro.tier, easy_ro.order) == ("economy", ["dsh", "pi", "codex", "claude"])            # routine analysis: DeepSeek first
    easy_rw = route(task(level=3, category="dev", meta={"difficulty": "easy"}))
    assert (easy_rw.tier, easy_rw.order) == ("economy", ["dsh", "codex", "claude"])                  # Pi never gets write work
    std = route(task(level=3, category="dev"))
    assert (std.tier, std.order[0]) == ("standard", "codex")
    prem = route(task(level=4, category="writing", priority=0))
    assert (prem.tier, prem.order) == ("premium", ["claude", "codex", "dsh"])
    assert route(task(level=3, category="doc", priority=0)).tier == "premium"                        # important + medium
    assert route(task(level=3, category="doc", priority=1)).tier == "standard"                       # this week + medium
    assert route(task(level=1, category="research", priority=3), sandbox="read-only").tier == "economy"
    assert route(task(level=4, category="dev", priority=3)).tier == "standard"                       # hard but unimportant


def test_affinity_hint_and_user_choice(real_executors):
    # economy: affinity and a pricier hint are ignored, the work is routine
    r = route(task(level=2, category="writing", meta={"executor_hint": "claude"}), sandbox="read-only")
    assert r.tier == "economy" and r.order[0] == "dsh"
    # standard: the hint counts, and the category's best executor goes first otherwise
    assert route(task(level=3, category="dev", meta={"executor_hint": "claude"})).order[0] == "claude"
    assert route(task(level=3, category="writing")).order[0] == "claude"
    # legacy tasks kept the suggestion in the executor column: still only a suggestion
    assert route(task(level=3, category="dev", executor="claude", source="sensor:git")).order[0] == "claude"
    assert route(task(level=2, category="research", executor="claude", source="sensor:git"), sandbox="read-only").order[0] == "dsh"
    # the user's own choice always goes first, in every tier
    r = route(task(level=2, category="research", executor="claude", meta={"executor_pinned": True}), sandbox="read-only")
    assert r.order[0] == "claude" and any("你指定" in x for x in r.reasons)
    assert route(task(level=2, category="research", executor="dsh", source="manual"), sandbox="read-only").order[0] == "dsh"


def test_mail_and_personal_categories_stay_with_trusted_executors(real_executors):
    assert route(task(level=2, category="research"), sandbox="read-only", n_mail=2).order == ["codex", "claude"]
    assert route(task(level=2, category="career"), sandbox="read-only").order == ["codex", "claude"]
    pinned = route(task(level=2, category="research", executor="dsh", source="manual"), sandbox="read-only", n_mail=1)
    assert "dsh" not in pinned.order and pinned.order[0] == "codex"                                  # even a user pin cannot send mail to DeepSeek
    assert any("邮件" in x for x in pinned.reasons)


def test_budget_pressure_reorders_but_never_drops(real_executors, monkeypatch):
    frac = {"codex": 1.0, "claude": 0.0, "pi": 0.0, "dsh": 0.0}
    monkeypatch.setattr(real_executors, "budget_fraction", lambda n: frac[n])
    r = route(task(level=3, category="dev"))                                                         # standard: codex would lead
    assert r.order == ["dsh", "claude", "codex"] and any("额度已用完" in x for x in r.reasons)
    frac.update(codex=0.9, claude=0.0, dsh=0.0)
    assert route(task(level=3, category="dev")).order[-1] == "codex"                                  # nearly used up: used last
    important = route(task(level=3, category="dev", priority=0))                                      # important: a nearly used-up codex is not demoted
    assert important.tier == "premium" and important.order == ["codex", "claude", "dsh"]


def test_falls_back_to_the_category_route_when_the_tier_has_nobody(env, monkeypatch):
    import awos.llm as llm
    monkeypatch.setattr(llm, "usable", lambda names: [])
    r = route(task(level=3, category="dev"))
    assert r.order == ["fake"] and any("类别默认路线" in x for x in r.reasons)


def test_every_run_records_why_it_went_where_it_went(env):
    from awos import repo
    from awos.engine.actions import add_manual_task
    env["fake"].push("execute", lambda spec: exec_result(summary="done"))
    tid = add_manual_task("整理材料", "d", level=3, category="dev", priority=0, triage=False)
    drain(3)
    routing = repo.get_task(tid)["meta"]["routing"]
    assert routing["importance"] == "high" and routing["tier"] in ("standard", "premium") and routing["order"] == ["fake"]
    assert routing["reasons"] and routing["reasons"][0].startswith("重要性")


def test_user_choice_is_pinned_while_triage_and_planner_hints_stay_soft(env):
    from awos import repo
    from awos.brain import planner
    from awos.engine.actions import add_manual_task, set_task_fields
    tid = add_manual_task("t", "d", executor="pi", triage=False)
    assert repo.get_task(tid)["meta"]["executor_pinned"] is True
    other = add_manual_task("t2", "d", triage=False)
    assert not repo.get_task(other)["meta"].get("executor_pinned")
    set_task_fields(other, executor="claude")
    assert repo.get_task(other)["meta"]["executor_pinned"] is True
    set_task_fields(other, executor="")                                   # "自动分配": back to the router's decision
    t = repo.get_task(other)
    assert not t["meta"].get("executor_pinned") and not t["executor"]
    parent = repo.get_task(add_manual_task("p", "d", level=3, triage=False))
    ids = planner.create_subtasks(parent, [{"key": "s1", "title": "a", "description": "d", "category": "dev", "level": 2, "depends_on": [],
                                            "acceptance": "ok", "executor_hint": "codex", "difficulty": "hard"}], "run_x")
    sub = repo.get_task(ids[0])
    assert sub["executor"] is None and sub["meta"]["executor_hint"] == "codex" and sub["meta"]["difficulty"] == "hard"


def test_verifier_is_the_cheapest_independent_executor_but_never_deepseek_for_private_work(real_executors, monkeypatch, tmp_path):
    import types
    from awos.engine import worker
    used = []
    monkeypatch.setattr(real_executors, "pick_executor", lambda route, preferred=None: preferred)
    monkeypatch.setattr(real_executors, "run_agent", lambda *a, **k: used.append(k["executor"]) or ("r", types.SimpleNamespace(
        status="succeeded", structured={"passed": True, "score": 9})))
    t = {"id": "t", "title": "t", "acceptance": "a", "attempts": 1}
    worker._verify(t, tmp_path, {"summary": "s"}, "codex")
    worker._verify(t, tmp_path, {"summary": "s"}, "dsh")
    worker._verify(t, tmp_path, {"summary": "s"}, "codex", trusted_only=True)
    assert used == ["dsh", "pi", "claude"]       # never the executor itself; private work only to claude/codex
