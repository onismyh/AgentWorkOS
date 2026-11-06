"""Git actions the Outbox runs on an agent's behalf, and the whiteboard rule for when no human sign-off is needed.

Rule: agents may commit and push to (local and remote) *branches* by default. Everything that reaches beyond a branch
stays with the human: the default/protected branches, force pushes, history rewrites, and merge/pull requests (MR/PR).
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

from ..brain import policy
from ..config import load_config

# A plain branch name: no refspecs (`HEAD:main`), no `+force` prefix, no option-looking names, no `..` or `//`.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,99}$")
_REMOTE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,49}$")
# Credentials, caches and leftovers that must never ride along in an agent's commit.
NEVER_COMMIT = re.compile(
    r"(^|/)(\.env[^/]*|[^/]*\.(pem|key|pfx|p12|bak|orig|tmp|swp)|[^/]*\.awos-bak-[^/]*|id_rsa[^/]*|secrets?[^/]*|__pycache__|[^/]*\.pyc"
    r"|node_modules|\.venv|\.pytest_cache|\.ipynb_checkpoints|\.DS_Store|Thumbs\.db)(/|$)", re.I)


def gh_exe() -> Path | None:
    p = load_config().root / "runtime" / "gh" / "bin" / "gh.exe"
    return p if p.exists() else None


def git(repo_dir: Path, *args: str, timeout: int = 300, extra: list[str] | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *(extra or []), "-C", str(repo_dir), *args], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout, creationflags=0x08000000)


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


def auto_allowed(atype: str, payload: dict) -> tuple[bool, str]:
    """May this git_commit / git_push run without asking the human? Returns (allowed, reason it is not).
    Only plain commits and pushes of a non-protected branch qualify; force pushes do not exist in the payload at all."""
    if atype not in ("git_commit", "git_push") or not policy.git_policy()["auto_branch_actions"]:
        return False, ""
    try:
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
    _check_commit_files(repo_dir, paths)
    note = ""
    branch = (payload.get("branch") or "").strip()
    if branch:
        if not valid_branch(branch):
            raise RuntimeError(f"not a plain branch name: {branch}")
        before = current_branch(repo_dir)
        if branch != before:
            exists = git(repo_dir, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}").returncode == 0
            sw = git(repo_dir, "switch", branch) if exists else git(repo_dir, "switch", "-c", branch)
            if sw.returncode != 0:
                return {"done": False, "details": f"无法切换到分支 {branch}：{(sw.stdout + sw.stderr)[-600:]}"}
            note = f"（已{'切换到' if exists else '新建并切换到'}分支 {branch}，原分支：{before or 'detached'}）\n"
            if exists:  # another branch's tree may differ: check again before staging
                _check_commit_files(repo_dir, paths)
    add = git(repo_dir, "add", "-A", "--", *paths, extra=_LIT)  # -A so deletions of the listed paths are staged too
    if add.returncode != 0:
        return {"done": False, "details": (add.stdout + add.stderr)[-1500:]}
    done = git(repo_dir, "commit", "-m", message, "--", *paths, extra=_LIT)
    sha = git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"done": done.returncode == 0, "details": (note + done.stdout + done.stderr)[-1500:], "commit": sha,
            "branch": current_branch(repo_dir)}


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
                          timeout=120, creationflags=0x08000000)


def open_mr(payload: dict) -> dict:
    """Open a pull/merge request on GitHub. Always human-approved first; merging is never done by an agent."""
    repo_dir = repo_dir_of(payload)
    gh = gh_exe()
    if gh is None:
        raise RuntimeError("gh is not installed under runtime/gh")
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
