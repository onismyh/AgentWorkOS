"""Sensor tests: real tiny git repos / files / SQLite fixtures in tmp dirs, a temp AWOS_DB, and a monkeypatched
awos.llm.run_agent for the connector sensors. All data here is synthetic."""
from __future__ import annotations

import json
import logging
import os
import sqlite3
import subprocess
import tempfile
import time
from datetime import timedelta
from pathlib import Path

os.environ.setdefault("AWOS_DB", os.path.join(tempfile.gettempdir(), "awos-test-sensors.db"))  # before awos imports

import httpx  # noqa: E402
import pytest  # noqa: E402

from awos.executors.base import RunResult  # noqa: E402
from awos.sensors.base import SensorContext  # noqa: E402

DAY = 86400


def ctx(config: dict | None = None, cursor: dict | None = None) -> SensorContext:
    return SensorContext(config=config or {}, cursor=cursor if cursor is not None else {}, log=logging.getLogger("test"))


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated config (var + inbox-drop under tmp, git sensor pointed at tmp/roots) and temp DB."""
    from awos import db
    from awos.config import reset_config_cache

    roots = tmp_path / "roots"
    roots.mkdir()
    cfg = tmp_path / "test.toml"
    cfg.write_text(f"""
[paths]
var = "{(tmp_path / 'var').as_posix()}"
runs = "{(tmp_path / 'var/runs').as_posix()}"
inbox_drop = "{(tmp_path / 'inbox').as_posix()}"
[sensors.git]
roots = ["{roots.as_posix()}"]
max_depth = 3
[sensors.inbox_drop]
enabled = true
""", encoding="utf-8")
    monkeypatch.setenv("AWOS_CONFIG", str(cfg))
    monkeypatch.setenv("AWOS_DB", str(tmp_path / "awos.db"))
    reset_config_cache()
    yield {"tmp": tmp_path, "roots": roots}
    db.close_thread_conns()
    reset_config_cache()


# --------------------------------------------------------------------------- git

def _git(repo: Path, *args: str, when: float | None = None) -> str:
    e = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com",
         "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.com"}
    if when:
        e["GIT_AUTHOR_DATE"] = e["GIT_COMMITTER_DATE"] = f"@{int(when)} +0000"
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, env=e, text=True).stdout


def _repo(path: Path, when: float | None = None) -> Path:
    path.mkdir(parents=True)
    _git(path, "-c", "init.defaultBranch=main", "init", "-q")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-qm", "init", when=when)
    return path


def make_repos(roots: Path) -> dict[str, Path]:
    now = time.time()
    dirty = _repo(roots / "proj" / "dirty", when=now - 10 * DAY)
    bare = roots / "remote.git"
    _git(roots, "init", "-q", "--bare", str(bare))
    _git(dirty, "remote", "add", "origin", str(bare))
    _git(dirty, "push", "-q", "-u", "origin", "main")
    (dirty / "a.txt").write_text("changed\n", encoding="utf-8")
    os.utime(dirty / "a.txt", (now - 3 * DAY, now - 3 * DAY))

    ahead = _repo(roots / "ahead", when=now - 5 * DAY)
    _git(ahead, "remote", "add", "origin", str(bare))
    _git(ahead, "fetch", "-q", "origin")
    _git(ahead, "branch", "-q", "--set-upstream-to=origin/main")
    _git(ahead, "reset", "-q", "--hard", "origin/main")
    (ahead / "b.txt").write_text("b\n", encoding="utf-8")
    _git(ahead, "add", "b.txt")
    _git(ahead, "commit", "-qm", "local work", when=now - 2 * DAY)

    _repo(roots / "local_only", when=now - 3600)              # recent commits, no remote -> backup risk
    (roots / "stub" / ".git").mkdir(parents=True)             # empty .git dir -> not a repo
    _repo(roots / "node_modules" / "pkg")                     # skipped dir
    return {"dirty": dirty, "ahead": ahead}


def test_git_sensor_conditions_and_idempotence(env):
    from awos.sensors.git import SENSOR, discover

    roots = env["roots"]
    make_repos(roots)
    found = {Path(p).name for p in discover([str(roots)], 3)}
    assert found == {"dirty", "ahead", "local_only"}

    c = ctx({"roots": [str(roots)], "max_depth": 3, "stale_dirty_hours": 48, "stale_unpushed_hours": 24})
    drafts = SENSOR.collect(c)
    conds = {(Path(d.payload["repo"]).name, d.payload["condition"]) for d in drafts}
    assert conds == {("dirty", "dirty_stale"), ("ahead", "unpushed"), ("local_only", "no_remote")}
    unp = next(d for d in drafts if d.payload["condition"] == "unpushed")
    assert unp.payload["ahead"] == 1 and unp.payload["upstream"] == "origin/main"
    discovered_at = c.cursor["discovered_at"]
    again = SENSOR.collect(c)                                  # cached repo list, same fingerprints
    assert c.cursor["discovered_at"] == discovered_at
    assert sorted(d.fingerprint for d in again) == sorted(d.fingerprint for d in drafts)
    snap = SENSOR.context_snapshot(c)
    assert snap and "| 仓库 |" in snap and "local_only" in snap


def test_git_sensor_respects_thresholds(env):
    from awos.sensors.git import SENSOR

    make_repos(env["roots"])
    drafts = SENSOR.collect(ctx({"roots": [str(env["roots"])], "max_depth": 3, "stale_dirty_hours": 24 * 7,
                                 "stale_unpushed_hours": 24 * 7}))
    assert {d.payload["condition"] for d in drafts} == {"no_remote"}


def test_git_via_runner_persists_cursor_and_dedupes(env):
    from awos import repo
    from awos.sensors import run_sensor

    make_repos(env["roots"])
    first = run_sensor("git")
    assert first["ok"] and first["new"] == 3
    assert run_sensor("git")["new"] == 0
    st = repo.sensor_state("git")
    assert len(st["cursor"]["repos"]) == 3 and st["consecutive_failures"] == 0


# --------------------------------------------------------------------------- files

def test_deadline_hint():
    from datetime import date

    from awos.sensors.files import deadline_hint

    today = date(2026, 11, 5)
    assert deadline_hint("博士答辩PPT.pptx", today)["keyword"] == "答辩"
    assert deadline_hint("abstract_DDL.docx", today)["keyword"] == "ddl"
    assert deadline_hint("汇报_2026-11-20.pptx", today)["date"] == "2026-11-20"
    assert deadline_hint("组会_1120.pptx", today)["date"] == "2026-11-20"
    assert deadline_hint("notes_0105.md", date(2026, 12, 20))["date"] == "2027-01-05"   # wraps to next year
    assert deadline_hint("参数索引_20260930.md", today) is None               # past date, no keyword
    assert deadline_hint("fig_v1120.png", today) is None                      # letter-prefixed digits
    assert deadline_hint("chapter2.tex", today) is None


def test_files_sensor_groups_and_skips(tmp_path):
    from awos.sensors.files import SENSOR

    root = tmp_path / "Paper work"
    now = time.time()

    def mk(rel: str, age_days: float = 0.0) -> Path:
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("x", encoding="utf-8")
        os.utime(p, (now - age_days * DAY, now - age_days * DAY))
        return p

    mk("thesis/chap1.tex")
    mk("thesis/figs/plot.pdf")
    mk("thesis/~$draft.docx")                # office lock file
    mk("thesis/node_modules/readme.md")      # skipped dir
    mk("thesis/old.docx", age_days=30)       # outside lookback
    mk("thesis/data.json")                   # extension not watched
    mk("defense/答辩PPT.pptx")
    mk("top.md")
    c = ctx({"roots": [str(root)], "extensions": [".docx", ".pptx", ".pdf", ".md", ".tex"], "lookback_days": 7,
             "max_depth": 5})
    drafts = SENSOR.collect(c)
    active = {d.payload["label"]: d for d in drafts if "count" in d.payload}
    assert set(active) == {"Paper work/thesis", "Paper work/defense", "Paper work"}
    assert active["Paper work/thesis"].payload["count"] == 2
    assert {f["path"] for f in active["Paper work/thesis"].payload["files"]} == {"chap1.tex", os.path.join("figs", "plot.pdf")}
    hints = [d for d in drafts if "hint" in d.payload]
    assert len(hints) == 1 and hints[0].payload["hint"]["keyword"] == "答辩"
    assert sorted(d.fingerprint for d in SENSOR.collect(c)) == sorted(d.fingerprint for d in drafts)
    assert "Paper work/thesis" in (SENSOR.context_snapshot(c) or "")


def test_self_exclusion():
    from awos.config import ROOT
    from awos.sensors._common import self_excluded
    from awos.sensors.files import scan
    from awos.sensors.git import discover

    assert self_excluded(ROOT) and self_excluded(ROOT / "docs" / "x.md") and self_excluded(ROOT / "var" / "runs")
    assert not self_excluded(ROOT.parent)
    assert discover([str(ROOT)], 3) == [] and scan([str(ROOT)], {".md"}, 0, 3, []) == []


# --------------------------------------------------------------------------- agent_history

def _write_history(path: Path, rows: list[dict]) -> None:
    with path.open("a", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def _agent_fixture(tmp_path: Path) -> dict:
    from awos.config import ROOT

    now = time.time()
    hist = tmp_path / "history.jsonl"
    rows = [{"display": "old prompt", "pastedContents": {}, "timestamp": int((now - 20 * DAY) * 1000), "project": "D:\\work\\projA", "sessionId": "s0"}]
    for i in range(4):
        rows.append({"display": f"请帮我修改第{i}章的图", "pastedContents": {"1": {"content": "PASTED-SECRET-BLOB"}},
                     "timestamp": int((now - 300 + i) * 1000), "project": "D:\\work\\projA", "sessionId": "sessA"})
    rows += [
        {"display": "/clear", "pastedContents": {}, "timestamp": int(now * 1000) - 200, "project": "D:\\work\\projA", "sessionId": "sessA"},
        {"display": "use token: sk-abcdefghijklmnopqrstuvwxyz123456 please", "pastedContents": {},
         "timestamp": int(now * 1000) - 100, "project": "D:\\work\\projB", "sessionId": "sessB"},
        {"display": "self run", "pastedContents": {}, "timestamp": int(now * 1000) - 50, "project": str(ROOT / "var" / "runs" / "x"),
         "sessionId": "self"},
    ]
    _write_history(hist, rows)

    codex = tmp_path / "codex"
    codex.mkdir()
    c = sqlite3.connect(codex / "state_5.sqlite")
    c.execute("CREATE TABLE threads (id TEXT, cwd TEXT, title TEXT, name TEXT, updated_at INTEGER, updated_at_ms INTEGER, "
              "source TEXT, thread_source TEXT, git_branch TEXT)")
    ms = int(now * 1000)
    c.executemany("INSERT INTO threads VALUES (?,?,?,?,?,?,?,?,?)", [
        ("t1", "\\\\?\\D:\\work\\projA", "long first message", "跑一下敏感性分析", ms // 1000, ms - 1000, "vscode", "user", "main"),
        ("t2", "D:\\work\\projA", "guardian", None, ms // 1000, ms - 900, '{"subagent":1}', "guardian_review", None),
        ("t3", "C:\\Temp\\x", "exec run", None, ms // 1000, ms - 800, "exec", "user", None),
    ])
    c.commit(); c.close()

    mem = tmp_path / "claude-mem.db"
    c = sqlite3.connect(mem)
    c.execute("CREATE TABLE observations (project TEXT, title TEXT, type TEXT, created_at_epoch INTEGER)")
    c.execute("CREATE TABLE session_summaries (project TEXT, request TEXT, next_steps TEXT, created_at_epoch INTEGER)")
    c.execute("INSERT INTO observations VALUES ('projA', 'calibration fixed', 'bugfix', ?)", (ms - 5000,))
    c.execute("INSERT INTO session_summaries VALUES ('projA', 'fix chapter 3', 'rerun solver', ?)", (ms - 4000,))
    c.execute("INSERT INTO observations VALUES ('projA', 'ancient', 'change', ?)", (ms - 40 * DAY * 1000,))
    c.commit(); c.close()
    return {"claude_history": str(hist), "codex_home": str(codex), "claude_mem_db": str(mem), "lookback_days": 7,
            "signal_days": 2}


def test_agent_history_groups_filters_and_redacts(tmp_path):
    from awos.sensors.agent_history import SENSOR

    cfg = _agent_fixture(tmp_path)
    c = ctx(cfg)
    drafts = SENSOR.collect(c)
    by = {d.payload["label"]: d for d in drafts}
    assert set(by) == {"projA", "projB"}                       # self run, old prompt excluded
    a = by["projA"]
    assert a.payload["counts"] == {"claude_prompts": 4, "codex_threads": 1, "claude_mem": 2}
    assert a.payload["codex"][0]["title"] == "跑一下敏感性分析"
    assert any(m.get("next_steps") == "rerun solver" for m in a.payload["claude_mem"])
    blob = json.dumps([d.payload for d in drafts], ensure_ascii=False) + "".join(d.body or "" for d in drafts)
    assert "PASTED-SECRET-BLOB" not in blob and "sk-abcdefghijklmnop" not in blob and "/clear" not in blob
    assert "[REDACTED]" in by["projB"].body
    assert c.cursor["claude_offset"] > 0

    # same state -> same fingerprints; a material increase in activity -> new fingerprint for that (project, day)
    again = SENSOR.collect(c)
    assert sorted(d.fingerprint for d in again) == sorted(d.fingerprint for d in drafts)
    _write_history(Path(cfg["claude_history"]), [
        {"display": f"more {i}", "pastedContents": {}, "timestamp": int(time.time() * 1000), "project": "D:\\work\\projA",
         "sessionId": "sessA"} for i in range(5)])
    third = {d.payload["label"]: d for d in SENSOR.collect(c)}
    assert third["projA"].fingerprint != a.fingerprint and third["projB"].fingerprint == by["projB"].fingerprint
    snap = SENSOR.context_snapshot(c)
    assert snap and "projA" in snap


def test_agent_history_missing_sources(tmp_path):
    from awos.sensors.agent_history import SENSOR

    c = ctx({"claude_history": str(tmp_path / "nope.jsonl"), "codex_home": str(tmp_path / "nocodex"),
             "claude_mem_db": str(tmp_path / "nomem.db")})
    assert SENSOR.collect(c) == []


# --------------------------------------------------------------------------- zotero

def _zitem(key: str, typ: str, added: str, title: str = "T") -> dict:
    return {"key": key, "data": {"key": key, "itemType": typ, "dateAdded": added, "title": title, "date": "2026-03",
                                 "creators": [{"lastName": "Li"}, {"lastName": "Wang"}], "publicationTitle": "Energy",
                                 "tags": [{"tag": "hydrogen"}], "collections": ["C1"]}}


def test_zotero_api_batches_new_items(monkeypatch):
    from awos.sensors import zotero

    items = [_zitem("K3", "journalArticle", "2026-11-05T01:00:00Z", "Hydrogen climate risk"),
             _zitem("A1", "attachment", "2026-11-05T00:59:00Z"),
             _zitem("K2", "conferencePaper", "2026-11-04T01:00:00Z", "Grid flexibility"),
             _zitem("K1", "journalArticle", "2026-10-01T00:00:00Z", "Old")]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/collections"):
            return httpx.Response(200, json=[{"key": "C1", "data": {"name": "Hydrogen"}}])
        start = int(req.url.params.get("start", 0))
        return httpx.Response(200, json=items[start:start + 25])

    monkeypatch.setattr(zotero, "_client", lambda: httpx.Client(base_url=zotero.API, transport=httpx.MockTransport(handler)))
    since = zotero.parse_iso("2026-10-15T00:00:00Z")
    c = ctx({}, {"since_ts": since})
    drafts = zotero.SENSOR.collect(c)
    assert len(drafts) == 1 and drafts[0].title == "新加入 Zotero 的 2 篇文献"
    p = drafts[0].payload
    assert [i["key"] for i in p["items"]] == ["K3", "K2"] and p["items"][0]["collections"] == ["Hydrogen"]
    assert drafts[0].fingerprint == zotero.fingerprint("zotero", "new_items", "K2", "K3")
    assert c.cursor["since_ts"] == zotero.parse_iso("2026-11-05T01:00:00Z")
    assert zotero.SENSOR.collect(c) == []                      # watermark advanced


def test_zotero_sqlite_fallback(tmp_path, monkeypatch):
    from awos.sensors import zotero

    db = tmp_path / "zotero.sqlite"
    c = sqlite3.connect(db)
    c.executescript("""
CREATE TABLE items(itemID INTEGER PRIMARY KEY, itemTypeID INT, dateAdded TEXT, key TEXT);
CREATE TABLE itemTypes(itemTypeID INT, typeName TEXT);
CREATE TABLE deletedItems(itemID INT);
CREATE TABLE fields(fieldID INT, fieldName TEXT);
CREATE TABLE itemDataValues(valueID INT, value TEXT);
CREATE TABLE itemData(itemID INT, fieldID INT, valueID INT);
CREATE TABLE creators(creatorID INT, firstName TEXT, lastName TEXT);
CREATE TABLE itemCreators(itemID INT, creatorID INT, orderIndex INT);
INSERT INTO itemTypes VALUES (1,'journalArticle'),(2,'attachment');
INSERT INTO items VALUES (1,1,'2026-11-04 10:00:00','NEW1'),(2,2,'2026-11-04 10:00:01','ATT1'),
                         (3,1,'2026-01-01 00:00:00','OLD1'),(4,1,'2026-11-04 11:00:00','DEL1');
INSERT INTO deletedItems VALUES (4);
INSERT INTO fields VALUES (1,'title'),(2,'date');
INSERT INTO itemDataValues VALUES (1,'Coal retrofit pathways'),(2,'2026-02-00 2026');
INSERT INTO itemData VALUES (1,1,1),(1,2,2);
INSERT INTO creators VALUES (1,'San','Zhang');
INSERT INTO itemCreators VALUES (1,1,0);
""")
    c.commit(); c.close()

    def boom(*a, **k):
        raise httpx.ConnectError("zotero not running")

    monkeypatch.setattr(zotero, "fetch_api", boom)
    cx = ctx({"data_dir": str(tmp_path)}, {"since_ts": zotero.parse_iso("2026-11-01T00:00:00Z")})
    drafts = zotero.SENSOR.collect(cx)
    assert len(drafts) == 1 and drafts[0].payload["via"] == "sqlite"
    it = drafts[0].payload["items"][0]
    assert (it["key"], it["title"], it["authors"], it["year"]) == ("NEW1", "Coal retrofit pathways", ["Zhang"], "2026")


# --------------------------------------------------------------------------- inbox_drop

def test_inbox_drop_via_runner(env):
    from awos import repo
    from awos.sensors import run_sensor

    inbox = env["tmp"] / "inbox"
    inbox.mkdir()
    f = inbox / "request.md"
    f.write_text("# 帮我整理本周组会材料\n\n周五之前。 password: hunter2hunter2\n", encoding="utf-8")
    (inbox / "ignore.pdf").write_bytes(b"%PDF")
    old = time.time() - 60
    os.utime(f, (old, old))
    res = run_sensor("inbox_drop")
    assert res["ok"] and res["new"] == 1
    sig = repo.list_signals()[0]
    assert sig["source"] == "inbox_drop" and sig["title"] == "帮我整理本周组会材料"
    assert "hunter2" not in sig["body"] and sig["payload"]["filename"] == "request.md"
    assert not f.exists() and len(list((inbox / "processed").glob("*_request.md"))) == 1
    assert (inbox / "ignore.pdf").exists()
    assert run_sensor("inbox_drop")["new"] == 0


# --------------------------------------------------------------------------- mailmaster

def _mail_db(root: Path) -> Path:
    acct = root / "someone@example-mail.com_1234"
    acct.mkdir(parents=True)
    c = sqlite3.connect(acct / "mail.db")
    c.execute("CREATE TABLE Mailbox(Id TEXT PRIMARY KEY, Type INTEGER)")
    c.execute("CREATE TABLE MailMeta(LocalId INTEGER PRIMARY KEY, RemoteId TEXT, ReceivedDate INTEGER, Deleted INTEGER, "
              "MessageId TEXT, Subject TEXT, Summary TEXT, Froms TEXT, Sender TEXT, Unread INTEGER, Mailbox TEXT, "
              "IsPromotion INTEGER, HasAttachments INTEGER, has_calendar INTEGER)")
    c.executemany("INSERT INTO Mailbox VALUES (?,?)", [("1", 1), ("3", 3), ("7", 7), ("5", 5)])
    now = int(time.time() * 1000)
    fr = lambda mail, name=None: json.dumps({"mail": mail, **({"name": name} if name else {})})  # noqa: E731
    rows = [
        (1, "r1", now - 3600_000, 0, "<m1@x>", "关于下周组会", "请在周五前回复 13800138000 或 a.b@example.org",
         fr("advisor@lab.example.edu", "王老师"), "{}", 1, "1", 0, 0, 0),
        (2, "r2", now - 3000_000, 0, "<m2@x>", "Weekly newsletter", "click to unsubscribe", fr("newsletter@shop.example.com"),
         "{}", 1, "1", 0, 0, 0),
        (3, "r3", now - 2000_000, 0, "<m3@x>", "Decision on your manuscript ENE-123", "Dear author",
         fr("no-reply@elsevier.com", "Energy"), "{}", 1, "7", 0, 0, 0),
        (4, "r4", now - 1000_000, 0, "<m4@x>", "Re: draft", "sent by me", fr("me@example-mail.com"), "{}", 0, "3", 0, 0, 0),
        (5, "r5", now - 500_000, 0, "<m5@x>", "限时优惠", "促销", fr("promo@deals.example.com"), "{}", 1, "1", 1, 0, 0),
        (6, "r6", now - 10 * DAY * 1000, 0, "<m6@x>", "old", "old", fr("x@example.org"), "{}", 0, "1", 0, 0, 0),
        (7, "r7", now - 100_000, 1, "<m7@x>", "deleted", "", fr("x@example.org"), "{}", 0, "1", 0, 0, 0),
    ]
    c.executemany("INSERT INTO MailMeta VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
    c.commit(); c.close()
    return acct


def test_mailmaster_filters_scrubs_and_watermarks(tmp_path):
    from awos.sensors.mailmaster import SENSOR

    _mail_db(tmp_path)
    c = ctx({"root": str(tmp_path), "lookback_days": 3})
    drafts = SENSOR.collect(c)
    subjects = {d.payload["subject"] for d in drafts}
    assert subjects == {"关于下周组会", "Decision on your manuscript ENE-123"}   # journal mail kept despite no-reply/ads box
    d = next(x for x in drafts if x.payload["subject"] == "关于下周组会")
    assert d.payload["account"] == "example-mail.com_1234" and d.payload["from_domain"] == "lab.example.edu"
    assert d.payload["from_name"] == "王老师" and d.payload["unread"] is True
    blob = json.dumps([x.payload for x in drafts], ensure_ascii=False) + "".join(x.body + x.title for x in drafts)
    assert "someone" not in blob and "advisor@" not in blob and "13800138000" not in blob and "a.b@example.org" not in blob
    assert c.cursor["last_skipped_bulk"] == 2
    assert SENSOR.collect(c) == []                                              # watermark advanced
    assert "example-mail.com_1234" in (SENSOR.context_snapshot(c) or "")


# --------------------------------------------------------------------------- connectors (gmail / notion)

class FakeRunAgent:
    def __init__(self, structured: dict | None, status: str = "succeeded", final_text: str = "") -> None:
        self.structured, self.status, self.final_text = structured, status, final_text
        self.calls: list[tuple[str, dict]] = []

    def __call__(self, prompt: str, **kw):
        self.calls.append((prompt, kw))
        return "run_x", RunResult(status=self.status, structured=self.structured, final_text=self.final_text,
                                  error=None if self.status == "succeeded" else "boom")


def _assert_safe_call(fake: FakeRunAgent) -> str:
    prompt, kw = fake.calls[0]
    assert kw["executor"] == "codex" and kw["allow_connectors"] is True and kw["allow_network"] is False
    assert kw["sandbox"] == "read-only" and kw["purpose"] == "sensor" and kw["output_schema"]
    assert "READ-ONLY" in prompt and "NEVER call any tool that sends" in prompt and "UNTRUSTED" in prompt
    return prompt


def test_gmail_sensor(monkeypatch):
    import awos.llm
    from awos.sensors.connectors import GmailSensor

    base = {"thread_id": "th1", "from_name": "Editor", "from_domain": "journal.example.org", "subject": "Revision due",
            "received_at": "2026-11-05T08:00:00+08:00", "one_line_summary": "返修截止 11 月 20 日", "action_needed": "submit",
            "deadline": "2026-11-20", "importance": 5}
    fake = FakeRunAgent({"complete": True, "items": [
        {**base, "message_id": "m1"},
        {**base, "message_id": "m2", "action_needed": "none", "deadline": None, "importance": 2},   # filtered
        {**base, "message_id": "m3", "action_needed": "bogus", "deadline": None, "importance": 4,
         "from_domain": "Someone <s@uni.example.edu>"},
    ]})
    monkeypatch.setattr(awos.llm, "run_agent", fake)
    c = ctx({"lookback_hours": 24})
    drafts = GmailSensor().collect(c)
    _assert_safe_call(fake)
    assert [d.external_id for d in drafts] == ["m1", "m3"]
    assert drafts[0].fingerprint == GmailSensor().collect(ctx({}))[0].fingerprint     # by message_id only
    assert drafts[1].payload["action_needed"] == "none" and drafts[1].payload["from_domain"] == "uni.example.edu"
    assert "截止: 2026-11-20" in drafts[0].body and c.cursor["since_ts"] > 0

    # partial result -> watermark must not advance
    monkeypatch.setattr(awos.llm, "run_agent", FakeRunAgent({"complete": False, "items": [{**base, "message_id": "m9"}]}))
    c2 = ctx({}, {"since_ts": 123.0})
    assert len(GmailSensor().collect(c2)) == 1 and c2.cursor["since_ts"] == 123.0


def test_gmail_sensor_failure_raises(monkeypatch):
    import awos.llm
    from awos.sensors.connectors import GmailSensor

    monkeypatch.setattr(awos.llm, "run_agent", FakeRunAgent(None, status="failed"))
    with pytest.raises(RuntimeError):
        GmailSensor().collect(ctx({}))
    monkeypatch.setattr(awos.llm, "run_agent", FakeRunAgent(None, final_text='```json\n{"items": []}\n```'))
    assert GmailSensor().collect(ctx({})) == []                                       # final_text fallback


def test_notion_sensor(monkeypatch):
    import awos.llm
    from awos.sensors.connectors import NotionSensor
    from awos.timeutil import local_now

    soon = (local_now() + timedelta(days=3)).date().isoformat()
    far = (local_now() + timedelta(days=40)).date().isoformat()
    page = {"page_id": "p1", "title": "日程安排", "url": "https://www.notion.so/p1", "last_edited": "2026-11-05T09:00:00Z",
            "summary": "本周日程", "items": [{"text": "组会汇报", "date": soon, "status": "todo"},
                                            {"text": "已完成的事", "date": soon, "status": "done"},
                                            {"text": "远期", "date": far, "status": None},
                                            {"text": "无日期待办", "date": None, "status": "todo"}]}
    fake = FakeRunAgent({"complete": True, "pages": [page]})
    monkeypatch.setattr(awos.llm, "run_agent", fake)
    drafts = NotionSensor().collect(ctx({"recent_days": 3, "ahead_days": 14}))
    prompt = _assert_safe_call(fake)
    assert "日程安排" in prompt
    assert len(drafts) == 2
    pg, item = drafts
    assert pg.external_id == "p1" and len(pg.payload["items"]) == 4
    assert item.payload["text"] == "组会汇报" and item.payload["date"] == soon
    page2 = {**page, "last_edited": "2026-11-06T09:00:00Z"}
    monkeypatch.setattr(awos.llm, "run_agent", FakeRunAgent({"complete": True, "pages": [page2]}))
    pg2, item2 = NotionSensor().collect(ctx({}))
    assert pg2.fingerprint != pg.fingerprint and item2.fingerprint == item.fingerprint


def test_registry_has_all_sensors():
    from awos.sensors import all_sensors

    assert {"git", "files", "agent_history", "zotero", "inbox_drop", "mailmaster", "gmail", "notion"} <= set(all_sensors())


# --------------------------------------------------------------------------- live (real Codex connectors)

def _codex_available() -> bool:
    try:
        from awos.executors import get_executor
        return bool(get_executor("codex").available()[0])
    except Exception:
        return False


@pytest.mark.live
def test_live_connectors(env, request):
    if not _codex_available():
        pytest.skip("codex executor not available")
    if "live" not in (request.config.getoption("-m") or "") and not os.environ.get("AWOS_LIVE"):
        pytest.skip("live connector test spends Codex runs; run with -m live or AWOS_LIVE=1")
    from awos.config import load_config
    from awos.sensors.connectors import GmailSensor, NotionSensor

    cfg = load_config()
    g = GmailSensor().collect(ctx(cfg.section("sensors.gmail")))
    n = NotionSensor().collect(ctx(cfg.section("sensors.notion")))
    print(f"\nLIVE gmail signals={len(g)} notion signals={len(n)}")
    assert isinstance(g, list) and isinstance(n, list)


def test_github_sensor_maps_items_and_soft_fails_when_logged_out(monkeypatch):
    import logging
    from awos.sensors import github as gh
    from awos.sensors.base import SensorContext

    ctx = SensorContext(config={"max_items": 5}, cursor={}, log=logging.getLogger("t"))
    monkeypatch.setattr(gh, "logged_in", lambda: False)
    import pytest as _pt
    with _pt.raises(RuntimeError, match="not logged in"):
        gh.SENSOR.collect(ctx)

    monkeypatch.setattr(gh, "logged_in", lambda: True)

    def fake(*args, **kw):
        if args[:2] == ("search", "prs") and "--review-requested=@me" in args:
            return [{"repository": {"nameWithOwner": "o/r"}, "title": "Fix x", "url": "https://github.com/o/r/pull/3", "number": 3,
                     "updatedAt": "2026-11-04T10:00:00Z", "author": {"login": "bob"}, "isDraft": False}]
        if args[:2] == ("search", "issues"):
            return [{"repository": {"nameWithOwner": "o/r"}, "title": "Bug", "url": "u", "number": 9, "updatedAt": "2026-11-03T10:00:00Z",
                     "author": {"login": "amy"}}]
        if args[0] == "api":
            return [{"id": "77", "reason": "mention", "updated_at": "2026-11-05T01:00:00Z", "repository": {"full_name": "o/r"},
                     "subject": {"title": "Question?", "type": "Issue", "url": "https://api.github.com/repos/o/r/issues/9"}}]
        return []
    monkeypatch.setattr(gh, "run_gh", fake)
    drafts = gh.SENSOR.collect(ctx)
    titles = [d.title for d in drafts]
    assert any("待你评审的 PR" in t and "o/r#3" in t for t in titles)
    assert any("指派给你的 issue" in t for t in titles) and any("通知" in t and "mention" in t for t in titles)
    assert len({d.fingerprint for d in drafts}) == len(drafts)
    assert drafts[0].url == "https://github.com/o/r/pull/3"
