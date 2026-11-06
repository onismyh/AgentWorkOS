"""Agent runtimes under runtime/ and their credentials: `awos setup` (called by scripts/install.sh / install.ps1).

* Installs the npm-based agent CLIs into runtime/<name> (pinned versions, `[runtimes]` in config/awos.toml) with the
  runtime Node (runtime/node, else `node` on PATH on macOS/Linux). Nothing is installed globally; lifecycle scripts are
  not run. Idempotent: an installed package with the pinned version is kept.
* Finds credentials that already exist on the machine and wires them up without copying secrets around more than needed:
    codex  ~/.codex/auth.json (ChatGPT login, shared with the Codex app/CLI); else OPENAI_API_KEY -> `codex login --with-api-key`
    dsh    DEEPSEEK_API_KEY -> var/secrets.toml [dsh] env; else the DeepSeek Harness account login (~/.dsh/.credentials.yaml)
    pi     DEEPSEEK_API_KEY -> var/secrets.toml [pi] env; else an existing Pi login (~/.pi/agent/auth.json)
  Existing var/secrets.toml sections are never modified; the file is created/appended with mode 0600. Keys are never printed.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .config import load_config
from .osutil import IS_WINDOWS, NO_WINDOW

DEFAULT_PACKAGES = {
    "codex": "@openai/codex@0.167.0",
    "pi": "@earendil-works/pi-coding-agent@1.5.0",
    "dsh": "@deepseek-ai/dsh@0.2.2-rc.1",
}
DSH_ACCOUNT_RECORD = "deepseek-account-platform/default"


def root() -> Path:
    return load_config().root


def runtime_dir(name: str) -> Path:
    return root() / "runtime" / name


def packages() -> dict[str, str]:
    cfg = load_config().get("runtimes") or {}
    return {name: str(cfg.get(name) or spec) for name, spec in DEFAULT_PACKAGES.items()}


def split_spec(spec: str) -> tuple[str, str | None]:
    """`@scope/pkg@1.2.3` -> ("@scope/pkg", "1.2.3")."""
    m = re.match(r"^(@?[^@]+)(?:@(.+))?$", spec)
    if not m:
        raise ValueError(f"bad npm spec {spec!r}")
    return m.group(1), m.group(2)


def installed_version(name: str, pkg: str) -> str | None:
    p = runtime_dir(name) / "node_modules" / pkg / "package.json"
    try:
        return str(json.loads(p.read_text(encoding="utf-8")).get("version"))
    except (OSError, ValueError):
        return None


def npm_argv() -> list[str] | None:
    """[node, npm-cli.js] for the runtime Node (so npm and the agents run on the same Node), else `npm` on PATH."""
    from .executors.noderuntime import node_exe
    node = node_exe()
    if node is not None:
        real = Path(os.path.realpath(node))
        for cli in (real.parent / "node_modules" / "npm" / "bin" / "npm-cli.js",              # Windows zip layout
                    real.parent.parent / "lib" / "node_modules" / "npm" / "bin" / "npm-cli.js"):  # POSIX tarball / Homebrew
            if cli.is_file():
                return [str(node), str(cli)]
    npm = shutil.which("npm")
    return [npm] if npm else None


def install_runtime(name: str, spec: str, *, log=print) -> bool:
    pkg, version = split_spec(spec)
    have = installed_version(name, pkg)
    if have and (version is None or have == version):
        log(f"    {name}: {pkg}@{have} already installed - kept")
        return True
    npm = npm_argv()
    if npm is None:
        log(f"    {name}: no npm/Node found - skipped (install Node >= 22.19 and re-run)")
        return False
    target = runtime_dir(name)
    target.mkdir(parents=True, exist_ok=True)
    argv = npm + ["install", "--prefix", str(target), "--no-audit", "--no-fund", "--omit=dev", "--ignore-scripts",
                  "--save-exact", spec]
    log(f"    {name}: npm install {spec} -> runtime/{name}")
    cp = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900,
                        stdin=subprocess.DEVNULL, **NO_WINDOW)
    if cp.returncode != 0:
        log(f"    {name}: npm install failed (exit {cp.returncode}): {(cp.stderr or cp.stdout)[-600:]}")
        return False
    log(f"    {name}: {pkg}@{installed_version(name, pkg)} installed")
    return True


# --------------------------------------------------------------------------- credentials


@dataclass
class Found:
    lines: list[str] = field(default_factory=list)          # human-readable report (no secret values)
    sections: dict[str, dict] = field(default_factory=dict)  # secrets.toml sections to add


def _mask(v: str) -> str:
    return f"{v[:3]}…{v[-2:]}" if len(v) > 8 else "…"


def _home() -> Path:
    return Path(os.path.expanduser("~"))


def codex_logged_in() -> bool:
    home = Path(os.environ.get("CODEX_HOME") or _home() / ".codex")
    try:
        d = json.loads((home / "auth.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return bool(d.get("OPENAI_API_KEY") or (d.get("tokens") or {}).get("refresh_token") or d.get("tokens"))


def dsh_account_credentials() -> Path | None:
    home = Path(os.environ.get("DSH_HOME") or _home() / ".dsh")
    p = home / ".credentials.yaml"
    try:
        return p if DSH_ACCOUNT_RECORD in p.read_text(encoding="utf-8", errors="replace") else None
    except OSError:
        return None


def pi_login_dir() -> Path | None:
    d = _home() / ".pi" / "agent"
    a = d / "auth.json"
    return d if a.is_file() and a.stat().st_size > 2 else None


def discover(secrets: dict) -> Found:
    f = Found()
    deepseek = (os.environ.get("DEEPSEEK_API_KEY") or "").strip()

    if codex_logged_in():
        f.lines.append("codex: logged in (~/.codex/auth.json) - shared with the Codex app/CLI")
    elif (os.environ.get("OPENAI_API_KEY") or "").strip():
        f.lines.append("codex: not logged in; OPENAI_API_KEY found in the environment -> codex login --with-api-key")
        f.sections["_codex_login"] = {}
    else:
        f.lines.append("codex: not logged in - run `codex login` (ChatGPT account) or set OPENAI_API_KEY and re-run setup")

    if secrets.get("dsh"):
        f.lines.append("dsh: var/secrets.toml [dsh] already configured - kept")
    elif deepseek:
        f.sections["dsh"] = {"env": {"DEEPSEEK_API_KEY": deepseek}}
        f.lines.append(f"dsh: DEEPSEEK_API_KEY from the environment ({_mask(deepseek)})")
    elif (cred := dsh_account_credentials()) is not None:
        f.sections["dsh"] = {"provider": "deepseek-account", "model": "deepseek-flash", "credentials": str(cred)}
        f.lines.append(f"dsh: DeepSeek account login found ({cred}) - used by reference, not copied")
    else:
        f.lines.append("dsh: no credentials - set DEEPSEEK_API_KEY or log in once in the DeepSeek Harness app, then re-run setup")

    if secrets.get("pi"):
        f.lines.append("pi: var/secrets.toml [pi] already configured - kept")
    elif deepseek:
        f.sections["pi"] = {"env": {"DEEPSEEK_API_KEY": deepseek}}
        f.lines.append(f"pi: DEEPSEEK_API_KEY from the environment ({_mask(deepseek)})")
    elif (d := pi_login_dir()) is not None:
        f.sections["pi"] = {"agent_dir": str(d)}
        f.lines.append(f"pi: existing Pi login found ({d})")
    else:
        f.lines.append("pi: no credentials - set DEEPSEEK_API_KEY (a DeepSeek platform API key) and re-run setup")
    return f


def _toml_value(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)  # JSON string escapes are valid TOML basic-string escapes


def append_sections(path: Path, sections: dict[str, dict]) -> list[str]:
    """Append new [section] tables to secrets.toml (never edits existing ones). Returns the names written."""
    try:
        existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RuntimeError(f"{path} is not valid TOML ({e}); fix it and re-run setup") from None
    new = {k: v for k, v in sections.items() if not k.startswith("_") and k not in existing}
    if not new:
        return []
    text = "".join(f"\n[{name}]  # added by `awos setup`\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in body.items())
                   for name, body in new.items())
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a", encoding="utf-8") as fh:
        fh.write(text)
    if not IS_WINDOWS:
        os.chmod(path, 0o600)
    tomllib.loads(path.read_text(encoding="utf-8"))  # never leave a broken file behind unnoticed
    return list(new)


def codex_api_key_login(log=print) -> None:
    from .executors.proc import resolve_command
    argv = resolve_command(load_config().get("executors.codex.command", "codex"))
    if not argv:
        log("    codex: CLI not installed - cannot log in")
        return
    cp = subprocess.run(argv + ["login", "--with-api-key"], input=os.environ["OPENAI_API_KEY"], capture_output=True, text=True,
                        timeout=120, **NO_WINDOW)
    log(f"    codex: login --with-api-key {'ok' if cp.returncode == 0 else 'failed: ' + (cp.stderr or cp.stdout)[-300:]}")


def seed_pi_settings(log=print) -> None:
    """runtime/pi-home/settings.json from config/runtimes/pi-settings.json (shellPath is a Windows-only setting)."""
    dst = runtime_dir("pi-home") / "settings.json"
    if dst.exists():
        return
    data = json.loads((root() / "config" / "runtimes" / "pi-settings.json").read_text(encoding="utf-8"))
    if not IS_WINDOWS:
        data.pop("shellPath", None)
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log("    pi: runtime/pi-home/settings.json written")


def setup(*, install: bool = True, credentials: bool = True, only: list[str] | None = None, log=print) -> int:
    ok = True
    if install:
        log("==> agent runtimes (runtime/)")
        for name, spec in packages().items():
            if only and name not in only:
                continue
            ok = install_runtime(name, spec, log=log) and ok
        seed_pi_settings(log)
    if credentials:
        log("==> credentials")
        cfg = load_config()
        path = cfg.path("var") / "secrets.toml"
        found = discover(cfg.secrets())
        for line in found.lines:
            log(f"    {line}")
        written = append_sections(path, found.sections)
        if written:
            log(f"    wrote [{'], ['.join(written)}] to {path} (mode 0600)")
        if "_codex_login" in found.sections:
            codex_api_key_login(log)
    log("==> executors")
    from .llm import available_executors
    for k, v in available_executors().items():
        log(f"    {k:8s} {'OK ' if v[0] else 'NO '} {v[1]}")
    return 0 if ok else 1
