"""Focused lifecycle tests for the owned OpenJiuwen runtime boundary."""

from __future__ import annotations

import asyncio
import importlib
import threading
from types import SimpleNamespace

import pytest

import agent.core.jiuwen_engine as jiuwen_engine
from agent.core.jiuwen_engine import JiuwenReactLoopEngine
from agent.exceptions import WorkflowCancelledError


_REAL_IMPORT_MODULE = importlib.import_module


class _FakeAgentCallbackEvent:
    BEFORE_MODEL_CALL = "before_model_call"
    AFTER_MODEL_CALL = "after_model_call"
    ON_MODEL_EXCEPTION = "on_model_exception"
    AFTER_TOOL_CALL = "after_tool_call"


class _FakeCallbackFramework:
    def __init__(self) -> None:
        self._callbacks = {}
        self._metrics = {}


class _FakeCallbackManager:
    def __init__(
        self,
        agent_id: str,
        framework: _FakeCallbackFramework,
        *,
        clear_error: Exception | None = None,
    ) -> None:
        self.agent_id = agent_id
        self.framework = framework
        self.clear_error = clear_error
        self.clear_calls = 0

    def _get_agent_event(self, event) -> str:
        return "{}_{}".format(self.agent_id, event)

    async def clear(self) -> None:
        self.clear_calls += 1
        if self.clear_error is not None:
            raise self.clear_error
        for event in (
            _FakeAgentCallbackEvent.BEFORE_MODEL_CALL,
            _FakeAgentCallbackEvent.AFTER_MODEL_CALL,
            _FakeAgentCallbackEvent.ON_MODEL_EXCEPTION,
            _FakeAgentCallbackEvent.AFTER_TOOL_CALL,
        ):
            self.framework._callbacks.pop(self._get_agent_event(event), None)


def _install_fake_runner(monkeypatch, framework, released_sessions):
    class FakeRunner:
        callback_framework = framework

        @classmethod
        async def release(cls, session_id):
            released_sessions.append(session_id)

    def fake_import_module(name):
        if name == "openjiuwen.core.runner":
            return SimpleNamespace(Runner=FakeRunner)
        return _REAL_IMPORT_MODULE(name)

    monkeypatch.setattr(jiuwen_engine.importlib, "import_module", fake_import_module)
    monkeypatch.setattr(
        jiuwen_engine,
        "_jiuwen_agent_callback_event_class",
        lambda: _FakeAgentCallbackEvent,
    )


def test_owned_runtime_cleanup_removes_only_owned_callbacks_metrics_and_session(monkeypatch):
    framework = _FakeCallbackFramework()
    released_sessions = []
    manager = _FakeCallbackManager("agent-a", framework)
    agent = SimpleNamespace(
        card=SimpleNamespace(id="agent-a"),
        agent_callback_manager=manager,
    )
    owned_event = manager._get_agent_event(_FakeAgentCallbackEvent.BEFORE_MODEL_CALL)
    unrelated_event = "agent-b_before_model_call"
    framework._callbacks = {
        owned_event: [object()],
        unrelated_event: [object()],
    }
    framework._metrics = {
        "{}:_on_before_model_call".format(owned_event): object(),
        "{}:_on_before_model_call".format(unrelated_event): object(),
    }
    _install_fake_runner(monkeypatch, framework, released_sessions)

    asyncio.run(
        jiuwen_engine._dispose_owned_jiuwen_runtime(
            agent,
            session_id="agent-a.session.1",
        )
    )

    assert manager.clear_calls == 1
    assert framework._callbacks == {unrelated_event: framework._callbacks[unrelated_event]}
    assert list(framework._metrics) == ["{}:_on_before_model_call".format(unrelated_event)]
    assert released_sessions == ["agent-a.session.1"]


def test_owned_runtime_cleanup_releases_session_when_callback_clear_fails(monkeypatch):
    framework = _FakeCallbackFramework()
    released_sessions = []
    manager = _FakeCallbackManager(
        "agent-failing-clear",
        framework,
        clear_error=RuntimeError("clear failed"),
    )
    agent = SimpleNamespace(
        card=SimpleNamespace(id="agent-failing-clear"),
        agent_callback_manager=manager,
    )
    owned_event = manager._get_agent_event(_FakeAgentCallbackEvent.AFTER_MODEL_CALL)
    framework._metrics = {
        "{}:_on_after_model_call".format(owned_event): object(),
        "agent-unrelated_after_model_call:_on_after_model_call": object(),
    }
    _install_fake_runner(monkeypatch, framework, released_sessions)

    with pytest.raises(RuntimeError, match="clear failed"):
        asyncio.run(
            jiuwen_engine._dispose_owned_jiuwen_runtime(
                agent,
                session_id="agent-failing-clear.session.1",
            )
        )

    assert list(framework._metrics) == [
        "agent-unrelated_after_model_call:_on_after_model_call"
    ]
    assert released_sessions == ["agent-failing-clear.session.1"]


def _offline_agent_config():
    return SimpleNamespace(
        main_agent_max_iterations=1,
        sub_agent_max_iterations=1,
        context_max_rounds=10,
        web_search=SimpleNamespace(enabled=False),
        limits=SimpleNamespace(model_output_token_limit=4096),
    )


def _offline_llm_config():
    return SimpleNamespace(
        provider="openai",
        model="offline",
        api_key="",
        base_url="http://127.0.0.1/unused",
        timeout=1,
        temperature=0,
        max_tokens=64,
    )


def _install_offline_engine_boundary(monkeypatch, create_agent, dispose):
    monkeypatch.setattr(jiuwen_engine, "is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(
        jiuwen_engine,
        "_resolve_jiuwen_llm_config",
        lambda *args, **kwargs: _offline_llm_config(),
    )
    monkeypatch.setattr(jiuwen_engine, "create_default_jiuwen_agent", create_agent)
    monkeypatch.setattr(jiuwen_engine, "register_current_runtime_tools", lambda *args, **kwargs: [])
    monkeypatch.setattr(jiuwen_engine, "unregister_current_runtime_tools", lambda *args, **kwargs: None)
    monkeypatch.setattr(jiuwen_engine, "_dispose_owned_jiuwen_runtime", dispose)
    monkeypatch.setattr(
        JiuwenReactLoopEngine,
        "_tools_schema_for_state",
        lambda self, state, caller: [],
    )
    monkeypatch.setattr(
        JiuwenReactLoopEngine,
        "_jiuwen_tool_infos_for_state",
        lambda self, state, caller: [],
    )
    monkeypatch.setattr(
        JiuwenReactLoopEngine,
        "_hydrate_selected_ontology_read_documents",
        lambda self, state: None,
    )


def _runtime_state(run_id: str, *, session_id: str = "shared-business-session"):
    return {
        "run_id": run_id,
        "session_id": session_id,
        "current_turn_id": 1,
        "user_request": "offline request",
        "messages": [],
        "results": {},
        "user_context": {"llm_request_mode": "sync"},
    }


def test_default_runtime_uses_unique_internal_sessions_for_concurrent_business_session(monkeypatch):
    created_agents = []
    disposed = []
    counter_lock = threading.Lock()

    class ModelBarrier:
        def __init__(self) -> None:
            self.condition = None
            self.arrived = 0
            self.max_active = 0

        async def wait(self) -> None:
            if self.condition is None:
                self.condition = asyncio.Condition()
            async with self.condition:
                self.arrived += 1
                self.max_active = max(self.max_active, self.arrived)
                if self.arrived == 2:
                    self.condition.notify_all()
                else:
                    await self.condition.wait_for(lambda: self.arrived == 2)

    barrier = ModelBarrier()

    class OfflineAgent:
        def __init__(self, agent_id):
            self.card = SimpleNamespace(id=agent_id)
            self.inputs = None

        async def invoke(self, inputs, session=None):
            del session
            self.inputs = dict(inputs)
            await barrier.wait()
            return {"output": "ok", "result_type": "answer"}

    def create_agent(**kwargs):
        del kwargs
        with counter_lock:
            agent = OfflineAgent("owned-agent-{}".format(len(created_agents) + 1))
            created_agents.append(agent)
        return agent

    async def dispose(agent, *, session_id):
        disposed.append((agent.card.id, session_id))

    _install_offline_engine_boundary(monkeypatch, create_agent, dispose)

    async def run(run_id):
        return await JiuwenReactLoopEngine(agent_config=_offline_agent_config()).arun(
            _runtime_state(run_id),
            caller="main",
            system_prompt="offline",
        )

    async def exercise():
        return await asyncio.gather(run("run-a"), run("run-b"))

    results = asyncio.run(exercise())

    internal_sessions = [agent.inputs["conversation_id"] for agent in created_agents]
    assert [result["final_output"] for result in results] == ["ok", "ok"]
    assert barrier.max_active == 2
    assert len(set(internal_sessions)) == 2
    assert "shared-business-session" not in internal_sessions
    assert {session_id for _agent_id, session_id in disposed} == set(internal_sessions)


def test_custom_agent_factory_retains_business_session_and_is_not_disposed(monkeypatch):
    disposed = []

    class CustomAgent:
        def __init__(self):
            self.inputs = None

        async def invoke(self, inputs, session=None):
            del session
            self.inputs = dict(inputs)
            return {"output": "custom", "result_type": "answer"}

    custom_agent = CustomAgent()

    async def dispose(agent, *, session_id):
        disposed.append((agent, session_id))

    monkeypatch.setattr(jiuwen_engine, "is_openjiuwen_available", lambda: True)
    monkeypatch.setattr(jiuwen_engine, "_dispose_owned_jiuwen_runtime", dispose)
    monkeypatch.setattr(
        JiuwenReactLoopEngine,
        "_tools_schema_for_state",
        lambda self, state, caller: [],
    )
    monkeypatch.setattr(
        JiuwenReactLoopEngine,
        "_jiuwen_tool_infos_for_state",
        lambda self, state, caller: [],
    )
    monkeypatch.setattr(
        JiuwenReactLoopEngine,
        "_hydrate_selected_ontology_read_documents",
        lambda self, state: None,
    )

    state = _runtime_state("run-custom", session_id="business-session-custom")
    engine = JiuwenReactLoopEngine(
        agent_config=_offline_agent_config(),
        agent_factory=lambda: custom_agent,
    )
    asyncio.run(engine.arun(state, caller="main", system_prompt="offline"))

    assert custom_agent.inputs["conversation_id"] == "business-session-custom"
    assert disposed == []


@pytest.mark.parametrize("outcome", ("success", "error", "cancel"))
def test_owned_cleanup_failure_preserves_engine_outcome(monkeypatch, outcome):
    disposed_sessions = []

    class OfflineAgent:
        card = SimpleNamespace(id="owned-cleanup-failure-agent")

        async def invoke(self, inputs, session=None):
            del inputs, session
            if outcome == "error":
                raise ValueError("primary invoke failure")
            if outcome == "cancel":
                raise WorkflowCancelledError("primary cancellation")
            return {"output": "primary success", "result_type": "answer"}

    def create_agent(**kwargs):
        del kwargs
        return OfflineAgent()

    async def dispose(agent, *, session_id):
        del agent
        disposed_sessions.append(session_id)
        raise RuntimeError("callback clear failed")

    _install_offline_engine_boundary(monkeypatch, create_agent, dispose)
    engine = JiuwenReactLoopEngine(agent_config=_offline_agent_config())

    if outcome == "success":
        result = asyncio.run(
            engine.arun(
                _runtime_state("run-cleanup-success"),
                caller="main",
                system_prompt="offline",
            )
        )
        assert result["final_output"] == "primary success"
    elif outcome == "error":
        with pytest.raises(ValueError, match="primary invoke failure"):
            asyncio.run(
                engine.arun(
                    _runtime_state("run-cleanup-error"),
                    caller="main",
                    system_prompt="offline",
                )
            )
    else:
        with pytest.raises(WorkflowCancelledError, match="primary cancellation"):
            asyncio.run(
                engine.arun(
                    _runtime_state("run-cleanup-cancel"),
                    caller="main",
                    system_prompt="offline",
                )
            )

    assert len(disposed_sessions) == 1
    assert disposed_sessions[0].startswith("owned-cleanup-failure-agent.session.")


def test_managed_stream_cancellation_waits_for_child_producer_cleanup(monkeypatch):
    monkeypatch.setattr(jiuwen_engine, "is_openjiuwen_available", lambda: True)
    producer_started = threading.Event()
    producer_cancelled = threading.Event()
    cancel_event = threading.Event()

    class ContextEngine:
        def __init__(self):
            self.saved = []

        async def save_contexts(self, session):
            self.saved.append(session)

    class ManagedAgent:
        card = SimpleNamespace(id="managed-cancel-agent")

        def __init__(self):
            self.context_engine = ContextEngine()

        async def invoke(self, inputs, session=None, _streaming=False):
            del inputs, session, _streaming
            producer_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                producer_cancelled.set()
                raise

        async def write_invoke_result_to_stream(self, result, session):
            del result, session

    class ManagedSession:
        def __init__(self):
            self.queue = asyncio.Queue()
            self.pre_run_inputs = None
            self.closed = False
            self.committed = False

        async def pre_run(self, **kwargs):
            self.pre_run_inputs = kwargs.get("inputs")

        async def stream_iterator(self):
            while True:
                item = await self.queue.get()
                if item is None:
                    return
                yield item

        async def close_stream(self):
            if not self.closed:
                self.closed = True
                await self.queue.put(None)

        async def commit(self):
            self.committed = True

    agent = ManagedAgent()
    session = ManagedSession()
    engine = JiuwenReactLoopEngine(
        agent_config=_offline_agent_config(),
        agent_factory=lambda: agent,
    )

    async def exercise():
        task = asyncio.create_task(
            engine._consume_managed_agent_stream(
                agent,
                {"query": "offline", "conversation_id": "managed-cancel-session"},
                session=session,
                state={"run_id": "managed-cancel-run"},
                caller="main",
                event_writer=None,
                iteration_provider=None,
                cancel_event=cancel_event,
                token_callback=None,
                reasoning_summary_callback=None,
                event_source_node=None,
            )
        )
        for _ in range(100):
            if producer_started.is_set():
                break
            await asyncio.sleep(0)
        assert producer_started.is_set()
        cancel_event.set()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0)
        return [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name() == "jiuwen-managed-stream-producer"
        ]

    pending_managed_producers = asyncio.run(exercise())

    assert producer_cancelled.is_set()
    assert session.closed is True
    assert session.committed is True
    assert agent.context_engine.saved == [session]
    assert pending_managed_producers == []
