"""Tracked agent invocation: every LLM/agent call in the system goes through run_agent().

It creates a `runs` row, writes prompt + raw logs under var/runs/<run_id>/, streams normalized events into
`run_events`, records cost in `cost_ledger`, enforces the daily budget, and picks an executor from routing.
"""
from __future__ import annotations

import logging
import threading
import time
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


def cancel_all_runs() -> int:
    """Signal every in-flight run to stop (daemon shutdown); each adapter then kills its process tree."""
    with _cancels_lock:
        events = list(_cancels.values())
    for ev in events:
        ev.set()
    if events:
        time.sleep(3)  # give adapters a moment to kill their process trees before the hard exit
    return len(events)


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


_avail_cache: dict[str, tuple[bool, str, float]] = {}
AVAIL_TTL_S = 600
_reserved: dict[str, float] = {}          # run_id -> reserved USD (pay-per-use executors)
_inflight: dict[str, str] = {}            # run_id -> executor
_budget_lock = threading.Lock()
_global_slots: threading.Semaphore | None = None


def _global_slot() -> threading.Semaphore:
    global _global_slots
    with _cancels_lock:
        if _global_slots is None:
            _global_slots = threading.Semaphore(int(load_config().get("scheduler.max_agent_processes", 5)))
        return _global_slots


def _available(name: str) -> tuple[bool, str]:
    hit = _avail_cache.get(name)
    if hit and time.monotonic() - hit[2] < AVAIL_TTL_S:
        return hit[0], hit[1]
    try:
        ok, why = get_executor(name).available()
    except KeyError:
        ok, why = False, "not registered"
    _avail_cache[name] = (ok, why, time.monotonic())
    return ok, why


def pick_executor(route: str, preferred: str | None = None) -> str:
    """Choose an executor for a routing key (category or purpose), honouring an explicit preference."""
    cfg = load_config()
    order = list(cfg.get(f"routing.{route}", None) or cfg.get("routing.other", ["claude", "codex"]))
    if preferred:
        order = [preferred] + [o for o in order if o != preferred]
    for name in order:
        if executor_enabled(name) and _available(name)[0]:
            return name
    raise RuntimeError(f"no available executor for route {route!r} (tried {order})")


RESERVE_WAIT_S = 300
RESERVE_POLL_S = 3


def typical_cost(executor: str, purpose: str, cap: float) -> float:
    """What a run of this kind usually costs (2x the recent average, at least $0.30, at most the per-run cap).
    Reserving the worst case ($1.50) for every concurrent run made a single in-flight run block the next one
    even with most of the day's budget unspent."""
    row = repo.db.q1("SELECT AVG(cost_usd) a, COUNT(*) n FROM (SELECT cost_usd FROM runs WHERE executor = ? AND purpose = ? "
                     "AND status = 'succeeded' AND cost_usd > 0 ORDER BY started_at DESC LIMIT 20)", (executor, purpose))
    if not row or not row["n"]:
        return round(cap, 3)  # no history yet: stay safe and reserve the full per-run cap
    return round(min(cap, max(0.30, 2 * float(row["a"]))), 3)


def reserve_budget(run_id: str, executor: str, reserve_usd: float) -> tuple[str, str]:
    """Admission control: reserve money (pay-per-use) or a run slot (subscription) before starting.
    Returns ("ok"|"budget"|"wait", reason): "budget" = real spend would exceed the cap (needs the human);
    "wait" = it only does not fit next to other runs' in-flight reservations (retry shortly)."""
    cfg = load_config()
    with _budget_lock:
        if executor == "codex":
            cap = int(cfg.get("budget.codex_daily_runs", 60))
            done = int(repo.cost_today("codex")["runs"])
            if done >= cap:
                return "budget", f"codex runs today {done}/{cap}"
            if done + sum(1 for e in _inflight.values() if e == "codex") >= cap:
                return "wait", f"codex runs in flight would reach {cap}"
        else:
            cap_usd = float(cfg.get("budget.daily_usd", 8.0))
            spent = float(repo.cost_today()["usd"])
            if spent + reserve_usd > cap_usd:
                return "budget", f"spent today ${spent:.2f} + ${reserve_usd:.2f} for this run > ${cap_usd:.2f}"
            if spent + sum(_reserved.values()) + reserve_usd > cap_usd:
                return "wait", f"spent ${spent:.2f} + in-flight ${sum(_reserved.values()):.2f} + ${reserve_usd:.2f} > ${cap_usd:.2f}"
            _reserved[run_id] = reserve_usd
        _inflight[run_id] = executor
        return "ok", "ok"


def release_budget(run_id: str) -> None:
    with _budget_lock:
        _reserved.pop(run_id, None)
        _inflight.pop(run_id, None)


def budget_ok(executor: str) -> tuple[bool, str]:
    """Read-only budget check (no reservation)."""
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
    """Run an agent synchronously and track everything. Returns (run_id, result). Never raises for agent failure
    (configuration errors such as an unknown/disabled executor raise before any run row is created)."""
    cfg = load_config()
    name = executor or pick_executor(route or purpose)
    if not executor_enabled(name):
        raise RuntimeError(f"executor {name!r} is disabled")
    ex = get_executor(name)
    if model is None and purpose in ("triage", "sensor", "digest") and name == "claude":
        model = cfg.get("executors.claude.triage_model") or None
    if model is None:
        model = cfg.get(f"executors.{name}.model") or None
    run_id = repo.create_run(purpose, name, task_id=task_id, model=model, attempt=attempt,
                             cwd=str(cwd) if cwd else None)
    cancel = threading.Event()
    with _cancels_lock:
        _cancels[run_id] = cancel
    budget_cap = max_budget_usd or cfg.get(f"executors.{name}.max_budget_usd_per_run")
    reserved = False
    result: RunResult | None = None
    try:
        log_dir = cfg.path("runs") / run_id
        log_dir.mkdir(parents=True, exist_ok=True)
        (log_dir / "prompt.md").write_text(prompt, encoding="utf-8")
        if cwd is None:
            cwd = log_dir / "cwd"
            cwd.mkdir(exist_ok=True)
        if check_budget:
            reserve = typical_cost(name, purpose, float(budget_cap or 0.5)) if name != "codex" else 0.0
            deadline = time.monotonic() + RESERVE_WAIT_S
            while True:
                state, why = reserve_budget(run_id, name, reserve)
                if state == "ok":
                    reserved = True
                    break
                if state == "budget":  # the day's REAL spend (plus this run) would exceed the cap
                    result = RunResult(status="failed", error=f"budget exhausted: {why}", error_class="budget")
                    return run_id, result
                # "wait": only other runs' in-flight reservations are in the way - they free up as those runs end
                if cancel.is_set() or time.monotonic() > deadline:
                    result = RunResult(status="failed", error=f"waited for budget reservations to free up: {why}",
                                       error_class="transient")
                    return run_id, result
                time.sleep(RESERVE_POLL_S)

        def on_event(kind: str, ev_name: str | None, data: Any) -> None:
            try:
                repo.add_run_event(run_id, kind, ev_name, data)
                if kind in ("tool_call", "status", "error", "usage"):
                    repo.update_run(run_id, heartbeat_at=utcnow())
            except Exception:  # pragma: no cover - never let logging kill a run
                log.exception("failed to record run event")

        def on_pid(pid: int) -> None:
            repo.update_run(run_id, pid=pid)

        spec = RunSpec(
            run_id=run_id, prompt=prompt, cwd=Path(cwd), log_dir=log_dir, purpose=purpose, sandbox=sandbox,  # type: ignore[arg-type]
            output_schema=output_schema, timeout_s=int(timeout_s or cfg.get(f"executors.{name}.timeout_seconds", 1200)),
            model=model, add_dirs=list(add_dirs or []), allow_network=allow_network, allow_connectors=allow_connectors,
            max_budget_usd=budget_cap, system_prompt=system_prompt, on_pid=on_pid,
        )
        with _global_slot(), _slot(name):
            if cancel.is_set():
                result = RunResult(status="cancelled", error="cancelled before start", error_class="cancelled")
                return run_id, result
            repo.update_run(run_id, status="running", started_at=utcnow(), heartbeat_at=utcnow(),
                            prompt_path=str(log_dir / "prompt.md"), log_path=str(log_dir))
            try:
                result = ex.run(spec, on_event, cancel)
            except Exception as e:  # adapter bug: record it, do not retry blindly
                log.exception("executor %s crashed", name)
                result = RunResult(status="failed", error=f"executor crash: {e!r}", error_class="permanent")
        return run_id, result
    except Exception as e:
        log.exception("run_agent infrastructure failure")
        result = RunResult(status="failed", error=f"infrastructure error: {e!r}", error_class="transient")
        return run_id, result
    finally:
        with _cancels_lock:
            _cancels.pop(run_id, None)
        if reserved:
            release_budget(run_id)
        if result is not None:
            _finish(run_id, name, model, task_id, result)


def _finish(run_id: str, name: str, model: str | None, task_id: str | None, result: RunResult) -> None:
    try:
        repo.update_run(run_id, status=result.status, ended_at=utcnow(), exit_code=result.exit_code,
                        final_text=(result.final_text or "")[:20000], structured=result.structured,
                        cost_usd=result.cost_usd or 0, tokens_in=result.tokens_in or 0, tokens_out=result.tokens_out or 0,
                        error=(result.error or None) and result.error[:4000], error_class=result.error_class,
                        model=result.model or model)
        if result.status != "failed" or result.cost_usd or result.tokens_in:
            repo.record_cost(run_id, name, result.model or model, result.cost_usd, result.tokens_in, result.tokens_out)
        if task_id and result.cost_usd:
            from . import db
            db.execute("UPDATE tasks SET cost_usd = cost_usd + ? WHERE id = ?", (result.cost_usd, task_id))
    except Exception:  # pragma: no cover
        log.exception("failed to persist run result %s", run_id)
