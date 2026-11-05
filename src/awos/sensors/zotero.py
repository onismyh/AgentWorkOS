"""Zotero sensor: new library items since the dateAdded watermark.

Primary: Zotero's local API (127.0.0.1:23119, no proxy). Fallback: the live `zotero.sqlite` opened read-only
(`immutable=1` because Zotero holds an exclusive lock), or a temp copy. The data dir comes from config
`data_dir`, else Zotero's prefs.js (`extensions.zotero.dataDir`), else ~/Zotero.
"""
from __future__ import annotations

import re
import sqlite3
import time
from collections import Counter
from pathlib import Path
from typing import Any

import httpx

from ..repo import fingerprint
from ._common import parse_iso, sqlite_ro, trunc, utc_iso
from .base import Sensor, SensorContext, SignalDraft

API = "http://127.0.0.1:23119/api/users/0"
SKIP_TYPES = {"attachment", "note", "annotation"}
PAGE = 25
MAX_PAGES = 8


def _creators(cs: list[dict[str, Any]]) -> list[str]:
    out = []
    for c in cs[:3]:
        out.append(c.get("lastName") or c.get("name") or c.get("firstName") or "")
    return [x for x in out if x] + (["et al."] if len(cs) > 3 else [])


def _year(date_s: str | None) -> str | None:
    m = re.search(r"(19|20)\d{2}", date_s or "")
    return m.group(0) if m else None


def _item(d: dict[str, Any]) -> dict[str, Any]:
    return {"key": d.get("key"), "title": trunc(d.get("title") or "(untitled)", 300), "item_type": d.get("itemType"),
            "authors": _creators(d.get("creators") or []), "year": _year(d.get("date")),
            "venue": trunc(d.get("publicationTitle") or d.get("proceedingsTitle") or d.get("publisher") or "", 120) or None,
            "doi": d.get("DOI") or None, "date_added": d.get("dateAdded"),
            "abstract": trunc(d.get("abstractNote") or "", 300) or None,
            "tags": [t.get("tag") for t in d.get("tags") or [] if t.get("tag")][:8],
            "collections": d.get("collections") or []}


def _client() -> httpx.Client:
    return httpx.Client(base_url=API, trust_env=False, timeout=15)  # localhost: never through the system proxy


def fetch_api(since_ts: float, limit_pages: int = MAX_PAGES) -> list[dict[str, Any]]:
    """Top-level items with dateAdded > since_ts, newest first, via the local API."""
    out: list[dict[str, Any]] = []
    with _client() as c:
        for page in range(limit_pages):
            r = c.get("/items/top", params={"sort": "dateAdded", "direction": "desc", "limit": PAGE,
                                            "start": page * PAGE, "format": "json"})
            r.raise_for_status()
            batch = r.json()
            stop = False
            for it in batch:
                d = it.get("data") or {}
                ts = parse_iso(d.get("dateAdded"))
                if ts is None or ts <= since_ts:
                    stop = True
                    break
                if d.get("itemType") not in SKIP_TYPES:
                    out.append(_item(d))
            if stop or len(batch) < PAGE:
                break
    return out


def collection_names() -> dict[str, str]:
    try:
        with _client() as c:
            r = c.get("/collections", params={"limit": 100, "format": "json"})
            r.raise_for_status()
            return {x["key"]: (x.get("data") or {}).get("name", "") for x in r.json()}
    except (httpx.HTTPError, ValueError, KeyError):
        return {}


def data_dir(cfg: dict[str, Any]) -> Path:
    if cfg.get("data_dir"):
        return Path(cfg["data_dir"])
    for prefs in (Path.home() / "AppData/Roaming/Zotero/Zotero/Profiles").glob("*/prefs.js"):
        m = re.search(r'user_pref\("extensions\.zotero\.dataDir",\s*"(.+?)"\)', prefs.read_text(encoding="utf-8", errors="replace"))
        if m:
            return Path(m.group(1).replace("\\\\", "\\"))
    return Path.home() / "Zotero"


def fetch_sqlite(db: Path, since_ts: float, limit: int = 200) -> list[dict[str, Any]]:
    """Same result shape as fetch_api, from zotero.sqlite (dateAdded is 'YYYY-MM-DD HH:MM:SS' UTC)."""
    since = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since_ts))
    with sqlite_ro(db) as c:
        rows = c.execute(
            """SELECT i.itemID, i.key, i.dateAdded, t.typeName FROM items i JOIN itemTypes t ON t.itemTypeID = i.itemTypeID
               WHERE i.dateAdded > ? AND t.typeName NOT IN ('attachment','note','annotation')
                 AND i.itemID NOT IN (SELECT itemID FROM deletedItems)
               ORDER BY i.dateAdded DESC LIMIT ?""", (since, limit)).fetchall()
        out = []
        for r in rows:
            fields = {f: v for f, v in c.execute(
                """SELECT f.fieldName, v.value FROM itemData d JOIN fields f ON f.fieldID = d.fieldID
                   JOIN itemDataValues v ON v.valueID = d.valueID WHERE d.itemID = ?""", (r["itemID"],))}
            creators = [{"lastName": x[0], "firstName": x[1]} for x in c.execute(
                """SELECT c.lastName, c.firstName FROM itemCreators ic JOIN creators c ON c.creatorID = ic.creatorID
                   WHERE ic.itemID = ? ORDER BY ic.orderIndex""", (r["itemID"],))]
            out.append(_item({**fields, "key": r["key"], "itemType": r["typeName"], "creators": creators,
                              "dateAdded": r["dateAdded"].replace(" ", "T") + "Z"}))
        return out


class ZoteroSensor(Sensor):
    name = "zotero"

    def _fetch(self, ctx: SensorContext, since_ts: float) -> tuple[list[dict[str, Any]], str]:
        try:
            return fetch_api(since_ts), "api"
        except (httpx.HTTPError, ValueError) as e:
            ctx.log.info("zotero local API unavailable (%s); falling back to sqlite", e)
        db = data_dir(ctx.config) / "zotero.sqlite"
        return fetch_sqlite(db, since_ts), "sqlite"

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        cur = ctx.cursor
        since = float(cur.get("since_ts") or time.time() - float(ctx.config.get("lookback_days", 7)) * 86400)
        items, via = self._fetch(ctx, since)
        if not items:
            cur["since_ts"] = since
            return []
        newest = max(parse_iso(i["date_added"]) or since for i in items)
        cur["since_ts"] = max(since, newest)
        if via == "api":
            names = collection_names()
            for i in items:
                i["collections"] = [names.get(k, k) for k in i["collections"]]
        items.sort(key=lambda i: i["date_added"] or "", reverse=True)
        keys = sorted(i["key"] for i in items)
        lines = []
        for i in items[:30]:
            who = ", ".join(i["authors"][:2]) + (" et al." if len(i["authors"]) > 2 else "")
            lines.append(f"- {i['title']}（{who or '佚名'}, {i['year'] or 'n.d.'}）" + (f" — {i['venue']}" if i["venue"] else ""))
        return [SignalDraft(
            title=f"新加入 Zotero 的 {len(items)} 篇文献",
            fingerprint=fingerprint("zotero", "new_items", *keys),
            body="\n".join(lines), url="zotero://select/library/items", external_id=",".join(keys[:50]),
            occurred_at=utc_iso(newest), payload={"via": via, "count": len(items), "items": items[:50]})]

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        try:
            items, via = self._fetch(ctx, time.time() - 90 * 86400)
        except (sqlite3.Error, OSError, httpx.HTTPError):
            return None
        if not items:
            return None
        names = collection_names() if via == "api" else {}
        tags = Counter(t for i in items for t in i["tags"])
        colls = Counter(names.get(k, k) for i in items for k in i["collections"])
        lines = [f"### Zotero：近 90 天新增 {len(items)} 篇"]
        if colls:
            lines.append("- 分类：" + "、".join(f"{k}（{v}）" for k, v in colls.most_common(6)))
        if tags:
            lines.append("- 标签：" + "、".join(f"{k}（{v}）" for k, v in tags.most_common(8)))
        for i in items[:8]:
            lines.append(f"- {(i['date_added'] or '')[:10]} {trunc(i['title'], 90)}（{i['year'] or 'n.d.'}）")
        return "\n".join(lines)


SENSOR = ZoteroSensor()
