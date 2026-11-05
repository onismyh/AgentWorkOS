"""Tests for hitl/notify.py (quiet hours, dedupe, channel dispatch, toast construction) and hitl/tokens.py."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import subprocess
import xml.etree.ElementTree as ET
from urllib.parse import parse_qs

import httpx
import pytest

from awos import db, repo, timeutil
from awos.config import reset_config_cache
from awos.hitl import notify as nt
from awos.hitl import tokens

# Placeholder secrets only (never real keys).
SECRETS = """
[notify]
wecom_webhook = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=WECOMKEY123456"
feishu_webhook = "https://open.feishu.cn/open-apis/bot/v2/hook/FEISHUHOOK123456"
feishu_secret = "FEISHUSECRET"
serverchan_key = "SCTKEY123456"
bark_url = "https://api.day.app/BARKDEVICEKEY123"
ntfy_url = "https://ntfy.sh/awos-test-topic"
pushplus_token = "PUSHPLUSTOKEN123"
"""

OK_BODIES = {
    "qyapi.weixin.qq.com": {"errcode": 0, "errmsg": "ok"},
    "open.feishu.cn": {"code": 0, "msg": "success"},
    "sctapi.ftqq.com": {"code": 0, "message": ""},
    "api.day.app": {"code": 200, "message": "success"},
    "ntfy.sh": {"id": "abc123", "event": "message"},
    "www.pushplus.plus": {"code": 200, "msg": "ok"},
}


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated config (var dir, port 9999, quiet hours 23:30-07:30), temp DB, fake toast, daytime clock."""
    var = tmp_path / "var"
    var.mkdir()
    cfg_file = tmp_path / "test.toml"

    def configure(*, server: str = 'host = "127.0.0.1"\nport = 9999', quiet: str = '["23:30", "07:30"]',
                  notify: str = 'channels = ["toast"]\ndisabled_channels = []\nwebhook_min_level = "info"') -> None:
        cfg_file.write_text(f'[paths]\nvar = "{var.as_posix()}"\n[server]\n{server}\n'
                            f'[scheduler]\nquiet_hours = {quiet}\n[notify]\n{notify}\n', encoding="utf-8")
        reset_config_cache()

    monkeypatch.setenv("AWOS_CONFIG", str(cfg_file))
    monkeypatch.setenv("AWOS_DB", str(tmp_path / "test.db"))
    monkeypatch.delenv("AWOS_NOTIFY_DISABLE", raising=False)
    monkeypatch.setattr(timeutil, "local_hm", lambda: "12:00")
    toasts: list[nt.Message] = []
    monkeypatch.setattr(nt, "send_toast", lambda m, **kw: toasts.append(m) or "OK setting=Enabled")
    configure()
    yield {"configure": configure, "var": var, "toasts": toasts}
    db.close_thread_conns()
    reset_config_cache()


@pytest.fixture
def http(env, monkeypatch):
    """Mock all webhook HTTP traffic; returns (captured requests, per-host override dict)."""
    (env["var"] / "secrets.toml").write_text(SECRETS, encoding="utf-8")
    captured: list[httpx.Request] = []
    overrides: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        o = overrides.get(request.url.host)
        if isinstance(o, Exception):
            raise o
        if isinstance(o, httpx.Response):
            return o
        return httpx.Response(200, json=OK_BODIES.get(request.url.host, {}))

    monkeypatch.setattr(nt, "HTTP_TRANSPORT", httpx.MockTransport(handler))
    return captured, overrides


def _rows() -> list[dict]:
    return db.q("SELECT * FROM notifications ORDER BY rowid")


# --------------------------------------------------------------------------- quiet hours


@pytest.mark.parametrize("hm,quiet", [("23:29", False), ("23:30", True), ("23:59", True), ("00:00", True),
                                      ("03:00", True), ("07:29", True), ("07:31", False), ("12:00", False)])
def test_quiet_hours_cross_midnight(env, hm, quiet):
    assert nt.in_quiet_hours(hm) is quiet


def test_quiet_hours_same_day_and_disabled(env):
    env["configure"](quiet='["13:00", "14:00"]')
    assert nt.in_quiet_hours("13:30") and not nt.in_quiet_hours("14:01") and not nt.in_quiet_hours("03:00")
    env["configure"](quiet="[]")
    assert not nt.in_quiet_hours("03:00")


def test_quiet_hours_suppress_non_high(env, monkeypatch):
    monkeypatch.setattr(timeutil, "local_hm", lambda: "03:00")
    ids = nt.notify("夜间普通通知", "body", level="action", ref_id="q1")
    assert len(ids) == 1 and not env["toasts"]
    row = _rows()[0]
    assert row["status"] == "suppressed" and row["error"] == "quiet hours" and row["sent_at"] is None
    ids = nt.notify("夜间高风险", "body", level="high", ref_id="q2")
    assert len(ids) == 1 and len(env["toasts"]) == 1
    assert _rows()[-1]["status"] == "sent"


def test_disable_env_suppresses(env, monkeypatch):
    monkeypatch.setenv("AWOS_NOTIFY_DISABLE", "1")
    nt.notify("t", "b", level="high")
    assert not env["toasts"] and _rows()[0]["status"] == "suppressed"


# --------------------------------------------------------------------------- dedupe


def test_dedupe_window(env, monkeypatch):
    assert len(nt.notify("待你确认（1）", "x", ref_type="approval", ref_id="apv_1")) == 1
    assert nt.notify("待你确认（1）", "x", ref_type="approval", ref_id="apv_1") == []  # within 30 min
    assert len(nt.notify("待你确认（1）", "x", ref_type="approval", ref_id="apv_2")) == 1  # other ref
    assert len(nt.notify("待你确认（1）", "x", ref_id="apv_1", dedupe=False)) == 1
    assert len(env["toasts"]) == 3
    db.execute("UPDATE notifications SET created_at = ?", (db.ts_after(-31 * 60),))  # age them out
    assert len(nt.notify("待你确认（1）", "x", ref_type="approval", ref_id="apv_1")) == 1


def test_dedupe_ignores_failures(env, monkeypatch):
    def boom(m, **kw):
        raise RuntimeError("toast failed (exit 1): nope")

    monkeypatch.setattr(nt, "send_toast", boom)
    nt.notify("same", "x", ref_id="r")
    assert _rows()[0]["status"] == "failed" and "nope" in _rows()[0]["error"]
    monkeypatch.setattr(nt, "send_toast", lambda m, **kw: "OK")
    assert len(nt.notify("same", "x", ref_id="r")) == 1  # a failed attempt does not block a retry
    assert nt.notify("same", "x", ref_id="r") == []


# --------------------------------------------------------------------------- channels


def test_configured_channels(env):
    assert nt.configured_channels() == ["toast"]
    (env["var"] / "secrets.toml").write_text(SECRETS, encoding="utf-8")
    assert nt.configured_channels() == ["toast", "wecom", "feishu", "serverchan", "bark", "ntfy", "pushplus"]
    env["configure"](notify='channels = ["toast", "webhook"]\ndisabled_channels = ["bark", "ntfy"]')
    assert nt.configured_channels() == ["toast", "wecom", "feishu", "serverchan", "pushplus"]


def test_webhook_dispatch_all_channels(env, http):
    captured, _ = http
    url = "http://127.0.0.1:9999/#outbox"
    title = '审批：发送邮件 <test> & "quotes"'
    body = "第一行摘要\n" + "x" * 500
    ids = nt.notify(title, body, level="action", ref_type="approval", ref_id="apv_1", url=url)
    rows = _rows()
    assert len(ids) == 7 and {r["status"] for r in rows} == {"sent"}
    assert {r["channel"] for r in rows} == {"toast", "wecom", "feishu", "serverchan", "bark", "ntfy", "pushplus"}
    assert all(r["ref_type"] == "approval" and r["ref_id"] == "apv_1" and r["sent_at"] for r in rows)
    assert env["toasts"][0].url == url and env["toasts"][0].body == body  # toast gets the full body
    by_host = {r.url.host: r for r in captured}
    assert len(captured) == 6

    wecom = json.loads(by_host["qyapi.weixin.qq.com"].content)
    assert by_host["qyapi.weixin.qq.com"].url.params["key"] == "WECOMKEY123456"
    assert wecom["msgtype"] == "markdown" and title in wecom["markdown"]["content"]
    assert f"[打开控制台]({url})" in wecom["markdown"]["content"] and "x" * 300 not in wecom["markdown"]["content"]

    feishu = json.loads(by_host["open.feishu.cn"].content)
    assert feishu["msg_type"] == "interactive" and feishu["card"]["header"]["title"]["content"] == title
    button = feishu["card"]["elements"][1]["actions"][0]
    assert button["url"] == url and button["text"]["content"] == "打开控制台"
    expect = base64.b64encode(hmac.new(f"{feishu['timestamp']}\nFEISHUSECRET".encode(), b"", hashlib.sha256).digest()).decode()
    assert feishu["sign"] == expect

    sc = by_host["sctapi.ftqq.com"]
    form = parse_qs(sc.content.decode())
    assert str(sc.url) == "https://sctapi.ftqq.com/SCTKEY123456.send" and len(form["title"][0]) <= 32
    assert url in form["desp"][0]

    bark = by_host["api.day.app"]
    assert str(bark.url) == "https://api.day.app/BARKDEVICEKEY123"
    assert json.loads(bark.content)["url"] == url and json.loads(bark.content)["level"] == "active"

    ntfy = by_host["ntfy.sh"]
    nj = json.loads(ntfy.content)
    assert str(ntfy.url) == "https://ntfy.sh/" and nj["topic"] == "awos-test-topic"
    assert nj["click"] == url and nj["priority"] == 4 and len(nj["message"]) <= 200

    pp = json.loads(by_host["www.pushplus.plus"].content)
    assert pp["token"] == "PUSHPLUSTOKEN123" and pp["template"] == "markdown"


def test_webhook_failures_recorded_and_redacted(env, http):
    captured, overrides = http
    overrides["qyapi.weixin.qq.com"] = httpx.Response(200, json={"errcode": 93000, "errmsg": "invalid webhook url"})
    overrides["api.day.app"] = httpx.ConnectError("cannot connect to https://api.day.app/BARKDEVICEKEY123")
    overrides["ntfy.sh"] = httpx.Response(500, text="boom")
    ids = nt.notify("t", "b", level="high")  # must not raise
    rows = {r["channel"]: r for r in _rows()}
    assert len(ids) == 7
    assert rows["wecom"]["status"] == "failed" and "93000" in rows["wecom"]["error"]
    assert rows["bark"]["status"] == "failed" and "BARKDEVICEKEY123" not in rows["bark"]["error"]
    assert "***" in rows["bark"]["error"]
    assert rows["ntfy"]["status"] == "failed" and "HTTP 500" in rows["ntfy"]["error"]
    assert rows["feishu"]["status"] == rows["toast"]["status"] == "sent"
    assert all("WECOMKEY123456" not in (r["error"] or "") for r in rows.values())


def test_explicit_unconfigured_and_unknown_channels(env):
    nt.notify("t", "b", channels=["wecom", "carrier-pigeon"])
    rows = {r["channel"]: r for r in _rows()}
    assert rows["wecom"]["status"] == "failed" and "not configured" in rows["wecom"]["error"]
    assert rows["carrier-pigeon"]["status"] == "failed" and "unknown channel" in rows["carrier-pigeon"]["error"]


def test_webhook_min_level(env, http):
    captured, _ = http
    env["configure"](notify='channels = ["toast"]\nwebhook_min_level = "action"')
    nt.notify("info only", "b", level="info")
    rows = {r["channel"]: r for r in _rows()}
    assert rows["toast"]["status"] == "sent" and rows["wecom"]["status"] == "suppressed" and not captured


def test_email_channel(env, monkeypatch):
    (env["var"] / "secrets.toml").write_text(
        '[notify]\nsmtp_host = "smtp.example.com"\nsmtp_user = "me@example.com"\nsmtp_password = "PLACEHOLDER"\n',
        encoding="utf-8")
    sent = []

    class FakeSMTP:
        def __init__(self, host, port, timeout):
            sent.append(("connect", host, port, timeout))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def login(self, user, pw):
            sent.append(("login", user))

        def send_message(self, msg):
            sent.append(("send", msg["Subject"], msg["To"], msg.get_content()))

    monkeypatch.setattr(nt.smtplib, "SMTP_SSL", FakeSMTP)
    nt.notify("晨报", "今天 3 件事", url="http://127.0.0.1:9999/")
    assert {r["channel"]: r["status"] for r in _rows()} == {"toast": "sent", "email": "sent"}
    assert sent[0] == ("connect", "smtp.example.com", 465, nt.SMTP_TIMEOUT)
    assert sent[2][1] == "[AgentWorkOS] 晨报" and sent[2][2] == "me@example.com" and "今天 3 件事" in sent[2][3]


def test_post_webhook(env, http):
    captured, overrides = http
    text = "完整的已批准消息 " + "y" * 400
    res = nt.post_webhook("wecom", "组会通知", text)
    assert res["done"] is True
    assert "y" * 400 in json.loads(captured[0].content)["markdown"]["content"]  # full approved text, not a summary
    assert nt.post_webhook("toast", "t", "x")["done"] is False
    overrides["open.feishu.cn"] = httpx.Response(200, json={"code": 19021, "msg": "sign match fail"})
    res = nt.post_webhook("feishu", "t", "x")
    assert res["done"] is False and "19021" in res["error"]


def test_serverchan3_url():
    assert nt.serverchan_url("SCT12345abc") == "https://sctapi.ftqq.com/SCT12345abc.send"
    assert nt.serverchan_url("sctp1234tABCDEF") == "https://1234.push.ft07.com/send/sctp1234tABCDEF.send"


def test_dashboard_url(env):
    assert nt.dashboard_url() == "http://127.0.0.1:9999/"
    assert nt.dashboard_url("outbox") == "http://127.0.0.1:9999/#outbox"
    assert nt.dashboard_url("task/tsk_1") == "http://127.0.0.1:9999/#task/tsk_1"
    env["configure"](server='host = "0.0.0.0"\nport = 8000')
    assert nt.base_url() == "http://127.0.0.1:8000"
    env["configure"](server='port = 8000\npublic_url = "http://awos.tail1234.ts.net:8000/"')
    assert nt.dashboard_url("outbox") == "http://awos.tail1234.ts.net:8000/#outbox"


# --------------------------------------------------------------------------- toast construction


def test_toast_xml_escaping_and_actions():
    url = "http://127.0.0.1:8765/#outbox?a=1&b=2"
    title = "A <b> & \"c\" 'd' 中文标题"
    xml = nt.build_toast_xml(title, "正文\x01 <x> & y", url=url, level="high")
    root = ET.fromstring(xml)
    assert root.get("activationType") == "protocol" and root.get("launch") == url and root.get("duration") == "long"
    texts = root.findall("./visual/binding/text")
    assert texts[0].text == title and texts[1].text == "正文 <x> & y" and texts[2].get("placement") == "attribution"
    action = root.find("./actions/action")
    assert action.get("content") == "打开控制台" and action.get("arguments") == url
    assert action.get("activationType") == "protocol"
    assert root.find("audio") is not None

    plain = ET.fromstring(nt.build_toast_xml("t", "b"))
    assert plain.get("launch") is None and plain.find("actions") is None and plain.get("duration") is None


def test_toast_command_construction():
    xml = nt.build_toast_xml("AgentWorkOS 测试通知", "中文 & <特殊> 'quote' \"dq\"", url="http://127.0.0.1:8765/")
    argv = nt.build_toast_command(xml, app_id=nt.POWERSHELL_AUMID, tag="abc!@#'x")
    assert argv[0].lower().endswith("powershell.exe")
    assert argv[1:6] == ["-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand"]
    script = base64.b64decode(argv[6]).decode("utf-16-le")
    assert f"CreateToastNotifier('{nt.POWERSHELL_AUMID}')" in script
    assert "$toast.Tag = 'abcx'" in script  # sanitized: no quote injection
    b64 = re.search(r"FromBase64String\('([A-Za-z0-9+/=]+)'\)", script).group(1)
    assert base64.b64decode(b64).decode("utf-8") == xml
    assert all(ord(ch) < 128 for ch in script)  # only ASCII crosses the process boundary
    s2 =base64.b64decode(nt.build_toast_command(xml, app_id="it's")[6]).decode("utf-16-le")
    assert "CreateToastNotifier('it''s')" in s2


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NO_WINDOW"), reason="Windows only")
def test_send_toast_process_handling(monkeypatch, tmp_path):
    calls = []

    def fake_run(argv, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, b"OK setting=Enabled", b"")

    monkeypatch.setattr(nt, "_run_powershell", fake_run)
    assert nt.send_toast(nt.Message("标题", "正文")) == "OK setting=Enabled"
    assert calls[0][1] == nt.TOAST_TIMEOUT == 20

    monkeypatch.setattr(nt, "_run_powershell", lambda a, timeout: subprocess.CompletedProcess(
        a, 2, b"", "toasts disabled: DisabledForApplication".encode()))
    with pytest.raises(RuntimeError, match="DisabledForApplication"):
        nt.send_toast(nt.Message("t", "b"))

    def slow(a, timeout):
        raise subprocess.TimeoutExpired(a, timeout)

    monkeypatch.setattr(nt, "_run_powershell", slow)
    with pytest.raises(RuntimeError, match="timed out"):
        nt.send_toast(nt.Message("t", "b"))


@pytest.mark.skipif(not hasattr(subprocess, "CREATE_NO_WINDOW"), reason="Windows only")
def test_powershell_output_cleanup():
    noise = '#< CLIXML\r\n<Objs Version="1.1.0.1"><Obj S="progress"><AV>正在准备首次使用模块。</AV></Obj></Objs>'
    raw = (noise + "元素未找到").encode("gbk")  # host messages arrive in the ANSI code page, not UTF-8
    cleaned = nt._ps_text(raw)
    assert "CLIXML" not in cleaned and "Objs" not in cleaned
    assert nt._ps_text("OK setting=Enabled".encode()) == "OK setting=Enabled"


# --------------------------------------------------------------------------- tokens


def _approval(**kw) -> str:
    return repo.create_approval(title="发送邮件给导师", kind="action", action_type="send_email",
                                options=[{"key": "approve", "label": "发送"}, {"key": "reject", "label": "不发"}],
                                payload={"to": "x@example.com"}, **kw)


def test_sign_verify_roundtrip(env):
    aid = _approval()
    tok = tokens.sign(aid)
    assert tokens.verify(tok) == (aid, None)
    assert re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", tok)
    row = repo.get_approval(aid)
    assert row["token_hash"] == hashlib.sha256(tok.encode()).hexdigest()
    key = env["var"] / "link_key"
    assert key.exists() and len(key.read_bytes()) == 32
    tok2 = tokens.sign(aid, "approve")
    assert tokens.verify(tok2) == (aid, "approve")


def test_single_active_token(env):
    aid = _approval()
    t1 = tokens.sign(aid)
    t2 = tokens.sign(aid, "reject")
    with pytest.raises(ValueError, match="superseded"):
        tokens.verify(t1)
    assert tokens.verify(t2) == (aid, "reject")
    tokens.revoke(aid)
    with pytest.raises(ValueError, match="superseded"):
        tokens.verify(t2)


def test_tampered_tokens_rejected(env):
    aid, other = _approval(), _approval()
    tok = tokens.sign(aid, "reject")
    body, sig = tok.split(".")
    claims = json.loads(base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)))
    claims.update(a=other, o="approve")
    forged = base64.urlsafe_b64encode(json.dumps(claims).encode()).rstrip(b"=").decode() + "." + sig
    with pytest.raises(ValueError, match="bad signature"):
        tokens.verify(forged)
    flipped = tok[:-1] + ("A" if tok[-1] != "A" else "B")
    with pytest.raises(ValueError, match="bad signature"):
        tokens.verify(flipped)
    for junk in ["", "abc", "a.b.c", "!!!.???", "x" * 5000, None]:
        with pytest.raises(ValueError):
            tokens.verify(junk)  # type: ignore[arg-type]
    (env["var"] / "link_key").write_bytes(b"\x01" * 32)  # key rotated -> old links die
    with pytest.raises(ValueError, match="bad signature"):
        tokens.verify(tok)


def test_expiry(env):
    aid = _approval()
    with pytest.raises(ValueError, match="expired"):
        tokens.verify(tokens.sign(aid, ttl_hours=-1))
    soon = _approval(expires_at=db.ts_after(-5))  # approval already past its own expiry -> link capped
    with pytest.raises(ValueError, match="expired"):
        tokens.verify(tokens.sign(soon, ttl_hours=24))


def test_single_use_after_decision_and_consume(env):
    aid = _approval()
    tok = tokens.sign(aid, "approve")
    assert tokens.verify(tok) == (aid, "approve")
    repo.decide_approval(aid, "approve", via="link")
    with pytest.raises(ValueError, match="not pending"):
        tokens.verify(tok)
    with pytest.raises(ValueError, match="not pending"):
        tokens.sign(aid)

    aid2 = _approval()
    tok2 = tokens.sign(aid2)
    assert tokens.consume(tok2) == (aid2, None)
    with pytest.raises(ValueError):
        tokens.consume(tok2)
    with pytest.raises(ValueError):
        tokens.verify(tok2)


def test_payload_change_invalidates(env):
    aid = _approval()
    tok = tokens.sign(aid)
    db.update("approvals", "id", aid, {"payload": json.dumps({"to": "attacker@example.com"})})
    with pytest.raises(ValueError, match="changed"):
        tokens.verify(tok)


def test_sign_validation(env):
    aid = _approval()
    with pytest.raises(ValueError, match="unknown option"):
        tokens.sign(aid, "nuke")
    with pytest.raises(KeyError):
        tokens.sign("apv_missing")


def test_approval_link(env):
    aid = _approval()
    link = tokens.approval_link(aid, "approve")
    assert link.startswith("http://127.0.0.1:9999/approve/")
    assert tokens.verify(link.rsplit("/", 1)[1]) == (aid, "approve")
