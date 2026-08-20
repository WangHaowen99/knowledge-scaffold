"""Phase-0 contract tests for the A/B async Runtime boundary."""

from __future__ import annotations

import asyncio

import pytest
from pydantic import ValidationError

from agent.runtime import (
    PublicEvent,
    PublicEventAdapter,
    PublicEventDraft,
    RuntimeErrorInfo,
    RuntimeEvent,
    RuntimeHistoryMessage,
    RuntimeInvocationContext,
    RuntimeResult,
)
from agent.runtime.mock import FixedAsyncAgentRuntime, ListRuntimeEventSink


def _context(run_id: str = "run-01", **arguments) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session-01",
        turn_id=1,
        workspace_id="workspace-01",
        username="alice",
        query="请执行阶段 0 测试",
        agent_mode="thinking",
        llm_request_mode="stream",
        arguments=arguments or None,
    )


def test_invocation_context_is_strict_immutable_and_normalized():
    context = _context()

    assert context.model_profile_id is None
    assert context.timeout_seconds is None
    with pytest.raises(ValidationError):
        RuntimeInvocationContext.model_validate({**context.model_dump(), "unexpected": True})
    with pytest.raises(ValidationError):
        RuntimeInvocationContext.model_validate({**context.model_dump(), "query": "  "})
    with pytest.raises(ValidationError):
        RuntimeInvocationContext.model_validate(
            {**context.model_dump(), "llm_request_mode": "async"}
        )
    with pytest.raises(ValidationError):
        context.query = "changed"


def test_history_message_contract_is_provider_neutral_and_role_safe():
    context = RuntimeInvocationContext(
        **{
            **_context().model_dump(),
            "history_messages": [
                {"role": "user", "content": "第一轮问题"},
                {
                    "role": "assistant",
                    "content": "",
                    "reasoning_content": "需要查询工具",
                    "tool_calls": [
                        {
                            "id": "call-01",
                            "type": "function",
                            "function": {"name": "lookup", "arguments": "{}"},
                        }
                    ],
                },
                {
                    "role": "tool",
                    "content": "查询结果",
                    "tool_call_id": "call-01",
                    "name": "lookup",
                },
            ],
        }
    )

    assert [message.role for message in context.history_messages] == [
        "user",
        "assistant",
        "tool",
    ]
    assert context.history_messages[1].reasoning_content == "需要查询工具"
    assert context.history_messages[2].tool_call_id == "call-01"
    assert isinstance(context.history_messages[0], RuntimeHistoryMessage)

    with pytest.raises(ValidationError, match="requires tool_call_id"):
        RuntimeHistoryMessage(role="tool", content="orphan tool result")
    with pytest.raises(ValidationError, match="cannot contain tool or reasoning"):
        RuntimeHistoryMessage(
            role="user",
            content="invalid",
            reasoning_content="private",
        )
    with pytest.raises(ValidationError):
        RuntimeHistoryMessage.model_validate(
            {"role": "system", "content": "untrusted system prompt"}
        )


def test_runtime_result_enforces_terminal_error_shape():
    error = RuntimeErrorInfo(
        error_code="MODEL_TIMEOUT",
        message="模型调用超时",
        retriable=True,
        source="model",
    )

    assert RuntimeResult(status="completed", content="ok").error is None
    assert RuntimeResult(status="failed", error=error).error == error
    assert RuntimeResult(status="cancelled", error=error).status == "cancelled"
    with pytest.raises(ValidationError):
        RuntimeResult(status="completed", error=error)
    with pytest.raises(ValidationError):
        RuntimeResult(status="failed")


def test_draft_has_no_public_identity_but_public_event_requires_it():
    draft = PublicEventDraft(
        run_id="run-01",
        event_type="token.delta",
        status="running",
        payload={"text": "hello"},
    )

    assert draft.event_id is None
    assert draft.seq is None
    with pytest.raises(ValidationError):
        PublicEventDraft.model_validate({**draft.model_dump(), "seq": 1})
    with pytest.raises(ValidationError):
        PublicEvent(
            event_id="event-01",
            run_id="run-01",
            seq=0,
            event_type="token.delta",
            status="running",
        )

    public = PublicEvent(
        event_id="event-01",
        run_id="run-01",
        seq=1,
        event_type="token.delta",
        status="running",
        payload=draft.payload,
    )
    assert public.seq == 1


def test_turn_finished_requires_one_consistent_terminal_status():
    event = RuntimeEvent(
        event_type="turn.finished",
        status="completed",
        run_id="run-01",
        payload={"status": "success", "content": "ok"},
    )
    assert event.status == "completed"

    with pytest.raises(ValidationError, match="terminal status"):
        RuntimeEvent(
            event_type="turn.finished",
            status="running",
            run_id="run-01",
            payload={"status": "completed"},
        )
    with pytest.raises(ValidationError, match="must match"):
        RuntimeEvent(
            event_type="turn.finished",
            status="failed",
            run_id="run-01",
            payload={"status": "cancelled"},
        )
    with pytest.raises(ValidationError, match="must match"):
        PublicEventDraft(
            event_type="turn.finished",
            status="completed",
            run_id="run-01",
            payload={"status": "failed"},
        )


def test_public_adapter_redacts_secrets_without_redacting_usage_tokens():
    event = RuntimeEvent(
        event_type="turn.finished",
        status="failed",
        run_id="run-01",
        payload={
            "status": "failed",
            "usage": {"input_tokens": 12, "output_tokens": 7, "total_tokens": 19},
            "error": {
                "error_code": "TOOL_FAILED",
                "message": "Authorization: Bearer top-secret",
                "source": "tool",
                "details": {
                    "access_token": "secret-token",
                    "model_api_key": "secret-key",
                    "refreshToken": "secret-refresh-token",
                    "password_hint": "not a credential field",
                },
            },
        },
    )

    draft = PublicEventAdapter().adapt(event)

    assert draft is not None
    assert draft.status == "failed"
    assert draft.payload["usage"] == {
        "input_tokens": 12,
        "output_tokens": 7,
        "total_tokens": 19,
    }
    details = draft.payload["error"]["details"]
    assert details["access_token"] == "[REDACTED]"
    assert details["model_api_key"] == "[REDACTED]"
    assert details["refreshToken"] == "[REDACTED]"
    assert details["password_hint"] == "not a credential field"
    assert "top-secret" not in draft.payload["error"]["message"]
    assert draft.payload["omitted"]["sensitive_fields_redacted"] == 3


def test_public_adapter_uses_tool_whitelist_and_marks_truncation():
    event = RuntimeEvent(
        event_type="tool.started",
        status="running",
        run_id="run-01",
        source={"node": "react", "component": "tool", "internal_task_id": "task-1"},
        payload={
            "tool_name": "lookup",
            "tool_call_id": "call-01",
            "iteration": 2,
            "arguments": {"password": "secret"},
            "request_headers": {"Authorization": "Bearer secret"},
        },
    )
    draft = PublicEventAdapter(max_text_length=32).adapt(event)

    assert draft is not None
    assert draft.payload == {
        "name": "lookup",
        "tool_call_id": "call-01",
        "iteration": 2,
    }
    assert draft.source == {"node": "react", "component": "tool"}

    delta = RuntimeEvent(
        event_type="token.delta",
        status="running",
        run_id="run-01",
        payload={"text": "x" * 40},
    )
    truncated = PublicEventAdapter(max_text_length=32).adapt(delta)
    assert truncated is not None
    assert truncated.payload["text"] == "x" * 32 + "..."
    assert truncated.payload["truncated"] is True
    assert truncated.payload["omitted"]["text_fields_truncated"] == 1


def test_reasoning_and_terminal_trace_follow_include_trace_switch():
    reasoning = RuntimeEvent(
        event_type="reasoning.delta",
        status="running",
        run_id="run-01",
        payload={"text": "internal reasoning"},
    )
    terminal = RuntimeEvent(
        event_type="turn.finished",
        status="completed",
        run_id="run-01",
        payload={
            "status": "completed",
            "content": "answer",
            "trace": {"prompt": "secret prompt", "step": "model"},
        },
    )

    assert PublicEventAdapter(include_trace=False).adapt(reasoning) is None
    hidden = PublicEventAdapter(include_trace=False).adapt(terminal)
    assert hidden is not None
    assert "trace" not in hidden.payload

    visible_reasoning = PublicEventAdapter(include_trace=True).adapt(reasoning)
    visible_terminal = PublicEventAdapter(include_trace=True).adapt(terminal)
    assert visible_reasoning is not None
    assert visible_reasoning.payload["text"] == "internal reasoning"
    assert visible_terminal is not None
    assert visible_terminal.payload["trace"] == {
        "prompt": "[REDACTED]",
        "step": "model",
    }
    assert visible_terminal.payload["omitted"]["sensitive_fields_redacted"] == 1


def test_public_adapter_preserves_interaction_prompt_but_not_internal_prompts():
    interaction = {
        "id": "interaction_synthetic",
        "type": "input",
        "prompt": "Provide a synthetic region code",
        "options": [],
    }
    requested = RuntimeEvent(
        event_type="interaction.requested",
        status="awaiting_input",
        run_id="run-interaction",
        payload={
            "interaction_id": interaction["id"],
            "type": interaction["type"],
            "prompt": interaction["prompt"],
            "options": [],
        },
    )
    terminal = RuntimeEvent(
        event_type="turn.finished",
        status="awaiting_input",
        run_id="run-interaction",
        payload={
            "status": "awaiting_input",
            "structured_content": {
                "type": "user_interaction",
                "text": interaction["prompt"],
                "interaction": interaction,
            },
            "trace": {"prompt": "internal model prompt"},
        },
    )

    requested_draft = PublicEventAdapter().adapt(requested)
    terminal_draft = PublicEventAdapter(include_trace=True).adapt(terminal)

    assert requested_draft is not None
    assert requested_draft.payload["prompt"] == interaction["prompt"]
    assert terminal_draft is not None
    assert (
        terminal_draft.payload["structured_content"]["interaction"]["prompt"]
        == interaction["prompt"]
    )
    assert terminal_draft.payload["trace"]["prompt"] == "[REDACTED]"


def test_fixed_runtime_success_and_tool_failure_flows():
    async def exercise():
        runtime = FixedAsyncAgentRuntime()
        success_sink = ListRuntimeEventSink()
        success = await runtime.arun(_context("run-success"), success_sink)
        failure_sink = ListRuntimeEventSink()
        failure = await runtime.arun(
            _context("run-failure", mock_scenario="tool_failure"),
            failure_sink,
        )
        return runtime, success, success_sink.events, failure, failure_sink.events

    runtime, success, success_events, failure, failure_events = asyncio.run(exercise())

    assert success.status == "completed"
    assert [event.event_type for event in success_events] == [
        "turn.started",
        "model.started",
        "tool.started",
        "tool.finished",
        "token.delta",
        "turn.finished",
    ]
    assert failure.status == "failed"
    assert failure.error is not None
    assert failure.error.error_code == "TOOL_EXECUTION_FAILED"
    assert [event.event_type for event in failure_events][-2:] == [
        "tool.finished",
        "turn.finished",
    ]
    assert asyncio.run(runtime.cancel("run-success", "too late")) is False


def test_fixed_runtime_hitl_resume_cancel_and_release():
    async def exercise_resume():
        runtime = FixedAsyncAgentRuntime()
        sink = ListRuntimeEventSink()
        task = asyncio.create_task(
            runtime.arun(
                _context("run-hitl", mock_scenario="awaiting_input"),
                sink,
            )
        )
        for _ in range(100):
            if any(event.event_type == "interaction.requested" for event in sink.events):
                break
            await asyncio.sleep(0)
        interaction = next(
            event for event in sink.events if event.event_type == "interaction.requested"
        )
        resumed = await runtime.resume_interaction(
            "run-hitl",
            interaction.payload["interaction_id"],
            "continue",
        )
        result = await task
        await runtime.release("jiuwen-session-01")
        return runtime, resumed, result, sink.events

    runtime, resumed, result, events = asyncio.run(exercise_resume())
    assert resumed is True
    assert result.status == "completed"
    assert [event.event_type for event in events] == [
        "turn.started",
        "interaction.requested",
        "token.delta",
        "turn.finished",
    ]
    assert runtime.released_session_ids == ["jiuwen-session-01"]

    async def exercise_cancel():
        cancel_runtime = FixedAsyncAgentRuntime()
        sink = ListRuntimeEventSink()
        task = asyncio.create_task(
            cancel_runtime.arun(
                _context("run-cancel", mock_scenario="awaiting_input"),
                sink,
            )
        )
        for _ in range(100):
            if any(event.event_type == "interaction.requested" for event in sink.events):
                break
            await asyncio.sleep(0)
        accepted = await cancel_runtime.cancel("run-cancel", "user requested")
        result = await task
        return accepted, result, sink.events

    accepted, cancelled, cancel_events = asyncio.run(exercise_cancel())
    assert accepted is True
    assert cancelled.status == "cancelled"
    assert cancelled.error is not None
    assert cancelled.error.message == "user requested"
    assert [event.event_type for event in cancel_events][-1] == "turn.finished"
    assert cancel_events[-1].status == "cancelled"
