"""Least-privilege sandboxing of agent processes, credential scrubbing, proxy pass-through and `awos setup`."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import pytest

from awos import osutil, runtimes
from awos.executors import proc, sandbox
from awos.executors.base import RunSpec
from awos.executors.claude import ClaudeExecutor, secret_rules


def _spec(tmp_path: Path, **kw) -> RunSpec:
    (tmp_path / "ws").mkdir(exist_ok=True)
    (tmp_path / "logs").mkdir(exist_ok=True)
    return RunSpec(run_id="r", prompt="p", cwd=tmp_path / "ws", log_dir=tmp_path / "logs", **kw)


def test_child_env_drops_credentials_except_the_executors_own(monkeypatch):
    for k, v in {"ANTHROPIC_AUTH_TOKEN": "a", "OPENAI_API_KEY": "o", "GH_TOKEN": "g", "SSH_AUTH_SOCK": "/s",
                 "AWS_SECRET_ACCESS_KEY": "w", "AWOS_X": "1", "PATH_EXTRA": "keep", "HOME": "/h"}.items():
        monkeypatch.setenv(k, v)
    plain = proc.child_env()
    assert not {"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GH_TOKEN", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "AWOS_X"} & set(plain)
    assert plain["PATH_EXTRA"] == "keep" and plain["HOME"] == "/h"
    claude = proc.child_env(keep_secrets=("ANTHROPIC_", "AWS_"))
    assert claude["ANTHROPIC_AUTH_TOKEN"] == "a" and "AWS_SECRET_ACCESS_KEY" in claude and "OPENAI_API_KEY" not in claude
    assert proc.child_env({"DEEPSEEK_API_KEY": "d"})["DEEPSEEK_API_KEY"] == "d"           # explicit extras always pass


def test_system_proxy_parsing():
    out = """<dictionary> {
  HTTPEnable : 1
  HTTPPort : 7897
  HTTPProxy : 127.0.0.1
  HTTPSEnable : 1
  HTTPSPort : 7897
  HTTPSProxy : 127.0.0.1
}"""
    env = osutil.parse_scutil_proxy(out)
    assert env["HTTPS_PROXY"] == env["https_proxy"] == "http://127.0.0.1:7897" and "127.0.0.1" in env["NO_PROXY"]
    assert osutil.parse_scutil_proxy("<dictionary> {\n  HTTPEnable : 0\n}") == {}


def test_secret_paths_cover_keys_and_honour_exemptions():
    paths = sandbox.secret_paths()
    home = os.path.realpath(os.path.expanduser("~"))
    assert os.path.join(home, ".ssh") in paths and any(p.endswith("secrets.toml") for p in paths)
    own = os.path.realpath(os.path.expanduser("~/.dsh/.credentials.yaml"))
    assert own in paths and own not in sandbox.secret_paths(("~/.dsh/.credentials.yaml",))


def test_seatbelt_profile_shape(tmp_path):
    prof = sandbox.seatbelt_profile(writable=[tmp_path / "a b"], deny_read=["/x/\"q"])
    assert prof.startswith("(version 1)\n(allow default)\n(deny file-write*)")
    assert f'(subpath "{os.path.realpath(tmp_path / "a b")}")' in prof and '"/x/\\"q"' in prof
    assert prof.index("(deny file-write*)") < prof.index("(allow file-write*") < prof.index("(deny file-read*")


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt")
def test_wrapped_process_writes_only_where_allowed_and_cannot_read_secrets(tmp_path, monkeypatch):
    # what the Pi / DSH adapters do: wrap the agent argv, then run it through run_process
    secret_dir = tmp_path / "home" / ".ssh"
    secret_dir.mkdir(parents=True)
    (secret_dir / "id").write_text("SECRET", encoding="utf-8")
    monkeypatch.setattr(sandbox, "SECRET_PATHS", (str(secret_dir),))
    ws, logs, outside = tmp_path / "ws", tmp_path / "logs", tmp_path / "outside.txt"
    ws.mkdir()
    logs.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent(f"""
        import os, tempfile
        res = {{}}
        for name, fn in [("inside", lambda: open("in.txt", "w").write("ok")),
                         ("outside", lambda: open({str(outside)!r}, "w").write("bad")),
                         ("secret", lambda: open({str(secret_dir / "id")!r}).read()),
                         ("tmp", lambda: tempfile.NamedTemporaryFile(delete=False).write(b"x"))]:
            try:
                fn(); res[name] = True
            except OSError:
                res[name] = False
        print(res)
    """), encoding="utf-8")
    argv, env = sandbox.wrap([sys.executable, "-I", str(probe)], log_dir=logs, writable=[ws])
    assert argv[:2] == [sandbox.SANDBOX_EXEC, "-f"] and env["TMPDIR"].startswith(str(logs / "tmp"))
    lines: list[str] = []
    pr = proc.run_process(argv, cwd=ws, stdin_text=None, timeout_s=60, env=env, on_stdout=lines.append)
    assert pr.exit_code == 0, pr.stderr_tail
    assert lines[-1] == str({"inside": True, "outside": False, "secret": False, "tmp": True})
    assert not outside.exists() and (ws / "in.txt").exists()


def test_claude_turns_on_its_sandbox_and_denies_credential_reads(tmp_path):
    extra = tmp_path / "ro"
    extra.mkdir()
    argv = ClaudeExecutor().build_argv(_spec(tmp_path, sandbox="workspace-write", add_dirs=[extra]))
    settings = json.loads(argv[argv.index("--settings") + 1])
    deny = argv[argv.index("--disallowedTools") + 1]
    if sys.platform == "darwin":
        sb = settings["sandbox"]
        assert sb["enabled"] and sb["failIfUnavailable"] and not sb["allowUnsandboxedCommands"]
        assert sb["network"]["allowedDomains"] == [] and os.path.realpath(extra) in sb["filesystem"]["denyWrite"]
        assert any(p.endswith("/.ssh") for p in sb["filesystem"]["denyRead"])
        assert "Read(//" in deny and set(secret_rules()) <= set(deny.split())
    assert settings["disableAllHooks"] is True
    bare = ClaudeExecutor().build_argv(_spec(tmp_path, purpose="triage"))
    assert "--bare" in bare and ("--settings" in bare) == (sys.platform == "darwin")


def test_runtime_cli_resolves_without_path(tmp_path, monkeypatch):
    from awos.config import load_config
    monkeypatch.setattr(proc.shutil, "which", lambda name, *a, **k: None)
    rt = load_config().root / "runtime" / "zz-fake-cli"
    try:
        (rt / "node_modules" / "zz" / "bin").mkdir(parents=True)
        (rt / "node_modules" / ".bin").mkdir(parents=True)
        js = rt / "node_modules" / "zz" / "bin" / "cli.js"
        js.write_text("console.log(1)", encoding="utf-8")
        (rt / "node_modules" / ".bin" / "zz-fake-cli").symlink_to(js)
        monkeypatch.setattr("awos.executors.noderuntime.node_exe", lambda: Path("/opt/node"))
        assert proc.resolve_command("zz-fake-cli") == ["/opt/node", os.path.realpath(js)]
        assert proc.resolve_command("does-not-exist-cli") is None
    finally:
        import shutil
        shutil.rmtree(rt, ignore_errors=True)


def test_setup_discovers_credentials_and_never_rewrites_existing_sections(tmp_path, monkeypatch):
    home = tmp_path / "home"
    (home / ".dsh").mkdir(parents=True)
    (home / ".dsh" / ".credentials.yaml").write_text("records:\n  deepseek-account-platform/default:\n    kind: grant\n", encoding="utf-8")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("DSH_HOME", raising=False)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    f = runtimes.discover({})
    assert f.sections["dsh"]["provider"] == "deepseek-account" and "pi" not in f.sections
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-0123456789")
    f = runtimes.discover({"dsh": {"env": {"DEEPSEEK_API_KEY": "old"}}})
    assert "dsh" not in f.sections and f.sections["pi"] == {"env": {"DEEPSEEK_API_KEY": "sk-test-0123456789"}}
    assert all("0123456789" not in line for line in f.lines)                                    # keys are never printed
    sec = tmp_path / "secrets.toml"
    sec.write_text('[notify]\nbark_url = "x"  # mine\n', encoding="utf-8")
    assert runtimes.append_sections(sec, {"pi": f.sections["pi"], "notify": {"bark_url": "y"}, "_codex_login": {}}) == ["pi"]
    data = tomllib.loads(sec.read_text(encoding="utf-8"))
    assert data["notify"]["bark_url"] == "x" and data["pi"]["env"]["DEEPSEEK_API_KEY"] == "sk-test-0123456789"
    assert "# mine" in sec.read_text(encoding="utf-8")
    if os.name == "posix":
        assert oct(sec.stat().st_mode & 0o777) == "0o600"
    assert runtimes.append_sections(sec, {"pi": {"x": 1}}) == []


def test_split_npm_spec():
    assert runtimes.split_spec("@openai/codex@0.167.0") == ("@openai/codex", "0.167.0")
    assert runtimes.split_spec("@deepseek-ai/dsh@0.2.2-rc.1") == ("@deepseek-ai/dsh", "0.2.2-rc.1")
    assert runtimes.split_spec("left-pad") == ("left-pad", None)


def test_dsh_account_mode_mounts_the_login_by_reference(tmp_path, monkeypatch):
    from awos.executors import dsh
    cred = tmp_path / "creds.yaml"
    cred.write_text("records: {}\n", encoding="utf-8")
    monkeypatch.setattr(dsh, "runtime_secrets", lambda name: {"provider": "deepseek-account", "model": "deepseek-flash",
                                                               "credentials": str(cred)})
    monkeypatch.setattr(dsh, "runtime_dir", lambda name: tmp_path / name)
    assert dsh.account_credentials() == cred
    home = dsh.prepare_home()
    assert (home / ".credentials.yaml").is_symlink() and os.path.realpath(home / ".credentials.yaml") == os.path.realpath(cred)
    patch = dsh.patch_file(_spec(tmp_path), "deepseek-flash").read_text(encoding="utf-8")
    assert "provider: deepseek-account" in patch and "- id: deepseek-account\n  disabled: false" in patch
    if sandbox.enabled():
        assert "runnerCommand" in patch and "awos-runner" in patch
    env = dsh.build_env(_spec(tmp_path))
    assert set(env) >= {"DSH_HOME", "DSH_TELEMETRY_DISABLED"} and "PATH" not in env                # only what DSH needs


def test_passthrough_runner_drops_profile_args():
    from awos.executors.dsh import PASSTHROUGH_RUNNER
    out = subprocess.run([*PASSTHROUGH_RUNNER, "--ro-bind", "/", "/", "--bind", "/w", "/w", "--", "/bin/echo", "a b"],
                         capture_output=True, text=True)
    assert out.returncode == 0 and out.stdout == "a b\n"
    bad = subprocess.run([*PASSTHROUGH_RUNNER, "--ro-bind", "/"], capture_output=True, text=True)
    assert bad.returncode == 125 and "awos-runner:" in bad.stderr
