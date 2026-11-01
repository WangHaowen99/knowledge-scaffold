"""启动恢复注册表（详设 8.3 措施 1；方案 B8；契约 O-35）：各模块登记的步骤按顺序跑、留存报告，
后台步骤做完补进报告；单步出错不挡其余；对话历史在启动时修到终态。测试数据全部为合成内容。"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app import config
from app.governance import startup_recovery
from app.services import agent_sessions, conversation_store, startup_report
from app.services.session_host import recovery, timeline
from app.services.workspace import WorkspaceManager


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))


@pytest.fixture()
def extra_steps():
    added: list[str] = []

    def add(step_id, *args, **kwargs):
        startup_recovery.register(step_id, *args, **kwargs)
        added.append(step_id)

    yield add
    for step_id in added:
        startup_recovery.unregister(step_id)


def test_kernel_steps_are_registered_in_order():
    ids = [step.id for step in startup_recovery.steps()]
    expected = ["index_lock", "write_intents", "uncommitted", "state_tracking", "single_owner", "pending_uploads",
                "eval_retirement", "assembly", "history_repair", "interrupted_execs",
                "dependencies", "purge"]
    assert [step_id for step_id in ids if step_id in expected] == expected
    background = {step.id for step in startup_recovery.steps() if step.background}
    assert background == {"dependencies", "purge"}


def test_registered_steps_run_in_order_and_failures_do_not_stop_others(manager, extra_steps):
    calls = []
    extra_steps("zz_quiet", "只做事", lambda m: calls.append("quiet"), order=95)
    extra_steps("zz_broken", "会出错", lambda m: 1 / 0, order=96)
    extra_steps("zz_item", "进报告", lambda m: {"result": "ok", "tone": "ok", "detail": "合成步骤"}, order=97)
    report = startup_recovery.run(manager, include_background=False)
    items = {item["id"]: item for item in report["items"]}
    assert calls == ["quiet"] and "zz_quiet" not in items
    assert items["zz_broken"]["result"] == "failed" and "ZeroDivisionError" in items["zz_broken"]["detail"]
    assert items["zz_item"] == {"id": "zz_item", "title": "进报告", "result": "ok", "tone": "ok", "detail": "合成步骤"}
    ids = [item["id"] for item in report["items"]]
    assert ids.index("zz_broken") < ids.index("zz_item") and "purge" not in ids  # 后台步骤不在前台跑
    assert startup_report.latest()["items"] == report["items"]


def test_background_steps_are_added_to_report_when_done(manager):
    startup_recovery.run(manager, include_background=False)
    done = asyncio.run(startup_recovery.run_background(manager))
    assert [item["id"] for item in done] == ["dependencies", "purge"]
    ids = [item["id"] for item in startup_report.latest()["items"]]
    assert ids[-2:] == ["dependencies", "purge"]
    # 再跑一次后台步骤：同 ID 替换，不重复
    asyncio.run(startup_recovery.run_background(manager))
    assert [item["id"] for item in startup_report.latest()["items"]].count("purge") == 1


def test_history_repair_closes_running_turns_and_orphan_cards(manager):
    project_id = manager.create_project("历史修复")["id"]
    for record in (
        {"type": "user_message", "text": "合成消息"},
        {"type": "session_state", "state": "running"},
        {"type": "permission_request", "request_id": "r1", "tool_name": "Bash", "tool_input": {}},
        {"type": "question_asked", "payload": {"question_id": "q1", "title": "合成问题"}},
    ):
        agent_sessions.append_history(project_id, conversation_store.DEFAULT_ID, record)
    item = recovery.repair_histories(manager)
    assert item["id"] == "history_repair" and item["result"] == "cleared" and item["projects"][0]["count"] == 3
    envelopes = [timeline.project(event, project_id, "default") for event in agent_sessions.load_history(project_id)]
    kinds = [envelope["type"] for envelope in envelopes if envelope]
    assert kinds[-4:] == ["session.interrupted", "session.state", "permission.resolved", "question.withdrawn"]
    assert envelopes[-4]["data"]["by"] == "restart"
    assert recovery.repair_histories(manager)["result"] == "ok"  # 修过一次就停在终态


def test_quiet_migrations_still_run_through_registry(manager, monkeypatch):
    called = []
    monkeypatch.setattr(manager, "migrate_state_tracking", lambda: called.append("state_tracking"))
    monkeypatch.setattr(manager, "commit_pending_uploads", lambda: called.append("pending_uploads"))
    startup_recovery.run(manager, include_background=False)
    assert called == ["state_tracking", "pending_uploads"]
