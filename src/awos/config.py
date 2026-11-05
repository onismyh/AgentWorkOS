"""Configuration loading: config/awos.toml <- config/local.toml <- env (AWOS_*)."""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any

ROOT = Path(os.environ.get("AWOS_HOME") or Path(__file__).resolve().parents[2])


def _deep_merge(base: dict, override: dict) -> dict:
    out = dict(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def _load_toml(path: Path) -> dict:
    if path.exists():
        with path.open("rb") as f:
            return tomllib.load(f)
    return {}


@dataclass
class Config:
    root: Path
    data: dict[str, Any] = field(default_factory=dict)

    def get(self, dotted: str, default: Any = None) -> Any:
        cur: Any = self.data
        for part in dotted.split("."):
            if not isinstance(cur, dict) or part not in cur:
                return default
            cur = cur[part]
        return cur

    def section(self, name: str) -> dict[str, Any]:
        return self.get(name, {}) or {}

    def path(self, key: str) -> Path:
        """Resolve a [paths] entry to an absolute directory (created on demand)."""
        raw = self.get(f"paths.{key}")
        if raw is None:
            raise KeyError(f"paths.{key} not configured")
        p = Path(raw)
        if not p.is_absolute():
            p = self.root / p
        p.mkdir(parents=True, exist_ok=True)
        return p

    @property
    def db_path(self) -> Path:
        return Path(os.environ.get("AWOS_DB") or (self.path("var") / "awos.db"))

    def secrets(self) -> dict[str, Any]:
        """Secrets live outside git in var/secrets.toml. Never log the returned values."""
        return _load_toml(self.path("var") / "secrets.toml")


@lru_cache(maxsize=1)
def load_config() -> Config:
    data = _load_toml(ROOT / "config" / "awos.toml")
    data = _deep_merge(data, _load_toml(ROOT / "config" / "local.toml"))
    extra = os.environ.get("AWOS_CONFIG")
    if extra:
        data = _deep_merge(data, _load_toml(Path(extra)))
    return Config(root=ROOT, data=data)


def reset_config_cache() -> None:
    load_config.cache_clear()
