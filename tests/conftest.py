"""Shared pytest configuration."""
from __future__ import annotations

import os

# No test may let the scheduler run a real sensor (GitHub, Gmail, Notion, mail, ...): scheduled collection is switched
# off globally; tests that exercise a sensor call run_sensor()/collect() directly with fakes.
os.environ["AWOS_DISABLE_SENSORS"] = "1"
# Likewise no test may run a real executor (codex / claude / pi / dsh): only executors named fake* are ever enabled.
os.environ["AWOS_ONLY_FAKE_EXECUTORS"] = "1"
os.environ.setdefault("AWOS_NOTIFY_DISABLE", "1")


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "live: hits real external services/agents (skipped unless available; run with -m live)")


def pytest_collection_modifyitems(config, items) -> None:
    """`live` tests call real agents (and cost money): run them only when asked for with `-m live`."""
    import pytest

    if "live" in (config.getoption("-m") or ""):
        return
    skip = pytest.mark.skip(reason="live test: run with -m live")
    for item in items:
        if "live" in item.keywords:
            item.add_marker(skip)
