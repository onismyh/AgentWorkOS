"""The prompt context and profile.md show projects with their latest progress; working notes and feedback stay out of 备注."""
from __future__ import annotations

from tests.test_api import client, env  # noqa: F401  (fixtures)


def _seed():
    from awos import repo
    repo.upsert_fact("project", "学位论文 thuthesis", "博士学位论文修订", source="ctx", actor="agent")
    repo.upsert_fact("project", "电厂改造模型", "电厂改造研究", source="ctx", actor="agent")
    repo.upsert_fact("note", "进展·学位论文 thuthesis", "（学位论文 thuthesis）第 5 章图件已核对", source="task", actor="agent")
    repo.upsert_fact("note", "进展·学位论文 thuthesis·2", "（学位论文 thuthesis）更新的进展", source="task", actor="agent")
    repo.upsert_fact("note", "反馈·答复·某问题", "用户在回答「某问题」时说：这个项目不管", source="user-feedback", actor="user", pinned=True)
    repo.upsert_fact("note", "普通备注", "一条普通备注", source="user", actor="user")


def test_context_brief_nests_latest_progress_under_projects(client):
    from awos.brain.context import _latest_progress, context_brief
    from awos import repo
    _seed()
    prog = _latest_progress(repo.list_facts(kind="note"))
    assert set(prog) == {"学位论文 thuthesis"} and prog["学位论文 thuthesis"] in ("第 5 章图件已核对", "更新的进展")
    brief = context_brief()
    assert "**学位论文 thuthesis**：博士学位论文修订" in brief and "最新进展：" in brief
    assert "进展·" not in brief and "一条普通备注" in brief and "这个项目不管" in brief        # progress notes are not listed twice, feedback still is
    assert brief.count("最新进展") == 1                                                       # only the project that has progress


def test_profile_markdown_has_no_html_and_keeps_notes_clean(client):
    from awos.brain.context import render_profile
    _seed()
    text = render_profile()
    assert "<sub>" not in text and "进展·" not in text and "反馈·" not in text
    assert "- **学位论文 thuthesis**：博士学位论文修订" in text and "  - 最新进展：" in text and "一条普通备注" in text
