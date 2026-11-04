"""S-21 子任务步骤：从对话历史还原一个子任务的说明、状态与逐步明细，游标分页。"""

import pytest

from app.api import deps
from app.api.errors import ApiError
from app.routers import sessions as sessions_router
from app.services.session_host import subagents, timeline

from test_conversations import _write_history, conv_env  # noqa: F401 - 夹具

SUB = {"task_id": "call-9", "tool_use_id": "call-9", "description": "", "agent_type": "general-purpose"}


def history() -> list[dict]:
    return [
        {"type": "user_message", "text": "检查", "ts": 99.0},
        {"type": "subagent_started", **SUB, "brief": "检查合成操作\n逐条列出", "title": "检查合成操作", "ts": 100.0},
        {"type": "subagent_activity", **SUB, "step_id": "s-a", "kind": "tool", "tool": "Read",
         "step_text": "读取资料「a」", "step_count": 1, "ts": 101.0},
        {"type": "subagent_activity", **SUB, "step_id": "s-a", "kind": "tool", "tool": "Read", "status": "ok",
         "step_text": "读取资料「a」", "step_count": 1, "ts": 103.5},
        {"type": "subagent_activity", **SUB, "step_id": "call-9-say-2", "kind": "text", "text": "**读完**了",
         "ts": 104.0},
        {"type": "subagent_activity", **SUB, "step_id": "s-b", "kind": "tool", "tool": "Grep",
         "step_text": "搜索「合成」", "step_count": 2, "ts": 105.0},
        {"type": "subagent_activity", "task_id": "other", "tool_use_id": "other", "step_id": "x", "kind": "tool",
         "tool": "Read", "ts": 105.5},
    ]


def test_steps_of_rebuilds_task_and_step_durations():
    task, steps = subagents.steps_of(history(), "call-9")
    assert task == {"task_id": "call-9", "title": "检查合成操作", "brief": "检查合成操作\n逐条列出",
                    "status": "running", "started_at": timeline.iso(100.0), "ended_at": None,
                    "step_count": 2, "summary": None}
    assert [(step["id"], step["kind"], step["text"], step["state"]) for step in steps] == [
        ("s-a", "tool", "读取资料「a」", "done"), ("call-9-say-2", "text", "读完了", "done"),
        ("s-b", "tool", "搜索「合成」", "running")]
    assert steps[0]["duration_ms"] == 2500 and steps[0]["ended_at"] == timeline.iso(103.5)
    assert steps[2]["duration_ms"] is None and steps[2]["started_at"] == timeline.iso(105.0)
    finished = history() + [{"type": "subagent_finished", **SUB, "status": "failed", "summary": "子任务出错",
                             "ts": 110.0}]
    task, _ = subagents.steps_of(finished, "call-9")
    assert task["status"] == "failed" and task["summary"] == "子任务出错" and task["ended_at"] == timeline.iso(110.0)
    assert subagents.steps_of(history(), "missing") is None


def test_steps_endpoint_pages_and_reports_missing_task(conv_env, monkeypatch):  # noqa: F811
    monkeypatch.setattr(deps, "require_owner", lambda project_id, request: None)
    _write_history(conv_env.sessions_dir / "p1.jsonl", history())
    first = sessions_router.subagent_steps("p1", "default", "call-9", conv_env.request, limit=2)
    assert first["task"]["title"] == "检查合成操作" and len(first["items"]) == 2 and first["next_cursor"]
    second = sessions_router.subagent_steps("p1", "default", "call-9", conv_env.request, cursor=first["next_cursor"],
                                            limit=2)
    assert [item["id"] for item in second["items"]] == ["s-b"] and second["next_cursor"] is None
    with pytest.raises(ApiError) as missing:
        sessions_router.subagent_steps("p1", "default", "missing", conv_env.request)
    assert missing.value.status == 404 and missing.value.code == "subagent_not_found"
    with pytest.raises(ApiError):  # 游标绑定子任务
        sessions_router.subagent_steps("p1", "default", "other", conv_env.request, cursor=first["next_cursor"])
