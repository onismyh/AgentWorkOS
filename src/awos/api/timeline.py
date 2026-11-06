"""Per-project timeline: what the agents did, how it turned out, what the human changed - plus milestones and a flow graph.

Everything is derived at read time from rows that already exist (tasks, runs, approvals, artifacts, the audit `events` table and
the context facts); the only table of its own is `milestones` (explicit, user-set markers). Two views share one `Dataset`:
`lanes()` (the project list of the board's timeline mode) and `project_timeline(key)` (header stats, milestones, the flow graph
and the day-grouped feed). Texts are plain language on purpose: no run ids, no raw logs, only short excerpts.
"""
from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .. import db, repo
from ..brain import projects as projects_mod
from ..config import load_config
from ..timeutil import local_today, tz
from . import diagnose

UNASSIGNED = "__none__"
UNASSIGNED_NAME = "未归类"
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".svg"}
EXEC_LABEL = {"codex": "Codex", "claude": "Claude", "pi": "Pi", "dsh": "DeepSeek", "internal": "系统"}
SOURCE_LABEL = {"mailmaster": "邮件", "gmail": "邮件", "git": "Git 仓库", "files": "本地文档", "notion": "Notion", "zotero": "文献库",
                "agent_history": "AI 会话", "github": "GitHub", "inbox_drop": "投递箱"}
ACTION_LABEL = {"git_commit": "提交代码", "git_push": "推送分支", "open_mr": "发起 MR", "apply_to_project": "写入项目目录", "delete": "清理文件",
                "send_email": "发邮件", "post_message": "发消息", "publish": "发布", "calendar_write": "写日历", "notion_write": "写 Notion",
                "spend": "付款", "submit_form": "提交表单", "other": "执行动作"}
RUN_PURPOSES = ("execute", "verify", "plan")
OUTCOME_ISSUE = ("failed", "recovered")
_DATE = re.compile(r"(20\d\d)[-/.年](\d{1,2})[-/.月](\d{1,2})")
_STOP = {"data", "论文", "项目", "稿件", "模型", "汇报", "论文作者授权", "research", "project", "model"}


# --------------------------------------------------------------------------- small helpers


def _j(v: Any, default: Any) -> Any:
    if isinstance(v, (dict, list)):
        return v
    try:
        out = json.loads(v) if v else default
    except (TypeError, ValueError):
        return default
    return out if out is not None else default


def clip(text: Any, n: int = 200) -> str:
    """One readable line: markdown marks, code fences and line breaks removed, cut at n characters."""
    s = re.sub(r"```.*?```", " ", str(text or ""), flags=re.S)
    s = re.sub(r"[#>*`_]+|\|", " ", s)
    s = re.sub(r"^\s*[-+]\s+", "", s, flags=re.M)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1].rstrip() + "…"


def _dt(ts: str | None) -> datetime | None:
    return db.parse_ts(ts)


def _local_day(ts: str) -> str:
    d = _dt(ts)
    return d.astimezone(tz()).strftime("%Y-%m-%d") if d else ts[:10]


def _day_label(day: str, today: date) -> str:
    try:
        d = date.fromisoformat(day)
    except ValueError:
        return day
    if d == today:
        return "今天"
    if d == today - timedelta(days=1):
        return "昨天"
    return f"{d.month}月{d.day}日 周{'一二三四五六日'[d.weekday()]}"


def _exec_label(name: str | None) -> str:
    return EXEC_LABEL.get(name or "", name or "Agent")


def _norm(s: str) -> str:
    return projects_mod._norm(s)


def _proj_tokens(key: str) -> set[str]:
    """Distinctive words of a project name (long enough to be specific): used to attach tasks that never got a project."""
    out: set[str] = set()
    for t in re.split(r"[\s_()（）·,，、/\\]+", key):
        t = t.strip().lower()
        if not t or t in _STOP:
            continue
        if t.isascii():
            if len(t) >= 5 or (len(t) >= 4 and any(c.isdigit() for c in t)):
                out.add(t)
        elif len(t) >= 4:
            out.add(t)
    return out


# --------------------------------------------------------------------------- the dataset


class Dataset:
    """One consistent read of everything the timeline needs (a few hundred rows), plus the task -> project assignment."""

    def __init__(self) -> None:
        self.today = local_today()
        self.projects = projects_mod.canonical_projects()
        self.pkeys = {p["key"]: p for p in self.projects}
        self.tasks = {t["id"]: {**t, "meta": _j(t.get("meta"), {})} for t in db.q(
            "SELECT id, parent_id, root_id, title, description, kind, category, priority, level, level_cap, status, status_reason, executor, "
            "project, source, summary, cost_usd, created_at, updated_at, finished_at, meta FROM tasks")}
        self.runs = db.q("SELECT id, task_id, purpose, executor, status, started_at, ended_at, cost_usd, error, error_class, structured "
                         "FROM runs WHERE task_id IS NOT NULL ORDER BY started_at")
        self.approvals = [{**a, "options": _j(a.get("options"), []), "payload": _j(a.get("payload"), {}), "result": _j(a.get("result"), {})}
                          for a in db.q("SELECT * FROM approvals WHERE task_id IS NOT NULL ORDER BY created_at")]
        self.artifacts = db.q("SELECT id, task_id, run_id, kind, title, path, bytes, created_at FROM artifacts WHERE task_id IS NOT NULL ORDER BY created_at")
        self.deps = db.q("SELECT task_id, depends_on FROM task_deps")
        self.titems = {r["entity_id"]: r["topic_key"] for r in db.q("SELECT entity_id, topic_key FROM topic_items WHERE entity_type = 'task'")}
        self.tlinks = {r["topic_key"]: r["project_key"] for r in db.q("SELECT topic_key, project_key FROM topic_projects")}
        self.topic_titles = {r["key"]: r["title"] for r in db.q("SELECT key, title FROM topics")}
        self.events = db.q("SELECT id, ts, entity_type, entity_id, type, actor, data FROM events WHERE "
                           "(entity_type = 'task' AND type IN ('status_changed', 'retried', 'updated')) OR "
                           "(entity_type = 'topic' AND type = 'project_linked') OR entity_type = 'milestone' OR "
                           "(entity_type = 'context' AND type IN ('created', 'updated')) ORDER BY id")
        self.var = load_config().path("var").resolve()
        self._tok = {k: _proj_tokens(k) for k in self.pkeys}
        self.proj_of: dict[str, str | None] = {}
        for tid in self.tasks:
            self._resolve(tid)

    # ---- task -> project
    def _match_name(self, name: str | None) -> str | None:
        m = projects_mod.match_project(name, self.projects)
        return m["key"] if m else None

    def _resolve(self, tid: str, depth: int = 0) -> str | None:
        if tid in self.proj_of:
            return self.proj_of[tid]
        t = self.tasks[tid]
        found: str | None = None
        for ent in (tid, t.get("root_id"), t.get("parent_id")):
            pk = self.tlinks.get(self.titems.get(ent or ""))
            if pk in self.pkeys:
                found = pk
                break
        found = found or self._match_name(t.get("project"))
        if not found:
            up = t.get("parent_id") or (t.get("root_id") if t.get("root_id") != tid else None)
            if up and up in self.tasks and depth < 6:
                found = self._resolve(up, depth + 1)
        if not found:
            found = self._by_text(f"{t['title']} {(t.get('description') or '')[:300]}")
        self.proj_of[tid] = found
        return found

    def _by_text(self, text: str) -> str | None:
        n, low = _norm(text), text.lower()
        best, best_len, tie = None, 0, False
        for key in self.pkeys:
            pn = _norm(key)
            score = len(pn) if len(pn) >= 4 and pn in n else max((len(t) for t in self._tok[key] if t in low), default=0)
            if score > best_len:
                best, best_len, tie = key, score, False
            elif score and score == best_len and key != best:
                tie = True
        return None if tie else best

    def project_tasks(self, key: str) -> list[dict]:
        want = None if key == UNASSIGNED else key
        return [t for tid, t in self.tasks.items() if self.proj_of.get(tid) == want]

    # ---- artifacts
    def media_of(self, a: dict) -> dict | None:
        p = Path(a["path"]) if a.get("path") else None
        if not p or not p.is_file():
            return None
        try:
            p.resolve().relative_to(self.var)
        except ValueError:
            return None
        ext = p.suffix.lower()
        return {"id": a["id"], "title": a.get("title") or p.name, "ext": ext, "image": ext in IMAGE_EXTS,
                "url": f"/api/artifacts/{a['id']}/content"}


# --------------------------------------------------------------------------- events


def _ev(ts: str, actor: str, kind: str, title: str, outcome: str = "info", detail: str = "", *, task: dict | None = None, run: str | None = None,
        approval: str | None = None, cost: float = 0.0, executor: str | None = None, key: str = "") -> dict:
    return {"id": f"{kind}:{key or (task or {}).get('id', '')}:{ts}", "ts": ts, "day": _local_day(ts), "actor": actor, "human": actor == "human",
            "kind": kind, "title": title, "outcome": outcome, "detail": detail, "executor": executor, "cost": round(float(cost or 0), 4),
            "refs": {"task": (task or {}).get("id"), "run": run, "approval": approval}, "task_title": clip((task or {}).get("title"), 60),
            "media": [], "files": []}


def _task_events(ds: Dataset) -> dict[str, list[dict]]:
    """Events per project key (UNASSIGNED for tasks without a project)."""
    out: dict[str, list[dict]] = {}

    def put(project: str | None, e: dict) -> dict:
        out.setdefault(project or UNASSIGNED, []).append(e)
        return e

    # --- task creation
    for tid, t in ds.tasks.items():
        src = (t.get("source") or "")
        title = clip(t["title"], 90)
        if src == "manual":
            put(ds.proj_of[tid], _ev(t["created_at"], "human", "created", f"你新建了任务：{title}", "info", clip(t.get("description"), 160), task=t))
        elif src == "subtask":
            put(ds.proj_of[tid], _ev(t["created_at"], "system", "created", f"拆出子任务：{title}", "info", "", task=t))
        elif src == "context":
            put(ds.proj_of[tid], _ev(t["created_at"], "system", "created", f"上下文分析发现：{title}", "info", clip(t.get("description"), 160), task=t))
        elif src.startswith("sensor:"):
            what = SOURCE_LABEL.get(src.split(":", 1)[1], src.split(":", 1)[1])
            put(ds.proj_of[tid], _ev(t["created_at"], "system", "created", f"发现新事项（来自{what}）：{title}", "info", clip(t.get("description"), 160), task=t))
        else:
            put(ds.proj_of[tid], _ev(t["created_at"], "system", "created", f"新增任务：{title}", "info", "", task=t))

    # --- agent runs
    later_ok: dict[tuple[str, str], str] = {}
    for r in ds.runs:
        if r["status"] == "succeeded":
            later_ok[(r["purpose"], r["task_id"])] = max(r["id"], later_ok.get((r["purpose"], r["task_id"]), ""))
    run_events: dict[str, dict] = {}
    for r in ds.runs:
        t = ds.tasks.get(r["task_id"])
        if not t or r["purpose"] not in RUN_PURPOSES:
            continue
        label = _exec_label(r["executor"])
        ts = r.get("ended_at") or r.get("started_at") or t["created_at"]
        actor, st = f"agent:{r['executor']}", r["status"]
        st_json = _j(r.get("structured"), {})
        verb = {"execute": "执行", "verify": "质检", "plan": "拆解"}[r["purpose"]]
        if st == "succeeded":
            if r["purpose"] == "verify":
                ok = st_json.get("passed")
                title = f"{label} 质检：{'通过' if ok else '未通过，要求返工'}" + (f"（{st_json['score']}/10）" if st_json.get("score") is not None else "")
                e = _ev(ts, actor, "verify", title, "done" if ok else "failed", clip(st_json.get("feedback"), 220), task=t, run=r["id"], cost=r["cost_usd"], executor=r["executor"])
            elif r["purpose"] == "plan":
                n = len(st_json.get("subtasks") or [])
                e = _ev(ts, actor, "plan", f"{label} 把任务拆成 {n} 个子任务" if n else f"{label} 做了任务规划", "done", clip(st_json.get("rationale") or st_json.get("summary"), 200),
                        task=t, run=r["id"], cost=r["cost_usd"], executor=r["executor"])
            else:
                e = _ev(ts, actor, "run", f"{label} 完成了一轮执行", "done", clip(st_json.get("summary"), 240), task=t, run=r["id"], cost=r["cost_usd"], executor=r["executor"])
        elif st in ("failed", "timeout", "interrupted"):
            d = {**r, "task_status": t["status"]}
            diagnose.annotate([d])
            dg = d["diagnosis"]
            recovered = dg["resolved"] or later_ok.get((r["purpose"], r["task_id"]), "") > r["id"]
            e = _ev(ts, actor, "run", f"{label} {verb}没成功：{dg['label']}", "recovered" if recovered else "failed", clip(dg["hint"], 160),
                    task=t, run=r["id"], cost=r["cost_usd"], executor=r["executor"])
        elif st == "cancelled":
            e = _ev(ts, actor, "run", f"{label} 的{verb}被取消", "cancelled", "", task=t, run=r["id"], executor=r["executor"])
        else:
            e = _ev(ts, actor, "run", f"{label} 正在{verb}", "running", "", task=t, run=r["id"], executor=r["executor"])
        run_events[r["id"]] = put(ds.proj_of[t["id"]], e)

    # --- approvals: what the agent asked, what the human decided, what happened afterwards
    run_exec = {r["id"]: r["executor"] for r in ds.runs}
    for a in ds.approvals:
        t = ds.tasks.get(a["task_id"])
        if not t:
            continue
        proj = ds.proj_of[t["id"]]
        label = ACTION_LABEL.get(a.get("action_type") or "", "")
        title = clip(a["title"], 100)
        ex = run_exec.get(a.get("run_id"))
        asker = f"agent:{ex}" if ex else "system"
        auto = a.get("decided_via") == "auto"
        if auto and a["status"] in ("executed", "failed"):
            ok = a["status"] == "executed"
            put(proj, _ev(a.get("executed_at") or a.get("decided_at") or a["created_at"], "system", "action",
                          f"按白板规则自动放行并执行：{title}" if ok else f"自动放行的动作没执行成功：{title}", "done" if ok else "failed",
                          clip((a.get("result") or {}).get("details") or (a.get("result") or {}).get("error"), 180), task=t, approval=a["id"], key=a["id"]))
            continue
        if a["kind"] == "budget":
            ask = f"需要追加预算：{title.replace('预算不足：', '')}"
        elif a["kind"] == "decision":
            ask = f"请你拍板：{title}"
        else:
            ask = f"请你批准{('（' + label + '）') if label else ''}：{title}"
        pending = a["status"] == "pending"
        put(proj, _ev(a["created_at"], asker, "ask", ask, "waiting" if pending else "info", clip(a.get("recommendation"), 160) if pending else "",
                      task=t, approval=a["id"], executor=ex, key=a["id"]))
        if a["status"] == "expired":
            put(proj, _ev(a.get("expires_at") or a["created_at"], "system", "ask", f"这条请求过期没人处理：{title}", "failed", "", task=t, approval=a["id"], key=a["id"] + "x"))
        if a.get("decided_at") and not auto:
            opt = next((o.get("label") for o in a["options"] if o.get("key") == a.get("decision")), a.get("decision"))
            note = clip(a.get("decision_note"), 200)
            if a["status"] == "rejected" or a.get("decision") == "reject":
                title_h, outcome = f"你拒绝了：{title}", "rejected"
            elif a["kind"] == "budget":
                title_h, outcome = "你同意追加预算", "done"
            elif a["kind"] in ("decision", "review") or a.get("decision") not in ("approve", None):
                title_h, outcome = f"你选择了「{clip(opt, 40)}」：{title}", "done"
            else:
                title_h, outcome = f"你批准了{('（' + label + '）') if label else ''}：{title}", "done"
            put(proj, _ev(a["decided_at"], "human", "decision", title_h, outcome, note, task=t, approval=a["id"], key=a["id"]))
            if a["kind"] == "action" and a["status"] in ("executed", "failed") and a.get("executed_at"):
                ok = a["status"] == "executed"
                put(proj, _ev(a["executed_at"], "system", "action", f"已执行：{title}" if ok else f"执行没成功：{title}", "done" if ok else "failed",
                              clip((a.get("result") or {}).get("details") or (a.get("result") or {}).get("error"), 180), task=t, approval=a["id"], key=a["id"] + "r"))

    # --- task outcomes + the human's task actions (from the audit log)
    last_to: dict[tuple[str, str], dict] = {}
    retried: dict[str, list[str]] = {}
    for e in ds.events:
        if e["entity_type"] != "task" or e["entity_id"] not in ds.tasks:
            continue
        d = _j(e.get("data"), {})
        t = ds.tasks[e["entity_id"]]
        proj = ds.proj_of[t["id"]]
        if e["type"] == "retried" and e["actor"] == "user":
            retried.setdefault(t["id"], []).append(e["ts"])
            put(proj, _ev(e["ts"], "human", "intervene", f"你让 Agent 重新来过：{clip(t['title'], 70)}", "info", clip(d.get("note"), 200), task=t, key=f"retry{e['id']}"))
        elif e["type"] == "updated" and e["actor"] == "user" and d.get("snoozed_until"):
            until = _local_day(d["snoozed_until"])
            put(proj, _ev(e["ts"], "human", "intervene", f"你把它推迟到 {until}：{clip(t['title'], 70)}", "info", "", task=t, key=f"snz{e['id']}"))
        elif e["type"] == "status_changed":
            to, reason = d.get("to"), (d.get("reason") or "")
            if e["actor"] == "user":
                nm = clip(t["title"], 70)
                if to == "cancelled" and reason not in ("parent cancelled", "parent topic dismissed"):
                    why = "" if reason in ("cancelled by user", "") else reason
                    put(proj, _ev(e["ts"], "human", "intervene", f"你取消了任务：{nm}", "cancelled", clip(why, 200), task=t, key=f"st{e['id']}"))
                elif to == "dismissed" and reason not in ("parent cancelled", "parent topic dismissed"):
                    why = "" if reason in ("dismissed", "") else reason
                    put(proj, _ev(e["ts"], "human", "intervene", f"你忽略了：{nm}", "cancelled", clip(why, 200), task=t, key=f"st{e['id']}"))
                elif to == "done":
                    why = "" if reason in ("completed by user", "") else reason
                    put(proj, _ev(e["ts"], "human", "intervene", f"你验收通过：{nm}", "done", clip(why, 200), task=t, key=f"st{e['id']}"))
                elif to == "ready" and reason == "accepted by user":
                    put(proj, _ev(e["ts"], "human", "intervene", f"你接受了建议，排队执行：{nm}", "info", "", task=t, key=f"st{e['id']}"))
                elif to == "ready" and reason == "rework requested":
                    notes = [n for n in t["meta"].get("notes") or [] if str(n.get("text", "")).startswith("[返工意见]") and n.get("ts", "") <= e["ts"]]
                    put(proj, _ev(e["ts"], "human", "intervene", f"你要求返工：{nm}", "info", clip((notes[-1]["text"] if notes else "").replace("[返工意见]", ""), 200),
                                  task=t, key=f"st{e['id']}"))
            elif to in ("review", "waiting_user", "failed", "done"):
                last_to[(t["id"], to)] = {**e, "d": d}

    for (tid, to), e in last_to.items():
        t = ds.tasks[tid]
        proj = ds.proj_of[tid]
        ex = t["meta"].get("last_executor") or t.get("executor")
        actor = f"agent:{ex}" if ex else "system"
        nm = clip(t["title"], 80)
        still = t["status"] == to
        if to == "done":
            put(proj, _ev(e["ts"], actor, "outcome", f"任务完成：{nm}", "done", clip(t.get("summary"), 260), task=t, executor=ex, key="done"))
        elif to == "review":
            put(proj, _ev(e["ts"], actor, "outcome", f"交付物已就绪，等你验收：{nm}", "waiting" if still else "done", clip(t.get("summary"), 260), task=t, executor=ex, key="review"))
        elif to == "waiting_user":
            put(proj, _ev(e["ts"], actor, "outcome", f"交还给你接手：{nm}", "waiting" if still else "info", clip(e["d"].get("reason") or t.get("status_reason"), 200), task=t, executor=ex, key="handback"))
        elif to == "failed":
            put(proj, _ev(e["ts"], actor, "outcome", f"任务没做成：{nm}", "failed" if still else "recovered", clip(e["d"].get("reason") or t.get("status_reason"), 200), task=t, executor=ex, key="failed"))

    # --- deliverables ride on the run that produced them (images become thumbnails, other files small chips)
    by_task: dict[str, list[dict]] = {}
    for ev in (e for lst in out.values() for e in lst):
        if ev["kind"] in ("run", "verify", "plan") and ev["refs"]["task"]:
            by_task.setdefault(ev["refs"]["task"], []).append(ev)
    for a in ds.artifacts:
        m = ds.media_of(a)
        if not m or a["task_id"] not in ds.tasks:
            continue
        evs = sorted(by_task.get(a["task_id"], []), key=lambda x: x["ts"])
        host = run_events.get(a.get("run_id"))
        if host is None and evs:
            host = next((e for e in reversed(evs) if e["ts"] <= a["created_at"]), evs[-1])
        if host is None:
            continue
        bucket = host["media"] if m["image"] else host["files"]
        if len(bucket) < 6:
            bucket.append(m)
    return out


def _side_events(ds: Dataset) -> dict[str, list[dict]]:
    """Events that belong to a project directly: progress notes, relinks, milestone edits."""
    out: dict[str, list[dict]] = {}
    for e in ds.events:
        d = _j(e.get("data"), {})
        if e["entity_type"] == "milestone":
            pk = d.get("project")
            title = clip(d.get("title"), 70)
            if e["type"] == "created":
                text = f"你设定了里程碑：{title}"
            elif e["type"] == "deleted":
                text = f"你删除了里程碑：{title}"
            elif d.get("to") == "done" and d.get("from") != "done":
                text = f"你把里程碑标为完成：{title}"
            else:
                text = f"你调整了里程碑：{title}"
            out.setdefault(pk or UNASSIGNED, []).append(_ev(e["ts"], "human", "milestone", text, "done" if d.get("to") == "done" else "info", "", key=f"ms{e['id']}"))
        elif e["entity_type"] == "topic" and d.get("project") in ds.pkeys:
            tt = clip(ds.topic_titles.get(e["entity_id"], e["entity_id"]), 50)
            human = e["actor"] == "user"
            out.setdefault(d["project"], []).append(_ev(e["ts"], "human" if human else "system", "relink",
                                                        f"你把话题「{tt}」归入本项目" if human else f"话题「{tt}」被自动归入本项目", "info", "", key=f"lk{e['id']}"))
        elif e["entity_type"] == "context" and str(d.get("key", "")).startswith(projects_mod.PROGRESS_PREFIX) and e["actor"].startswith("agent"):
            name = d["key"][len(projects_mod.PROGRESS_PREFIX):]
            pk = ds._match_name(name)
            val = re.sub(r"^（.+?）", "", str(d.get("value") or d.get("new") or ""))
            if pk and val:
                out.setdefault(pk, []).append(_ev(e["ts"], "agent", "progress", "Agent 更新了项目进展", "info", clip(val, 240), key=f"pg{e['id']}"))
    return out


# --------------------------------------------------------------------------- milestones


def _milestones(ds: Dataset, key: str, tasks: list[dict]) -> list[dict]:
    """Explicit rows + auto-derived markers (labelled by `source`). `status` is effective: an undone planned date in the past is 'missed'."""
    today = ds.today.isoformat()
    out: list[dict] = []
    for m in repo.list_milestones(key):
        st = m["status"]
        if st in ("planned", "active") and m.get("target_date") and m["target_date"] < today:
            st = "missed"
        out.append({"id": m["id"], "title": m["title"], "summary": m.get("summary") or "", "target_date": m.get("target_date"), "status": st,
                    "stored_status": m["status"], "source": m["source"], "done_at": m.get("done_at"), "editable": True,
                    "evidence": m.get("evidence") or {}})
    if key != UNASSIGNED:
        for f in projects_mod.related_facts(key, limit=8)["deadline"]:
            mt = _DATE.search(f"{f['key']} {f['value']}")
            if not mt:
                continue
            try:
                dd = date(int(mt.group(1)), int(mt.group(2)), int(mt.group(3)))
            except ValueError:
                continue
            out.append({"id": f"auto:deadline:{_norm(f['key'])[:24]}", "title": clip(f["key"], 60), "summary": clip(f["value"], 160), "target_date": dd.isoformat(),
                        "status": "planned" if dd >= ds.today else "missed", "stored_status": "planned", "source": "auto", "origin": "来自上下文里的截止日期",
                        "done_at": None, "editable": False, "evidence": {}})
    delivered = [t for t in tasks if t["status"] == "done" and min(int(t["level"] or 1), int(t.get("level_cap") or 5)) >= 4 and t.get("finished_at")]
    for t in sorted(delivered, key=lambda x: x["finished_at"], reverse=True)[:6]:
        out.append({"id": f"auto:task:{t['id']}", "title": f"交付：{clip(t['title'], 56)}", "summary": clip(t.get("summary"), 160), "target_date": None, "status": "done",
                    "stored_status": "done", "source": "auto", "origin": "Agent 自主完成的重要交付", "done_at": t["finished_at"], "editable": False, "evidence": {"task_id": t["id"]}})

    def when(m: dict) -> str:
        return m.get("target_date") or (_local_day(m["done_at"]) if m.get("done_at") else "9999-99-99")
    return sorted(out, key=lambda m: (when(m), m["title"]))


# --------------------------------------------------------------------------- flow graph


def _flow(ds: Dataset, tasks: list[dict], events: list[dict]) -> dict:
    """Block diagram data: tasks as nodes, dependencies / roll-ups as edges, human approvals as gates."""
    live = {"running", "ready", "waiting_approval", "waiting_user", "review", "blocked"}
    pick = sorted(tasks, key=lambda t: (t["status"] in live, t["updated_at"]), reverse=True)[:36]
    ids = {t["id"] for t in pick}
    run_ex: dict[str, str] = {}
    for r in ds.runs:
        if r["purpose"] == "execute" and r["task_id"] in ids:
            run_ex[r["task_id"]] = r["executor"]
    nodes = [{"id": t["id"], "type": "task", "title": clip(t["title"], 64), "status": t["status"], "level": min(int(t["level"] or 1), int(t.get("level_cap") or 5)),
              "executor": t["meta"].get("last_executor") or run_ex.get(t["id"]) or t.get("executor"), "cost": round(float(t["cost_usd"] or 0), 3),
              "parent": t.get("parent_id") if t.get("parent_id") in ids else None, "ts": t["updated_at"]} for t in pick]
    edges, linked = [], set()
    for d in ds.deps:
        if d["task_id"] in ids and d["depends_on"] in ids:
            edges.append({"from": d["depends_on"], "to": d["task_id"], "type": "dep"})
            linked.add((d["depends_on"], d["task_id"]))
    for t in pick:  # a parent waits for its subtasks: show the roll-up unless a dependency already says so
        p = t.get("parent_id")
        if p in ids and (t["id"], p) not in linked and (p, t["id"]) not in linked:
            edges.append({"from": t["id"], "to": p, "type": "rollup"})
    gates = []
    human_gates = [a for a in ds.approvals if a["task_id"] in ids and a.get("decided_via") != "auto"]
    for a in sorted(human_gates, key=lambda a: (a["status"] == "pending", a["created_at"]), reverse=True)[:10]:   # open asks first, then the newest decisions
        opt = next((o.get("label") for o in a["options"] if o.get("key") == a.get("decision")), None)
        state = "waiting" if a["status"] == "pending" else "rejected" if a["status"] == "rejected" else "expired" if a["status"] == "expired" else "decided"
        gates.append({"id": a["id"], "type": "gate", "title": clip(a["title"], 56), "state": state, "decision": clip(opt, 30) if opt else None,
                      "kind": a["kind"], "task": a["task_id"], "ts": a.get("decided_at") or a["created_at"]})
        edges.append({"from": a["task_id"], "to": a["id"], "type": "gate"})
    return {"nodes": nodes + gates, "edges": edges, "truncated": len(tasks) > len(pick)}


# --------------------------------------------------------------------------- views


def _stats(ds: Dataset, tasks: list[dict], events: list[dict]) -> dict:
    ids = {t["id"] for t in tasks}
    runs = [r for r in ds.runs if r["task_id"] in ids and r["purpose"] in RUN_PURPOSES and r["status"] in ("succeeded", "failed", "timeout", "interrupted", "cancelled")]
    per_exec: dict[str, dict] = {}
    for r in runs:
        d = per_exec.setdefault(r["executor"], {"runs": 0, "ok": 0})
        d["runs"] += 1
        d["ok"] += 1 if r["status"] == "succeeded" else 0
    live = [t for t in tasks if t["status"] not in ("cancelled", "dismissed")]
    asks = sum(1 for a in ds.approvals if a["task_id"] in ids and a["status"] == "pending") + sum(1 for t in tasks if t["status"] in ("waiting_user", "review"))
    stamps = [e["ts"] for e in events]
    return {"tasks": len(live), "tasks_done": sum(1 for t in live if t["status"] == "done"),
            "tasks_active": sum(1 for t in live if t["status"] in ("running", "ready", "blocked", "waiting_approval")),
            "tasks_failed": sum(1 for t in live if t["status"] == "failed"),
            "agent_runs": len(runs), "agent_ok": sum(1 for r in runs if r["status"] == "succeeded"), "executors": per_exec,
            "interventions": sum(1 for e in events if e["human"]), "open_asks": asks,
            "cost_usd": round(sum(float(r["cost_usd"] or 0) for r in runs), 3), "first_ts": min(stamps) if stamps else None, "last_ts": max(stamps) if stamps else None}


def _progress(stats: dict, miles: list[dict]) -> dict:
    explicit = [m for m in miles if m["editable"]]
    pct_tasks = round(100 * stats["tasks_done"] / stats["tasks"]) if stats["tasks"] else 0
    pct_ms = round(100 * sum(1 for m in explicit if m["status"] == "done") / len(explicit)) if explicit else None
    return {"pct": pct_ms if pct_ms is not None else pct_tasks, "basis": "milestones" if pct_ms is not None else "tasks",
            "tasks": {"done": stats["tasks_done"], "total": stats["tasks"]},
            "milestones": {"done": sum(1 for m in explicit if m["status"] == "done"), "total": len(explicit)}}


def _next_milestone(miles: list[dict]) -> dict | None:
    todo = [m for m in miles if m["status"] != "done" and m.get("target_date")]
    todo += [m for m in miles if m["status"] in ("planned", "active") and not m.get("target_date") and m["editable"]]
    m = todo[0] if todo else None
    return {"title": m["title"], "target_date": m.get("target_date"), "status": m["status"]} if m else None


def _all_events(ds: Dataset) -> dict[str, list[dict]]:
    ev = _task_events(ds)
    for pk, lst in _side_events(ds).items():
        ev.setdefault(pk, []).extend(lst)
    for lst in ev.values():
        lst.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
    return ev


def _lane(ds: Dataset, key: str, name: str, summary: str, events: list[dict]) -> dict:
    tasks = ds.project_tasks(key)
    stats = _stats(ds, tasks, events)
    miles = _milestones(ds, key, tasks)
    return {"key": key, "name": name, "summary": clip(summary, 120), "progress": _progress(stats, miles), "next_milestone": _next_milestone(miles),
            "last_activity": stats["last_ts"], "stats": {k: stats[k] for k in ("tasks", "tasks_done", "tasks_active", "agent_runs", "agent_ok", "interventions", "open_asks", "cost_usd")}}


def lanes() -> dict:
    """The project list: every canonical project plus the pile of tasks nobody has assigned yet; busiest first."""
    ds = Dataset()
    ev = _all_events(ds)
    out = [_lane(ds, p["key"], p["key"], p["value"], ev.get(p["key"], [])) for p in ds.projects]
    out.sort(key=lambda p: (p["last_activity"] or ""), reverse=True)
    pile = _lane(ds, UNASSIGNED, UNASSIGNED_NAME, "还没有归到任何项目的任务", ev.get(UNASSIGNED, []))
    if pile["stats"]["tasks"] or pile["last_activity"]:
        out.append(pile)
    return {"projects": out, "today": ds.today.isoformat()}


def project_timeline(key: str) -> dict | None:
    ds = Dataset()
    if key != UNASSIGNED and key not in ds.pkeys:
        return None
    ev = _all_events(ds)
    events = ev.get(key, [])[:400]
    tasks = ds.project_tasks(key)
    stats = _stats(ds, tasks, events)
    miles = _milestones(ds, key, tasks)
    days: list[dict] = []
    for e in events:
        if not days or days[-1]["day"] != e["day"]:
            days.append({"day": e["day"], "label": _day_label(e["day"], ds.today), "events": []})
        days[-1]["events"].append(e)
    proj = ds.pkeys.get(key)
    return {"key": key, "name": proj["key"] if proj else UNASSIGNED_NAME, "summary": clip(proj["value"], 300) if proj else "还没有归到任何项目的任务",
            "today": ds.today.isoformat(), "stats": stats, "progress": _progress(stats, miles), "milestones": miles, "flow": _flow(ds, tasks, events), "days": days,
            "event_count": len(events)}
