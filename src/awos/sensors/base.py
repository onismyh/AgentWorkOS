"""Sensor contract: a sensor turns an external/local source into SignalDrafts. The runner persists + dedupes them."""
from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class SignalDraft:
    title: str
    fingerprint: str                  # stable dedupe key; use repo.fingerprint(source, external_id, version-ish)
    body: str | None = None           # bounded excerpt, redact secrets; <= 4000 chars
    url: str | None = None            # file:///..., https://..., notion://...
    external_id: str | None = None
    occurred_at: str | None = None    # ISO UTC
    payload: dict[str, Any] = field(default_factory=dict)


@dataclass
class SensorContext:
    config: dict[str, Any]            # [sensors.<name>] section
    cursor: dict[str, Any]            # mutable watermark persisted after a successful collect()
    log: logging.Logger


class Sensor(ABC):
    name: str = "base"
    # Personal-context sensors may also return facts (kind, key, value) via collect_context().

    @abstractmethod
    def collect(self, ctx: SensorContext) -> list[SignalDraft]:
        """Return new observations. Raise on failure (runner records error + backoff). Must be idempotent:
        the same underlying item must always produce the same fingerprint."""

    def context_snapshot(self, ctx: SensorContext) -> str | None:
        """Optional: a short markdown description of what this source says about the user's current work,
        used by the context builder. Must not include secrets or full message bodies."""
        return None
