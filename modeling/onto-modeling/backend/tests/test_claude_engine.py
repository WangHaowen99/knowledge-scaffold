# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Claude 引擎事件归一化测试（子代理开始/终态与归卡、上下文用量与压缩提示）。"""

import asyncio
from pathlib import Path
from types import SimpleNamespace

from claude_agent_sdk import (
    AssistantMessage,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
)

from app import config
from app.services.agent_sessions import _is_visible_chat_event
from app.services.engines import claude as claude_engine
from app.services.engines.base import EngineContext, FRIENDLY_ERROR
from app.services.engines.claude import (
    ClaudeEngine,
    _normalize_task_event,
    context_limits,
    normalize_message,
)


class FakeClient:
    def __init__(self, messages):
        self.messages = messages

    async def receive_messages(self):
        for message in self.messages:
            yield message


def make_engine(tmp_path: Path, messages, logged):
    ctx = EngineContext(
        project_id="synthetic",
        cwd=tmp_path,
        request_permission=lambda *args: None,
        load_resume_token=lambda: None,
        save_resume_token=lambda token: None,
        clear_resume_token=lambda: None,
        log_error=lambda *args: logged.append(args),
    )
    engine = ClaudeEngine(ctx)
    engine.client = FakeClient(messages)
    return engine


def result(is_error=False, errors=None, subtype="success"):
    return ResultMessage(
        subtype=subtype,
        duration_ms=1,
        duration_api_ms=1,
        is_error=is_error,
        num_turns=1,
        session_id="synthetic-session",
        errors=errors,
    )


def assistant(text):
    return AssistantMessage(
        content=[TextBlock(text=text)],
        model="synthetic-model",
        session_id="synthetic-session",
    )


def _without_exit(events):
    """去掉引擎给会话层的内部信号：脚本消息放完流就结束，会多发一条 exited；
    不转发的消息会产出 _activity。这些用例只看可展示的事件。"""
    return [
        event
        for event in events
        if event.get("reason") != "exited" and event.get("type") != "_activity"
    ]


def collect(engine):
    async def run():
        return _without_exit([event async for event in engine.events()])

    return asyncio.run(run())


def test_keyword_in_successful_answer_is_preserved(tmp_path):
    text = "The platform uses OAuth and Claude Code; exit code 0 means success."
    logged = []
    events = collect(make_engine(tmp_path, [assistant(text), result()], logged))

    assert events[0]["blocks"][0]["text"] == text
    assert events[1]["is_error"] is False
    assert logged == []


def test_failed_result_classifies_legacy_error_text(tmp_path):
    text = "API Error: authentication_error"
    logged = []
    events = collect(
        make_engine(
            tmp_path,
            [assistant(text), result(True, ["authentication_error"])],
            logged,
        )
    )

    assert "模型网关未配置" in events[0]["blocks"][0]["text"]
    assert logged == [("assistant_text", text)]


def test_stream_is_not_changed_before_success_result(tmp_path):
    text = "Normal answer mentioning API Error as an example."
    stream = StreamEvent(
        uuid="synthetic-event",
        session_id="synthetic-session",
        event={
            "type": "content_block_delta",
            "delta": {"type": "text_delta", "text": text},
        },
    )
    events = collect(make_engine(tmp_path, [stream, assistant(text), result()], []))

    assert events[0]["text"] == text
    assert events[1]["blocks"][0]["text"] == text


def test_assistant_structured_error_is_classified(tmp_path):
    text = "The request could not be completed."
    logged = []
    message = AssistantMessage(
        content=[TextBlock(text=text)],
        model="synthetic-model",
        error="authentication_failed",
        session_id="synthetic-session",
    )
    events = collect(make_engine(tmp_path, [message], logged))

    assert "模型网关未配置" in events[0]["blocks"][0]["text"]
    assert logged == [("assistant_error", text)]


def test_failed_result_replaces_even_without_keyword(tmp_path):
    text = "The request stopped before the answer was complete."
    logged = []
    events = collect(make_engine(tmp_path, [assistant(text), result(True)], logged))

    assert events[0]["blocks"][0]["text"] == FRIENDLY_ERROR
    assert logged == [("result_error", text)]




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
        self.usage_queries = 0

    async def connect(self):
        pass

    async def disconnect(self):
        pass

    async def get_context_usage(self):
        self.usage_queries += 1
        if self._context_usage is None:
            raise RuntimeError("unsupported")
        return self._context_usage

    async def receive_messages(self):
        for message in self._messages:
            yield message
            # 让首轮结束后发起的后台查询在下一条消息前跑完
            await asyncio.sleep(0)


# 网关后接非 Claude 模型：CLI 按环境变量定窗口（实测触发点 147000；用量环的窗口取模型窗口 200000）
_CUSTOM_MODEL_ENV = {
    "ANTHROPIC_MODEL": "gateway-model-x",
    "CLAUDE_CODE_MAX_CONTEXT_TOKENS": "200000",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW": "180000",
    "CLAUDE_CODE_MAX_OUTPUT_TOKENS": "32000",
}


def _engine(tmp_path, client, monkeypatch, env=None):
    monkeypatch.setattr(claude_engine, "_session_env", lambda: dict(env or {}))
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
    events = _without_exit([event async for event in engine.events()])
    if engine._limits_task is not None:
        await engine._limits_task
    return events


def test_stream_end_reports_engine_exit(tmp_path):
    """CLI 以退出码 0 结束时消息流正常结束、不抛异常：不发终止信号，这一轮会永远停在运行中。"""
    engine = make_engine(tmp_path, [assistant("做到一半")], [])

    async def run():
        return [event async for event in engine.events()]

    events = asyncio.run(run())

    # 待定文字先放出来，不随退出丢失
    assert events[0]["type"] == "assistant_message"
    assert events[-1] == {"type": "_engine_end", "reason": "exited", "error": FRIENDLY_ERROR}
    assert engine.client is None


def test_dropped_messages_still_report_activity(tmp_path):
    """不转发的消息（子代理增量、状态等）也说明运行时还在动：要报活动，免得被判长时间无响应。"""
    sub_delta = StreamEvent(
        uuid="u1",
        session_id="synthetic-session",
        event={"type": "content_block_delta", "delta": {"type": "text_delta", "text": "子"}},
        parent_tool_use_id="tool-1",
    )
    engine = make_engine(tmp_path, [sub_delta, SystemMessage(subtype="status", data={})], [])

    async def run():
        return [event async for event in engine.events()]

    events = asyncio.run(run())

    assert [event["type"] for event in events] == ["_activity", "_activity", "_engine_end"]


def test_turn_end_reports_last_call_usage_not_cumulative_result(tmp_path, monkeypatch):
    client = _ScriptedClient(
        [
            _assistant({"input_tokens": 30000, "output_tokens": 100}),
            _assistant({"input_tokens": 45000, "output_tokens": 800}),
            _assistant({"input_tokens": 999999}, parent_tool_use_id="tool-1"),
            _result({"input_tokens": 75000, "output_tokens": 900}),
        ],
    )
    engine = _engine(tmp_path, client, monkeypatch, env=_CUSTOM_MODEL_ENV)

    events = asyncio.run(_collect(engine))

    assert [e["type"] for e in events][-2:] == ["context_usage", "result"]
    assert events[-2] == {
        "type": "context_usage",
        "used": 45800,
        "window": 200000,
        "compact_at": 147000,
    }


def test_custom_model_limits_come_from_env_without_querying_cli(tmp_path, monkeypatch):
    """向 CLI 查用量会让它先对网关逐类计数 token，网关不支持时首条消息要空等几分钟。"""
    client = _ScriptedClient([_assistant({"input_tokens": 1000}), _result({})])
    engine = _engine(tmp_path, client, monkeypatch, env=_CUSTOM_MODEL_ENV)

    asyncio.run(_collect(engine))

    assert client.usage_queries == 0
    assert (engine._context_window, engine._compact_at) == (200000, 147000)


def test_claude_model_queries_limits_only_after_first_turn(tmp_path, monkeypatch):
    """Claude 官方模型名按 CLI 内置窗口，本地不知道：首轮不挡，结束后再查。"""
    client = _ScriptedClient(
        [
            _assistant({"input_tokens": 1000}),
            _result({}),
            _assistant({"input_tokens": 2000}),
            _result({}),
        ],
        context_usage={
            "rawMaxTokens": 200000,
            "isAutoCompactEnabled": True,
            "autoCompactThreshold": 167000,
        },
    )
    engine = _engine(tmp_path, client, monkeypatch, env={"ANTHROPIC_MODEL": "claude-sonnet-4-5"})

    async def scenario():
        await engine.connect()
        assert engine._limits_task is None
        events = []
        async for event in engine.events():
            events.append(event)
            if event["type"] == "context_usage" and engine._limits_task is not None:
                # The production lookup runs in the background. Wait for the
                # scripted lookup before expecting its value in a later turn.
                await engine._limits_task
        return events

    events = asyncio.run(scenario())

    usage_events = [e for e in events if e["type"] == "context_usage"]
    assert usage_events[0]["window"] is None
    assert usage_events[1]["window"] == 200000
    assert client.usage_queries == 1


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
        engine._load_limits_after_turn()  # 首轮结束后发起的查询
        task = engine._limits_task
        await asyncio.sleep(0)  # 让后台任务先跑到 get_context_usage
        await engine.disconnect()
        await asyncio.gather(task, return_exceptions=True)
        return task

    task = asyncio.run(scenario())

    # 取消不能被兜底的 except Exception 吞成"取不到用量"
    assert task.cancelled()


def test_session_env_disables_cli_background_tasks(tmp_path, monkeypatch):
    """CLI 默认后台派遣子代理，平台以主轮结束判定空闲：子代理必须同步执行。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    assert claude_engine._session_env()["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_cli_options_carry_background_tasks_switch(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(claude_engine, "_mcp_servers", lambda project_id="": {})
    monkeypatch.setattr(claude_engine, "_permission_mode", lambda: "default")
    ctx = EngineContext(
        project_id="p1",
        cwd=tmp_path,
        request_permission=None,
        load_resume_token=lambda: None,
        save_resume_token=lambda token: None,
        clear_resume_token=lambda: None,
        log_error=lambda kind, text: None,
    )

    options = ClaudeEngine(ctx)._build_options(None)

    assert options.env["CLAUDE_CODE_DISABLE_BACKGROUND_TASKS"] == "1"


def test_session_env_asks_cli_to_report_session_state(tmp_path, monkeypatch):
    """会话层以 CLI 自报的 idle 判定空闲；旧版 CLI 须显式要求后台工作期间保持 running。"""
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)

    env = claude_engine._session_env()

    assert env["CLAUDE_CODE_EMIT_SESSION_STATE_EVENTS"] == "1"
    assert env["CLAUDE_CODE_BG_TASKS_REPORT_RUNNING"] == "1"


def test_cli_session_state_becomes_internal_event():
    message = SystemMessage(subtype="session_state_changed", data={"state": "idle"})

    assert normalize_message(message) == {"type": "_cli_state", "state": "idle"}


def test_cli_state_before_failed_result_keeps_text_pending(tmp_path):
    """状态事件可能先于 result 到达：待定文字仍要等 result 判定成败后再放出。"""
    text = "The request stopped before the answer was complete."
    state = SystemMessage(subtype="session_state_changed", data={"state": "idle"})
    logged = []

    events = collect(make_engine(tmp_path, [assistant(text), state, result(True)], logged))

    assert [event["type"] for event in events] == ["_cli_state", "assistant_message", "result"]
    assert events[1]["blocks"][0]["text"] == FRIENDLY_ERROR
