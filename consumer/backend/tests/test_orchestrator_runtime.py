"""Tests for the orchestrator-owned agent loop runtime."""

from contextlib import contextmanager
import json
from types import SimpleNamespace

import httpx
from openai import APIStatusError
import pytest

from agentic.orchestrator import AgenticOrchestrator, _public_workflow_error_message
from agentic.config import AgentConfig, AppConfig
from agentic.exceptions import InputTokenLimitError
from agentic.services.llm import LLMPurpose, LLMRouter, configure_llm_router, get_llm_router, reset_llm_router_for_tests
from agentic.stores import ChatStore, ExecutionStore, RunStore, RunStatus
from agentic.utils.read_docs import build_read_document_record, merge_read_documents_registry
from agentic.utils.streaming import build_agent_event, emit_agent_event


def test_public_workflow_error_preserves_llm_rate_limit_detail():
    request = httpx.Request("POST", "https://llm.example/v1/chat/completions")
    response = httpx.Response(
        429,
        json={"error": {"code": "1302", "message": "您的账户已达到速率限制，请您控制请求频率"}},
        request=request,
    )
    exc = APIStatusError("rate limited", response=response, body=response.json())

    assert (
        _public_workflow_error_message(exc)
        == "您的账户已达到速率限制，请您控制请求频率"
    )


def test_public_workflow_error_preserves_input_token_limit_detail():
    assert (
        _public_workflow_error_message(InputTokenLimitError(50001, 50000))
        == "输入 Token 长度超过上限: 50001 / 50000"
    )


def test_public_workflow_error_does_not_expose_generic_value_error_by_prefix():
    assert (
        _public_workflow_error_message(ValueError("输入 Token 长度超过上限: internal"))
        == "Workflow execution failed"
    )


@pytest.fixture(autouse=True)
def _configure_runtime_llm(monkeypatch, tmp_path):
    import agentic.config.system_config as system_config_module
    import agentic.utils.ontologies as ontologies_module
    import agentic.utils.skills as skills_module
    from agentic.workspaces import create_workspace

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    runtime_root = tmp_path / ".onto-runtime"
    config_path.write_text(
        """
{
  "llm_profiles": {
    "default_profile_id": "default",
    "profiles": [
      {
        "id": "default",
        "name": "Default Model",
        "provider": "openai_compatible",
        "base_url": "https://default.example/v1",
        "model": "default-model",
        "api_key": "default-secret",
        "enabled": true
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setattr(ontologies_module, "_global_registry", None)
    monkeypatch.setattr(skills_module, "_global_registry", None)
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    configure_llm_router(
        LLMRouter(
            AppConfig.from_env(),
            agent_config=AgentConfig.from_env(),
        )
    )
    yield
    reset_llm_router_for_tests()


class _FakeLLM:
    def __init__(self) -> None:
        self.config = SimpleNamespace(model="unit-model", api_key=None)

    def clear_run(self, run_id: str) -> None:
        del run_id
        return


class _ConfigurableFakeLLM(_FakeLLM):
    def __init__(self) -> None:
        super().__init__()
        self.selected_configs = []

    def with_config(self, config):
        self.selected_configs.append(config)
        child = _FakeLLM()
        child.config = config
        return child


class _FakeMemoryFactory:
    class _Manager:
        def ensure_session(self, app_session_id: str, existing_session_id=None) -> str:
            del app_session_id
            return existing_session_id or "ov-session-1"

        def append_session_message(self, **kwargs) -> None:
            del kwargs
            return

        def build_memory_context(self, query: str, ov_session_id: str) -> str:
            del query, ov_session_id
            return ""

        def append_turn_summary(self, **kwargs) -> None:
            del kwargs
            return

        def commit_session(self, ov_session_id: str) -> None:
            del ov_session_id
            return

    def __init__(self) -> None:
        self.manager = self._Manager()

    def for_user(self, user_id):
        del user_id
        return self.manager


class _FakeJiuwenAgent:
    def __init__(self, result):
        self.result = result
        self.inputs = None

    async def invoke(self, inputs, session=None):
        del session
        self.inputs = inputs
        return self.result


def test_submit_reflection_does_not_depend_on_memory_manager():
    config = AgentConfig.from_env()
    config.evolution_passive_reflection_enabled = True
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
        agent_config=config,
    )

    class _Recorder:
        def __init__(self) -> None:
            self.snapshots = []

        def submit(self, snapshot) -> None:
            self.snapshots.append(snapshot)

    recorder = _Recorder()
    orchestrator.reflection_worker = recorder

    orchestrator._submit_reflection(
        run_id="run-reflection",
        session_id="sess-reflection",
        turn_id=1,
        user_id="alice",
        workspace_id="workspace-1",
        ontology_id="demo-ontology",
        status="success",
        user_request="run operation",
        final_output="done",
        messages=[{"role": "assistant", "content": "done"}],
        duration_ms=123,
    )

    assert len(recorder.snapshots) == 1
    snapshot = recorder.snapshots[0]
    assert snapshot.ontology_id == "demo-ontology"


def test_reflection_worker_and_patcher_do_not_receive_llm_params(monkeypatch):
    captured = {}

    class _ReflectionRecorder:
        def __init__(self, *, max_workers):
            captured["reflection_max_workers"] = max_workers

    def fake_init_operation_patcher(*, config):
        captured["patcher_config"] = config
        return object()

    monkeypatch.setattr("agentic.orchestrator.ReflectionWorker", _ReflectionRecorder)
    monkeypatch.setattr("agentic.orchestrator.init_operation_patcher", fake_init_operation_patcher)

    AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
    )

    assert "reflection_max_workers" not in captured
    assert "patcher_config" in captured


def test_submit_reflection_skips_when_passive_reflection_disabled(caplog):
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
    )

    class _Recorder:
        def __init__(self) -> None:
            self.snapshots = []

        def submit(self, snapshot) -> None:
            self.snapshots.append(snapshot)

    recorder = _Recorder()
    orchestrator.reflection_worker = recorder

    with caplog.at_level("INFO", logger="orchestrator"):
        orchestrator._submit_reflection(
            run_id="run-disabled",
            session_id="sess-disabled",
            turn_id=1,
            user_id="alice",
            workspace_id="workspace-1",
            ontology_id="demo-ontology",
            status="success",
            user_request="run operation",
            final_output="done",
            messages=[{"role": "assistant", "content": "done"}],
            duration_ms=123,
        )

    assert recorder.snapshots == []
    assert "reflection skipped run=run-disabled" in caplog.text
    assert "reason=passive_reflection_disabled" in caplog.text


def test_submit_reflection_logs_when_ontology_missing(caplog):
    config = AgentConfig.from_env()
    config.evolution_passive_reflection_enabled = True
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
        agent_config=config,
    )

    class _Recorder:
        def __init__(self) -> None:
            self.snapshots = []

        def submit(self, snapshot) -> None:
            self.snapshots.append(snapshot)

    recorder = _Recorder()
    orchestrator.reflection_worker = recorder

    with caplog.at_level("INFO", logger="orchestrator"):
        orchestrator._submit_reflection(
            run_id="run-no-ontology",
            session_id="sess-no-ontology",
            turn_id=1,
            user_id="alice",
            workspace_id="workspace-1",
            ontology_id=None,
            status="success",
            user_request="run operation",
            final_output="done",
            messages=[{"role": "assistant", "content": "done"}],
            duration_ms=123,
        )

    assert recorder.snapshots == []
    assert "reflection skipped run=run-no-ontology" in caplog.text
    assert "reason=missing_ontology_id" in caplog.text


def test_stream_agent_loop_emits_custom_before_values(monkeypatch):
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
    )

    class _FakeEngine:
        def __init__(self, *, memory_manager=None, agent_config=None):
            del memory_manager, agent_config

        def run(
            self,
            state,
            *,
            caller,
            event_writer=None,
            snapshot_callback=None,
            iteration_limit=None,
            observer=None,
            event_source_node=None,
        ):
            del caller, iteration_limit, observer, event_source_node
            emit_agent_event(
                run_id=state["run_id"],
                session_id=state["session_id"],
                turn_id=state["current_turn_id"],
                event_type="tool.progress",
                source={"node": "test", "component": "unit"},
                payload={"message": "before values"},
                writer=event_writer,
            )
            state["messages"].append({"role": "assistant", "content": "done"})
            snapshot_callback(state)
            return state

    monkeypatch.setattr("agentic.orchestrator.JiuwenReactLoopEngine", lambda **_kwargs: _FakeEngine())

    events = list(
        orchestrator._stream_agent_loop(
            {
                "run_id": "run-runtime-order",
                "session_id": "sess-runtime-order",
                "current_turn_id": 1,
                "messages": [],
            },
            memory_manager=None,
        )
    )

    assert [event[0] for event in events] == ["custom", "values"]
    assert events[0][1]["data"]["type"] == "tool.progress"
    assert events[1][1]["messages"][-1]["content"] == "done"


def test_stream_agent_loop_can_use_injected_jiuwen_agent_factory(monkeypatch):
    monkeypatch.setattr("agentic.loop.jiuwen_adapter.is_openjiuwen_available", lambda: True)

    config = AgentConfig.from_env()
    fake_agent = _FakeJiuwenAgent({"output": "jiuwen orchestrator answer"})
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
        agent_config=config,
        jiuwen_agent_factory=lambda: fake_agent,
    )

    events = list(
        orchestrator._stream_agent_loop(
            {
                "run_id": "run-jiuwen-orchestrator",
                "session_id": "sess-jiuwen-orchestrator",
                "current_turn_id": 2,
                "user_request": "hello jiuwen",
                "messages": [],
                "results": {},
            },
            memory_manager=None,
        )
    )

    values = [event[1] for event in events if event[0] == "values"]
    custom = [event[1]["data"] for event in events if event[0] == "custom"]
    assert fake_agent.inputs["query"].startswith("hello jiuwen\n\n<system-reminder>")
    assert "# currentDate" in fake_agent.inputs["query"]
    assert values[-1]["final_output"] == "jiuwen orchestrator answer"
    assert values[-1]["messages"][0]["role"] == "system"
    assert values[-1]["messages"][1] == {"role": "user", "content": "hello jiuwen"}
    assert custom[-1]["type"] == "iteration.completed"
    assert custom[-1]["source"] == {"node": "jiuwen_react", "component": "iteration"}


def test_stream_agent_loop_runs_engine_inside_workspace_scope(monkeypatch):
    active_workspaces = []
    observed_scopes = []

    @contextmanager
    def fake_workspace_scope(workspace_id):
        active_workspaces.append(workspace_id)
        try:
            yield workspace_id
        finally:
            active_workspaces.pop()

    class _FakeEngine:
        def __init__(self, *, memory_manager=None, agent_config=None):
            del memory_manager, agent_config

        def run(
            self,
            state,
            *,
            caller,
            event_writer=None,
            snapshot_callback=None,
            iteration_limit=None,
            observer=None,
            event_source_node=None,
        ):
            del caller, event_writer, iteration_limit, observer, event_source_node
            observed_scopes.append(tuple(active_workspaces))
            state["messages"].append({"role": "assistant", "content": "done"})
            snapshot_callback(state)
            return state

    monkeypatch.setattr("agentic.orchestrator.workspace_scope", fake_workspace_scope)
    monkeypatch.setattr("agentic.orchestrator.JiuwenReactLoopEngine", lambda **_kwargs: _FakeEngine())

    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(
        orchestrator._stream_agent_loop(
            {
                "run_id": "run-workspace-scope",
                "session_id": "sess-workspace-scope",
                "current_turn_id": 1,
                "workspace_id": "workspace-a",
                "messages": [],
            },
            memory_manager=None,
        )
    )

    assert observed_scopes == [("workspace-a",)]
    assert events[-1][0] == "values"


def test_stream_agent_loop_does_not_deepcopy_engine_snapshots(monkeypatch):
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
    )

    class _Snapshot(dict):
        def __deepcopy__(self, memo):
            del memo
            raise AssertionError("orchestrator should not copy engine snapshots")

    class _FakeEngine:
        def __init__(self, *, memory_manager=None, agent_config=None):
            del memory_manager, agent_config

        def run(
            self,
            state,
            *,
            caller,
            event_writer=None,
            snapshot_callback=None,
            iteration_limit=None,
            observer=None,
            event_source_node=None,
        ):
            del caller, event_writer, iteration_limit, observer, event_source_node
            state["messages"].append({"role": "assistant", "content": "done"})
            snapshot_callback(_Snapshot({"messages": list(state["messages"])}))
            return state

    monkeypatch.setattr("agentic.orchestrator.JiuwenReactLoopEngine", lambda **_kwargs: _FakeEngine())

    events = list(
        orchestrator._stream_agent_loop(
            {
                "run_id": "run-runtime-no-snapshot-copy",
                "session_id": "sess-runtime-no-snapshot-copy",
                "current_turn_id": 1,
                "messages": [],
            },
            memory_manager=None,
        )
    )

    assert events[0][0] == "values"
    assert events[0][1]["messages"][-1]["content"] == "done"


def test_stream_agent_loop_propagates_engine_errors(monkeypatch):
    orchestrator = AgenticOrchestrator(
        memory_factory=_FakeMemoryFactory(),
    )

    class _FailingEngine:
        def __init__(self, *, memory_manager=None, agent_config=None):
            del memory_manager, agent_config

        def run(self, state, **kwargs):
            del state, kwargs
            raise RuntimeError("boom")

    monkeypatch.setattr("agentic.orchestrator.JiuwenReactLoopEngine", lambda **_kwargs: _FailingEngine())

    with pytest.raises(RuntimeError, match="boom"):
        list(
            orchestrator._stream_agent_loop(
                {
                    "run_id": "run-runtime-error",
                    "session_id": "sess-runtime-error",
                    "current_turn_id": 1,
                    "messages": [],
                },
                memory_manager=None,
            )
        )


def test_orchestrator_uses_requested_run_id(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

    class _SinglePassGraph:
        def stream(self, graph_input, config, stream_mode=None):
            del config, stream_mode
            state = dict(graph_input)
            state["messages"] = [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": graph_input.get("user_request", "")},
                {"role": "assistant", "content": "done"},
            ]
            state["final_output"] = {"message": "done"}
            yield ("values", state)

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        graph = _SinglePassGraph()
        yield from graph.stream(
            initial_state,
            {"configurable": {"thread_id": initial_state.get("run_id")}},
            stream_mode=["values"],
        )

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(
        orchestrator.run(
            user_request="hello",
            session_id="sess-requested-run",
            requested_run_id="client-run-123",
        )
    )

    turn_start = next(
        event for event in events
        if event.get("event") == "agent.v2" and event.get("data", {}).get("type") == "turn.started"
    )
    assert turn_start["data"]["run_id"] == "client-run-123"

    run = run_store.get_run("client-run-123")
    assert run is not None


def test_orchestrator_buffers_custom_events_until_flush(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    actions = []

    real_append_event = chat_store.append_event

    def spy_append_event(session_id, event):
        actions.append(("append_event", event.get("data", {}).get("type")))
        return real_append_event(session_id, event)

    def spy_append_events(session_id, events):
        events = list(events)
        actions.append(("append_events", [event.get("data", {}).get("type") for event in events]))
        for event in events:
            real_append_event(session_id, event)
        return True

    chat_store.append_event = spy_append_event
    chat_store.append_events = spy_append_events

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        for text in ("a", "b"):
            yield (
                "custom",
                build_agent_event(
                    run_id=initial_state["run_id"],
                    session_id=initial_state["session_id"],
                    turn_id=initial_state["current_turn_id"],
                    event_type="token.delta",
                    source={"node": "agent_loop", "component": "llm"},
                    payload={"channel": "content", "text": text, "iteration": 1},
                ),
            )
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "ab"},
        ]
        state["final_output"] = "ab"
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    stream = orchestrator.run(
        user_request="hello",
        session_id="sess-buffered-events",
        requested_run_id="run-buffered-events",
    )

    first = next(stream)
    actions.append(("yield", first["data"]["type"]))
    second = next(stream)
    actions.append(("yield", second["data"]["type"]))

    assert first["data"]["type"] == "turn.started"
    assert second["data"]["type"] == "token.delta"
    assert ("append_event", "token.delta") not in actions

    remaining = list(stream)
    assert any(event["data"]["type"] == "turn.finished" for event in remaining)
    assert ("append_events", ["token.delta", "token.delta"]) in actions

    event_log_path = tmp_path / "chats" / "sess-buffered-events" / "events.jsonl"
    snapshot_path = tmp_path / "chats" / "sess-buffered-events" / "snapshot.json"

    log_lines = event_log_path.read_text(encoding="utf-8").splitlines()

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert log_lines or snapshot.get("events")

    loaded_session = chat_store.get_session("sess-buffered-events")
    assert loaded_session is not None
    token_events = [
        event for event in loaded_session.events
        if event.get("data", {}).get("type") == "token.delta"
    ]
    assert len(token_events) == 1
    assert token_events[0]["data"]["payload"]["text"] == "ab"


def test_orchestrator_emits_run_finished_before_final_event_is_yielded(monkeypatch, tmp_path):
    from agentic.observability import RuntimeEventType

    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    trace_events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            trace_events.append(event.type)
            return True

        def close(self, timeout=0.0):
            del timeout

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = "done"
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)
    monkeypatch.setattr("agentic.orchestrator.get_observer", lambda: RecordingObserver())

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    stream = orchestrator.run(
        user_request="hello",
        session_id="sess-trace-before-final",
        requested_run_id="run-trace-before-final",
    )
    try:
        for event in stream:
            if event.get("data", {}).get("type") == "turn.finished":
                break
        assert RuntimeEventType.RUN_FINISHED in trace_events
    finally:
        stream.close()


def test_orchestrator_does_not_mark_trace_success_before_final_event_persists(monkeypatch, tmp_path):
    from agentic.observability import RuntimeEventType

    class FailingFinalEventChatStore(ChatStore):
        def append_event(self, session_id, event):
            data = event.get("data", {}) if isinstance(event, dict) else {}
            payload = data.get("payload", {}) if isinstance(data, dict) else {}
            if data.get("type") == "turn.finished" and payload.get("status") == "success":
                raise RuntimeError("final event persistence failed")
            return super().append_event(session_id, event)

    run_store = RunStore()
    chat_store = FailingFinalEventChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    trace_events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            trace_events.append(event.type)
            return True

        def close(self, timeout=0.0):
            del timeout

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = "done"
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)
    monkeypatch.setattr("agentic.orchestrator.get_observer", lambda: RecordingObserver())

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(orchestrator.run(
        user_request="hello",
        session_id="sess-final-event-fail",
        requested_run_id="run-final-event-fail",
    ))

    assert RuntimeEventType.RUN_FINISHED not in trace_events
    assert RuntimeEventType.RUN_FAILED in trace_events
    assert events[-1]["data"]["type"] == "turn.finished"
    assert events[-1]["data"]["payload"]["status"] == "error"


def test_orchestrator_clears_run_scoped_subagent_manager(monkeypatch, tmp_path):
    from agentic.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del memory_manager, observer
        manager_module.get_subagent_manager(initial_state["run_id"], self.agent_config)
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = {"message": "done"}
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="hello",
            session_id="sess-subagent-cleanup",
            requested_run_id="run-subagent-cleanup",
        )
    )

    assert "run-subagent-cleanup" not in manager_module._RUN_MANAGERS


def test_orchestrator_run_trace_includes_agent_mode(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    runtime_events = []

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = {"message": "done"}
        yield ("values", state)

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            runtime_events.append(event)
            return True

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)
    monkeypatch.setattr("agentic.orchestrator.get_observer", lambda: RecordingObserver())

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="hello",
            session_id="sess-agent-mode-log",
            user_context={"agent_mode": "thinking"},
        )
    )

    run_started = next(event for event in runtime_events if event.type == "run.started")
    assert run_started.payload["agent_mode"] == "thinking"


def test_orchestrator_restores_read_documents_between_new_session_turns(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    object_path = "ontologies/demo/objects/主CHANGE_ORDER.md"
    read_documents = merge_read_documents_registry(
        None,
        [
            build_read_document_record(
                file_path=object_path,
                content="# 主CHANGE_ORDER",
                source="ontology_context",
                turn_id=1,
                iteration=1,
            )
        ],
    )
    seen_initial_read_documents = []

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        seen_initial_read_documents.append(initial_state.get("read_documents"))
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = {"message": "done"}
        if state["current_turn_id"] == 1:
            state["read_documents"] = read_documents
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(orchestrator.run(user_request="第一轮读取主CHANGE_ORDER", session_id="sess-read-docs"))
    list(orchestrator.run(user_request="第二轮查询主CHANGE_ORDER", session_id="sess-read-docs"))

    assert seen_initial_read_documents[0]["by_path"] == {}
    assert object_path in seen_initial_read_documents[1]["by_path"]


def test_orchestrator_uses_requested_model_profile(monkeypatch, tmp_path):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm_profiles": {
    "default_profile_id": "fast",
    "profiles": [
      {
        "id": "fast",
        "name": "Fast Model",
        "provider": "openai_compatible",
        "base_url": "https://fast.example/v1",
        "model": "fast-model",
        "api_key": "fast-secret",
        "enabled": true
      },
      {
        "id": "accurate",
        "name": "Accurate Model",
        "provider": "deepseek",
        "base_url": "https://accurate.example/v1",
        "model": "accurate-model",
        "api_key": "accurate-secret",
        "enabled": true
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    configure_llm_router(
        LLMRouter(
            AppConfig.from_env(),
            agent_config=AgentConfig.from_env(),
        )
    )

    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        selected_config = get_llm_router().get_config(
            LLMPurpose.MAIN,
            profile_id=initial_state["user_context"]["model_profile_id"],
        )
        assert selected_config.profile_id == "accurate"
        assert selected_config.provider == "deepseek"
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = {"message": "done"}
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="hello",
            session_id="sess-model-profile",
            user_context={"model_profile_id": "accurate"},
            requested_run_id="run-model-profile",
        )
    )

    run = run_store.get_run("run-model-profile")
    assert run.metadata["model_profile_id"] == "accurate"
    assert run.metadata["model_provider"] == "deepseek"


def test_orchestrator_rejects_invalid_selected_ontology_id(tmp_path):
    orchestrator = AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=ChatStore(str(tmp_path / "chats")),
        execution_store=ExecutionStore(str(tmp_path / "executions")),
        memory_factory=_FakeMemoryFactory(),
    )

    with pytest.raises(ValueError, match="Invalid selected_ontology_id"):
        list(
            orchestrator.run(
                user_request="hello",
                session_id="sess-invalid-ontology",
                user_context={"selected_ontology_id": "not-exists"},
            )
        )


def test_orchestrator_emits_awaiting_input_for_pending_user_interaction(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    reflection_calls = []

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        state = dict(initial_state)
        interaction = {
            "id": "interaction_call_user",
            "type": "input",
            "prompt": "请补充物料编号",
            "options": [],
            "tool_call_id": "call_user",
        }
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_user",
                        "type": "function",
                        "function": {"name": "request_user_interaction", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "request_user_interaction",
                "tool_call_id": "call_user",
                "content": '{"status":"awaiting_input"}',
            },
        ]
        state["pending_user_interaction"] = interaction
        state["final_output"] = {
            "type": "user_interaction",
            "text": interaction["prompt"],
            "options": [],
            "interaction": interaction,
        }
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)
    monkeypatch.setattr(
        AgenticOrchestrator,
        "_submit_reflection",
        lambda self, **kwargs: reflection_calls.append(kwargs),
    )

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(orchestrator.run(user_request="hello", session_id="sess-await"))
    finished = next(
        event for event in events
        if event.get("event") == "agent.v2" and event.get("data", {}).get("type") == "turn.finished"
    )

    assert finished["data"]["payload"]["status"] == "awaiting_input"
    assert finished["data"]["payload"]["final_output"]["type"] == "user_interaction"
    assert run_store.get_run(finished["data"]["run_id"]).status == RunStatus.AWAITING
    history = execution_store.get_session_history("sess-await")
    assert history["turns"][0]["status"] == "awaiting_input"
    assert reflection_calls == []


def test_orchestrator_injects_interaction_response_into_resume_message(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.set_llm_context(
        "sess-resume",
        [
            {"role": "system", "content": "sys"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_user",
                        "type": "function",
                        "function": {"name": "request_user_interaction", "arguments": "{}"},
                    }
                ],
            },
            {
                "role": "tool",
                "name": "request_user_interaction",
                "tool_call_id": "call_user",
                "content": '{"status":"awaiting_input"}',
            },
        ],
    )
    captured = {}

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        captured["user_message"] = initial_state["messages"][-1]["content"]
        state = dict(initial_state)
        state["messages"] = list(initial_state["messages"]) + [
            {"role": "assistant", "content": "done"},
        ]
        state["final_output"] = "done"
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="方案A",
            session_id="sess-resume",
            user_context={
                "interaction_response": {
                    "interaction_id": "interaction_call_user",
                    "type": "single_choice",
                    "value": "A",
                    "option_id": "a",
                }
            },
        )
    )

    assert "<user-interaction-response>" in captured["user_message"]
    assert '"interaction_id": "interaction_call_user"' in captured["user_message"]
    assert '"option_id": "a"' in captured["user_message"]


def test_orchestrator_memory_hooks_do_not_require_session_id(tmp_path):
    orchestrator = AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=ChatStore(str(tmp_path / "chats")),
        execution_store=ExecutionStore(str(tmp_path / "executions")),
        memory_factory=_FakeMemoryFactory(),
    )

    class _Manager:
        def __init__(self) -> None:
            self.session_messages = []
            self.turn_summaries = []

        def append_session_message(self, **kwargs) -> None:
            self.session_messages.append(kwargs)

        def append_turn_summary(self, **kwargs) -> None:
            self.turn_summaries.append(kwargs)

        def build_memory_context(self, query: str, ov_session_id: str | None) -> str:
            return "## Relevant Long-Term Memory\n- [{}] {}".format(
                ov_session_id,
                query,
            )

    manager = _Manager()

    orchestrator._append_memory_session_message(
        manager,
        ov_session_id=None,
        role="user",
        content="remember this",
        metadata={"scope": "user"},
    )
    orchestrator._append_memory_turn_summary(
        manager,
        ov_session_id=None,
        user_request="remember this",
        final_output={"status": "ok"},
        status="completed",
    )
    memory_context = orchestrator._build_memory_context(
        manager,
        query="remember this",
        ov_session_id=None,
        allow_injection=True,
    )

    assert manager.session_messages == [
        {
            "ov_session_id": None,
            "role": "user",
            "content": "remember this",
            "metadata": {"scope": "user"},
        }
    ]
    assert manager.turn_summaries == [
        {
            "ov_session_id": None,
            "user_request": "remember this",
            "final_output": {"status": "ok"},
            "status": "completed",
        }
    ]
    assert memory_context == "## Relevant Long-Term Memory\n- [None] remember this"


def test_orchestrator_records_evolution_recall_outcomes(monkeypatch, tmp_path):
    orchestrator = AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=ChatStore(str(tmp_path / "chats")),
        execution_store=ExecutionStore(str(tmp_path / "executions")),
        memory_factory=_FakeMemoryFactory(),
    )

    class _Buffer:
        def __init__(self) -> None:
            self.calls = []

        def record_outcome(self, fingerprints, **kwargs) -> None:
            self.calls.append((list(fingerprints), kwargs))

    buffer = _Buffer()
    monkeypatch.setattr("agentic.orchestrator.get_proposal_buffer", lambda: buffer)

    orchestrator._record_evolution_recall_outcomes(
        {
            "evolution_recall_records": [
                {
                    "workspace_id": "ws-1",
                    "ontology_id": "demo",
                    "operation_key": "流程A",
                    "fingerprints": ["fp-1", "", "fp-2"],
                }
            ]
        }
    )

    assert buffer.calls == [
        (
            ["fp-1", "fp-2"],
            {
                "success": True,
                "workspace_id": "ws-1",
                "ontology_id": "demo",
                "operation_key": "流程A",
            },
        )
    ]
