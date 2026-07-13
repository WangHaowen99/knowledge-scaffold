"""Tests for cancellation handling in orchestrator flow."""

from types import SimpleNamespace

import pytest

from agent.core.orchestrator import AgenticOrchestrator
from llm import configure_llm_router, reset_llm_router_for_tests
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from persistence.run_store import RunStore


class _FakeRouter:
    def cancel_run(self, run_id: str) -> None:
        del run_id

    def clear_run(self, run_id: str) -> None:
        del run_id


@pytest.fixture(autouse=True)
def _reset_router(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "unit-model")
    configure_llm_router(_FakeRouter())
    yield
    reset_llm_router_for_tests()


def _patch_stream_agent_loop(monkeypatch, graph) -> None:
    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        yield from graph.stream(
            initial_state,
            {"configurable": {"thread_id": initial_state.get("run_id")}},
            stream_mode=["custom", "values"],
        )

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)


class _FakeLLM:
    def __init__(self) -> None:
        self.config = SimpleNamespace(model="unit-model", api_key=None)
        self.call_count = 0

    def call(self, *args, **kwargs):
        self.call_count += 1
        return SimpleNamespace(content="unused", tool_calls=None)

    def cancel_run(self, run_id: str) -> None:
        return

    def clear_run(self, run_id: str) -> None:
        return


class _FakeMemoryFactory:
    class _Manager:
        def ensure_session(self, app_session_id: str, existing_session_id=None) -> str:
            del app_session_id
            return existing_session_id or "ov-session-1"

        def append_session_message(self, **kwargs) -> None:
            return

        def build_memory_context(self, query: str, ov_session_id: str) -> str:
            del query, ov_session_id
            return ""

        def append_turn_summary(self, **kwargs) -> None:
            return

        def commit_session(self, ov_session_id: str) -> None:
            del ov_session_id
            return

    def __init__(self) -> None:
        self.manager = self._Manager()

    def for_user(self, user_id):
        del user_id
        return self.manager


class _FakeGraph:
    def __init__(self, run_store: RunStore, cancel_reason: str) -> None:
        self.run_store = run_store
        self.cancel_reason = cancel_reason

    def stream(self, graph_input, config, stream_mode=None):
        run_id = config["configurable"]["thread_id"]
        # Yield state update first so last_state gets updated before cancellation
        state = dict(graph_input)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": graph_input.get("user_request", "")},
        ]
        yield ("values", state)
        # Cancel on next iteration so the loop picks it up
        self.run_store.cancel_run(run_id, metadata={"cancel_reason": self.cancel_reason})
        yield ("values", state)


@pytest.mark.parametrize(
    "cancel_reason, expect_messages_persisted",
    [
        ("user_stop", True),
        ("disconnect", False),
    ],
)
def test_cancelled_turn_persistence(monkeypatch, tmp_path, cancel_reason, expect_messages_persisted):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    fake_llm = _FakeLLM()
    fake_graph = _FakeGraph(run_store, cancel_reason)

    _patch_stream_agent_loop(monkeypatch, fake_graph)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    session_id = "session_cancelled"
    events = list(orchestrator.run(user_request="half-run request", session_id=session_id))
    assert events
    # No LLM summarization call should happen
    assert fake_llm.call_count == 0

    turn_start = next(e for e in events if e.get("event") == "agent.v2" and e.get("data", {}).get("type") == "turn.started")
    run_id = turn_start["data"]["run_id"]
    turn_finish = next(e for e in events if e.get("event") == "agent.v2" and e.get("data", {}).get("type") == "turn.finished")

    assert turn_finish["data"]["payload"]["status"] == "cancelled"
    assert turn_finish["data"]["payload"]["cancel_reason"] == cancel_reason

    # For user_stop, messages are persisted so next turn has context
    llm_context = execution_store.get_llm_context(session_id)
    if expect_messages_persisted:
        assert len(llm_context) >= 1
        # Verify they are OpenAI-format messages (have "role" key)
        assert llm_context[0].get("role") == "system"
    else:
        assert len(llm_context) == 0

    # Turn execution record is always created
    history = execution_store.get_session_history(session_id)
    assert history is not None
    turn = next((t for t in history["turns"] if t["turn_id"] == 1), None)
    assert turn is not None
    assert turn["status"] == "cancelled"

    run = run_store.get_run(run_id)
    assert run is not None
    assert run.metadata["cancel_reason"] == cancel_reason
