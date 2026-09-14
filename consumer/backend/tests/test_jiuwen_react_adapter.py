# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for the openJiuwen ReAct adapter boundary."""

from internal_access import internal
import asyncio
import importlib
import importlib.util
import json
import threading
import time
from operator import getitem
from types import SimpleNamespace

import pytest
import utils.logging as logging_utils
from agent.context import apply_sliding_window
from agent.core.jiuwen import smoke as jiuwen_smoke
from agent.core.jiuwen.smoke import run_jiuwen_smoke
from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter, jiuwen_tool_info_from_spec
from agent.core.jiuwen.trace import jiuwen_tool_call_to_trace_tool_call
from agent.core.jiuwen_engine import (
    JiuwenBackendUnavailable,
    JiuwenReactLoopEngine,
    _JiuwenModelCompletionSignal,
    _JiuwenModelTraceBridge,
    _patch_jiuwen_openai_reasoning_alias,
    _raise_if_jiuwen_cancelled,
    create_default_jiuwen_agent,
    register_current_runtime_tools,
    warm_up_jiuwen_runtime,
)
from agent.exceptions import (
    ModelContentFilteredError,
    ModelOutputTruncatedError,
    TerminalToolExecutionError,
    WorkflowCancelledError,
)
from agent.runtime.async_utils import (
    clear_bounded_offload_pools,
    configure_bounded_offload_pools,
)
from agent.runtime.offload import BoundedOffloadPool
from agent.tools.base import ToolBatchResult, ToolInvocation
from agent.tools.registry import get_tool_schema, get_tool_spec
from backend_synthetic_credentials import synthetic_secret
from config.settings import AgentConfig, LimitsConfig, WebSearchConfig
from llm import LLMPurpose

_REAL_IMPORT_MODULE = importlib.import_module


def _run_engine(engine, state, **kwargs):
    return asyncio.run(engine.arun(state, **kwargs))


@pytest.fixture(autouse=True)
def _lifespan_owned_offload_pools():
    def new_pool(name):
        return BoundedOffloadPool(
            name=name,
            max_workers=4,
            max_pending=16,
            capacity_wait_seconds=1.0,
            operation_timeout_seconds=5.0,
        )

    pools = tuple(new_pool(name) for name in ("short-test", "external-test", "tool-test"))
    configure_bounded_offload_pools(
        short_io=pools[0],
        external_io=pools[1],
        tool_io=pools[2],
    )
    try:
        yield
    finally:

        async def close_pools():
            deadline = asyncio.get_running_loop().time() + 2.0
            await asyncio.gather(*(pool.close(deadline=deadline) for pool in pools))

        asyncio.run(close_pools())
        clear_bounded_offload_pools()


class _FakeContextEngineConfig:
    """Shared fake for ContextEngineConfig capturing constructor kwargs."""

    def __init__(self, **kwargs):
        self.kwargs = kwargs


class _RemovableFakeResourceManager:
    def __init__(self):
        self.removed_tool_ids = []

    def remove_tool(self, tool_id, tag=None, skip_if_tag_not_exists=True):
        del tag, skip_if_tag_not_exists
        tool_ids = tool_id if isinstance(tool_id, list) else [tool_id]
        if not hasattr(self, "removed_tool_ids"):
            self.removed_tool_ids = []
        self.removed_tool_ids.extend(tool_ids)
        registered_tools = getattr(self, "registered_tools", None)
        if isinstance(registered_tools, dict):
            for registered_tool_id in tool_ids:
                registered_tools.pop(registered_tool_id, None)
        return SimpleNamespace(success=True)


@pytest.fixture(autouse=True)
def _configure_workspace_runtime(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module
    import utils.skills as skills_module
    from workspace.workspaces import create_workspace

    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    monkeypatch.setattr(skills_module, "_global_registry", None)
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)


def test_jiuwen_adapter_support_modules_are_split_out():
    for module_name in (
        "agent.core.jiuwen",
        "agent.core.jiuwen.common",
        "agent.core.jiuwen.tools",
        "agent.core.jiuwen.trace",
        "agent.core.jiuwen.patches",
        "agent.core.jiuwen.smoke",
        "agent.core.jiuwen_engine",
    ):
        assert importlib.util.find_spec(module_name) is not None
    assert importlib.util.find_spec("agent.core.jiuwen_smoke") is None


def test_loop_package_no_longer_exports_factory():
    loop_module = importlib.import_module("agent.core")

    assert importlib.util.find_spec("agent.core.factory") is None
    assert not hasattr(loop_module, "create_jiuwen_loop_engine")


def test_loop_package_no_longer_reexports_runtime_factory():
    import agent.core as loop_module

    assert loop_module.__all__ == []


def test_jiuwen_tool_info_from_spec_uses_runtime_tool_spec():
    spec = get_tool_spec("ontology_context")

    tool_info = jiuwen_tool_info_from_spec(spec)

    assert tool_info["type"] == "function"
    assert tool_info["name"] == "ontology_context"
    assert tool_info["description"] == spec.schema["function"]["description"]
    assert tool_info["parameters"] == spec.schema["function"]["parameters"]
    assert "function" not in tool_info


def test_jiuwen_tool_call_to_trace_tool_call_wraps_flat_tool_call():
    jiuwen_call = SimpleNamespace(
        id="call_1",
        type="function",
        name="ontology_context",
        arguments='{"ontology_id":"demo","request_type":"root"}',
        index=2,
    )

    tool_call = jiuwen_tool_call_to_trace_tool_call(jiuwen_call)

    assert tool_call == {
        "id": "call_1",
        "type": "function",
        "function": {
            "name": "ontology_context",
            "arguments": '{"ontology_id":"demo","request_type":"root"}',
        },
        "index": 2,
    }


def test_jiuwen_tool_call_to_trace_tool_call_accepts_mapping_payload():
    jiuwen_call = {
        "id": "call_2",
        "type": "function",
        "name": "skill",
        "arguments": {"skill_id": "finance"},
    }

    tool_call = jiuwen_tool_call_to_trace_tool_call(jiuwen_call)

    assert tool_call["id"] == "call_2"
    assert tool_call["function"]["name"] == "skill"
    assert tool_call["function"]["arguments"] == '{"skill_id":"finance"}'


def test_jiuwen_tool_call_trace_normalizer_is_not_exported_from_tool_binding():
    tools_module = importlib.import_module("agent.core.jiuwen.tools")
    adapter_module = importlib.import_module("agent.core.jiuwen_engine")

    assert not hasattr(tools_module, "jiuwen_tool_call_to_openai_tool_call")
    assert not hasattr(adapter_module, "jiuwen_tool_call_to_openai_tool_call")


def test_jiuwen_loop_engine_constructs_when_jiuwen_available(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())

    assert isinstance(engine, JiuwenReactLoopEngine)


def test_jiuwen_loop_engine_fails_loudly_when_dependency_missing(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: False)

    with pytest.raises(JiuwenBackendUnavailable, match="openjiuwen"):
        JiuwenReactLoopEngine(agent_config=_agent_config())


def test_jiuwen_loop_engine_accepts_agent_factory(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    fake_agent = _FakeJiuwenAgent({"output": "factory answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )

    state = {
        "run_id": "run-factory",
        "session_id": "sess-factory",
        "current_turn_id": 1,
        "user_request": "hello",
        "messages": [],
        "results": {},
    }
    _run_engine(engine, state, caller="main")

    assert isinstance(engine, JiuwenReactLoopEngine)
    assert getitem(state, "final_output") == "factory answer"


def test_jiuwen_loop_engine_invokes_agent_and_updates_current_state(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    fake_agent = _FakeJiuwenAgent({"output": "jiuwen answer", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen",
        "session_id": "sess-jiuwen",
        "current_turn_id": 7,
        "user_request": "请分析这个本体",
        "messages": [],
        "results": {},
    }
    events = []
    snapshots = []

    result = _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
        snapshot_callback=snapshots.append,
        iteration_limit=3,
    )

    assert result is state
    assert fake_agent.inputs["conversation_id"] == "sess-jiuwen"
    assert fake_agent.inputs["query"].startswith("请分析这个本体\n\n<system-reminder>")
    assert "# currentDate" in fake_agent.inputs["query"]
    assert state["messages"][0]["role"] == "system"
    assert "Ontology-Driven Intelligent Agent" in state["messages"][0]["content"]
    assert state["messages"][1] == {"role": "user", "content": "请分析这个本体"}
    assert state["messages"][-1] == {"role": "assistant", "content": "jiuwen answer"}
    assert getitem(state, "final_output") == "jiuwen answer"
    assert state["results"]["output"] == "jiuwen answer"
    assert snapshots[-1]["final_output"] == "jiuwen answer"
    event = events[-1]["data"]
    assert event["type"] == "iteration.completed"
    assert event["source"] == {"node": "agent_loop", "component": "iteration"}
    assert event["payload"]["status"] == "final_output"
    assert event["payload"]["tool_calls_count"] == 0


def test_jiuwen_loop_engine_emits_runtime_trace_events(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    observer = RecordingObserver()
    fake_agent = _FakeJiuwenAgent({"output": "traced answer", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )

    _run_engine(
        engine,
        {
            "run_id": "run-jiuwen-trace",
            "session_id": "sess-jiuwen-trace",
            "current_turn_id": 7,
            "user_request": "trace request",
            "messages": [],
            "results": {},
        },
        caller="main",
        observer=observer,
    )

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.AGENT_LOOP_STARTED in event_types
    assert RuntimeEventType.AGENT_ITERATION_PREPARED in event_types
    assert RuntimeEventType.AGENT_ITERATION_FINISHED in event_types
    assert RuntimeEventType.AGENT_LOOP_FINISHED in event_types
    assert observer.events[-1].payload["status"] == RuntimeStatus.SUCCESS


def test_jiuwen_loop_engine_finishes_trace_when_setup_fails(monkeypatch):
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

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_context",
        fail_selected_context,
    )

    observer = RecordingObserver()
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: _FakeJiuwenAgent({"output": "unused", "result_type": "answer"}),
    )

    with pytest.raises(RuntimeError, match="selected context failed"):
        _run_engine(
            engine,
            {
                "run_id": "run-jiuwen-setup-fails",
                "session_id": "sess-jiuwen-setup-fails",
                "current_turn_id": 7,
                "user_request": "trace request",
                "user_context": {"selected_ontology_id": "demo"},
                "messages": [],
                "results": {},
            },
            caller="main",
            observer=observer,
        )

    loop_finished = [event for event in observer.events if event.type == RuntimeEventType.AGENT_LOOP_FINISHED]
    assert loop_finished
    assert loop_finished[-1].payload["status"] == RuntimeStatus.ERROR


def test_jiuwen_loop_engine_marks_loop_error_when_post_processing_fails(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    observer = RecordingObserver()
    fake_agent = _FakeJiuwenAgent({"output": "answer before snapshot", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )

    def fail_snapshot(_state):
        raise RuntimeError("snapshot failed")

    with pytest.raises(RuntimeError, match="snapshot failed"):
        _run_engine(
            engine,
            {
                "run_id": "run-jiuwen-post-processing-error",
                "session_id": "sess-jiuwen-post-processing-error",
                "current_turn_id": 7,
                "user_request": "trace request",
                "messages": [],
                "results": {},
            },
            caller="main",
            observer=observer,
            snapshot_callback=fail_snapshot,
        )

    loop_finished = [event for event in observer.events if event.type == RuntimeEventType.AGENT_LOOP_FINISHED]
    assert loop_finished[-1].payload["status"] == RuntimeStatus.ERROR


def test_jiuwen_loop_engine_traces_each_internal_model_call(monkeypatch):
    from integration.observability import RuntimeEventType

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}
            self.unregistered = []

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

        async def unregister(self, event, callback):
            self.unregistered.append((event, callback))

    class FakeUsage:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def model_dump(self):
            return {
                "prompt_tokens": 11,
                "completion_tokens": 7,
                "total_tokens": 18,
            }

    class FakeMessage:
        def __init__(self, role, content):
            self.role = role
            self.content = content

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    class FakeToolInfo:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def model_dump(self):
            return {"name": "business_data_search"}

    class FakeResponse:
        content = "模型回答"
        reasoning_content = "模型思考"
        usage_metadata = FakeUsage()
        finish_reason = "tool_calls"
        tool_calls = [
            SimpleNamespace(
                id="call_1",
                type="function",
                name="business_data_search",
                arguments='{"query":"CHANGE_ORDER-001"}',
            )
        ]

    class FakeInternalTraceJiuwenAgent:
        def __init__(self):
            self.agent_callback_manager = FakeCallbackManager()
            self._config = SimpleNamespace(model_name="jiuwen-model")

        async def stream(self, inputs):
            del inputs
            for idx in (1, 2):
                ctx = SimpleNamespace(
                    agent=self,
                    inputs=SimpleNamespace(
                        messages=[
                            FakeMessage("system", "system {}".format(idx)),
                            FakeMessage("user", "query {}".format(idx)),
                        ],
                        tools=[FakeToolInfo()],
                        response=FakeResponse(),
                    ),
                    exception=None,
                )
                await self.agent_callback_manager.callbacks[FakeAgentCallbackEvent.BEFORE_MODEL_CALL](ctx)
                await self.agent_callback_manager.callbacks[FakeAgentCallbackEvent.AFTER_MODEL_CALL](ctx)
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_agent_callback_event_class",
        lambda: FakeAgentCallbackEvent,
    )
    fake_agent = FakeInternalTraceJiuwenAgent()
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    observer = RecordingObserver()
    engine_state = {
        "run_id": "run-jiuwen-internal-llm-trace",
        "session_id": "sess-jiuwen-internal-llm-trace",
        "current_turn_id": 1,
        "user_request": "trace internal calls",
        "messages": [],
        "results": {},
    }

    protocol_events = []
    _run_engine(
        engine,
        engine_state,
        caller="main",
        observer=observer,
        event_writer=protocol_events.append,
    )

    llm_started = [event for event in observer.events if event.type == RuntimeEventType.LLM_STARTED]
    llm_finished = [event for event in observer.events if event.type == RuntimeEventType.LLM_FINISHED]
    assert len(llm_started) == 2
    assert len(llm_finished) == 2
    assert [event.span_key for event in llm_started] == [
        "llm:main:1:1",
        "llm:main:1:2",
    ]
    assert [event.span_key for event in llm_finished] == [
        "llm:main:1:1",
        "llm:main:1:2",
    ]
    assert llm_started[0].payload["request_label"] == "jiuwen_react_model_call"
    assert llm_started[0].payload["latest_input"] == "query 1"
    assert llm_started[0].payload["tools_count"] == 1
    assert llm_finished[0].payload["completion_text"] == "模型回答"
    assert llm_finished[0].payload["reasoning_text"] == "模型思考"
    assert llm_finished[0].payload["tool_calls_count"] == 1
    assert llm_finished[0].payload["usage"]["total_tokens"] == 18
    iteration_payload = next(
        event["data"]["payload"] for event in protocol_events if event["data"]["type"] == "iteration.completed"
    )
    assert "model_elapsed_ms" in iteration_payload
    assert "setup_elapsed_ms" in iteration_payload
    assert "llm_elapsed_ms" in iteration_payload
    assert iteration_payload["setup_elapsed_ms"] == max(
        0,
        iteration_payload["total_elapsed_ms"]
        - iteration_payload["model_elapsed_ms"]
        - iteration_payload["tools_elapsed_ms"],
    )
    assert {event for event, _callback in fake_agent.agent_callback_manager.unregistered} == {
        FakeAgentCallbackEvent.BEFORE_MODEL_CALL,
        FakeAgentCallbackEvent.AFTER_MODEL_CALL,
        FakeAgentCallbackEvent.ON_MODEL_EXCEPTION,
    }


def test_jiuwen_model_trace_reports_timing_without_observability(monkeypatch):
    from integration.observability import NOOP_OBSERVER, AgentRuntimeTrace

    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

    class FakeResponse:
        content = "done"
        reasoning_content = ""
        usage_metadata = None
        finish_reason = "stop"
        tool_calls = []

    timings = []
    bridge = _JiuwenModelTraceBridge(
        runtime_trace=AgentRuntimeTrace(
            observer=NOOP_OBSERVER,
            run_id="run-model-timing",
            session_id="session-model-timing",
            turn_id=1,
            caller="main",
        ),
        state={"run_id": "run-model-timing"},
        caller="main",
        iteration_provider=lambda: 2,
        model_provider="openai_compatible",
        stream_enabled=True,
        enable_reasoning=False,
        event_class_provider=lambda: FakeAgentCallbackEvent,
        model_timing_observer=lambda iteration, elapsed_ms: timings.append((iteration, elapsed_ms)),
        sliding_window_applied=True,
    )
    fake_agent = SimpleNamespace(
        agent_callback_manager=FakeCallbackManager(),
        _config=SimpleNamespace(model_name="compat-model"),
    )
    ctx = SimpleNamespace(
        agent=fake_agent,
        inputs=SimpleNamespace(messages=[], tools=[], response=FakeResponse()),
        exception=None,
    )
    monkeypatch.setattr("agent.core.jiuwen.trace.elapsed_ms_since", lambda _started_at: 37)

    assert asyncio.run(bridge.register(fake_agent)) is True
    asyncio.run(fake_agent.agent_callback_manager.callbacks["before_model_call"](ctx))
    asyncio.run(fake_agent.agent_callback_manager.callbacks["after_model_call"](ctx))

    assert timings == [(2, 37)]


def test_jiuwen_model_trace_reports_completion_without_observability(monkeypatch):
    from integration.observability import NOOP_OBSERVER, AgentRuntimeTrace

    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

    class FakeResponse:
        content = "synthetic partial answer"
        reasoning_content = ""
        usage_metadata = None
        finish_reason = "length"
        tool_calls = []

    completions = []
    bridge = _JiuwenModelTraceBridge(
        runtime_trace=AgentRuntimeTrace(
            observer=NOOP_OBSERVER,
            run_id="run-completion-signal",
            session_id="session-completion-signal",
            turn_id=1,
            caller="main",
        ),
        state={"run_id": "run-completion-signal"},
        caller="main",
        iteration_provider=lambda: 3,
        model_provider="openai_compatible",
        stream_enabled=True,
        enable_reasoning=False,
        event_class_provider=lambda: FakeAgentCallbackEvent,
        model_completion_callback=completions.append,
        sliding_window_applied=True,
    )
    fake_agent = SimpleNamespace(
        agent_callback_manager=FakeCallbackManager(),
        _config=SimpleNamespace(model_name="synthetic-model"),
    )
    ctx = SimpleNamespace(
        agent=fake_agent,
        inputs=SimpleNamespace(messages=[], tools=[], response=FakeResponse()),
        exception=None,
    )

    assert asyncio.run(bridge.register(fake_agent)) is True
    asyncio.run(fake_agent.agent_callback_manager.callbacks["before_model_call"](ctx))
    asyncio.run(fake_agent.agent_callback_manager.callbacks["after_model_call"](ctx))

    assert completions == [
        {
            "iteration": 3,
            "call_index": 1,
            "finish_reason": "length",
            "has_tool_calls": False,
            "usage": {},
        }
    ]


def test_jiuwen_model_completion_signal_rejects_incomplete_final_answers():
    signal = _JiuwenModelCompletionSignal()
    signal.record({"finish_reason": "FinishReason.LENGTH", "has_tool_calls": False})

    with pytest.raises(ModelOutputTruncatedError) as truncated:
        signal.raise_if_incomplete("synthetic partial answer")

    assert truncated.value.partial_output == "synthetic partial answer"
    assert truncated.value.finish_reason == "length"

    signal.record({"finish_reason": "content-filter", "has_tool_calls": False})
    with pytest.raises(ModelContentFilteredError):
        signal.raise_if_incomplete("")


@pytest.mark.parametrize("finish_reason", [None, "stop", "eos", "stop_sequence"])
def test_jiuwen_model_completion_signal_accepts_complete_final_answers(finish_reason):
    signal = _JiuwenModelCompletionSignal()
    signal.record({"finish_reason": finish_reason, "has_tool_calls": False})

    signal.raise_if_incomplete("synthetic complete answer")


def test_jiuwen_model_completion_signal_accepts_completed_tool_calls():
    signal = _JiuwenModelCompletionSignal()
    signal.record({"finish_reason": "tool_calls", "has_tool_calls": True})

    signal.raise_if_incomplete("")


def test_jiuwen_model_completion_signal_rejects_truncated_tool_calls():
    signal = _JiuwenModelCompletionSignal()
    signal.record({"finish_reason": "length", "has_tool_calls": True})

    with pytest.raises(ModelOutputTruncatedError):
        signal.raise_if_incomplete("")


def test_jiuwen_model_completion_signal_detects_limit_when_provider_reports_stop():
    signal = _JiuwenModelCompletionSignal(max_output_tokens=80)
    signal.record(
        {
            "finish_reason": "stop",
            "has_tool_calls": False,
            "usage": {"completion_tokens": 80},
        }
    )

    with pytest.raises(ModelOutputTruncatedError) as truncated:
        signal.raise_if_incomplete("synthetic partial answer")

    assert truncated.value.finish_reason == "token_limit_reached"


def test_jiuwen_model_completion_signal_accepts_stop_below_configured_limit():
    signal = _JiuwenModelCompletionSignal(max_output_tokens=80)
    signal.record(
        {
            "finish_reason": "stop",
            "has_tool_calls": False,
            "usage": {"completion_tokens": 79},
        }
    )

    signal.raise_if_incomplete("synthetic complete answer")


def test_jiuwen_model_completion_signal_does_not_infer_limit_for_tool_calls():
    signal = _JiuwenModelCompletionSignal(max_output_tokens=80)
    signal.record(
        {
            "finish_reason": "tool_calls",
            "has_tool_calls": True,
            "usage": {"completion_tokens": 80},
        }
    )

    signal.raise_if_incomplete("")


def test_jiuwen_loop_engine_rejects_length_as_a_successful_final_answer(monkeypatch):
    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

        async def unregister(self, event, callback):
            del event, callback

    class FakeResponse:
        content = "synthetic partial answer"
        reasoning_content = ""
        usage_metadata = None
        finish_reason = "length"
        tool_calls = []

    class TruncatedAgent:
        def __init__(self):
            self.agent_callback_manager = FakeCallbackManager()
            self._config = SimpleNamespace(model_name="synthetic-model")

        async def stream(self, inputs):
            del inputs
            ctx = SimpleNamespace(
                agent=self,
                inputs=SimpleNamespace(
                    messages=[],
                    tools=[],
                    response=FakeResponse(),
                ),
                exception=None,
            )
            await self.agent_callback_manager.callbacks["before_model_call"](ctx)
            await self.agent_callback_manager.callbacks["after_model_call"](ctx)
            yield SimpleNamespace(
                type="answer",
                payload={
                    "output": "synthetic partial answer",
                    "result_type": "answer",
                },
            )

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_agent_callback_event_class",
        lambda: FakeAgentCallbackEvent,
    )
    state = {
        "run_id": "run-truncated-final-answer",
        "session_id": "session-truncated-final-answer",
        "current_turn_id": 1,
        "user_request": "synthetic request",
        "messages": [],
        "results": {},
    }
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=TruncatedAgent,
    )

    with pytest.raises(ModelOutputTruncatedError) as raised:
        _run_engine(engine, state, caller="main")

    assert raised.value.partial_output == "synthetic partial answer"
    assert "final_output" not in state
    assert all(message.get("role") != "assistant" for message in state["messages"])


def test_jiuwen_model_trace_stashes_assistant_context_for_tool_calls():
    from integration.observability.traces.agent import AgentRuntimeTrace

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeMessage:
        role = "user"
        content = "query"

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    class FakeResponse:
        content = "需要先查询业务数据。"
        reasoning_content = "用户问题需要工具补充事实。"
        usage_metadata = None
        finish_reason = "tool_calls"
        tool_calls = [
            SimpleNamespace(
                id="call_1",
                type="function",
                name="business_data_search",
                arguments='{"query":"CHANGE_ORDER-001"}',
            )
        ]

    state = {"run_id": "run-jiuwen-pending-context", "messages": [], "results": {}}
    bridge = _JiuwenModelTraceBridge(
        runtime_trace=AgentRuntimeTrace(
            observer=RecordingObserver(),
            run_id="run-jiuwen-pending-context",
            session_id="sess-jiuwen-pending-context",
            turn_id=1,
            caller="main",
        ),
        state=state,
        caller="main",
        iteration_provider=lambda: 1,
        model_provider="openai_compatible",
        llm_config=None,
        stream_enabled=True,
        enable_reasoning=True,
        sliding_window_applied=True,
    )
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_config=SimpleNamespace(model_name="compat-model")),
        inputs=SimpleNamespace(messages=[FakeMessage()], tools=[], response=FakeResponse()),
        exception=None,
    )

    asyncio.run(internal(bridge).on_before_model_call(ctx))
    asyncio.run(internal(bridge).on_after_model_call(ctx))

    assert getitem(state, "_jiuwen_pending_assistant_context") == {
        "content": "需要先查询业务数据。",
        "reasoning_content": "用户问题需要工具补充事实。",
    }


def test_jiuwen_model_trace_normalizes_openai_compatible_cache_usage():
    from integration.observability import RuntimeEventType
    from integration.observability.traces.agent import AgentRuntimeTrace

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeUsage:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def model_dump(self):
            return {
                "prompt_tokens": 6250,
                "completion_tokens": 8,
                "total_tokens": 6258,
                "prompt_cache_hit_tokens": 6144,
                "prompt_cache_miss_tokens": 106,
            }

    class FakeMessage:
        role = "user"
        content = "query"

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    class FakeResponse:
        content = "done"
        reasoning_content = ""
        usage_metadata = FakeUsage()
        finish_reason = "stop"
        tool_calls = []

    observer = RecordingObserver()
    runtime_trace = AgentRuntimeTrace(
        observer=observer,
        run_id="run-jiuwen-cache-usage",
        session_id="sess-jiuwen-cache-usage",
        turn_id=1,
        caller="main",
    )
    bridge = _JiuwenModelTraceBridge(
        runtime_trace=runtime_trace,
        state={"run_id": "run-jiuwen-cache-usage"},
        caller="main",
        iteration_provider=lambda: 1,
        model_provider="openai_compatible",
        stream_enabled=True,
        enable_reasoning=False,
        sliding_window_applied=True,
    )
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_config=SimpleNamespace(model_name="compat-model")),
        inputs=SimpleNamespace(messages=[FakeMessage()], tools=[], response=FakeResponse()),
        exception=None,
    )

    asyncio.run(internal(bridge).on_before_model_call(ctx))
    asyncio.run(internal(bridge).on_after_model_call(ctx))

    llm_finished = [event for event in observer.events if event.type == RuntimeEventType.LLM_FINISHED]
    assert len(llm_finished) == 1
    usage = llm_finished[0].payload["usage"]
    assert usage["cache_hit_tokens"] == 6144
    assert usage["cache_miss_tokens"] == 106
    assert usage["cache_total_input_tokens"] == 6250
    assert usage["cache_hit_rate"] == 6144 / 6250


def test_jiuwen_model_trace_normalizes_usage_metadata_cache_tokens():
    from integration.observability import RuntimeEventType
    from integration.observability.traces.agent import AgentRuntimeTrace

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeUsage:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def model_dump(self):
            return {
                "input_tokens": 6250,
                "output_tokens": 8,
                "total_tokens": 6258,
                "cache_tokens": 6144,
            }

    class FakeMessage:
        role = "user"
        content = "query"

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    class FakeResponse:
        content = "done"
        reasoning_content = ""
        usage_metadata = FakeUsage()
        finish_reason = "stop"
        tool_calls = []

    observer = RecordingObserver()
    runtime_trace = AgentRuntimeTrace(
        observer=observer,
        run_id="run-jiuwen-cache-tokens",
        session_id="sess-jiuwen-cache-tokens",
        turn_id=1,
        caller="main",
    )
    bridge = _JiuwenModelTraceBridge(
        runtime_trace=runtime_trace,
        state={"run_id": "run-jiuwen-cache-tokens"},
        caller="main",
        iteration_provider=lambda: 1,
        model_provider="deepseek",
        stream_enabled=True,
        enable_reasoning=False,
        sliding_window_applied=True,
    )
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_config=SimpleNamespace(model_name="deepseek-v4-pro")),
        inputs=SimpleNamespace(messages=[FakeMessage()], tools=[], response=FakeResponse()),
        exception=None,
    )

    asyncio.run(internal(bridge).on_before_model_call(ctx))
    asyncio.run(internal(bridge).on_after_model_call(ctx))

    llm_finished = [event for event in observer.events if event.type == RuntimeEventType.LLM_FINISHED]
    assert len(llm_finished) == 1
    usage = llm_finished[0].payload["usage"]
    assert usage["prompt_tokens"] == 6250
    assert usage["cache_hit_tokens"] == 6144
    assert usage["cache_total_input_tokens"] == 6250
    assert usage["cache_hit_rate"] == 6144 / 6250


def test_jiuwen_model_trace_includes_runtime_config_api_params():
    from integration.observability import RuntimeEventType
    from integration.observability.traces.agent import AgentRuntimeTrace

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeMessage:
        role = "user"
        content = "query"

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    observer = RecordingObserver()
    runtime_trace = AgentRuntimeTrace(
        observer=observer,
        run_id="run-jiuwen-api-params",
        session_id="sess-jiuwen-api-params",
        turn_id=1,
        caller="main",
    )
    bridge = _JiuwenModelTraceBridge(
        runtime_trace=runtime_trace,
        state={"run_id": "run-jiuwen-api-params"},
        caller="main",
        iteration_provider=lambda: 1,
        model_provider="deepseek",
        stream_enabled=True,
        enable_reasoning=True,
        prompt_build_ms=17,
        system_prompt_chars=123,
        sliding_window_applied=True,
        llm_config=SimpleNamespace(
            base_url="https://deepseek.example.net/v1",
            temperature=0.2,
            max_tokens=1024,
            timeout=30,
        ),
    )
    ctx = SimpleNamespace(
        agent=SimpleNamespace(_config=SimpleNamespace(model_name="deepseek-model")),
        inputs=SimpleNamespace(messages=[FakeMessage()], tools=[]),
        exception=None,
    )

    asyncio.run(internal(bridge).on_before_model_call(ctx))

    llm_started = [event for event in observer.events if event.type == RuntimeEventType.LLM_STARTED]
    assert len(llm_started) == 1
    api_params = llm_started[0].payload["api_params"]
    assert api_params["model"] == "deepseek-model"
    assert api_params["base_url"] == "https://deepseek.example.net/v1"
    assert api_params["temperature"] == 0.2
    assert api_params["max_tokens"] == 1024
    assert api_params["timeout"] == 30
    assert api_params["enable_reasoning"] is True
    assert api_params["tools_count"] == 0
    assert api_params["request_label"] == "jiuwen_react_model_call"
    assert api_params["prompt_build_ms"] == 17
    assert api_params["system_prompt_chars"] == 123
    assert api_params["sliding_window_applied"] is True


def test_jiuwen_loop_engine_raises_when_agent_returns_error_result_type(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    observer = RecordingObserver()
    fake_agent = _FakeJiuwenAgent(
        {
            "output": "Max iterations reached without completion",
            "result_type": "error",
        }
    )
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-error",
        "session_id": "sess-jiuwen-error",
        "current_turn_id": 8,
        "user_request": "请回答",
        "messages": [],
        "results": {},
    }

    with pytest.raises(RuntimeError, match="Max iterations reached without completion"):
        _run_engine(engine, state, caller="main", observer=observer)

    assert "final_output" not in state
    assert state["messages"][0]["role"] == "system"
    assert state["messages"][1] == {"role": "user", "content": "请回答"}
    loop_finished = [event for event in observer.events if event.type == RuntimeEventType.AGENT_LOOP_FINISHED]
    assert loop_finished[-1].payload["status"] == RuntimeStatus.ERROR


def test_jiuwen_loop_engine_marks_loop_error_when_agent_raises(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FailingAgent:
        async def stream(self, inputs):
            del inputs
            if getattr(self, "_keep_async_generator", False):
                yield None
            raise RuntimeError("jiuwen exploded")

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    observer = RecordingObserver()
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FailingAgent(),
    )

    with pytest.raises(RuntimeError, match="jiuwen exploded"):
        _run_engine(
            engine,
            {
                "run_id": "run-jiuwen-agent-error",
                "session_id": "sess-jiuwen-agent-error",
                "current_turn_id": 9,
                "user_request": "fail",
                "messages": [],
                "results": {},
            },
            caller="main",
            observer=observer,
        )

    loop_finished = [event for event in observer.events if event.type == RuntimeEventType.AGENT_LOOP_FINISHED]
    assert loop_finished[-1].payload["status"] == RuntimeStatus.ERROR


def test_jiuwen_loop_engine_prepares_each_pseudo_iteration(monkeypatch):
    from integration.observability import RuntimeEventType

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}

        async def register_callback(self, event, callback, priority=100):
            del priority
            self.callbacks[event] = callback

        async def unregister(self, event, callback):
            del event, callback

    class FakeUsage:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def model_dump(self):
            return {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}

    class FakeResponse:
        content = "模型回答"
        reasoning_content = ""
        usage_metadata = FakeUsage()
        finish_reason = "stop"
        tool_calls = []

    class FakeMessage:
        role = "user"
        content = "query"

        def model_dump(self):
            return {"role": self.role, "content": self.content}

    captured = {}

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_config(self, purpose, profile_id=None, workspace_id=None):
            del purpose, profile_id, workspace_id
            return SimpleNamespace(provider="openai_compatible", model="model-a")

    class FakeAgent:
        def __init__(self):
            self.agent_callback_manager = FakeCallbackManager()
            self._config = SimpleNamespace(model_name="model-a")

        async def stream(self, inputs):
            del inputs
            for _ in range(2):
                ctx = SimpleNamespace(
                    agent=self,
                    inputs=SimpleNamespace(
                        messages=[FakeMessage()],
                        tools=[],
                        response=FakeResponse(),
                    ),
                    exception=None,
                )
                await self.agent_callback_manager.callbacks[FakeAgentCallbackEvent.BEFORE_MODEL_CALL](ctx)
                await self.agent_callback_manager.callbacks[FakeAgentCallbackEvent.AFTER_MODEL_CALL](ctx)
                if "iteration_event_callback" in captured:
                    captured["iteration_event_callback"](
                        "skill",
                        5,
                        {"started_at_ms": 100, "finished_at_ms": 105, "elapsed_ms": 5},
                    )
            yield SimpleNamespace(type="answer", payload={"output": "done", "result_type": "answer"})

    def fake_register_current_runtime_tools(*args, **kwargs):
        del args
        captured["iteration_event_callback"] = kwargs["iteration_event_callback"]
        return []

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", lambda **_kwargs: FakeAgent())
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", fake_register_current_runtime_tools)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_agent_callback_event_class",
        lambda: FakeAgentCallbackEvent,
    )

    observer = RecordingObserver()
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    _run_engine(
        engine,
        {
            "run_id": "run-jiuwen-pseudo-iterations",
            "session_id": "sess-jiuwen-pseudo-iterations",
            "current_turn_id": 10,
            "user_request": "iterate",
            "messages": [],
            "results": {},
        },
        caller="main",
        observer=observer,
    )

    prepared_events = [event for event in observer.events if event.type == RuntimeEventType.AGENT_ITERATION_PREPARED]
    prepared_iterations = [event.payload["iteration"] for event in prepared_events]
    finished_iterations = [
        event.payload["iteration"]
        for event in observer.events
        if event.type == RuntimeEventType.AGENT_ITERATION_FINISHED
    ]
    assert prepared_iterations == [1, 2, 3]
    assert finished_iterations == [1, 2, 3]
    assert len({event.payload["llm_message_count"] for event in prepared_events}) == 1
    assert len({event.payload["llm_input_chars"] for event in prepared_events}) == 1


def test_jiuwen_loop_engine_streams_llm_output_as_token_delta(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeStreamingJiuwenAgent:
        def __init__(self):
            self.inputs = None

        async def stream(self, inputs):
            self.inputs = inputs
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "处理中", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

        async def invoke(self, inputs):
            del inputs
            raise AssertionError("stream-capable Jiuwen agents should not fall back to invoke")

    fake_agent = FakeStreamingJiuwenAgent()
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-stream",
        "session_id": "sess-jiuwen-stream",
        "current_turn_id": 8,
        "user_request": "请流式回答",
        "messages": [],
        "results": {},
    }
    events = []
    snapshots = []

    _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
        snapshot_callback=snapshots.append,
    )

    token_events = [event["data"] for event in events if event["data"]["type"] == "token.delta"]
    assert fake_agent.inputs["query"].startswith("请流式回答\n\n<system-reminder>")
    assert "# currentDate" in fake_agent.inputs["query"]
    assert [event["source"] for event in token_events] == [
        {"node": "agent_loop", "component": "llm"},
        {"node": "agent_loop", "component": "llm"},
    ]
    assert [event["payload"] for event in token_events] == [
        {
            "channel": "content",
            "text": "处理中",
            "iteration": 1,
        },
        {
            "channel": "content",
            "text": "最终答案",
            "iteration": 1,
        },
    ]
    assert getitem(state, "final_output") == "最终答案"
    assert state["messages"][-1] == {"role": "assistant", "content": "最终答案"}


def test_jiuwen_loop_engine_streams_acp_output_as_token_delta(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "ACP处理中", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "ACP最终答案", "result_type": "answer"},
            )

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeStreamingJiuwenAgent(),
    )
    events = []

    _run_engine(
        engine,
        {
            "run_id": "run-jiuwen-acp-stream",
            "session_id": "sess-jiuwen-acp-stream",
            "current_turn_id": 8,
            "user_request": "请流式回答",
            "messages": [],
            "results": {},
        },
        caller="acp",
        event_writer=events.append,
    )

    token_events = [event["data"] for event in events if event["data"]["type"] == "token.delta"]
    assert [event["source"] for event in token_events] == [
        {"node": "acp", "component": "llm"},
        {"node": "acp", "component": "llm"},
    ]
    assert [event["payload"]["text"] for event in token_events] == [
        "ACP处理中",
        "ACP最终答案",
    ]


def test_jiuwen_loop_engine_does_not_duplicate_streamed_answer_chunk(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeDuplicateAnswerStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "最终答案", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeDuplicateAnswerStreamingJiuwenAgent(),
    )
    state = {
        "run_id": "run-jiuwen-duplicate-answer",
        "session_id": "sess-jiuwen-duplicate-answer",
        "current_turn_id": 9,
        "user_request": "请流式回答",
        "messages": [],
        "results": {},
    }
    events = []
    snapshots = []

    _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
        snapshot_callback=snapshots.append,
    )

    token_events = [event["data"] for event in events if event["data"]["type"] == "token.delta"]
    assert [event["payload"]["text"] for event in token_events] == ["最终答案"]
    assert getitem(state, "final_output") == "最终答案"


def test_jiuwen_loop_engine_streams_answer_chunk_as_content_delta(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeAnswerOnlyStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeAnswerOnlyStreamingJiuwenAgent(),
    )
    state = {
        "run_id": "run-jiuwen-answer-stream",
        "session_id": "sess-jiuwen-answer-stream",
        "current_turn_id": 9,
        "user_request": "请直接回答",
        "messages": [],
        "results": {},
    }
    events = []

    _run_engine(engine, state, caller="main", event_writer=events.append)

    token_events = [event["data"] for event in events if event["data"]["type"] == "token.delta"]
    assert len(token_events) == 1
    assert token_events[0]["payload"] == {
        "channel": "content",
        "text": "最终答案",
        "iteration": 1,
    }
    assert getitem(state, "final_output") == "最终答案"


def test_jiuwen_loop_engine_uses_last_user_message_when_user_request_missing(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.shared.messages.current_utc8_date_text", lambda: "2026/06/08")

    fake_agent = _FakeJiuwenAgent({"output": "from history", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-history",
        "session_id": "sess-jiuwen-history",
        "current_turn_id": 1,
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "history question"},
        ],
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    assert fake_agent.inputs["query"].startswith("history question\n\n<system-reminder>")
    assert "Today's date is 2026/06/08." in fake_agent.inputs["query"]


def test_jiuwen_loop_engine_seeds_persisted_history_into_new_context(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeMessage:
        role = ""

        def __init__(self, **kwargs):
            self.payload = {"role": self.role, **kwargs}

        def model_dump(self):
            return dict(self.payload)

    class FakeUserMessage(FakeMessage):
        role = "user"

    class FakeAssistantMessage(FakeMessage):
        role = "assistant"

    class FakeToolMessage(FakeMessage):
        role = "tool"

    class FakeSession:
        def __init__(self, session_id):
            self.session_id = session_id
            self.pre_run_inputs = None
            self.closed = False
            self.committed = False

        def get_session_id(self):
            return self.session_id

        async def pre_run(self, **kwargs):
            self.pre_run_inputs = kwargs.get("inputs")

        async def close_stream(self):
            self.closed = True

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_message_classes",
        lambda: {
            "user": FakeUserMessage,
            "assistant": FakeAssistantMessage,
            "tool": FakeToolMessage,
        },
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._create_jiuwen_agent_session",
        lambda agent, inputs: FakeSession(inputs["conversation_id"]),
    )

    class FakeContextEngine:
        def __init__(self):
            self.calls = []

        async def create_context(self, **kwargs):
            self.calls.append(kwargs)
            return SimpleNamespace(name="ctx")

    class FakeHistoryJiuwenAgent:
        card = SimpleNamespace(id="history-agent")

        def __init__(self):
            self.context_engine = FakeContextEngine()
            self.inputs = None
            self.session = None

        async def invoke(self, inputs, session=None):
            self.inputs = inputs
            self.session = session
            return {"output": "from restored history", "result_type": "answer"}

    fake_agent = FakeHistoryJiuwenAgent()
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: fake_agent,
    )
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(context_max_rounds=4),
    )
    state = {
        "run_id": "run-jiuwen-restored-history",
        "session_id": "sess-jiuwen-restored-history",
        "current_turn_id": 2,
        "user_request": "继续分析",
        "messages": [
            {"role": "system", "content": "old system prompt"},
            {"role": "user", "content": "查询 CHANGE_ORDER-001"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_change_order",
                        "type": "function",
                        "function": {
                            "name": "business_data_search",
                            "arguments": '{"query":"CHANGE_ORDER-001"}',
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_change_order",
                "name": "business_data_search",
                "content": "CHANGE_ORDER-001 查询结果",
            },
            {"role": "assistant", "content": "CHANGE_ORDER-001 已查询完成"},
            {"role": "user", "content": "继续分析"},
        ],
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    assert fake_agent.session is not None
    owned_session_id = fake_agent.session.get_session_id()
    assert owned_session_id.startswith("history-agent.session.")
    assert owned_session_id != "sess-jiuwen-restored-history"
    assert fake_agent.session.pre_run_inputs["conversation_id"] == owned_session_id
    assert fake_agent.session.closed is True
    assert fake_agent.session.committed is True
    assert len(fake_agent.context_engine.calls) == 1
    history_messages = fake_agent.context_engine.calls[0]["history_messages"]
    dumped = [message.model_dump() if hasattr(message, "model_dump") else vars(message) for message in history_messages]
    assert [message["role"] for message in dumped] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]
    assert dumped[0]["content"] == "查询 CHANGE_ORDER-001"
    assert dumped[1]["tool_calls"][0]["function"]["name"] == "business_data_search"
    assert dumped[2]["tool_call_id"] == "call_change_order"
    assert dumped[-1]["content"] == "CHANGE_ORDER-001 已查询完成"
    assert "old system prompt" not in [message.get("content") for message in dumped]
    assert [message.get("content") for message in dumped].count("继续分析") == 0
    assert fake_agent.inputs["query"].startswith("继续分析\n\n<system-reminder>")
    assert getitem(state, "final_output") == "from restored history"


def test_suppressed_solver_turn_removes_the_complete_solver_history_turn():
    from agent.core.jiuwen_engine import _build_jiuwen_history_source_messages
    from agent.core.shared.solver_route_guard import bind_solver_route_requirement

    state = {
        "user_request": "【合成测试数据】请统计资源负荷和容量校验结果。",
        "user_context": bind_solver_route_requirement(
            {},
            None,
            suppress_solver_tools=True,
        ),
        "messages": [
            {"role": "system", "content": "historical system prompt"},
            {"role": "user", "content": "【合成测试数据】请生成候选方案。"},
            {
                "role": "assistant",
                "content": "正在调用求解工具。",
                "tool_calls": [
                    {
                        "id": "solver-call",
                        "type": "function",
                        "function": {
                            "name": "solve_synthetic_route_aaaaaaaa",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "solver-call",
                "name": "solve_synthetic_route_aaaaaaaa",
                "content": "synthetic authoritative result",
            },
            {"role": "assistant", "content": "合成结果摘要。"},
            {"role": "user", "content": "Read ordinary synthetic data."},
            {
                "role": "assistant",
                "content": "正在读取普通业务数据。",
                "tool_calls": [
                    {
                        "id": "script-call",
                        "type": "function",
                        "function": {
                            "name": "execute_script",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "script-call",
                "name": "execute_script",
                "content": "synthetic script result",
            },
            {
                "role": "user",
                "content": "【合成测试数据】请统计资源负荷和容量校验结果。",
            },
        ],
    }

    history = _build_jiuwen_history_source_messages(state)

    assert [message.get("name") for message in history] == [None, None, "execute_script"]
    assert history[0]["content"] == "Read ordinary synthetic data."
    assert all(message.get("tool_call_id") != "solver-call" for message in history)
    assert all(
        all(
            call.get("function", {}).get("name") != "solve_synthetic_route_aaaaaaaa"
            for call in message.get("tool_calls", [])
        )
        for message in history
    )
    assert history[-1]["content"] == "synthetic script result"


def test_jiuwen_loop_engine_finalizes_seeded_session_after_stream(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeMessage:
        role = ""

        def __init__(self, **kwargs):
            self.payload = {"role": self.role, **kwargs}

    class FakeUserMessage(FakeMessage):
        role = "user"

    class FakeAssistantMessage(FakeMessage):
        role = "assistant"

    class FakeToolMessage(FakeMessage):
        role = "tool"

    class FakeSession:
        def __init__(self, session_id):
            self.session_id = session_id
            self.closed = False
            self.committed = False

        def get_session_id(self):
            return self.session_id

        async def pre_run(self, **kwargs):
            del kwargs

        async def close_stream(self):
            self.closed = True

        async def commit(self):
            self.committed = True

    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_message_classes",
        lambda: {
            "user": FakeUserMessage,
            "assistant": FakeAssistantMessage,
            "tool": FakeToolMessage,
        },
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._create_jiuwen_agent_session",
        lambda agent, inputs: FakeSession(inputs["conversation_id"]),
    )

    class FakeContextEngine:
        def __init__(self):
            self.created_sessions = []
            self.saved_sessions = []

        async def create_context(self, **kwargs):
            self.created_sessions.append(kwargs["session"])

        async def save_contexts(self, session):
            self.saved_sessions.append(session)

    class FakeStreamHistoryAgent:
        card = SimpleNamespace(id="history-stream-agent")

        def __init__(self):
            self.context_engine = FakeContextEngine()
            self.session = None

        async def stream(self, inputs, session=None):
            del inputs
            self.session = session
            yield SimpleNamespace(
                type="answer",
                payload={"output": "stream restored answer", "result_type": "answer"},
            )

    fake_agent = FakeStreamHistoryAgent()
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: fake_agent,
    )
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(context_max_rounds=4),
    )
    state = {
        "run_id": "run-jiuwen-stream-history",
        "session_id": "sess-jiuwen-stream-history",
        "current_turn_id": 2,
        "user_request": "继续",
        "messages": [
            {"role": "user", "content": "上一轮问题"},
            {"role": "assistant", "content": "上一轮答案"},
            {"role": "user", "content": "继续"},
        ],
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    assert fake_agent.session is not None
    assert fake_agent.context_engine.created_sessions == [fake_agent.session]
    assert fake_agent.context_engine.saved_sessions == [fake_agent.session]
    assert fake_agent.session.closed is False
    assert fake_agent.session.committed is False


def test_jiuwen_loop_engine_appends_current_date_after_selected_ontology_context(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.shared.messages.current_utc8_date_text", lambda: "2026/06/08")
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_context",
        lambda selected_ontology_id=None, **_kwargs: "<Selected-Ontology>\nid: {}</Selected-Ontology>".format(
            selected_ontology_id
        ),
        raising=False,
    )

    fake_agent = _FakeJiuwenAgent({"output": "dated selected answer", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-date-selected",
        "session_id": "sess-jiuwen-date-selected",
        "current_turn_id": 10,
        "user_request": "CHANGE_ORDER-001变更涉及哪些物料变更？",
        "user_context": {"selected_ontology_id": "example-domain-knowledge-agent"},
        "messages": [],
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    query = fake_agent.inputs["query"]
    assert query.startswith("CHANGE_ORDER-001变更涉及哪些物料变更？")
    assert "<Ontology-Selection-Constraint>" in query
    assert "<Selected-Ontology>" in query
    assert query.index("<Ontology-Selection-Constraint>") < query.index("<system-reminder>")
    assert query.index("<Selected-Ontology>") < query.index("<system-reminder>")
    assert "Today's date is 2026/06/08." in query


def test_suppressed_solver_turn_appends_current_turn_provenance_to_query(monkeypatch):
    from agent.core.shared.solver_route_guard import bind_solver_route_requirement

    monkeypatch.setattr(
        "agent.core.shared.messages.current_utc8_date_text",
        lambda: "2026/06/08",
    )
    engine = object.__new__(JiuwenReactLoopEngine)

    def append_selected_ontology_context(_state, query):
        return query + "\n<Selected-Ontology>synthetic</Selected-Ontology>"

    internal(engine).append_selected_ontology_context_to_query = append_selected_ontology_context
    state = {
        "session_id": "session-synthetic",
        "user_request": "【合成测试数据】请统计资源负荷和容量校验结果。",
        "user_context": bind_solver_route_requirement(
            {},
            None,
            suppress_solver_tools=True,
        ),
    }

    query = internal(engine).build_inputs(state)["query"]

    assert "<Current-Turn-Tool-Provenance>" in query
    assert "Only the latest user message defines the active request." in query
    assert "Do not mention unavailable or omitted tools" in query
    assert "Solver tools are unavailable" not in query
    assert query.index("<Current-Turn-Tool-Provenance>") < query.index("<system-reminder>")


def test_jiuwen_loop_engine_injects_selected_ontology_context_into_inputs(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_context",
        lambda selected_ontology_id=None, **_kwargs: (
            "<Selected-Ontology>\nid: {}\noperations_catalog:\n- 变更影响分析\n</Selected-Ontology>"
        ).format(selected_ontology_id),
        raising=False,
    )

    fake_agent = _FakeJiuwenAgent({"output": "selected ontology answer", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-selected-ontology",
        "session_id": "sess-jiuwen-selected-ontology",
        "current_turn_id": 10,
        "user_request": "CHANGE_ORDER-001变更涉及哪些物料变更？",
        "user_context": {"selected_ontology_id": "example-domain-knowledge-agent"},
        "messages": [],
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    query = fake_agent.inputs["query"]
    assert query.startswith("CHANGE_ORDER-001变更涉及哪些物料变更？")
    assert 'Ontology "example-domain-knowledge-agent" is the active ontology for this session.' in query
    assert 'Always use ontology_id "example-domain-knowledge-agent"' in query
    assert "<Selected-Ontology>" in query
    assert "id: example-domain-knowledge-agent" in query
    assert [message["role"] for message in state["messages"]] == ["system", "user", "assistant"]
    assert "Selected-Ontology" not in state["messages"][1]["content"]


def test_jiuwen_loop_engine_falls_back_to_sole_enabled_ontology(monkeypatch):
    """Verify the sole enabled ontology fallback."""
    # The preload context, including its value-domain hint, must use the same
    # constraint wording without writing the selection back into state.
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_sole_enabled_ontology_id",
        lambda workspace_id=None: "sole-ontology",
    )
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_context",
        lambda selected_ontology_id=None, **_kwargs: (
            "<Selected-Ontology>\nid: {}\nvalue_domain_hint: demo\n</Selected-Ontology>"
        ).format(selected_ontology_id),
        raising=False,
    )

    fake_agent = _FakeJiuwenAgent({"output": "sole ontology answer", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-sole-ontology",
        "session_id": "sess-jiuwen-sole-ontology",
        "current_turn_id": 1,
        "workspace_id": "ws-sole",
        "user_request": "KUL是什么意思",
        "user_context": {},
        "messages": [],
        "results": {},
    }

    asyncio.run(engine.arun(state, caller="main"))

    query = fake_agent.inputs["query"]
    assert query.startswith("KUL是什么意思")
    assert "<Ontology-Selection-Constraint>" in query
    assert 'Ontology "sole-ontology" is the active ontology for this session.' in query
    assert 'Always use ontology_id "sole-ontology"' in query
    assert "<Selected-Ontology>" in query
    assert "value_domain_hint: demo" in query
    # The fallback must not claim an explicit user selection...
    assert "The user explicitly selected" not in query
    # ...and must not mutate the session-level selection state.
    assert "selected_ontology_id" not in state["user_context"]


def test_jiuwen_checkpoint_tool_requires_explicit_ontology_selection(monkeypatch):
    from agent.core.shared.tool_context import build_tool_context
    from agent.tools.registry import TOOL_SPECS

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_sole_enabled_ontology_id",
        lambda workspace_id=None: "sole-ontology",
    )
    monkeypatch.setattr(
        "ontology.capabilities.is_business_checkpoint_available",
        lambda *_args, **_kwargs: True,
    )
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())

    def tool_names(state):
        schemas = internal(engine).tools_schema_for_state(state, caller="main")
        tool_infos = internal(engine).jiuwen_tool_infos_for_state(state, caller="main")
        schema_names = {item["function"]["name"] for item in schemas}
        jiuwen_names = {item["name"] for item in tool_infos}
        return schema_names, jiuwen_names

    fallback_state = {"workspace_id": "demo", "user_context": {}}
    schema_names, jiuwen_names = tool_names(fallback_state)
    assert "report_business_checkpoint" not in schema_names
    assert "report_business_checkpoint" not in jiuwen_names

    fallback_context = build_tool_context(
        fallback_state,
        memory_manager=None,
        agent_config=_agent_config(),
        caller="main",
        agent_iteration=1,
        workspace_id="demo",
        project_root="",
    )
    assert fallback_context.selected_ontology_id == "sole-ontology"
    assert fallback_context.selected_ontology_is_explicit is False
    cached_outcome = TOOL_SPECS["report_business_checkpoint"].execute(
        args={
            "stage": "synthetic_stage",
            "stage_title": "Synthetic stage",
            "phase": "synthetic_stage.started",
            "phase_title": "Synthetic stage started",
            "summary": "Synthetic checkpoint payload.",
        },
        context=fallback_context,
        tool_call_id="cached-checkpoint-tool",
    )
    assert "disabled" in cached_outcome.content

    selected_state = {
        "workspace_id": "demo",
        "user_context": {"selected_ontology_id": "sole-ontology"},
    }
    schema_names, jiuwen_names = tool_names(selected_state)
    assert "report_business_checkpoint" in schema_names
    assert "report_business_checkpoint" in jiuwen_names

    selected_context = build_tool_context(
        selected_state,
        memory_manager=None,
        agent_config=_agent_config(),
        caller="main",
        agent_iteration=1,
        workspace_id="demo",
        project_root="",
    )
    assert selected_context.selected_ontology_id == "sole-ontology"
    assert selected_context.selected_ontology_is_explicit is True


def test_active_ontology_version_mirrors_the_sole_enabled_fallback(monkeypatch):
    """A session that never pinned a revision still gets one, and a session."""
    # Details: A session that never pinned a revision still gets one, and a session that did pin one is never silently
    # upgraded to the active version.
    from agent.core.shared.ontology_context import active_ontology_version_from_state

    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_sole_enabled_ontology_id",
        lambda workspace_id=None: "sole-ontology",
    )
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_active_ontology_version",
        lambda ontology_id, workspace_id=None: (
            "2.1.0" if ontology_id == "sole-ontology" else None
        ),
    )

    auto_selected = {"workspace_id": "ws-version", "user_context": {}}
    assert active_ontology_version_from_state(auto_selected) == "2.1.0"

    explicit_id_only = {
        "workspace_id": "ws-version",
        "user_context": {"selected_ontology_id": "sole-ontology"},
    }
    assert active_ontology_version_from_state(explicit_id_only) == "2.1.0"

    version_locked = {
        "workspace_id": "ws-version",
        "user_context": {
            "selected_ontology_id": "sole-ontology",
            "selected_ontology_version": "1.0.0",
        },
    }
    assert active_ontology_version_from_state(version_locked) == "1.0.0"

    # Zero or several enabled packages → no unambiguous package, no revision.
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_sole_enabled_ontology_id",
        lambda workspace_id=None: None,
    )
    assert active_ontology_version_from_state(auto_selected) is None


def test_auto_selected_package_still_exposes_solver_cards(monkeypatch):
    """Solver cards are revision-bound, so an auto-selected package used to."""
    # Details: Solver cards are revision-bound, so an auto-selected package used to lose them entirely; the resolved
    # active revision must reach both the tool schema and the ToolContext.
    import agent.tools.registry as tool_registry
    from agent.core.shared.tool_context import build_tool_context
    from agent.tools.solver import build_solver_tool_specs

    (solver_spec,) = build_solver_tool_specs(
        [
            {
                "package_id": "sole-ontology",
                "package_revision": "2.1.0",
                "operation_key": "solve_synthetic_route",
                "decision_model_id": "model-synthetic",
                "problem_family": "synthetic_family.v1",
                "input_schema": {
                    "type": "object",
                    "properties": {"item_count": {"type": "integer", "minimum": 1}},
                    "required": ["item_count"],
                    "additionalProperties": False,
                },
                "result_schema": {"type": "object"},
                "contract_digest": "sha256:" + "b" * 64,
                "validator_revision": "verifier-synthetic-1",
                "enabled": True,
            }
        ]
    )

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_sole_enabled_ontology_id",
        lambda workspace_id=None: "sole-ontology",
    )
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_active_ontology_version",
        lambda ontology_id, workspace_id=None: (
            "2.1.0" if ontology_id == "sole-ontology" else None
        ),
    )
    monkeypatch.setattr(
        tool_registry,
        "get_solver_tool_specs_for_context",
        lambda **kwargs: (solver_spec,)
        if kwargs
        == {
            "selected_ontology_id": "sole-ontology",
            "selected_ontology_version": "2.1.0",
            "workspace_id": "ws-solver",
        }
        else (),
    )

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {"workspace_id": "ws-solver", "user_context": {}}

    schema_names = {
        item["function"]["name"]
        for item in internal(engine).tools_schema_for_state(state, caller="main")
    }
    jiuwen_names = {
        item["name"] for item in internal(engine).jiuwen_tool_infos_for_state(state, caller="main")
    }
    assert solver_spec.name in schema_names
    assert solver_spec.name in jiuwen_names

    context = build_tool_context(
        state,
        memory_manager=None,
        agent_config=_agent_config(),
        caller="main",
        agent_iteration=1,
        workspace_id="ws-solver",
        project_root="",
    )
    assert context.selected_ontology_id == "sole-ontology"
    assert context.selected_ontology_version == "2.1.0"
    # The revision is resolved for this run only; the session made no choice.
    assert context.selected_ontology_is_explicit is False
    assert "selected_ontology_version" not in state["user_context"]


def test_jiuwen_loop_engine_no_fallback_with_multiple_ontologies(monkeypatch):
    """No explicit selection + multiple (or zero) enabled ontologies → the."""
    # Details: No explicit selection + multiple (or zero) enabled ontologies → the query is returned unchanged.
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.get_sole_enabled_ontology_id",
        lambda workspace_id=None: None,
    )

    fake_agent = _FakeJiuwenAgent({"output": "direct answer", "result_type": "answer"})
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-multi-ontology",
        "session_id": "sess-jiuwen-multi-ontology",
        "current_turn_id": 1,
        "workspace_id": "ws-multi",
        "user_request": "KUL是什么意思",
        "user_context": {},
        "messages": [],
        "results": {},
    }

    asyncio.run(engine.arun(state, caller="main"))

    query = fake_agent.inputs["query"]
    assert query.startswith("KUL是什么意思")
    assert "<Ontology-Selection-Constraint>" not in query
    assert "<Selected-Ontology>" not in query


def test_jiuwen_loop_engine_registers_selected_ontology_preload_records(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_context",
        lambda *args, **kwargs: "<Selected-Ontology>\n- selected ontology\n</Selected-Ontology>",
        raising=False,
    )
    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_records",
        lambda *args, **kwargs: [
            {
                "path": "ontologies/demo/objects/index.md",
                "normalized_path": "ontologies/demo/objects/index.md",
                "doc_type": "objects_index",
                "ontology_id": "demo",
                "source": "preload",
                "turn_id": 12,
                "iteration": None,
                "content_hash": "abc123",
                "size_bytes": 12,
                "mtime_ns": None,
            }
        ],
        raising=False,
    )

    observed = {}

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            observed["tool_by_name"] = {item.card.name: item for item in tools}

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            observed["ability_cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-selected-read-docs")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()

        async def invoke(self, inputs, session=None):
            del inputs, session
            await observed["tool_by_name"]["ontology_context"].invoke({"resource": "root"})
            return {"output": "done", "result_type": "answer"}

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        observed["tool_context_read_documents"] = context.read_documents
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "ontology_context",
                }
            ],
        )

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: FakeAgent(),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-jiuwen-preloaded-ledger",
        "session_id": "sess-jiuwen-preloaded-ledger",
        "current_turn_id": 12,
        "user_request": "分析这次变更影响",
        "user_context": {"selected_ontology_id": "demo-ontology"},
        "messages": [],
        "results": {},
    }

    final_state = _run_engine(engine, state, caller="main")

    assert final_state["read_documents"]["by_path"]["ontologies/demo/objects/index.md"]["source"] == "preload"
    assert final_state["read_documents"]["recent_paths"] == ["ontologies/demo/objects/index.md"]
    assert observed["tool_context_read_documents"]["by_path"]["ontologies/demo/objects/index.md"]["source"] == "preload"
    assert final_state["final_output"] == "done"


def test_jiuwen_loop_engine_registers_current_tools_for_default_agent(monkeypatch):
    from integration.observability import NOOP_OBSERVER

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.shared.prompting.build_ontologies_section",
        lambda selected_ontology_id=None, **_kwargs: (
            "<Available-Ontologies>\n- {}: selected\n</Available-Ontologies>".format(selected_ontology_id or "all")
        ),
        raising=False,
    )
    monkeypatch.setattr(
        "agent.core.shared.prompting.build_skills_section",
        lambda **_kwargs: "<Available-Skills></Available-Skills>",
        raising=False,
    )

    fake_agent = _FakeJiuwenAgent({"output": "registered"})
    calls = {}

    def fake_create_default_jiuwen_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
        calls["llm_config"] = llm_config
        calls["max_iterations"] = max_iterations
        calls["system_prompt"] = system_prompt
        return fake_agent

    def fake_register(
        agent,
        *,
        state,
        context_factory,
        tool_infos=None,
        tool_batch_executor=None,
        iteration_event_callback=None,
        tool_batch_started_callback=None,
        tool_batch_failed_callback=None,
        terminal_tool_batch_callback=None,
        state_snapshot_callback=None,
        terminal_error_callback=None,
    ):
        calls["agent"] = agent
        calls["state"] = state
        calls["context"] = context_factory()
        calls["tool_infos"] = tool_infos
        calls["tool_batch_executor"] = tool_batch_executor
        calls["iteration_event_callback"] = iteration_event_callback
        calls["tool_batch_started_callback"] = tool_batch_started_callback
        calls["tool_batch_failed_callback"] = tool_batch_failed_callback
        calls["terminal_tool_batch_callback"] = terminal_tool_batch_callback
        calls["state_snapshot_callback"] = state_snapshot_callback
        calls["terminal_error_callback"] = terminal_error_callback
        return []

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", fake_register)
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-register",
        "session_id": "sess-register",
        "current_turn_id": 3,
        "user_request": "needs a tool",
        "messages": [],
        "results": {},
        "user_context": {
            "agent_mode": "multi_agent",
            "selected_ontology_id": "demo-ontology",
        },
    }

    _run_engine(
        engine,
        state,
        caller="main",
        iteration_limit=50,
        tool_event_emitter=lambda *_args: None,
    )

    assert calls["agent"] is fake_agent
    assert calls["max_iterations"] == 50
    assert "Ontology-Driven Intelligent Agent" in calls["system_prompt"]
    assert "- demo-ontology: selected" in calls["system_prompt"]
    assert "spawn_sub_agent" in calls["system_prompt"]
    assert calls["state"] is state
    assert calls["context"].caller == "main"
    assert calls["context"].agent_mode == "multi_agent"
    assert calls["context"].run_id == "run-register"
    assert calls["context"].observer is NOOP_OBSERVER
    assert callable(calls["iteration_event_callback"])
    assert callable(calls["terminal_error_callback"])
    tool_names = [info["name"] for info in calls["tool_infos"]]
    assert "spawn_sub_agent" in tool_names


def test_jiuwen_loop_engine_uses_selected_model_profile_for_default_agent(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_current_workspace_id", lambda: "ws-selected-profile")
    monkeypatch.setattr(
        "agent.core.shared.prompting.build_ontologies_section",
        lambda selected_ontology_id=None, **_kwargs: "<Available-Ontologies></Available-Ontologies>",
        raising=False,
    )
    monkeypatch.setattr(
        "agent.core.shared.prompting.build_skills_section",
        lambda **_kwargs: "<Available-Skills></Available-Skills>",
        raising=False,
    )

    fake_agent = _FakeJiuwenAgent({"output": "selected model answer"})
    calls = {}

    def fake_create_default_jiuwen_agent(*, llm_config, max_iterations, system_prompt=None, **kwargs):
        del max_iterations, system_prompt
        calls["llm_config"] = llm_config
        calls["stream_enabled"] = kwargs.get("stream_enabled")
        return fake_agent

    def fake_register(
        agent,
        *,
        state,
        context_factory,
        tool_infos=None,
        tool_batch_executor=None,
        iteration_event_callback=None,
        tool_batch_started_callback=None,
        tool_batch_failed_callback=None,
        terminal_tool_batch_callback=None,
        state_snapshot_callback=None,
        terminal_error_callback=None,
    ):
        del (
            agent,
            state,
            context_factory,
            tool_infos,
            tool_batch_executor,
            iteration_event_callback,
            tool_batch_started_callback,
            tool_batch_failed_callback,
            terminal_tool_batch_callback,
            state_snapshot_callback,
            terminal_error_callback,
        )
        return []

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_config(self, purpose, profile_id=None, workspace_id=None):
            calls["purpose"] = purpose
            calls["profile_id"] = profile_id
            calls["workspace_id"] = workspace_id
            return SimpleNamespace(model="accurate-model", profile_id=profile_id)

    monkeypatch.setattr(
        "config.settings.AppConfig.from_env",
        lambda: SimpleNamespace(
            llm=SimpleNamespace(model="default-model"),
            project_root="/private/tmp/onto-agent-jiuwen-selected-profile-test",
        ),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", fake_register)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    _run_engine(
        engine,
        {
            "run_id": "run-selected-profile",
            "session_id": "sess-selected-profile",
            "current_turn_id": 5,
            "user_request": "use selected model",
            "messages": [],
            "results": {},
            "user_context": {
                "agent_mode": "quick",
                "model_profile_id": "accurate",
            },
        },
        caller="main",
    )

    assert calls["llm_config"].model == "accurate-model"
    assert calls["llm_config"].profile_id == "accurate"
    assert calls["stream_enabled"] is True
    assert calls["purpose"] == LLMPurpose.MAIN
    assert calls["profile_id"] == "accurate"
    assert calls["workspace_id"] == "ws-selected-profile"


def test_jiuwen_loop_engine_clamps_profile_max_tokens_to_platform_output_limit(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_current_workspace_id", lambda: "ws-output-limit")

    fake_agent = _FakeJiuwenAgent({"output": "limited output"})
    calls = {}

    def fake_create_default_jiuwen_agent(*, llm_config, max_iterations, system_prompt=None, agent_id=None, **_kwargs):
        del max_iterations, system_prompt, agent_id
        calls["llm_config"] = llm_config
        return fake_agent

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_config(self, purpose, profile_id=None, workspace_id=None):
            del purpose, profile_id, workspace_id
            return SimpleNamespace(
                model="large-output-model",
                max_tokens=4096,
                temperature=0.2,
            )

    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", lambda *_args, **_kwargs: [])

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(
            limits=LimitsConfig(model_output_token_limit=128),
        )
    )
    _run_engine(
        engine,
        {
            "run_id": "run-output-limit",
            "session_id": "sess-output-limit",
            "current_turn_id": 5,
            "user_request": "use bounded output",
            "messages": [],
            "results": {},
        },
        caller="main",
    )

    assert calls["llm_config"].max_tokens == 128


def test_jiuwen_loop_engine_enables_reasoning_for_thinking_modes(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_current_workspace_id", lambda: "ws-reasoning-mode")

    calls = []

    def fake_create_default_jiuwen_agent(
        *,
        llm_config,
        max_iterations,
        system_prompt=None,
        agent_id=None,
        enable_reasoning=False,
        **_kwargs,
    ):
        del llm_config, max_iterations, system_prompt, agent_id
        calls.append(bool(enable_reasoning))
        return _FakeJiuwenAgent({"output": "mode answer"})

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", lambda *_args, **_kwargs: [])

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    for mode in ("quick", "thinking", "multi_agent"):
        _run_engine(
            engine,
            {
                "run_id": "run-reasoning-{}".format(mode),
                "session_id": "sess-reasoning-{}".format(mode),
                "current_turn_id": 5,
                "user_request": "mode request",
                "messages": [],
                "results": {},
                "user_context": {"agent_mode": mode},
            },
            caller="main",
        )

    assert calls == [False, True, True]


def test_jiuwen_loop_engine_uses_subagent_purpose_for_subagent_caller(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_current_workspace_id", lambda: "ws-subagent-profile")

    fake_agent = _FakeJiuwenAgent({"output": "subagent model answer"})
    calls = {}

    def fake_create_default_jiuwen_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
        del system_prompt
        calls["llm_config"] = llm_config
        calls["max_iterations"] = max_iterations
        return fake_agent

    def fake_register(
        agent,
        *,
        state,
        context_factory,
        tool_infos=None,
        tool_batch_executor=None,
        iteration_event_callback=None,
        tool_batch_started_callback=None,
        tool_batch_failed_callback=None,
        terminal_tool_batch_callback=None,
        state_snapshot_callback=None,
        terminal_error_callback=None,
    ):
        del (
            agent,
            state,
            context_factory,
            tool_infos,
            tool_batch_executor,
            iteration_event_callback,
            tool_batch_started_callback,
            tool_batch_failed_callback,
            terminal_tool_batch_callback,
            state_snapshot_callback,
            terminal_error_callback,
        )
        return []

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_config(self, purpose, profile_id=None, workspace_id=None):
            calls["purpose"] = purpose
            calls["profile_id"] = profile_id
            calls["workspace_id"] = workspace_id
            return SimpleNamespace(model="subagent-model", profile_id=profile_id)

    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", fake_register)

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(
            main_agent_max_iterations=17,
            sub_agent_max_iterations=3,
        )
    )
    _run_engine(
        engine,
        {
            "run_id": "run-subagent-selected-profile",
            "session_id": "sess-subagent-selected-profile",
            "current_turn_id": 6,
            "user_request": "use selected subagent model",
            "messages": [],
            "results": {},
            "user_context": {
                "agent_mode": "quick",
                "model_profile_id": "accurate",
            },
        },
        caller="subagent",
        system_prompt="subagent system prompt",
    )

    assert calls["llm_config"].model == "subagent-model"
    assert calls["purpose"] == LLMPurpose.SUBAGENT
    assert calls["profile_id"] == "accurate"
    assert calls["workspace_id"] == "ws-subagent-profile"
    assert calls["max_iterations"] == 3


def test_jiuwen_loop_engine_routes_stream_tokens_to_token_callback(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_reasoning",
                payload={"content": "先分析", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "处理中", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeStreamingJiuwenAgent(),
    )
    events = []
    callbacks = []

    _run_engine(
        engine,
        {
            "run_id": "run-token-callback",
            "session_id": "sess-token-callback",
            "current_turn_id": 5,
            "user_request": "callback request",
            "messages": [],
            "results": {},
            "user_context": {"agent_mode": "thinking"},
        },
        caller="main",
        event_writer=events.append,
        token_callback=lambda token_type, token, meta: callbacks.append((token_type, token, meta)),
    )

    assert callbacks[0][0:2] == ("reasoning", "先分析")
    assert callbacks[0][2]["reasoning_block_id"].startswith("reasoning_")
    assert callbacks[0][2]["iteration"] == 1
    assert callbacks[1:] == [
        ("content", "处理中", {"iteration": 1}),
        ("content", "最终答案", {"iteration": 1}),
    ]
    assert [event["data"]["type"] for event in events if event["data"]["type"] == "token.delta"] == []


def test_jiuwen_loop_engine_preserves_streamed_reasoning_content_on_assistant_message(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeReasoningStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_reasoning",
                payload={"content": "先判断是否需要查询。", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="llm_reasoning",
                payload={"content": "再组织答案。", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeReasoningStreamingJiuwenAgent(),
    )
    state = {
        "run_id": "run-reasoning-content",
        "session_id": "sess-reasoning-content",
        "current_turn_id": 5,
        "user_request": "reasoning request",
        "messages": [],
        "results": {},
        "user_context": {"agent_mode": "thinking"},
    }

    _run_engine(engine, state, caller="main")

    assert state["messages"][-1] == {
        "role": "assistant",
        "content": "最终答案",
        "reasoning_content": "先判断是否需要查询。再组织答案。",
    }


def test_jiuwen_loop_engine_emits_reasoning_summary_for_streamed_reasoning(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeSummaryService:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def _dispatch_reasoning_summary(
            self,
            *,
            reasoning_text,
            reasoning_block_id,
            run_id,
            reasoning_summary_callback,
        ):
            assert reasoning_text == "先判断是否需要查询。"
            assert reasoning_block_id.startswith("reasoning_")
            assert run_id == "run-reasoning-summary"
            reasoning_summary_callback(reasoning_block_id, "准备查询数据")
            return True

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_summary_service(self):
            return FakeSummaryService()

    class FakeReasoningStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_reasoning",
                payload={"content": "先判断是否需要查询。", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "最终答案", "result_type": "answer"},
            )

    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeReasoningStreamingJiuwenAgent(),
    )
    events = []

    _run_engine(
        engine,
        {
            "run_id": "run-reasoning-summary",
            "session_id": "sess-reasoning-summary",
            "current_turn_id": 5,
            "user_request": "reasoning request",
            "messages": [],
            "results": {},
            "user_context": {"agent_mode": "thinking"},
        },
        caller="main",
        event_writer=events.append,
    )

    token_event = next(event["data"] for event in events if event["data"]["type"] == "token.delta")
    summary_event = next(event["data"] for event in events if event["data"]["type"] == "reasoning.summary")
    assert token_event["payload"]["reasoning_block_id"].startswith("reasoning_")
    assert summary_event["payload"] == {
        "channel": "reasoning",
        "summary": "准备查询数据",
        "reasoning_block_id": token_event["payload"]["reasoning_block_id"],
        "iteration": 1,
    }


def test_jiuwen_loop_engine_emits_acp_reasoning_summary(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeSummaryService:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def _dispatch_reasoning_summary(
            self,
            *,
            reasoning_text,
            reasoning_block_id,
            run_id,
            reasoning_summary_callback,
        ):
            assert reasoning_text == "ACP先思考。"
            assert reasoning_block_id.startswith("reasoning_")
            assert run_id == "run-acp-reasoning-summary"
            reasoning_summary_callback(reasoning_block_id, "ACP摘要")
            return True

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_summary_service(self):
            return FakeSummaryService()

    class FakeReasoningStreamingJiuwenAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_reasoning",
                payload={"content": "ACP先思考。", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "ACP最终答案", "result_type": "answer"},
            )

    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: FakeReasoningStreamingJiuwenAgent(),
    )
    events = []

    _run_engine(
        engine,
        {
            "run_id": "run-acp-reasoning-summary",
            "session_id": "sess-acp-reasoning-summary",
            "current_turn_id": 5,
            "user_request": "reasoning request",
            "messages": [],
            "results": {},
            "user_context": {"agent_mode": "thinking"},
        },
        caller="acp",
        event_writer=events.append,
    )

    token_event = next(event["data"] for event in events if event["data"]["type"] == "token.delta")
    summary_event = next(event["data"] for event in events if event["data"]["type"] == "reasoning.summary")
    assert token_event["source"] == {"node": "acp", "component": "llm"}
    assert summary_event["source"] == {"node": "acp", "component": "llm"}
    assert summary_event["payload"] == {
        "channel": "reasoning",
        "summary": "ACP摘要",
        "reasoning_block_id": token_event["payload"]["reasoning_block_id"],
        "iteration": 1,
    }


def test_jiuwen_loop_engine_uses_invoke_when_request_mode_is_sync(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    class FakeStreamCapableJiuwenAgent:
        def __init__(self):
            self.invoked = False

        async def stream(self, inputs):
            del inputs
            raise AssertionError("sync request mode must not use Jiuwen stream()")

        async def invoke(self, inputs):
            del inputs
            self.invoked = True
            return {"output": "sync answer", "result_type": "answer"}

    fake_agent = FakeStreamCapableJiuwenAgent()
    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(),
        agent_factory=lambda: fake_agent,
    )
    state = {
        "run_id": "run-jiuwen-sync-mode",
        "session_id": "sess-jiuwen-sync-mode",
        "current_turn_id": 5,
        "user_request": "sync request",
        "messages": [],
        "results": {},
        "user_context": {"llm_request_mode": "sync"},
    }

    _run_engine(engine, state, caller="main")

    assert fake_agent.invoked is True
    assert getitem(state, "final_output") == "sync answer"


def test_jiuwen_loop_engine_passes_full_history_to_context_engine(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_current_workspace_id", lambda: "ws-input-limit")
    monkeypatch.setattr(
        "agent.core.shared.prompting.build_ontologies_section",
        lambda selected_ontology_id=None, **_kwargs: "<Available-Ontologies></Available-Ontologies>",
        raising=False,
    )
    monkeypatch.setattr(
        "agent.core.shared.prompting.build_skills_section",
        lambda **_kwargs: "<Available-Skills></Available-Skills>",
        raising=False,
    )

    sliding_window_calls = []

    def fail_if_called_apply_sliding_window(messages, max_rounds):
        sliding_window_calls.append((messages, max_rounds))
        raise AssertionError(
            "apply_sliding_window must not be invoked by the engine; ContextEngine is responsible for windowing."
        )

    # Patch the source module so that, if the engine ever re-imports
    # apply_sliding_window, the test fails loudly.
    monkeypatch.setattr(
        "agent.context.apply_sliding_window",
        fail_if_called_apply_sliding_window,
    )

    fake_agent = _FakeJiuwenAgent({"output": "invoked with full history"})
    create_calls = []

    def fake_create_default_jiuwen_agent(
        *, llm_config, max_iterations, system_prompt=None, agent_id=None, context_max_rounds=None, **_kwargs
    ):
        create_calls.append({"context_max_rounds": context_max_rounds})
        del llm_config, max_iterations, system_prompt, agent_id
        return fake_agent

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", lambda *_args, **_kwargs: [])

    engine = JiuwenReactLoopEngine(agent_config=_agent_config(context_max_rounds=1))
    current_message = {
        "role": "user",
        "content": "this request uses the current context window",
    }
    persisted_messages = [
        {"role": "user", "content": "old request outside the window"},
        {"role": "assistant", "content": "old answer outside the window"},
        current_message,
    ]
    state = {
        "run_id": "run-input-limit",
        "session_id": "sess-input-limit",
        "current_turn_id": 5,
        "user_request": current_message["content"],
        "messages": list(persisted_messages),
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    assert sliding_window_calls == []
    assert create_calls and create_calls[-1]["context_max_rounds"] == 1
    assert fake_agent.inputs["query"].startswith(current_message["content"])
    assert state["messages"][:3] == persisted_messages
    assert getitem(state, "final_output") == "invoked with full history"


def test_jiuwen_loop_engine_exposes_web_search_only_when_enabled(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    def run_with_web_search(enabled):
        fake_agent = _FakeJiuwenAgent({"output": "registered"})
        calls = {}

        def fake_create_default_jiuwen_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
            del llm_config, max_iterations, system_prompt
            return fake_agent

        def fake_register(
            agent,
            *,
            state,
            context_factory,
            tool_infos=None,
            tool_batch_executor=None,
            iteration_event_callback=None,
            tool_batch_started_callback=None,
            tool_batch_failed_callback=None,
            terminal_tool_batch_callback=None,
            state_snapshot_callback=None,
            terminal_error_callback=None,
        ):
            del (
                agent,
                state,
                context_factory,
                tool_batch_executor,
                iteration_event_callback,
                tool_batch_started_callback,
                tool_batch_failed_callback,
                terminal_tool_batch_callback,
                state_snapshot_callback,
                terminal_error_callback,
            )
            calls["tool_infos"] = tool_infos
            return []

        monkeypatch.setattr(
            "agent.core.jiuwen_engine.create_default_jiuwen_agent",
            fake_create_default_jiuwen_agent,
        )
        monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", fake_register)
        engine = JiuwenReactLoopEngine(
            agent_config=_agent_config(
                web_search=WebSearchConfig(enabled=enabled),
            )
        )
        _run_engine(
            engine,
            {
                "run_id": "run-register-web-search-{}".format(enabled),
                "session_id": "sess-register-web-search-{}".format(enabled),
                "current_turn_id": 3,
                "user_request": "latest external info",
                "messages": [],
                "results": {},
                "user_context": {"agent_mode": "quick"},
            },
            caller="main",
        )
        return [info["name"] for info in calls["tool_infos"]]

    assert "web_search" not in run_with_web_search(False)
    assert "web_search" in run_with_web_search(True)


def test_jiuwen_loop_engine_uses_main_tool_schema_for_acp_caller(monkeypatch):
    from agent.tools.registry import get_tools_schema

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {"user_context": {"agent_mode": "quick"}}

    schemas = internal(engine).tools_schema_for_state(state, caller="acp")
    jiuwen_names = [schema["function"]["name"] for schema in schemas]
    expected_schemas = get_tools_schema(
        "main",
        include_memory=False,
        include_orchestration=False,
        include_web_search=False,
    )
    expected_names = [schema["function"]["name"] for schema in expected_schemas]

    assert jiuwen_names == expected_names


def test_jiuwen_loop_engine_exposes_memory_only_for_explicit_memory_request(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())

    ordinary_schemas = internal(engine).tools_schema_for_state(
        {"user_request": "summarize this plan", "messages": []},
        caller="main",
    )
    memory_schemas = internal(engine).tools_schema_for_state(
        {"user_request": "remember that I prefer tables", "messages": []},
        caller="main",
    )
    ordinary_names = {schema["function"]["name"] for schema in ordinary_schemas}
    memory_names = {schema["function"]["name"] for schema in memory_schemas}

    assert "memory" not in ordinary_names
    assert "memory" in memory_names


def test_jiuwen_loop_engine_stops_stream_when_run_is_cancelled(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr("agent.core.jiuwen_engine.get_current_workspace_id", lambda: "ws-cancel")

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    cancel_event = threading.Event()

    class FakeRouter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_config(self, purpose, profile_id=None, workspace_id=None):
            del purpose, profile_id, workspace_id
            return SimpleNamespace(model="model-cancel")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_cancel_event(self, run_id):
            assert run_id == "run-jiuwen-cancel"
            return cancel_event

    class FakeStreamingAgent:
        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "first", "result_type": "answer"},
            )
            cancel_event.set()
            await asyncio.sleep(0.05)
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "second", "result_type": "answer"},
            )

    monkeypatch.setattr("agent.core.jiuwen_engine.get_llm_router", lambda: FakeRouter(), raising=False)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: FakeStreamingAgent(),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", lambda *_args, **_kwargs: [])

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    observer = RecordingObserver()
    events = []
    state = {
        "run_id": "run-jiuwen-cancel",
        "session_id": "sess-jiuwen-cancel",
        "current_turn_id": 8,
        "user_request": "cancel streaming",
        "messages": [],
        "results": {},
    }

    with pytest.raises(WorkflowCancelledError):
        _run_engine(
            engine,
            state,
            caller="main",
            event_writer=events.append,
            observer=observer,
        )

    token_texts = [event["data"]["payload"]["text"] for event in events if event["data"]["type"] == "token.delta"]
    assert token_texts == ["first"]
    assert "final_output" not in state or getitem(state, "final_output") in (None, "")
    loop_finished = [event for event in observer.events if event.type == RuntimeEventType.AGENT_LOOP_FINISHED]
    assert loop_finished[-1].payload["status"] == RuntimeStatus.CANCELLED


def test_jiuwen_loop_engine_resolves_current_workspace(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.get_current_workspace_id",
        lambda: "ws-current",
        raising=False,
    )
    project_root_calls = []

    def fake_get_workspace_project_root(workspace_id=None):
        project_root_calls.append(workspace_id)
        return "/workspace/{}".format(workspace_id or "missing")

    monkeypatch.setattr(
        "agent.core.jiuwen_engine.get_workspace_project_root",
        fake_get_workspace_project_root,
    )

    fake_agent = _FakeJiuwenAgent({"output": "registered"})
    calls = {}

    def fake_create_default_jiuwen_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
        del llm_config, max_iterations, system_prompt
        return fake_agent

    def fake_register(
        agent,
        *,
        state,
        context_factory,
        tool_infos=None,
        tool_batch_executor=None,
        iteration_event_callback=None,
        tool_batch_started_callback=None,
        tool_batch_failed_callback=None,
        terminal_tool_batch_callback=None,
        state_snapshot_callback=None,
        terminal_error_callback=None,
    ):
        del (
            agent,
            tool_infos,
            tool_batch_executor,
            iteration_event_callback,
            tool_batch_started_callback,
            tool_batch_failed_callback,
            terminal_tool_batch_callback,
            state_snapshot_callback,
            terminal_error_callback,
        )
        calls["state_workspace_id"] = state.get("workspace_id")
        calls["context"] = context_factory()
        return []

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_create_default_jiuwen_agent)
    monkeypatch.setattr("agent.core.jiuwen_engine.register_current_runtime_tools", fake_register)
    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-register-workspace",
        "session_id": "sess-register-workspace",
        "current_turn_id": 4,
        "user_request": "needs workspace",
        "messages": [],
        "results": {},
    }

    _run_engine(engine, state, caller="main")

    assert getitem(state, "workspace_id") == "ws-current"
    assert calls["state_workspace_id"] == "ws-current"
    assert calls["context"].workspace_id == "ws-current"
    assert calls["context"].project_root == "/workspace/ws-current"
    assert project_root_calls == ["ws-current"]


def test_current_runtime_tool_adapter_executes_current_tool_batch_and_merges_state():
    captured = {}
    state = {
        "run_id": "run-tool-adapter",
        "messages": [],
        "results": {"existing": "kept"},
    }

    def fake_executor(tool_calls, context):
        captured["tool_calls"] = tool_calls
        captured["context"] = context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": "jiuwen_skill",
                    "name": "skill",
                }
            ],
            state_updates={
                "results": {"tool": "done"},
                "errors": ["soft warning"],
            },
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="skill",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    result = asyncio.run(adapter.invoke({"skill_id": "finance"}))

    assert result == "tool response"
    tool_call = captured["tool_calls"][0]
    assert isinstance(tool_call, ToolInvocation)
    assert tool_call.id == "jiuwen_run-tool-adapter_skill_1"
    assert tool_call.name == "skill"
    assert tool_call.arguments == {"skill_id": "finance"}
    assert captured["context"].name == "ctx"
    assert state["messages"][0]["role"] == "assistant"
    assert state["messages"][0]["tool_calls"][0]["function"]["name"] == "skill"
    assert state["messages"][1] == {
        "role": "tool",
        "content": "tool response",
        "tool_call_id": "jiuwen_skill",
        "name": "skill",
    }
    assert state["results"] == {"existing": "kept", "tool": "done"}
    assert state["errors"] == ["soft warning"]


def test_current_runtime_tool_adapter_returns_terminal_content_without_callback():
    state = {"messages": [], "results": {}}
    terminal_batches = []

    def fake_executor(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "需要用户补充信息",
                    "tool_call_id": tool_calls[0].id,
                    "name": "request_user_interaction",
                }
            ],
            state_updates={
                "pending_user_interaction": {"id": "interaction_1", "type": "input"},
                "final_output": {"type": "user_interaction", "text": "需要用户补充信息"},
            },
            terminal=True,
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="request_user_interaction",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
        terminal_tool_batch_callback=terminal_batches.append,
    )

    result = asyncio.run(adapter.invoke({"type": "input", "prompt": "需要用户补充信息"}))

    assert result == "需要用户补充信息"
    assert len(terminal_batches) == 1
    assert terminal_batches[0].terminal is True
    assert state["pending_user_interaction"]["id"] == "interaction_1"
    assert state["final_output"] == {"type": "user_interaction", "text": "需要用户补充信息"}
    assert len([message for message in state["messages"] if message.get("role") == "tool"]) == 1


def test_current_runtime_tool_adapter_reports_terminal_tool_error():
    state = {"messages": [], "results": {}}
    terminal_errors = []

    def fake_executor(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": '{"error_code":"GRAPH_BACKEND_TIMEOUT","retryable":false}',
                    "tool_call_id": tool_calls[0].id,
                    "name": "business_data_search",
                }
            ],
            state_updates={
                "terminal_error": {
                    "error_code": "GRAPH_BACKEND_TIMEOUT",
                    "message": "Graph query timed out.",
                    "tool_name": "business_data_search",
                }
            },
            terminal=True,
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="business_data_search",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
        terminal_error_callback=terminal_errors.append,
    )

    result = asyncio.run(adapter.invoke({"query": "list units"}))

    assert "GRAPH_BACKEND_TIMEOUT" in result
    assert len(terminal_errors) == 1
    assert isinstance(terminal_errors[0], TerminalToolExecutionError)
    assert terminal_errors[0].error_code == "GRAPH_BACKEND_TIMEOUT"
    assert terminal_errors[0].public_message == "Graph query timed out."


def test_current_runtime_tool_adapter_does_not_repeat_pending_user_interaction():
    calls = []
    terminal_batches = []
    state = {"messages": [], "results": {}}

    def fake_executor(tool_calls, context):
        del context
        calls.append(tool_calls[0].id)
        time.sleep(0.05)
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "请补充当前切换批次",
                    "tool_call_id": tool_calls[0].id,
                    "name": "request_user_interaction",
                }
            ],
            state_updates={
                "pending_user_interaction": {"id": "interaction_current_batch", "type": "input"},
                "final_output": {"type": "user_interaction", "text": "请补充当前切换批次"},
            },
            terminal=True,
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="request_user_interaction",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
        terminal_tool_batch_callback=terminal_batches.append,
    )

    async def invoke_twice():
        return await asyncio.gather(
            adapter.invoke({"type": "input", "prompt": "请补充当前切换批次"}),
            adapter.invoke({"type": "input", "prompt": "请补充当前切换批次"}),
            return_exceptions=True,
        )

    results = asyncio.run(invoke_twice())

    assert len(calls) == 1
    assert results == ["请补充当前切换批次", "请补充当前切换批次"]
    assert len(terminal_batches) == 2
    assert all(batch.terminal for batch in terminal_batches)
    assert state["pending_user_interaction"]["id"] == "interaction_current_batch"
    assistant_messages = [message for message in state["messages"] if message.get("role") == "assistant"]
    tool_messages = [message for message in state["messages"] if message.get("role") == "tool"]
    assert len(assistant_messages) == 1
    assert len(assistant_messages[0]["tool_calls"]) == 1
    assert len(tool_messages) == 1


def test_current_runtime_tool_adapter_parses_json_string_tool_arguments_for_executor():
    captured = {}
    state = {"messages": [], "results": {}}

    def fake_executor(tool_calls, context):
        del context
        captured["tool_call"] = tool_calls[0]
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "skill",
                }
            ],
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="skill",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    result = asyncio.run(adapter.invoke('{"skill_id":"finance"}'))

    assert result == "tool response"
    assert captured["tool_call"].arguments == {"skill_id": "finance"}
    assert state["messages"][0]["tool_calls"][0]["function"]["arguments"] == '{"skill_id":"finance"}'


def test_current_runtime_tool_adapter_emits_tool_batch_trace_events():
    from integration.observability import RuntimeEventType, RuntimeStatus

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    observer = RecordingObserver()
    state = {"messages": [], "results": {}}

    def fake_executor(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "skill",
                }
            ],
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="skill",
        state=state,
        context_factory=lambda: SimpleNamespace(
            run_id="run-jiuwen-tool-batch",
            session_id="sess-jiuwen-tool-batch",
            turn_id=3,
            caller="main",
            agent_iteration=2,
            subagent_lineage=None,
            observer=observer,
        ),
        tool_batch_executor=fake_executor,
    )

    asyncio.run(adapter.invoke({"skill_id": "finance"}))

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.TOOL_BATCH_STARTED in event_types
    assert RuntimeEventType.TOOL_BATCH_FINISHED in event_types
    started = next(event for event in observer.events if event.type == RuntimeEventType.TOOL_BATCH_STARTED)
    finished = next(event for event in observer.events if event.type == RuntimeEventType.TOOL_BATCH_FINISHED)
    assert started.payload["iteration"] == 2
    assert finished.payload["status"] == RuntimeStatus.SUCCESS
    assert finished.payload["tool_calls_count"] == 1
    assert finished.payload["tool_names"] == ["skill"]


def test_current_runtime_tool_adapter_uses_unique_tool_call_ids_per_invocation():
    captured_tool_call_ids = []
    state = {"run_id": "run-unique-tool-calls", "messages": [], "results": {}}

    def fake_executor(tool_calls, context):
        del context
        captured_tool_call_ids.append(tool_calls[0].id)
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response {}".format(len(captured_tool_call_ids)),
                    "tool_call_id": tool_calls[0].id,
                    "name": "business_data_search",
                }
            ],
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="business_data_search",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    asyncio.run(adapter.invoke({"query": "first"}))
    asyncio.run(adapter.invoke({"query": "second"}))

    assert len(captured_tool_call_ids) == 2
    assert captured_tool_call_ids[0] != captured_tool_call_ids[1]
    assert captured_tool_call_ids[0] == "jiuwen_run-unique-tool-calls_business_data_search_1"
    assert captured_tool_call_ids[1] == "jiuwen_run-unique-tool-calls_business_data_search_2"
    assistant_tool_call_ids = [
        message["tool_calls"][0]["id"]
        for message in state["messages"]
        if message.get("role") == "assistant" and message.get("tool_calls")
    ]
    tool_message_ids = [message["tool_call_id"] for message in state["messages"] if message.get("role") == "tool"]
    assert assistant_tool_call_ids == captured_tool_call_ids
    assert tool_message_ids == captured_tool_call_ids


def test_current_runtime_tool_adapter_preserves_pending_assistant_context():
    state = {
        "messages": [],
        "results": {},
        "_jiuwen_pending_assistant_context": {
            "content": "需要先查询业务数据。",
            "reasoning_content": "用户问题需要工具补充事实。",
        },
    }

    def fake_executor(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "business_data_search",
                }
            ],
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="business_data_search",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    result = asyncio.run(adapter.invoke({"query": "CHANGE_ORDER-001"}))

    assert result == "tool response"
    assistant_message = state["messages"][0]
    assert assistant_message["role"] == "assistant"
    assert assistant_message["content"] == "需要先查询业务数据。"
    assert assistant_message["reasoning_content"] == "用户问题需要工具补充事实。"
    assert len(assistant_message["tool_calls"]) == 1
    assert "_jiuwen_pending_assistant_context" not in state


def test_current_runtime_tool_adapter_allows_parallel_jiuwen_tool_invocations():
    starts = []
    lock = threading.Lock()
    state = {"messages": [], "results": {}}

    def fake_executor(tool_calls, context):
        del context
        with lock:
            starts.append(time.perf_counter())
        time.sleep(0.25)
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "business_data_search",
                }
            ],
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="business_data_search",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    async def run_two_invocations():
        return await asyncio.gather(
            adapter.invoke({"query": "first"}),
            adapter.invoke({"query": "second"}),
        )

    started_at = time.perf_counter()
    results = asyncio.run(run_two_invocations())
    elapsed = time.perf_counter() - started_at

    assert results == ["tool response", "tool response"]
    assert len(starts) == 2
    assert max(starts) - min(starts) < 0.15
    assert elapsed < 0.40
    assert len(state["messages"]) == 3
    assert state["messages"][0]["role"] == "assistant"
    assert len(state["messages"][0]["tool_calls"]) == 2
    assert [message["role"] for message in state["messages"][1:]] == ["tool", "tool"]


def test_current_runtime_tool_adapter_serializes_unsafe_jiuwen_tool_invocations():
    active = 0
    max_active = 0
    lock = threading.Lock()
    state = {"messages": [], "results": {}}

    def fake_executor(tool_calls, context):
        del context
        nonlocal active, max_active
        with lock:
            active += 1
            max_active = max(max_active, active)
        time.sleep(0.12)
        with lock:
            active -= 1
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "script response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "execute_script",
                }
            ],
        )

    adapter = CurrentRuntimeToolAdapter(
        tool_name="execute_script",
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_batch_executor=fake_executor,
    )

    async def run_two_invocations():
        return await asyncio.gather(
            adapter.invoke({"ontology_id": "demo", "operation_key": "first"}),
            adapter.invoke({"ontology_id": "demo", "operation_key": "second"}),
        )

    started_at = time.perf_counter()
    results = asyncio.run(run_two_invocations())
    elapsed = time.perf_counter() - started_at

    assert results == ["script response", "script response"]
    assert max_active == 1
    assert elapsed >= 0.20
    assert len(state["messages"][0]["tool_calls"]) == 2
    assert [message["role"] for message in state["messages"][1:]] == ["tool", "tool"]


def test_register_current_runtime_tools_adds_abilities_and_runner_resources(monkeypatch):
    created = {}

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            created["tool"] = tool
            created["tag"] = tag
            created["refresh"] = refresh
            return SimpleNamespace(success=True)

    class FakeAbilityManager:
        def __init__(self):
            self.cards = []

        def add(self, cards):
            self.cards.extend(cards)
            return SimpleNamespace(added=True)

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    captured = {}

    def fake_executor(tool_calls, context):
        captured["tool_calls"] = tool_calls
        captured["context"] = context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "registered tool response",
                    "tool_call_id": "jiuwen_skill",
                    "name": "skill",
                }
            ]
        )

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    state = {"messages": [], "results": {}}
    agent = SimpleNamespace(
        card=SimpleNamespace(id="agent-1"),
        ability_manager=FakeAbilityManager(),
    )

    cards = register_current_runtime_tools(
        agent,
        state=state,
        context_factory=lambda: SimpleNamespace(name="ctx"),
        tool_infos=[jiuwen_tool_info_from_spec(get_tool_spec("skill"))],
        tool_batch_executor=fake_executor,
    )
    result = asyncio.run(created["tool"].invoke({"skill_id": "finance"}))

    assert [card.name for card in cards] == ["skill"]
    assert agent.ability_manager.cards == cards
    assert created["tool"].card.id == "agent-1.tool.skill"
    assert created["tool"].card.name == "skill"
    assert created["tag"] == "agent-1"
    assert created["refresh"] is True
    assert result == "registered tool response"
    assert captured["tool_calls"][0].name == "skill"
    assert captured["tool_calls"][0].arguments == {"skill_id": "finance"}
    assert captured["context"].name == "ctx"


def test_jiuwen_loop_default_path_invokes_registered_tool_and_reports_tool_metrics(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {}

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}
            created["tag"] = tag
            created["refresh"] = refresh

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-e2e")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()
            self.inputs = None

        async def invoke(self, inputs, session=None):
            del session
            self.inputs = inputs
            tool_result = await created["tool_by_name"]["ontology_context"].invoke({"resource": "root"})
            return {"output": "agent saw {}".format(tool_result)}

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        created["tool_calls"] = tool_calls
        created["context"] = context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "registered tool response",
                    "tool_call_id": "jiuwen_ontology_context",
                    "name": "ontology_context",
                }
            ],
        )

    fake_agent = FakeAgent()

    def fake_create_default_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
        del llm_config
        del system_prompt
        created["max_iterations"] = max_iterations
        return fake_agent

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        fake_create_default_agent,
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.aexecute_tool_batch",
        fake_executor,
    )

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-e2e",
        "session_id": "sess-e2e",
        "current_turn_id": 9,
        "user_request": "use a tool",
        "messages": [],
        "results": {},
    }
    events = []
    snapshots = []

    _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
        snapshot_callback=snapshots.append,
    )

    assert fake_agent.inputs["query"].startswith("use a tool\n\n<system-reminder>")
    assert "# currentDate" in fake_agent.inputs["query"]
    assert created["max_iterations"] == 50
    assistant_tool_message = next(
        message for message in state["messages"] if message.get("role") == "assistant" and message.get("tool_calls")
    )
    assert assistant_tool_message["tool_calls"][0]["function"]["name"] == "ontology_context"
    tool_message = next(message for message in state["messages"] if message.get("role") == "tool")
    assert tool_message["name"] == "ontology_context"
    assert state["messages"].index(assistant_tool_message) < state["messages"].index(tool_message)
    assert getitem(state, "final_output") == "agent saw registered tool response"
    assert created["tool_calls"][0].name == "ontology_context"
    assert created["tool_calls"][0].arguments == {"resource": "root"}
    assert created["context"].caller == "main"
    assert created["tag"] == "agent-e2e"
    assert created["refresh"] is True
    assert len(snapshots) >= 3
    assert any(
        any(message.get("role") == "tool" for message in snapshot.get("messages", []))
        and "final_output" not in snapshot
        for snapshot in snapshots
    )
    iteration_events = [event["data"] for event in events if event["data"]["type"] == "iteration.completed"]
    assert [event["payload"]["iteration"] for event in iteration_events] == [1, 2]
    tool_payload = iteration_events[0]["payload"]
    assert tool_payload["status"] == "continue"
    assert tool_payload["tool_calls_count"] == 1
    assert tool_payload["tool_names"] == ["ontology_context"]
    final_payload = iteration_events[-1]["payload"]
    assert final_payload["status"] == "final_output"
    assert final_payload["tool_calls_count"] == 0
    assert final_payload["tool_names"] == []


def test_jiuwen_loop_keeps_multiple_tools_from_one_llm_turn_in_same_iteration(monkeypatch):
    from integration.observability import RuntimeEventType

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {"contexts": []}

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def __init__(self):
            super().__init__()
            self.registered_tools = {}

        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}
            self.registered_tools.update({item.card.id: item for item in tools})

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-two-tools")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()

        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "需要查询两个 SourceItem。", "result_type": "answer"},
            )
            first = await created["tool_by_name"]["business_data_search"].invoke({"query": "EP001"})
            second = await created["tool_by_name"]["business_data_search"].invoke({"query": "EP002"})
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "两个 SourceItem 查询完成。", "result_type": "answer"},
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "{} / {}".format(first, second), "result_type": "answer"},
            )

    resource_mgr = FakeResourceMgr()
    resource_count_before_run = len(resource_mgr.registered_tools)

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=resource_mgr)),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        created["contexts"].append(context)
        args_text = json.dumps(tool_calls[0].arguments, ensure_ascii=False, separators=(",", ":"))
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "result for {}".format(args_text),
                    "tool_call_id": tool_calls[0].id,
                    "name": "business_data_search",
                }
            ],
        )

    fake_agent = FakeAgent()

    def fake_create_default_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
        del llm_config, max_iterations, system_prompt
        return fake_agent

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        fake_create_default_agent,
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-two-tools",
        "session_id": "sess-two-tools",
        "current_turn_id": 10,
        "user_request": "use two tools",
        "messages": [],
        "results": {},
    }
    events = []
    observer = RecordingObserver()

    _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
        observer=observer,
    )

    assert [context.agent_iteration for context in created["contexts"]] == [1, 1]
    token_events = [event["data"] for event in events if event["data"]["type"] == "token.delta"]
    assert [event["payload"]["iteration"] for event in token_events] == [1, 2, 2]
    iteration_events = [event["data"] for event in events if event["data"]["type"] == "iteration.completed"]
    assert [event["payload"]["iteration"] for event in iteration_events] == [1, 1, 2]
    assert iteration_events[1]["payload"]["tool_calls_count"] == 2
    assert iteration_events[1]["payload"]["tool_names"] == [
        "business_data_search",
        "business_data_search",
    ]
    assert iteration_events[-1]["payload"]["status"] == "final_output"
    trace_iteration_finished = [
        event.payload for event in observer.events if event.type == RuntimeEventType.AGENT_ITERATION_FINISHED
    ]
    assert [payload["iteration"] for payload in trace_iteration_finished] == [1, 2]
    assert trace_iteration_finished[0]["tool_calls_count"] == 2
    assert trace_iteration_finished[0]["tool_names"] == [
        "business_data_search",
        "business_data_search",
    ]
    tool_batch_started = [
        event.payload for event in observer.events if event.type == RuntimeEventType.TOOL_BATCH_STARTED
    ]
    tool_batch_finished = [
        event.payload for event in observer.events if event.type == RuntimeEventType.TOOL_BATCH_FINISHED
    ]
    assert [payload["iteration"] for payload in tool_batch_started] == [1]
    assert [payload["iteration"] for payload in tool_batch_finished] == [1]
    assert tool_batch_finished[0]["tool_calls_count"] == 2
    assert tool_batch_finished[0]["tool_names"] == [
        "business_data_search",
        "business_data_search",
    ]
    assert len(resource_mgr.registered_tools) == resource_count_before_run
    assert resource_mgr.removed_tool_ids


def test_jiuwen_loop_returns_when_tool_batch_is_terminal(monkeypatch):
    from integration.observability import RuntimeEventType

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {}

    class FakeAgentCallbackEvent:
        BEFORE_MODEL_CALL = "before_model_call"
        AFTER_MODEL_CALL = "after_model_call"
        ON_MODEL_EXCEPTION = "on_model_exception"
        AFTER_TOOL_CALL = "after_tool_call"

    class FakeCallbackManager:
        def __init__(self):
            self.callbacks = {}
            self.priorities = {}

        async def register_callback(self, event, callback, priority=100):
            self.callbacks[event] = callback
            self.priorities[event] = priority

        async def unregister(self, event, callback):
            if self.callbacks.get(event) == callback:
                self.callbacks.pop(event, None)

    class FakeToolCallbackContext:
        def __init__(self, tool_result, tool_call_id):
            self.inputs = SimpleNamespace(
                tool_call=SimpleNamespace(id=tool_call_id, name="request_user_interaction"),
                tool_name="request_user_interaction",
                tool_args={"type": "single_choice", "prompt": "请选择处理方式", "options": []},
                tool_result=tool_result,
                tool_msg=SimpleNamespace(content=tool_result),
            )
            self.exception = None
            self.force_finish_result = None

        def request_force_finish(self, result):
            self.force_finish_result = result

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def __init__(self):
            super().__init__()
            self.registered_tools = {}

        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}
            self.registered_tools.update({item.card.id: item for item in tools})

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-terminal-tool")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()
            self.agent_callback_manager = FakeCallbackManager()
            self.model_calls = 0
            self.force_finish_results = []

        async def stream(self, inputs):
            del inputs
            self.model_calls += 1
            tool_result = await created["tool_by_name"]["request_user_interaction"].invoke(
                {"type": "single_choice", "prompt": "请选择处理方式", "options": []}
            )
            callback = self.agent_callback_manager.callbacks[FakeAgentCallbackEvent.AFTER_TOOL_CALL]
            callback_context = FakeToolCallbackContext(
                tool_result,
                "model-generated-request-user-interaction-call",
            )
            assert callback_context.inputs.tool_call.id != created["terminal_tool_call_id"]
            callback_result = callback(callback_context)
            if asyncio.iscoroutine(callback_result):
                await callback_result
            if callback_context.force_finish_result is not None:
                self.force_finish_results.append(callback_context.force_finish_result)
                yield SimpleNamespace(
                    type="llm_output",
                    payload={
                        "content": (
                            "继续\n\n<user-interaction-response>"
                            '{"option_id":"proceed","value":"proceed"}'
                            "</user-interaction-response>"
                        ),
                        "result_type": "answer",
                    },
                )
                yield SimpleNamespace(
                    type="answer",
                    payload=callback_context.force_finish_result,
                )
                return
            self.model_calls += 1
            yield SimpleNamespace(
                type="answer",
                payload={"output": "should not continue", "result_type": "answer"},
            )

    resource_mgr = FakeResourceMgr()
    resource_count_before_run = len(resource_mgr.registered_tools)

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=resource_mgr)),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        del context
        created["terminal_tool_call_id"] = tool_calls[0].id
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "请选择处理方式",
                    "tool_call_id": tool_calls[0].id,
                    "name": "request_user_interaction",
                }
            ],
            state_updates={
                "final_output": {"type": "user_interaction", "text": "请选择处理方式"},
                "pending_user_interaction": {"type": "single_choice", "id": "interaction_1"},
            },
            terminal=True,
        )

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._jiuwen_agent_callback_event_class",
        lambda: FakeAgentCallbackEvent,
    )
    fake_agent = FakeAgent()
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: fake_agent,
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-jiuwen-terminal-tool",
        "session_id": "sess-jiuwen-terminal-tool",
        "current_turn_id": 11,
        "user_request": "need user interaction",
        "messages": [],
        "results": {},
    }
    events = []
    observer = RecordingObserver()
    snapshots = []

    async def persist_snapshot(snapshot):
        await asyncio.sleep(0)
        snapshots.append(snapshot)

    final_state = _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
        observer=observer,
        snapshot_callback=persist_snapshot,
    )

    assert final_state["pending_user_interaction"]["id"] == "interaction_1"
    assert final_state["final_output"] == {"type": "user_interaction", "text": "请选择处理方式"}
    assert fake_agent.model_calls == 1
    assert fake_agent.force_finish_results
    assert fake_agent.force_finish_results[-1]["result_type"] == "answer"
    assert FakeAgentCallbackEvent.AFTER_TOOL_CALL not in fake_agent.agent_callback_manager.callbacks
    assert len(resource_mgr.registered_tools) == resource_count_before_run
    assert resource_mgr.removed_tool_ids
    assert not any(
        message.get("content") == "should not continue"
        for message in final_state["messages"]
        if isinstance(message, dict)
    )
    assert not any(event["data"]["type"] in {"token.delta", "reasoning.summary"} for event in events)
    assert not any(
        "user-interaction-response" in str(message.get("content") or "")
        for message in final_state["messages"]
        if isinstance(message, dict)
    )
    iteration_events = [event["data"] for event in events if event["data"]["type"] == "iteration.completed"]
    assert [event["payload"]["status"] for event in iteration_events] == ["terminal_tool_batch"]
    trace_iteration_finished = [
        event.payload for event in observer.events if event.type == RuntimeEventType.AGENT_ITERATION_FINISHED
    ]
    assert [payload["status"] for payload in trace_iteration_finished] == ["terminal_tool_batch"]
    assert len(snapshots) == 3
    assert snapshots[-1]["pending_user_interaction"]["id"] == "interaction_1"


def test_jiuwen_loop_binds_subagent_log_context_for_agent_and_tools(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {}
    observed_agent_contexts = []
    observed_tool_contexts = []

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-subagent-log")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()

        async def stream(self, inputs):
            del inputs
            observed_agent_contexts.append(logging_utils.get_log_context())
            await created["tool_by_name"]["ontology_context"].invoke({"resource": "root"})
            yield SimpleNamespace(
                type="answer",
                payload={"output": "child done", "result_type": "answer"},
            )

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        del context
        observed_tool_contexts.append(logging_utils.get_log_context())
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "ontology_context",
                }
            ],
        )

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: FakeAgent(),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-jiuwen-subagent-log-context",
        "session_id": "sess-jiuwen-subagent-log-context",
        "current_turn_id": 7,
        "user_request": "child task",
        "messages": [],
        "results": {},
    }

    final_state = _run_engine(
        engine,
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

    assert final_state["final_output"] == "child done"
    assert observed_agent_contexts
    assert observed_tool_contexts
    for context in [observed_agent_contexts[0], observed_tool_contexts[0]]:
        assert context["agent_type"] == "subagent"
        assert context["agent_id"] == "sub_abc123"
        assert context["task_id"] == "task-17"
        assert context["group_id"] == "group-9"
        assert context["depth"] == 2
        assert context["run_id"] == "run-jiuwen-subagent-log-context"
        assert context["turn_id"] == 7


def test_jiuwen_loop_marks_started_tool_batch_failed_when_agent_errors(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {}

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class FailingAfterToolAgent:
        card = SimpleNamespace(id="agent-tool-then-error")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()

        async def stream(self, inputs):
            del inputs
            await created["tool_by_name"]["ontology_context"].invoke({"resource": "root"})
            if getattr(self, "_keep_async_generator", False):
                yield None
            raise RuntimeError("agent failed after tool")

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "ontology_context",
                }
            ],
        )

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: FailingAfterToolAgent(),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    observer = RecordingObserver()
    state = {
        "run_id": "run-jiuwen-tool-then-error",
        "session_id": "sess-jiuwen-tool-then-error",
        "current_turn_id": 9,
        "user_request": "tool then fail",
        "messages": [],
        "results": {},
    }

    with pytest.raises(RuntimeError, match="agent failed after tool"):
        _run_engine(engine, state, caller="main", observer=observer)

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.TOOL_BATCH_STARTED in event_types
    tool_failed = [event for event in observer.events if event.type == RuntimeEventType.TOOL_BATCH_FAILED]
    assert tool_failed[-1].payload["status"] == RuntimeStatus.ERROR


def test_jiuwen_loop_marks_started_tool_batch_cancelled_when_agent_cancelled(monkeypatch):
    from integration.observability import RuntimeEventType, RuntimeStatus

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {}

    class RecordingObserver:
        enabled = True

        def __init__(self):
            self.events = []

        def emit(self, event):
            self.events.append(event)
            return True

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class CancellingAfterToolAgent:
        card = SimpleNamespace(id="agent-tool-then-cancel")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()

        async def stream(self, inputs):
            del inputs
            await created["tool_by_name"]["ontology_context"].invoke({"resource": "root"})
            if getattr(self, "_keep_async_generator", False):
                yield None
            raise WorkflowCancelledError("Workflow cancelled by user")

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        del context
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "tool response",
                    "tool_call_id": tool_calls[0].id,
                    "name": "ontology_context",
                }
            ],
        )

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **_kwargs: CancellingAfterToolAgent(),
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    observer = RecordingObserver()
    state = {
        "run_id": "run-jiuwen-tool-then-cancel",
        "session_id": "sess-jiuwen-tool-then-cancel",
        "current_turn_id": 9,
        "user_request": "tool then cancel",
        "messages": [],
        "results": {},
    }

    with pytest.raises(WorkflowCancelledError):
        _run_engine(engine, state, caller="main", observer=observer)

    event_types = [event.type for event in observer.events]
    assert RuntimeEventType.TOOL_BATCH_STARTED in event_types
    tool_failed = [event for event in observer.events if event.type == RuntimeEventType.TOOL_BATCH_FAILED]
    assert tool_failed[-1].payload["status"] == RuntimeStatus.CANCELLED


def test_jiuwen_loop_reports_parallel_tool_window_in_iteration_metrics(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    created = {}

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            del tag, refresh
            tools = tool if isinstance(tool, list) else [tool]
            created["tool_by_name"] = {item.card.name: item for item in tools}

    class FakeAbilityManager:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def add(self, cards):
            created["ability_cards"] = cards

    class FakeAgent:
        card = SimpleNamespace(id="agent-parallel-tools")

        def __init__(self):
            self.ability_manager = FakeAbilityManager()

        async def stream(self, inputs):
            del inputs
            yield SimpleNamespace(
                type="llm_output",
                payload={"content": "并行查询两个对象。", "result_type": "answer"},
            )
            first, second = await asyncio.gather(
                created["tool_by_name"]["business_data_search"].invoke({"query": "slow"}),
                created["tool_by_name"]["business_data_search"].invoke({"query": "fast"}),
            )
            yield SimpleNamespace(
                type="answer",
                payload={"output": "{} / {}".format(first, second), "result_type": "answer"},
            )

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fake_executor(tool_calls, context):
        del context
        args_text = json.dumps(tool_calls[0].arguments, ensure_ascii=False, separators=(",", ":"))
        time.sleep(0.12 if "slow" in args_text else 0.08)
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "result for {}".format(args_text),
                    "tool_call_id": tool_calls[0].id,
                    "name": "business_data_search",
                }
            ],
        )

    fake_agent = FakeAgent()

    def fake_create_default_agent(*, llm_config, max_iterations, system_prompt=None, **_kwargs):
        del llm_config, max_iterations, system_prompt
        return fake_agent

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        fake_create_default_agent,
    )
    monkeypatch.setattr("agent.core.jiuwen_engine.aexecute_tool_batch", fake_executor)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    state = {
        "run_id": "run-parallel-tools",
        "session_id": "sess-parallel-tools",
        "current_turn_id": 11,
        "user_request": "use parallel tools",
        "messages": [],
        "results": {},
    }
    events = []

    _run_engine(
        engine,
        state,
        caller="main",
        event_writer=events.append,
    )

    iteration_events = [event["data"]["payload"] for event in events if event["data"]["type"] == "iteration.completed"]
    tool_payload = next(
        payload for payload in iteration_events if payload["iteration"] == 1 and payload["tool_calls_count"] == 2
    )

    assert tool_payload["tools_window_elapsed_ms"] == tool_payload["tools_elapsed_ms"]
    assert tool_payload["tools_execution_elapsed_ms"] >= 180
    assert tool_payload["tools_elapsed_ms"] < tool_payload["tools_execution_elapsed_ms"]
    assert tool_payload["total_elapsed_ms"] < (
        tool_payload["llm_elapsed_ms"] + tool_payload["tools_execution_elapsed_ms"]
    )


def test_run_jiuwen_smoke_constructs_agent_and_registers_tools_without_invoke():
    calls = {}

    class FakeAgent:
        card = SimpleNamespace(id="agent-smoke")

        async def invoke(self, inputs, session=None):
            del inputs, session
            raise AssertionError("smoke must not call invoke")

    def fake_create(*, llm_config):
        calls["llm_config"] = llm_config
        return FakeAgent()

    def fake_register(
        agent,
        *,
        state,
        context_factory,
        tool_infos=None,
        tool_batch_executor=None,
        iteration_event_callback=None,
        tool_batch_started_callback=None,
        tool_batch_failed_callback=None,
        terminal_tool_batch_callback=None,
        state_snapshot_callback=None,
    ):
        del (
            iteration_event_callback,
            tool_batch_started_callback,
            tool_batch_failed_callback,
            terminal_tool_batch_callback,
            state_snapshot_callback,
        )
        calls["agent"] = agent
        calls["state"] = state
        calls["context"] = context_factory()
        calls["tool_infos"] = tool_infos
        calls["tool_batch_executor"] = tool_batch_executor
        return [
            SimpleNamespace(name="business_data_search"),
            SimpleNamespace(name="ontology_context"),
        ]

    result = run_jiuwen_smoke(
        llm_config=SimpleNamespace(model="model-a"),
        create_agent=fake_create,
        register_tools=fake_register,
    )

    assert result == {
        "ok": True,
        "agent_id": "agent-smoke",
        "registered_tool_count": 2,
        "registered_tool_names": ["business_data_search", "ontology_context"],
    }
    assert calls["llm_config"].model == "model-a"
    assert calls["state"]["user_request"] == "jiuwen smoke check"
    assert calls["context"].caller == "main"
    assert calls["context"].agent_mode == "quick"
    tool_names = [info["name"] for info in calls["tool_infos"]]
    assert "ontology_context" in tool_names
    assert "business_data_search" in tool_names


def test_register_current_runtime_tools_uses_run_scoped_tool_ids(monkeypatch):
    registered_batches = []

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            tools = tool if isinstance(tool, list) else [tool]
            registered_batches.append(
                {
                    "tag": tag,
                    "refresh": refresh,
                    "ids": [item.card.id for item in tools],
                    "names": [item.card.name for item in tools],
                }
            )

    class FakeAbilityManager:
        def __init__(self):
            self.cards = []

        def add(self, cards):
            self.cards.extend(cards)

    class FakeAgent:
        def __init__(self, agent_id):
            self.card = SimpleNamespace(id=agent_id)
            self.ability_manager = FakeAbilityManager()

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    tool_info = jiuwen_tool_info_from_spec(get_tool_spec("business_data_search"))
    first_agent = FakeAgent("agent-run-1")
    second_agent = FakeAgent("agent-run-2")

    register_current_runtime_tools(
        first_agent,
        state={"run_id": "run-1", "messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(run_id="run-1"),
        tool_infos=[tool_info],
    )
    register_current_runtime_tools(
        second_agent,
        state={"run_id": "run-2", "messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(run_id="run-2"),
        tool_infos=[tool_info],
    )

    assert first_agent.ability_manager.cards[0].name == "business_data_search"
    assert second_agent.ability_manager.cards[0].name == "business_data_search"
    assert first_agent.ability_manager.cards[0].id != second_agent.ability_manager.cards[0].id
    assert registered_batches[0]["ids"] != registered_batches[1]["ids"]
    assert registered_batches[0]["names"] == ["business_data_search"]
    assert registered_batches[1]["names"] == ["business_data_search"]


def test_register_current_runtime_tools_uses_native_jiuwen_tool_infos(monkeypatch):
    registered_batches = []

    class FakeToolCard:
        def __init__(self, **kwargs):
            self.id = kwargs["id"]
            self.name = kwargs["name"]
            self.description = kwargs.get("description", "")
            self.input_params = kwargs.get("input_params", {})

    class FakeTool:
        def __init__(self, card):
            self.card = card

    class FakeResourceMgr(_RemovableFakeResourceManager):
        def add_tool(self, tool, *, tag=None, refresh=False):
            tools = tool if isinstance(tool, list) else [tool]
            registered_batches.append([item.card for item in tools])

    class FakeAbilityManager:
        def __init__(self):
            self.cards = []

        def add(self, cards):
            self.cards.extend(cards)

    class FakeAgent:
        def __init__(self):
            self.card = SimpleNamespace(id="agent-native-tools")
            self.ability_manager = FakeAbilityManager()

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.foundation.tool": SimpleNamespace(Tool=FakeTool, ToolCard=FakeToolCard),
            "openjiuwen.core.runner": SimpleNamespace(Runner=SimpleNamespace(resource_mgr=FakeResourceMgr())),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    def fail_schema_registry(*_args, **_kwargs):
        raise AssertionError("legacy OpenAI tool schema registry should not be used")

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    monkeypatch.setattr(
        "agent.tools.registry.get_tools_schema",
        fail_schema_registry,
    )

    agent = FakeAgent()
    register_current_runtime_tools(
        agent,
        state={"run_id": "run-native-tools", "messages": [], "results": {}},
        context_factory=lambda: SimpleNamespace(run_id="run-native-tools"),
    )

    registered_names = [card.name for card in agent.ability_manager.cards]
    assert "ontology_context" in registered_names
    assert "business_data_search" in registered_names
    business_data_card = next(card for card in agent.ability_manager.cards if card.name == "business_data_search")
    assert business_data_card.input_params["type"] == "object"
    assert registered_batches[0][0].id.startswith("agent-native-tools.tool.")


def test_jiuwen_smoke_cli_reports_backend_unavailable_as_json(monkeypatch, capsys):
    def fake_run(*, agent_mode):
        del agent_mode
        raise JiuwenBackendUnavailable("Missing module: jsonschema_path.")

    monkeypatch.setattr(jiuwen_smoke, "run_jiuwen_smoke", fake_run)

    exit_code = jiuwen_smoke.main([])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert payload == {
        "ok": False,
        "error": "Missing module: jsonschema_path.",
    }


def test_create_default_jiuwen_agent_lazy_loads_react_agent(monkeypatch):
    created = {}

    class FakeAgentCard:
        def __init__(self, **kwargs):
            created["card_kwargs"] = kwargs

    class FakeReActAgent:
        def __init__(self, *, card):
            created["card"] = card
            self.configured_with = None

        def configure(self, config):
            self.configured_with = config
            return self

    class FakeModelClientConfig:
        def __init__(self, **kwargs):
            created["model_client_config"] = kwargs

    class FakeModelRequestConfig:
        def __init__(self, **kwargs):
            created["model_request_config"] = kwargs

    class FakeReActAgentConfig:
        def __init__(self, **kwargs):
            created["react_config"] = kwargs

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.single_agent.schema.agent_card": SimpleNamespace(AgentCard=FakeAgentCard),
            "openjiuwen.core.single_agent.agents.react_agent": SimpleNamespace(
                ReActAgent=FakeReActAgent,
                ReActAgentConfig=FakeReActAgentConfig,
            ),
            "openjiuwen.core.foundation.llm.schema.config": SimpleNamespace(
                ModelClientConfig=FakeModelClientConfig,
                ModelRequestConfig=FakeModelRequestConfig,
            ),
            "openjiuwen.core.foundation.llm.model_clients.openai_model_client": SimpleNamespace(
                OpenAIModelClient=type("FakeOpenAIModelClient", (), {})
            ),
            "openjiuwen.core.context_engine": SimpleNamespace(ContextEngineConfig=_FakeContextEngineConfig),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    llm_config = SimpleNamespace(
        provider="openai_compatible",
        api_key=synthetic_secret("jiuwen_react_adapter-credential-01"),
        base_url="https://llm.example.net/v1",
        model="model-a",
        timeout=33,
        max_tokens=2048,
        temperature=0.2,
    )

    agent = create_default_jiuwen_agent(
        llm_config=llm_config,
        max_iterations=50,
        system_prompt="system rules",
    )

    assert isinstance(agent, FakeReActAgent)
    assert created["card_kwargs"]["id"] == "onto_agent_jiuwen_react"
    assert created["card_kwargs"]["name"] == "onto_agent_jiuwen_react"
    assert created["model_client_config"] == {
        "client_provider": "OpenAI",
        "api_key": synthetic_secret("jiuwen_react_adapter-credential-01"),
        "api_base": "https://llm.example.net/v1",
        "timeout": 180.0,
        "max_retries": 1,
        "verify_ssl": False,
    }
    assert created["model_client_config"]["verify_ssl"] is False
    assert created["model_request_config"]["model"] == "model-a"
    assert created["model_request_config"]["temperature"] == 0.2
    assert created["model_request_config"]["max_tokens"] == 2048
    assert "enable_reasoning" not in created["model_request_config"]
    assert created["model_request_config"]["extra_body"]["reasoning"]["enabled"] is False
    assert created["model_request_config"]["extra_body"]["thinking"]["type"] == "disabled"
    assert created["react_config"]["model_client_config"] is not None
    assert created["react_config"]["model_config_obj"] is not None
    assert created["react_config"]["max_iterations"] == 50
    assert created["react_config"]["prompt_template"] == [{"role": "system", "content": "system rules"}]
    assert agent.configured_with is not None


def test_create_default_jiuwen_agent_reports_missing_transitive_dependency(monkeypatch):
    def fake_import_module(name):
        del name
        raise ModuleNotFoundError("No module named 'jsonschema_path'", name="jsonschema_path")

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)

    with pytest.raises(JiuwenBackendUnavailable, match="jsonschema_path"):
        create_default_jiuwen_agent(llm_config=SimpleNamespace())


def test_create_default_jiuwen_agent_does_not_send_enable_reasoning_top_level(monkeypatch):
    created = {}

    class FakeAgentCard:
        def __init__(self, **kwargs):
            created["card_kwargs"] = kwargs

    class FakeReActAgent:
        def __init__(self, *, card):
            self.card = card

        def configure(self, config):
            created["react_config"] = config
            return self

    class FakeModelClientConfig:
        def __init__(self, **kwargs):
            created["model_client_config"] = kwargs

    class FakeModelRequestConfig:
        def __init__(self, **kwargs):
            created["model_request_config"] = kwargs

    class FakeReActAgentConfig:
        def __init__(self, **kwargs):
            created["react_agent_config"] = kwargs

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.single_agent.schema.agent_card": SimpleNamespace(AgentCard=FakeAgentCard),
            "openjiuwen.core.single_agent.agents.react_agent": SimpleNamespace(
                ReActAgent=FakeReActAgent,
                ReActAgentConfig=FakeReActAgentConfig,
            ),
            "openjiuwen.core.foundation.llm.schema.config": SimpleNamespace(
                ModelClientConfig=FakeModelClientConfig,
                ModelRequestConfig=FakeModelRequestConfig,
            ),
            "openjiuwen.core.foundation.llm.model_clients.openai_model_client": SimpleNamespace(
                OpenAIModelClient=type("FakeOpenAIModelClient", (), {})
            ),
            "openjiuwen.core.context_engine": SimpleNamespace(ContextEngineConfig=_FakeContextEngineConfig),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)

    create_default_jiuwen_agent(
        llm_config=SimpleNamespace(
            provider="openai_compatible",
            api_key=synthetic_secret("jiuwen_react_adapter-credential-01"),
            base_url="https://llm.example.net/v1",
            model="model-a",
            timeout=33,
            max_tokens=2048,
            temperature=0.2,
        ),
        enable_reasoning=True,
    )

    assert "enable_reasoning" not in created["model_request_config"]
    assert created["model_request_config"]["extra_body"]["reasoning"]["enabled"] is True
    assert created["model_request_config"]["extra_body"]["thinking"]["type"] == "enabled"


def test_create_default_jiuwen_agent_passes_provider_stream_usage_options(monkeypatch):
    created = {}

    class FakeAgentCard:
        def __init__(self, **kwargs):
            created["card_kwargs"] = kwargs

    class FakeReActAgent:
        def __init__(self, *, card):
            self.card = card

        def configure(self, config):
            created["react_config"] = config
            return self

    class FakeModelClientConfig:
        def __init__(self, **kwargs):
            created["model_client_config"] = kwargs

    class FakeModelRequestConfig:
        def __init__(self, **kwargs):
            created["model_request_config"] = kwargs

    class FakeReActAgentConfig:
        def __init__(self, **kwargs):
            created["react_agent_config"] = kwargs

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.single_agent.schema.agent_card": SimpleNamespace(AgentCard=FakeAgentCard),
            "openjiuwen.core.single_agent.agents.react_agent": SimpleNamespace(
                ReActAgent=FakeReActAgent,
                ReActAgentConfig=FakeReActAgentConfig,
            ),
            "openjiuwen.core.foundation.llm.schema.config": SimpleNamespace(
                ModelClientConfig=FakeModelClientConfig,
                ModelRequestConfig=FakeModelRequestConfig,
            ),
            "openjiuwen.core.foundation.llm.model_clients.openai_model_client": SimpleNamespace(
                OpenAIModelClient=type("FakeOpenAIModelClient", (), {})
            ),
            "openjiuwen.core.context_engine": SimpleNamespace(ContextEngineConfig=_FakeContextEngineConfig),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)

    create_default_jiuwen_agent(
        llm_config=SimpleNamespace(
            provider="deepseek",
            api_key=synthetic_secret("jiuwen_react_adapter-credential-01"),
            base_url="https://deepseek.example.net/v1",
            model="deepseek-model",
            timeout=33,
            max_tokens=2048,
            temperature=0.2,
        ),
        enable_reasoning=False,
    )

    assert created["model_request_config"]["stream_options"] == {"include_usage": True}

    created.clear()
    create_default_jiuwen_agent(
        llm_config=SimpleNamespace(
            provider="deepseek",
            api_key=synthetic_secret("jiuwen_react_adapter-credential-01"),
            base_url="https://deepseek.example.net/v1",
            model="deepseek-model",
            timeout=33,
            max_tokens=2048,
            temperature=0.2,
        ),
        enable_reasoning=False,
        stream_enabled=False,
    )

    assert "stream_options" not in created["model_request_config"]


def test_patch_jiuwen_openai_reasoning_alias_accepts_delta_reasoning_field():
    class FakeParsedChunk:
        def __init__(self, reasoning_content=None):
            self.reasoning_content = reasoning_content

    class FakeOpenAIModelClient:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def _parse_stream_chunk(self, chunk):
            del chunk
            return FakeParsedChunk()

    _patch_jiuwen_openai_reasoning_alias(SimpleNamespace(OpenAIModelClient=FakeOpenAIModelClient))

    parsed = internal(FakeOpenAIModelClient()).parse_stream_chunk(
        SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(reasoning="兼容思考字段"))])
    )

    assert parsed.reasoning_content == "兼容思考字段"


def test_patch_jiuwen_openai_reasoning_alias_preserves_stream_cache_tokens():
    class FakeUsage:
        prompt_tokens = 6250
        completion_tokens = 8
        total_tokens = 6258
        prompt_cache_hit_tokens = 6144
        prompt_cache_miss_tokens = 106

    class FakeUsageMetadata:
        cache_tokens = 0

    class FakeParsedChunk:
        def __init__(self):
            self.reasoning_content = None
            self.usage_metadata = FakeUsageMetadata()

    class FakeOpenAIModelClient:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def _parse_stream_chunk(self, chunk):
            del chunk
            return FakeParsedChunk()

    _patch_jiuwen_openai_reasoning_alias(SimpleNamespace(OpenAIModelClient=FakeOpenAIModelClient))

    parsed = internal(FakeOpenAIModelClient()).parse_stream_chunk(
        SimpleNamespace(
            usage=FakeUsage(),
            choices=[SimpleNamespace(delta=SimpleNamespace(reasoning="兼容思考字段"))],
        )
    )

    assert parsed.usage_metadata.cache_tokens == 6144


def test_patch_jiuwen_openai_reasoning_alias_accepts_message_reasoning_field():
    class FakeParsedMessage:
        def __init__(self, reasoning_content=None):
            self.reasoning_content = reasoning_content

    class FakeOpenAIModelClient:
        async def _parse_response(self, response, parser=None):
            del response, parser
            return FakeParsedMessage()

    _patch_jiuwen_openai_reasoning_alias(SimpleNamespace(OpenAIModelClient=FakeOpenAIModelClient))

    parsed = asyncio.run(
        internal(FakeOpenAIModelClient()).parse_response(
            SimpleNamespace(
                choices=[SimpleNamespace(message={"reasoning": "非流式兼容思考字段"})],
                usage=None,
            )
        )
    )

    assert parsed.reasoning_content == "非流式兼容思考字段"


def test_patch_jiuwen_openai_reasoning_alias_preserves_non_stream_finish_reason():
    class FakeParsedMessage:
        finish_reason = "stop"
        reasoning_content = None
        usage_metadata = None

    class FakeOpenAIModelClient:
        async def _parse_response(self, response, parser=None):
            del response, parser
            return FakeParsedMessage()

    _patch_jiuwen_openai_reasoning_alias(SimpleNamespace(OpenAIModelClient=FakeOpenAIModelClient))

    parsed = asyncio.run(
        internal(FakeOpenAIModelClient()).parse_response(
            SimpleNamespace(
                choices=[SimpleNamespace(finish_reason="length")],
                usage=None,
            )
        )
    )

    assert parsed.finish_reason == "length"


def test_create_default_jiuwen_agent_maps_glm_provider_to_openai_client_provider(monkeypatch):
    created = {}

    class FakeAgentCard:
        def __init__(self, **kwargs):
            created["card_kwargs"] = kwargs

    class FakeReActAgent:
        def __init__(self, *, card):
            self.card = card

        def configure(self, config):
            created["react_config"] = config
            return self

    class FakeModelClientConfig:
        def __init__(self, **kwargs):
            created["model_client_config"] = kwargs

    class FakeModelRequestConfig:
        def __init__(self, **kwargs):
            created["model_request_config"] = kwargs

    class FakeReActAgentConfig:
        def __init__(self, **kwargs):
            created["react_agent_config"] = kwargs

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.single_agent.schema.agent_card": SimpleNamespace(AgentCard=FakeAgentCard),
            "openjiuwen.core.single_agent.agents.react_agent": SimpleNamespace(
                ReActAgent=FakeReActAgent,
                ReActAgentConfig=FakeReActAgentConfig,
            ),
            "openjiuwen.core.foundation.llm.schema.config": SimpleNamespace(
                ModelClientConfig=FakeModelClientConfig,
                ModelRequestConfig=FakeModelRequestConfig,
            ),
            "openjiuwen.core.foundation.llm.model_clients.openai_model_client": SimpleNamespace(
                OpenAIModelClient=type("FakeOpenAIModelClient", (), {})
            ),
            "openjiuwen.core.context_engine": SimpleNamespace(ContextEngineConfig=_FakeContextEngineConfig),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)

    create_default_jiuwen_agent(
        llm_config=SimpleNamespace(
            provider="glm",
            api_key=synthetic_secret("jiuwen_react_adapter-credential-01"),
            base_url="https://glm.example.net/v1",
            model="glm-model",
            timeout=30,
            max_tokens=1024,
            temperature=0.2,
        ),
        enable_reasoning=True,
    )

    assert created["model_client_config"]["client_provider"] == "OpenAI"
    assert created["model_request_config"]["extra_body"]["reasoning"]["enabled"] is True
    assert created["model_request_config"]["extra_body"]["chat_template_kwargs"]["enable_thinking"] is True


def test_warm_up_jiuwen_runtime_requires_openjiuwen(monkeypatch):
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: False)

    with pytest.raises(JiuwenBackendUnavailable, match="openjiuwen"):
        warm_up_jiuwen_runtime()


def test_warm_up_jiuwen_runtime_constructs_agent_without_invoking(monkeypatch):
    """Warm-up creates a throwaway agent but never calls invoke/stream."""
    created = {}

    class _NoInvokeAgent:
        def __init__(self):
            created["agent_instance"] = self

        async def invoke(self, *args, **kwargs):
            raise AssertionError("warm-up must not call agent.invoke")

        async def stream(self, *args, **kwargs):
            raise AssertionError("warm-up must not call agent.stream")

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._resolve_jiuwen_llm_config",
        lambda state, *, caller, agent_config=None: SimpleNamespace(
            provider="openai_compatible",
            api_key="",
            base_url="",
            model="warmup-model",
            timeout=60,
            max_tokens=128,
            temperature=0.5,
        ),
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **kwargs: _NoInvokeAgent(),
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.get_jiuwen_tool_infos",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.importlib.import_module",
        lambda name: SimpleNamespace(__name__=name) if name.startswith("openjiuwen.") else _REAL_IMPORT_MODULE(name),
    )

    result = warm_up_jiuwen_runtime(agent_config=_agent_config())

    assert result["mode"] == "jiuwen-runtime"
    assert result["duration_ms"] >= 0
    assert "agent_instance" in created


def test_warm_up_jiuwen_runtime_does_not_register_tools(monkeypatch):
    """Warm-up must not bind fake-state tool instances into Jiuwen."""
    register_calls = []

    def spy_register(*args, **kwargs):
        register_calls.append({"args": args, "kwargs": kwargs})
        return []

    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._resolve_jiuwen_llm_config",
        lambda state, *, caller, agent_config=None: SimpleNamespace(),
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.create_default_jiuwen_agent",
        lambda **kwargs: object(),
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.get_jiuwen_tool_infos",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.importlib.import_module",
        lambda name: SimpleNamespace(__name__=name) if name.startswith("openjiuwen.") else _REAL_IMPORT_MODULE(name),
    )
    monkeypatch.setattr(
        "agent.core.jiuwen_engine.register_current_runtime_tools",
        spy_register,
    )

    warm_up_jiuwen_runtime(agent_config=_agent_config())

    assert register_calls == []


class _FakeJiuwenAgent:
    def __init__(self, result):
        self.result = result
        self.inputs = None

    async def invoke(self, inputs, session=None):
        del session
        self.inputs = inputs
        return self.result


def _agent_config(**overrides):
    config = {
        "llm_connection_max_retries": 3,
        "main_agent_max_iterations": 50,
        "sub_agent_max_iterations": 5,
        "sub_agent_max_concurrency": 4,
        "sub_agent_max_depth": 2,
        "parallel_default_max_workers": 10,
        "context_max_rounds": 10,
    }
    config.update(overrides)
    return AgentConfig(**config)


_SHARED_CROSS_CALL_LOCK = asyncio.Lock()


async def _acquire_shared_cross_call_lock_with_waiter() -> str:
    await _SHARED_CROSS_CALL_LOCK.acquire()
    waiter = asyncio.create_task(_SHARED_CROSS_CALL_LOCK.acquire())
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            if not waiter.done():
                break
        assert not waiter.done()
    finally:
        _SHARED_CROSS_CALL_LOCK.release()
    await waiter
    _SHARED_CROSS_CALL_LOCK.release()
    return "ok"


def test_native_async_calls_share_the_caller_loop_for_lock_waiters():
    async def exercise():
        result_a = await _acquire_shared_cross_call_lock_with_waiter()
        result_b = await _acquire_shared_cross_call_lock_with_waiter()
        return result_a, result_b

    assert asyncio.run(exercise()) == ("ok", "ok")


def test_native_async_preflight_rejects_an_already_cancelled_run():
    cancel_event = threading.Event()
    cancel_event.set()

    with pytest.raises(WorkflowCancelledError):
        _raise_if_jiuwen_cancelled(
            cancel_event,
            {"run_id": "run-pre-cancelled"},
        )


# ---------------------------------------------------------------------------
# Stage 1: explicit ContextEngineConfig
# ---------------------------------------------------------------------------


def _install_fake_jiuwen_modules(monkeypatch, created):
    """Install a shared fake openjiuwen module set for factory tests."""

    class FakeAgentCard:
        def __init__(self, **kwargs):
            created["card_kwargs"] = kwargs

    class FakeReActAgent:
        def __init__(self, *, card):
            self.card = card

        def configure(self, config):
            created["react_config"] = config
            return self

    class FakeModelClientConfig:
        def __init__(self, **kwargs):
            created["model_client_config"] = kwargs

    class FakeModelRequestConfig:
        def __init__(self, **kwargs):
            created["model_request_config"] = kwargs

    class FakeReActAgentConfig:
        def __init__(self, **kwargs):
            created["react_agent_config"] = kwargs

    def fake_import_module(name):
        modules = {
            "openjiuwen.core.single_agent.schema.agent_card": SimpleNamespace(AgentCard=FakeAgentCard),
            "openjiuwen.core.single_agent.agents.react_agent": SimpleNamespace(
                ReActAgent=FakeReActAgent,
                ReActAgentConfig=FakeReActAgentConfig,
            ),
            "openjiuwen.core.foundation.llm.schema.config": SimpleNamespace(
                ModelClientConfig=FakeModelClientConfig,
                ModelRequestConfig=FakeModelRequestConfig,
            ),
            "openjiuwen.core.foundation.llm.model_clients.openai_model_client": SimpleNamespace(
                OpenAIModelClient=type("FakeOpenAIModelClient", (), {})
            ),
            "openjiuwen.core.context_engine": SimpleNamespace(ContextEngineConfig=_FakeContextEngineConfig),
        }
        return modules.get(name) or _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr("agent.core.jiuwen_engine.importlib.import_module", fake_import_module)
    return FakeReActAgent


def _stub_llm_config():
    return SimpleNamespace(
        provider="openai_compatible",
        api_key=synthetic_secret("jiuwen_react_adapter-credential-01"),
        base_url="https://llm.example.net/v1",
        model="model-a",
        timeout=30,
        max_tokens=2048,
        temperature=0.2,
    )


def test_factory_passes_explicit_context_engine_config(monkeypatch):
    created: dict = {}
    _install_fake_jiuwen_modules(monkeypatch, created)

    create_default_jiuwen_agent(
        llm_config=_stub_llm_config(),
        max_iterations=50,
        system_prompt="system rules",
        context_max_rounds=14,
    )

    cec = created["react_agent_config"]["context_engine_config"]
    assert isinstance(cec, _FakeContextEngineConfig)
    assert cec.kwargs["max_context_message_num"] is None
    assert cec.kwargs["default_window_round_num"] == 14
    assert cec.kwargs["enable_reload"] is False
    assert "enable_kv_cache_release" not in cec.kwargs
    assert created["react_agent_config"]["kv_cache_affinity_config"] == {
        "enable_kv_cache_release": False,
        "enable_kv_cache_affinity": False,
    }
    assert created["react_agent_config"]["context_processors"] == []
    assert created["react_agent_config"]["parallel_tool_calls"] is True


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 10),
        ("abc", 10),
        (0, 1),
        (-5, 1),
        (1, 1),
        (14, 14),
    ],
)
def test_normalize_context_max_rounds(value, expected):
    from agent.core.jiuwen_engine import _normalize_context_max_rounds

    assert _normalize_context_max_rounds(value) == expected


def test_factory_default_context_max_rounds_is_ten(monkeypatch):
    created: dict = {}
    _install_fake_jiuwen_modules(monkeypatch, created)

    create_default_jiuwen_agent(
        llm_config=_stub_llm_config(),
        max_iterations=50,
        system_prompt="system rules",
    )

    cec = created["react_agent_config"]["context_engine_config"]
    assert cec.kwargs["default_window_round_num"] == 10
    assert cec.kwargs["max_context_message_num"] is None


def test_create_default_jiuwen_agent_passes_required_tool_choice(monkeypatch):
    created: dict = {}
    _install_fake_jiuwen_modules(monkeypatch, created)

    create_default_jiuwen_agent(
        llm_config=_stub_llm_config(),
        max_iterations=50,
        system_prompt="system rules",
        tool_choice="required",
    )

    assert created["model_request_config"]["tool_choice"] == "required"
    assert "tool_choice" not in created["model_request_config"].get("extra_body", {})


def test_create_agent_passes_context_max_rounds_from_agent_config(monkeypatch):
    captured = {}

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_factory)
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._resolve_jiuwen_llm_config",
        lambda state, *, caller, agent_config=None: _stub_llm_config(),
    )

    engine = JiuwenReactLoopEngine(
        agent_config=_agent_config(context_max_rounds=7),
    )
    internal(engine).create_agent(
        max_iterations=10,
        system_prompt="prompt",
        caller="main",
    )

    assert captured["context_max_rounds"] == 7


def test_create_agent_solver_route_requires_tool_choice(monkeypatch):
    captured = {}

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_factory)
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    internal(engine).create_agent(
        max_iterations=10,
        system_prompt="prompt",
        state={
            "user_context": {
                "_required_solver_operation_key": "optimize_minus_one_route",
                "_required_solver_tool_name": "solve_optimize_minus_one_route_abcd1234",
            }
        },
        caller="main",
        llm_config=_stub_llm_config(),
    )

    assert captured["tool_choice"] == "required"


def test_create_agent_normal_turn_keeps_automatic_tool_choice(monkeypatch):
    captured = {}

    def fake_factory(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace()

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_factory)
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)

    engine = JiuwenReactLoopEngine(agent_config=_agent_config())
    internal(engine).create_agent(
        max_iterations=10,
        system_prompt="prompt",
        state={"user_context": {}},
        caller="main",
        llm_config=_stub_llm_config(),
    )

    assert captured.get("tool_choice") is None


def test_history_messages_uses_same_normalization_as_factory(monkeypatch):
    """Both _build_jiuwen_history_messages and factory must fall back to 10."""
    # Details: Both _build_jiuwen_history_messages and factory must fall back to 10 (not 50) when context_max_rounds
    # is None/invalid, proving the normalization path is unified.
    monkeypatch.setattr("agent.core.jiuwen_engine.is_openjiuwen_available", lambda: True)
    captured: dict = {}

    def fake_factory(
        *, llm_config, max_iterations, system_prompt=None, agent_id=None, context_max_rounds=None, **_kwargs
    ):
        captured["context_max_rounds"] = context_max_rounds
        del llm_config, max_iterations, system_prompt, agent_id
        return SimpleNamespace()

    monkeypatch.setattr("agent.core.jiuwen_engine.create_default_jiuwen_agent", fake_factory)
    monkeypatch.setattr(
        "agent.core.jiuwen_engine._resolve_jiuwen_llm_config",
        lambda state, *, caller, agent_config=None: _stub_llm_config(),
    )

    from agent.core.jiuwen_engine import _build_jiuwen_history_messages

    config_with_none = _agent_config()
    config_with_none.context_max_rounds = None

    # _build_jiuwen_history_messages no longer applies sliding window; verify
    # it simply forwards full history without depending on max_rounds.
    messages_out = _build_jiuwen_history_messages(
        {"messages": [{"role": "user", "content": "hi"}]},
        config_with_none,
    )
    assert messages_out == []

    # Normalization is now exercised through _create_agent ?
    # create_default_jiuwen_agent(context_max_rounds=...).
    engine = JiuwenReactLoopEngine(agent_config=config_with_none)
    internal(engine).create_agent(
        max_iterations=10,
        system_prompt="prompt",
        caller="main",
    )
    # context_max_rounds falls back to 10 (not 50) when None
    assert captured["context_max_rounds"] == 10


def test_factory_does_not_truncate_messages_under_two_hundred(monkeypatch):
    """With max_context_message_num=None, a single round with >200 messages."""
    # Details: With max_context_message_num=None, a single round with >200 messages must not be truncated by the
    # Jiuwen message buffer.
    created: dict = {}
    _install_fake_jiuwen_modules(monkeypatch, created)

    large_history = [
        {"role": "user", "content": "start"},
    ]
    for i in range(250):
        large_history.append({"role": "assistant", "content": f"assistant {i}"})
        large_history.append({"role": "tool", "content": f"tool {i}", "tool_call_id": f"tc-{i}", "name": "noop"})

    from agent.core.jiuwen_engine import _build_jiuwen_history_messages

    config = _agent_config(context_max_rounds=1)
    converted = _build_jiuwen_history_messages(
        {"messages": large_history},
        config,
    )
    # All 501 history messages (excluding current user) are converted;
    # the Jiuwen ContextEngineConfig with max_context_message_num=None
    # will not truncate them further.
    assert len(converted) > 200
