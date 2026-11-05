"""Sensor registry + runner. Concrete sensors live in sibling modules and expose `SENSOR = <instance>`."""
from __future__ import annotations

import importlib
import logging
import threading
from datetime import datetime, timedelta, timezone

from .. import repo
from ..config import load_config
from ..db import utcnow
from ..timeutil import in_window
from .base import Sensor, SensorContext, SignalDraft  # noqa: F401

log = logging.getLogger("awos.sensors")

BUILTIN = ("git", "files", "agent_history", "zotero", "inbox_drop", "mailmaster", "gmail", "notion")
_REGISTRY: dict[str, Sensor] = {}
_RUNNING: dict[str, threading.Lock] = {}
_RUNNING_GUARD = threading.Lock()


def register(sensor: Sensor) -> None:
    _REGISTRY[sensor.name] = sensor


def _load() -> None:
    if _REGISTRY:
        return
    for mod in BUILTIN + ("connectors",):
        try:
            m = importlib.import_module(f"{__name__}.{mod}")
        except ImportError as e:
            log.debug("sensor module %s not importable: %s", mod, e)
            continue
        for attr in ("SENSOR", "SENSORS"):
            obj = getattr(m, attr, None)
            if obj is None:
                continue
            for s in (obj if isinstance(obj, (list, tuple)) else [obj]):
                register(s)


def all_sensors() -> dict[str, Sensor]:
    _load()
    return dict(_REGISTRY)


def get_sensor(name: str) -> Sensor:
    _load()
    return _REGISTRY[name]


def _in_active_hours(cfg: dict) -> bool:
    hours = cfg.get("active_hours")
    return True if not hours else in_window(hours)


def is_due(name: str) -> bool:
    cfg = load_config().section(f"sensors.{name}") if load_config().get(f"sensors.{name}") else {}
    if not cfg.get("enabled", False):
        return False
    st = repo.sensor_state(name)
    if not st.get("enabled", 1):
        return False
    if not _in_active_hours(cfg):
        return False
    nxt = st.get("next_run_at")
    return not nxt or nxt <= utcnow()


def run_sensor(name: str, force: bool = False) -> dict:
    """Collect from one sensor, persist new signals, update its state. Returns a summary dict."""
    with _RUNNING_GUARD:
        lock = _RUNNING.setdefault(name, threading.Lock())
    if not lock.acquire(blocking=False):
        return {"sensor": name, "ok": False, "error": "already running", "new": 0}
    try:
        return _run_sensor(name)
    finally:
        lock.release()


def _run_sensor(name: str) -> dict:
    sensor = get_sensor(name)
    cfg = load_config().get(f"sensors.{name}", {}) or {}
    st = repo.sensor_state(name)
    ctx = SensorContext(config=cfg, cursor=dict(st.get("cursor") or {}), log=logging.getLogger(f"awos.sensors.{name}"))
    interval = float(cfg.get("interval_minutes", 60))
    started = utcnow()
    try:
        drafts = sensor.collect(ctx)
    except Exception as e:
        fails = int(st.get("consecutive_failures") or 0) + 1
        backoff_min = min(interval * (2 ** min(fails, 5)), 24 * 60)
        repo.update_sensor_state(name, last_run_at=started, last_error=f"{type(e).__name__}: {e}"[:2000],
                                 consecutive_failures=fails,
                                 next_run_at=(datetime.now(timezone.utc) + timedelta(minutes=backoff_min)).strftime("%Y-%m-%dT%H:%M:%SZ"))
        repo.log_event("sensor", name, "failed", {"error": str(e)[:500], "consecutive_failures": fails}, actor=f"sensor:{name}")
        log.warning("sensor %s failed: %s", name, e)
        return {"sensor": name, "ok": False, "error": str(e), "new": 0}
    new = 0
    for d in drafts:
        if repo.add_signal(name, d.title, d.fingerprint, body=d.body, url=d.url, external_id=d.external_id,
                           occurred_at=d.occurred_at, payload=d.payload):
            new += 1
    repo.update_sensor_state(name, last_run_at=started, last_ok_at=utcnow(), last_error=None, consecutive_failures=0,
                             cursor=ctx.cursor, last_count=new,
                             next_run_at=(datetime.now(timezone.utc) + timedelta(minutes=interval)).strftime("%Y-%m-%dT%H:%M:%SZ"))
    repo.log_event("sensor", name, "collected", {"drafts": len(drafts), "new": new}, actor=f"sensor:{name}")
    return {"sensor": name, "ok": True, "drafts": len(drafts), "new": new}
