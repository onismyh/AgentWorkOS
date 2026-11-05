"""Tracked agent invocation: every LLM/agent call in the system goes through run_agent().

It creates a `runs` row, writes prompt + raw logs under var/runs/<run_id>/, streams normalized events into
`run_events`, records cost in `cost_ledger`, enforces the daily budget, and picks an executor from routing.
"""
from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Any

from . import repo
from .config import load_config
from .db import utcnow
from .executors import all_executors, get_executor
from .executors.base import RunResult, RunSpec

log = logging.getLogger("awos.llm")

# Global cancel registry so the API can cancel a running run.
_cancels: dict[str, threading.Event] = {}
_cancels_lock = threading.Lock()
_exec_slots: dict[str, threading.Semaphore] = {}


def _slot(executor: str) -> threading.Semaphore:
    with _cancels_lock:
        if executor not in _exec_slots:
            n = int(load_config().get(f"executors.{executor}.max_concurrent", 1) or 1)
            _exec_slots[executor] = threading.Semaphore(n)
        return _exec_slots[executor]


def cancel_run(run_id: str) -> bool:
    with _cancels_lock:
        ev = _cancels.get(run_id)
    if ev:
        ev.set()
        return True
    return False


def executor_enabled(name: str) -> bool:
    return bool(load_config().get(f"executors.{name}.enabled", False))


def available_executors() -> dict[str, tuple[bool, str]]:
    out = {}
    for name, ex in all_executors().items():
        if not executor_enabled(name):
            out[name] = (False, "disabled in config")
            continue
        try:
            out[name] = ex.available()
        except Exception as e:  # pragma: no cover
            out[name] = (False, f"error: {e}")
    return out


_avail_cache: dict[str, tuple[bool, str]] = {}


def pick_executor(route: str, preferred: str | None = None) -> str:
    """Choose an executor for a routing key (category or purpose), honouring an explicit preference."""
    cfg = load_config()
    order = list(cfg.get(f"routing.{route}", None) or cfg.get("routing.other", ["claude", "codex"]))
    if preferred:
        order = [preferred] + [o for o in order if o != preferred]
    for name in order:
        if not executor_enabled(name):
            continue
        if name not in _avail_cache:
            try:
                _avail_cache[name] = get_executor(name).available()
            except KeyError:
                _avail_cache[name] = (False, "not registered")
        if _avail_cache[name][0]:
            return name
    raise RuntimeError(f"no available executor for route {route!r} (tried {order})")


def budget_ok(executor: str) -> tuple[bool, str]:
    cfg = load_config()
    if executor == "codex":
        cap = int(cfg.get("budget.codex_daily_runs", 60))
        used = repo.cost_today("codex")["runs"]
        return (used < cap, f"codex runs today {used}/{cap}")
    cap_usd = float(cfg.get("budget.daily_usd", 8.0))
    spent = float(repo.cost_today()["usd"])
    return (spent < cap_usd, f"spent today ${spent:.2f}/${cap_usd:.2f}")


def run_agent(prompt: str, *, purpose: str, route: str | None = None, executor: str | None = None,
              task_id: str | None = None, attempt: int = 1, cwd: Path | None = None, sandbox: str = "read-only",
              output_schema: dict | None = None, model: str | None = None, timeout_s: int | None = None,
              add_dirs: list[Path] | None = None, allow_network: bool = True, allow_connectors: bool = False,
              system_prompt: str | None = None, max_budget_usd: float | None = None,
              check_budget: bool = True) -> tuple[str, RunResult]:
    """Run an agent synchronously and track everything. Returns (run_id, result). Never raises for agent failure."""
    cfg = load_config()
    name = executor or pick_executor(route or purpose)
    ex = get_executor(name)
    if model is None and purpose == "triage" and name == "claude":
        model = cfg.get("executors.claude.triage_model") or None
    if model is None:
        model = cfg.get(f"executors.{name}.model") or None
    run_id = repo.create_run(purpose, name, task_id=task_id, model=model, attempt=attempt,
                             cwd=str(cwd) if cwd else None)
    log_dir = cfg.path("runs") / run_id
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
    if cwd is None:
        cwd = log_dir / "cwd"
        cwd.mkdir(exist_ok=True)

    if check_budget:
        ok, why = budget_ok(name)
        if not ok:
            res = RunResult(status="failed", error=f"budget exhausted: {why}", error_class="budget")
            repo.update_run(run_id, status="failed", error=res.error, error_class="budget", started_at=utcnow(), ended_at=utcnow())
            return run_id, res

    cancel = threading.Event()
    with _cancels_lock:
        _cancels[run_id] = cancel

    def on_event(kind: str, ev_name: str | None, data: Any) -> None:
        try:
            repo.add_run_event(run_id, kind, ev_name, data)
            if kind in ("tool_call", "status", "error"):
                repo.update_run(run_id, heartbeat_at=utcnow())
        except Exception:  # pragma: no cover - never let logging kill a run
            log.exception("failed to record run event")

    def on_pid(pid: int) -> None:
        repo.update_run(run_id, pid=pid)

    spec = RunSpec(
        run_id=run_id, prompt=prompt, cwd=Path(cwd), log_dir=log_dir, purpose=purpose, sandbox=sandbox,  # type: ignore[arg-type]
        output_schema=output_schema, timeout_s=int(timeout_s or cfg.get(f"executors.{name}.timeout_seconds", 1200)),
        model=model, add_dirs=list(add_dirs or []), allow_network=allow_network, allow_connectors=allow_connectors,
        max_budget_usd=max_budget_usd or cfg.get(f"executors.{name}.max_budget_usd_per_run"),
        system_prompt=system_prompt, on_pid=on_pid,
    )
    sem = _slot(name)
    sem.acquire()
    try:
        repo.update_run(run_id, status="running", started_at=utcnow(), heartbeat_at=utcnow(),
                        prompt_path=str(log_dir / "prompt.md"), log_path=str(log_dir))
        try:
            result = ex.run(spec, on_event, cancel)
        except Exception as e:  # adapter bug: still record it
            log.exception("executor %s crashed", name)
            result = RunResult(status="failed", error=f"executor crash: {e!r}", error_class="transient")
    finally:
        sem.release()
        with _cancels_lock:
            _cancels.pop(run_id, None)

    repo.update_run(run_id, status=result.status, ended_at=utcnow(), exit_code=result.exit_code,
                    final_text=(result.final_text or "")[:20000], structured=result.structured,
                    cost_usd=result.cost_usd or 0, tokens_in=result.tokens_in or 0, tokens_out=result.tokens_out or 0,
                    error=(result.error or None) and result.error[:4000], error_class=result.error_class,
                    model=result.model or model)
    repo.record_cost(run_id, name, result.model or model, result.cost_usd, result.tokens_in, result.tokens_out)
    if task_id and result.cost_usd:
        from . import db
        db.execute("UPDATE tasks SET cost_usd = cost_usd + ? WHERE id = ?", (result.cost_usd, task_id))
    return run_id, result
