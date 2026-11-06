"""The whiteboard (standing rules every agent sees) and the git rule it carries: agents may commit/push branches by default,
protected branches and MRs/PRs wait for the human, and delivery leaves no intermediate files behind."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

from tests.test_api import H, client, env  # noqa: F401  (fixtures: API + DB)
from tests.test_engine import _approve_action, _git_repo
from tests.test_engine import env as git_env  # noqa: F401  (engine fixture: fake executors + a work root under tmp)


def _sh(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=True).stdout.strip()


def _proposed(atype: str, payload: dict, title: str = "x") -> list[str]:
    """What the worker does with a result that proposes one action; returns the created approval ids."""
    from awos import repo
    from awos.engine.actions import add_manual_task
    from awos.hitl import outbox
    task = repo.get_task(add_manual_task("t", "d", level=3, category="other"))
    out = {"proposed_actions": [{"type": atype, "title": title, "description": "d", "risk": "low", "payload_json": json.dumps(payload)}]}
    outbox.create_from_result(task, "run_x", out)
    return [a["id"] for a in repo.list_approvals(task_id=task["id"], limit=10)]


def _repo_with_remote(git_env) -> tuple[Path, Path, str]:
    r = git_env["tmp"] / "proj"; r.mkdir()
    _git_repo(r)
    remote = git_env["tmp"] / "remote.git"
    subprocess.run(["git", "init", "--bare", "-q", str(remote)], check=True, capture_output=True)
    _sh(r, "remote", "add", "origin", remote.as_posix())
    return r, remote, _sh(r, "rev-parse", "--abbrev-ref", "HEAD")


# --------------------------------------------------------------------------- whiteboard


def test_whiteboard_is_seeded_once_pinned_and_leads_every_prompt(client):
    from awos import repo
    from awos.brain import whiteboard
    from awos.brain.context import context_brief, render_profile
    repo.upsert_fact("goal", "毕业", "按期毕业", source="user", actor="user")
    assert whiteboard.ensure_defaults() == 2 and whiteboard.ensure_defaults() == 0       # seeded once
    rules = repo.list_facts(kind="rule")
    assert {f["key"] for f in rules} == {"Git 交付权限", "交付时清理中间产物"} and all(f["pinned"] for f in rules)
    brief = context_brief(max_chars=200)                                                  # even a tiny budget keeps the whole whiteboard
    assert brief.startswith("### 白板") and "open_mr" in brief and "中间产物" in brief and brief.index("白板") < brief.index("毕业")
    assert "## 白板·共同规则" in render_profile()
    repo.delete_fact(rules[0]["id"])                                                      # the user may drop a rule ...
    assert whiteboard.ensure_defaults() == 0 and len(repo.list_facts(kind="rule")) == 1   # ... and it is not re-added


def test_agents_can_read_but_never_write_the_whiteboard(client):
    from awos import repo
    from awos.brain import whiteboard
    from awos.brain.context import agent_write_fact, apply_updates
    whiteboard.ensure_defaults()
    before = {f["key"]: f["value"] for f in repo.list_facts(kind="rule")}
    assert agent_write_fact("rule", "Git 交付权限", "随便推 main", source="t", confidence=0.9, actor="agent", may_define_projects=False) is None
    assert agent_write_fact("rule", "新规则", "x", source="t", confidence=0.9, actor="agent", may_define_projects=True) is None
    assert apply_updates([{"kind": "rule", "key": "k", "value": "v"}], "task") == 0
    assert {f["key"]: f["value"] for f in repo.list_facts(kind="rule")} == before


def test_whiteboard_is_editable_from_the_context_api_and_listed_first(client):
    d = client.get("/api/context").json()
    assert next(iter(d["kinds"])) == "rule"
    r = client.post("/api/context", json={"kind": "rule", "key": "沟通", "value": "对外沟通先问我"}, headers=H)
    assert r.status_code == 201 and r.json()["fact"]["pinned"] == 1
    assert [f["key"] for f in client.get("/api/context").json()["groups"]["rule"]] == ["沟通"]


def test_whiteboard_endpoint_lists_the_rules_for_the_workbench(client):
    from awos.brain import whiteboard
    assert client.get("/api/whiteboard").json()["rules"] == []
    whiteboard.ensure_defaults()
    d = client.get("/api/whiteboard").json()
    assert d["title"].startswith("白板") and [r["key"] for r in d["rules"]] == ["Git 交付权限", "交付时清理中间产物"]
    assert all(r["value"] and r["id"] for r in d["rules"])


# --------------------------------------------------------------------------- git rule


def test_branch_commit_and_push_run_without_asking_and_leave_the_default_branch_alone(git_env):
    r, remote, main = _repo_with_remote(git_env)
    (r / "a.txt").write_text("2", encoding="utf-8")
    ids = _proposed("git_commit", {"repo": str(r), "branch": "agent/fix-a", "paths": ["a.txt"], "message": "fix a"})
    assert len(ids) == 1
    from awos import repo as db
    a = db.get_approval(ids[0])
    assert a["status"] == "executed" and a["decided_via"] == "auto", a
    assert _sh(r, "rev-parse", "--abbrev-ref", "HEAD") == main                            # the user's checkout never moves
    assert _sh(r, "status", "--porcelain") == "M a.txt" and _sh(r, "diff", "--cached", "--name-only") == ""
    assert _sh(r, "log", "--format=%s", "agent/fix-a").splitlines()[0] == "fix a"
    assert _sh(r, "show", "agent/fix-a:a.txt") == "2" and _sh(r, "worktree", "list").count("\n") == 0   # worktree gone
    assert _sh(r, "log", "--format=%s", main).splitlines() == ["init"]                        # main untouched
    ids = _proposed("git_push", {"repo": str(r), "remote": "origin", "branch": "agent/fix-a"})
    assert db.get_approval(ids[0])["status"] == "executed", db.get_approval(ids[0])["result"]
    assert "agent/fix-a" in _sh(remote, "branch", "--list")


def test_protected_branch_and_odd_refspecs_still_need_the_human(git_env):
    from awos import repo as db
    r, remote, main = _repo_with_remote(git_env)
    (r / "a.txt").write_text("3", encoding="utf-8")
    cases = [("git_commit", {"repo": str(r), "paths": ["a.txt"], "message": "m"}),                    # would commit onto main
             ("git_commit", {"repo": str(r), "branch": main, "paths": ["a.txt"], "message": "m"}),
             ("git_push", {"repo": str(r), "remote": "origin", "branch": main}),
             ("git_push", {"repo": str(r), "remote": "origin", "branch": "HEAD:main"}),                 # refspec
             ("git_push", {"repo": str(r), "remote": "origin", "branch": "+agent/x"}),                  # force prefix
             ("git_push", {"repo": str(r), "remote": "--upload-pack=x", "branch": "agent/x"})]
    for atype, payload in cases:
        (aid,) = _proposed(atype, payload)
        a = db.get_approval(aid)
        assert a["status"] == "pending" and a["decided_via"] is None, (atype, payload)
    assert _sh(r, "log", "--format=%s", main).splitlines() == ["init"] and _sh(r, "branch", "--list", "agent/*") == ""
    assert "agent/x" not in _sh(remote, "branch", "--list")


def test_opening_an_mr_always_waits_for_the_user_and_never_merges(git_env, monkeypatch):
    from awos import repo as db
    from awos.hitl import gitops
    r, _, _ = _repo_with_remote(git_env)
    (aid,) = _proposed("open_mr", {"repo": str(r), "branch": "agent/fix-a", "title": "Fix a", "body": "why"}, title="发起 MR")
    a = db.get_approval(aid)
    assert a["status"] == "pending" and a["action_type"] == "open_mr"
    calls = []
    monkeypatch.setattr(gitops, "gh_exe", lambda: Path("gh.exe"))
    monkeypatch.setattr(gitops, "_run_gh", lambda args, cwd: calls.append(args) or subprocess.CompletedProcess(
        args, 0, "https://github.com/me/proj/pull/7\n", ""))
    done = _approve_action("open_mr", {"repo": str(r), "branch": "agent/fix-a", "title": "Fix a", "body": "why"})
    assert done["status"] == "executed" and done["result"]["url"].endswith("/pull/7")
    assert calls[0][1:3] == ["pr", "create"] and "merge" not in " ".join(calls[0])
    bad = _approve_action("open_mr", {"repo": str(r), "branch": "HEAD:main", "title": "t"})
    assert bad["status"] == "failed"


def test_auto_approval_is_rechecked_at_execution_and_can_be_switched_off(git_env, monkeypatch):
    from awos import repo as db
    from awos.brain import policy
    from awos.hitl import outbox
    r, _, main = _repo_with_remote(git_env)
    (r / "a.txt").write_text("4", encoding="utf-8")
    payload = {"repo": str(r), "branch": main, "paths": ["a.txt"], "message": "m"}
    aid = db.create_approval(title="c", kind="action", action_type="git_commit", options=[{"key": "approve", "label": "ok"}],
                             payload={**payload, "_hash": outbox.payload_hash(payload)})
    db.decide_approval(aid, "approve", via="auto", actor="system")                    # an auto sign-off must not unlock main
    outbox.execute_action(db.get_approval(aid))
    assert db.get_approval(aid)["status"] == "failed" and _sh(r, "log", "--format=%s", main).splitlines() == ["init"]
    monkeypatch.setattr(policy, "git_policy", lambda: {"auto_branch_actions": False, "protected_branches": ["main", "master"]})
    (aid2,) = _proposed("git_commit", {"repo": str(r), "branch": "agent/z", "paths": ["a.txt"], "message": "m"})
    assert db.get_approval(aid2)["status"] == "pending"                                # switched off -> everything asks again


def test_commits_refuse_intermediate_files(git_env):
    r, _, _ = _repo_with_remote(git_env)
    for name in ("draft.bak", "notes.tmp", "a.txt.orig", ".DS_Store", "__pycache__/x.pyc", "node_modules/p/i.js"):
        (r / Path(name).parent).mkdir(parents=True, exist_ok=True)
        (r / name).write_text("x", encoding="utf-8")
        a = _approve_action("git_commit", {"repo": str(r), "paths": [name], "message": "m"})
        assert a["status"] == "failed", name
    (r / "final.md").write_text("ok", encoding="utf-8")
    assert _approve_action("git_commit", {"repo": str(r), "paths": ["final.md"], "message": "m"})["status"] == "executed"


def test_prompts_tell_agents_the_new_rules():
    from awos.brain import prompts
    text = prompts.EXECUTE + prompts.SECURITY_RULES + prompts.VERIFY
    for needle in ("open_mr", "agent/<主题> 分支不需要用户批准", "独立工作区", "交付前清理", "中间产物", "不能自己创建或合并 MR"):
        assert needle in text, needle


# --------------------------------------------------------------------------- review fixes: ways around the rule


def test_ref_spellings_of_the_default_branch_still_need_the_human(git_env):
    from awos import repo as db
    from awos.hitl import gitops
    r, remote, main = _repo_with_remote(git_env)
    _sh(r, "push", "-q", "origin", main)
    for name in ("HEAD", "refs/heads/" + main, "heads/" + main, main.upper(), "@"):
        assert not gitops.valid_branch(name) or name == main.upper(), name
        (aid,) = _proposed("git_push", {"repo": str(r), "remote": "origin", "branch": name})
        assert db.get_approval(aid)["status"] == "pending", name
    (r / "a.txt").write_text("5", encoding="utf-8")
    _sh(r, "commit", "-qam", "local only")
    assert _sh(remote, "log", "--format=%s", main).splitlines() == ["init"]               # remote main never moved


def test_auto_commit_lists_files_and_never_ships_secrets(git_env):
    from awos import repo as db
    r, remote, main = _repo_with_remote(git_env)
    (r / ".env.local").write_text("TOKEN=s3cret", encoding="utf-8")
    (r / "sub").mkdir()
    (r / "sub" / "id_rsa").write_text("key", encoding="utf-8")
    (r / "sub" / "ok.md").write_text("ok", encoding="utf-8")
    for paths in (["."], ["*"], [":/"], ["sub"]):                                          # whole tree / patterns / dirs
        (aid,) = _proposed("git_commit", {"repo": str(r), "branch": "agent/x", "paths": paths, "message": "m"})
        assert db.get_approval(aid)["status"] != "executed", paths
    assert _sh(r, "branch", "--list", "agent/*") == "" and _sh(r, "rev-parse", "--abbrev-ref", "HEAD") == main
    for paths in (["."], ["sub"]):                                                          # even when a human approves it
        a = _approve_action("git_commit", {"repo": str(r), "paths": paths, "message": "m"})
        assert a["status"] == "failed", paths
    assert _sh(r, "log", "--format=%s", main).splitlines() == ["init"] and _sh(r, "diff", "--cached", "--name-only") == ""
    a = _approve_action("git_commit", {"repo": str(r), "paths": ["sub/ok.md"], "message": "m"})
    assert a["status"] == "executed" and _sh(r, "show", "--name-only", "--format=", "HEAD") == "sub/ok.md"


def test_auto_git_waits_behind_an_earlier_pending_action(git_env):
    from awos import repo as db
    from awos.engine.actions import add_manual_task
    from awos.hitl import outbox
    r, _, main = _repo_with_remote(git_env)
    (r / "a.txt").write_text("6", encoding="utf-8")
    task = db.get_task(add_manual_task("t", "d", level=3, category="other"))
    acts = [{"type": "send_email", "title": "first", "description": "d", "risk": "low", "payload_json": "{}"},
            {"type": "git_commit", "title": "then commit", "description": "d", "risk": "low",
             "payload_json": json.dumps({"repo": str(r), "branch": "agent/y", "paths": ["a.txt"], "message": "m"})}]
    outbox.create_from_result(task, "run_y", {"proposed_actions": acts})
    by = {a["title"]: a for a in db.list_approvals(task_id=task["id"], limit=10)}
    assert by["first"]["status"] == "pending" and by["then commit"]["status"] == "pending"
    assert _sh(r, "branch", "--list", "agent/*") == ""


def test_auto_check_errors_fall_back_to_asking(git_env, monkeypatch):
    from awos import repo as db
    from awos.hitl import gitops
    r, _, _ = _repo_with_remote(git_env)

    def boom(*a, **k):
        raise subprocess.TimeoutExpired("git", 1)

    monkeypatch.setattr(gitops, "default_branch", boom)
    (aid,) = _proposed("git_push", {"repo": str(r), "remote": "origin", "branch": "agent/z"})
    assert db.get_approval(aid)["status"] == "pending" and "TimeoutExpired" in db.get_approval(aid)["context_md"]


def test_policy_keeps_approval_expiry_under_actions():
    from awos.brain import policy
    assert policy.approval_expiry_hours("high") == 48 and policy.approval_expiry_hours("low") == 72
    assert policy.git_policy()["protected_branches"] == ["main", "master"]


def test_only_agent_branches_run_without_asking(git_env):
    from awos import repo as db
    r, _, main = _repo_with_remote(git_env)
    _sh(r, "branch", "feature/mine")
    (r / "a.txt").write_text("7", encoding="utf-8")
    for branch in ("feature/mine", "agentx", "agent/", "fix-a"):
        (aid,) = _proposed("git_commit", {"repo": str(r), "branch": branch, "paths": ["a.txt"], "message": "m"})
        assert db.get_approval(aid)["status"] == "pending", branch
    assert _sh(r, "log", "--format=%s", "feature/mine").splitlines() == ["init"]


def _task_with(**fields):
    from awos import repo
    from awos.engine.actions import add_manual_task
    tid = add_manual_task("t", "d", level=fields.pop("level", 3), category=fields.pop("category", "other"))
    if fields.get("meta"):
        repo.merge_task_meta(tid, **fields.pop("meta"))
    if fields:
        repo.update_task(tid, **fields)
    return repo.get_task(tid)


def test_sensitive_tasks_always_ask(git_env):
    from awos import db as rawdb
    from awos import repo as db
    from awos.hitl import gitops, outbox
    r, _, _ = _repo_with_remote(git_env)
    (r / "a.txt").write_text("8", encoding="utf-8")
    rawdb.insert("signals", {"id": "sig_mail", "source": "mailmaster", "fingerprint": "f1", "title": "邮件", "payload": "{}",
                             "observed_at": rawdb.utcnow()})
    cases = {"L2": _task_with(level=2), "private": _task_with(category="career"),
             "mail-routed": _task_with(meta={"routing": {"trusted_only": True}}),
             "from mail": _task_with(meta={"signals": ["sig_mail"]}), "github": _task_with(source="sensor:github")}
    payload = {"repo": str(r), "branch": "agent/s", "paths": ["a.txt"], "message": "m"}
    for name, task in cases.items():
        ok, why = gitops.auto_allowed("git_commit", payload, task=task)
        assert not ok and why, name
        outbox.create_from_result(task, "run_s", {"proposed_actions": [{"type": "git_commit", "title": name, "description": "d",
                                                                         "risk": "low", "payload_json": json.dumps(payload)}]})
        (a,) = db.list_approvals(task_id=task["id"], limit=5)
        assert a["status"] == "pending" and "需要你批准" in a["context_md"], name
    assert gitops.auto_allowed("git_commit", payload, task=_task_with())[0]                      # an ordinary L3 task may
    assert _sh(r, "branch", "--list", "agent/*") == ""


def test_untouched_default_rule_is_updated_but_user_edits_stay(client):
    from awos import repo
    from awos.brain import whiteboard
    old = whiteboard.PREVIOUS_DEFAULTS["Git 交付权限"][0]
    whiteboard.ensure_defaults()
    repo.upsert_fact("rule", "Git 交付权限", old, source="user", actor="user", pinned=True)       # as seeded by an older version
    whiteboard.ensure_defaults()
    assert "agent/<主题> 分支可以直接" in {f["key"]: f["value"] for f in whiteboard.rules()}["Git 交付权限"]
    repo.upsert_fact("rule", "Git 交付权限", "我自己的写法", source="user", actor="user", pinned=True)
    whiteboard.ensure_defaults()
    assert {f["key"]: f["value"] for f in whiteboard.rules()}["Git 交付权限"] == "我自己的写法"
