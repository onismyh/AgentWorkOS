"""Project timeline + milestones: task -> project assignment, plain-language events (agent vs human), media, flow graph, milestone CRUD."""
from __future__ import annotations

from datetime import date, timedelta

from tests.test_api import H, client, env  # noqa: F401  (fixtures)


def _project(name: str, summary: str = "测试项目") -> None:
    from awos import repo
    repo.upsert_fact("project", name, summary, source="user", actor="user", pinned=True)


def _task(title: str, **kw) -> str:
    from awos import repo
    kw.setdefault("source", "manual")
    return repo.create_task(title, **kw)


def _run(task_id: str, executor: str = "codex", *, status: str = "succeeded", purpose: str = "execute", summary: str | None = "做完了", cost: float = 0.0,
         error: str | None = None, error_class: str | None = None) -> str:
    from awos import db, repo
    rid = repo.create_run(purpose, executor, task_id=task_id)
    repo.update_run(rid, status=status, started_at=db.utcnow(), ended_at=db.utcnow(), cost_usd=cost, error=error, error_class=error_class,
                    structured={"summary": summary} if summary else None)
    return rid


def _tl(client, key: str) -> dict:
    r = client.get(f"/api/projects/{key}/timeline")
    assert r.status_code == 200, r.text
    return r.json()


def _events(tl: dict) -> list[dict]:
    return [e for d in tl["days"] for e in d["events"]]


def test_tasks_are_assigned_to_projects_by_topic_text_and_parent(client):
    from awos import db, repo
    _project("电厂改造模型")
    _project("ConfX 出访与汇报")
    _project("学位论文 thuthesis")
    a = _task("整理仓库", project="电厂改造")                                    # tasks.project text matches the canonical name
    b = _task("准备 ConfX 报销材料清单")                                         # a distinctive word of the project name appears in the title
    c = _task("子任务：核对数据", parent_id=a)                                      # subtasks follow their root
    d = _task("某个无关的杂事")                                                    # nothing matches
    e = _task("话题里的任务")
    db.insert("topic_items", {"entity_type": "task", "entity_id": e, "topic_key": "thesis", "updated_at": db.utcnow()})
    repo.link_topic_project("thesis", "学位论文 thuthesis", "user")                # the topic link wins over text
    lanes = {p["key"]: p for p in client.get("/api/timeline").json()["projects"]}
    assert lanes["电厂改造模型"]["stats"]["tasks"] == 2 and lanes["ConfX 出访与汇报"]["stats"]["tasks"] == 1
    assert lanes["学位论文 thuthesis"]["stats"]["tasks"] == 1 and lanes["__none__"]["stats"]["tasks"] == 1
    assert {n["id"] for n in _tl(client, "电厂改造模型")["flow"]["nodes"]} == {a, c}
    assert _tl(client, "__none__")["flow"]["nodes"][0]["id"] == d


def test_events_tell_what_the_agent_did_and_what_the_human_changed(client):
    from awos import repo
    from awos.engine import actions
    _project("电厂改造模型")
    t = _task("整理文件", project="电厂改造模型", level=4)
    _run(t, "codex", status="failed", summary=None, error="429 Too Many Requests", error_class="transient")
    _run(t, "claude", summary="已整理 12 个文件，附清单", cost=0.31)
    ask = repo.create_approval(title="提交整理结果", kind="action", action_type="git_commit", task_id=t,
                               options=[{"key": "approve", "label": "批准"}, {"key": "reject", "label": "拒绝"}])
    repo.decide_approval(ask, "approve", note="先别推送", via="dashboard")
    auto = repo.create_approval(title="独立提交脚本", kind="action", action_type="git_commit", task_id=t, options=[{"key": "approve", "label": "批准"}])
    repo.decide_approval(auto, "approve", via="auto", actor="system")
    repo.set_approval_status(auto, "executed", result={"done": True, "details": "ok"})
    actions.retry_task(t, note="再核对一遍")
    actions.cancel_task(_task("不要了", project="电厂改造模型"), reason="不需要了")
    tl = _tl(client, "电厂改造模型")
    ev = _events(tl)
    assert any(e["actor"] == "agent:claude" and e["outcome"] == "done" and "已整理 12 个文件" in e["detail"] and e["cost"] == 0.31 for e in ev)
    rec = next(e for e in ev if e["actor"] == "agent:codex")
    assert rec["outcome"] == "recovered" and "限流" in rec["title"]                       # the later success makes the failure a recovered one
    mine = next(e for e in ev if e["human"] and e["title"].startswith("你批准了（提交代码）"))
    assert mine["detail"] == "先别推送" and mine["refs"]["approval"] == ask
    assert any(e["actor"] == "system" and "按白板规则自动放行" in e["title"] for e in ev)
    assert any(e["human"] and e["title"].startswith("你让 Agent 重新来过") and e["detail"] == "再核对一遍" for e in ev)
    assert any(e["human"] and e["title"].startswith("你取消了任务") and e["detail"] == "不需要了" for e in ev)
    assert tl["stats"]["interventions"] >= 4 and tl["stats"]["agent_runs"] == 2 and tl["stats"]["agent_ok"] == 1
    assert tl["stats"]["executors"] == {"codex": {"runs": 1, "ok": 0}, "claude": {"runs": 1, "ok": 1}} and tl["days"][0]["label"] == "今天"
    assert all(not e["detail"].startswith("{") for e in ev)                                # excerpts, never raw JSON / logs


def test_images_ride_on_the_run_that_produced_them_and_nothing_outside_var_is_served(client, env):
    from awos import db
    _project("电厂改造模型")
    t = _task("画图", project="电厂改造模型")
    rid = _run(t, "claude")
    png = env["var"] / "workspaces" / "fig.png"
    png.parent.mkdir(parents=True, exist_ok=True)
    png.write_bytes(b"\x89PNG\r\n\x1a\n")
    doc = env["var"] / "workspaces" / "notes.md"
    doc.write_text("# n", encoding="utf-8")
    outside = env["tmp"] / "secret.png"
    outside.write_bytes(b"\x89PNG")
    for title, p, run in (("图 1", png, rid), ("说明", doc, rid), ("泄露", outside, rid)):
        db.insert("artifacts", {"id": db.new_id("art"), "task_id": t, "run_id": run, "kind": "file", "title": title, "path": str(p), "created_at": db.utcnow()})
    ev = next(e for e in _events(_tl(client, "电厂改造模型")) if e["kind"] == "run")
    assert [m["title"] for m in ev["media"]] == ["图 1"] and ev["media"][0]["image"] and ev["media"][0]["url"].startswith("/api/artifacts/art_")
    assert [f["title"] for f in ev["files"]] == ["说明"]
    assert client.get(ev["media"][0]["url"]).status_code == 200


def test_milestones_crud_effective_status_and_derived_markers(client):
    from awos import repo
    _project("电厂改造模型")
    done = _task("交付稿件", project="电厂改造模型", level=4, status="done")
    repo.db.update("tasks", "id", done, {"finished_at": "2026-11-05T10:00:00Z"})
    repo.upsert_fact("deadline", "电厂改造模型投稿", "2099-05-10 前提交修回稿", source="user", actor="user", pinned=True)
    assert client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "x"}).status_code in (401, 403)        # token protected
    assert client.post("/api/milestones", json={"project_key": "没有这个项目", "title": "x"}, headers=H).status_code == 422
    assert client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "x", "target_date": "明天"}, headers=H).status_code == 422
    past = (date.today() - timedelta(days=3)).isoformat()
    m1 = client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "初稿完成", "target_date": past, "summary": "第 1-3 章"}, headers=H).json()["id"]
    m2 = client.post("/api/milestones", json={"project_key": "电厂改造模型", "title": "投出去", "target_date": "2099-01-01"}, headers=H).json()["id"]
    tl = _tl(client, "电厂改造模型")
    by = {m["title"]: m for m in tl["milestones"]}
    assert by["初稿完成"]["status"] == "missed" and by["初稿完成"]["stored_status"] == "planned" and by["投出去"]["status"] == "planned"
    assert by["电厂改造模型投稿"]["source"] == "auto" and by["电厂改造模型投稿"]["target_date"] == "2099-05-10" and not by["电厂改造模型投稿"]["editable"]
    assert by["交付：交付稿件"]["source"] == "auto" and by["交付：交付稿件"]["status"] == "done"
    assert tl["progress"] == {**tl["progress"], "pct": 0, "basis": "milestones"}
    r = client.patch(f"/api/milestones/{m1}", json={"status": "done"}, headers=H).json()["milestone"]
    assert r["status"] == "done" and r["done_at"]
    tl = _tl(client, "电厂改造模型")
    assert tl["progress"]["pct"] == 50 and any(e["human"] and e["title"].startswith("你把里程碑标为完成") for e in _events(tl))
    lane = next(p for p in client.get("/api/timeline").json()["projects"] if p["key"] == "电厂改造模型")
    assert lane["next_milestone"]["title"] == "投出去" and lane["progress"]["milestones"] == {"done": 1, "total": 2}
    assert client.patch(f"/api/milestones/{m2}", json={"target_date": None, "title": "投稿"}, headers=H).json()["milestone"]["target_date"] is None
    assert client.patch("/api/milestones/auto:task:x", json={"title": "x"}, headers=H).status_code == 404
    assert client.delete(f"/api/milestones/{m2}", headers=H).status_code == 200 and client.delete(f"/api/milestones/{m2}", headers=H).status_code == 404


def test_flow_graph_has_dependencies_rollups_and_human_gates(client):
    from awos import repo
    _project("电厂改造模型")
    root = _task("总任务", project="电厂改造模型")
    a = _task("第一步", parent_id=root)
    b = _task("第二步", parent_id=root)
    repo.add_dependency(b, a)
    gate = repo.create_approval(title="选方案", kind="decision", task_id=a, options=[{"key": "x", "label": "方案 X"}])
    repo.decide_approval(gate, "x", via="dashboard")
    pending = repo.create_approval(title="批准写入", kind="action", task_id=b, options=[{"key": "approve", "label": "批准"}])
    flow = _tl(client, "电厂改造模型")["flow"]
    kinds = {(e["type"]) for e in flow["edges"]}
    assert {e["type"] for e in flow["edges"] if e["from"] == a and e["to"] == b} == {"dep"} and kinds == {"dep", "rollup", "gate"}
    gates = {n["id"]: n for n in flow["nodes"] if n["type"] == "gate"}
    assert gates[gate]["state"] == "decided" and gates[gate]["decision"] == "方案 X" and gates[pending]["state"] == "waiting"
    assert {n["id"] for n in flow["nodes"] if n["type"] == "task"} == {root, a, b}


def test_unknown_project_is_a_404_and_the_pile_of_unassigned_tasks_has_its_own_timeline(client):
    assert client.get("/api/projects/没有/timeline").status_code == 404
    _task("没人认领的事")
    tl = _tl(client, "__none__")
    assert tl["name"] == "未归类" and tl["stats"]["tasks"] == 1 and _events(tl)[0]["title"].startswith("你新建了任务")


# --------------------------------------------------------------------------- front end (no browser; node is used when available)

import json  # noqa: E402
import re  # noqa: E402
import shutil  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402

DASH = Path(__file__).resolve().parents[1] / "src" / "awos" / "dashboard"
NODE = shutil.which("node")


def test_timeline_mode_is_wired_into_the_board():
    imports = re.findall(r'@import url\("([^"]+)"\)', (DASH / "style.css").read_text(encoding="utf-8"))
    assert imports.index("css/timeline.css") < imports.index("css/glass.css")                      # glass overrides, so it stays last
    board = (DASH / "js" / "views" / "board.js").read_text(encoding="utf-8")
    assert "mode') === 'timeline'" in board and "modeSwitch('board')" in board and "d?.__timeline" in board
    tl = (DASH / "js" / "views" / "project-timeline.js").read_text(encoding="utf-8")
    assert "'/timeline'" in tl and "/timeline`" in tl and "'/milestones'" in tl and "role=\"button\"" in tl
    assert ".tl-main > *" in (DASH / "js" / "shell.js").read_text(encoding="utf-8")                 # joins the navigation-only entrance cascade
    css = (DASH / "css" / "timeline.css").read_text(encoding="utf-8")
    assert "@media (max-width: 900px)" in css and "prefers-reduced-motion" in css


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_flow_layout_orders_ranks_wraps_tall_columns_and_survives_cycles():
    flow = (DASH / "js" / "ui" / "flow.js").as_uri()
    code = f"""
import {{ layoutFlow, wrapText, flowSVG }} from {json.dumps(flow)};
const t = (id, extra = {{}}) => ({{ id, type: 'task', title: id, status: 'done', level: 2, ts: id, ...extra }});
const L = layoutFlow({{ nodes: [t('a'), t('b'), t('c'), {{ id: 'g', type: 'gate', title: 'ask', state: 'waiting', task: 'b', ts: 'z' }}],
  edges: [{{ from: 'a', to: 'b', type: 'dep' }}, {{ from: 'b', to: 'c', type: 'dep' }}, {{ from: 'b', to: 'g', type: 'gate' }}] }});
const x = Object.fromEntries(L.nodes.map((n) => [n.id, n.x]));
const many = layoutFlow({{ nodes: Array.from({{ length: 20 }}, (_, i) => t('n' + String(i).padStart(2, '0'))), edges: [] }});
const cyc = layoutFlow({{ nodes: [t('p'), t('q')], edges: [{{ from: 'p', to: 'q', type: 'dep' }}, {{ from: 'q', to: 'p', type: 'dep' }}] }});
const svg = String(flowSVG({{ nodes: [t('<img onerror=1>')], edges: [] }}));
console.log(JSON.stringify({{ order: x.a < x.b && x.b < x.c && x.g > x.b, paths: L.edges.every((e) => e.d.startsWith('M')),
  wrapped: new Set(many.nodes.map((n) => n.x)).size, rows: Math.max(...many.nodes.map((n) => n.y)) < 8 * 80,
  cycle: cyc.nodes.length === 2, w: wrapText('一二三四五六七八九十一二三四五六七八九十', 60, 2), safe: !svg.includes('<img') }}));
"""
    r = subprocess.run([NODE, "--input-type=module", "-e", code], capture_output=True, text=True, timeout=60, encoding="utf-8")
    assert r.returncode == 0, r.stderr[:800]
    out = json.loads(r.stdout.strip().splitlines()[-1])
    assert out["order"] and out["paths"] and out["wrapped"] == 3 and out["rows"] and out["cycle"] and out["safe"]
    assert len(out["w"]) == 2 and out["w"][1].endswith("…")
