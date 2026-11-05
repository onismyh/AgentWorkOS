"""Files sensor: recently modified documents, coalesced per project folder per ISO week, plus deadline-ish
hints in file names (截止 / deadline / DDL / 提交 / 答辩 / upcoming dates such as 2026-11-20 or 1120)."""
from __future__ import annotations

import os
import re
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from ..repo import fingerprint
from ..timeutil import local_today
from ._common import SKIP_DIRS, is_under, iso_week, local_dt, local_str, norm, self_excluded, utc_iso
from .base import Sensor, SensorContext, SignalDraft

DEFAULT_EXT = (".docx", ".doc", ".pptx", ".xlsx", ".pdf", ".md", ".tex", ".txt")
TEMP_PREFIX = ("~$", ".~", "~WRL", ".#")
TEMP_SUFFIX = (".tmp", ".crdownload", ".part", ".swp", ".bak")
MAX_FILES_PER_SIGNAL = 50

_KW_RE = re.compile(r"(截止|deadline|ddl|提交|答辩|投稿|due)", re.I)
_FULL_DATE_RE = re.compile(r"(?<!\d)(20\d{2})[-_.年]?(0[1-9]|1[0-2])[-_.月]?(0[1-9]|[12]\d|3[01])(?!\d)")
_MD_RE = re.compile(r"(?<![\dA-Za-z])(0[1-9]|1[0-2])[-_.]?(0[1-9]|[12]\d|3[01])(?!\d)")


def deadline_hint(filename: str, today: date, ahead_days: int = 60) -> dict[str, Any] | None:
    """Keyword in the name, or an upcoming date (today..today+ahead_days) embedded in it."""
    stem = Path(filename).stem
    kw = _KW_RE.search(stem)
    when: date | None = None
    for m in _FULL_DATE_RE.finditer(stem):
        try:
            d = date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
        except ValueError:
            continue
        if today <= d <= today + timedelta(days=ahead_days) or (kw and abs((d - today).days) <= ahead_days):
            when = d
            break
    if when is None:
        for m in _MD_RE.finditer(_FULL_DATE_RE.sub(" ", stem)):
            for y in (today.year, today.year + 1):
                try:
                    d = date(y, int(m.group(1)), int(m.group(2)))
                except ValueError:
                    continue
                if today <= d <= today + timedelta(days=ahead_days):
                    when = d
                    break
            if when:
                break
    if not kw and not when:
        return None
    return {"keyword": kw.group(1).lower() if kw else None, "date": when.isoformat() if when else None}


def scan(roots: list[str], exts: set[str], cutoff: float, max_depth: int, exclude: list[str]) -> list[dict[str, Any]]:
    """Walk roots (depth-limited, heavy dirs skipped) and return documents modified after `cutoff`."""
    skip_paths = [norm(x) for x in exclude]
    out: list[dict[str, Any]] = []

    def walk(root: str, d: str, depth: int, rel: tuple[str, ...]) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        for e in entries:
            try:
                if e.is_dir(follow_symlinks=False):
                    if (depth < max_depth and not e.is_junction() and e.name not in SKIP_DIRS
                            and not e.name.startswith(".") and not self_excluded(e.path)
                            and not any(is_under(e.path, s) for s in skip_paths)):
                        walk(root, e.path, depth + 1, rel + (e.name,))
                    continue
                n = e.name
                if n.startswith(TEMP_PREFIX) or n.lower().endswith(TEMP_SUFFIX) or os.path.splitext(n)[1].lower() not in exts:
                    continue
                st = e.stat()
            except OSError:
                continue
            if st.st_mtime >= cutoff:
                out.append({"root": root, "dirs": rel, "name": n, "path": e.path, "mtime": st.st_mtime, "size": st.st_size})

    for r in roots:
        if os.path.isdir(r) and not self_excluded(r):
            r = os.path.normpath(r)
            walk(r, r, 1, ())
    return out


def _project(f: dict[str, Any], depth: int = 1) -> tuple[str, str]:
    """Project folder = the root plus its first `depth` sub-folder level(s): `D:/papers/<project>`."""
    parts = f["dirs"][:depth]
    path = os.path.join(f["root"], *parts)
    label = "/".join((Path(f["root"]).name, *parts))
    return path, label


def _size(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


class FilesSensor(Sensor):
    name = "files"

    def _scan(self, ctx: SensorContext) -> list[dict[str, Any]]:
        cfg = ctx.config
        exts = {e.lower() for e in cfg.get("extensions", DEFAULT_EXT)}
        cutoff = time.time() - float(cfg.get("lookback_days", 7)) * 86400
        return scan([str(r) for r in cfg.get("roots", [])], exts, cutoff, int(cfg.get("max_depth", 5)),
                    list(cfg.get("exclude", [])))

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        files = self._scan(ctx)
        week = iso_week()
        today = local_today()
        week_start = today - timedelta(days=today.weekday())
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        labels: dict[str, str] = {}
        depth = int(ctx.config.get("group_depth", 1))
        for f in files:
            p, label = _project(f, depth)
            groups[p].append(f)
            labels[p] = label
        out: list[SignalDraft] = []
        ahead = int(ctx.config.get("deadline_ahead_days", 60))
        for proj, fs in groups.items():
            fs.sort(key=lambda f: -f["mtime"])
            this_week = [f for f in fs if local_dt(f["mtime"]).date() >= week_start]
            if this_week:  # one coalesced signal per active folder per ISO week
                listing = [{"path": os.path.relpath(f["path"], proj), "mtime": local_str(f["mtime"]), "size": f["size"]}
                           for f in fs[:MAX_FILES_PER_SIGNAL]]
                body = "\n".join(f"- {x['mtime']} · {x['path']} ({_size(x['size'])})" for x in listing[:12])
                if len(fs) > 12:
                    body += f"\n- …另有 {len(fs) - 12} 个文件"
                out.append(SignalDraft(
                    title=f"文档活动 · {labels[proj]}: 近 {ctx.config.get('lookback_days', 7)} 天 {len(fs)} 个文档有改动",
                    fingerprint=fingerprint("files", norm(proj), "active", week),
                    body=f"文件夹: {proj}\n{body}", url=Path(proj).as_uri(), external_id=proj,
                    occurred_at=utc_iso(fs[0]["mtime"]),
                    payload={"project": proj, "label": labels[proj], "week": week, "count": len(fs),
                             "this_week": len(this_week), "files": listing}))
            hints: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = defaultdict(list)
            for f in fs:
                h = deadline_hint(f["name"], today, ahead)
                if h:
                    hints[h["date"] or h["keyword"]].append((f, h))
            for key, items in hints.items():
                h = items[0][1]
                names = [os.path.relpath(f["path"], proj) for f, _ in items[:20]]
                what = f"日期 {h['date']}" if h["date"] else f"关键词「{h['keyword']}」"
                out.append(SignalDraft(
                    title=f"截止线索 · {labels[proj]}: {Path(names[0]).name}（{what}）",
                    fingerprint=fingerprint("files", norm(proj), "deadline_hint", key),
                    body=f"文件名里出现了{what}，可能对应某个截止日期/提交/答辩：\n" + "\n".join(f"- {n}" for n in names),
                    url=Path(items[0][0]["path"]).as_uri(), external_id=items[0][0]["path"],
                    occurred_at=utc_iso(items[0][0]["mtime"]),
                    payload={"project": proj, "label": labels[proj], "hint": h, "files": names}))
        return out

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        files = self._scan(ctx)
        if not files:
            return None
        groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for f in files:
            groups[_project(f, int(ctx.config.get("group_depth", 1)))[1]].append(f)
        ranked = sorted(groups.items(), key=lambda kv: -max(f["mtime"] for f in kv[1]))
        lines = [f"### 最近 {ctx.config.get('lookback_days', 7)} 天有改动的文档（{len(files)} 个，{len(groups)} 个文件夹）"]
        for label, fs in ranked[:12]:
            fs.sort(key=lambda f: -f["mtime"])
            recent = "、".join(f["name"] for f in fs[:3])
            lines.append(f"- **{label}**：{len(fs)} 个，最近 {local_str(fs[0]['mtime'])}（{recent}）")
        return "\n".join(lines)


SENSOR = FilesSensor()
