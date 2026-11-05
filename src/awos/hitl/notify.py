"""Notifications: native Windows toast (PowerShell 5.1 + WinRT, nothing to install) and optional phone webhooks.

    notify(title, body, level="info"|"action"|"high", ref_type=, ref_id=, url=) -> [notification ids]

* Channels = config `notify.channels` (default ["toast"]; the word "webhook" is accepted and ignored) plus every
  webhook channel whose secret is present in `var/secrets.toml` [notify], minus `notify.disabled_channels`.
* Quiet hours (`scheduler.quiet_hours`, Beijing time via awos.timeutil) suppress everything except level="high".
* De-duplication: same title + ref_id already sent/suppressed within 30 min -> skipped (returns []).
* Every attempt is recorded via repo.add_notification (status sent | failed | suppressed). notify() never raises.
* Cloud channels only get the title plus a short summary (`notify.phone_body_chars`, default 200): full context
  stays on the local dashboard. Error strings are scrubbed of secret values before they are stored.
* Toast AppUserModelID: Windows PowerShell's registered AUMID (works on any Win10/11 with no setup). Override with
  `notify.toast_app_id` if a branded AUMID has been registered.
* Tests / other modules can set AWOS_NOTIFY_DISABLE=1 to record notifications as suppressed without sending.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import re
import smtplib
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from email.message import EmailMessage
from typing import Any, Callable
from urllib.parse import urlsplit
from xml.sax.saxutils import escape as _escape

import httpx

from .. import repo, timeutil
from ..config import load_config
from ..db import q1, ts_after

log = logging.getLogger("awos.notify")

LEVELS = ("info", "action", "high")
DEDUPE_SECONDS = 30 * 60
HTTP_TIMEOUT = httpx.Timeout(10.0, connect=5.0)
TOAST_TIMEOUT = 20
SMTP_TIMEOUT = 15
EMAIL_BODY_CHARS = 4000
POWERSHELL_AUMID = r"{1AC14E77-02E7-4E5D-B744-2EB1AE5198B7}\WindowsPowerShell\v1.0\powershell.exe"
POWERSHELL_EXE = os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "WindowsPowerShell", "v1.0",
                              "powershell.exe")
HTTP_TRANSPORT: httpx.BaseTransport | None = None  # tests inject httpx.MockTransport here


@dataclass(frozen=True)
class Message:
    title: str
    body: str
    level: str = "info"
    url: str | None = None
    summary: str = ""  # redacted short body for cloud channels


# --------------------------------------------------------------------------- urls


def base_url() -> str:
    """http://127.0.0.1:<port> (or `server.public_url` if explicitly configured, e.g. a Tailscale name)."""
    cfg = load_config()
    public = str(cfg.get("server.public_url") or "").strip()
    if public:
        return public.rstrip("/")
    host = str(cfg.get("server.host") or "127.0.0.1")
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    if ":" in host:
        host = f"[{host}]"
    return f"http://{host}:{int(cfg.get('server.port', 8765))}"


def dashboard_url(path: str = "") -> str:
    """Dashboard deep link: http://127.0.0.1:<port>/#<path> (hash routing)."""
    return f"{base_url()}/#{path}" if path else f"{base_url()}/"


# --------------------------------------------------------------------------- policy helpers


def in_quiet_hours(hm: str | None = None) -> bool:
    """True if local (configured-timezone) HH:MM is inside scheduler.quiet_hours; windows may cross midnight."""
    window = load_config().get("scheduler.quiet_hours") or None
    if not window or len(window) != 2:
        return False
    return timeutil.in_window(window, hm)


def is_duplicate(title: str, ref_id: str | None, window_seconds: int = DEDUPE_SECONDS) -> bool:
    row = q1("SELECT 1 FROM notifications WHERE title = ? AND ref_id IS ? AND created_at >= ? "
             "AND status IN ('sent', 'suppressed') LIMIT 1", (title[:300], ref_id, ts_after(-window_seconds)))
    return row is not None


def _notify_secrets() -> dict[str, Any]:
    try:
        s = load_config().secrets().get("notify") or {}
        return s if isinstance(s, dict) else {}
    except Exception as e:  # malformed secrets.toml must not break notifications
        log.warning("cannot read var/secrets.toml [notify]: %s", type(e).__name__)
        return {}


def configured_channels(secrets: dict[str, Any] | None = None) -> list[str]:
    cfg = load_config()
    secrets = _notify_secrets() if secrets is None else secrets
    chans = [c for c in (cfg.get("notify.channels") or ["toast"]) if c != "webhook"]
    for name, (keys, _) in WEBHOOKS.items():
        if name not in chans and all(str(secrets.get(k) or "").strip() for k in keys):
            chans.append(name)
    disabled = set(cfg.get("notify.disabled_channels") or [])
    return [c for c in chans if c not in disabled]


def _summary(body: str, limit: int) -> str:
    body = (body or "").strip()
    return body if len(body) <= limit else body[: max(0, limit - 1)].rstrip() + "…"


def _secret_fragments(s: dict[str, Any]) -> list[str]:
    out: set[str] = set()
    for v in s.values():
        if not isinstance(v, str) or len(v) < 6:
            continue
        out.add(v)
        u = urlsplit(v)
        if u.scheme and u.netloc:
            out.update(seg for seg in u.path.split("/") if len(seg) >= 8)
            out.update(kv.split("=", 1)[1] for kv in u.query.split("&") if "=" in kv and len(kv.split("=", 1)[1]) >= 6)
    return sorted(out, key=len, reverse=True)


def _redact(text: str, s: dict[str, Any]) -> str:
    for frag in _secret_fragments(s):
        text = text.replace(frag, "***")
    return text


# --------------------------------------------------------------------------- toast

_XML_INVALID = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _x(s: str) -> str:
    return _escape(_XML_INVALID.sub("", s), {'"': "&quot;", "'": "&apos;"})


def build_toast_xml(title: str, body: str, *, url: str | None = None, level: str = "info") -> str:
    launch = f' activationType="protocol" launch="{_x(url)}"' if url else ""
    duration = ' duration="long"' if level in ("action", "high") else ""
    actions = (f'<actions><action content="打开控制台" activationType="protocol" arguments="{_x(url)}"/></actions>'
               if url else "")
    audio = '<audio src="ms-winsoundevent:Notification.Reminder"/>' if level == "high" else ""
    return (f'<toast{launch}{duration}><visual><binding template="ToastGeneric">'
            f'<text hint-maxLines="2">{_x(title[:200])}</text><text>{_x(_summary(body, 600))}</text>'
            f'<text placement="attribution">AgentWorkOS</text></binding></visual>{actions}{audio}</toast>')


_TOAST_PS = r"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
try { [Console]::OutputEncoding = New-Object System.Text.UTF8Encoding $false } catch { }
try {
  [void][Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType = WindowsRuntime]
  [void][Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType = WindowsRuntime]
  $xml = [Text.Encoding]::UTF8.GetString([Convert]::FromBase64String('__XML__'))
  $doc = New-Object Windows.Data.Xml.Dom.XmlDocument
  $doc.LoadXml($xml)
  $toast = New-Object Windows.UI.Notifications.ToastNotification $doc
  $toast.Tag = '__TAG__'
  $toast.Group = 'AgentWorkOS'
  $notifier = [Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('__APPID__')
  $setting = 'Unknown'
  try { $setting = [string]$notifier.Setting } catch { }
  if ($setting -ne 'Enabled' -and $setting -ne 'Unknown') { [Console]::Error.Write("toasts disabled: $setting"); exit 2 }
  $notifier.Show($toast)
  [Console]::Out.Write("OK setting=$setting")
} catch {
  [Console]::Error.Write($_.Exception.Message)
  exit 1
}
"""


def build_toast_command(xml: str, *, app_id: str = POWERSHELL_AUMID, tag: str = "awos") -> list[str]:
    """powershell.exe argv that shows `xml` as a toast. Text travels as base64 -> no quoting/encoding issues."""
    script = (_TOAST_PS.replace("__XML__", base64.b64encode(xml.encode("utf-8")).decode("ascii"))
              .replace("__TAG__", re.sub(r"[^A-Za-z0-9_-]", "", tag)[:16] or "awos")
              .replace("__APPID__", app_id.replace("'", "''")))
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return [POWERSHELL_EXE, "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden", "-EncodedCommand", encoded]


def _run_powershell(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    return subprocess.run(argv, capture_output=True, timeout=timeout, creationflags=flags, stdin=subprocess.DEVNULL)


def _ps_text(raw: bytes | None) -> str:
    """Decode PowerShell output (UTF-8, or the ANSI code page for host messages) and drop CLIXML progress noise."""
    raw = raw or b""
    try:
        s = raw.decode("utf-8")
    except UnicodeDecodeError:
        s = raw.decode("mbcs" if os.name == "nt" else "utf-8", "replace")
    return re.sub(r"#< CLIXML\s*(<Objs.*?</Objs>)?", "", s, flags=re.S).strip()


def send_toast(m: Message, *, tag: str | None = None) -> str:
    """Show a toast; returns the PowerShell status line. Raises RuntimeError on failure."""
    if os.name != "nt":
        raise RuntimeError("toast is only supported on Windows")
    app_id = str(load_config().get("notify.toast_app_id") or POWERSHELL_AUMID)
    tag = tag or hashlib.sha1(f"{m.title}|{m.url}".encode("utf-8")).hexdigest()[:16]
    argv = build_toast_command(build_toast_xml(m.title, m.body, url=m.url, level=m.level), app_id=app_id, tag=tag)
    try:
        cp = _run_powershell(argv, TOAST_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"toast timed out after {TOAST_TIMEOUT}s") from None
    out, err = _ps_text(cp.stdout), _ps_text(cp.stderr)
    if cp.returncode != 0 or not out.startswith("OK"):
        raise RuntimeError(f"toast failed (exit {cp.returncode}): {err or out}"[:400])
    return out


# --------------------------------------------------------------------------- webhooks (each raises on failure)


def _check(resp: httpx.Response, ok: Callable[[dict], bool]) -> None:
    try:
        data = resp.json()
    except ValueError:
        data = {}
    if resp.status_code >= 400 or not isinstance(data, dict) or not ok(data):
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:200]}")


def _link_md(m: Message) -> str:
    return f"\n\n[打开控制台]({m.url})" if m.url else ""


def _clip_bytes(s: str, limit: int) -> str:
    return s.encode("utf-8")[:limit].decode("utf-8", "ignore")


def _send_wecom(client: httpx.Client, s: dict, m: Message) -> None:
    color = {"high": "warning", "action": "info"}.get(m.level, "comment")
    content = f'**<font color="{color}">{m.title}</font>**\n{m.summary}{_link_md(m)}'
    r = client.post(s["wecom_webhook"], json={"msgtype": "markdown", "markdown": {"content": _clip_bytes(content, 4000)}})
    _check(r, lambda d: d.get("errcode") == 0)


def _send_feishu(client: httpx.Client, s: dict, m: Message) -> None:
    card: dict[str, Any] = {
        "config": {"wide_screen_mode": True},
        "header": {"template": {"high": "red", "action": "orange"}.get(m.level, "blue"),
                   "title": {"tag": "plain_text", "content": m.title[:100]}},
        "elements": [{"tag": "div", "text": {"tag": "lark_md", "content": m.summary or m.title}}],
    }
    if m.url:
        card["elements"].append({"tag": "action", "actions": [{"tag": "button", "type": "primary", "url": m.url,
                                                               "text": {"tag": "plain_text", "content": "打开控制台"}}]})
    payload: dict[str, Any] = {"msg_type": "interactive", "card": card}
    secret = str(s.get("feishu_secret") or "")
    if secret:  # custom-bot signature check: base64(HMAC-SHA256(key=f"{ts}\n{secret}", msg=b""))
        ts = str(int(time.time()))
        payload["timestamp"] = ts
        payload["sign"] = base64.b64encode(hmac.new(f"{ts}\n{secret}".encode(), b"", hashlib.sha256).digest()).decode()
    r = client.post(s["feishu_webhook"], json=payload)
    _check(r, lambda d: d.get("code", d.get("StatusCode", -1)) == 0)


def serverchan_url(key: str) -> str:
    """Server酱 Turbo keys (SCT...) -> sctapi.ftqq.com; Server酱³ keys (sctp<uid>t...) -> <uid>.push.ft07.com."""
    m = re.match(r"^sctp(\d+)t", key)
    return f"https://{m.group(1)}.push.ft07.com/send/{key}.send" if m else f"https://sctapi.ftqq.com/{key}.send"


def _send_serverchan(client: httpx.Client, s: dict, m: Message) -> None:
    r = client.post(serverchan_url(str(s["serverchan_key"]).strip()),
                    data={"title": m.title[:32], "desp": f"{m.summary}{_link_md(m)}"})
    _check(r, lambda d: d.get("code") == 0)


def _send_bark(client: httpx.Client, s: dict, m: Message) -> None:
    payload = {"title": m.title, "body": m.summary or m.title, "group": "AgentWorkOS",
               "level": "timeSensitive" if m.level == "high" else "active"}
    if m.url:
        payload["url"] = m.url
    r = client.post(str(s["bark_url"]).rstrip("/"), json=payload)
    _check(r, lambda d: d.get("code") == 200)


def _send_ntfy(client: httpx.Client, s: dict, m: Message) -> None:
    u = urlsplit(str(s["ntfy_url"]).rstrip("/"))
    base, _, topic = u.path.rpartition("/")
    if not topic:
        raise RuntimeError("ntfy_url must look like https://ntfy.sh/<topic>")
    payload: dict[str, Any] = {"topic": topic, "title": m.title, "message": m.summary or m.title,
                               "priority": {"high": 5, "action": 4}.get(m.level, 3), "tags": ["robot"]}
    if m.url:
        payload["click"] = m.url
        payload["actions"] = [{"action": "view", "label": "打开控制台", "url": m.url}]
    token = str(s.get("ntfy_token") or "")
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    r = client.post(f"{u.scheme}://{u.netloc}{base}/", json=payload, headers=headers)
    _check(r, lambda d: "id" in d)


def _send_pushplus(client: httpx.Client, s: dict, m: Message) -> None:
    r = client.post("https://www.pushplus.plus/send", json={"token": s["pushplus_token"], "title": m.title,
                                                            "content": f"{m.summary}{_link_md(m)}", "template": "markdown"})
    _check(r, lambda d: d.get("code") == 200)


def _send_email(_client: httpx.Client, s: dict, m: Message) -> None:
    user, port = str(s["smtp_user"]), int(s.get("smtp_port") or 465)
    msg = EmailMessage()
    msg["Subject"] = f"[AgentWorkOS] {m.title}"
    msg["From"] = str(s.get("smtp_from") or user)
    msg["To"] = str(s.get("smtp_to") or user)
    msg.set_content(_summary(m.body, EMAIL_BODY_CHARS) + (f"\n\n{m.url}" if m.url else ""))
    if s.get("smtp_starttls") or port == 587:
        with smtplib.SMTP(str(s["smtp_host"]), port, timeout=SMTP_TIMEOUT) as smtp:
            smtp.starttls()
            smtp.login(user, str(s["smtp_password"]))
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(str(s["smtp_host"]), port, timeout=SMTP_TIMEOUT) as smtp:
            smtp.login(user, str(s["smtp_password"]))
            smtp.send_message(msg)


# channel -> (required secret keys in var/secrets.toml [notify], sender)
WEBHOOKS: dict[str, tuple[tuple[str, ...], Callable[[httpx.Client, dict, Message], None]]] = {
    "wecom": (("wecom_webhook",), _send_wecom),
    "feishu": (("feishu_webhook",), _send_feishu),
    "serverchan": (("serverchan_key",), _send_serverchan),
    "bark": (("bark_url",), _send_bark),
    "ntfy": (("ntfy_url",), _send_ntfy),
    "pushplus": (("pushplus_token",), _send_pushplus),
    "email": (("smtp_host", "smtp_user", "smtp_password"), _send_email),
}


# --------------------------------------------------------------------------- dispatch


def _deliver(channel: str, m: Message, secrets: dict[str, Any], client: httpx.Client) -> tuple[str, str | None]:
    try:
        if channel == "toast":
            send_toast(m)
        elif channel in WEBHOOKS:
            keys, sender = WEBHOOKS[channel]
            if not all(str(secrets.get(k) or "").strip() for k in keys):
                return "failed", f"{channel} not configured in var/secrets.toml [notify] ({', '.join(keys)})"
            sender(client, secrets, m)
        else:
            return "failed", f"unknown channel {channel!r}"
        return "sent", None
    except Exception as e:  # never raise: record the failure (scrubbed of secrets)
        err = _redact(f"{type(e).__name__}: {e}", secrets)[:500]
        log.warning("notify via %s failed: %s", channel, err)
        return "failed", err


def post_webhook(channel: str, title: str, text: str) -> dict[str, Any]:
    """Approved `post_message` action (hitl/outbox): post the full user-approved text to a configured group bot.

    No quiet hours / dedupe / truncation to a summary (the human approved this exact content). Never raises.
    """
    if channel not in WEBHOOKS:
        return {"done": False, "channel": channel, "error": f"unsupported webhook channel {channel!r}"}
    secrets = _notify_secrets()
    m = Message(title=(title or "").strip() or "AgentWorkOS", body=text or "", level="info", summary=text or "")
    with httpx.Client(timeout=HTTP_TIMEOUT, transport=HTTP_TRANSPORT, headers={"User-Agent": "AgentWorkOS/0.1"}) as client:
        status, err = _deliver(channel, m, secrets, client)
    if status == "sent":
        return {"done": True, "channel": channel, "details": f"posted to {channel}"}
    return {"done": False, "channel": channel, "error": err}


def notify(title: str, body: str = "", *, level: str = "info", ref_type: str | None = None, ref_id: str | None = None,
           url: str | None = None, channels: list[str] | None = None, dedupe: bool = True) -> list[str]:
    """Send a notification on all configured channels; returns the notification row ids ([] if de-duplicated)."""
    try:
        return _notify(title, body, level=level, ref_type=ref_type, ref_id=ref_id, url=url, channels=channels,
                       dedupe=dedupe)
    except Exception as e:  # pragma: no cover - last-resort guard; notify must never break the caller
        log.exception("notify crashed")
        try:
            return [repo.add_notification("notify", (title or "")[:300], body, ref_type, ref_id, "failed",
                                          f"{type(e).__name__}: {e}"[:300])]
        except Exception:
            return []


def _notify(title: str, body: str, *, level: str, ref_type: str | None, ref_id: str | None, url: str | None,
            channels: list[str] | None, dedupe: bool) -> list[str]:
    cfg = load_config()
    level = level if level in LEVELS else "info"
    title = (title or "").strip() or "AgentWorkOS"
    body = body or ""
    if dedupe and is_duplicate(title, ref_id):
        return []
    secrets = _notify_secrets()
    chans = list(dict.fromkeys(channels if channels is not None else configured_channels(secrets)))

    reason = None
    if os.environ.get("AWOS_NOTIFY_DISABLE"):
        reason = "disabled by AWOS_NOTIFY_DISABLE"
    elif level != "high" and in_quiet_hours():
        reason = "quiet hours"
    if reason:
        return [repo.add_notification(ch, title, body, ref_type, ref_id, "suppressed", reason) for ch in chans]

    min_level = str(cfg.get("notify.webhook_min_level") or "info")
    min_idx = LEVELS.index(min_level) if min_level in LEVELS else 0
    m = Message(title=title, body=body, level=level, url=url,
                summary=_summary(body, int(cfg.get("notify.phone_body_chars") or 200)))
    results: dict[str, tuple[str, str | None]] = {}
    targets = []
    for ch in chans:
        if ch != "toast" and LEVELS.index(level) < min_idx:
            results[ch] = ("suppressed", f"below notify.webhook_min_level={min_level}")
        else:
            targets.append(ch)
    if targets:
        with httpx.Client(timeout=HTTP_TIMEOUT, transport=HTTP_TRANSPORT,
                          headers={"User-Agent": "AgentWorkOS/0.1"}) as client:
            with ThreadPoolExecutor(max_workers=min(8, len(targets))) as pool:
                futs = {ch: pool.submit(_deliver, ch, m, secrets, client) for ch in targets}
                results.update({ch: f.result() for ch, f in futs.items()})
    return [repo.add_notification(ch, title, body, ref_type, ref_id, *results[ch]) for ch in chans]
