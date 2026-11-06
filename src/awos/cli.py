"""`awos` command line: daemon lifecycle, inspection and human operations."""
from __future__ import annotations

import argparse
import json
import logging
import logging.handlers
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from .config import ROOT, load_config

LOCK = "daemon.lock"
STOP = "daemon.stop"


def _var() -> Path:
    return load_config().path("var")


def setup_logging(to_file: bool = True, level: int = logging.INFO) -> None:
    handlers: list[logging.Handler] = []
    if to_file:
        logs = load_config().path("logs")
        handlers.append(logging.handlers.RotatingFileHandler(logs / "daemon.log", maxBytes=5_000_000, backupCount=5, encoding="utf-8"))
    if sys.stderr is not None and sys.stderr.isatty() or not to_file:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s", handlers=handlers, force=True)
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def _read_lock() -> dict | None:
    p = _var() / LOCK
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _daemon_state() -> dict:
    from . import repo
    from .db import parse_ts
    from .engine.recovery import pid_alive
    lock = _read_lock() or {}
    alive = pid_alive(lock.get("pid"))
    hb = repo.kv_get("daemon.heartbeat") or {}
    age = None
    if hb.get("ts"):
        age = time.time() - parse_ts(hb["ts"]).timestamp()
    return {"pid": lock.get("pid"), "alive": alive, "heartbeat_age_s": age, "port": lock.get("port"), "started_at": lock.get("started_at")}


# ---------------------------------------------------------------------------- daemon


def cmd_daemon(args) -> int:
    setup_logging(to_file=True)
    log = logging.getLogger("awos.daemon")
    from .db import utcnow
    from .engine.recovery import pid_alive
    lock = _read_lock()
    if lock and pid_alive(lock.get("pid")) and lock.get("pid") != os.getpid():
        log.warning("daemon already running (pid %s)", lock["pid"])
        return 0
    cfg = load_config()
    port = int(args.port or cfg.get("server.port", 8765))
    (_var() / STOP).unlink(missing_ok=True)
    (_var() / LOCK).write_text(json.dumps({"pid": os.getpid(), "port": port, "started_at": utcnow()}), encoding="utf-8")

    from .engine.scheduler import Scheduler
    sched = Scheduler()
    server = None
    if not args.no_api:
        try:
            import uvicorn
            from .api.app import create_app
            server = uvicorn.Server(uvicorn.Config(create_app(), host=cfg.get("server.host", "127.0.0.1"), port=port,
                                                   log_level="warning", access_log=False))
            threading.Thread(target=server.run, daemon=True, name="api").start()
            log.info("API listening on http://%s:%s", cfg.get("server.host", "127.0.0.1"), port)
        except Exception:
            log.exception("API server failed to start; continuing without dashboard")

    def watch_stop() -> None:
        while not sched.stop_event.is_set():
            if (_var() / STOP).exists():
                log.info("stop file found; shutting down")
                sched.stop()
                if server is not None:
                    server.should_exit = True
                return
            time.sleep(5)

    threading.Thread(target=watch_stop, daemon=True, name="stop-watch").start()
    if sys.platform != "win32":
        import signal

        def on_term(signum, frame) -> None:  # launchctl / kill: shut down like the stop file does
            log.info("signal %s; shutting down", signum)
            sched.stop()

        signal.signal(signal.SIGTERM, on_term)
        signal.signal(signal.SIGHUP, on_term)
    try:
        sched.run_forever()
    except KeyboardInterrupt:
        sched.stop()
    finally:
        if server is not None:
            server.should_exit = True
        lk = _read_lock()
        if lk and lk.get("pid") == os.getpid():
            (_var() / LOCK).unlink(missing_ok=True)
        (_var() / STOP).unlink(missing_ok=True)
        # Worker threads are non-daemon: without a hard exit, a stopped daemon lingers until its agent runs finish.
        # Anything interrupted here is requeued by startup_recovery on the next start.
        from .llm import cancel_all_runs
        cancel_all_runs()
        logging.shutdown()
        os._exit(0)
    return 0


LAUNCHD_LABEL = "com.agentworkos.daemon"  # scripts/service-install.sh


def _spawn_daemon() -> int:
    exe = Path(sys.executable)
    pyw = exe.with_name("pythonw.exe") if exe.name.lower() == "python.exe" else exe
    if sys.platform == "win32":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP | CREATE_NO_WINDOW
        detach: dict = {"creationflags": 0x00000008 | 0x00000200 | 0x08000000}
    else:
        detach = {"start_new_session": True}  # own session: survives the terminal / watchdog that started it
    p = subprocess.Popen([str(pyw if pyw.exists() else exe), "-X", "utf8", "-m", "awos.cli", "daemon"], cwd=str(ROOT),
                         close_fds=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, **detach)
    return p.pid


def _launchd_kickstart() -> bool:
    """macOS: start the daemon through its LaunchAgent if one is loaded (so launchd, not us, owns the process)."""
    target = f"gui/{os.getuid()}/{LAUNCHD_LABEL}"
    try:
        if subprocess.run(["launchctl", "print", target], capture_output=True, timeout=15).returncode != 0:
            return False
        return subprocess.run(["launchctl", "kickstart", target], capture_output=True, timeout=15).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def cmd_ensure_running(args) -> int:
    st = _daemon_state()
    if st["alive"] and (st["heartbeat_age_s"] is None or st["heartbeat_age_s"] < 600):
        print(f"daemon ok pid={st['pid']} heartbeat_age={st['heartbeat_age_s'] and round(st['heartbeat_age_s'])}s")
        return 0
    if st["alive"]:
        from .engine.recovery import kill_tree
        print(f"daemon pid={st['pid']} is hung (heartbeat {round(st['heartbeat_age_s'])}s); restarting")
        kill_tree(st["pid"])
        time.sleep(2)
    if sys.platform == "win32":
        # Prefer the registered scheduled task so the daemon is not a child of the short-lived watchdog task.
        r = subprocess.run(["schtasks", "/Run", "/TN", r"\AgentWorkOS\Daemon"], capture_output=True,
                           creationflags=0x08000000)  # bytes: schtasks prints in the OEM code page (GBK)
        if r.returncode == 0:
            print("daemon task started via Task Scheduler")
            return 0
    elif sys.platform == "darwin" and _launchd_kickstart():
        print(f"daemon started via launchd ({LAUNCHD_LABEL})")
        return 0
    pid = _spawn_daemon()
    print(f"daemon started pid={pid}")
    return 0


def cmd_stop(args) -> int:
    st = _daemon_state()
    if not st["alive"]:
        print("daemon not running")
        return 0
    (_var() / STOP).write_text("stop", encoding="utf-8")
    for _ in range(30):
        time.sleep(1)
        if not _daemon_state()["alive"]:
            print("daemon stopped")
            return 0
    from .engine.recovery import kill_tree
    kill_tree(st["pid"])
    print("daemon killed")
    return 0


def cmd_status(args) -> int:
    from . import repo
    from .db import q
    st = _daemon_state()
    counts = {r["status"]: r["n"] for r in q("SELECT status, COUNT(*) n FROM tasks GROUP BY status")}
    pend = q("SELECT COUNT(*) n FROM approvals WHERE status = 'pending'")[0]["n"]
    running = q("SELECT COUNT(*) n FROM runs WHERE status = 'running'")[0]["n"]
    cost = repo.cost_today()
    out = {"daemon": st, "tasks": counts, "pending_approvals": pend, "running_runs": running,
           "cost_today_usd": round(float(cost["usd"]), 3), "runs_today": cost["runs"],
           "dashboard": f"http://127.0.0.1:{load_config().get('server.port', 8765)}/"}
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


# ---------------------------------------------------------------------------- operations


def cmd_add(args) -> int:
    from .engine.actions import add_manual_task
    tid = add_manual_task(args.title, args.description, level=args.level, category=args.category, priority=args.priority,
                          triage=not args.no_triage)
    print(tid)
    return 0


def cmd_inbox(args) -> int:
    from . import repo
    for st in ("triaged", "waiting_user", "review", "failed"):
        rows = repo.list_tasks(status=st, limit=50)
        if rows:
            print(f"== {st} ({len(rows)})")
            for t in rows:
                print(f"  {t['id']}  P{t['priority']} L{t['level']} [{t['category']}] {t['title']}")
    pend = repo.list_approvals(status="pending")
    if pend:
        print(f"== outbox ({len(pend)})")
        for a in pend:
            opts = "/".join(o["key"] for o in a["options"])
            print(f"  {a['id']}  [{a['kind']}:{a.get('action_type') or '-'} risk={a['risk']}] {a['title']}  options={opts}")
    return 0


def cmd_approve(args) -> int:
    from .engine.actions import decide
    from .hitl import outbox
    outbox.EXECUTE_INLINE = False  # this process exits right away; the daemon executes approved actions
    a = decide(args.approval_id, args.option, note=args.note, via="cli")
    print(json.dumps({"id": a["id"], "status": a["status"], "decision": a["decision"]}, ensure_ascii=False))
    if a["status"] == "approved" and a["kind"] == "action":
        print("queued: the daemon will execute it on its next tick (<= 1 min)")
    return 0


def cmd_task(args) -> int:
    from . import repo
    t = repo.get_task(args.task_id)
    if not t:
        print("not found")
        return 1
    t["children"] = [{"id": c["id"], "title": c["title"], "status": c["status"]} for c in repo.children(t["id"])]
    t["runs"] = [{"id": r["id"], "purpose": r["purpose"], "executor": r["executor"], "status": r["status"], "cost": r["cost_usd"],
                  "error": r.get("error")} for r in repo.list_runs(task_id=t["id"])]
    t["approvals"] = [{"id": a["id"], "title": a["title"], "status": a["status"]} for a in repo.list_approvals(task_id=t["id"])]
    t["artifacts"] = [{"title": a["title"], "path": a["path"]} for a in repo.list_artifacts(task_id=t["id"])]
    print(json.dumps(t, ensure_ascii=False, indent=2, default=str))
    return 0


def cmd_retry(args) -> int:
    from .engine.actions import retry_task
    retry_task(args.task_id, note=args.note)
    print("ok")
    return 0


def cmd_sensors(args) -> int:
    from . import repo
    from .sensors import all_sensors
    states = {s["name"]: s for s in repo.list_sensor_states()}
    for name in all_sensors():
        s = states.get(name, {})
        enabled = load_config().get(f"sensors.{name}.enabled", False)
        print(f"{name:14s} enabled={enabled!s:5s} last_ok={s.get('last_ok_at')} next={s.get('next_run_at')} "
              f"new={s.get('last_count')} fails={s.get('consecutive_failures')} err={(s.get('last_error') or '')[:80]}")
    return 0


def cmd_sensor_run(args) -> int:
    setup_logging(to_file=False)
    from .sensors import run_sensor
    print(json.dumps(run_sensor(args.name, force=True), ensure_ascii=False))
    return 0


def cmd_triage(args) -> int:
    setup_logging(to_file=False)
    from .brain.triage import triage_once
    print(json.dumps(triage_once(limit=args.limit), ensure_ascii=False))
    return 0


def cmd_tick(args) -> int:
    setup_logging(to_file=False)
    from .engine.scheduler import Scheduler
    s = Scheduler()
    print(json.dumps(s.tick(), ensure_ascii=False))
    if args.wait:
        while s.active or s.bg_jobs:
            time.sleep(2)
            s._reap()
    return 0


def cmd_run_task(args) -> int:
    """Run one specific task synchronously in the foreground (debugging)."""
    setup_logging(to_file=False)
    from . import repo
    from .engine.worker import process_task
    owner = f"cli:{os.getpid()}"
    t = repo.get_task(args.task_id)
    if t["status"] != "ready":
        repo.set_task_status(t["id"], "ready", "cli run", force=True)
    claimed = repo.claim_next_task(owner, 600, executor_ok=lambda row: row["id"] == args.task_id)
    if not claimed:
        print("could not claim (deps pending or backoff)")
        return 1
    process_task(args.task_id, owner)
    t = repo.get_task(args.task_id)
    print(json.dumps({"status": t["status"], "reason": t.get("status_reason"), "summary": (t.get("summary") or "")[:600]},
                     ensure_ascii=False, indent=2))
    return 0


def cmd_context(args) -> int:
    setup_logging(to_file=False)
    from .brain.context import context_brief, refresh_context, render_profile
    if args.action == "refresh":
        print(json.dumps(refresh_context(), ensure_ascii=False))
    elif args.action == "profile":
        print(render_profile())
    else:
        print(context_brief())
    return 0


def cmd_digest(args) -> int:
    from .engine.digest import build_digest, send_digest
    if args.send:
        print(send_digest("manual"))
    else:
        print(build_digest()[0])
    return 0


def cmd_mail(args) -> int:
    from . import mailstore
    if args.action == "search":
        for h in mailstore.search(args.arg, days=args.days, limit=args.limit):
            print(f"{h['ref']}  {h['received']}  {h['from'][:30]:30s}  {h['subject']}")
        return 0
    m = mailstore.read_mail(args.arg, max_chars=args.chars)
    print("\n".join([f"主题: {m['subject']}", f"发件人: {m['from']}", f"收件人: {', '.join(m['to'])}",
                     f"抄送: {', '.join(m['cc'])}", f"时间: {m['received']}", ""]))
    print(m["body"] if m["body_available"] else "（本地没有这封邮件的正文）")
    return 0


def cmd_doctor(args) -> int:
    from .llm import available_executors
    from .sensors import all_sensors
    print("root:", ROOT)
    print("db:", load_config().db_path)
    print("executors:")
    for k, v in available_executors().items():
        print(f"  {k:8s} {'OK ' if v[0] else 'NO '} {v[1]}")
    print("sensors:", ", ".join(all_sensors()))
    print("daemon:", _daemon_state())
    return 0


def cmd_setup(args) -> int:
    from .runtimes import setup
    return setup(install=not args.no_install, credentials=not args.no_credentials, only=args.only or None,
                 deepseek_key_file=args.deepseek_key_file)


def cmd_backup(args) -> int:
    from . import db
    from .timeutil import local_now
    dest = load_config().path("backups") / f"awos-manual-{local_now().strftime('%Y%m%d-%H%M%S')}.db"
    print(db.backup(dest))
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="awos", description="AgentWorkOS - local proactive personal agent OS")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("daemon", help="run scheduler + API (foreground)"); d.add_argument("--no-api", action="store_true"); d.add_argument("--port", type=int); d.set_defaults(fn=cmd_daemon)
    sub.add_parser("ensure-running", help="start the daemon if not healthy").set_defaults(fn=cmd_ensure_running)
    sub.add_parser("stop").set_defaults(fn=cmd_stop)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    a = sub.add_parser("add", help="add a task to the inbox")
    a.add_argument("title"); a.add_argument("-d", "--description"); a.add_argument("--level", type=int); a.add_argument("--category")
    a.add_argument("--priority", type=int, default=2); a.add_argument("--no-triage", action="store_true"); a.set_defaults(fn=cmd_add)
    sub.add_parser("inbox").set_defaults(fn=cmd_inbox)
    ap = sub.add_parser("approve", help="decide an Outbox item"); ap.add_argument("approval_id"); ap.add_argument("option", nargs="?", default="approve"); ap.add_argument("--note"); ap.set_defaults(fn=cmd_approve)
    t = sub.add_parser("task"); t.add_argument("task_id"); t.set_defaults(fn=cmd_task)
    r = sub.add_parser("retry"); r.add_argument("task_id"); r.add_argument("--note"); r.set_defaults(fn=cmd_retry)
    rt = sub.add_parser("run-task", help="process one task in the foreground"); rt.add_argument("task_id"); rt.set_defaults(fn=cmd_run_task)
    sub.add_parser("sensors").set_defaults(fn=cmd_sensors)
    sr = sub.add_parser("sensor-run"); sr.add_argument("name"); sr.set_defaults(fn=cmd_sensor_run)
    tr = sub.add_parser("triage"); tr.add_argument("--limit", type=int, default=25); tr.set_defaults(fn=cmd_triage)
    tk = sub.add_parser("tick", help="run one scheduler tick"); tk.add_argument("--wait", action="store_true"); tk.set_defaults(fn=cmd_tick)
    c = sub.add_parser("context"); c.add_argument("action", choices=["show", "refresh", "profile"], nargs="?", default="show"); c.set_defaults(fn=cmd_context)
    dg = sub.add_parser("digest"); dg.add_argument("--send", action="store_true"); dg.set_defaults(fn=cmd_digest)
    ml = sub.add_parser("mail", help="search / show local Mail Master mail (read-only)")
    ml.add_argument("action", choices=["search", "show"]); ml.add_argument("arg", help="search text or mail ref")
    ml.add_argument("--days", type=int, default=30); ml.add_argument("--limit", type=int, default=20)
    ml.add_argument("--chars", type=int, default=6000); ml.set_defaults(fn=cmd_mail)
    sub.add_parser("doctor").set_defaults(fn=cmd_doctor)
    su = sub.add_parser("setup", help="install codex / pi / dsh into runtime/ and wire up credentials found on this machine")
    su.add_argument("--no-install", action="store_true"); su.add_argument("--no-credentials", action="store_true")
    su.add_argument("--only", action="append", choices=["codex", "pi", "dsh"])
    su.add_argument("--deepseek-key-file", help="text file holding a DeepSeek API key (sk-...) for pi / dsh"); su.set_defaults(fn=cmd_setup)
    sub.add_parser("backup").set_defaults(fn=cmd_backup)
    args = p.parse_args(argv)
    return int(args.fn(args) or 0)


if __name__ == "__main__":
    sys.exit(main())
