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

import awos.executors.noderuntime  # noqa: F401  (bound before any test patches awos.config)
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
                 "AWS_SECRET_ACCESS_KEY": "w", "AWOS_X": "1", "PATH_EXTRA": "keep", "HOME": "/h",
                 "DATABASE_URL": "postgres://me:pw@db/x", "PLAIN_URL": "https://example.com/a",
                 "DBUS_SESSION_BUS_ADDRESS": "unix:path=/run/bus", "TERM_SESSION_ID": "t1"}.items():
        monkeypatch.setenv(k, v)
    plain = proc.child_env(proxy=False)
    assert not {"ANTHROPIC_AUTH_TOKEN", "OPENAI_API_KEY", "GH_TOKEN", "SSH_AUTH_SOCK", "AWS_SECRET_ACCESS_KEY", "AWOS_X",
                "DATABASE_URL", "TERM_SESSION_ID"} & set(plain)
    assert plain["PATH_EXTRA"] == "keep" and plain["HOME"] == "/h" and plain["PLAIN_URL"].startswith("https://")
    assert plain["DBUS_SESSION_BUS_ADDRESS"] == "unix:path=/run/bus"                      # keyring logins on Linux still work
    claude = proc.child_env(keep_secrets=("ANTHROPIC_", "AWS_"), proxy=False)
    assert claude["ANTHROPIC_AUTH_TOKEN"] == "a" and "AWS_SECRET_ACCESS_KEY" in claude and "OPENAI_API_KEY" not in claude
    assert proc.child_env({"DEEPSEEK_API_KEY": "d"}, proxy=False)["DEEPSEEK_API_KEY"] == "d"   # explicit extras always pass


def test_claude_keeps_cloud_keys_only_when_it_uses_that_cloud(monkeypatch):
    from awos.executors.claude import keep_env
    monkeypatch.delenv("CLAUDE_CODE_USE_BEDROCK", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_USE_VERTEX", raising=False)
    assert keep_env() == ("ANTHROPIC_", "CLAUDE_CODE_")
    monkeypatch.setenv("CLAUDE_CODE_USE_BEDROCK", "1")
    assert "AWS_" in keep_env() and "GOOGLE_" not in keep_env()


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


def test_secret_and_private_paths():
    home = os.path.realpath(os.path.expanduser("~"))
    assert os.path.join(home, ".ssh") in sandbox.secret_paths() and any(p.endswith("secrets.toml") for p in sandbox.secret_paths())
    assert os.path.join(home, ".claude", ".credentials.json") in sandbox.secret_paths()          # no exemption for anyone
    from awos.config import load_config
    priv = sandbox.private_paths()
    assert os.path.realpath(load_config().path("var")) in priv and os.path.join(home, ".codex") in priv


def test_seatbelt_profile_shape(tmp_path):
    prof = sandbox.seatbelt_profile(writable=[tmp_path / "a b"], readable=[tmp_path / "r"], writable_files=[tmp_path / "f"],
                                    readonly_within=[tmp_path / "a b" / "profiles"], own_files=[Path("/x/\"q")],
                                    localhost_ports=[7897])
    lines = prof.splitlines()
    assert lines[:3] == ["(version 1)", "(allow default)", "(deny file-write*)"]
    assert f'(subpath "{os.path.realpath(tmp_path / "a b")}")' in prof and '"/x/\\"q"' in prof
    order = [next(i for i, l in enumerate(lines) if l.startswith(p)) for p in
             ("(allow file-write*", "(deny file-write* (subpath", "(deny file-read* ", "(allow file-read* ", "(deny file-read* file-write*",
              "(allow file-read* file-write*", '(deny network-outbound (remote ip "localhost:*"))')]
    assert order == sorted(order)                       # each narrower rule follows the broader one (last match wins)
    assert '(allow network-outbound (remote ip "localhost:7897"))' in prof and "(deny appleevent-send)" in prof
    assert "launchservicesd" in prof


@pytest.mark.skipif(sys.platform != "darwin", reason="macOS Seatbelt")
def test_wrapped_process_writes_only_where_allowed_and_cannot_read_secrets(tmp_path, monkeypatch):
    # what the Pi / DSH adapters do: wrap the agent argv, then run it through run_process
    secret_dir = tmp_path / "home" / ".ssh"
    secret_dir.mkdir(parents=True)
    (secret_dir / "id").write_text("SECRET", encoding="utf-8")
    own = tmp_path / "home" / "own-login.json"
    own.write_text("{}", encoding="utf-8")
    var = tmp_path / "var"
    (var / "workspaces" / "other").mkdir(parents=True)
    (var / "awos.db").write_text("MAIL", encoding="utf-8")
    monkeypatch.setattr(sandbox, "SECRET_PATHS", (str(secret_dir), str(own)))
    monkeypatch.setattr(sandbox, "PRIVATE_DATA", ())
    monkeypatch.setattr(sandbox, "private_paths", lambda: [os.path.realpath(var)])
    import http.server
    import threading
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    ws, logs, outside = var / "workspaces" / "mine", tmp_path / "logs", tmp_path / "outside.txt"
    ws.mkdir()
    logs.mkdir()
    probe = tmp_path / "probe.py"
    probe.write_text(textwrap.dedent(f"""
        import os, socket, tempfile
        res = {{}}
        for name, fn in [("inside", lambda: open("in.txt", "w").write("ok")),
                         ("outside", lambda: open({str(outside)!r}, "w").write("bad")),
                         ("secret", lambda: open({str(secret_dir / "id")!r}).read()),
                         ("own_login", lambda: open({str(own)!r}, "w").write("{{}}")),
                         ("awos_db", lambda: open({str(var / "awos.db")!r}).read()),
                         ("localhost", lambda: socket.create_connection(("127.0.0.1", {srv.server_port}), timeout=3).close()),
                         ("tmp", lambda: tempfile.NamedTemporaryFile(delete=False).write(b"x"))]:
            try:
                fn(); res[name] = True
            except OSError:
                res[name] = False
        print(res)
    """), encoding="utf-8")
    argv, env = sandbox.wrap([sys.executable, "-I", str(probe)], log_dir=logs, writable=[ws], readable=[ws], own_files=[own])
    assert argv[:2] == [sandbox.SANDBOX_EXEC, "-f"] and env["TMPDIR"].startswith(str(logs / "tmp"))
    lines: list[str] = []
    pr = proc.run_process(argv, cwd=ws, stdin_text=None, timeout_s=60, env=env, on_stdout=lines.append)
    assert pr.exit_code == 0, pr.stderr_tail
    srv.shutdown()
    assert lines[-1] == str({"inside": True, "outside": False, "secret": False, "own_login": True, "awos_db": False,
                             "localhost": False, "tmp": True})
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
        from awos.config import load_config
        assert os.path.realpath(load_config().path("var")) in sb["filesystem"]["denyRead"]
        assert sb["filesystem"]["allowRead"] == [] or all("/var/" in p for p in sb["filesystem"]["allowRead"])
        assert "Read(//" in deny and set(secret_rules()) <= set(deny.split()) and "api_token)" in deny
    assert settings["disableAllHooks"] is True
    bare = ClaudeExecutor().build_argv(_spec(tmp_path, purpose="triage"))
    assert "--bare" in bare and ("--settings" in bare) == (sys.platform == "darwin")


def test_runtime_cli_resolves_without_path(tmp_path, monkeypatch):
    from types import SimpleNamespace
    monkeypatch.setattr(proc.shutil, "which", lambda name, *a, **k: None)
    monkeypatch.setattr("awos.config.load_config", lambda: SimpleNamespace(root=tmp_path))
    rt = tmp_path / "runtime" / "zz-fake-cli"
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
    (home / ".pi" / "agent").mkdir(parents=True)
    (home / ".pi" / "agent" / "auth.json").write_text('{"x": 1}', encoding="utf-8")
    f = runtimes.discover({})
    assert f.sections["dsh"]["provider"] == "deepseek-account"
    assert f.sections["pi"] == {"credentials": str(home / ".pi" / "agent" / "auth.json")}      # the login file, not the dir
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


def test_codex_profile_hides_private_data_but_keeps_the_runs_own_dirs(tmp_path):
    from awos.config import load_config
    from awos.executors.codex import CodexExecutor, profiles_supported
    assert profiles_supported("codex-cli 0.167.0") and profiles_supported("codex-cli 1.0.0")
    assert not profiles_supported("codex-cli 0.166.0-alpha.6") and not profiles_supported("codex not found on PATH")
    var = os.path.realpath(load_config().path("var"))
    ws = Path(var) / "workspaces" / "t_x"
    fs = sandbox.codex_filesystem(cwd=ws, log_dir=Path(var) / "runs" / "r", readable=[Path("/opt/ref")], write=False)
    assert fs[":root"] == "read" and fs[var] == "none" and fs[str(ws)] == "read" and ":workspace_roots" not in fs
    assert fs[os.path.join(var, "runs", "r", "tmp")] == "write" and fs["/opt/ref"] == "read"
    assert sandbox.codex_filesystem(cwd=ws, log_dir=tmp_path, readable=[], write=True)[":workspace_roots"] == "write"
    if sandbox.enabled():
        import awos.executors.codex as cx
        old = cx.version_check
        try:
            cx.version_check = lambda argv, name, ttl_s=600: (True, "codex-cli 0.150.0")
            assert CodexExecutor.sandbox_args(_spec(tmp_path)) == ["-s", "read-only"]           # old Codex: coarse but never none
            cx.version_check = lambda argv, name, ttl_s=600: (True, "codex-cli 0.167.0")
            (tmp_path / "ü 中").mkdir()
            args = CodexExecutor.sandbox_args(_spec(tmp_path / "ü 中"))
            tomllib.loads(args[1].replace("permissions.awos.filesystem=", "x="))                   # valid TOML, non-ASCII kept
        finally:
            cx.version_check = old


def test_pi_never_uses_the_users_agent_dir(tmp_path, monkeypatch):
    from awos.executors import pi
    login = tmp_path / "user-pi" / "auth.json"
    login.parent.mkdir()
    login.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(pi, "runtime_secrets", lambda name: {"credentials": str(login)})
    monkeypatch.setattr(pi, "runtime_dir", lambda name: tmp_path / name)
    home = pi.prepare_home()
    assert home == tmp_path / "pi-home" and os.path.realpath(home / "auth.json") == os.path.realpath(login)
    assert pi.build_env(_spec(tmp_path))["PI_CODING_AGENT_DIR"] == str(home)
