"""Time helpers. The Windows system clock on this machine is set to a UTC-4 zone while the user lives on
Beijing time, so ALL wall-clock logic (quiet hours, digests, active hours, cost days) must use the configured
zone (`timezone` in config, default Asia/Shanghai), never naive datetime.now()."""
from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from zoneinfo import ZoneInfo

from .config import load_config


@lru_cache(maxsize=1)
def tz() -> ZoneInfo:
    return ZoneInfo(load_config().get("timezone", "Asia/Shanghai"))


def local_now() -> datetime:
    return datetime.now(timezone.utc).astimezone(tz())


def local_today() -> date:
    return local_now().date()


def local_hm() -> str:
    return local_now().strftime("%H:%M")


def in_window(window: list[str] | tuple[str, str] | None, hm: str | None = None) -> bool:
    """True if local HH:MM is inside [start, end]; windows may cross midnight (e.g. ["23:30", "07:30"])."""
    if not window:
        return False
    start, end = window
    hm = hm or local_hm()
    return start <= hm <= end if start <= end else (hm >= start or hm <= end)


def to_local_str(iso_utc: str | None) -> str:
    if not iso_utc:
        return ""
    dt = datetime.strptime(iso_utc, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return dt.astimezone(tz()).strftime("%Y-%m-%d %H:%M")
