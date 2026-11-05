"""End-to-end engine tests with a scripted fake executor (no model calls)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import pytest


ROUTES = ["triage", "plan", "verify", "context", "dev", "research", "writing", "doc", "data", "pm", "schedule", "comms",
          "admin", "career", "learning", "system", "other", "readonly", "write", "brief"]


@pytest.fixture()
def env(tmp_path, monkeypatch):
    cfg = tmp_path / "test.toml"
    cfg.write_text(f"""
[paths]
var = "{(tmp_path / 'var').as_posix()}"
workspaces = "{(tmp_path / 'var/workspaces').as_posix()}"
artifacts = "{(tmp_path / 'var/artifacts').as_posix()}"
logs = "{(tmp_path / 'var/logs').as_posix()}"
runs = "{(tmp_path / 'var/runs').as_posix()}"
backups = "{(tmp_path / 'var/backups').as_posix()}"
context = "{(tmp_path / 'var/context').as_posix()}"
inbox_drop = "{(tmp_path / 'var/inbox-drop').as_posix()}"
[executors.codex]
enabled = false
[executors.claude]
enabled = false
[executors.fake]
enabled = true
max_concurrent = 4
[routing]
{chr(10).join(f'{k} = ["fake"]' for k in ROUTES)}
[scheduler]
retry_backoff_seconds = [0, 0, 0]
[policy.permissions]
readable_roots = ["{tmp_path.as_posix()}"]
[notify]
digest_times = []
""" + "".join(f"[sensors.{n}]\nenabled = false\n" for n in ("git", "files", "agent_history", "zotero", "inbox_drop",
                                                              "gmail", "notion", "mailmaster")),
        encoding="utf-8")
    monkeypatch.setenv("AWOS_CONFIG", str(cfg))
    monkeypatch.setenv("AWOS_DB", str(tmp_path / "awos.db"))
    from awos.config import reset_config_cache
    from awos.brain.policy import reset_policy_cache
    reset_config_cache(); reset_policy_cache()
    import awos.llm as llm
    llm._avail_cache.clear(); llm._exec_slots.clear()
    from awos import repo as _repo
    _repo.kv_set("context.auto", False)
    from awos import executors
    fake = FakeExecutor()
    executors.register(fake)
    # notifications are best-effort; capture them
    sent: list[tuple] = []
    import awos.hitl.notify as notify_mod
    monkeypatch.setattr(notify_mod, "notify", lambda *a, **k: sent.append((a, k)) or [], raising=False)
    yield {"fake": fake, "tmp": tmp_path, "sent": sent}
    reset_config_cache(); reset_policy_cache()


from awos.executors.base import Executor, RunResult  # noqa: E402


class FakeExecutor(Executor):
    name = "fake"

    def __init__(self) -> None:
        self.script: dict[str, list] = {}
        self.calls: list[dict] = []
        self.lock = threading.Lock()

    def available(self):
        return True, "fake"

    def push(self, purpose: str, *results) -> None:
        self.script.setdefault(purpose, []).extend(results)

    def run(self, spec, on_event, cancel):
        with self.lock:
            self.calls.append({"purpose": spec.purpose, "prompt": spec.prompt, "cwd": str(spec.cwd), "sandbox": spec.sandbox})
            queue = self.script.get(spec.purpose) or []
            item = queue.pop(0) if queue else None
        on_event("tool_call", "fake_tool", {"purpose": spec.purpose})
        if callable(item):
            item = item(spec)
        if item is None:
            return RunResult(status="failed", error="no scripted result", error_class="permanent")
        if isinstance(item, RunResult):
            return item
        return RunResult(status="succeeded", structured=item, final_text=json.dumps(item, ensure_ascii=False), cost_usd=0.01)


def exec_result(**kw) -> dict:
    base = {"status": "completed", "summary": "done", "deliverables": [], "verification": {"performed": True, "method": "check", "passed": True, "details": ""},
            "proposed_actions": [], "decisions_needed": [], "subtasks": [], "followups": [], "context_updates": []}
    base.update(kw)
    return base


def drain(max_ticks: int = 30):
    from awos.engine.scheduler import Scheduler
    s = Scheduler()
    s.bg_jobs = {}
    import time
    for _ in range(max_ticks):
        s.tick()
        for f in list(s.active.values()) + list(s.bg_jobs.values()):
            f.result(timeout=30)
        s._reap()
        time.sleep(0.05)
    return s


def test_signal_to_l3_review_with_deliverable(env):
    from awos import repo
    fake = env["fake"]
    sid = repo.add_signal("git", "仓库 paper 有 12 个未提交改动", "fp1", body="dirty since 3 days")

    fake.push("triage", lambda spec: {"items": [{"ref_ids": [sid], "action": "create_task", "title": "整理并提交 paper 仓库改动",
                                                 "description": "d", "kind": "issue", "category": "dev", "priority": 1, "level": 3,
                                                 "level_reason": "可以生成提交建议", "complexity": "simple", "autostart": True,
                                                 "acceptance": "有提交说明草稿", "reason": "r"}]})

    def do_exec(spec):
        Path(spec.cwd, "commit_plan.md").write_text("# plan", encoding="utf-8")
        return exec_result(summary="生成了提交计划", deliverables=[{"path": "commit_plan.md", "title": "提交计划", "kind": "report"}],
                           context_updates=[{"kind": "project", "key": "paper", "value": "正在修改论文代码"}])
    fake.push("execute", do_exec)
    drain(6)
    tasks = repo.list_tasks()
    assert len(tasks) == 1
    t = tasks[0]
    assert t["status"] == "review", t
    assert t["level"] == 3
    arts = repo.list_artifacts(task_id=t["id"])
    assert any(a["title"] == "提交计划" for a in arts)
    # a task agent may only report PROGRESS: it becomes a rolling note, it never creates a project
    assert repo.list_facts(kind="project") == []
    assert [f["key"] for f in repo.list_facts(kind="note")] == ["进展·paper"]
    exec_call = [c for c in fake.calls if c["purpose"] == "execute"][0]
    assert exec_call["sandbox"] == "workspace-write"
    assert "<untrusted>" in exec_call["prompt"]
    assert repo.list_signals()[0]["status"] == "triaged"
    assert repo.run_events(repo.list_runs(task_id=t["id"])[0]["id"])  # tool calls recorded


def test_action_needs_approval_then_executes_apply(env):
    from awos import repo
    from awos.engine.actions import add_manual_task, decide
    fake, tmp = env["fake"], env["tmp"]
    proj = tmp / "proj"; proj.mkdir()
    (proj / "notes.md").write_text("old", encoding="utf-8")
    tid = add_manual_task("更新项目笔记", "把笔记更新为新版", level=4, category="doc")

    def do_exec(spec):
        Path(spec.cwd, "notes.md").write_text("new", encoding="utf-8")
        payload = {"files": [{"src": "notes.md", "dest": str(proj / "notes.md")}]}
        return exec_result(deliverables=[{"path": "notes.md", "title": "新笔记", "kind": "draft"}],
                           proposed_actions=[{"type": "apply_to_project", "title": "写回项目笔记", "description": "覆盖 notes.md",
                                              "risk": "low", "payload_json": json.dumps(payload)}])
    fake.push("execute", do_exec)
    drain(5)
    t = repo.get_task(tid)
    assert t["status"] == "waiting_approval"
    pend = repo.list_approvals(status="pending", task_id=tid)
    assert len(pend) == 1 and pend[0]["action_type"] == "apply_to_project"
    assert (proj / "notes.md").read_text(encoding="utf-8") == "old"   # nothing happened before approval
    decide(pend[0]["id"], "approve", note="ok")
    import time
    for _ in range(50):
        if repo.get_approval(pend[0]["id"])["status"] in ("executed", "failed"):
            break
        time.sleep(0.1)
    a = repo.get_approval(pend[0]["id"])
    assert a["status"] == "executed", a
    assert (proj / "notes.md").read_text(encoding="utf-8") == "new"
    assert list(proj.glob("notes.awos-bak-*.md"))  # backup kept
    time.sleep(0.2)
    assert repo.get_task(tid)["status"] == "review"
    with pytest.raises(ValueError):
        decide(pend[0]["id"], "approve")  # once only


def test_complex_task_decomposes_runs_children_and_synthesizes(env):
    from awos import repo
    from awos.engine.actions import add_manual_task
    fake = env["fake"]
    tid = add_manual_task("写一篇综述", "检索+写作", level=3, category="research")
    repo.merge_task_meta(tid, complexity="complex")
    fake.push("plan", {"decompose": True, "rationale": "r", "synthesis": "merge", "subtasks": [
        {"key": "s1", "title": "检索文献", "description": "", "category": "research", "level": 3, "depends_on": [], "acceptance": "a"},
        {"key": "s2", "title": "整理数据", "description": "", "category": "data", "level": 3, "depends_on": [], "acceptance": "a"},
        {"key": "s3", "title": "写初稿", "description": "", "category": "writing", "level": 5, "depends_on": ["s1", "s2"], "acceptance": "a"}]})
    fake.push("execute", exec_result(summary="s?"), exec_result(summary="s?"), exec_result(summary="s3 done"),
              exec_result(summary="综述完成"))
    drain(12)
    kids = repo.children(tid)
    assert len(kids) == 3 and all(k["status"] == "done" for k in kids), [(k["title"], k["status"]) for k in kids]
    s3 = [k for k in kids if k["title"] == "写初稿"][0]
    assert s3["level"] == 3  # capped by parent
    parent = repo.get_task(tid)
    assert parent["status"] == "review", parent["status_reason"]
    # s3 ran after s1/s2 and parent synthesis prompt includes child results
    synth = [c for c in fake.calls if c["purpose"] == "execute"][-1]
    assert "子任务成果" in synth["prompt"]


def test_transient_failure_retries_then_fails(env):
    from awos import repo
    from awos.engine.actions import add_manual_task
    fake = env["fake"]
    tid = add_manual_task("会失败的任务", level=2, category="research")
    fail = RunResult(status="failed", error="429 rate limit", error_class="transient")
    fake.push("execute", fail, fail, fail)
    drain(8)
    t = repo.get_task(tid)
    assert t["status"] == "failed" and t["attempts"] == 3


def test_decision_l4_resumes_with_choice(env):
    from awos import repo
    from awos.engine.actions import add_manual_task, decide
    fake = env["fake"]
    tid = add_manual_task("选择期刊投稿", level=4, category="writing")
    fake.push("execute", exec_result(decisions_needed=[{"question": "投哪个期刊？", "context": "c", "recommendation": "a",
                                                        "options": [{"key": "a", "label": "Applied Energy", "consequence": ""},
                                                                    {"key": "b", "label": "Energy", "consequence": ""}]}]))
    drain(4)
    pend = repo.list_approvals(status="pending", task_id=tid)
    assert repo.get_task(tid)["status"] == "waiting_approval" and pend[0]["kind"] == "decision"
    decide(pend[0]["id"], "a")
    assert repo.get_task(tid)["status"] == "ready"
    fake.push("execute", lambda spec: exec_result(summary="按 Applied Energy 格式整理" if "Applied Energy" in spec.prompt else "missing"))
    drain(4)
    t = repo.get_task(tid)
    assert t["status"] == "review" and "Applied Energy" in t["summary"]


def test_recovery_requeues_orphaned_running_task(env):
    from awos import repo
    from awos.engine.recovery import startup_recovery
    tid = repo.create_task("orphan", status="ready", level=2)
    claimed = repo.claim_next_task("old-owner:1", 1)
    rid = repo.create_run("execute", "fake", task_id=tid)
    repo.update_run(rid, status="running", pid=999999)
    st = startup_recovery("new-owner:2")
    assert st["tasks"] == 1 and st["runs"] == 1
    assert repo.get_task(tid)["status"] == "ready"
    assert repo.get_run(rid)["status"] == "interrupted"


def test_cancel_while_running_is_respected(env):
    from awos import repo
    from awos.engine.actions import add_manual_task, cancel_task
    fake = env["fake"]
    tid = add_manual_task("长任务", level=3, category="dev")

    def slow(spec):
        cancel_task(tid)  # human cancels mid-run
        return exec_result()
    fake.push("execute", slow)
    drain(3)
    assert repo.get_task(tid)["status"] == "cancelled"


def test_fencing_rejects_stale_worker(env):
    from awos import repo
    tid = repo.create_task("fenced", status="ready", level=2)
    repo.claim_next_task("worker-A", 60)
    repo.set_task_status(tid, "ready", "recovered", force=True)        # recovery re-queues
    repo.claim_next_task("worker-B", 60)                               # B takes over
    with pytest.raises(repo.LeaseLost):
        repo.set_task_status(tid, "done", "stale A result", expect_owner="worker-A")
    assert repo.get_task(tid)["lease_owner"] == "worker-B"
    repo.set_task_status(tid, "review", "B result", expect_owner="worker-B")
    assert repo.get_task(tid)["status"] == "review"


def test_extra_cannot_override_status_and_claim_respects_max_attempts(env):
    from awos import repo
    tid = repo.create_task("x", status="ready", level=1, max_attempts=1)
    repo.claim_next_task("w", 60)
    repo.set_task_status(tid, "ready", "again", extra={"status": "done", "lease_owner": "evil"}, force=True)
    t = repo.get_task(tid)
    assert t["status"] == "ready" and t["lease_owner"] is None
    assert repo.claim_next_task("w", 60) is None   # attempts(1) >= max_attempts(1)
    with pytest.raises(ValueError):
        repo.update_task(tid, status="done")


def test_expired_approval_cannot_be_approved(env):
    from awos import repo
    aid = repo.create_approval(title="t", kind="action", action_type="send_email",
                               options=[{"key": "approve", "label": "ok"}, {"key": "reject", "label": "no"}],
                               expires_at="2000-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        repo.decide_approval(aid, "approve")
    from awos.hitl.outbox import expire_approvals
    expire_approvals()
    assert repo.get_approval(aid)["status"] == "expired"


def test_cancel_after_approval_before_execution_withdraws_action(env):
    from awos import repo
    from awos.engine.actions import cancel_task
    tid = repo.create_task("send something", status="ready", level=4)
    aid = repo.create_approval(title="send", kind="action", action_type="send_email", task_id=tid,
                               options=[{"key": "approve", "label": "ok"}, {"key": "reject", "label": "no"}],
                               payload={"to": "x@example.com"})
    repo.decide_approval(aid, "approve")          # approved, runner not started yet
    cancel_task(tid)
    assert repo.get_approval(aid)["status"] == "cancelled"
    from awos.hitl.outbox import execute_action
    assert execute_action(repo.get_approval(aid)) == {"skipped": "not in approved state"}


def test_artifact_outside_var_rejected(env, tmp_path):
    from awos import repo
    outside = tmp_path.parent / "secret.txt"
    with pytest.raises(ValueError):
        repo.add_artifact("bad", path=outside)


def test_cli_style_approval_is_picked_up_by_scheduler_tick(env, monkeypatch):
    """A short-lived process must not run the action in a thread that dies with it; the daemon tick executes it."""
    from awos import repo
    from awos.engine.actions import add_manual_task, decide
    from awos.hitl import outbox
    fake, tmp = env["fake"], env["tmp"]
    proj = tmp / "proj2"; proj.mkdir()
    tid = add_manual_task("写入笔记", "x", level=4, category="doc")

    def do_exec(spec):
        Path(spec.cwd, "n.md").write_text("hi", encoding="utf-8")
        payload = {"files": [{"src": "n.md", "dest": str(proj / "n.md")}]}
        return exec_result(deliverables=[{"path": "n.md", "title": "n", "kind": "draft"}],
                           proposed_actions=[{"type": "apply_to_project", "title": "apply", "description": "d",
                                              "risk": "low", "payload_json": json.dumps(payload)}])
    fake.push("execute", do_exec)
    drain(5)
    pend = repo.list_approvals(status="pending", task_id=tid)[0]
    monkeypatch.setattr(outbox, "EXECUTE_INLINE", False)
    decide(pend["id"], "approve", via="cli")
    import time
    time.sleep(0.3)
    assert repo.get_approval(pend["id"])["status"] == "approved" and not (proj / "n.md").exists()  # nothing ran inline
    drain(4)  # scheduler ticks pick it up
    for _ in range(50):
        if repo.get_approval(pend["id"])["status"] == "executed":
            break
        time.sleep(0.1)
    assert repo.get_approval(pend["id"])["status"] == "executed" and (proj / "n.md").read_text(encoding="utf-8") == "hi"


def test_mail_tasks_never_reach_deepseek_executors(env, monkeypatch, tmp_path):
    """A task built from mail signals gets its mails exported and is kept off pi/dsh (incl. the verifier)."""
    from awos import mailstore, repo
    from awos.engine.actions import add_manual_task
    fake = env["fake"]
    seen = {}
    monkeypatch.setattr(mailstore, "export_for_task", lambda task, ws: ("## 相关邮件原文\n- x", 1))
    tid = add_manual_task("回复邮件", "d", level=3, category="comms", executor="dsh")

    import awos.llm as llm
    real_cands = llm.executor_candidates

    def spy(route, preferred=None):
        seen.setdefault("calls", []).append((route, preferred))
        return real_cands(route, preferred=preferred)
    monkeypatch.setattr(llm, "executor_candidates", spy)
    fake.push("execute", lambda spec: exec_result(summary="ok") if "相关邮件原文" in spec.prompt else exec_result(summary="NO MAIL BLOCK"))
    drain(4)
    assert repo.get_task(tid)["summary"] == "ok"
    assert all(p != "dsh" for _, p in seen["calls"])


def _git_repo(path: Path) -> None:
    import subprocess
    for args in (["init", "-q"], ["config", "user.email", "t@example.com"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True)
    (path / "a.txt").write_text("1", encoding="utf-8")
    subprocess.run(["git", "-C", str(path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(path), "commit", "-q", "-m", "init"], check=True, capture_output=True)


def _approve_action(atype: str, payload: dict) -> dict:
    from awos import repo
    from awos.hitl.outbox import payload_hash
    aid = repo.create_approval(title=atype, kind="action", action_type=atype,
                               options=[{"key": "approve", "label": "ok"}, {"key": "reject", "label": "no"}],
                               payload={**payload, "_hash": payload_hash(payload)})
    repo.decide_approval(aid, "approve")
    from awos.hitl.outbox import execute_action
    execute_action(repo.get_approval(aid))
    return repo.get_approval(aid)


def test_git_commit_action_commits_only_listed_paths(env):
    import subprocess
    r = env["tmp"] / "gitproj"; r.mkdir()
    _git_repo(r)
    (r / "a.txt").write_text("2", encoding="utf-8")
    (r / "b.txt").write_text("new", encoding="utf-8")       # not listed -> must stay uncommitted
    a = _approve_action("git_commit", {"repo": str(r), "paths": ["a.txt"], "message": "update a"})
    assert a["status"] == "executed", a["result"]
    log = subprocess.run(["git", "-C", str(r), "log", "--oneline"], capture_output=True, text=True).stdout
    assert "update a" in log
    status = subprocess.run(["git", "-C", str(r), "status", "--short"], capture_output=True, text=True).stdout
    assert "b.txt" in status and "a.txt" not in status


def test_git_commit_action_refuses_dangerous_inputs(env):
    r = env["tmp"] / "gitproj2"; r.mkdir()
    _git_repo(r)
    (r / ".env").write_text("SECRET=1", encoding="utf-8")
    for payload in ({"repo": str(r), "paths": [".env"], "message": "m"},
                    {"repo": str(r), "paths": ["../outside.txt"], "message": "m"},
                    {"repo": str(r), "paths": [], "message": "m"},
                    {"repo": str(r), "paths": ["a.txt"], "message": ""},
                    {"repo": str(env["tmp"].parent), "paths": ["a.txt"], "message": "m"}):
        a = _approve_action("git_commit", payload)
        assert a["status"] == "failed", payload


def test_budget_reservations_wait_but_real_overspend_fails(env, monkeypatch):
    import awos.llm as llm
    from awos import repo
    from awos.config import load_config
    cap = float(load_config().get("budget.daily_usd"))               # follow the configured daily cap
    repo.record_cost(None, "fake", None, cap - 3.0)                 # real spend so far: $3 below the cap
    llm._reserved["other"], llm._inflight["other"] = 2.5, "fake"    # another run is in flight, reserving $2.5
    assert llm.reserve_budget("me", "fake", 1.0)[0] == "wait"       # fits the day, just not next to the other run
    llm.release_budget("other")
    assert llm.reserve_budget("me", "fake", 1.0)[0] == "ok"
    llm.release_budget("me")
    repo.record_cost(None, "fake", None, 2.5)                       # real spend now cap-0.5: one more $1 run would exceed the cap
    assert llm.reserve_budget("me2", "fake", 1.0)[0] == "budget"


def test_run_agent_gives_transient_not_budget_error_when_only_reservations_block(env, monkeypatch):
    import awos.llm as llm
    from awos.config import load_config
    monkeypatch.setattr(llm, "RESERVE_WAIT_S", 0.2)
    monkeypatch.setattr(llm, "RESERVE_POLL_S", 0.05)
    llm._reserved["other"], llm._inflight["other"] = float(load_config().get("budget.daily_usd")) - 0.1, "fake"
    try:
        run_id, res = llm.run_agent("x", purpose="execute", executor="fake")
    finally:
        llm.release_budget("other")
    assert res.status == "failed" and res.error_class == "capacity" and "reservations" in res.error


def test_typical_cost_tracks_recent_runs_and_respects_the_cap(env):
    import awos.llm as llm
    from awos import repo
    assert llm.typical_cost("fake", "execute", 1.5) == 1.5                     # no history: reserve the full per-run cap
    for c in (0.4, 0.5, 0.6):
        rid = repo.create_run("execute", "fake")
        repo.update_run(rid, status="succeeded", cost_usd=c, started_at=repo.utcnow())
    assert llm.typical_cost("fake", "execute", 1.5) == 1.0                     # 2 x avg(0.5)
    assert llm.typical_cost("fake", "execute", 0.8) == 0.8                     # never above the per-run cap


def test_capacity_wait_requeues_without_using_a_retry_and_eventually_asks_the_human(env):
    from awos import repo
    from awos.engine.actions import add_manual_task
    from awos.engine.worker import fail_or_retry
    from awos.executors.base import RunResult  # noqa: F401
    tid = add_manual_task("等额度的任务", level=2, category="research")
    repo.set_task_status(tid, "ready", "t", force=True)
    claimed = repo.claim_next_task("w", 60)
    assert claimed["attempts"] == 1
    fail_or_retry(claimed, "w", "waited for budget reservations", "capacity")
    t = repo.get_task(tid)
    assert t["status"] == "ready" and t["attempts"] == 0 and t["meta"]["capacity_waits"] == 1     # attempt handed back
    assert t["next_attempt_at"] and "不计入重试" in t["status_reason"]
    repo.merge_task_meta(tid, capacity_waits=30)                                                    # ~1 hour of waiting already
    repo.db.execute("UPDATE tasks SET next_attempt_at = NULL WHERE id = ?", (tid,))
    claimed = repo.claim_next_task("w", 60)
    fail_or_retry(claimed, "w", "waited for budget reservations", "capacity")
    assert repo.get_task(tid)["status"] == "waiting_approval" and repo.list_approvals(status="pending", task_id=tid)[0]["kind"] == "budget"
