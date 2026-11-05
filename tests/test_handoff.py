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


def test_ids_created_in_the_same_second_sort_in_creation_order():
    from awos.db import new_id
    ids = [new_id("run") for _ in range(300)]
    assert ids == sorted(ids) and len(set(ids)) == 300
    assert all(len(i) == len("run_") + 12 + 3 + 3 for i in ids)


# --------------------------------------------------------------------------- background jobs fall back too


def test_background_run_falls_back_to_the_next_executor_of_its_route(two):
    from awos import db, llm
    from awos.executors.base import RunResult
    two["fake"].push("digest", RunResult(status="failed", error="exceeded retry limit, last status: 429 Too Many Requests", error_class="transient"))
    two["fake2"].push("digest", exec_result(summary="由 fake2 完成"))
    run_id, res = llm.run_agent("p", purpose="digest", route="other", check_budget=False)
    assert res.status == "succeeded" and db.q1("SELECT executor FROM runs WHERE id = ?", (run_id,))["executor"] == "fake2"
    assert [r["executor"] for r in db.q("SELECT executor FROM runs WHERE purpose = 'digest' ORDER BY id")] == ["fake", "fake2"]


def test_background_fallback_skips_permanent_failures_and_explicit_executors(two):
    from awos import llm
    from awos.executors.base import RunResult
    two["fake"].push("digest", RunResult(status="failed", error="bad schema", error_class="permanent"))
    two["fake2"].push("digest", exec_result(summary="never used"))
    _, res = llm.run_agent("p", purpose="digest", route="other", check_budget=False)
    assert res.status == "failed" and len(two["fake2"].script["digest"]) == 1                  # permanent: no fallback
    two["fake"].push("digest", RunResult(status="failed", error="429", error_class="transient"))
    _, res = llm.run_agent("p", purpose="digest", executor="fake", check_budget=False)
    assert res.status == "failed" and len(two["fake2"].script["digest"]) == 1                  # explicit executor: exactly one run


def test_no_fallback_run_is_started_while_the_daemon_shuts_down(two):
    from awos import llm
    from awos.executors.base import RunResult
    two["fake"].push("digest", RunResult(status="failed", error="exit 1", error_class="transient"))
    two["fake2"].push("digest", exec_result(summary="never used"))
    llm._shutdown.set()
    try:
        _, res = llm.run_agent("p", purpose="digest", route="other", check_budget=False)
    finally:
        llm._shutdown.clear()
    assert res.status == "failed" and len(two["fake2"].script["digest"]) == 1


# --------------------------------------------------------------------------- 异常 page diagnosis


def _failed_run(purpose="digest", executor="claude", error="x", cls="transient", status="failed", task_id=None):
    from awos import repo
    rid = repo.create_run(purpose, executor, task_id=task_id)
    repo.update_run(rid, status=status, ended_at="2026-11-05T10:00:00Z", error=error, error_class=cls)
    return rid


def test_failures_are_classified_in_plain_language(env):
    from awos.api.diagnose import classify
    c = lambda **r: classify({"status": "failed", **r})  # noqa: E731
    assert c(error="budget exhausted: spent today $7.42 > $8.00", error_class="budget") == "budget"
    assert c(error="waited for budget reservations to free up", error_class="capacity") == "queue"
    assert c(error='{"message": "exceeded retry limit, last status: 429 Too Many Requests"}') == "upstream"
    assert c(error="Selected model is at capacity. Please try a different model.") == "upstream"
    assert c(error="Reconnecting... 5/5 (workspace routing discovery timed out)") == "network"
    assert c(error="exit 1", error_class="transient") == "killed"
    assert c(error="invalid schema", error_class="permanent") == "permanent"
    assert classify({"status": "interrupted", "error": "daemon restarted"}) == "restart"


def test_a_failure_is_resolved_once_a_later_run_of_the_same_kind_succeeded(env):
    from awos import repo
    from awos.api import diagnose
    from awos.db import q1
    old = _failed_run(error="429 Too Many Requests")
    still = _failed_run(purpose="brief", error="at capacity")                                    # nothing succeeded after this one
    ok = repo.create_run("digest", "claude")
    repo.update_run(ok, status="succeeded", ended_at="2026-11-05T11:00:00Z")
    rows = [q1("SELECT * FROM runs WHERE id = ?", (i,)) for i in (old, still)]
    diagnose.annotate(rows)
    assert rows[0]["diagnosis"]["resolved"] is True and rows[0]["diagnosis"]["kind"] == "upstream"
    assert rows[1]["diagnosis"]["resolved"] is False and rows[1]["diagnosis"]["hint"]


def test_open_failure_count_ignores_recovered_failures(env):
    from awos import repo
    from awos.api import diagnose
    from awos.db import utcnow
    _failed_run(error="429 Too Many Requests")                                                   # recovered below
    ok = repo.create_run("digest", "claude")
    repo.update_run(ok, status="succeeded", ended_at=utcnow())
    _failed_run(purpose="brief", error="at capacity")                                           # still open
    assert diagnose.open_failure_count("2000-01-01T00:00:00Z") == 1
