"""Agent runtimes under runtime/ and their credentials: `awos setup` (called by scripts/install.sh / install.ps1).

* Installs the npm-based agent CLIs into runtime/<name> (pinned versions, `[runtimes]` in config/awos.toml) with the
  runtime Node (runtime/node, else `node` on PATH on macOS/Linux). Nothing is installed globally; lifecycle scripts are
  not run. Idempotent: an installed package with the pinned version is kept.
* Finds credentials that already exist on the machine and wires them up without copying secrets around more than needed:
    codex  ~/.codex/auth.json (ChatGPT login, shared with the Codex app/CLI); else OPENAI_API_KEY -> `codex login --with-api-key`
    dsh    DEEPSEEK_API_KEY -> var/secrets.toml [dsh] env; else the DeepSeek Harness account login (~/.dsh/.credentials.yaml)
    pi     DEEPSEEK_API_KEY -> var/secrets.toml [pi] env; else an existing Pi login (~/.pi/agent/auth.json, linked)
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
    return f"…{v[-2:]}, {len(v)} chars" if len(v) > 8 else "set"


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


_SK = re.compile(r"\bsk-[A-Za-z0-9]{16,}\b")
SETUP_MARK = "# added by `awos setup`"


def read_key_file(path: str | Path) -> str | None:
    """The DeepSeek API key (`sk-...`) in a text file; None if there is none."""
    try:
        m = _SK.search(Path(os.path.expanduser(str(path))).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None
    return m.group(0) if m else None


def discover(secrets: dict, deepseek_key: str | None = None) -> Found:
    """`deepseek_key` (from --deepseek-key-file) or DEEPSEEK_API_KEY wins over an account login, and also replaces a
    [pi] / [dsh] section that setup wrote earlier without an API key (sections the user wrote are never touched)."""
    f = Found()
    deepseek = (deepseek_key or os.environ.get("DEEPSEEK_API_KEY") or "").strip()
    for name in ("pi", "dsh"):
        if deepseek and secrets.get(name) and not (secrets[name].get("env") or {}).get("DEEPSEEK_API_KEY") \
                and secrets[name].get("_by_setup"):
            secrets = {k: v for k, v in secrets.items() if k != name}
            f.sections[f"_replace_{name}"] = {}

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
        f.lines.append(f"dsh: DeepSeek API key ({_mask(deepseek)})")
    elif (cred := dsh_account_credentials()) is not None:
        f.sections["dsh"] = {"provider": "deepseek-account", "model": "deepseek-flash", "credentials": str(cred)}
        f.lines.append(f"dsh: DeepSeek account login found ({cred}) - used by reference, not copied")
    else:
        f.lines.append("dsh: no credentials - set DEEPSEEK_API_KEY or log in once in the DeepSeek Harness app, then re-run setup")

    if secrets.get("pi"):
        f.lines.append("pi: var/secrets.toml [pi] already configured - kept")
    elif deepseek:
        f.sections["pi"] = {"env": {"DEEPSEEK_API_KEY": deepseek}}
        f.lines.append(f"pi: DeepSeek API key ({_mask(deepseek)})")
    elif (d := pi_login_dir()) is not None:
        f.sections["pi"] = {"credentials": str(d / "auth.json")}
        f.lines.append(f"pi: existing Pi login found ({d / 'auth.json'}) - linked, the rest of ~/.pi stays private")
    else:
        f.lines.append("pi: no credentials - set DEEPSEEK_API_KEY (a DeepSeek platform API key) and re-run setup")
    return f


def _toml_value(v) -> str:
    if isinstance(v, dict):
        return "{ " + ", ".join(f"{k} = {_toml_value(x)}" for k, x in v.items()) + " }"
    return json.dumps(str(v), ensure_ascii=False)  # JSON string escapes are valid TOML basic-string escapes


def setup_written(path: Path) -> set[str]:
    """Sections of secrets.toml that `awos setup` wrote (header carries SETUP_MARK)."""
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return set()
    return set(re.findall(r"^\[([A-Za-z0-9_.-]+)\]\s*" + re.escape(SETUP_MARK), text, re.M))


def _drop_setup_sections(path: Path, names: set[str]) -> None:
    """Remove [name] tables that setup wrote (up to the next table header)."""
    lines = path.read_text(encoding="utf-8").splitlines(keepends=True)
    out, skipping = [], False
    for line in lines:
        m = re.match(r"^\[([A-Za-z0-9_.-]+)\]", line)
        if m:
            skipping = m.group(1) in names and SETUP_MARK in line
        if not skipping:
            out.append(line)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write("".join(out))
    os.replace(tmp, path)


def append_sections(path: Path, sections: dict[str, dict]) -> list[str]:
    """Append new [section] tables to secrets.toml. Existing tables are never edited, except ones setup wrote itself
    when `_replace_<name>` asks for it (an API key replacing an account login). Returns the names written."""
    try:
        existing = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise RuntimeError(f"{path} is not valid TOML ({e}); fix it and re-run setup") from None
    replace = {k[len("_replace_"):] for k in sections if k.startswith("_replace_")} & setup_written(path) & set(sections)
    if replace:
        _drop_setup_sections(path, replace)
        existing = {k: v for k, v in existing.items() if k not in replace}
    new = {k: v for k, v in sections.items() if not k.startswith("_") and k not in existing}
    if not new:
        return []
    text = "".join(f"\n[{name}]  {SETUP_MARK}\n" + "".join(f"{k} = {_toml_value(v)}\n" for k, v in body.items())
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


def fetch_pi_tools(log=print) -> None:
    """Pi looks for rg / fd in runtime/pi-home/bin before PATH and downloads them there; agent runs may not write bin/,
    so fetch them now (trusted, outside any sandbox)."""
    from .executors import pi
    from .executors.noderuntime import node_exe
    node = node_exe()
    mgr = runtime_dir("pi") / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "utils" / "tools-manager.js"
    if node is None or not mgr.is_file():
        return
    js = ("import(process.argv[1]).then(m => Promise.all([m.ensureTool('rg'), m.ensureTool('fd')]))"
          ".then(r => { console.log(JSON.stringify(r)); process.exit(r.every(Boolean) ? 0 : 1); }, e => { console.error(e); process.exit(1); })")
    env = {**os.environ, "PI_CODING_AGENT_DIR": str(pi.pi_home())}
    cp = subprocess.run([str(node), "-e", js, mgr.as_uri()], capture_output=True, text=True, timeout=300, env=env,
                        stdin=subprocess.DEVNULL, **NO_WINDOW)
    log(f"    pi: rg / fd {'ready' if cp.returncode == 0 else 'not fetched (grep/find fall back to PATH): ' + (cp.stderr or cp.stdout)[-200:]}")


def init_dsh_profile(log=print) -> None:
    """Create DSH's headless profile in runtime/dsh-home now (trusted, outside any sandbox): agent runs may not write it."""
    from .executors import dsh
    from .executors.noderuntime import node_exe
    node = node_exe()
    if node is None or not dsh.entry().exists() or (runtime_dir("dsh-home") / "profiles" / "headless").is_dir():
        return
    runtime_dir("dsh-home").mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "DSH_HOME": str(runtime_dir("dsh-home")), "DSH_TELEMETRY_DISABLED": "1", "DSH_TELEMETRY_MODE": "DISABLED"}
    cp = subprocess.run([str(node), str(dsh.entry()), "--profile", "headless", "--dump-config"], capture_output=True, text=True,
                        timeout=300, env=env, stdin=subprocess.DEVNULL, **NO_WINDOW)
    log(f"    dsh: headless profile {'created' if cp.returncode == 0 else 'not created: ' + cp.stderr[-300:]}")


def setup(*, install: bool = True, credentials: bool = True, only: list[str] | None = None, deepseek_key_file: str | None = None,
          log=print) -> int:
    ok = True
    if install:
        log("==> agent runtimes (runtime/)")
        for name, spec in packages().items():
            if only and name not in only:
                continue
            ok = install_runtime(name, spec, log=log) and ok
        seed_pi_settings(log)
        fetch_pi_tools(log)
        init_dsh_profile(log)
    if credentials:
        log("==> credentials")
        cfg = load_config()
        path = cfg.path("var") / "secrets.toml"
        key = None
        if deepseek_key_file:
            key = read_key_file(deepseek_key_file)
            log(f"    deepseek key file {deepseek_key_file}: {'found (' + _mask(key) + ')' if key else 'no sk-... key in it'}")
        secrets = cfg.secrets()
        for name in setup_written(path):
            if isinstance(secrets.get(name), dict):
                secrets[name] = {**secrets[name], "_by_setup": True}
        found = discover(secrets, key)
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
