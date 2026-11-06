"""Agent-history sensor: what the user has been doing with AI agents (Claude Code prompts, Codex threads,
claude-mem observations/summaries), coalesced per (project, local day).

All sources are read-only. Claude `history.jsonl` is read from a byte-offset watermark (the start of the lookback
window), so a large file is never re-read from the top. `pastedContents` is never read. AgentWorkOS' own sessions
(cwd under the AgentWorkOS root, its var/runs/workspaces dirs, or the temp dir) and Codex exec / subagent /
guardian threads are excluded (`codex exec --ephemeral` runs leave no thread rows at all).
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import tempfile
import time
from collections import defaultdict
from datetime import timedelta
from pathlib import Path
from typing import Any

from ..osutil import path_basename
from ..repo import fingerprint
from ..timeutil import local_today
from ._common import is_under, local_dt, norm, parse_iso, redact, self_excluded, sqlite_ro, trunc, utc_iso
from .base import Sensor, SensorContext, SignalDraft

HOME = Path.home()
MAX_PROMPTS = 15
TAIL_BYTES = 8 << 20  # first run on a huge history file: only look at the last 8 MB
_SLASH_ONLY = re.compile(r"^/[\w:-]+(\s+\S+)?\s*$")
_TEMP = tempfile.gettempdir()


def _excluded(project: str) -> bool:
    """AgentWorkOS' own runs (cwd under its root / var / runs / workspaces) and temp-dir exec sessions."""
    return not project or self_excluded(project) or is_under(project, _TEMP)


def _bucket(n: int) -> int:
    """Coarse count bucket: a (project, day) re-signals only when activity grows materially."""
    for i, edge in enumerate((1, 3, 6, 11, 21, 41)):
        if n < edge:
            return i
    return 6


def _paths(cfg: dict[str, Any]) -> tuple[Path, Path, Path]:
    return (Path(cfg.get("claude_history") or HOME / ".claude" / "history.jsonl"),
            Path(cfg.get("codex_home") or HOME / ".codex"),
            Path(cfg.get("claude_mem_db") or HOME / ".claude-mem" / "claude-mem.db"))


def read_claude(path: Path, cutoff: float, cursor: dict[str, Any]) -> list[dict[str, Any]]:
    """User prompts newer than `cutoff` (epoch s). Updates cursor['claude_offset'] to the window start."""
    if not path.is_file():
        return []
    size = path.stat().st_size
    off = int(cursor.get("claude_offset") or 0)
    if off > size:  # rotated / truncated
        off = 0
    out: list[dict[str, Any]] = []
    window_start: int | None = None
    with path.open("rb") as f:
        if off == 0 and size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
            f.readline()  # drop the partial line
        else:
            f.seek(off)
        pos = f.tell()
        for raw in f:
            start, pos = pos, pos + len(raw)
            try:
                r = json.loads(raw)
            except ValueError:
                continue
            ts = r.get("timestamp")
            if not isinstance(ts, (int, float)) or ts / 1000 < cutoff:
                continue
            if window_start is None:
                window_start = start
            text = (r.get("display") or "").strip()
            project = r.get("project") or ""
            if not text or _SLASH_ONLY.match(text) or _excluded(project):
                continue
            out.append({"src": "claude", "project": project, "ts": ts / 1000, "text": redact(trunc(text, 200)),
                        "session": (r.get("sessionId") or "")[:8]})
    cursor["claude_offset"] = window_start if window_start is not None else pos
    return out


def _codex_state_db(codex_home: Path) -> Path | None:
    cands = sorted(codex_home.glob("state_*.sqlite"),
                   key=lambda p: int(re.sub(r"\D", "", p.stem) or 0), reverse=True)
    return cands[0] if cands else None


def read_codex(codex_home: Path, cutoff: float) -> list[dict[str, Any]]:
    """User-initiated Codex threads updated after `cutoff` (state_N.sqlite; session_index.jsonl fallback)."""
    db = _codex_state_db(codex_home)
    out: list[dict[str, Any]] = []
    if db is not None:
        try:
            with sqlite_ro(db) as c:
                cols = {r[1] for r in c.execute("PRAGMA table_info(threads)")}
                upd = "updated_at_ms" if "updated_at_ms" in cols else "updated_at * 1000"
                title = "COALESCE(NULLIF(name, ''), title)" if "name" in cols else "title"
                where = [f"{upd} >= ?", "source != 'exec'"]
                if "thread_source" in cols:
                    where.append("thread_source = 'user'")
                else:
                    where.append("source NOT LIKE '%subagent%'")
                branch = "git_branch" if "git_branch" in cols else "NULL"
                rows = c.execute(f"SELECT id, cwd, {title} AS title, {upd} AS upd, {branch} AS branch FROM threads "
                                 f"WHERE {' AND '.join(where)} ORDER BY upd DESC LIMIT 500", (cutoff * 1000,)).fetchall()
            for r in rows:
                cwd = str(r["cwd"] or "").removeprefix("\\\\?\\")
                if _excluded(cwd):
                    continue
                out.append({"src": "codex", "project": cwd, "ts": r["upd"] / 1000, "text": redact(trunc(r["title"], 200)),
                            "id": r["id"], "branch": r["branch"]})
            return out
        except (sqlite3.Error, OSError):
            pass
    idx = codex_home / "session_index.jsonl"
    if idx.is_file():
        for line in idx.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            ts = parse_iso(r.get("updated_at"))
            if ts and ts >= cutoff:
                out.append({"src": "codex", "project": "(codex)", "ts": ts, "id": r.get("id"),
                            "text": redact(trunc(r.get("thread_name"), 200)), "branch": None})
    return out


def read_mem(db: Path, cutoff: float) -> tuple[list[dict[str, Any]], float | None]:
    """claude-mem observation titles + session summaries (request / next_steps). Returns (entries, last_ts)."""
    if not db.is_file():
        return [], None
    out: list[dict[str, Any]] = []
    with sqlite_ro(db) as c:
        last = c.execute("SELECT MAX(created_at_epoch) FROM observations").fetchone()[0]
        scale = 1000 if (last or 0) > 1e11 else 1
        cut = cutoff * scale
        for r in c.execute("SELECT project, title, type, created_at_epoch AS t FROM observations "
                           "WHERE created_at_epoch >= ? AND title IS NOT NULL ORDER BY t DESC LIMIT 300", (cut,)):
            out.append({"src": "mem", "kind": "observation", "project": r["project"], "ts": r["t"] / scale,
                        "text": redact(trunc(r["title"], 160)), "type": trunc(r["type"], 40)})
        for r in c.execute("SELECT project, request, next_steps, created_at_epoch AS t FROM session_summaries "
                           "WHERE created_at_epoch >= ? ORDER BY t DESC LIMIT 50", (cut,)):
            out.append({"src": "mem", "kind": "summary", "project": r["project"], "ts": r["t"] / scale,
                        "text": redact(trunc(r["request"], 160)), "next_steps": redact(trunc(r["next_steps"], 200))})
    return out, (last / scale if last else None)


def _resolve_mem_projects(entries: list[dict[str, Any]]) -> None:
    """claude-mem stores project basenames; map them onto full paths seen in Claude/Codex activity."""
    by_name = {path_basename(e["project"]).lower(): e["project"] for e in entries if e["src"] != "mem" and e["project"]}
    for e in entries:
        if e["src"] == "mem":
            e["project"] = by_name.get(str(e["project"]).lower(), e["project"])


class AgentHistorySensor(Sensor):
    name = "agent_history"

    def _gather(self, ctx: SensorContext, days: float) -> tuple[list[dict[str, Any]], float | None]:
        claude, codex_home, mem_db = _paths(ctx.config)
        cutoff = time.time() - days * 86400
        entries = read_claude(claude, cutoff, ctx.cursor) + read_codex(codex_home, cutoff)
        mem_last = None
        try:
            mem, mem_last = read_mem(mem_db, cutoff)
            entries += mem
        except (sqlite3.Error, OSError) as e:
            ctx.log.warning("claude-mem unreadable: %s", e)
        _resolve_mem_projects(entries)
        return entries, mem_last

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        lookback = float(ctx.config.get("lookback_days", 7))
        entries, _ = self._gather(ctx, lookback)
        first_day = (local_today() - timedelta(days=int(ctx.config.get("signal_days", 2)) - 1)).isoformat()
        groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
        display: dict[str, str] = {}
        for e in entries:
            day = local_dt(e["ts"]).date().isoformat()
            if day < first_day:
                continue
            key = norm(e["project"])
            groups[(key, day)].append(e)
            display.setdefault(key, str(e["project"]))
        out: list[SignalDraft] = []
        for (key, day), es in sorted(groups.items()):
            es.sort(key=lambda e: -e["ts"])
            project = display[key]
            label = path_basename(project) or project
            prompts = [e for e in es if e["src"] == "claude"]
            codex = [e for e in es if e["src"] == "codex"]
            mem = [e for e in es if e["src"] == "mem"]
            n = len(prompts) + len(codex) + len(mem)
            parts = [f"{len(prompts)} 条 Claude 提示"] if prompts else []
            parts += [f"{len(codex)} 个 Codex 线程"] if codex else []
            parts += [f"{len(mem)} 条 claude-mem 记录"] if mem else []
            hm = lambda e: local_dt(e["ts"]).strftime("%H:%M")  # noqa: E731
            lines = [f"项目: {project}", f"日期: {day}"]
            if prompts:
                lines += ["", "Claude Code 提示（新→旧）:"] + [f"- {hm(e)} {e['text']}" for e in prompts[:MAX_PROMPTS]]
            if codex:
                lines += ["", "Codex 线程:"] + [f"- {hm(e)} {e['text']}" + (f" [{e['branch']}]" if e.get("branch") else "")
                                               for e in codex[:10]]
            if mem:
                lines += ["", "claude-mem:"]
                for e in mem[:10]:
                    lines.append(f"- {e['text']}" + (f" → 下一步: {e['next_steps']}" if e.get("next_steps") else ""))
            out.append(SignalDraft(
                title=f"AI 会话 · {label} · {day}: " + "、".join(parts),
                fingerprint=fingerprint("agent_history", key, day, _bucket(n)),
                body="\n".join(lines), url=Path(project).as_uri() if os.path.isabs(project) else None,
                external_id=f"{key}@{day}", occurred_at=utc_iso(es[0]["ts"]),
                payload={"project": project, "label": label, "day": day,
                         "counts": {"claude_prompts": len(prompts), "codex_threads": len(codex), "claude_mem": len(mem)},
                         "prompts": [{"time": hm(e), "text": e["text"], "session": e["session"]} for e in prompts[:MAX_PROMPTS]],
                         "codex": [{"time": hm(e), "title": e["text"], "id": e.get("id"), "branch": e.get("branch")}
                                   for e in codex[:10]],
                         "claude_mem": [{k: e.get(k) for k in ("kind", "text", "type", "next_steps") if e.get(k)}
                                        for e in mem[:10]]}))
        return out

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        cur = dict(ctx.cursor)  # don't move the real watermark from a snapshot
        entries, mem_last = self._gather(SensorContext(ctx.config, cur, ctx.log), 7)
        lines = []
        if entries:
            by: dict[str, list[dict[str, Any]]] = defaultdict(list)
            for e in entries:
                by[str(e["project"])].append(e)
            lines.append("### 最近 7 天的 AI 会话（按项目）")
            for project, es in sorted(by.items(), key=lambda kv: -max(e["ts"] for e in kv[1]))[:12]:
                es.sort(key=lambda e: -e["ts"])
                days = len({local_dt(e["ts"]).date() for e in es})
                nc = sum(e["src"] == "claude" for e in es)
                nx = sum(e["src"] == "codex" for e in es)
                recent = "；".join(trunc(e["text"], 60) for e in es[:3])
                lines.append(f"- **{path_basename(project) or project}**（{project}）：{nc} 条 Claude 提示、{nx} 个 Codex 线程，"
                             f"活跃 {days} 天，最近 {local_dt(es[0]['ts']).strftime('%m-%d %H:%M')}。最近在做：{recent}")
        if mem_last and mem_last < time.time() - 7 * 86400:
            lines.append(f"- claude-mem 记录停在 {local_dt(mem_last).strftime('%Y-%m-%d')}（之后未采集，仅作历史参考）")
        return "\n".join(lines) or None


SENSOR = AgentHistorySensor()
