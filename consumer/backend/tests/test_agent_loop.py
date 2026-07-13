"""Tests for the single-loop agent runtime."""

from types import SimpleNamespace

import pytest

from config import AgentConfig, WebSearchConfig
from agent.core.engine import AgentLoopEngine
from agent.core.engine import (
    _append_current_date_reminder_to_latest_user_message,
    _build_tool_timing_metrics,
)
from llm import configure_llm_router, reset_llm_router_for_tests
from agent.tools.registry import ToolBatchResult
import utils.logging as logging_utils
from workspace.workspaces import workspace_scope


class _FakeLLM:
    def __init__(self, responses):
        self._responses = iter(responses)
        self.calls = []

    def call(self, messages, **kwargs):
        self.calls.append(
            {
                "messages": [dict(message) for message in messages],
                "tools": kwargs.get("tools"),
                "kwargs": kwargs,
                "log_context": logging_utils.get_log_context(),
            }
        )
        response = next(self._responses)
        if not hasattr(response, "finish_reason"):
            response.finish_reason = "stop" if not getattr(response, "tool_calls", None) else "tool_calls"
        return response


class _FakeRouter:
    def __init__(self, llm, agent_config):
        self.llm = llm
        self.agent_config = agent_config

    def call(self, *, purpose, messages, profile_id=None, **kwargs):
        del purpose, profile_id
        return self.llm.call(messages, **kwargs)

    def get_config(self, purpose, profile_id=None, workspace_id=None):
        del purpose, profile_id, workspace_id
        return getattr(
            self.llm,
            "config",
            SimpleNamespace(
                model="fake-model",
                base_url="https://fake.example/v1",
                temperature=0.0,
                max_tokens=512,
                timeout=30,
            ),
        )


@pytest.fixture(autouse=True)
def _reset_llm_router():
    reset_llm_router_for_tests()
    with workspace_scope("test-workspace"):
        yield
    reset_llm_router_for_tests()


def _agent_config(**overrides):
    config = {
        "llm_connection_max_retries": 3,
        "main_agent_max_iterations": 50,
        "sub_agent_max_iterations": 5,
        "sub_agent_max_concurrency": 4,
        "sub_agent_max_depth": 2,
        "parallel_default_max_workers": 10,
        "context_max_rounds": 10,
        "context_prune_tools": set(),
    }
    config.update(overrides)
    return AgentConfig(**config)


def _make_engine(llm, *, agent_config):
    configure_llm_router(_FakeRouter(llm, agent_config))
    return AgentLoopEngine(agent_config=agent_config)


def test_agent_loop_seeds_messages_and_finishes_without_tools(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM([SimpleNamespace(content="final answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    snapshots = []
    state = {
        "run_id": "run-loop-final",
        "session_id": "sess-loop-final",
        "current_turn_id": 1,
        "user_request": "hello",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main", snapshot_callback=snapshots.append)

    assert llm.calls[0]["kwargs"]["stream"] is True
    assert len(llm.calls) == 1
    assert llm.calls[0]["messages"][0]["role"] == "system"
    assert llm.calls[0]["messages"][1]["role"] == "user"
    assert llm.calls[0]["messages"][1]["content"].startswith("hello\n\n<system-reminder>")
    assert "# currentDate" in llm.calls[0]["messages"][1]["content"]
    assert llm.calls[0]["kwargs"]["enable_reasoning"] is False
    assert "spawn_sub_agent" not in [
        tool["function"]["name"] for tool in llm.calls[0]["tools"]
    ]
    assert "temperature" not in llm.calls[0]["kwargs"] or llm.calls[0]["kwargs"]["temperature"] is None
    assert [message["role"] for message in final_state["messages"]] == ["system", "user", "assistant"]
    assert final_state["final_output"] == "final answer"
    assert final_state["results"]["output"] == "final answer"
    assert len(snapshots) == 2


def test_agent_loop_keeps_existing_system_prompt_locked(monkeypatch):
    def fail_build_prompt(*args, **kwargs):
        del args, kwargs
        raise AssertionError("system prompt should stay locked for an existing session")

    monkeypatch.setattr(AgentLoopEngine, "_build_main_system_prompt", fail_build_prompt)

    llm = _FakeLLM([SimpleNamespace(content="next answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    state = {
        "run_id": "run-locked-system",
        "session_id": "sess-locked-system",
        "current_turn_id": 2,
        "user_request": "next question",
        "user_context": {},
        "messages": [
            {"role": "system", "content": "locked system prompt"},
            {"role": "user", "content": "first question"},
            {"role": "assistant", "content": "first answer"},
            {"role": "user", "content": "next question"},
        ],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert llm.calls[0]["messages"][0] == {
        "role": "system",
        "content": "locked system prompt",
    }
    assert final_state["messages"][0] == {
        "role": "system",
        "content": "locked system prompt",
    }


def test_current_date_reminder_appends_to_latest_user_message(monkeypatch):
    monkeypatch.setattr("agent.core.engine._current_utc8_date_text", lambda: "2026/06/08")
    messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "first question"},
        {"role": "assistant", "content": "first answer"},
        {"role": "user", "content": "second question"},
    ]

    updated = _append_current_date_reminder_to_latest_user_message(messages)

    assert updated is not messages
    assert updated[1]["content"] == "first question"
    assert updated[3]["content"].startswith("second question\n\n<system-reminder>")
    assert "Today's date is 2026/06/08." in updated[3]["content"]
    assert updated[3]["content"].endswith("</system-reminder>")
    assert messages[3]["content"] == "second question"


def test_agent_loop_exposes_web_search_only_when_enabled(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    disabled_llm = _FakeLLM([SimpleNamespace(content="disabled", tool_calls=None)])
    disabled_engine = _make_engine(
        disabled_llm,
        agent_config=_agent_config(web_search=WebSearchConfig(enabled=False)),
    )
    disabled_engine.run(
        {
            "run_id": "run-web-disabled",
            "session_id": "sess-web-disabled",
            "current_turn_id": 1,
            "user_request": "latest news",
            "user_context": {},
            "messages": [],
        },
        caller="main",
    )
    disabled_tool_names = [
        tool["function"]["name"]
        for tool in disabled_llm.calls[0]["tools"]
    ]

    enabled_llm = _FakeLLM([SimpleNamespace(content="enabled", tool_calls=None)])
    enabled_engine = _make_engine(
        enabled_llm,
        agent_config=_agent_config(web_search=WebSearchConfig(enabled=True)),
    )
    enabled_engine.run(
        {
            "run_id": "run-web-enabled",
            "session_id": "sess-web-enabled",
            "current_turn_id": 1,
            "user_request": "latest news",
            "user_context": {},
            "messages": [],
        },
        caller="main",
    )
    enabled_tool_names = [
        tool["function"]["name"]
        for tool in enabled_llm.calls[0]["tools"]
    ]

    assert "web_search" not in disabled_tool_names
    assert "web_search" in enabled_tool_names


def test_noop_observer_does_not_materialize_trace_payloads(monkeypatch):
    from integration.observability import NOOP_OBSERVER

    class _ExplodingContent:
        def __str__(self):
            raise AssertionError("noop observability should not stringify message content")

    llm = _FakeLLM([SimpleNamespace(content="final answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-noop-observer-payload",
        "session_id": "sess-noop-observer-payload",
        "current_turn_id": 1,
        "user_request": "hello",
        "user_context": {},
        "messages": [{"role": "user", "content": _ExplodingContent()}],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="subagent", observer=NOOP_OBSERVER)

    assert final_state["final_output"] == "final answer"


def test_agent_loop_finishes_trace_when_setup_fails(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    def fail_selected_context(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("selected context failed")

    observer = RecordingObserver()
    llm = _FakeLLM([])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-setup-fails",
        "session_id": "sess-setup-fails",
        "current_turn_id": 1,
        "user_request": "hello",
        "user_context": {"selected_ontology_id": "demo"},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }
    monkeypatch.setattr(
        "agent.core.engine.build_selected_ontology_preload_context",
        fail_selected_context,
    )

    with pytest.raises(RuntimeError, match="selected context failed"):
        engine.run(state, caller="main", observer=observer)

    assert [event.type for event in observer.events] == [
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    ]
    assert observer.events[-1].payload["status"] == RuntimeStatus.ERROR


def test_agent_loop_closes_llm_trace_before_streamed_tool_wait_cancellation(monkeypatch):
    from agent.exceptions import WorkflowCancelledError
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    def cancel_wait(self):
        del self
        raise WorkflowCancelledError("Workflow cancelled by user")

    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine._StreamedToolCallExecutor.wait_all", cancel_wait)

    observer = RecordingObserver()
    llm = _FakeLLM([
        SimpleNamespace(
            content="partial answer",
            tool_calls=None,
            usage={"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
        )
    ])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-wait-cancel",
        "session_id": "sess-wait-cancel",
        "current_turn_id": 1,
        "user_request": "hello",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    with pytest.raises(WorkflowCancelledError):
        engine.run(state, caller="main", observer=observer)

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.LLM_FINISHED in event_types
    llm_finished = [
        event for event in observer.events
        if event.type == RuntimeEventType.LLM_FINISHED
    ]
    assert llm_finished[-1].payload["wall_duration_ms"] >= 0
    assert llm_finished[-1].payload["stream_duration_ms"] >= 0
    tool_failed = [
        event for event in observer.events
        if event.type == RuntimeEventType.TOOL_BATCH_FAILED
    ]
    assert tool_failed[-1].payload["status"] == RuntimeStatus.CANCELLED
    loop_finished = [
        event for event in observer.events
        if event.type == RuntimeEventType.AGENT_LOOP_FINISHED
    ]
    assert loop_finished[-1].payload["status"] == RuntimeStatus.CANCELLED


def test_agent_loop_marks_started_streamed_tool_batch_failed_when_llm_errors(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FailingStreamingLLM:
        def call(self, messages, **kwargs):
            del messages
            kwargs["tool_calls_closed_callback"](
                [
                    {
                        "id": "call_read_stream_error",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ]
            )
            raise RuntimeError("llm stream failed")

    def fake_execute_tool_batch(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": tool_calls[0]["id"],
                    "content": "file body",
                }
            ]
        )

    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    observer = RecordingObserver()
    engine = _make_engine(FailingStreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-stream-tool-llm-error",
        "session_id": "sess-stream-tool-llm-error",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    with pytest.raises(RuntimeError, match="llm stream failed"):
        engine.run(state, caller="main", observer=observer)

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.TOOL_BATCH_STARTED in event_types
    assert RuntimeEventType.LLM_FAILED in event_types
    tool_failed = [
        event for event in observer.events
        if event.type == RuntimeEventType.TOOL_BATCH_FAILED
    ]
    assert tool_failed[-1].payload["status"] == RuntimeStatus.ERROR


def test_agent_loop_marks_started_streamed_tool_batch_cancelled_when_llm_cancelled(monkeypatch):
    from agent.exceptions import WorkflowCancelledError
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class CancellingStreamingLLM:
        def call(self, messages, **kwargs):
            del messages
            kwargs["tool_calls_closed_callback"](
                [
                    {
                        "id": "call_read_stream_cancel",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ]
            )
            raise WorkflowCancelledError("Workflow cancelled by user")

    def fake_execute_tool_batch(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": tool_calls[0]["id"],
                    "content": "file body",
                }
            ]
        )

    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    observer = RecordingObserver()
    engine = _make_engine(CancellingStreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-stream-tool-llm-cancel",
        "session_id": "sess-stream-tool-llm-cancel",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    with pytest.raises(WorkflowCancelledError):
        engine.run(state, caller="main", observer=observer)

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.TOOL_BATCH_STARTED in event_types
    assert RuntimeEventType.LLM_FAILED in event_types
    tool_failed = [
        event for event in observer.events
        if event.type == RuntimeEventType.TOOL_BATCH_FAILED
    ]
    assert tool_failed[-1].payload["status"] == RuntimeStatus.CANCELLED


def test_iteration_metrics_include_zero_tool_timing_without_tools(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM([SimpleNamespace(content="final answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=1))
    events = []
    state = {
        "run_id": "run-loop-final-metrics",
        "session_id": "sess-loop-final-metrics",
        "current_turn_id": 1,
        "user_request": "hello",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main", event_writer=events.append)

    iteration_event = next(event for event in events if event["data"]["type"] == "iteration.completed")
    payload = iteration_event["data"]["payload"]

    assert payload["tools_window_elapsed_ms"] == 0
    assert payload["tools_wall_elapsed_ms"] == 0
    assert payload["tools_execution_elapsed_ms"] == 0
    assert payload["tools_overlap_llm_ms"] == 0
    assert payload["tools_net_wait_ms"] == 0


def test_agent_loop_passes_sync_request_mode_directly_to_llm(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM([SimpleNamespace(content="final answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-loop-sync-mode",
        "session_id": "sess-loop-sync-mode",
        "current_turn_id": 1,
        "user_request": "hello",
        "user_context": {"llm_request_mode": "sync"},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main")

    assert llm.calls[0]["kwargs"]["stream"] is False


def test_agent_loop_executes_tool_batch_and_continues(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ],
            ),
            SimpleNamespace(content="done", tool_calls=None),
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    snapshots = []

    def fake_execute_tool_batch(tool_calls, context):
        assert tool_calls[0]["function"]["name"] == "read_file"
        assert context.run_id == "run-loop-tools"
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read",
                    "content": "file body",
                }
            ],
            state_updates={"results": {"tool_step": "ok"}},
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    state = {
        "run_id": "run-loop-tools",
        "session_id": "sess-loop-tools",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main", snapshot_callback=snapshots.append)

    assert [message["role"] for message in final_state["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert final_state["messages"][3]["name"] == "read_file"
    assert final_state["results"]["tool_step"] == "ok"
    assert final_state["results"]["output"] == "done"
    assert len(snapshots) == 4


def test_agent_loop_preserves_reasoning_content_after_tool_call(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    first_reasoning = "需要先读取文件再回答"
    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                reasoning_content=first_reasoning,
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ],
            ),
            SimpleNamespace(content="done", tool_calls=None),
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))

    monkeypatch.setattr(
        "agent.core.engine.execute_tool_batch",
        lambda tool_calls, context: ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": tool_calls[0]["id"],
                    "content": "file body",
                }
            ]
        ),
    )

    state = {
        "run_id": "run-loop-reasoning-context",
        "session_id": "sess-loop-reasoning-context",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {"agent_mode": "thinking"},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assistant_with_tool = final_state["messages"][2]
    assert assistant_with_tool["reasoning_content"] == first_reasoning
    assert llm.calls[1]["messages"][2]["reasoning_content"] == first_reasoning


def test_agent_loop_preserves_prior_turn_reasoning_content_before_new_user_request(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM([SimpleNamespace(content="continued", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=2))
    state = {
        "run_id": "run-loop-strip-prior-reasoning",
        "session_id": "sess-loop-strip-prior-reasoning",
        "current_turn_id": 2,
        "user_request": "second question",
        "user_context": {"agent_mode": "thinking"},
        "messages": [
            {"role": "system", "content": "old sys"},
            {"role": "user", "content": "first question"},
            {
                "role": "assistant",
                "content": "",
                "reasoning_content": "old tool reasoning",
                "tool_calls": [
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "read_file",
                "tool_call_id": "call_read",
                "content": "file body",
            },
            {
                "role": "assistant",
                "content": "first answer",
                "reasoning_content": "old final reasoning",
            },
            {"role": "user", "content": "second question"},
        ],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    llm_messages = llm.calls[0]["messages"]
    assert llm_messages[2]["reasoning_content"] == "old tool reasoning"
    assert llm_messages[4]["reasoning_content"] == "old final reasoning"
    assert final_state["messages"][2]["reasoning_content"] == "old tool reasoning"
    assert final_state["messages"][4]["reasoning_content"] == "old final reasoning"


def test_agent_loop_executes_closed_stream_tool_calls_without_reexecution(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    tool_call = {
        "id": "call_read_stream",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    class _StreamingLLM:
        def __init__(self):
            self.calls = []
            self._call_count = 0

        def call(self, messages, **kwargs):
            self.calls.append({"messages": [dict(message) for message in messages], "kwargs": kwargs})
            self._call_count += 1
            if self._call_count == 1:
                kwargs["tool_calls_closed_callback"]([dict(tool_call)])
                return SimpleNamespace(content="", tool_calls=[dict(tool_call)], finish_reason="tool_calls")
            return SimpleNamespace(content="done", tool_calls=None, finish_reason="stop")

    llm = _StreamingLLM()
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    execute_calls = []

    def fake_execute_tool_batch(tool_calls, context):
        execute_calls.append([tool_call["function"]["name"] for tool_call in tool_calls])
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read_stream",
                    "content": "file body",
                }
            ],
            state_updates={"results": {"tool_step": "streamed"}},
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    state = {
        "run_id": "run-loop-stream-closed",
        "session_id": "sess-loop-stream-closed",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert execute_calls == [["read_file"]]
    assert [message["role"] for message in final_state["messages"]] == [
        "system",
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert final_state["messages"][3]["tool_call_id"] == "call_read_stream"
    assert final_state["results"]["tool_step"] == "streamed"
    assert final_state["final_output"] == "done"


def test_agent_loop_defers_streamed_barrier_tool_calls_until_stream_finishes(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    spawn_tool_call = {
        "index": 0,
        "id": "call_spawn_stream",
        "type": "function",
        "function": {
            "name": "spawn_sub_agent",
            "arguments": "{\"tasks\":[{\"id\":\"nested\",\"task\":\"nested fan-out\"}]}",
        },
    }
    read_tool_call = {
        "index": 1,
        "id": "call_read_after_spawn",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }
    events = []

    class _StreamingLLM:
        def __init__(self):
            self._call_count = 0

        def call(self, messages, **kwargs):
            del messages
            self._call_count += 1
            events.append("llm:{}:start".format(self._call_count))
            if self._call_count == 1:
                kwargs["tool_calls_closed_callback"]([dict(spawn_tool_call)])
                events.append("llm:1:after_spawn_callback")
                kwargs["tool_calls_closed_callback"]([dict(read_tool_call)])
                events.append("llm:1:after_read_callback")
                return SimpleNamespace(
                    content="",
                    tool_calls=[dict(spawn_tool_call), dict(read_tool_call)],
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(content="done", tool_calls=None, finish_reason="stop")

    def fake_execute_tool_batch(tool_calls, context):
        del context
        names = [tool_call["function"]["name"] for tool_call in tool_calls]
        events.append("execute:{}".format(",".join(names)))
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": tool_call["function"]["name"],
                    "tool_call_id": tool_call["id"],
                    "content": "{} result".format(tool_call["function"]["name"]),
                }
                for tool_call in tool_calls
            ]
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    engine = _make_engine(_StreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=3))
    state = {
        "run_id": "run-loop-stream-barrier",
        "session_id": "sess-loop-stream-barrier",
        "current_turn_id": 1,
        "user_request": "delegate then read",
        "user_context": {"agent_mode": "multi_agent"},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert events == [
        "llm:1:start",
        "llm:1:after_spawn_callback",
        "llm:1:after_read_callback",
        "execute:spawn_sub_agent,read_file",
        "llm:2:start",
    ]
    assert final_state["final_output"] == "done"


def test_stream_closed_tool_calls_execute_with_triggering_assistant_context(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    tool_call = {
        "id": "call_read_stream_context",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    class _StreamingLLM:
        def call(self, messages, **kwargs):
            del messages
            kwargs["tool_calls_closed_callback"]([dict(tool_call)])
            return SimpleNamespace(content="", tool_calls=[dict(tool_call)], finish_reason="tool_calls")

    captured_context_messages = []

    def fake_execute_tool_batch(tool_calls, context):
        del tool_calls
        captured_context_messages.extend([dict(message) for message in context.messages])
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read_stream_context",
                    "content": "file body",
                }
            ],
            terminal=True,
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    engine = _make_engine(_StreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-loop-stream-context",
        "session_id": "sess-loop-stream-context",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main")

    assistant_messages = [
        message for message in captured_context_messages
        if message.get("role") == "assistant"
    ]
    assert assistant_messages
    assert assistant_messages[-1]["tool_calls"][0]["id"] == "call_read_stream_context"


def test_stream_closed_tool_calls_do_not_deepcopy_entire_state(monkeypatch, caplog):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    class _Uncopyable:
        def __deepcopy__(self, memo):
            del memo
            raise AssertionError("streamed tool execution should not deepcopy full state")

    tool_call = {
        "id": "call_read_stream_no_full_copy",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    class _StreamingLLM:
        def call(self, messages, **kwargs):
            del messages
            kwargs["tool_calls_closed_callback"]([dict(tool_call)])
            return SimpleNamespace(content="", tool_calls=[dict(tool_call)], finish_reason="tool_calls")

    def fake_execute_tool_batch(tool_calls, context):
        del tool_calls, context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read_stream_no_full_copy",
                    "content": "file body",
                }
            ],
            terminal=True,
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    engine = _make_engine(_StreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    state = {
        "run_id": "run-loop-stream-no-full-copy",
        "session_id": "sess-loop-stream-no-full-copy",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
        "uncopyable": _Uncopyable(),
    }

    with caplog.at_level("ERROR"):
        engine.run(state, caller="main")

    assert "Streamed tool call raised in worker thread" not in caplog.text


def test_iteration_metrics_exclude_streamed_tool_overlap_from_llm_elapsed(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    perf_samples = iter([
        0.00,
        0.00,
        0.01,
        0.02,
        0.03,
        2.03,
        5.03,
        5.03,
        5.03,
        5.03,
        5.04,
    ])
    monkeypatch.setattr("agent.core.engine.time.perf_counter", lambda: next(perf_samples))

    tool_call = {
        "id": "call_read_stream_metric",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    class _StreamingLLM:
        def call(self, messages, **kwargs):
            del messages
            kwargs["tool_calls_closed_callback"]([dict(tool_call)])
            return SimpleNamespace(content="", tool_calls=[dict(tool_call)], finish_reason="tool_calls")

    def fake_execute_tool_batch(tool_calls, context):
        del tool_calls, context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read_stream_metric",
                    "content": "file body",
                }
            ],
            terminal=True,
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    engine = _make_engine(_StreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    events = []
    state = {
        "run_id": "run-loop-stream-metric",
        "session_id": "sess-loop-stream-metric",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main", event_writer=events.append)

    iteration_event = next(event for event in events if event["data"]["type"] == "iteration.completed")
    payload = iteration_event["data"]["payload"]

    assert abs(payload["llm_elapsed_ms"] - 5010) <= 1
    assert abs(payload["llm_stream_elapsed_ms"] - 5010) <= 1
    assert abs(payload["llm_wall_elapsed_ms"] - 5010) <= 1
    assert "llm_exclusive_elapsed_ms" not in payload
    assert abs(payload["tools_elapsed_ms"] - 2000) <= 1
    assert abs(payload["total_elapsed_ms"] - 5040) <= 1


def test_iteration_metrics_report_tool_net_wait_and_wall_time(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    perf_samples = iter([
        0.00,  # iteration started
        0.00,  # prompt start
        0.01,  # prompt end
        0.02,  # llm start
        3.02,  # llm response finished
        3.52,  # wait_all finished
        3.53,  # tail tool batch start
        3.54,  # tail tool batch finished
        3.55,  # iteration total
    ])
    monkeypatch.setattr("agent.core.engine.time.perf_counter", lambda: next(perf_samples))

    tool_call = {
        "id": "call_read_stream_net_wait",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    class _StreamingLLM:
        def call(self, messages, **kwargs):
            del messages, kwargs
            return SimpleNamespace(content="", tool_calls=[dict(tool_call)], finish_reason="tool_calls")

    def fake_execute_tool_batch(tool_calls, context):
        del tool_calls, context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read_stream_net_wait",
                    "content": "file body",
                }
            ],
            terminal=True,
            tool_intervals_ms=[
                {"started_at_ms": 1020, "finished_at_ms": 1030, "elapsed_ms": 10},
                {"started_at_ms": 2500, "finished_at_ms": 3520, "elapsed_ms": 1020},
            ],
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    engine = _make_engine(_StreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    events = []
    state = {
        "run_id": "run-loop-net-wait",
        "session_id": "sess-loop-net-wait",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main", event_writer=events.append)

    iteration_event = next(event for event in events if event["data"]["type"] == "iteration.completed")
    payload = iteration_event["data"]["payload"]

    assert payload["tools_execution_elapsed_ms"] == 1030
    assert payload["tools_wall_elapsed_ms"] == 1030
    assert payload["tools_net_wait_ms"] == 500
    assert payload["tools_window_elapsed_ms"] == payload["tools_elapsed_ms"]


def test_streamed_tool_net_wait_uses_llm_stream_end_before_wait_all(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    perf_samples = iter([
        0.00,  # iteration started
        0.00,  # prompt start
        0.01,  # prompt end
        0.02,  # llm start
        1.52,  # llm stream response finished
        2.53,  # wait_all observes tool still running
        2.54,  # wait_all finished
        2.55,  # llm wall finished
        2.56,  # tail tool batch start
        2.57,  # tail tool batch finished
        2.58,  # iteration total
    ])
    monkeypatch.setattr("agent.core.engine.time.perf_counter", lambda: next(perf_samples))

    tool_call = {
        "id": "call_streamed_wait",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    def fake_stream_execute(self, closed_tool_calls):
        self.batch_result = ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_streamed_wait",
                    "content": "file body",
                }
            ],
            terminal=True,
            tool_intervals_ms=[
                {"started_at_ms": 1000, "finished_at_ms": 2500, "elapsed_ms": 1500},
            ],
        )
        self.executed_tool_calls.extend(closed_tool_calls)
        self.elapsed_ms = 1500

    def fake_wait_all(self):
        # Simulate waiting for streamed tools after the upstream LLM stream has closed.
        import agent.core.engine as engine_module

        assert self.engine is not None
        engine_module.time.perf_counter()
        engine_module.time.perf_counter()

    monkeypatch.setattr("agent.core.engine._StreamedToolCallExecutor.execute", fake_stream_execute)
    monkeypatch.setattr("agent.core.engine._StreamedToolCallExecutor.wait_all", fake_wait_all)

    class _StreamingLLM:
        def call(self, messages, **kwargs):
            del messages
            kwargs["tool_calls_closed_callback"]([dict(tool_call)])
            return SimpleNamespace(content="", tool_calls=[dict(tool_call)], finish_reason="tool_calls")

    engine = _make_engine(_StreamingLLM(), agent_config=_agent_config(main_agent_max_iterations=1))
    events = []
    state = {
        "run_id": "run-loop-stream-net-wait",
        "session_id": "sess-loop-stream-net-wait",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main", event_writer=events.append)

    iteration_event = next(event for event in events if event["data"]["type"] == "iteration.completed")
    payload = iteration_event["data"]["payload"]

    assert payload["llm_stream_elapsed_ms"] == 1500
    assert payload["tools_wall_elapsed_ms"] == 1500
    assert payload["tools_overlap_llm_ms"] == 520
    assert payload["tools_net_wait_ms"] == 980


def test_tool_timing_metrics_report_zero_net_wait_when_tools_are_fully_covered_by_llm():
    metrics = _build_tool_timing_metrics(
        [
            {"started_at_ms": 1100, "finished_at_ms": 1300, "elapsed_ms": 200},
            {"started_at_ms": 1250, "finished_at_ms": 1500, "elapsed_ms": 250},
        ],
        llm_started_at_ms=1000,
        llm_finished_at_ms=2000,
        tools_window_elapsed_ms=900,
    )

    assert metrics["tools_wall_elapsed_ms"] == 400
    assert metrics["tools_execution_elapsed_ms"] == 450
    assert metrics["tools_overlap_llm_ms"] == 400
    assert metrics["tools_net_wait_ms"] == 0


def test_tool_timing_metrics_treat_tools_after_llm_as_net_wait():
    metrics = _build_tool_timing_metrics(
        [
            {"started_at_ms": 2500, "finished_at_ms": 2700, "elapsed_ms": 200},
            {"started_at_ms": 2600, "finished_at_ms": 2900, "elapsed_ms": 300},
        ],
        llm_started_at_ms=1000,
        llm_finished_at_ms=2000,
        tools_window_elapsed_ms=900,
    )

    assert metrics["tools_wall_elapsed_ms"] == 400
    assert metrics["tools_execution_elapsed_ms"] == 500
    assert metrics["tools_overlap_llm_ms"] == 0
    assert metrics["tools_net_wait_ms"] == 400


def test_agent_loop_merges_streamed_state_updates_across_closed_batches(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    read_tool_call = {
        "index": 0,
        "id": "call_read_stream",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"a.txt\"}"},
    }
    search_tool_call = {
        "index": 1,
        "id": "call_search_stream",
        "type": "function",
        "function": {"name": "business_data_search", "arguments": "{\"ontology_id\":\"demo\",\"entity\":\"TargetItem\",\"query\":\"TargetItem\"}"},
    }

    class _StreamingLLM:
        def __init__(self):
            self.calls = []
            self._call_count = 0

        def call(self, messages, **kwargs):
            self.calls.append({"messages": [dict(message) for message in messages], "kwargs": kwargs})
            self._call_count += 1
            if self._call_count == 1:
                kwargs["tool_calls_closed_callback"]([dict(read_tool_call)])
                kwargs["tool_calls_closed_callback"]([dict(search_tool_call)])
                return SimpleNamespace(
                    content="",
                    tool_calls=[dict(read_tool_call), dict(search_tool_call)],
                    finish_reason="tool_calls",
                )
            return SimpleNamespace(content="done", tool_calls=None, finish_reason="stop")

    llm = _StreamingLLM()
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    execute_calls = []

    def fake_execute_tool_batch(tool_calls, context):
        tool_name = tool_calls[0]["function"]["name"]
        execute_calls.append(tool_name)
        if tool_name == "read_file":
            return ToolBatchResult(
                messages=[
                    {
                        "role": "tool",
                        "name": "read_file",
                        "tool_call_id": "call_read_stream",
                        "content": "file body",
                    }
                ],
                state_updates={"results": {"first_stream": True}},
            )
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "business_data_search",
                    "tool_call_id": "call_search_stream",
                    "content": "search body",
                }
            ],
            state_updates={"results": {"second_stream": True}},
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    state = {
        "run_id": "run-loop-stream-merge",
        "session_id": "sess-loop-stream-merge",
        "current_turn_id": 1,
        "user_request": "read then search",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert execute_calls == ["read_file", "business_data_search"]
    assert final_state["results"]["first_stream"] is True
    assert final_state["results"]["second_stream"] is True
    assert final_state["final_output"] == "done"


def test_agent_loop_filters_streamed_tool_calls_by_index_when_final_id_arrives_late(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    streamed_tool_call = {
        "index": 0,
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }
    final_tool_call = {
        "index": 0,
        "id": "call_read_stream",
        "type": "function",
        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
    }

    class _StreamingLLM:
        def __init__(self):
            self.calls = []
            self._call_count = 0

        def call(self, messages, **kwargs):
            self.calls.append({"messages": [dict(message) for message in messages], "kwargs": kwargs})
            self._call_count += 1
            if self._call_count == 1:
                kwargs["tool_calls_closed_callback"]([dict(streamed_tool_call)])
                return SimpleNamespace(content="", tool_calls=[dict(final_tool_call)], finish_reason="tool_calls")
            return SimpleNamespace(content="done", tool_calls=None, finish_reason="stop")

    llm = _StreamingLLM()
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    execute_calls = []

    def fake_execute_tool_batch(tool_calls, context):
        del context
        execute_calls.append([tool_call["function"]["name"] for tool_call in tool_calls])
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": None,
                    "content": "file body",
                }
            ],
            state_updates={"results": {"tool_step": "streamed"}},
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    state = {
        "run_id": "run-loop-stream-index-dedupe",
        "session_id": "sess-loop-stream-index-dedupe",
        "current_turn_id": 1,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert execute_calls == [["read_file"]]
    assert final_state["results"]["tool_step"] == "streamed"
    assert final_state["final_output"] == "done"


def test_agent_loop_executes_tool_calls_in_single_batch(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"ontologies/demo/objects/TargetItem.md\"}"},
                    },
                    {
                        "id": "call_search",
                        "type": "function",
                        "function": {"name": "business_data_search", "arguments": "{\"ontology_id\":\"demo\",\"entity\":\"TargetItem\",\"query\":\"TargetItem where 编码 = 'A001'\"}"},
                    },
                    {
                        "id": "call_exec",
                        "type": "function",
                        "function": {"name": "execute_script", "arguments": "{\"ontology_id\":\"demo\",\"operation_key\":\"流程A\",\"arguments\":{}}"},
                    },
                ],
            ),
            SimpleNamespace(content="done", tool_calls=None),
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))
    batch_calls = []

    def fake_execute_tool_batch(tool_calls, context):
        tool_names = [tool_call["function"]["name"] for tool_call in tool_calls]
        batch_calls.append(tool_names)
        assert tool_names == ["read_file", "business_data_search", "execute_script"]
        assert not context.read_documents
        assert context.messages[-1]["role"] == "assistant"
        return ToolBatchResult(
            messages=[
                {
                    "name": "read_file",
                    "role": "tool",
                    "tool_call_id": "call_read",
                    "content": "File: ontologies/demo/objects/TargetItem.md\n\n# TargetItem",
                },
                {
                    "role": "tool",
                    "name": "business_data_search",
                    "tool_call_id": "call_search",
                    "content": "search result",
                },
                {
                    "role": "tool",
                    "name": "execute_script",
                    "tool_call_id": "call_exec",
                    "content": "script done",
                },
            ],
            state_updates={
                "read_document_records": [
                    {
                        "path": "ontologies/demo/objects/TargetItem.md",
                        "normalized_path": "ontologies/demo/objects/TargetItem.md",
                        "doc_type": "object_doc",
                        "ontology_id": "demo",
                        "entity_name": "TargetItem",
                        "source": "read_file",
                        "turn_id": 1,
                        "iteration": 1,
                        "content_hash": "abc123",
                        "size_bytes": 7,
                        "mtime_ns": None,
                    }
                ],
                "results": {"searched": True, "executed": True},
            },
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    state = {
        "run_id": "run-phase-scheduler",
        "session_id": "sess-phase-scheduler",
        "current_turn_id": 1,
        "user_request": "执行分阶段工具调用",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert batch_calls == [["read_file", "business_data_search", "execute_script"]]
    assert final_state["read_documents"]["by_path"]["ontologies/demo/objects/TargetItem.md"]["entity_name"] == "TargetItem"
    assert final_state["results"]["searched"] is True
    assert final_state["results"]["executed"] is True
    assert [message.get("name") for message in final_state["messages"] if message.get("role") == "tool"] == [
        "read_file",
        "business_data_search",
        "execute_script",
    ]
    assert final_state["final_output"] == "done"


def test_agent_loop_persists_selected_ontology_context_on_first_user_message(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        "agent.core.engine.build_selected_ontology_preload_context",
        lambda *args, **kwargs: "<Selected-Ontology>\n- selected ontology\n</Selected-Ontology>",
    )

    llm = _FakeLLM([SimpleNamespace(content="final answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=2))
    state = {
        "run_id": "run-preloaded-ontology",
        "session_id": "sess-preloaded-ontology",
        "current_turn_id": 1,
        "user_request": "分析这次变更影响",
        "user_context": {"selected_ontology_id": "demo-ontology"},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert [message["role"] for message in llm.calls[0]["messages"][:2]] == ["system", "user"]
    assert llm.calls[0]["messages"][1]["content"].startswith("分析这次变更影响")
    assert (
        "<Selected-Ontology>"
        in llm.calls[0]["messages"][1]["content"]
    )
    assert [message["role"] for message in final_state["messages"]] == ["system", "user", "assistant"]
    assert "Selected-Ontology" in final_state["messages"][1]["content"]


def test_agent_loop_builds_selected_ontology_context_once_per_run(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")
    preload_calls = []

    def fake_build_selected_ontology_preload_context(*args, **kwargs):
        del args
        preload_calls.append(kwargs)
        return "<Selected-Ontology>\n- cached selected ontology\n</Selected-Ontology>"

    monkeypatch.setattr(
        "agent.core.engine.build_selected_ontology_preload_context",
        fake_build_selected_ontology_preload_context,
    )

    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ],
            ),
            SimpleNamespace(content="done", tool_calls=None),
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=3))

    monkeypatch.setattr(
        "agent.core.engine.execute_tool_batch",
        lambda tool_calls, context: ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": tool_calls[0]["id"],
                    "content": "file body",
                }
            ]
        ),
    )

    state = {
        "run_id": "run-preloaded-ontology-cache",
        "session_id": "sess-preloaded-ontology-cache",
        "current_turn_id": 1,
        "user_request": "请继续分析",
        "user_context": {
            "selected_ontology_id": "demo-ontology",
        },
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert final_state["final_output"] == "done"
    assert len(preload_calls) == 1
    assert llm.calls[0]["messages"][1]["content"].count("<Selected-Ontology>") == 1
    assert llm.calls[1]["messages"][1]["content"].count("<Selected-Ontology>") == 1
    assert preload_calls[0] == {
        "selected_ontology_id": "demo-ontology",
    }


def test_agent_loop_keeps_selected_ontology_context_on_first_user_message(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    def fail_build_selected_ontology_preload_context(*args, **kwargs):
        del args, kwargs
        raise AssertionError("selected ontology context should already be persisted")

    monkeypatch.setattr(
        "agent.core.engine.build_selected_ontology_preload_context",
        fail_build_selected_ontology_preload_context,
    )

    llm = _FakeLLM([SimpleNamespace(content="done", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=2))
    state = {
        "run_id": "run-preloaded-ontology-first-user",
        "session_id": "sess-preloaded-ontology-first-user",
        "current_turn_id": 2,
        "user_request": "第二轮问题",
        "user_context": {"selected_ontology_id": "demo-ontology"},
        "messages": [
            {"role": "system", "content": "locked system"},
            {
                "role": "user",
                "content": "第一轮问题\n\n<Selected-Ontology>\n- frozen ontology\n</Selected-Ontology>",
            },
            {"role": "assistant", "content": "第一轮回答"},
            {"role": "user", "content": "第二轮问题"},
        ],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    engine.run(state, caller="main")

    assert "<Selected-Ontology>" in llm.calls[0]["messages"][1]["content"]
    assert "frozen ontology" in llm.calls[0]["messages"][1]["content"]
    assert "<Selected-Ontology>" not in llm.calls[0]["messages"][3]["content"]


def test_agent_loop_registers_selected_ontology_preload_records(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")
    monkeypatch.setattr(
        "agent.core.engine.build_selected_ontology_preload_context",
        lambda *args, **kwargs: "<Selected-Ontology>\n- selected ontology\n</Selected-Ontology>",
    )
    monkeypatch.setattr(
        "agent.core.engine.build_selected_ontology_preload_records",
        lambda *args, **kwargs: [
            {
                "path": "ontologies/demo/objects-list.md",
                "normalized_path": "ontologies/demo/objects-list.md",
                "doc_type": "objects_list",
                "ontology_id": "demo",
                "source": "preload",
                "turn_id": 1,
                "iteration": None,
                "content_hash": "abc123",
                "size_bytes": 12,
                "mtime_ns": None,
            }
        ],
    )

    llm = _FakeLLM([SimpleNamespace(content="final answer", tool_calls=None)])
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=2))
    state = {
        "run_id": "run-preloaded-ledger",
        "session_id": "sess-preloaded-ledger",
        "current_turn_id": 1,
        "user_request": "分析这次变更影响",
        "user_context": {"selected_ontology_id": "demo-ontology"},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert final_state["read_documents"]["by_path"]["ontologies/demo/objects-list.md"]["source"] == "preload"
    assert final_state["read_documents"]["recent_paths"] == ["ontologies/demo/objects-list.md"]
    assert final_state["final_output"] == "final answer"


def test_agent_loop_terminal_user_interaction_tool(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_interaction",
                        "type": "function",
                        "function": {
                            "name": "request_user_interaction",
                            "arguments": (
                                '{"type":"single_choice","prompt":"请选择处理方式",'
                                '"options":[{"id":"a","label":"方案A","value":"A"}]}'
                            ),
                        },
                    }
                ],
            )
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=2))

    state = {
        "run_id": "run-interaction-tool",
        "session_id": "sess-interaction-tool",
        "current_turn_id": 1,
        "user_request": "run interactive operation",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert len(llm.calls) == 1
    assert final_state["pending_user_interaction"]["type"] == "single_choice"
    assert final_state["pending_user_interaction"]["id"] == "interaction_call_interaction"
    assert final_state["final_output"]["type"] == "user_interaction"
    assert final_state["final_output"]["text"] == "请选择处理方式"
    assert final_state["messages"][-1]["role"] == "tool"
    assert final_state["messages"][-1]["name"] == "request_user_interaction"


def test_agent_loop_returns_when_tool_batch_is_terminal(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_plan",
                        "type": "function",
                        "function": {"name": "execute_script", "arguments": "{}"},
                    }
                ],
            )
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=2))

    monkeypatch.setattr(
        "agent.core.engine.execute_tool_batch",
        lambda tool_calls, context: ToolBatchResult(
            state_updates={
                "final_output": {"type": "text", "text": "terminal tool output"},
            },
            terminal=True,
        ),
    )

    state = {
        "run_id": "run-terminal-tool",
        "session_id": "sess-terminal-tool",
        "current_turn_id": 1,
        "user_request": "run terminal tool",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(state, caller="main")

    assert len(llm.calls) == 1
    assert final_state["final_output"]["text"] == "terminal tool output"


def test_agent_loop_raises_when_iteration_limit_is_exceeded(monkeypatch):
    monkeypatch.setattr("agent.core.engine.build_ontologies_section", lambda *args, **kwargs: "")
    monkeypatch.setattr("agent.core.engine.build_skills_section", lambda *args, **kwargs: "")

    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ],
            )
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(main_agent_max_iterations=1))

    monkeypatch.setattr(
        "agent.core.engine.execute_tool_batch",
        lambda tool_calls, context: ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_1",
                    "content": "file body",
                }
            ]
        ),
    )

    state = {
        "run_id": "run-limit",
        "session_id": "sess-limit",
        "current_turn_id": 1,
        "user_request": "loop forever",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    with pytest.raises(RuntimeError, match="Agent loop iteration limit exceeded \\(1\\)"):
        engine.run(state, caller="main", iteration_limit=1)


def test_agent_loop_binds_subagent_log_context_for_llm_and_tools(monkeypatch):
    llm = _FakeLLM(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_read",
                        "type": "function",
                        "function": {"name": "read_file", "arguments": "{\"file_path\":\"x.txt\"}"},
                    }
                ],
            ),
            SimpleNamespace(content="done", tool_calls=None),
        ]
    )
    engine = _make_engine(llm, agent_config=_agent_config(sub_agent_max_iterations=3))
    observed_tool_contexts = []

    def fake_execute_tool_batch(tool_calls, context):
        del tool_calls, context
        observed_tool_contexts.append(logging_utils.get_log_context())
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "name": "read_file",
                    "tool_call_id": "call_read",
                    "content": "file body",
                }
            ]
        )

    monkeypatch.setattr("agent.core.engine.execute_tool_batch", fake_execute_tool_batch)

    state = {
        "run_id": "run-subagent-log-context",
        "session_id": "sess-subagent-log-context",
        "current_turn_id": 7,
        "user_request": "read it",
        "user_context": {},
        "messages": [],
        "results": {},
        "completed_ids": [],
        "failed_ids": [],
        "errors": [],
        "final_output": None,
    }

    final_state = engine.run(
        state,
        caller="subagent",
        system_prompt="subagent system prompt",
        subagent_lineage={
            "subagent_id": "sub_abc123",
            "task_id": "task-17",
            "group_id": "group-9",
            "depth": 2,
            "parent_subagent_id": "sub_parent_1",
            "root_tool_call_id": "tool_root_1",
        },
    )

    assert final_state["final_output"] == "done"
    assert llm.calls
    assert observed_tool_contexts
    for call in llm.calls:
        assert call["log_context"]["agent_type"] == "subagent"
        assert call["log_context"]["agent_id"] == "sub_abc123"
        assert call["log_context"]["task_id"] == "task-17"
        assert call["log_context"]["group_id"] == "group-9"
        assert call["log_context"]["depth"] == 2
        assert call["log_context"]["run_id"] == "run-subagent-log-context"
        assert call["log_context"]["turn_id"] == 7

    assert observed_tool_contexts[0]["agent_type"] == "subagent"
    assert observed_tool_contexts[0]["agent_id"] == "sub_abc123"
    assert observed_tool_contexts[0]["task_id"] == "task-17"
