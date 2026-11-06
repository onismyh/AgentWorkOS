"""Least-privilege confinement for agent processes.

Every agent run gets only what its task needs. macOS refuses nested Seatbelt sandboxes, so an agent whose own sandbox can
be configured tightly enough uses that one, and the others run under AgentWorkOS's `sandbox-exec` profile:

  claude  Claude Code's sandbox for its shell commands, switched on: writes only in the workspace, no network, no
          unsandboxed escape hatch; credential files, AgentWorkOS's data (var/) and other agents' histories unreadable
          except the run's own workspace and readable roots; Read/Edit deny rules for the most sensitive files.
  codex   a Codex permission profile for its commands (Seatbelt): same read limits, writes only in the workspace (none
          for read-only runs) and a private TMPDIR, no network.
  dsh     the whole DSH process under the profile below (DSH's own Seatbelt profile only limits writes and allows the temp
          areas; its `runnerCommand` hook is pointed at a pass-through runner instead).
  pi      no sandbox of its own: the whole process under the profile below.

The `sandbox-exec` profile (Pi, DSH): allow by default, then take away writes outside the workspace / the agent's own
state, reads of credentials, of AgentWorkOS's data and of other agents' histories, any connection to localhost (the
dashboard API hands its token to whoever loads the page; no proxy is passed to these agents, their model APIs are reached
directly), unix sockets other than DNS (ssh-agent, docker), AppleEvents and LaunchServices (`osascript` / `open` would
start programs outside the sandbox).

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

# Credentials and keys an agent never needs to read.
SECRET_PATHS = ("~/.ssh", "~/.aws", "~/.gnupg", "~/.kube", "~/.docker/config.json", "~/.netrc", "~/.git-credentials",
                "~/.npmrc", "~/.pypirc", "~/.config/gh", "~/.config/gcloud", "~/.azure", "~/Library/Keychains",
                "~/Library/Cookies", "~/.codex/auth.json", "~/.claude/.credentials.json", "~/.dsh/.credentials.yaml",
                "~/.pi/agent/auth.json")
# Other agents' histories and the user's mail/messages: private data no agent's shell needs.
PRIVATE_DATA = ("~/.codex", "~/.claude", "~/.claude.json", "~/.claude-mem", "~/.dsh", "~/.pi", "~/Library/Mail",
                "~/Library/Messages")
LAUNCH_SERVICES = ("com.apple.coreservices.launchservicesd", "com.apple.lsd.mapdb", "com.apple.coreservices.quarantine-resolver")


def _real(p: str | Path) -> str:
    return os.path.realpath(os.path.expanduser(str(p)))


def _uniq(paths) -> list[str]:
    out: list[str] = []
    for p in paths:
        r = _real(p)
        if r not in out:
            out.append(r)
    return out


def secret_paths() -> list[str]:
    """Absolute, symlink-resolved credential paths (+ AgentWorkOS's own secrets and `sandbox.deny_read` extras)."""
    cfg = load_config()
    return _uniq([*SECRET_PATHS, cfg.path("var") / "secrets.toml", *(cfg.get("sandbox.deny_read") or [])])


def private_paths() -> list[str]:
    """AgentWorkOS's data (every task's workspace and mail, the DB, run logs, the API token) + other agents' histories."""
    return _uniq([load_config().path("var"), *PRIVATE_DATA])


def enabled() -> bool:
    return IS_MAC and bool(load_config().get("sandbox.enabled", True))


def _q(path: str) -> str:
    return '"' + path.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _filters(paths: list[str], files: list[str] = ()) -> str:
    return " ".join([*(f"(subpath {_q(p)})" for p in paths), *(f"(literal {_q(p)})" for p in files)])


MDNS_SOCKET = "/private/var/run/mDNSResponder"   # DNS lookups go through this socket; every other unix socket is denied


def _inside_private(paths: list[str]) -> list[str]:
    """Only paths inside a private area need re-allowing; a parent of one (e.g. ~/Desktop holding var/) must not unlock it."""
    priv = private_paths()
    return [p for p in paths if any(p == q or p.startswith(q + os.sep) for q in priv)]


def seatbelt_profile(*, writable: list[Path], readable: list[Path], writable_files: list[Path] = (),
                     readonly_within: list[Path] = (), own_files: list[Path] = ()) -> str:
    """The `sandbox-exec` profile for agents without a usable sandbox of their own. Seatbelt applies the last matching
    rule, so each narrower allow/deny comes after the broader one it refines:
      writes: nowhere, except /dev and `writable` dirs minus `readonly_within` (an agent's own code / config inside its
              state dir, so a run cannot plant something the next run would load), plus single `writable_files`
      reads:  not AgentWorkOS's data / other agents' histories, except `readable` (+ the writable ones)
              never credentials, except `own_files` (the agent's own login, read/write for token refresh)
      no localhost and no unix sockets (ssh-agent, docker, ...) except DNS, no AppleEvents, no LaunchServices."""
    w_dirs = _uniq(writable)
    w_files = _uniq(writable_files)
    lines = ["(version 1)", "(allow default)", "(deny file-write*)",
             f'(allow file-write* (subpath "/dev") {_filters(w_dirs)})']
    ro = _uniq(readonly_within)
    if ro:
        lines.append(f"(deny file-write* {_filters(ro)})")
    if w_files:                                   # single files, may sit inside a read-only area
        lines.append(f"(allow file-write* {_filters([], w_files)})")
    lines.append(f"(deny file-read* {_filters(private_paths())})")
    back = _inside_private(_uniq([*readable, *writable]))
    if back or w_files:
        lines.append(f"(allow file-read* {_filters(back, w_files)})")
    lines.append(f"(deny file-read* file-write* {_filters(secret_paths())})")
    own = _uniq(own_files)
    if own:
        lines.append(f"(allow file-read* file-write* {_filters([], own)})")
        # resolving a linked login (realpath) stats each parent directory: metadata only, never their contents
        home = _real("~")
        parents = _uniq(q for p in own for q in Path(p).parents if str(q).startswith(home + os.sep))
        if parents:
            lines.append(f"(allow file-read-metadata {_filters([], parents)})")
    lines.append('(deny network-outbound (remote ip "localhost:*"))')
    lines.append("(deny network-outbound (remote unix-socket))")
    lines.append(f'(allow network-outbound (remote unix-socket (path-literal "{MDNS_SOCKET}")))')
    lines.append("(deny appleevent-send)")
    lines.append("(deny mach-lookup " + " ".join(f'(global-name "{n}")' for n in LAUNCH_SERVICES) + ")")
    return "\n".join(lines) + "\n"


def wrap(argv: list[str], *, log_dir: Path, writable: list[Path], readable: list[Path] = (), writable_files: list[Path] = (),
         readonly_within: list[Path] = (), own_files: list[Path] = ()) -> tuple[list[str], dict[str, str]]:
    """argv run under sandbox-exec (macOS) with a per-run private TMPDIR; returns (argv, extra env). No-op elsewhere."""
    if not enabled():
        return argv, {}
    if not Path(SANDBOX_EXEC).exists():
        raise RuntimeError("sandbox-exec not found: refusing to run the agent unconfined")
    tmp = log_dir / "tmp"
    tmp.mkdir(parents=True, exist_ok=True)
    profile = log_dir / "sandbox.sb"
    profile.write_text(seatbelt_profile(writable=[*writable, tmp], readable=[*readable, log_dir], writable_files=writable_files,
                                        readonly_within=readonly_within, own_files=own_files), encoding="utf-8")
    return [SANDBOX_EXEC, "-f", str(profile), *argv], {"TMPDIR": str(tmp) + os.sep}


def claude_settings(*, cwd: Path, read_only_dirs: list[Path]) -> dict:
    """`sandbox` block for Claude Code's --settings (macOS Seatbelt / Linux bubblewrap); {} where it cannot run."""
    if not load_config().get("sandbox.enabled", True):
        return {}
    if not (IS_MAC or (os.name == "posix" and shutil.which("bwrap"))):
        return {}
    keep_ro = [_real(p) for p in read_only_dirs if _real(p) != _real(cwd)]
    back = _inside_private(_uniq([cwd, *read_only_dirs]))
    return {"sandbox": {
        "enabled": True,
        "failIfUnavailable": True,            # never fall back to running shell commands unconfined
        "allowUnsandboxedCommands": False,    # no `dangerouslyDisableSandbox` escape hatch
        "autoAllowBashIfSandboxed": False,    # which commands may run is still decided by --allowedTools
        "filesystem": {"denyRead": [*private_paths(), *secret_paths()],
                       "allowRead": back,   # back inside var/: this run's workspace + readable roots (allowRead beats denyRead)
                       "denyWrite": keep_ro},
        "network": {"allowedDomains": []},    # shell commands get no network (web access goes through WebFetch rules)
    }}


def codex_filesystem(*, cwd: Path, log_dir: Path, readable: list[Path], write: bool) -> dict[str, str]:
    """Codex permission profile (more specific paths win): read the disk except private data and credentials; this run's
    workspace (write for write runs) and readable roots back; a private TMPDIR."""
    fs: dict[str, str] = {":root": "read"}
    for p in private_paths():
        fs[p] = "none"
    for p in _uniq(readable):
        fs[p] = "read"
    if write:
        fs[":workspace_roots"] = "write"
    else:
        fs[_real(cwd)] = "read"
    fs[_real(log_dir / "tmp")] = "write"
    for p in secret_paths():
        fs[p] = "none"
    return fs
