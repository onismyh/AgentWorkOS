"""Least-privilege confinement for agent processes.

Every executor runs inside an OS sandbox on macOS, each with only what its job needs:

  claude  Claude Code's own sandbox for Bash (Seatbelt): writes only in the workspace, no network for shell commands, no
          unsandboxed escape hatch; Read/Edit deny rules + sandbox denyRead keep credential files out of reach.
  codex   Codex's own Seatbelt sandbox (`-s read-only|workspace-write`, network off).
  dsh     DSH's own Seatbelt sandbox (`--permission-mode`, fails closed), isolated DSH_HOME.
  pi      no sandbox of its own -> wrapped here in `sandbox-exec` with the profile below.

(macOS refuses nested Seatbelt sandboxes, so the agents that sandbox their own commands are not wrapped a second time.)
All executors also get a scrubbed environment (proc.child_env): credential-looking variables are dropped except the ones
the executor needs for its own login.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from ..config import load_config
from ..osutil import IS_MAC

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

# Credentials and keys an agent never needs to read (its own login file is exempted per executor).
SECRET_PATHS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.kube", "~/.docker/config.json", "~/.netrc", "~/.git-credentials",
                "~/.npmrc", "~/.pypirc", "~/.config/gh", "~/.config/gcloud", "~/.azure", "~/Library/Keychains",
                "~/Library/Cookies", "~/.codex/auth.json", "~/.claude/.credentials.json", "~/.dsh/.credentials.yaml",
                "~/.pi/agent/auth.json")


def secret_paths(exempt: tuple[str, ...] = ()) -> list[str]:
    """Absolute, symlink-resolved credential paths (+ AgentWorkOS's own secrets and `sandbox.deny_read` extras)."""
    cfg = load_config()
    raw = [*SECRET_PATHS, str(cfg.path("var") / "secrets.toml"), *(cfg.get("sandbox.deny_read") or [])]
    skip = {os.path.realpath(os.path.expanduser(p)) for p in exempt}
    out: list[str] = []
    for p in raw:
        real = os.path.realpath(os.path.expanduser(str(p)))
        if real not in skip and real not in out:
            out.append(real)
    return out


def enabled() -> bool:
    return IS_MAC and bool(load_config().get("sandbox.enabled", True))


def _q(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def seatbelt_profile(*, writable: list[Path], deny_read: list[str]) -> str:
    """Allow-by-default profile that takes away what an agent must not do: write anywhere but `writable` (+ /dev), read
    credential files. Seatbelt applies the last matching rule, so the specific allows/denies follow the broad ones."""
    w = " ".join(f"(subpath {_q(os.path.realpath(p))})" for p in writable)
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             f'(allow file-write* (subpath "/dev") {w})']
    if deny_read:
        rules = " ".join(f"(subpath {_q(p)})" if not os.path.isfile(p) else f"(literal {_q(p)})" for p in deny_read)
        lines += [f"(deny file-read* file-write* {rules})"]
    return "\n".join(lines) + "\n"


def wrap(argv: list[str], *, log_dir: Path, writable: list[Path], exempt: tuple[str, ...] = ()) -> tuple[list[str], dict[str, str]]:
    """argv run under sandbox-exec (macOS) with a per-run private TMPDIR; returns (argv, extra env). No-op elsewhere."""
    if not enabled():
        return argv, {}
    if not Path(SANDBOX_EXEC).exists():
        raise RuntimeError("sandbox-exec not found: refusing to run the agent unconfined")
    tmp = log_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    profile = log_dir / "sandbox.sb"
    profile.write_text(seatbelt_profile(writable=[*writable, tmp], deny_read=secret_paths(exempt)), encoding="utf-8")
    return [SANDBOX_EXEC, "-f", str(profile), *argv], {"TMPDIR": str(tmp) + os.sep}


def claude_settings(*, cwd: Path, read_only_dirs: list[Path]) -> dict:
    """`sandbox` block for Claude Code's --settings (macOS Seatbelt / Linux bubblewrap); {} where it cannot run."""
    if not load_config().get("sandbox.enabled", True):
        return {}
    if not (IS_MAC or (os.name == "posix" and shutil.which("bwrap"))):
        return {}
    keep_ro = [os.path.realpath(p) for p in read_only_dirs if os.path.realpath(p) != os.path.realpath(cwd)]
    return {"sandbox": {
        "enabled": True,
        "failIfUnavailable": True,            # never fall back to running shell commands unconfined
        "allowUnsandboxedCommands": False,    # no `dangerouslyDisableSandbox` escape hatch
        "autoAllowBashIfSandboxed": False,    # which commands may run is still decided by --allowedTools
        "filesystem": {"denyRead": secret_paths(("~/.claude/.credentials.json",)), "denyWrite": keep_ro},
        "network": {"allowedDomains": []},    # shell commands get no network (web access goes through WebFetch rules)
    }}
