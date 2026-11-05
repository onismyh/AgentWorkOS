"""Morning / evening digest: what needs the human, what got done, what it cost."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from .. import repo
from ..config import load_config
from ..db import q
from ..timeutil import local_now


def build_digest() -> tuple[str, str]:
    since = (datetime.now(timezone.utc) - timedelta(hours=14)).strftime("%Y-%m-%dT%H:%M:%SZ")
    pending = repo.list_approvals(status="pending", limit=50)
    review = repo.list_tasks(status="review", limit=50)
    handoff = repo.list_tasks(status="waiting_user", limit=50)
    suggested = repo.list_tasks(status="triaged", root_only=True, limit=50)
    failed = repo.list_tasks(status="failed", limit=20)
    running = repo.list_tasks(status=["running", "ready", "blocked"], limit=50)
    done = q("SELECT id, title, summary FROM tasks WHERE status = 'done' AND finished_at >= ? ORDER BY finished_at DESC LIMIT 20", (since,))
    cost = repo.cost_today()
    now = local_now().strftime("%Y-%m-%d %H:%M")

    def sec(title: str, rows: list[dict], fmt) -> list[str]:
        return [f"## {title}（{len(rows)}）"] + [fmt(r) for r in rows[:10]] + [""] if rows else []

    md = [f"# AgentWorkOS 摘要 · {now}", "",
          f"待你确认 **{len(pending)}** · 待验收 **{len(review)}** · 待接手 **{len(handoff)}** · 建议 **{len(suggested)}** · "
          f"进行中 **{len(running)}** · 近期完成 **{len(done)}** · 今日花费 **${float(cost['usd']):.2f}**", ""]
    md += sec("需要你确认（Outbox）", pending, lambda a: f"- [{a['risk']}] {a['title']} — 建议：{(a.get('recommendation') or '-')[:60]}")
    md += sec("交付物待验收", review, lambda t: f"- L{t['level']} {t['title']}")
    md += sec("AI 已整理好、等你接手", handoff, lambda t: f"- {t['title']}：{(t.get('status_reason') or '')[:60]}")
    md += sec("AI 发现的新事项（待你决定要不要做）", suggested, lambda t: f"- P{t['priority']} L{t['level']} {t['title']}")
    md += sec("失败", failed, lambda t: f"- {t['title']}：{(t.get('status_reason') or '')[:80]}")
    md += sec("近期完成", done, lambda t: f"- {t['title']}")
    text = "\n".join(md)
    short = (f"待确认 {len(pending)} · 待验收 {len(review)} · 待接手 {len(handoff)} · 新建议 {len(suggested)} · "
             f"完成 {len(done)}")
    if pending:
        short += f"\n首要：{pending[0]['title'][:50]}"
    return text, short


def send_digest(label: str = "") -> str:
    text, short = build_digest()
    reports = load_config().path("var") / "reports"
    reports.mkdir(exist_ok=True)
    path = reports / f"digest-{local_now().strftime('%Y%m%d-%H%M')}.md"
    path.write_text(text, encoding="utf-8")
    repo.add_artifact(f"摘要 {local_now().strftime('%m-%d %H:%M')}", kind="report", path=path)
    try:
        from ..hitl.notify import dashboard_url, notify
        notify(f"AgentWorkOS 摘要 {label}".strip(), short, level="info", ref_type="digest", ref_id=path.name, url=dashboard_url("inbox"))
    except Exception:
        pass
    return str(path)
