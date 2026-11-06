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
from ..brain import context as context_mod, projects as projects_mod
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
        self.projects = projects_mod.canonical_projects()          # watched projects only: what the board lists
        self.archived = sorted(repo.list_facts(kind="project", status="archived"), key=lambda f: f["updated_at"], reverse=True)
        self.all_projects = [*self.projects, *self.archived]       # tasks keep their attribution to an archived project (history, "close what the board showed")
        self.pkeys = {p["key"]: p for p in self.all_projects}
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
        self.run_exec = {r["id"]: r["executor"] for r in self.runs}
        notes = context_mod._latest_progress(repo.list_facts(kind="note", status="active"))   # newest `进展·<project>` note per project
        self.progress = {self._match_name(name) or name: text for name, text in notes.items()}
        self.proj_of: dict[str, str | None] = {}
        self.guessed: set[str] = set()   # attributed only by matching the project name in the task's text
        for tid in self.tasks:
            self._resolve(tid)

    # ---- task -> project
    def _match_name(self, name: str | None) -> str | None:
        m = projects_mod.match_project(name, self.all_projects)
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
                if found and up in self.guessed:
                    self.guessed.add(tid)
        if not found:
            found = self._by_text(f"{t['title']} {(t.get('description') or '')[:300]}")
            if found:
                self.guessed.add(tid)
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

    def project_tasks(self, key: str, *, explicit: bool = False) -> list[dict]:
        """Tasks the board shows under `key`; explicit=True leaves out the ones only guessed from their text (the ones
        bulk actions such as archiving must not touch)."""
        want = None if key == UNASSIGNED else key
        return [t for tid, t in self.tasks.items() if self.proj_of.get(tid) == want and not (explicit and tid in self.guessed)]

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


# --------------------------------------------------------------------------- review: health, state, last, asks, highlights

# Thresholds of the portfolio review (days). Deterministic on purpose: the same rules feed the board and the guardian.
STALLED_DAYS = 5            # no activity this long while there is open work (or a deadline within DEADLINE_WINDOW_DAYS) = stalled
QUIET_DAYS = 14             # no tasks, no milestones and nothing for this long = quiet (neutral, not an alarm)
RISK_SOON_DAYS = 3          # a deadline this close, while stalled or with open asks = risk
ATTENTION_SOON_DAYS = 7     # a deadline this close = needs attention
DEADLINE_WINDOW_DAYS = 30
HEALTH_LEVELS = ("risk", "attention", "stalled", "ok", "quiet")
HEALTH_LABEL = {"risk": "有风险", "attention": "要你处理", "stalled": "停滞", "ok": "正常", "quiet": "暂无动静"}
OPEN_STATUSES = frozenset({"inbox", "triaged", "ready", "blocked", "running", "waiting_approval", "waiting_user", "review", "snoozed"})
ASK_LABEL = {"decision": "拍板", "approval": "批准", "budget": "追加预算", "review": "验收", "handback": "接手"}
STATE_MAX = 60
HIGHLIGHTS_MAX = 6


def _md(d: date) -> str:
    return f"{d.month}月{d.day}日"


def _until(today: date, iso: str | None) -> int | None:
    try:
        return (date.fromisoformat(iso) - today).days if iso else None
    except ValueError:
        return None


def health_of(today: date, *, idle_days: int | None, tasks: int, tasks_active: int, tasks_failed: int, open_asks: int, milestones: list[dict],
              pct: int = 0, pile_open: int | None = None) -> dict:
    """Where a project stands, as {level, label, reason}. Pure: callers pass numbers, `idle_days` is days since the last activity (None = never).
    milestones: dicts with title / status (effective: an undone date in the past is 'missed') / target_date (ISO or None); `pile_open` is only
    set for the unassigned pile (tasks waiting for a project)."""
    def out(level: str, reason: str) -> dict:
        return {"level": level, "label": HEALTH_LABEL[level], "reason": reason}

    if pile_open is not None:
        if pile_open:
            return out("attention", f"有 {pile_open} 个任务还没归到项目")
        return out("ok" if tasks else "quiet", "没有待归类的任务" if tasks else "暂无动静")
    open_ms = [m for m in milestones if m.get("status") != "done"]
    overdue = [m for m in open_ms if m.get("status") == "missed" or (_until(today, m.get("target_date")) or 0) < 0]
    upcoming = sorted(((n, m) for m in open_ms for n in [_until(today, m.get("target_date"))] if n is not None and n >= 0 and m.get("status") != "missed"),
                      key=lambda x: x[0])
    soon = upcoming[0] if upcoming else None
    stalled = idle_days is None or idle_days >= STALLED_DAYS
    open_work = tasks_active > 0 or open_asks > 0
    idle_txt = f"{idle_days} 天没动静" if idle_days is not None else "还没有动静"

    def due(n: int) -> str:
        return "今天截止" if n == 0 else f"{_md(today + timedelta(days=n))}截止"

    if overdue:
        m = overdue[0]
        n = _until(today, m.get("target_date"))
        return out("risk", f"「{clip(m['title'], 14)}」{f'已逾期 {-n} 天' if n is not None and n < 0 else '已逾期'}")
    if tasks_failed:
        return out("risk", f"有 {tasks_failed} 个任务没做成")
    if soon and soon[0] <= RISK_SOON_DAYS and (stalled or open_asks):
        return out("risk", f"{due(soon[0])}，{idle_txt if stalled else f'还有 {open_asks} 项等你处理'}")
    if open_asks:
        return out("attention", f"有 {open_asks} 项等你处理")
    if soon and soon[0] <= ATTENTION_SOON_DAYS:
        return out("attention", f"{due(soon[0])}，进度 {pct}%")
    near = soon is not None and soon[0] <= DEADLINE_WINDOW_DAYS
    if stalled and (open_work or near):
        return out("stalled", idle_txt if open_work or not near else f"{idle_txt}，{due(soon[0])}")
    if tasks == 0 and not milestones and (idle_days is None or idle_days >= QUIET_DAYS):
        return out("quiet", "没有任务，也没有里程碑")
    if tasks_active:
        return out("ok", f"{tasks_active} 个任务在推进")
    if idle_days is None:
        return out("ok", "一切平稳")
    return out("ok", "今天有动静" if idle_days == 0 else f"{idle_days} 天前有动静")


def _is_activity(e: dict) -> bool:
    """Anything but the system merely noticing something (a discovered task, an automatic relink) counts as the project moving."""
    return not (e["actor"] == "system" and e["kind"] in ("created", "relink"))


def _sentence(text: Any, n: int = STATE_MAX) -> str:
    s = clip(text, 400)
    for i, ch in enumerate(s):
        if ch in "。；;！!" and i >= 6:
            s = s[:i]
            break
    return clip(s.rstrip("，、 "), n)


def _tidy_note(text: str) -> str:
    """A progress note as one readable phrase: no leftover bracket from a nested project name, no time stamp, no file paths."""
    s = re.sub(r"^[）)\s]+", "", str(text or ""))
    s = re.sub(r"^\d{2}-\d{2}(?: \d{2}:\d{2})?\s*", "", s)
    s = re.sub(r"(?<![A-Za-z])[A-Za-z]:[/\\][^\s，。；、）)]*", "", s)  # drive paths, not the "s:/" of https://
    s = re.sub(r"(?<![\w.:/~])(?:~|/(?:Users|home|Volumes|private|tmp|var|opt|mnt))/[^\s，。；、）)]*", "", s)  # macOS / Linux
    return re.sub(r"\s{2,}", " ", s).strip()


def _state_line(ds: Dataset, key: str, summary: str, pile_open: int | None) -> str:
    if pile_open is not None:
        return f"有 {pile_open} 个任务还没归到任何项目" if pile_open else "没有待归类的任务"
    note = _tidy_note(ds.progress.get(key, ""))
    return _sentence(note if len(note) >= 8 else summary)


def _curate(ds: Dataset, events: list[dict]) -> list[dict]:
    """The meaningful things, newest first, one plain sentence each: results agents delivered, decisions you made, interventions that changed
    course, failures nobody recovered. The request / 'executed' / progress-update rows of the same thing collapse into one sentence."""
    appr = {a["id"]: a for a in ds.approvals}
    done_tasks = {e["refs"]["task"] for e in events if e["kind"] == "outcome" and e["outcome"] == "done"}
    accepted = {e["refs"]["task"] for e in events if e["human"] and e["kind"] == "intervene" and e["title"].startswith("你验收通过")}
    out: list[dict] = []
    seen: set[tuple] = set()
    in_flight = 0
    for e in events:
        k, o, ref = e["kind"], e["outcome"], e["refs"]
        a = appr.get(ref.get("approval") or "")
        text, detail, key, actor, fallback = None, e["detail"], None, e["actor"], False
        if e["human"] and k == "decision":
            key, text = ("apv", ref["approval"]), e["title"]
            if a and a["status"] == "executed":
                text += "，已执行"
            elif a and a["status"] == "failed":
                text, o = text + "，执行没成功", "failed"
        elif k == "action" and a and a.get("decided_via") == "auto":
            key = ("apv", ref["approval"])
            text = f"Agent 自动完成了：{e['title'].split('：', 1)[-1]}" if o == "done" else e["title"]
            ex = ds.run_exec.get(a.get("run_id"))
            actor = f"agent:{ex}" if ex else "agent"
        elif k == "outcome" and o == "done":
            key = ("task", ref["task"])
            text = f"Agent 完成了：{e['title'].split('：', 1)[-1]}" + ("，你已验收" if ref["task"] in accepted else "")
        elif (k == "outcome" or k == "run") and o == "failed":
            key, text = ("fail", ref["task"]), e["title"]
        elif e["human"] and k == "intervene":
            if not (e["title"].startswith("你验收通过") and ref["task"] in done_tasks):   # folded into "Agent 完成了：…，你已验收"
                key, text = ("ev", e["id"]), e["title"]
        elif e["human"] and (k == "created" or (k == "milestone" and o == "done")):
            key, text = ("ev", e["id"]), e["title"]
        elif k == "run" and o == "done" and detail and in_flight < 2 and ref.get("task") and ref["task"] not in done_tasks \
                and ds.tasks.get(ref["task"], {}).get("status") != "done":
            key, text, fallback = ("run", ref["task"]), f"Agent 做了一轮：{e['task_title']}", True
        if text is None or key in seen:
            continue
        seen.add(key)
        in_flight += 1 if fallback else 0
        out.append({"ts": e["ts"], "day": e["day"], "actor": actor, "human": e["human"], "text": clip(text, 120), "detail": clip(detail, 120), "outcome": o,
                    "executor": e.get("executor"), "refs": ref, "_fallback": fallback})
    return out


def _asks(ds: Dataset, tasks: list[dict]) -> list[dict]:
    """What is waiting on the user right now, newest first: pending approvals, deliveries to accept, tasks handed back."""
    ids = {t["id"] for t in tasks}
    out = []
    for a in ds.approvals:
        if a["task_id"] in ids and a["status"] == "pending":
            kind = "decision" if a["kind"] in ("decision", "review") else "budget" if a["kind"] == "budget" else "approval"
            out.append({"kind": kind, "label": ASK_LABEL[kind], "title": clip(a["title"], 90), "ts": a["created_at"], "ref": {"approval": a["id"]}})
    for t in tasks:
        if t["status"] in ("review", "waiting_user"):
            kind = "review" if t["status"] == "review" else "handback"
            out.append({"kind": kind, "label": ASK_LABEL[kind], "title": clip(t["title"], 90), "ts": t["updated_at"], "ref": {"task": t["id"]}})
    return sorted(out, key=lambda x: (x["ts"], *x["ref"].values()), reverse=True)


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
    mine = [t for t in live if t["status"] in OPEN_STATUSES and t["id"] not in ds.guessed]   # what archiving would close
    mine_ids = {t["id"] for t in mine}
    close_asks = (sum(1 for a in ds.approvals if a["task_id"] in mine_ids and a["status"] == "pending")
                  + sum(1 for t in mine if t["status"] in ("waiting_user", "review")))
    stamps = [e["ts"] for e in events if _is_activity(e)]
    return {"tasks": len(live), "tasks_done": sum(1 for t in live if t["status"] == "done"),
            "tasks_active": sum(1 for t in live if t["status"] in ("running", "ready", "blocked", "waiting_approval")),
            "tasks_failed": sum(1 for t in live if t["status"] == "failed"), "tasks_open": sum(1 for t in live if t["status"] in OPEN_STATUSES),
            "agent_runs": len(runs), "agent_ok": sum(1 for r in runs if r["status"] == "succeeded"), "executors": per_exec,
            "interventions": sum(1 for e in events if e["human"]), "open_asks": asks, "close_open": len(mine), "close_asks": close_asks,
            "cost_usd": round(sum(float(r["cost_usd"] or 0) for r in runs), 3), "first_ts": min((e["ts"] for e in events), default=None), "last_ts": max(stamps) if stamps else None}


def _progress(stats: dict, miles: list[dict]) -> dict:
    explicit = [m for m in miles if m["editable"]]
    pct_tasks = round(100 * stats["tasks_done"] / stats["tasks"]) if stats["tasks"] else 0
    pct_ms = round(100 * sum(1 for m in explicit if m["status"] == "done") / len(explicit)) if explicit else None
    return {"pct": pct_ms if pct_ms is not None else pct_tasks, "basis": "milestones" if pct_ms is not None else "tasks",
            "tasks": {"done": stats["tasks_done"], "total": stats["tasks"]},
            "milestones": {"done": sum(1 for m in explicit if m["status"] == "done"), "total": len(explicit)}}


def _next_milestone(miles: list[dict], today: date) -> dict | None:
    todo = [m for m in miles if m["status"] != "done" and m.get("target_date")]
    todo += [m for m in miles if m["status"] in ("planned", "active") and not m.get("target_date") and m["editable"]]
    m = todo[0] if todo else None
    return {"title": m["title"], "target_date": m.get("target_date"), "status": m["status"], "days": _until(today, m.get("target_date"))} if m else None


def _all_events(ds: Dataset) -> dict[str, list[dict]]:
    ev = _task_events(ds)
    for pk, lst in _side_events(ds).items():
        ev.setdefault(pk, []).extend(lst)
    for lst in ev.values():
        lst.sort(key=lambda e: (e["ts"], e["id"]), reverse=True)
    return ev


def _review(ds: Dataset, key: str, name: str, summary: str, events: list[dict]) -> dict:
    """Everything one project's row / review needs, computed once."""
    tasks = ds.project_tasks(key)
    stats = _stats(ds, tasks, events)
    miles = _milestones(ds, key, tasks)
    progress = _progress(stats, miles)
    pile_open = sum(1 for t in tasks if t["status"] in OPEN_STATUSES) if key == UNASSIGNED else None
    idle = (ds.today - date.fromisoformat(_local_day(stats["last_ts"]))).days if stats["last_ts"] else None
    health = health_of(ds.today, idle_days=idle, tasks=stats["tasks"], tasks_active=stats["tasks_active"], tasks_failed=stats["tasks_failed"],
                       open_asks=stats["open_asks"], milestones=miles, pct=progress["pct"], pile_open=pile_open)
    curated = _curate(ds, events)
    first = next((c for c in curated if not c["_fallback"]), None)
    return {"tasks": tasks, "stats": stats, "milestones": miles, "progress": progress, "health": health,
            "state": _state_line(ds, key, summary, pile_open), "asks": _asks(ds, tasks),
            "last": {"ts": first["ts"], "text": first["text"], "actor": first["actor"]} if first else None,
            "highlights": [{k: v for k, v in c.items() if k != "_fallback"} for c in curated[:HIGHLIGHTS_MAX]],
            "next": _next_milestone(miles, ds.today)}


def _lane(ds: Dataset, key: str, name: str, summary: str, events: list[dict]) -> dict:
    r = _review(ds, key, name, summary, events)
    return {"key": key, "name": name, "summary": clip(summary, 120), "progress": r["progress"], "next_milestone": r["next"], "last_activity": r["stats"]["last_ts"],
            "health": r["health"], "state": r["state"], "last": r["last"], "asks": r["asks"][:3],
            "stats": {k: r["stats"][k] for k in ("tasks", "tasks_done", "tasks_active", "tasks_open", "agent_runs", "agent_ok", "interventions", "open_asks",
                                                 "close_open", "close_asks", "cost_usd")}}


def _attention_order(lane: dict) -> tuple:
    """risk, attention, stalled, ok, quiet; inside a group the nearest deadline first, then the most recent activity; the unassigned pile last."""
    nm = lane["next_milestone"] or {}
    days = nm.get("days")
    ts = db.parse_ts(lane["last_activity"]) if lane["last_activity"] else None
    return (HEALTH_LEVELS.index(lane["health"]["level"]), 1 if lane["key"] == UNASSIGNED else 0, days if days is not None else 10 ** 6, -(ts.timestamp() if ts else 0))


def lanes() -> dict:
    """The portfolio: every canonical project (also those with no tasks) plus the pile of unassigned tasks, in the order the user should look at them."""
    ds = Dataset()
    ev = _all_events(ds)
    out = [_lane(ds, p["key"], p["key"], p["value"], ev.get(p["key"], [])) for p in ds.projects]
    pile = _lane(ds, UNASSIGNED, UNASSIGNED_NAME, "还没有归到任何项目的任务", ev.get(UNASSIGNED, []))
    if any(t["status"] in OPEN_STATUSES for t in ds.project_tasks(UNASSIGNED)):   # nothing open in the pile = nothing to review
        out.append(pile)
    out.sort(key=_attention_order)
    archived = []
    for f in ds.archived:   # no longer watched: kept as one quiet line each so it can be restored
        mine = ds.project_tasks(f["key"])
        archived.append({"key": f["key"], "name": f["key"], "summary": clip(f["value"], 120), "archived_at": f["updated_at"], "tasks": len(mine),
                         "open": sum(1 for t in mine if t["status"] in OPEN_STATUSES)})
    return {"projects": out, "archived": archived, "today": ds.today.isoformat()}


def project_timeline(key: str) -> dict | None:
    ds = Dataset()
    if key != UNASSIGNED and key not in ds.pkeys:
        return None
    ev = _all_events(ds)
    events = ev.get(key, [])
    proj = ds.pkeys.get(key)
    r = _review(ds, key, proj["key"] if proj else UNASSIGNED_NAME, proj["value"] if proj else "还没有归到任何项目的任务", events)
    shown = events[:400]
    days: list[dict] = []
    for e in shown:
        if not days or days[-1]["day"] != e["day"]:
            days.append({"day": e["day"], "label": _day_label(e["day"], ds.today), "events": []})
        days[-1]["events"].append(e)
    return {"key": key, "name": proj["key"] if proj else UNASSIGNED_NAME, "summary": clip(proj["value"], 300) if proj else "还没有归到任何项目的任务",
            "archived": proj is not None and proj.get("status") == "archived", "today": ds.today.isoformat(), "health": r["health"], "state": r["state"], "last": r["last"], "asks": r["asks"][:5], "highlights": r["highlights"],
            "stats": r["stats"], "progress": r["progress"], "milestones": r["milestones"], "flow": _flow(ds, r["tasks"], shown), "days": days,
            "event_count": len(shown)}
