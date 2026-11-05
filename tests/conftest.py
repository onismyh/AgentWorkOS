"""Shared pytest configuration."""
from __future__ import annotations


def pytest_configure(config) -> None:
    config.addinivalue_line("markers", "live: hits real external services/agents (skipped unless available; run with -m live)")
