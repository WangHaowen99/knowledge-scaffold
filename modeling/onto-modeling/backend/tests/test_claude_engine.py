"""Claude 引擎事件归一化测试（子代理开始/终态与归卡、上下文用量与压缩提示）。"""

import asyncio
from types import SimpleNamespace

from claude_agent_sdk import AssistantMessage, ResultMessage, SystemMessage, TextBlock

from app.services.agent_sessions import _is_visible_chat_event
from app.services.engines import claude as claude_engine
from app.services.engines.base import EngineContext
from app.services.engines.claude import (
    ClaudeEngine,
    _normalize_task_event,
    context_limits,
    normalize_message,
)


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


# ---- 上下文用量与压缩提示 ----

_USAGE = {
    "input_tokens": 1200,
    "cache_creation_input_tokens": 300,
    "cache_read_input_tokens": 40000,
    "output_tokens": 500,
}


def _assistant(usage=None, parent_tool_use_id=None):
    return AssistantMessage(
        content=[TextBlock(text="好的")],
        model="example-model",
        parent_tool_use_id=parent_tool_use_id,
        usage=usage,
    )


def _result(usage):
    return ResultMessage(
        subtype="success",
        duration_ms=10,
        duration_api_ms=10,
        is_error=False,
        num_turns=2,
        session_id="sdk-1",
        usage=usage,
    )


def test_main_agent_message_carries_single_call_context_tokens():
    event = normalize_message(_assistant(_USAGE))

    assert event["context_tokens"] == 42000


def test_subagent_and_usage_less_messages_have_no_context_tokens():
    assert "context_tokens" not in normalize_message(_assistant(_USAGE, "tool-1"))
    assert "context_tokens" not in normalize_message(_assistant(None))
    assert "context_tokens" not in normalize_message(_assistant({"input_tokens": 0}))


def test_compact_boundary_becomes_visible_compaction_event():
    message = SystemMessage(
        subtype="compact_boundary",
        data={
            "type": "system",
            "subtype": "compact_boundary",
            "compact_metadata": {"trigger": "auto", "pre_tokens": 160000, "post_tokens": 9000},
        },
    )

    event = normalize_message(message)

    assert event == {
        "type": "context_compacted",
        "trigger": "auto",
        "pre_tokens": 160000,
        "post_tokens": 9000,
    }
    assert _is_visible_chat_event(event) is True


def test_other_system_messages_are_still_dropped():
    assert normalize_message(SystemMessage(subtype="status", data={})) is None


def test_context_limits_uses_cli_threshold_without_override():
    usage = {
        "rawMaxTokens": 200000,
        "maxTokens": 200000,
        "isAutoCompactEnabled": True,
        "autoCompactThreshold": 167000,
    }

    assert context_limits(usage, {}) == (200000, 167000)


def test_context_limits_applies_pct_override_cli_does_not_report():
    """CLI 报告的阈值不含百分比覆盖：有效窗口 180000 × 60% = 108000。"""
    usage = {
        "rawMaxTokens": 200000,
        "isAutoCompactEnabled": True,
        "autoCompactThreshold": 167000,
    }

    assert context_limits(usage, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "60"}) == (200000, 108000)
    # 百分比只能让压缩提前，不会推后
    assert context_limits(usage, {"CLAUDE_AUTOCOMPACT_PCT_OVERRIDE": "99"}) == (200000, 167000)


def test_context_limits_without_auto_compact_has_no_trigger():
    usage = {"maxTokens": 200000, "isAutoCompactEnabled": False, "autoCompactThreshold": 167000}

    assert context_limits(usage, {}) == (200000, None)
    assert context_limits(None, {}) == (None, None)


class _ScriptedClient:
    def __init__(self, messages, context_usage=None):
        self._messages = messages
        self._context_usage = context_usage

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_context_usage(self):
        if self._context_usage is None:
            raise RuntimeError("unsupported")
        return self._context_usage

    async def receive_messages(self):
        for message in self._messages:
            yield message


def _engine(tmp_path, client, monkeypatch):
    monkeypatch.setattr(claude_engine, "_session_env", lambda: {})
    monkeypatch.setattr(claude_engine, "_mcp_servers", lambda project_id="": {})
    monkeypatch.setattr(claude_engine, "_permission_mode", lambda: "default")
    monkeypatch.setattr(claude_engine, "_mark_workspace_trusted", lambda cwd: None)
    monkeypatch.setattr(ClaudeEngine, "_create_client", lambda self, options: client)
    ctx = EngineContext(
        project_id="p1",
        cwd=tmp_path,
        request_permission=None,
        load_resume_token=lambda: None,
        save_resume_token=lambda token: None,
        clear_resume_token=lambda: None,
        log_error=lambda kind, text: None,
    )
    return ClaudeEngine(ctx)


async def _collect(engine):
    await engine.connect()
    await engine._limits_task
    return [event async for event in engine.events()]


def test_turn_end_reports_last_call_usage_not_cumulative_result(tmp_path, monkeypatch):
    client = _ScriptedClient(
        [
            _assistant({"input_tokens": 30000, "output_tokens": 100}),
            _assistant({"input_tokens": 45000, "output_tokens": 800}),
            _assistant({"input_tokens": 999999}, parent_tool_use_id="tool-1"),
            _result({"input_tokens": 75000, "output_tokens": 900}),
        ],
        context_usage={
            "rawMaxTokens": 200000,
            "isAutoCompactEnabled": True,
            "autoCompactThreshold": 167000,
        },
    )
    engine = _engine(tmp_path, client, monkeypatch)

    events = asyncio.run(_collect(engine))

    assert [e["type"] for e in events][-2:] == ["context_usage", "result"]
    assert events[-2] == {
        "type": "context_usage",
        "used": 45800,
        "window": 200000,
        "compact_at": 167000,
    }


def test_compaction_resets_used_and_unsupported_cli_omits_limits(tmp_path, monkeypatch):
    client = _ScriptedClient(
        [
            _assistant({"input_tokens": 150000}),
            SystemMessage(
                subtype="compact_boundary",
                data={"compact_metadata": {"trigger": "auto", "pre_tokens": 150000}},
            ),
            _result({"input_tokens": 150000}),
            _assistant({"input_tokens": 12000}),
            _result({"input_tokens": 12000}),
        ]
    )
    engine = _engine(tmp_path, client, monkeypatch)

    events = asyncio.run(_collect(engine))

    usage_events = [e for e in events if e["type"] == "context_usage"]
    # 压缩后没有新调用前不知道已用量，不发用量事件
    assert usage_events == [
        {"type": "context_usage", "used": 12000, "window": None, "compact_at": None}
    ]
    assert any(e["type"] == "context_compacted" for e in events)


def test_disconnect_cancels_pending_context_limits_task(tmp_path, monkeypatch):
    class _HangingClient(_ScriptedClient):
        async def get_context_usage(self):
            await asyncio.Event().wait()

    engine = _engine(tmp_path, _HangingClient([]), monkeypatch)

    async def scenario():
        await engine.connect()
        task = engine._limits_task
        await asyncio.sleep(0)  # 让后台任务先跑到 get_context_usage
        await engine.disconnect()
        await asyncio.gather(task, return_exceptions=True)
        return task

    task = asyncio.run(scenario())

    # 取消不能被兜底的 except Exception 吞成"取不到用量"
    assert task.cancelled()
