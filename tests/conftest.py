"""Shared pytest configuration."""
from __future__ import annotations

import os

# No test may let the scheduler run a real sensor (GitHub, Gmail, Notion, mail, ...): scheduled collection is switched
# off globally; tests that exercise a sensor call run_sensor()/collect() directly with fakes.
os.environ["AWOS_DISABLE_SENSORS"] = "1"
os.environ.setdefault("AWOS_NOTIFY_DISABLE", "1")


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "live: hits real external services/agents (skipped unless available; run with -m live)")
