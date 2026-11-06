"""Git actions the Outbox runs on an agent's behalf, and the whiteboard rule for when no human sign-off is needed.

Rule: agents may commit and push their own `agent/<topic>` branches without asking. Such commits are built with git
plumbing (private index, commit-tree, compare-and-swap update-ref), so the user's checkout, index and working tree are
never switched or touched and nothing is checked out anywhere. Everything
else stays with the human: other branches, the default/protected branches, force pushes, history rewrites, merge/pull
requests (MR/PR), and every git action of a sensitive task (mail / notes / external issues, private categories, L1-L2).
"""
from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import uuid
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
    dec = policy.decide(task)     # the effective level after category caps, and the sandbox the run really had
    if dec.level < g["auto_min_level"] or dec.sandbox == "read-only":
        return f"L{dec.level} 是只读层级的任务，不应自己提交"
    from .. import repo
    seen = {task["id"]} if task.get("id") else set()
    for up in (task.get("parent_id"), task.get("root_id")):
        while up and up not in seen and len(seen) < 12:   # a subtask inherits where its parents' content came from
            seen.add(up)
            parent = repo.get_task(up)
            if not parent:
                break
            why = _own_sensitivity(parent, g)
            if why:
                return f"上级任务：{why}"
            up = parent.get("parent_id")
    return _own_sensitivity(task, g)


def _own_sensitivity(task: dict, g: dict) -> str | None:
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
    if atype == "git_commit" and repo_hooks(repo_dir) and not hooks_sandboxable():
        return False, f"仓库配置了 git hooks（{', '.join(repo_hooks(repo_dir))}），这台机器上没法在沙箱里运行它们：要你批准后再提交"
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
    for args in (("diff", "--cached", "--no-renames", "--name-only", "-z"), ("diff", "--no-renames", "--name-only", "-z"),
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


HOOKS = ("pre-commit", "prepare-commit-msg", "commit-msg", "post-commit")
# For git commands AgentWorkOS runs itself (unsandboxed) inside a clone holding agent-written content.
NO_REPO_CODE = ["-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]
DEP_DIRS = ("node_modules", ".venv", "venv")   # untracked dependency dirs hooks usually need (linked in, read-only)


def repo_hooks(repo_dir: Path) -> list[str]:
    """The commit hooks this repo would run (core.hooksPath honoured)."""
    hp = git(repo_dir, "config", "--get", "core.hooksPath").stdout.strip()
    if hp:
        d = Path(os.path.expanduser(hp))
        d = d if d.is_absolute() else repo_dir / d
    else:
        d = repo_dir / git(repo_dir, "rev-parse", "--git-path", "hooks").stdout.strip()
    return [h for h in HOOKS if (d / h).is_file() and os.access(d / h, os.X_OK)]


def hooks_sandboxable() -> bool:
    from ..executors import sandbox
    return sandbox.enabled()


def commit(payload: dict, *, human_approved: bool = False) -> dict:
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
            hooks = repo_hooks(repo_dir)
            if hooks:
                if not hooks_sandboxable() and not human_approved:
                    raise RuntimeError(f"repo has git hooks ({', '.join(hooks)}) that cannot run sandboxed here: needs approval")
                return _commit_with_hooks(repo_dir, branch, files, message, sandboxed=hooks_sandboxable())
            return _commit_to_branch(repo_dir, branch, files, message)
    note = ""
    add = git(repo_dir, "add", "-A", "--", *paths, extra=_LIT)  # -A so deletions of the listed paths are staged too
    if add.returncode != 0:
        return {"done": False, "details": (add.stdout + add.stderr)[-1500:]}
    done = git(repo_dir, "commit", "-m", message, "--", *paths, extra=_LIT)
    sha = git(repo_dir, "rev-parse", "--short", "HEAD").stdout.strip()
    return {"done": done.returncode == 0, "details": (note + done.stdout + done.stderr)[-1500:], "commit": sha,
            "branch": current_branch(repo_dir)}


def _exec_mode(repo_dir: Path, base: str, rel: str, st: os.stat_result) -> str:
    """100755/100644 from the file's exec bit, except where it means nothing (Windows, core.fileMode=false): then
    keep the mode the path already has on the branch."""
    trust = os.name != "nt" and git(repo_dir, "config", "--type=bool", "core.fileMode").stdout.strip() != "false"
    if trust:
        return "100755" if st.st_mode & 0o111 else "100644"
    cur = git(repo_dir, "ls-tree", base, "--", rel, extra=_LIT).stdout.split(" ", 1)[0]
    return cur if cur in ("100644", "100755") else "100644"


def _blob(repo_dir: Path, rel: str, base: str) -> tuple[str, str] | None:
    """(mode, sha) of a working-tree entry written into the object store, with the clean filters `git add` would apply
    for that path (CRLF, LFS, ...); None if it is gone. Never follows a symlink: a link is stored as a link."""
    path = repo_dir / rel
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(st.st_mode):
        p = subprocess.run(["git", "-C", str(repo_dir), "hash-object", "-w", "--stdin"], input=os.fsencode(os.readlink(path)),
                           capture_output=True, timeout=60, **NO_WINDOW)
        mode = "120000"
    elif stat.S_ISREG(st.st_mode):
        p = subprocess.run(["git", "-C", str(repo_dir), "hash-object", "-w", "--path", rel, "--", str(path)],
                           capture_output=True, timeout=300, **NO_WINDOW)
        mode = _exec_mode(repo_dir, base, rel, st)
    else:
        raise RuntimeError(f"not a regular file or symlink: {path}")
    if p.returncode != 0:
        raise RuntimeError(f"git hash-object failed for {path}: {p.stderr.decode('utf-8', 'replace')[-300:]}")
    return mode, p.stdout.decode().strip()


def _commit_to_branch(repo_dir: Path, branch: str, files: list[str], message: str) -> dict:
    """Commit `files` (as they are in the user's working tree) onto `branch` without checking anything out: a private
    index built from the branch tip (or HEAD for a new branch), the files' blobs, `commit-tree`, then a compare-and-swap
    `update-ref`. Nothing in the user's checkout, index or working tree changes, nothing is written anywhere through the
    branch's tree (no symlink can redirect a write), no hooks run, and a concurrent commit to the same branch fails cleanly
    instead of being lost."""
    ref = f"refs/heads/{branch}"
    tip = git(repo_dir, "rev-parse", "--verify", "--quiet", ref).stdout.strip()
    base = tip or git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip()
    if not base:
        return {"done": False, "details": "仓库还没有任何提交，无法从 HEAD 新建分支"}
    tmp_dir = load_config().path("var") / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    index = tmp_dir / f"git-index-{uuid.uuid4().hex}"
    env = {**os.environ, "GIT_INDEX_FILE": str(index)}
    run = lambda *a, **k: subprocess.run(["git", "-C", str(repo_dir), *a], capture_output=True, text=True,  # noqa: E731
                                        encoding="utf-8", errors="replace", timeout=300, env=env, **NO_WINDOW, **k)
    try:
        r = run("read-tree", base)
        if r.returncode != 0:
            return {"done": False, "details": r.stderr[-600:]}
        for f in files:
            entry = _blob(repo_dir, f, base)
            r = (run("update-index", "--add", "--cacheinfo", f"{entry[0]},{entry[1]},{f}") if entry
                 else run("update-index", "--force-remove", "--", f))
            if r.returncode != 0:
                return {"done": False, "details": r.stderr[-600:]}
        tree = run("write-tree").stdout.strip()
        if tree == run("rev-parse", f"{base}^{{tree}}").stdout.strip():
            return {"done": True, "details": f"分支 {branch} 上这些文件已经是最新，没有新的提交", "commit": base[:12], "branch": branch}
        c = run("commit-tree", tree, "-p", base, input=message)
        if c.returncode != 0:
            return {"done": False, "details": c.stderr[-600:]}
        sha = c.stdout.strip()
        u = run("update-ref", "-m", f"awos: {message.splitlines()[0][:80]}", ref, sha, tip or "")   # "" = must not exist yet
        if u.returncode != 0:
            return {"done": False, "details": f"分支 {branch} 在提交期间被改动，未写入：{u.stderr[-400:]}"}
        note = (f"（直接提交到分支 {branch}{'' if tip else '（新建自当前 HEAD）'}，没有检出任何东西；"
                "你的分支、暂存区和工作区都没有被改动）\n")
        return {"done": True, "details": note + f"{sha[:12]} {message.splitlines()[0][:200]}", "commit": sha[:12], "branch": branch}
    finally:
        index.unlink(missing_ok=True)
        Path(str(index) + ".lock").unlink(missing_ok=True)


def _copy_into(src_root: Path, dst_root: Path, files: list[str]) -> None:
    """Copy the listed working-tree entries into a fresh checkout without ever following a symlink of that checkout
    (a branch can turn a directory into a link pointing anywhere)."""
    real_root = os.path.realpath(dst_root)
    for f in files:
        src, dst = src_root / f, dst_root / f
        parent = dst.parent
        parent.mkdir(parents=True, exist_ok=True)
        if not (os.path.realpath(parent) + os.sep).startswith(real_root + os.sep):
            raise RuntimeError(f"refusing to write through a symlink on the branch: {f}")
        if dst.is_symlink() or dst.is_file():
            dst.unlink()
        elif dst.exists():
            raise RuntimeError(f"a directory is in the way on the branch: {f}")
        if src.is_symlink():
            os.symlink(os.readlink(src), dst)
        elif src.is_file():
            shutil.copy2(src, dst)


def _commit_with_hooks(repo_dir: Path, branch: str, files: list[str], message: str, *, sandboxed: bool) -> dict:
    """A normal `git commit` (so the repo's hooks run and may fix files) in a throw-away `clone --shared` of the repo, inside
    the agent sandbox when `sandboxed`: only the clone is writable, network only through an egress proxy (package
    registries etc.), no credentials. The resulting commit is fetched back and written to `branch` with a CAS update-ref."""
    from ..executors import egress, proc, sandbox
    ref = f"refs/heads/{branch}"
    tip = git(repo_dir, "rev-parse", "--verify", "--quiet", ref).stdout.strip()
    base = tip or git(repo_dir, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip()
    if not base:
        return {"done": False, "details": "仓库还没有任何提交，无法从 HEAD 新建分支"}
    run_dir = load_config().path("var") / "tmp" / f"hooks-{uuid.uuid4().hex[:12]}"
    work = run_dir / repo_dir.name
    run_dir.mkdir(parents=True)
    try:
        c = git(run_dir, "clone", "--quiet", "--shared", "--no-checkout", "--", str(repo_dir), str(work), extra=NO_REPO_CODE)
        if c.returncode != 0:
            return {"done": False, "details": f"无法建立临时副本：{c.stderr[-400:]}"}
        for key in ("user.name", "user.email", "core.hooksPath", "commit.gpgsign"):
            v = git(repo_dir, "config", "--get", key).stdout.strip()
            if v:
                git(work, "config", key, v)
        if not git(repo_dir, "config", "--get", "core.hooksPath").stdout.strip():
            src_hooks = repo_dir / git(repo_dir, "rev-parse", "--git-path", "hooks").stdout.strip()
            for h in HOOKS:
                if (src_hooks / h).is_file():
                    shutil.copy2(src_hooks / h, work / ".git" / "hooks" / h)
        # trusted side, outside the sandbox: never run anything the branch brings (post-checkout hooks, fsmonitor)
        co = subprocess.run(["git", *NO_REPO_CODE, "-C", str(work), "checkout", "--quiet", "-B", branch, base], capture_output=True,
                            text=True, timeout=600, env={**os.environ, "GIT_LFS_SKIP_SMUDGE": "1"}, **NO_WINDOW)
        if co.returncode != 0:
            return {"done": False, "details": f"无法检出 {branch}：{co.stderr[-400:]}"}
        _copy_into(repo_dir, work, files)
        for d in DEP_DIRS:   # the user's installed dependencies, readable but not writable inside the sandbox
            if (repo_dir / d).is_dir() and not (work / d).exists():
                os.symlink(repo_dir / d, work / d)
        logs = run_dir / "logs"
        logs.mkdir()
        spec = type("S", (), {"allow_network": True, "net_allow": []})()
        steps = [["git", "add", "-A", "--", *files], ["git", "commit", "--quiet", "-m", message]]
        out: list[str] = []
        with egress.EgressProxy(sandbox.allowed_domains(spec)) as px:
            for argv in steps:
                if sandboxed:
                    argv, env = sandbox.wrap(argv, log_dir=logs, writable=[work], readable=[work], proxy=px)
                else:
                    env = {**sandbox.cache_env(logs / "tmp")}
                env["PRE_COMMIT_HOME"] = str(logs / "tmp" / "cache" / "pre-commit")
                pr = proc.run_process(argv, cwd=work, stdin_text=None, timeout_s=900, env=env, proxy=not sandboxed,
                                      on_stdout=out.append, on_stderr=out.append)
                if pr.exit_code != 0:
                    tail = "\n".join(out)[-1200:] or pr.stderr_tail[-1200:]
                    return {"done": False, "details": f"git hooks 没通过（{' '.join(argv[-4:])[:80]}），未提交：\n{tail}"}
        sha = git(work, "rev-parse", "HEAD", extra=NO_REPO_CODE).stdout.strip()
        if sha == base:
            return {"done": True, "details": f"分支 {branch} 上这些文件已经是最新，没有新的提交", "commit": base[:12], "branch": branch}
        f = git(repo_dir, "fetch", "--quiet", "--no-tags", "--", str(work), sha)
        if f.returncode != 0:
            return {"done": False, "details": f"无法取回提交：{f.stderr[-400:]}"}
        u = git(repo_dir, "update-ref", "-m", f"awos: {message.splitlines()[0][:80]}", ref, sha, tip or "")
        if u.returncode != 0:
            return {"done": False, "details": f"分支 {branch} 在提交期间被改动，未写入：{u.stderr[-400:]}"}
        mode = "在沙箱里" if sandboxed else "经你批准后"
        return {"done": True, "commit": sha[:12], "branch": branch,
                "details": f"（{mode}运行了仓库的 git hooks 后提交到分支 {branch}；你的分支、暂存区和工作区都没有被改动）\n"
                           + "\n".join(out)[-800:]}
    finally:
        shutil.rmtree(run_dir, ignore_errors=True)


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
