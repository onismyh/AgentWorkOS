"""GitHub sensor via the `gh` CLI (runtime/gh, authenticated through `gh auth login`): review requests, assigned issues,
the user's own open PRs and unread participating notifications. Read-only; a missing/expired login is a soft failure
(the runner backs off) so the sensor can be enabled before the user has logged in."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from ..config import load_config
from ..repo import fingerprint
from ._common import parse_iso, redact, trunc, utc_iso
from .base import Sensor, SensorContext, SignalDraft

PR_FIELDS = "repository,title,url,number,updatedAt,author,isDraft"
ISSUE_FIELDS = "repository,title,url,number,updatedAt,author"


def gh_exe() -> Path | None:
    p = load_config().root / "runtime" / "gh" / "bin" / "gh.exe"
    return p if p.exists() else None


def run_gh(*args: str, timeout: int = 60) -> Any:
    exe = gh_exe()
    if exe is None:
        raise RuntimeError("gh not installed (runtime/gh)")
    env = {"GH_NO_UPDATE_NOTIFIER": "1", "GH_PROMPT_DISABLED": "1", "NO_COLOR": "1"}
    import os
    cp = subprocess.run([str(exe), *args], capture_output=True, timeout=timeout, env={**os.environ, **env},
                        creationflags=0x08000000)
    out, err = cp.stdout.decode("utf-8", "replace"), cp.stderr.decode("utf-8", "replace")
    if cp.returncode != 0:
        if "gh auth login" in err or "not logged" in err.lower() or "authentication" in err.lower():
            raise RuntimeError("github: not logged in (run: runtime\\gh\\bin\\gh.exe auth login)")
        raise RuntimeError(f"gh {' '.join(args[:2])} failed: {err.strip()[:300]}")
    return json.loads(out) if out.strip() else []


def logged_in() -> bool:
    exe = gh_exe()
    if exe is None:
        return False
    return subprocess.run([str(exe), "auth", "status"], capture_output=True, timeout=30, creationflags=0x08000000).returncode == 0


def _repo(r: Any) -> str:
    if isinstance(r, dict):
        return r.get("nameWithOwner") or r.get("name") or "?"
    return str(r or "?")


class GitHubSensor(Sensor):
    name = "github"

    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        if not logged_in():
            raise RuntimeError("github: not logged in (run: runtime\\gh\\bin\\gh.exe auth login)")
        out: list[SignalDraft] = []
        limit = str(int(ctx.config.get("max_items", 20)))

        for pr in run_gh("search", "prs", "--review-requested=@me", "--state=open", "--json", PR_FIELDS, "--limit", limit):
            out.append(self._item("review", "待你评审的 PR", pr))
        for it in run_gh("search", "issues", "--assignee=@me", "--state=open", "--json", ISSUE_FIELDS, "--limit", limit):
            out.append(self._item("issue", "指派给你的 issue", it))
        for pr in run_gh("search", "prs", "--author=@me", "--state=open", "--json", PR_FIELDS, "--limit", limit):
            out.append(self._item("mine", "你的未合并 PR", pr))
        for n in run_gh("api", "notifications?participating=true&per_page=30"):
            subj = n.get("subject") or {}
            repo = (n.get("repository") or {}).get("full_name", "?")
            ts = parse_iso(n.get("updated_at"))
            out.append(SignalDraft(
                title=f"GitHub 通知 · {repo}: {trunc(subj.get('title'), 100)}（{n.get('reason')}）",
                fingerprint=fingerprint("github", "notif", n.get("id"), n.get("updated_at")),
                body=f"类型: {subj.get('type')}\n原因: {n.get('reason')}\n仓库: {repo}", url=(subj.get("url") or "").replace("api.github.com/repos", "github.com"),
                external_id=str(n.get("id")), occurred_at=utc_iso(ts) if ts else None,
                payload={"repo": repo, "reason": n.get("reason"), "type": subj.get("type"), "title": subj.get("title")}))
        return out

    @staticmethod
    def _item(kind: str, label: str, it: dict) -> SignalDraft:
        repo, num = _repo(it.get("repository")), it.get("number")
        ts = parse_iso(it.get("updatedAt"))
        author = (it.get("author") or {}).get("login", "?")
        return SignalDraft(
            title=f"GitHub · {label} · {repo}#{num}: {trunc(redact(it.get('title')), 110)}",
            fingerprint=fingerprint("github", kind, repo, num, it.get("updatedAt")),
            body=f"{label}\n仓库: {repo}\n作者: {author}\n更新: {it.get('updatedAt')}" + ("\n草稿 PR" if it.get("isDraft") else ""),
            url=it.get("url"), external_id=f"{repo}#{num}", occurred_at=utc_iso(ts) if ts else None,
            payload={"kind": kind, "repo": repo, "number": num, "title": it.get("title"), "author": author})

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        if not logged_in():
            return None
        repos = run_gh("repo", "list", "--limit", "15", "--json", "nameWithOwner,pushedAt,isPrivate,description")
        lines = ["| 仓库 | 最近推送 | 可见性 |", "|---|---|---|"]
        for r in repos[:12]:
            lines.append(f"| {r['nameWithOwner']} | {(r.get('pushedAt') or '')[:10]} | {'私有' if r.get('isPrivate') else '公开'} |")
        return "\n".join(lines)


SENSOR = GitHubSensor()
