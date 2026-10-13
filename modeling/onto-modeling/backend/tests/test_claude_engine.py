"""Claude 子代理事件归一化测试（开始/终态 + 子代理消息归卡）。"""

from types import SimpleNamespace

from app.services.engines.claude import _normalize_task_event


def test_task_started_exposes_parent_tool_identity():
    message = SimpleNamespace(
        subtype="task_started",
        task_id="task-1",
        description="分析资料",
        tool_use_id="tool-1",
        task_type="general-purpose",
    )

    assert _normalize_task_event(message) == {
        "type": "subagent_started",
        "task_id": "task-1",
        "tool_use_id": "tool-1",
        "description": "分析资料",
        "agent_type": "general-purpose",
    }


def test_task_progress_heartbeat_is_dropped():
    """周期心跳不产出事件：实时过程由子代理消息提供，历史不该被心跳刷爆。"""
    message = SimpleNamespace(
        subtype="task_progress",
        task_id="task-1",
        description="分析资料",
        tool_use_id="tool-1",
        usage={"total_tokens": 1200, "tool_uses": 4, "duration_ms": 8200},
        last_tool_name="Read",
    )

    assert _normalize_task_event(message) is None


def test_task_started_supports_generic_system_message_fallback():
    message = SimpleNamespace(
        subtype="task_started",
        data={
            "task_id": "task-1",
            "description": "分析资料",
            "tool_use_id": "tool-1",
            "task_type": "general-purpose",
        },
    )

    event = _normalize_task_event(message)

    assert event["type"] == "subagent_started"
    assert event["tool_use_id"] == "tool-1"
    assert event["agent_type"] == "general-purpose"


def test_task_notification_maps_terminal_status_and_keeps_only_duration():
    message = SimpleNamespace(
        subtype="task_notification",
        task_id="task-1",
        description="分析资料",
        tool_use_id="tool-1",
        status="failed",
        summary="子任务失败",
        usage={"tool_uses": 2, "total_tokens": 900, "duration_ms": 63000},
    )

    assert _normalize_task_event(message) == {
        "type": "subagent_finished",
        "task_id": "task-1",
        "tool_use_id": "tool-1",
        "description": "分析资料",
        "status": "failed",
        "summary": "子任务失败",
        "usage": {"duration_ms": 63000},
    }


def test_task_updated_terminal_status_uses_patch_fallback():
    message = SimpleNamespace(
        subtype="task_updated",
        data={"task_id": "task-1"},
        task_id="task-1",
        patch={"status": "killed", "summary": "已停止", "usage": {"duration_ms": 900}},
        status="killed",
        tool_use_id="tool-1",
    )

    event = _normalize_task_event(message)

    assert event["type"] == "subagent_finished"
    assert event["status"] == "killed"
    assert event["summary"] == "已停止"
    assert event["usage"] == {"duration_ms": 900}


def test_task_updated_non_terminal_status_is_dropped():
    message = SimpleNamespace(
        subtype="task_updated",
        task_id="task-1",
        patch={"status": "running"},
        status="running",
        tool_use_id="tool-1",
    )

    assert _normalize_task_event(message) is None


def test_non_task_system_message_is_ignored():
    assert _normalize_task_event(SimpleNamespace(subtype="session_start")) is None
