"""Cross-platform helpers (awos.osutil)."""
from __future__ import annotations

import subprocess
import sys
import time

import pytest

from awos import osutil
from awos.engine.recovery import kill_tree, pid_alive


def test_path_basename_any_os():
    assert osutil.path_basename("D:\\work\\projA") == "projA"
    assert osutil.path_basename("\\\\?\\D:\\work\\projA\\") == "projA"
    assert osutil.path_basename("/Users/me/projB/") == "projB"
    assert osutil.path_basename("projC") == "projC"
    assert osutil.path_basename("") == ""


def test_bundled_or_path(tmp_path, monkeypatch):
    missing = tmp_path / "runtime" / "gh" / "bin" / "gh"
    monkeypatch.setattr(osutil.shutil, "which", lambda n: "/opt/bin/" + n)
    if osutil.IS_WINDOWS:
        assert osutil.bundled_or_path(missing, "gh") is None  # Windows keeps using only the bundled copy
    else:
        assert str(osutil.bundled_or_path(missing, "gh")) == "/opt/bin/gh"
    missing.parent.mkdir(parents=True)
    bundled = missing.with_name(osutil.exe_name("gh"))
    bundled.write_text("")
    assert osutil.bundled_or_path(missing, "gh") == bundled


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process groups")
def test_recovery_kill_tree_kills_process_group():
    # A detached daemon / executor leads its own session; killing it must take its children too.
    p = subprocess.Popen([sys.executable, "-c", "import subprocess, sys, time; "
                          "c = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
                          "print(c.pid, flush=True); time.sleep(60)"], stdout=subprocess.PIPE, start_new_session=True)
    child = int(p.stdout.readline())
    assert pid_alive(p.pid) and pid_alive(child)
    kill_tree(p.pid)
    p.wait(10)
    for _ in range(50):
        if not pid_alive(child):
            break
        time.sleep(0.1)
    assert not pid_alive(child)
