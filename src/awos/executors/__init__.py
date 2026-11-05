"""Executor registry. Adapters register themselves here; callers use get_executor(name)."""
from __future__ import annotations

from .base import Executor, RunResult, RunSpec  # noqa: F401

_REGISTRY: dict[str, Executor] = {}


def register(executor: Executor) -> None:
    _REGISTRY[executor.name] = executor


def _load_builtin() -> None:
    if _REGISTRY:
        return
    import importlib

    for mod in ("codex", "claude", "pi", "dsh"):
        try:
            m = importlib.import_module(f"{__name__}.{mod}")
        except ImportError:
            continue
        cls = getattr(m, "EXECUTOR", None)
        if cls is not None:
            register(cls)


def get_executor(name: str) -> Executor:
    _load_builtin()
    if name not in _REGISTRY:
        raise KeyError(f"executor {name!r} not registered (have {sorted(_REGISTRY)})")
    return _REGISTRY[name]


def all_executors() -> dict[str, Executor]:
    _load_builtin()
    return dict(_REGISTRY)
