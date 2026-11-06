"""Git actions the Outbox runs on an agent's behalf, and the whiteboard rule for when no human sign-off is needed.

Rule: agents may commit and push their own `agent/<topic>` branches without asking. Such commits are made in a separate
git worktree under var/worktrees, so the user's checkout, index and working tree are never switched or touched. Everything
else stays with the human: other branches, the default/protected branches, force pushes, history rewrites, merge/pull
requests (MR/PR), and every git action of a sensitive task (mail / notes / external issues, private categories, L1-L2).
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from pathlib import Path

from ..brain import policy
from ..config import load_config
from ..osutil import NO_WINDOW, bundled_or_path

# A plain branch name: no refspecs (`HEAD:main`), no `+force` prefix, no option-looking names, no `..` or `//`.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
# Credentials, caches and leftovers that must never ride along in an agent's commit.
NEVER_COMMIT = re.compile(
    r"(^|/)(\.env[^/]*|[^/]*\.(pem|key|pfx|p12|bak|orig|tmp|swp)|[^/]*\.awos-bak-[^/]*|id_rsa[^/]*|secrets?[^/]*|__pycache__|[^/]*\.pyc"
    r"|node_modules|\.venv|\.pytest_cache|\.ipynb_checkpoints|\.DS_Store|Thumbs\.db)(/|$)", re.I)


def gh_exe() -> Path | None:
    return bundled_or_path(load_config().root / "runtime" / "gh" / "bin" / "gh", "gh")


def git(repo_dir: Path, *args: str, timeout: int = 300, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *(extra or []), "-C", str(repo_dir), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, **NO_WINDOW)


def _credential_args() -> list[str]:
    gh = gh_exe()
    if gh is None:
        return []
    # per-invocation credential helper: no change to the user's global git config
    return ["-c", "credential.https://github.com.helper=", "-c", f'credential.https://github.com.helper=!"{gh.as_posix()}" auth git-credential']


def repo_dir_of(payload: dict) -> Path:
    repo_dir = Path(payload["repo"]).resolve()
    if not policy.writable_destination(repo_dir):
        raise RuntimeError(f"repo not in allowed roots: {repo_dir}")
    if not (repo_dir / ".git").exists():
        raise RuntimeError(f"not a git repository: {repo_dir}")
    return repo_dir


# `HEAD`, `refs/heads/main`, `heads/main`, ... all name an existing ref (often the default branch) rather than a new branch.
_REF_SPELLINGS = re.compile(r"^(HEAD|FETCH_HEAD|ORIG_HEAD|MERGE_HEAD|@)$|^(refs|heads|remotes|tags)/", re.I)


def valid_branch(name: str) -> bool:
    return (bool(_BRANCH_RE.match(name)) and ".." not in name and "//" not in name and "@{" not in name
            and not name.endswith(("/", ".", ".lock")) and not _REF_SPELLINGS.search(name))


def current_branch(repo_dir: Path) -> str:
    """Checked-out branch name; "" when HEAD is detached or the repo has no commits."""
    p = git(repo_dir, "symbolic-ref", "--quiet", "--short", "HEAD")
    return p.stdout.strip() if p.returncode == 0 else ""


def default_branch(repo_dir: Path, remote: str = "origin") -> str | None:
    p = git(repo_dir, "symbolic-ref", "--quiet", "--short", f"refs/remotes/{remote}/HEAD")
    return p.stdout.strip().split("/", 1)[-1] if p.returncode == 0 and p.stdout.strip() else None


def protected_branches(repo_dir: Path, remote: str = "origin") -> set[str]:
    """Lower-cased: on macOS/Windows `MAIN` and `main` are the same loose ref file."""
    names = {str(n).lower() for n in policy.git_policy()["protected_branches"]}
    for r in {"origin", remote}:
        default = default_branch(repo_dir, r)
        if default:
            names.add(default.lower())
    return names


def is_protected(repo_dir: Path, branch: str, remote: str = "origin") -> bool:
    return branch.lower() in protected_branches(repo_dir, remote)


def _effective_branch(repo_dir: Path, payload: dict) -> str:
    return (payload.get("branch") or "").strip() or current_branch(repo_dir)


def sensitivity(task: dict | None) -> str | None:
    """Why a task's git actions must always wait for the human (None = not sensitive)."""
    if not task:
        return None
    g = policy.git_policy()
    if int(task.get("level") or 0) < g["auto_min_level"]:
        return f"L{task.get('level')} 是只读层级的任务，不应自己提交"
    meta = task.get("meta") or {}
    if (meta.get("routing") or {}).get("trusted_only"):
        return "任务带有邮件或私人内容"
    if task.get("category") in (load_config().get("routing.private_categories") or ("comms", "career", "admin", "schedule")):
        return f"任务属于私人类别（{task.get('category')}）"
    blocked = set(g["no_auto_sources"])
    src = str(task.get("source") or "")
    if src.startswith("sensor:") and src[7:] in blocked:
        return f"任务来自 {src[7:]}（外部或私人内容）"
    from .. import db
    ids = list(meta.get("signals") or []) + [r for u in meta.get("updates") or [] for r in u.get("refs") or []]
    if task.get("source_signal_id"):
        ids.append(task["source_signal_id"])
    for sid in dict.fromkeys(ids):
        row = db.q1("SELECT source FROM signals WHERE id = ?", (sid,))
        if row and row["source"] in blocked:
            return f"任务来自 {row['source']}（外部或私人内容）"
    return None


def auto_allowed(atype: str, payload: dict, task: dict | None = None) -> tuple[bool, str]:
    """May this git_commit / git_push run without asking the human? Returns (allowed, reason it is not).
    Only plain commits and pushes of a non-protected branch qualify; force pushes do not exist in the payload at all."""
    if atype not in ("git_commit", "git_push") or not policy.git_policy()["auto_branch_actions"]:
        return False, ""
    try:
        why = sensitivity(task)
        if why:
            return False, why
        return _auto_allowed(atype, payload)
    except Exception as e:  # git missing / timing out / anything odd: fall back to asking, never crash the task
        return False, f"无法检查（{type(e).__name__}: {str(e)[:200]}）"


def _auto_allowed(atype: str, payload: dict) -> tuple[bool, str]:
    try:
        repo_dir = repo_dir_of(payload)
    except (KeyError, TypeError, RuntimeError) as e:
        return False, str(e)
    remote = str(payload.get("remote") or "origin")
    if atype == "git_push" and not _REMOTE_RE.match(remote):
        return False, f"远端名不合法：{remote}"
    branch = _effective_branch(repo_dir, payload)
    if not branch:
        return False, "没有指定分支，且仓库当前不在任何分支上（detached HEAD）"
    if not valid_branch(branch):
        return False, f"分支名不是普通分支名：{branch}"
    if is_protected(repo_dir, branch, remote):
        return False, f"目标是受保护分支 {branch}（直接动默认分支要你批准；给 branch 写一个新的功能分支名即可自动执行）"
    prefix = policy.git_policy()["auto_branch_prefix"]
    if not branch.startswith(prefix) or len(branch) <= len(prefix):
        return False, f"只有 {prefix}<主题> 分支可以自动提交/推送，{branch} 需要你批准"
    if atype == "git_commit":
        for p in payload.get("paths") or []:
            full = (repo_dir / str(p)).resolve()
            if full == repo_dir or full.is_dir():
                return False, f"路径是目录而不是文件：{p}（自动提交只接受逐个列出的文件）"
    return True, ""


_LIT = ["--literal-pathspecs"]  # `*`, `:/`, `:(glob)` ... in an agent's paths are file names, never patterns


def files_to_commit(repo_dir: Path, paths: list[str]) -> list[str]:
    """Every file `add -A -- paths` + `commit -- paths` would record: staged or unstaged changes to tracked files
    (deletions included) and untracked, non-ignored files. A listed directory expands to the files under it."""
    out: set[str] = set()
    for args in (("diff", "--cached", "--name-only", "-z"), ("diff", "--name-only", "-z"),
                 ("ls-files", "--others", "--exclude-standard", "-z")):
        p = git(repo_dir, *args, "--", *paths, extra=_LIT)
        if p.returncode != 0:
            raise RuntimeError(f"cannot list the files to commit: {p.stderr[-300:]}")
        out.update(f for f in p.stdout.split("\0") if f)
    return sorted(out)


def _check_commit_files(repo_dir: Path, paths: list[str]) -> list[str]:
    files = files_to_commit(repo_dir, paths)
    if not files:
        raise RuntimeError(f"nothing to commit for the listed paths: {paths[:10]}")
    bad = [f for f in files if NEVER_COMMIT.search(f)]
    if bad:
        raise RuntimeError(f"refusing to commit credential/cache/leftover-like files: {bad[:10]}")
    return files


def commit(payload: dict) -> dict:
    """Commit exactly the listed paths (never `add -A` of the tree) in a repo under the user's work roots; with `branch`
    the commit goes on that branch, created from the current HEAD when it does not exist yet. The files that would be
    committed are checked against NEVER_COMMIT before anything (branch switch, staging) happens."""
    repo_dir = repo_dir_of(payload)
    paths = [str(p) for p in (payload.get("paths") or [])]
    message = (payload.get("message") or "").strip()
    if not paths or not message:
        raise RuntimeError("git_commit needs an explicit non-empty 'paths' list and a 'message'")
    for p in paths:
        full = (repo_dir / p).resolve()
        if full != repo_dir and repo_dir not in full.parents:
            raise RuntimeError(f"path escapes the repository: {p}")
        if NEVER_COMMIT.search(p.replace("\\", "/")):
            raise RuntimeError(f"refusing to commit a credential/cache/leftover-like path: {p}")
    files = _check_commit_files(repo_dir, paths)
    branch = (payload.get("branch") or "").strip()
    if branch:
        if not valid_branch(branch):
            raise RuntimeError(f"not a plain branch name: {branch}")
        if branch != current_branch(repo_dir):
            return _commit_in_worktree(repo_dir, branch, files, message)
    note = ""
    add = git(repo_dir, "add", "-A", "--", *paths, extra=_LIT)  # -A so deletions of the listed paths are staged too
    if add.returncode != 0:
        return {"done": False, "details": (add.stdout + add.stderr)[-1500:]}
    done = git(repo_dir, "commit", "-m", message, "--", *paths, extra=_LIT)
    sha = git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"done": done.returncode == 0, "details": (note + done.stdout + done.stderr)[-1500:], "commit": sha,
            "branch": current_branch(repo_dir)}


def worktree_path(repo_dir: Path, branch: str) -> Path:
    tag = hashlib.sha1(str(repo_dir).encode("utf-8")).hexdigest()[:8]
    return load_config().path("var") / "worktrees" / f"{repo_dir.name}-{tag}" / branch.replace("/", "__")


def _commit_in_worktree(repo_dir: Path, branch: str, files: list[str], message: str) -> dict:
    """Commit `files` (as they are in the user's working tree) onto `branch` in a throw-away worktree: the user's
    checkout, index and working tree stay exactly as they were. The branch is created from HEAD if it does not exist."""
    wt = worktree_path(repo_dir, branch)
    git(repo_dir, "worktree", "prune")
    if wt.exists():
        git(repo_dir, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
    wt.parent.mkdir(parents=True, exist_ok=True)
    exists = git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
    add = (git(repo_dir, "worktree", "add", "--quiet", str(wt), branch) if exists
           else git(repo_dir, "worktree", "add", "--quiet", "-b", branch, str(wt), "HEAD"))
    if add.returncode != 0:
        return {"done": False, "details": f"无法为分支 {branch} 建立独立工作区：{(add.stdout + add.stderr)[-600:]}"}
    try:
        for f in files:
            src, dst = repo_dir / f, wt / f
            if src.is_symlink() or src.exists():
                dst.parent.mkdir(parents=True, exist_ok=True)
                if dst.is_symlink() or dst.is_file():
                    dst.unlink()
                if src.is_symlink():
                    os.symlink(os.readlink(src), dst)
                else:
                    shutil.copy2(src, dst)
            elif dst.is_symlink() or dst.exists():
                dst.unlink()
        st = git(wt, "add", "-A", "--", *files, extra=_LIT)
        if st.returncode != 0:
            return {"done": False, "details": (st.stdout + st.stderr)[-1500:]}
        if git(wt, "diff", "--cached", "--quiet", "--", *files, extra=_LIT).returncode == 0:
            sha = git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
            return {"done": True, "details": f"分支 {branch} 上这些文件已经是最新，没有新的提交", "commit": sha, "branch": branch}
        done = git(wt, "commit", "-m", message, "--", *files, extra=_LIT)
        sha = git(wt, "rev-parse", "--short", "HEAD").stdout.strip()
        note = f"（在独立工作区提交到分支 {branch}{'' if exists else '（新建自当前 HEAD）'}；你的工作区没有被切换或改动）\n"
        return {"done": done.returncode == 0, "details": (note + done.stdout + done.stderr)[-1500:], "commit": sha, "branch": branch}
    finally:
        git(repo_dir, "worktree", "remove", "--force", str(wt))
        shutil.rmtree(wt, ignore_errors=True)
        git(repo_dir, "worktree", "prune")


def push(payload: dict) -> dict:
    """Push one plain branch (never a refspec, never --force) and set its upstream."""
    repo_dir = repo_dir_of(payload)
    remote = str(payload.get("remote") or "origin")
    branch = _effective_branch(repo_dir, payload)
    if not _REMOTE_RE.match(remote):
        raise RuntimeError(f"not a plain remote name: {remote}")
    if not branch or not valid_branch(branch):
        raise RuntimeError(f"git_push needs a plain branch name, got {branch!r}")
    if git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode != 0:
        raise RuntimeError(f"no local branch {branch!r} to push")
    p = git(repo_dir, "push", "--set-upstream", remote, f"refs/heads/{branch}:refs/heads/{branch}", extra=_credential_args())
    return {"done": p.returncode == 0, "details": (p.stdout + p.stderr)[-1500:], "branch": branch}


def _run_gh(args: list[str], cwd: Path) -> subprocess.CompletedProcess:
    return subprocess.run(args, cwd=str(cwd), capture_output=True, text=True, encoding="utf-8", errors="replace",
                          timeout=120, **NO_WINDOW)


def open_mr(payload: dict) -> dict:
    """Open a pull/merge request on GitHub. Always human-approved first; merging is never done by an agent."""
    repo_dir = repo_dir_of(payload)
    gh = gh_exe()
    if gh is None:
        raise RuntimeError("gh is not installed (runtime/gh or PATH)")
    title, body = (payload.get("title") or "").strip(), (payload.get("body") or "").strip()
    branch = _effective_branch(repo_dir, payload)
    if not title or not branch or not valid_branch(branch):
        raise RuntimeError("open_mr needs a 'title' and a plain 'branch' name")
    base = (payload.get("base") or "").strip() or default_branch(repo_dir) or "main"
    if not valid_branch(base):
        raise RuntimeError(f"not a plain branch name: {base}")
    args = [str(gh), "pr", "create", "--head", branch, "--base", base, "--title", title, "--body", body or title]
    if payload.get("draft"):
        args.append("--draft")
    p = _run_gh(args, repo_dir)
    out = (p.stdout + p.stderr).strip()
    return {"done": p.returncode == 0, "details": out[-1500:], "url": out.splitlines()[-1] if p.returncode == 0 and out else None}
