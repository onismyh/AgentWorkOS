"""Git sensor: local repo hygiene — stale uncommitted work, unpushed commits, active repos without a remote.

Strictly local: never fetches; `behind` is as of the last fetch the user did. Remote URLs are never read
(they may embed tokens) — only remote names are counted.
"""
from __future__ import annotations

import os
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from ..repo import fingerprint
from ._common import SKIP_DIRS, is_under, iso_week, local_str, norm, self_excluded, trunc, utc_iso
from .base import Sensor, SensorContext, SignalDraft

_ENV = {**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)
REDISCOVER_S = 24 * 3600


def _git(repo: str, *args: str, timeout: float = 20) -> tuple[int, str]:
    try:
        p = subprocess.run(["git", "-C", repo, "-c", "core.quotepath=off", *args], capture_output=True,
                           timeout=timeout, env=_ENV, creationflags=_NO_WINDOW)
    except (subprocess.TimeoutExpired, OSError) as e:
        return -1, str(e)
    return p.returncode, p.stdout.decode("utf-8", "replace")


def _valid_git(p: str) -> bool:
    if os.path.isdir(p):
        return os.path.isfile(os.path.join(p, "HEAD"))      # empty/stub .git dirs are skipped
    try:
        with open(p, encoding="utf-8", errors="replace") as f:  # worktree / submodule pointer file
            return f.read(8) == "gitdir: "
    except OSError:
        return False


def discover(roots: list[str], max_depth: int = 4, exclude: list[str] | None = None) -> list[str]:
    found: list[str] = []
    skip_paths = [norm(x) for x in exclude or []]

    def walk(d: str, depth: int) -> None:
        try:
            entries = list(os.scandir(d))
        except OSError:
            return
        if any(e.name == ".git" for e in entries) and _valid_git(os.path.join(d, ".git")):
            found.append(os.path.normpath(d))
        if depth >= max_depth:
            return
        for e in entries:
            try:
                if not e.is_dir(follow_symlinks=False) or e.is_junction():
                    continue
            except OSError:
                continue
            if e.name in SKIP_DIRS or e.name.startswith(".") or self_excluded(e.path):
                continue
            if any(is_under(e.path, s) for s in skip_paths):
                continue
            walk(e.path, depth + 1)

    for r in roots:
        if os.path.isdir(r) and not self_excluded(r):
            walk(os.path.normpath(r), 0)
    return sorted(set(found))


def repo_stats(repo: str) -> dict[str, Any]:
    """One repo's state from local git metadata only (4 cheap git calls)."""
    st: dict[str, Any] = {"repo": repo, "name": "/".join(Path(repo).parts[-2:]), "error": None}
    rc, out = _git(repo, "status", "--porcelain=v2", "--branch", "-z", "--no-renames")
    if rc != 0:
        st["error"] = trunc(out, 200) or f"git status exit {rc}"
        return st
    head = branch = upstream = None
    ahead = behind = None
    tracked: list[str] = []
    untracked = 0
    for rec in out.split("\0"):
        if rec.startswith("# branch.oid "):
            head = rec[13:] if rec[13:] != "(initial)" else None
        elif rec.startswith("# branch.head "):
            branch = rec[14:]
        elif rec.startswith("# branch.upstream "):
            upstream = rec[18:]
        elif rec.startswith("# branch.ab "):
            a, b = rec[12:].split()
            ahead, behind = int(a), -int(b)
        elif rec.startswith("1 "):
            tracked.append(rec.split(" ", 8)[-1])
        elif rec.startswith("u "):
            tracked.append(rec.split(" ", 10)[-1])
        elif rec.startswith("? "):
            untracked += 1
    mtimes = []
    for rel in tracked[:2000]:
        try:
            mtimes.append(os.stat(os.path.join(repo, rel)).st_mtime)
        except OSError:
            pass  # deleted file
    st.update(head=head, branch=branch, upstream=upstream, behind=behind, dirty=len(tracked), untracked=untracked,
              dirty_sample=tracked[:10], oldest_dirty=min(mtimes) if mtimes else None,
              newest_dirty=max(mtimes) if mtimes else None)

    rc, out = _git(repo, "log", "-1", "--format=%H%x1f%ct%x1f%s")
    if rc == 0 and out.strip():
        sha, ct, subj = (out.strip().split("\x1f") + ["", ""])[:3]
        st["last_commit"] = {"sha": sha[:12], "ts": int(ct), "date": local_str(int(ct)), "subject": trunc(subj, 120)}
    else:
        st["last_commit"] = None
    rc, out = _git(repo, "remote")
    st["remotes"] = len([x for x in out.split() if x]) if rc == 0 else 0

    unpushed: list[int] = []
    if head and upstream:
        rc, out = _git(repo, "log", "--format=%ct", "-n", "500", "@{u}..HEAD")
        unpushed = [int(x) for x in out.split() if x.isdigit()] if rc == 0 else []
        ahead = ahead if ahead is not None else len(unpushed)
    elif head and st["remotes"]:  # branch without upstream: commits not on any remote-tracking ref
        rc, out = _git(repo, "log", "--format=%ct", "-n", "500", "HEAD", "--not", "--remotes")
        unpushed = [int(x) for x in out.split() if x.isdigit()] if rc == 0 else []
        ahead = len(unpushed)
    st["ahead"] = ahead
    st["oldest_unpushed"] = min(unpushed) if unpushed else None

    git_mtime = None
    if not head:  # no commits yet: the index mtime is the only activity hint (git status refreshes it otherwise)
        try:
            git_mtime = os.stat(os.path.join(repo, ".git", "index")).st_mtime
        except OSError:
            pass
    acts = [x for x in (st["newest_dirty"], (st["last_commit"] or {}).get("ts"), git_mtime) if x]
    st["last_activity"] = max(acts) if acts else None
    return st


def _age(seconds: float) -> str:
    h = seconds / 3600
    return f"{h:.0f} 小时" if h < 48 else f"{h / 24:.0f} 天"


class GitSensor(Sensor):
    name = "git"

    def _repos(self, ctx: SensorContext) -> list[str]:
        cfg, cur = ctx.config, ctx.cursor
        roots = [str(r) for r in cfg.get("roots", [])]
        key = fingerprint(roots, cfg.get("max_depth", 4), cfg.get("exclude", []))
        if cur.get("roots_key") != key or time.time() - float(cur.get("discovered_at") or 0) > REDISCOVER_S \
                or not cur.get("repos"):
            cur["repos"] = discover(roots, int(cfg.get("max_depth", 4)), list(cfg.get("exclude", [])))
            cur["discovered_at"] = time.time()
            cur["roots_key"] = key
            ctx.log.info("git: discovered %d repos", len(cur["repos"]))
        return [r for r in cur["repos"] if os.path.isdir(r)]

    def _stats(self, ctx: SensorContext) -> list[dict[str, Any]]:
        repos = self._repos(ctx)
        with ThreadPoolExecutor(max_workers=6) as pool:
            stats = list(pool.map(repo_stats, repos))
        ctx.cursor["summary"] = [_summary(s) for s in stats]
        ctx.cursor["summary_at"] = time.time()
        return stats

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        cfg = ctx.config
        stale_dirty = float(cfg.get("stale_dirty_hours", 48)) * 3600
        stale_unpushed = float(cfg.get("stale_unpushed_hours", 24)) * 3600
        recent = float(cfg.get("recent_days", 14)) * 86400
        now, week = time.time(), iso_week()
        out: list[SignalDraft] = []
        for s in self._stats(ctx):
            if s["error"]:
                ctx.log.debug("git: %s: %s", s["repo"], s["error"])
                continue
            repo, name, head = s["repo"], s["name"], s.get("head") or "nohead"
            url = Path(repo).as_uri()
            payload = _summary(s)
            if s["dirty"] and s["oldest_dirty"] and now - s["oldest_dirty"] >= stale_dirty:
                lines = [f"仓库: {repo}", f"分支: {s['branch']}", f"未提交的已跟踪文件: {s['dirty']}，未跟踪: {s['untracked']}",
                         f"最早改动: {local_str(s['oldest_dirty'])}（{_age(now - s['oldest_dirty'])}前）"]
                lines += [f"- {p}" for p in s["dirty_sample"]]
                out.append(SignalDraft(
                    title=f"git · {name}: {s['dirty']} 个文件未提交（最早已 {_age(now - s['oldest_dirty'])}）",
                    fingerprint=fingerprint("git", norm(repo), "dirty_stale", week, head),
                    body="\n".join(lines), url=url, external_id=repo, occurred_at=utc_iso(s["oldest_dirty"]),
                    payload={**payload, "condition": "dirty_stale"}))
            if s.get("ahead") and s.get("oldest_unpushed") and now - s["oldest_unpushed"] >= stale_unpushed:
                target = s.get("upstream") or "任何远程分支"
                out.append(SignalDraft(
                    title=f"git · {name}: {s['ahead']} 个提交未推送（{s['branch']}）",
                    fingerprint=fingerprint("git", norm(repo), "unpushed", week, head),
                    body=f"仓库: {repo}\n分支 {s['branch']} 比 {target} 超前 {s['ahead']} 个提交，"
                         f"最早的未推送提交在 {local_str(s['oldest_unpushed'])}。",
                    url=url, external_id=repo, occurred_at=utc_iso(s["oldest_unpushed"]),
                    payload={**payload, "condition": "unpushed"}))
            if not s["remotes"] and s.get("last_activity") and now - s["last_activity"] <= recent:
                lc = s.get("last_commit")
                out.append(SignalDraft(
                    title=f"git · {name}: 没有远程仓库但最近有改动（备份风险）",
                    fingerprint=fingerprint("git", norm(repo), "no_remote", week),
                    body=f"仓库: {repo}\n最近活动: {local_str(s['last_activity'])}\n"
                         f"最近提交: {lc['date'] + ' ' + lc['subject'] if lc else '（还没有提交）'}\n"
                         f"未提交: {s['dirty']}，未跟踪: {s['untracked']}",
                    url=url, external_id=repo, occurred_at=utc_iso(s["last_activity"]),
                    payload={**payload, "condition": "no_remote"}))
        return out

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        summ = ctx.cursor.get("summary")
        if not summ or time.time() - float(ctx.cursor.get("summary_at") or 0) > 6 * 3600:
            summ = [_summary(s) for s in self._stats(ctx)]
        cutoff = time.time() - 14 * 86400
        active = sorted((s for s in summ if (s.get("last_activity_ts") or 0) >= cutoff),
                        key=lambda s: -(s.get("last_activity_ts") or 0))
        if not active:
            return None
        rows = ["| 仓库 | 分支 | 未提交 | 未跟踪 | 超前/落后 | 最近提交 |", "|---|---|---|---|---|---|"]
        for s in active[:20]:
            lc = s.get("last_commit") or {}
            ab = f"{s.get('ahead') if s.get('ahead') is not None else '-'}/{s.get('behind') if s.get('behind') is not None else '-'}"
            if not s.get("remotes"):
                ab = "无远程"
            subj = trunc(lc.get("subject", ""), 50).replace("|", "/")
            rows.append(f"| {s['name']} | {s.get('branch') or '-'} | {s.get('dirty', 0)} | {s.get('untracked', 0)} | {ab} | "
                        f"{lc.get('date', '—')} {subj} |")
        return "### Git 仓库（近 14 天活跃）\n" + "\n".join(rows)


def _summary(s: dict[str, Any]) -> dict[str, Any]:
    keys = ("repo", "name", "branch", "upstream", "ahead", "behind", "dirty", "untracked", "remotes", "last_commit", "error")
    d = {k: s.get(k) for k in keys}
    d["head"] = (s.get("head") or "")[:12] or None
    d["oldest_dirty_at"] = local_str(s["oldest_dirty"]) if s.get("oldest_dirty") else None
    d["last_activity_ts"] = s.get("last_activity")
    return d


SENSOR = GitSensor()
