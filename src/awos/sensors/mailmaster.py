"""NetEase Mail Master sensor: new mail metadata from the client's local, unencrypted SQLite store.

`<root>/<account>/mail.db` → table MailMeta (Subject, Summary preview, Froms JSON {"mail","name"}, ReceivedDate ms,
Unread, Mailbox → Mailbox.Type). Read-only (`mode=ro` → `immutable=1` → temp copy). Never opens AccountConfig,
app.db, contacts.db or content.db (bodies). Signals carry subject, sender name + domain only, a ≤300-char
scrubbed preview, and a local pointer (mail:<account-hash>:<LocalId>) so a later step can re-read on demand.
"""
from __future__ import annotations

import json
import re
import sqlite3
import time
from pathlib import Path

from ..repo import fingerprint
from ._common import local_str, scrub_pii, sqlite_ro, trunc
from .base import Sensor, SensorContext, SignalDraft

# Mailbox.Type: 1 inbox, 2 drafts, 3 sent, 4 trash, 5 junk, 7 ads(广告), 18 subscriptions(订阅), 20 virus, <0 local/virtual
NEVER_BOX = {2, 3, 4, 20, -10, -20, -21, -22}
BULK_BOX = {5, 7, 18}
MAX_PER_ACCOUNT = 60

_BULK_LOCAL = re.compile(r"^(no-?reply|do-?not-?reply|donotreply|newsletters?|news|marketing|promo(tions?)?|edm|"
                         r"mailer(-daemon)?|bounces?|digest|notifications?|campaigns?|deals|offers)([._+-]|$)", re.I)
_BULK_DOMAIN = re.compile(r"(^|\.)(mailchimp|mcsv|list-manage|sendgrid|mailgun|amazonses|sendcloud|mktomail|hubspot|"
                          r"sailthru|exacttarget|substack|medium|linkedin|quora|zhihu|douban|bilibili|taobao|tmall|jd|"
                          r"meituan|ctrip|pinduoduo)\.|^(news|email|e|mkt|edm|marketing|promo|info|mail\d*|em)\.", re.I)
_BULK_TEXT = re.compile(r"(退订|取消订阅|unsubscribe|newsletter|周刊|月刊|weekly digest|daily digest|促销|优惠券?|折扣|限时|特价|"
                        r"秒杀|% ?off\b|\bsale\b|webinar|广告)", re.I)
# Things the user must not miss even if they look automated (journals, conferences, university, HR, funding).
_KEEP_DOMAIN = re.compile(r"(\.edu(\.[a-z]{2})?|\.ac\.[a-z]{2}|\.gov(\.[a-z]{2})?|tsinghua|elsevier|sciencedirect|springer|"
                          r"nature\.com|wiley|tandf|mdpi|ieee|acs\.org|rsc\.org|iop\.org|frontiersin|plos|cell\.com|science\.org|"
                          r"oup\.com|cambridge\.org|sagepub|editorialmanager|manuscriptcentral|scholarone|cnki|nsfc|"
                          r"copernicus|agu\.org|ieaghg|ghgt|aaas|aps\.org|researchgate|orcid|easychair|openreview)", re.I)
_KEEP_TEXT = re.compile(r"(审稿|投稿|稿件|录用|返修|修改稿|校样|manuscript|submission|revis|reviewer|referee|decision|accepted|"
                        r"proof|deadline|截止|会议|conference|abstract|registration|invitation|邀请|面试|笔试|offer|录取|招聘|"
                        r"interview|答辩|开题|中期|学位|奖学金|报销|导师|课题|基金|申请)", re.I)


def _from(froms: str | None) -> tuple[str, str, str]:
    """-> (name, local_part, domain) from the Froms JSON (dict or list of dicts)."""
    try:
        j = json.loads(froms or "{}")
    except ValueError:
        j = {}
    if isinstance(j, list):
        j = j[0] if j else {}
    addr = str(j.get("mail") or "")
    local, _, domain = addr.partition("@")
    return trunc(j.get("name") or "", 60), local, domain.lower()


def classify(subject: str, summary: str, local: str, domain: str, sender: str | None, box_type: int | None,
             is_promotion: int | None) -> str:
    """'keep' | 'bulk' (skipped) | 'never' (sent/drafts/trash...)."""
    if box_type in NEVER_BOX:
        return "never"
    important = bool(_KEEP_DOMAIN.search(domain) or _KEEP_TEXT.search(subject))
    bulk = (box_type in BULK_BOX or bool(is_promotion) or bool(_BULK_LOCAL.match(local)) or bool(_BULK_DOMAIN.search(domain))
            or bool(_BULK_TEXT.search(subject)) or bool(_BULK_TEXT.search(summary))
            or bool(re.search(r"bounce|list|mailer", sender or "", re.I)))
    return "keep" if important or not bulk else "bulk"


def accounts(root: Path) -> list[Path]:
    return sorted(p for p in root.iterdir() if p.is_dir() and (p / "mail.db").is_file()) if root.is_dir() else []


def account_label(d: Path) -> str:
    """Directory names are '<user>@<domain>_<n>'; expose only the domain part."""
    return d.name.rpartition("@")[2] or fingerprint("mm", d.name)[:8]


class MailMasterSensor(Sensor):
    name = "mailmaster"

    def _rows(self, db: Path, since_ms: float, limit: int = 500) -> tuple[list[sqlite3.Row], dict[str, int]]:
        with sqlite_ro(db) as c:
            boxes = {str(r["Id"]): r["Type"] for r in c.execute("SELECT Id, Type FROM Mailbox")}
            rows = c.execute(
                "SELECT LocalId, MessageId, RemoteId, ReceivedDate, Subject, Summary, Froms, Sender, Unread, Mailbox, "
                "IsPromotion, HasAttachments, has_calendar FROM MailMeta "
                "WHERE ReceivedDate > ? AND COALESCE(Deleted, 0) = 0 ORDER BY ReceivedDate LIMIT ?", (since_ms, limit)).fetchall()
        return rows, boxes

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        cfg, cur = ctx.config, ctx.cursor
        root = Path(cfg.get("root", "D:/MailMasterData"))
        lookback_ms = (time.time() - float(cfg.get("lookback_days", 3)) * 86400) * 1000
        wms: dict[str, float] = cur.setdefault("watermarks", {})
        out: list[SignalDraft] = []
        skipped = 0
        for d in accounts(root):
            akey = fingerprint("mailmaster-account", d.name)[:12]
            label = account_label(d)
            since = max(float(wms.get(akey) or 0), lookback_ms)
            try:
                rows, boxes = self._rows(d / "mail.db", since)
            except (sqlite3.Error, OSError) as e:
                ctx.log.warning("mailmaster %s unreadable: %s", label, e)
                continue
            kept = 0
            for r in rows:
                wms[akey] = max(float(wms.get(akey) or 0), float(r["ReceivedDate"] or 0))
                subject = trunc(r["Subject"] or "(无主题)", 200)
                summary = r["Summary"] or ""
                name, local, domain = _from(r["Froms"])
                verdict = classify(subject, summary, local, domain, r["Sender"], boxes.get(str(r["Mailbox"])), r["IsPromotion"])
                if verdict != "keep" or kept >= MAX_PER_ACCOUNT:
                    skipped += verdict == "bulk"
                    continue
                kept += 1
                received = float(r["ReceivedDate"]) / 1000
                snippet = trunc(scrub_pii(summary), 300)
                mid = r["MessageId"] or r["RemoteId"] or str(r["LocalId"])
                who = name or domain or "?"
                out.append(SignalDraft(
                    title=f"邮件 · {who}{' @' + domain if name and domain else ''}: {subject}",
                    fingerprint=fingerprint("mailmaster", akey, mid),
                    body=f"账户: {label}\n发件人: {name or '-'}（@{domain or '?'}）\n收到: {local_str(received)}\n"
                         f"未读: {'是' if r['Unread'] else '否'}" + ("\n含附件" if r["HasAttachments"] else "")
                         + ("\n含日历邀请" if r["has_calendar"] else "") + f"\n\n预览: {snippet}",
                    external_id=f"mail:{akey}:{r['LocalId']}",
                    occurred_at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(received)),
                    payload={"account": label, "subject": subject, "from_name": name, "from_domain": domain,
                             "received_at": local_str(received), "unread": bool(r["Unread"]),
                             "has_attachments": bool(r["HasAttachments"]), "has_calendar": bool(r["has_calendar"]),
                             "snippet": snippet, "ref": f"mail:{akey}:{r['LocalId']}"}))
        cur["last_skipped_bulk"] = skipped
        return out

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        root = Path(ctx.config.get("root", "D:/MailMasterData"))
        since = (time.time() - 3 * 86400) * 1000
        lines = []
        for d in accounts(root):
            try:
                rows, boxes = self._rows(d / "mail.db", since)
            except (sqlite3.Error, OSError):
                continue
            keep = []
            for r in rows:
                name, local, domain = _from(r["Froms"])
                if classify(r["Subject"] or "", r["Summary"] or "", local, domain, r["Sender"],
                            boxes.get(str(r["Mailbox"])), r["IsPromotion"]) == "keep":
                    keep.append((r, name or domain))
            if not keep:
                continue
            unread = sum(1 for r, _ in keep if r["Unread"])
            lines.append(f"- **{account_label(d)}**：近 3 天 {len(keep)} 封非群发邮件，{unread} 封未读")
            for r, who in keep[-6:][::-1]:
                lines.append(f"  - {local_str(r['ReceivedDate'] / 1000)} {who}: {trunc(r['Subject'], 70)}"
                             + (" (未读)" if r["Unread"] else ""))
        return ("### 邮箱大师（学校 / 163）\n" + "\n".join(lines)) if lines else None


SENSOR = MailMasterSensor()
