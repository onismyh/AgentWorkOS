"""Executor hand-off: a task that fails on one executor for a reason another may not share moves on to the next one."""
from __future__ import annotations

from pathlib import Path

import pytest

from tests.test_engine import FakeExecutor, drain, env, exec_result  # noqa: F401  (fixtures + helpers)


@pytest.fixture()
def two(env):
    """Two pay-per-use fake executors; routing 'other'/'research' = fake -> fake2."""
    from awos import executors
    from awos.config import load_config
    import awos.llm as llm
    cfg = load_config().data
    cfg.setdefault("executors", {})["fake2"] = {"enabled": True, "max_concurrent": 2}
    cfg["routing"]["other"] = ["fake", "fake2"]
    cfg["routing"]["research"] = ["fake", "fake2"]
    cfg["routing"]["readonly"] = ["fake", "fake2"]      # 'other' at L1/L2 is cheap-routed to readonly / write
    cfg["routing"]["write"] = ["fake", "fake2"]
    fake2 = FakeExecutor()
    fake2.name = "fake2"
    executors.register(fake2)
    llm._avail_cache.clear()
    env["fake2"] = fake2
    return env


def _run_one(env, title="任务", category="other", level=2):
    from awos import repo
    from awos.engine.actions import add_manual_task
    tid = add_manual_task(title, "d", level=level, category=category)
    drain(4)
    return repo.get_task(tid)


def test_budget_failure_hands_off_to_the_next_executor_instead_of_asking(two):
    from awos import repo
    from awos.executors.base import RunResult
    two["fake"].push("execute", RunResult(status="failed", error="budget exhausted: x", error_class="budget"))
    two["fake2"].push("execute", lambda spec: exec_result(summary="由 fake2 完成" if "执行器切换" in spec.prompt else "没有交接说明"))
    t = _run_one(two)
    assert t["status"] == "waiting_user" and t["summary"] == "由 fake2 完成"                 # L2 hand-off point; fake2 saw the hand-off note
    assert repo.list_approvals(task_id=t["id"]) == []                                         # no budget approval was needed
    assert [(h["from"], h["to"], h["reason"]) for h in t["meta"]["handoffs"]] == [("fake", "fake2", "budget")]
    ev = [e for e in repo.list_events("task", t["id"], limit=50) if e["type"] == "handoff"]
    assert ev and ev[0]["data"]["to"] == "fake2" and t["attempts"] == 1                       # one attempt, two executors
    assert t["meta"]["last_executor"] == "fake2"
    assert [r["executor"] for r in sorted(repo.list_runs(task_id=t["id"]), key=lambda r: r["id"]) if r["purpose"] == "execute"] == ["fake", "fake2"]


def test_transient_failure_also_hands_off_but_permanent_does_not(two):
    from awos.executors.base import RunResult
    two["fake"].push("execute", RunResult(status="failed", error="429 rate limit", error_class="transient"))
    two["fake2"].push("execute", exec_result(summary="ok"))
    t = _run_one(two, "t1")
    assert t["meta"]["last_executor"] == "fake2" and t["status"] == "waiting_user"
    two["fake"].push("execute", RunResult(status="failed", error="invalid schema", error_class="permanent"))
    two["fake2"].push("execute", exec_result(summary="never used"))
    t2 = _run_one(two, "t2")
    assert t2["status"] == "failed" and len(two["fake2"].script["execute"]) == 1               # permanent: no hand-off, fake2 untouched


def test_every_candidate_failing_ends_in_the_normal_failure_path(two):
    from awos import repo
    from awos.executors.base import RunResult
    bad = RunResult(status="failed", error="budget exhausted: x", error_class="budget")
    two["fake"].push("execute", bad)
    two["fake2"].push("execute", bad)
    t = _run_one(two)
    assert t["status"] == "waiting_approval" and repo.list_approvals(status="pending", task_id=t["id"])[0]["kind"] == "budget"


def test_filter_candidates_applies_mail_and_pi_rules():
    from awos.engine.worker import filter_candidates
    allc = ["codex", "pi", "dsh", "claude"]
    assert filter_candidates(allc, sandbox="read-only", n_mail=0) == ["codex", "pi", "dsh", "claude"]
    assert filter_candidates(allc, sandbox="workspace-write", n_mail=0) == ["codex", "dsh", "claude"]   # Pi has no sandbox: read-only only
    assert filter_candidates(allc, sandbox="read-only", n_mail=2) == ["codex", "claude"]                # mail never goes to DeepSeek


def test_pi_write_is_opt_in(env):
    from awos.config import load_config
    from awos.engine.worker import filter_candidates
    load_config().data.setdefault("executors", {}).setdefault("pi", {})["allow_write"] = True
    assert filter_candidates(["pi", "codex"], sandbox="workspace-write", n_mail=0) == ["pi", "codex"]


def test_choose_route_sends_cheap_categories_to_the_cheap_routes(env):
    from awos.engine.worker import choose_route
    t = lambda cat: {"category": cat}  # noqa: E731
    assert choose_route(t("research"), "read-only", 0, None) == "readonly"
    assert choose_route(t("research"), "workspace-write", 0, None) == "write"
    assert choose_route(t("writing"), "read-only", 0, None) == "readonly"                    # analysis of prose is cheap
    assert choose_route(t("writing"), "workspace-write", 0, None) == "writing"               # producing prose stays with its own route
    assert choose_route(t("career"), "read-only", 0, None) == "career"                       # personal categories are not cheap-routed
    assert choose_route(t("research"), "read-only", 1, None) == "research"                   # mail-derived: normal route (codex/claude only)
    assert choose_route(t("research"), "read-only", 0, "claude") == "research"               # an explicit executor choice wins


def test_budget_and_concurrency_defaults_were_raised():
    from awos.config import reset_config_cache, load_config
    reset_config_cache()
    c = load_config()
    assert c.get("budget.daily_usd") == 15.0 and c.get("budget.per_task_usd") == 5.0 and c.get("budget.codex_daily_runs") == 90
    assert c.get("scheduler.max_concurrent_runs") == 4 and c.get("executors.dsh.max_concurrent") == 2
    assert c.get("routing.write") == ["codex", "dsh", "claude"] and c.get("executors.pi.allow_write") is False


def test_handoff_appears_in_the_topic_history(two):
    from awos import repo
    from awos.brain import topics
    from awos.executors.base import RunResult
    two["fake"].push("execute", RunResult(status="failed", error="budget exhausted", error_class="budget"))
    two["fake2"].push("execute", exec_result(summary="ok"))
    t = _run_one(two, category="other")
    key = topics.resolve([t])[t["id"]][0]
    texts = [h["text"] for h in topics.history(key)]
    assert any("从 fake 交给 fake2 继续做（预算不足）" in x for x in texts)
