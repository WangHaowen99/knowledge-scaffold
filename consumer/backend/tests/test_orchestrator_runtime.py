"""Tests for the orchestrator-owned agent loop runtime."""

import asyncio
from contextlib import contextmanager
import json
import threading
import time
from types import SimpleNamespace

import httpx
from openai import APIStatusError
import pytest

import agent.core.orchestrator as orchestrator_module
from agent.core.orchestrator import (
    AgenticOrchestrator,
    _build_deterministic_artifact_writer,
    _public_workflow_error_message,
    _release_workflow_sandboxes,
)
from agent.direct_execution.contracts import (
    AwaitingInput as DeterministicAwaitingInput,
    Completed as DeterministicCompleted,
    Failed as DeterministicFailed,
)
from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter
from agent.core.jiuwen_engine import _JiuwenTerminalErrorSignal
from agent.exceptions import TerminalToolExecutionError
from agent.tools.base import ToolContext
from agent.tools.events import build_tool_finished_payload, emit_tool_finished
from agent.tools.executor import execute_tool_batch
from config import AgentConfig, AppConfig
from config.workspace_llm_profiles import update_workspace_llm_profiles
from llm import LLMPurpose, LLMRouter, configure_llm_router, get_llm_router, reset_llm_router_for_tests
from integration.observability import stack_profiler as stack_profiler_module
from ontology.ontology_catalog import (
    ObjectReferenceLookupResult,
    ObjectReferenceStatus,
)
from persistence.chat_store import ChatStore
from persistence.execution_store import INTERACTION_CANCELLED_MESSAGE, ExecutionStore
from persistence.run_store import RunStore
from persistence.run_store import RunStatus
from utils.read_docs import build_read_document_record, merge_read_documents_registry
from utils.streaming import build_agent_event, emit_agent_event
from workspace.workspaces import create_workspace


def test_terminal_cleanup_retries_deterministic_parser_companion_sandbox(monkeypatch):
    released_companions = []
    released_main = []
    monkeypatch.setattr(
        orchestrator_module,
        "release_run_sandbox_companions",
        lambda run_id: released_companions.append(run_id),
    )
    monkeypatch.setattr(
        orchestrator_module,
        "release_run_sandbox",
        lambda run_id: released_main.append(run_id),
    )

    _release_workflow_sandboxes("run-1")

    assert released_companions == ["run-1"]
    assert released_main == ["run-1"]


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


def test_public_workflow_error_does_not_expose_generic_value_error_by_prefix():
    assert (
        _public_workflow_error_message(ValueError("输入 Token 长度超过上限: internal"))
        == "Workflow execution failed"
    )


def test_public_workflow_error_preserves_terminal_tool_message():
    exc = TerminalToolExecutionError(
        error_code="GRAPH_BACKEND_TIMEOUT",
        public_message="Graph query timed out.",
        tool_name="business_data_search",
    )

    assert _public_workflow_error_message(exc) == "Graph query timed out."


@pytest.fixture(autouse=True)
def _configure_runtime_llm(monkeypatch, tmp_path):
    import config.system_config as system_config_module
    from integration.observability.provider import reset_observer
    from system_config_migration_helpers import apply_test_system_config_migrations

    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("ONTO_PLATFORM_ENABLED", "false")
    monkeypatch.setenv("ONTO_PLATFORM_CONFIG_PATH", str(runtime_root / "config" / "platform_config.json"))
    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
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
    apply_test_system_config_migrations(
        config_path,
        monkeypatch,
        runtime_root=runtime_root,
    )
    create_workspace("Test Workspace", workspace_id="test-workspace", set_as_default=True)
    configure_llm_router(
        LLMRouter(
            AppConfig.from_env(),
            agent_config=AgentConfig.from_env(),
        )
    )
    reset_observer(timeout=0)
    yield
    reset_llm_router_for_tests()
    reset_observer(timeout=0)


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
        def build_memory_context(self, query: str) -> str:
            del query
            return ""

    def __init__(self) -> None:
        self.manager = self._Manager()

    def for_user(self, user_id):
        del user_id
        return self.manager


class _EnabledObserver:
    enabled = True

    def __init__(self) -> None:
        self.events = []

    def emit(self, event):
        self.events.append(event)
        return True


def _deterministic_orchestrator(tmp_path, *, config=None):
    agent_config = config or AgentConfig.from_env()
    agent_config.deterministic_execution_enabled = True
    return AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=ChatStore(str(tmp_path / "det-chats")),
        execution_store=ExecutionStore(str(tmp_path / "det-executions")),
        agent_config=agent_config,
        memory_factory=_FakeMemoryFactory(),
    )


def test_orchestrator_completes_deterministic_outcome_without_agent_loop(monkeypatch, tmp_path):
    requests = []
    envelope = {
        "status": "success",
        "structured_result": {"value": "ok"},
        "artifacts": [],
        "execution": {
            "type": "deterministic",
            "authoritative": True,
            "complete": True,
            "operation_key": "query",
            "handler_execution_count": 1,
        },
    }
    def execute(_service, request, **_kwargs):
        requests.append(request)
        return DeterministicCompleted(
            envelope=envelope,
            operation_key="query",
            elapsed_ms=4,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.AgentLoopEngine",
        lambda *_args, **_kwargs: pytest.fail("Agent loop must not be constructed"),
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.AgenticOrchestrator._build_memory_context",
        lambda *_args, **_kwargs: pytest.fail(
            "Deterministic execution must not recall agent memory"
        ),
    )
    orchestrator = _deterministic_orchestrator(tmp_path)

    events = list(
        orchestrator.run(
            user_request="query",
            user_context={"arguments": {"code": "CODE-42"}},
            session_id="sess-deterministic-completed",
            requested_run_id="run-deterministic-completed",
        )
    )

    assert [event["data"]["type"] for event in events] == [
        "turn.started",
        "tool.started",
        "tool.finished",
        "turn.finished",
    ]
    assert requests[0].arguments == {"code": "CODE-42"}
    final_output = events[-1]["data"]["payload"]["final_output"]
    assert final_output == {**envelope, "text": '{\n  "value": "ok"\n}'}
    tool_finished = events[-2]["data"]["payload"]
    assert tool_finished["output_preview"] == '{"value":"ok"}'
    assert "output" not in tool_finished
    assert orchestrator.run_store.get_run("run-deterministic-completed").status == RunStatus.COMPLETED


def test_deterministic_request_does_not_require_llm_profile_resolution(monkeypatch, tmp_path):
    envelope = {
        "status": "success",
        "structured_result": {"value": "ok"},
        "artifacts": [],
        "execution": {"type": "deterministic", "operation_key": "query"},
    }
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        lambda *_args, **_kwargs: DeterministicCompleted(
            envelope=envelope,
            operation_key="query",
            elapsed_ms=1,
        ),
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.resolve_llm_profile_config",
        lambda *_args, **_kwargs: pytest.fail("LLM profile must remain unresolved"),
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: pytest.fail("LLM profile must remain unresolved"),
    )
    orchestrator = _deterministic_orchestrator(tmp_path)

    events = list(
        orchestrator.run(
            user_request="query",
            user_context={"requested_operation_key": "query"},
            arguments={"code": "A"},
            session_id="sess-deterministic-no-llm",
            requested_run_id="run-deterministic-no-llm",
        )
    )

    assert events[-1]["data"]["payload"]["status"] == "success"
    run = orchestrator.run_store.get_run("run-deterministic-no-llm")
    assert run.metadata["model_provider"] == "deterministic"


def test_deterministic_request_preserves_session_model_profile(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        lambda *_args, **_kwargs: DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "ok"},
                "artifacts": [],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        ),
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    orchestrator.chat_store.create_session(
        session_id="sess-deterministic-profile",
        metadata={
            "model_profile_id": "workspace:private",
            "model_profile_ref": {"scope": "workspace", "id": "private"},
        },
    )

    list(
        orchestrator.run(
            user_request="query",
            user_context={"requested_operation_key": "query"},
            session_id="sess-deterministic-profile",
            requested_run_id="run-deterministic-profile",
            workspace_id="test-workspace",
        )
    )

    session = orchestrator.chat_store.get_session("sess-deterministic-profile")
    assert session is not None
    assert session.metadata["model_profile_id"] == "workspace:private"
    assert session.metadata["model_profile_ref"] == {
        "scope": "workspace",
        "id": "private",
    }
    run = orchestrator.run_store.get_run("run-deterministic-profile")
    assert run.metadata["model_profile_ref"] == {
        "scope": "runtime",
        "id": "deterministic",
    }


def test_cancelled_deterministic_run_cannot_finish_late(monkeypatch, tmp_path):
    artifact_refs = []
    artifact_stored = threading.Event()

    def execute(_self, _request, **kwargs):
        kwargs["observer"]({"type": "selected", "operation_key": "query"})
        stored = kwargs["artifact_writer"]("late.txt", "late", "text/plain")
        artifact_refs.append(stored["output_ref"])
        artifact_stored.set()
        deadline = time.monotonic() + 5
        while not kwargs["cancel_event"].is_set() and time.monotonic() < deadline:
            time.sleep(0.01)
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "late"},
                "artifacts": [{"output_ref": artifact_refs[0]}],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    stream = orchestrator.run(
        user_request="query",
        user_context={"requested_operation_key": "query"},
        session_id="sess-deterministic-cancel",
        requested_run_id="run-deterministic-cancel",
    )

    assert next(stream)["data"]["type"] == "turn.started"
    assert next(stream)["data"]["type"] == "tool.started"
    assert artifact_stored.wait(5)
    assert orchestrator.cancel_run("run-deterministic-cancel", reason="user_stop") is True
    remaining = list(stream)

    assert [event["data"]["type"] for event in remaining] == [
        "tool.finished",
        "turn.finished",
    ]
    assert remaining[-1]["data"]["payload"]["status"] == "cancelled"
    assert orchestrator.run_store.get_run("run-deterministic-cancel").status == RunStatus.CANCELLED
    assert artifact_refs
    assert orchestrator.chat_store.get_tool_output(
        "sess-deterministic-cancel",
        "run-deterministic-cancel",
        artifact_refs[0],
    ) is None


def test_deterministic_cancellation_storage_failure_keeps_cancelled_terminal(
    monkeypatch,
    tmp_path,
):
    class _FailingCancellationChatStore(ChatStore):
        @staticmethod
        def _is_cancellation_batch(events):
            return any(
                (event.get("data") or {}).get("type") == "turn.finished"
                and ((event.get("data") or {}).get("payload") or {}).get("status")
                == "cancelled"
                for event in events
            )

        def append_events(self, session_id, events):
            event_list = list(events)
            if self._is_cancellation_batch(event_list):
                raise OSError("simulated cancellation append failure")
            return super().append_events(session_id, event_list)

        def append_events_once(self, session_id, events):
            event_list = list(events)
            if self._is_cancellation_batch(event_list):
                raise OSError("simulated cancellation recovery failure")
            return super().append_events_once(session_id, event_list)

    execution_started = threading.Event()

    def execute(_self, _request, **kwargs):
        kwargs["observer"]({"type": "selected", "operation_key": "query"})
        execution_started.set()
        while not kwargs["cancel_event"].is_set():
            time.sleep(0.01)
        return DeterministicFailed(
            code="execution_cancelled",
            message="cancelled",
            operation_key="query",
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    config = AgentConfig.from_env()
    config.deterministic_execution_enabled = True
    chat_store = _FailingCancellationChatStore(str(tmp_path / "cancel-failing-chats"))
    execution_store = ExecutionStore(str(tmp_path / "cancel-failing-executions"))
    orchestrator = AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=chat_store,
        execution_store=execution_store,
        agent_config=config,
        memory_factory=_FakeMemoryFactory(),
    )
    stream = orchestrator.run(
        user_request="query",
        user_context={"requested_operation_key": "query"},
        session_id="sess-deterministic-cancel-storage-failure",
        requested_run_id="run-deterministic-cancel-storage-failure",
    )

    assert next(stream)["data"]["type"] == "turn.started"
    assert next(stream)["data"]["type"] == "tool.started"
    assert execution_started.wait(2)
    assert orchestrator.cancel_run(
        "run-deterministic-cancel-storage-failure",
        reason="user_stop",
    ) is True
    remaining = list(stream)

    assert [event["data"]["type"] for event in remaining] == [
        "tool.finished",
        "turn.finished",
    ]
    assert remaining[-1]["data"]["payload"]["status"] == "cancelled"
    assert orchestrator.run_store.get_run(
        "run-deterministic-cancel-storage-failure"
    ).status == RunStatus.CANCELLED
    assert execution_store.get_turn(
        "sess-deterministic-cancel-storage-failure",
        1,
    )["status"] == "cancelled"
    assert execution_store.get_pending_deterministic_terminal(
        "sess-deterministic-cancel-storage-failure"
    ) is None


def test_cancel_wins_atomic_deterministic_completion_and_rolls_back_artifact(
    monkeypatch,
    tmp_path,
):
    artifact_refs = []

    def execute(_self, _request, **kwargs):
        kwargs["observer"]({"type": "selected", "operation_key": "query"})
        stored = kwargs["artifact_writer"]("late.txt", "late", "text/plain")
        artifact_refs.append(stored["output_ref"])
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "late"},
                "artifacts": [{"output_ref": artifact_refs[0]}],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    original_complete_run = orchestrator.run_store.complete_run_transaction

    def cancel_then_complete(run_id, result, commit):
        orchestrator.run_store.cancel_run(
            run_id,
            metadata={"cancel_reason": "completion_race"},
        )
        return original_complete_run(run_id, result, commit)

    monkeypatch.setattr(
        orchestrator.run_store,
        "complete_run_transaction",
        cancel_then_complete,
    )

    events = list(
        orchestrator.run(
            user_request="query",
            user_context={"requested_operation_key": "query"},
            session_id="sess-deterministic-completion-race",
            requested_run_id="run-deterministic-completion-race",
        )
    )

    assert [event["data"]["type"] for event in events] == [
        "turn.started",
        "tool.started",
        "tool.finished",
        "turn.finished",
    ]
    assert events[-2]["data"]["payload"]["status"] == "cancelled"
    assert events[-1]["data"]["payload"]["status"] == "cancelled"
    assert orchestrator.run_store.get_run(
        "run-deterministic-completion-race"
    ).status == RunStatus.CANCELLED
    assert artifact_refs
    assert orchestrator.chat_store.get_tool_output(
        "sess-deterministic-completion-race",
        "run-deterministic-completion-race",
        artifact_refs[0],
    ) is None


@pytest.mark.parametrize(
    "outcome,transition_name",
    [
        (
            DeterministicAwaitingInput(
                interaction={
                    "id": "interaction-race",
                    "type": "input",
                    "prompt": "Provide code",
                },
                checkpoint={
                    "source": "deterministic_execution",
                    "operation_key": "query",
                },
                operation_key="query",
            ),
            "await_run_transaction",
        ),
        (
            DeterministicFailed(
                code="handler_failed",
                message="failed safely",
                operation_key="query",
            ),
            "fail_run_transaction",
        ),
    ],
)
def test_cancel_wins_atomic_deterministic_non_success_terminal_transition(
    monkeypatch,
    tmp_path,
    outcome,
    transition_name,
):
    def execute(_self, _request, **kwargs):
        kwargs["observer"]({"type": "selected", "operation_key": "query"})
        return outcome

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    original_transition = getattr(orchestrator.run_store, transition_name)

    def cancel_then_transition(run_id, *args, **kwargs):
        orchestrator.run_store.cancel_run(
            run_id,
            metadata={"cancel_reason": "terminal_race"},
        )
        return original_transition(run_id, *args, **kwargs)

    monkeypatch.setattr(
        orchestrator.run_store,
        transition_name,
        cancel_then_transition,
    )

    events = list(
        orchestrator.run(
            user_request="query",
            user_context={"requested_operation_key": "query"},
            session_id="sess-deterministic-non-success-race-{}".format(
                transition_name
            ),
            requested_run_id="run-deterministic-non-success-race-{}".format(
                transition_name
            ),
        )
    )

    assert [event["data"]["type"] for event in events] == [
        "turn.started",
        "tool.started",
        "tool.finished",
        "turn.finished",
    ]
    assert events[-2]["data"]["payload"]["status"] == "cancelled"
    assert events[-1]["data"]["payload"]["status"] == "cancelled"
    run = orchestrator.run_store.get_run(events[-1]["data"]["run_id"])
    assert run.status == RunStatus.CANCELLED
    turn = orchestrator.execution_store.get_turn(
        events[-1]["data"]["session_id"],
        1,
    )
    assert turn["status"] == "cancelled"


def test_deterministic_artifact_rollback_retries_false_delete_result():
    class _Store:
        def __init__(self):
            self.delete_calls = []

        @staticmethod
        def store_tool_output(**kwargs):
            del kwargs
            return {"output_ref": "artifact-ref"}

        def delete_tool_output(self, **kwargs):
            self.delete_calls.append(kwargs["output_ref"])
            return len(self.delete_calls) > 1

    store = _Store()
    writer = _build_deterministic_artifact_writer(
        store,
        session_id="session-artifact-retry",
        run_id="run-artifact-retry",
    )
    assert writer is not None
    writer("result.json", "{}", "application/json")

    writer.cancel_and_rollback()
    writer.cancel_and_rollback()

    assert store.delete_calls == ["artifact-ref", "artifact-ref"]
    with pytest.raises(RuntimeError, match="transaction cancelled"):
        writer("late.json", "{}", "application/json")


def test_deterministic_artifact_batch_uses_one_journal_write(tmp_path, monkeypatch):
    session_id = "session-artifact-batch"
    run_id = "run-artifact-batch"
    chat_store = ChatStore(str(tmp_path / "artifact-batch-chats"))
    execution_store = ExecutionStore(str(tmp_path / "artifact-batch-executions"))
    chat_store.create_session(session_id=session_id)
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id=run_id,
        user_request="query",
    )
    save_calls = []
    original_save_session = execution_store._save_session

    def count_save(session):
        save_calls.append(session)
        return original_save_session(session)

    monkeypatch.setattr(execution_store, "_save_session", count_save)
    writer = _build_deterministic_artifact_writer(
        chat_store,
        session_id=session_id,
        run_id=run_id,
        execution_store=execution_store,
        turn_id=1,
    )
    assert writer is not None

    writer.prepare_artifacts(["first.txt", "second.txt", "third.txt"])

    assert save_calls == [session_id]
    transactions = execution_store.get_pending_deterministic_artifacts(session_id)
    assert len(transactions) == 1
    assert len(transactions[0]["output_refs"]) == 3

    for name in ("first.txt", "second.txt", "third.txt"):
        writer(name, name, "text/plain")

    assert save_calls == [session_id]
    writer.rollback([])
    assert execution_store.get_pending_deterministic_artifacts(session_id) == []
    assert save_calls == [session_id, session_id]


def test_deterministic_artifact_batch_rolls_back_unconsumed_reservations(tmp_path):
    class _FailSecondWriteChatStore(ChatStore):
        def __init__(self, base_dir):
            super().__init__(base_dir)
            self.write_count = 0

        def store_tool_output(self, **kwargs):
            self.write_count += 1
            if self.write_count == 2:
                raise OSError("storage unavailable")
            return super().store_tool_output(**kwargs)

    session_id = "session-artifact-batch-rollback"
    run_id = "run-artifact-batch-rollback"
    chat_store = _FailSecondWriteChatStore(
        str(tmp_path / "artifact-batch-rollback-chats")
    )
    execution_store = ExecutionStore(
        str(tmp_path / "artifact-batch-rollback-executions")
    )
    chat_store.create_session(session_id=session_id)
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id=run_id,
        user_request="query",
    )
    writer = _build_deterministic_artifact_writer(
        chat_store,
        session_id=session_id,
        run_id=run_id,
        execution_store=execution_store,
        turn_id=1,
    )
    assert writer is not None
    writer.prepare_artifacts(["first.txt", "second.txt", "third.txt"])
    first = writer("first.txt", "first", "text/plain")

    with pytest.raises(OSError, match="storage unavailable"):
        writer("second.txt", "second", "text/plain")
    writer.rollback([first["output_ref"]])

    assert execution_store.get_pending_deterministic_artifacts(session_id) == []
    assert chat_store.get_tool_output(
        session_id,
        run_id,
        first["output_ref"],
    ) is None


def test_closing_deterministic_stream_after_tool_started_cancels_worker_and_turn(
    monkeypatch,
    tmp_path,
):
    late_write_rejected = threading.Event()

    def execute(_self, _request, **kwargs):
        kwargs["observer"]({"type": "selected", "operation_key": "query"})
        while not kwargs["cancel_event"].is_set():
            time.sleep(0.01)
        try:
            kwargs["artifact_writer"]("late.txt", "late", "text/plain")
        except RuntimeError:
            late_write_rejected.set()
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "late"},
                "artifacts": [],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    terminal_syncs = []
    original_sync_event_log = orchestrator.chat_store.sync_event_log

    def sync_event_log(session_id):
        terminal_syncs.append(session_id)
        return original_sync_event_log(session_id)

    monkeypatch.setattr(orchestrator.chat_store, "sync_event_log", sync_event_log)
    stream = orchestrator.run(
        user_request="query",
        user_context={"requested_operation_key": "query"},
        session_id="sess-deterministic-close-started",
        requested_run_id="run-deterministic-close-started",
    )

    assert next(stream)["data"]["type"] == "turn.started"
    assert next(stream)["data"]["type"] == "tool.started"
    stream.close()

    assert late_write_rejected.wait(2)
    assert orchestrator.run_store.get_run(
        "run-deterministic-close-started"
    ).status == RunStatus.CANCELLED
    assert orchestrator.execution_store.get_turn(
        "sess-deterministic-close-started",
        1,
    )["status"] == "cancelled"
    artifact_root = (
        tmp_path
        / "det-chats"
        / "sess-deterministic-close-started"
        / "tool-outputs"
    )
    assert not list(artifact_root.glob("*/*"))
    persisted = [
        event
        for event in orchestrator.chat_store.get_session(
            "sess-deterministic-close-started"
        ).events
        if event.get("data", {}).get("run_id")
        == "run-deterministic-close-started"
    ]
    assert [event["data"]["type"] for event in persisted][-2:] == [
        "tool.finished",
        "turn.finished",
    ]
    assert persisted[-1]["data"]["payload"]["status"] == "cancelled"
    assert terminal_syncs == ["sess-deterministic-close-started"]


def test_closing_stream_after_turn_started_cancels_run_before_worker_start(
    monkeypatch,
    tmp_path,
):
    execute_called = False

    def execute(*_args, **_kwargs):
        nonlocal execute_called
        execute_called = True
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "unexpected"},
                "artifacts": [],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    stream = orchestrator.run(
        user_request="query",
        user_context={"requested_operation_key": "query"},
        session_id="sess-deterministic-close-turn-started",
        requested_run_id="run-deterministic-close-turn-started",
    )

    assert next(stream)["data"]["type"] == "turn.started"
    stream.close()

    assert execute_called is False
    assert orchestrator.run_store.get_run(
        "run-deterministic-close-turn-started"
    ).status == RunStatus.CANCELLED
    assert orchestrator.execution_store.get_turn(
        "sess-deterministic-close-turn-started",
        1,
    )["status"] == "cancelled"
    persisted = [
        event
        for event in orchestrator.chat_store.get_session(
            "sess-deterministic-close-turn-started"
        ).events
        if event.get("data", {}).get("run_id")
        == "run-deterministic-close-turn-started"
    ]
    assert persisted[-1]["data"]["type"] == "turn.finished"
    assert persisted[-1]["data"]["payload"]["status"] == "cancelled"


def test_closing_deterministic_stream_after_tool_finished_keeps_committed_success(
    monkeypatch,
    tmp_path,
):
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    artifact_refs = []

    def execute(_self, _request, **kwargs):
        stored = kwargs["artifact_writer"](
            "result.txt",
            "committed artifact",
            "text/plain",
        )
        artifact_refs.append(stored["output_ref"])
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "ok"},
                "artifacts": [
                    {
                        "name": "result.txt",
                        "media_type": "text/plain",
                        "size": len(b"committed artifact"),
                        "sha256": stored["sha256"],
                        "output_ref": stored["output_ref"],
                    }
                ],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    stream = orchestrator.run(
        user_request="query",
        user_context={"requested_operation_key": "query"},
        session_id="sess-deterministic-close-finished",
        requested_run_id="run-deterministic-close-finished",
    )

    assert next(stream)["data"]["type"] == "turn.started"
    assert next(stream)["data"]["type"] == "tool.started"
    assert next(stream)["data"]["type"] == "tool.finished"
    stream.close()

    assert orchestrator.run_store.get_run(
        "run-deterministic-close-finished"
    ).status == RunStatus.COMPLETED
    assert orchestrator.execution_store.get_turn(
        "sess-deterministic-close-finished",
        1,
    )["status"] == "success"
    llm_context = orchestrator.execution_store.get_llm_context(
        "sess-deterministic-close-finished"
    )
    assert [message["role"] for message in llm_context] == ["user", "assistant"]
    assert artifact_refs
    retained = orchestrator.chat_store.get_tool_output(
        "sess-deterministic-close-finished",
        "run-deterministic-close-finished",
        artifact_refs[0],
    )
    assert retained is not None
    assert retained["output"] == "committed artifact"


def test_deterministic_terminal_event_failure_rolls_back_success_state_and_artifacts(
    monkeypatch,
    tmp_path,
):
    class _FailingTerminalBatchChatStore(ChatStore):
        def append_events(self, session_id, events):
            event_list = list(events)
            if any(
                (event.get("data") or {}).get("type") == "turn.finished"
                and ((event.get("data") or {}).get("payload") or {}).get("status")
                == "success"
                for event in event_list
            ):
                return False
            return super().append_events(session_id, event_list)

    artifact_refs = []

    def execute(_self, _request, **kwargs):
        stored = kwargs["artifact_writer"]("result.txt", "payload", "text/plain")
        artifact_refs.append(stored["output_ref"])
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "ok"},
                "artifacts": [{"output_ref": artifact_refs[0]}],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    config = AgentConfig.from_env()
    config.deterministic_execution_enabled = True
    chat_store = _FailingTerminalBatchChatStore(str(tmp_path / "failing-chats"))
    execution_store = ExecutionStore(str(tmp_path / "failing-executions"))
    orchestrator = AgenticOrchestrator(
        run_store=RunStore(),
        chat_store=chat_store,
        execution_store=execution_store,
        agent_config=config,
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(
        orchestrator.run(
            user_request="query",
            user_context={"requested_operation_key": "query"},
            session_id="sess-deterministic-terminal-failure",
            requested_run_id="run-deterministic-terminal-failure",
        )
    )

    assert events[-1]["data"]["payload"]["status"] == "error"
    assert orchestrator.run_store.get_run(
        "run-deterministic-terminal-failure"
    ).status == RunStatus.FAILED
    assert execution_store.get_turn(
        "sess-deterministic-terminal-failure",
        1,
    )["status"] == "failed"
    assert execution_store.get_llm_context(
        "sess-deterministic-terminal-failure"
    ) == []
    assert execution_store.get_pending_deterministic_terminal(
        "sess-deterministic-terminal-failure"
    ) is None
    assert artifact_refs
    assert chat_store.get_tool_output(
        "sess-deterministic-terminal-failure",
        "run-deterministic-terminal-failure",
        artifact_refs[0],
    ) is None


def test_deterministic_terminal_commit_failure_keeps_durable_success_authoritative(
    monkeypatch,
    tmp_path,
):
    artifact_refs = []

    def execute(_self, _request, **kwargs):
        stored = kwargs["artifact_writer"]("result.txt", "payload", "text/plain")
        artifact_refs.append(stored["output_ref"])
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "ok"},
                "artifacts": [{"output_ref": artifact_refs[0]}],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)
    original_commit = orchestrator.execution_store.commit_deterministic_terminal

    def fail_commit(_session_id, _turn_id):
        raise OSError("simulated execution-store commit failure")

    monkeypatch.setattr(
        orchestrator.execution_store,
        "commit_deterministic_terminal",
        fail_commit,
    )
    events = list(
        orchestrator.run(
            user_request="query",
            user_context={"requested_operation_key": "query"},
            session_id="sess-deterministic-commit-failure",
            requested_run_id="run-deterministic-commit-failure",
        )
    )

    assert events[-1]["data"]["payload"]["status"] == "success"
    assert orchestrator.run_store.get_run(
        "run-deterministic-commit-failure"
    ).status == RunStatus.COMPLETED
    assert orchestrator.execution_store.get_turn(
        "sess-deterministic-commit-failure",
        1,
    )["status"] == "running"
    assert orchestrator.execution_store.get_pending_deterministic_terminal(
        "sess-deterministic-commit-failure"
    ) is not None
    assert orchestrator.execution_store.get_pending_deterministic_artifacts(
        "sess-deterministic-commit-failure"
    )
    assert orchestrator.chat_store.get_tool_output(
        "sess-deterministic-commit-failure",
        "run-deterministic-commit-failure",
        artifact_refs[0],
    ) is not None
    terminal_statuses = [
        event["data"]["payload"]["status"]
        for event in orchestrator.chat_store.get_session(
            "sess-deterministic-commit-failure"
        ).events
        if event.get("data", {}).get("type") == "turn.finished"
    ]
    assert terminal_statuses == ["success"]

    monkeypatch.setattr(
        orchestrator.execution_store,
        "commit_deterministic_terminal",
        original_commit,
    )
    assert orchestrator_module._recover_pending_deterministic_terminal(
        orchestrator.execution_store,
        orchestrator.chat_store,
        "sess-deterministic-commit-failure",
    ) is True
    assert orchestrator.execution_store.get_turn(
        "sess-deterministic-commit-failure",
        1,
    )["status"] == "success"
    assert orchestrator.execution_store.get_pending_deterministic_terminal(
        "sess-deterministic-commit-failure"
    ) is None
    assert orchestrator_module._recover_pending_deterministic_artifacts(
        orchestrator.execution_store,
        orchestrator.chat_store,
        "sess-deterministic-commit-failure",
    ) is True
    assert orchestrator.execution_store.get_pending_deterministic_artifacts(
        "sess-deterministic-commit-failure"
    ) == []
    assert orchestrator.chat_store.get_tool_output(
        "sess-deterministic-commit-failure",
        "run-deterministic-commit-failure",
        artifact_refs[0],
    ) is not None


def test_deterministic_artifact_journal_rolls_back_process_exit_before_terminal(
    tmp_path,
):
    session_id = "sess-deterministic-artifact-recovery"
    run_id = "run-deterministic-artifact-recovery"
    output_ref = "a" * 32
    execution_dir = tmp_path / "artifact-recovery-executions"
    chat_store = ChatStore(str(tmp_path / "artifact-recovery-chats"))
    chat_store.create_session(session_id=session_id)
    execution_store = ExecutionStore(str(execution_dir))
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id=run_id,
        user_request="query",
    )
    execution_store.reserve_deterministic_artifact(
        session_id,
        1,
        run_id,
        output_ref,
    )
    stored = chat_store.store_tool_output(
        session_id=session_id,
        run_id=run_id,
        tool_call_id="deterministic:result.txt",
        output="payload",
        output_ref=output_ref,
    )
    assert stored["output_ref"] == output_ref

    # A fresh ExecutionStore models process exit after ChatStore fsync but
    # before the deterministic terminal intent was prepared.
    reloaded_store = ExecutionStore(str(execution_dir))
    assert orchestrator_module._recover_pending_deterministic_artifacts(
        reloaded_store,
        chat_store,
        session_id,
    ) is True
    assert reloaded_store.get_pending_deterministic_artifacts(session_id) == []
    assert chat_store.get_tool_output(session_id, run_id, output_ref) is None


def test_deterministic_artifact_recovery_skips_an_active_run(tmp_path):
    session_id = "sess-deterministic-artifact-active"
    run_id = "run-deterministic-artifact-active"
    output_ref = "b" * 32
    execution_store = ExecutionStore(str(tmp_path / "active-executions"))
    chat_store = ChatStore(str(tmp_path / "active-chats"))
    chat_store.create_session(session_id=session_id)
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id=run_id,
        user_request="query",
    )
    execution_store.reserve_deterministic_artifact(
        session_id,
        1,
        run_id,
        output_ref,
    )
    chat_store.store_tool_output(
        session_id=session_id,
        run_id=run_id,
        tool_call_id="deterministic:result.txt",
        output="payload",
        output_ref=output_ref,
    )

    assert orchestrator_module._recover_pending_deterministic_artifacts(
        execution_store,
        chat_store,
        session_id,
        run_is_active=lambda candidate: candidate == run_id,
    ) is False
    assert execution_store.get_pending_deterministic_artifacts(session_id)
    assert chat_store.get_tool_output(session_id, run_id, output_ref) is not None


def test_deterministic_artifact_recovery_does_not_invert_run_store_lock(tmp_path):
    session_id = "sess-deterministic-artifact-lock-order"
    run_id = "run-deterministic-artifact-lock-order"
    output_ref = "c" * 32
    execution_store = ExecutionStore(str(tmp_path / "lock-order-executions"))
    chat_store = ChatStore(str(tmp_path / "lock-order-chats"))
    run_store = RunStore()
    chat_store.create_session(session_id=session_id)
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id=run_id,
        user_request="query",
    )
    execution_store.reserve_deterministic_artifact(
        session_id,
        1,
        run_id,
        output_ref,
    )
    chat_store.store_tool_output(
        session_id=session_id,
        run_id=run_id,
        tool_call_id="deterministic:result.txt",
        output="payload",
        output_ref=output_ref,
    )
    run_store.create_run(run_id, "query")
    run_store.update_status(run_id, RunStatus.RUNNING)

    completion_holds_run_lock = threading.Event()
    recovery_checks_run = threading.Event()
    errors = []

    def commit_execution_state():
        completion_holds_run_lock.set()
        if not recovery_checks_run.wait(timeout=5):
            raise AssertionError("artifact recovery did not inspect the run")
        with execution_store.deterministic_terminal_transaction(session_id):
            pass

    def complete_run():
        try:
            run_store.complete_run_transaction(
                run_id,
                {"status": "success"},
                commit_execution_state,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    def run_is_active(candidate_run_id):
        recovery_checks_run.set()
        run = run_store.get_run(candidate_run_id)
        return run is not None and run.status == RunStatus.RUNNING

    def recover_artifacts():
        try:
            orchestrator_module._recover_pending_deterministic_artifacts(
                execution_store,
                chat_store,
                session_id,
                run_is_active=run_is_active,
            )
        except Exception as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    completion_thread = threading.Thread(target=complete_run, daemon=True)
    recovery_thread = threading.Thread(target=recover_artifacts, daemon=True)
    completion_thread.start()
    assert completion_holds_run_lock.wait(timeout=5)
    recovery_thread.start()
    completion_thread.join(timeout=5)
    recovery_thread.join(timeout=5)

    assert not completion_thread.is_alive()
    assert not recovery_thread.is_alive()
    assert errors == []
    assert run_store.get_run(run_id).status == RunStatus.COMPLETED


def test_deterministic_terminal_commit_recovers_once_after_process_exit(tmp_path):
    session_id = "sess-deterministic-terminal-recovery"
    execution_dir = tmp_path / "recover-executions"
    chat_store = ChatStore(str(tmp_path / "recover-chats"))
    chat_store.create_session(session_id=session_id)
    execution_store = ExecutionStore(str(execution_dir))
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-terminal-recovery",
        user_request="query",
    )
    events = [
        build_agent_event(
            run_id="run-terminal-recovery",
            session_id=session_id,
            turn_id=1,
            event_type="tool.finished",
            source={"node": "deterministic_execution", "component": "service"},
            payload={"status": "completed"},
        ),
        build_agent_event(
            run_id="run-terminal-recovery",
            session_id=session_id,
            turn_id=1,
            event_type="turn.finished",
            source={"node": "deterministic_execution", "component": "service"},
            payload={"status": "success"},
        ),
    ]
    final_output = {"status": "success", "structured_result": {"value": "ok"}}
    llm_context = [
        {"role": "user", "content": "query"},
        {"role": "assistant", "content": "ok"},
    ]
    execution_store.prepare_deterministic_terminal(
        session_id,
        1,
        status="success",
        final_output=final_output,
        summary=None,
        llm_context=llm_context,
        events=events,
    )
    # Model a process exit after the chat batch reached disk but before the
    # execution turn/context commit.
    assert chat_store.append_events(session_id, events) is True

    reloaded_store = ExecutionStore(str(execution_dir))
    assert orchestrator_module._recover_pending_deterministic_terminal(
        reloaded_store,
        chat_store,
        session_id,
    ) is True
    assert orchestrator_module._recover_pending_deterministic_terminal(
        reloaded_store,
        chat_store,
        session_id,
    ) is False

    turn = reloaded_store.get_turn(session_id, 1)
    assert turn["status"] == "success"
    assert turn["final_output"] == final_output
    assert reloaded_store.get_llm_context(session_id) == llm_context
    assert reloaded_store.get_pending_deterministic_terminal(session_id) is None
    loaded = chat_store.get_session(session_id)
    event_ids = [event["data"]["event_id"] for event in loaded.events]
    assert event_ids == [event["data"]["event_id"] for event in events]


def test_protocol_preflight_recovers_pending_awaiting_checkpoint(monkeypatch, tmp_path):
    session_id = "sess-deterministic-preflight-recovery"
    orchestrator = _deterministic_orchestrator(tmp_path)
    orchestrator.chat_store.create_session(
        session_id=session_id,
        metadata={
            "selected_ontology_id": "demo-ontology",
            "selected_ontology_version": "1.0.0",
        },
    )
    orchestrator.execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-preflight-recovery",
        user_request="query",
    )
    checkpoint = {
        "version": 1,
        "operation_key": "query",
        "interaction": {"id": "interaction-code", "type": "input"},
        "arguments": {},
    }
    events = [
        build_agent_event(
            run_id="run-preflight-recovery",
            session_id=session_id,
            turn_id=1,
            event_type="turn.finished",
            source={"node": "deterministic_execution", "component": "service"},
            payload={"status": "awaiting_input"},
        )
    ]
    orchestrator.execution_store.prepare_deterministic_terminal(
        session_id,
        1,
        status="awaiting_input",
        final_output={"type": "user_interaction"},
        llm_context=[],
        events=events,
        deterministic_checkpoint=checkpoint,
    )
    captured = {}

    def potentially_applicable(_service, request):
        captured["checkpoint"] = request.checkpoint
        return True

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        potentially_applicable,
    )

    assert orchestrator.is_deterministic_request_potentially_applicable(
        user_request="CODE-A",
        user_context={
            "interaction_response": {
                "interaction_id": "interaction-code",
                "type": "input",
                "value": "CODE-A",
            }
        },
        arguments=None,
        session_id=session_id,
    ) is True
    assert captured["checkpoint"] == checkpoint
    assert (
        orchestrator.execution_store.get_turn(session_id, 1)["status"]
        == "awaiting_input"
    )
    assert (
        orchestrator.execution_store.get_pending_deterministic_terminal(session_id)
        is None
    )


def test_terminal_recovery_waits_for_active_terminal_transaction(tmp_path):
    class _BlockingChatStore(ChatStore):
        def __init__(self, storage_dir):
            super().__init__(storage_dir)
            self.append_started = threading.Event()
            self.allow_append = threading.Event()

        def append_events(self, session_id, events):
            self.append_started.set()
            assert self.allow_append.wait(timeout=5)
            return super().append_events(session_id, events)

    session_id = "sess-deterministic-terminal-concurrency"
    chat_store = _BlockingChatStore(str(tmp_path / "concurrent-chats"))
    execution_store = ExecutionStore(str(tmp_path / "concurrent-executions"))
    chat_store.create_session(session_id=session_id)
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-terminal-concurrency",
        user_request="query",
    )
    events = [
        build_agent_event(
            run_id="run-terminal-concurrency",
            session_id=session_id,
            turn_id=1,
            event_type="turn.finished",
            source={"node": "deterministic_execution", "component": "service"},
            payload={"status": "success"},
        )
    ]
    persist_errors = []
    recovery_results = []

    def persist():
        try:
            orchestrator_module._persist_deterministic_terminal(
                execution_store,
                chat_store,
                session_id=session_id,
                turn_id=1,
                status="success",
                final_output={"status": "success"},
                llm_context=[],
                events=events,
                error_message="terminal persistence failed",
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            persist_errors.append(exc)

    persist_thread = threading.Thread(target=persist)
    persist_thread.start()
    assert chat_store.append_started.wait(timeout=5)

    recovery_thread = threading.Thread(
        target=lambda: recovery_results.append(
            orchestrator_module._recover_pending_deterministic_terminal(
                execution_store,
                chat_store,
                session_id,
            )
        )
    )
    recovery_thread.start()
    recovery_thread.join(timeout=0.05)
    assert recovery_thread.is_alive()

    chat_store.allow_append.set()
    persist_thread.join(timeout=5)
    recovery_thread.join(timeout=5)

    assert not persist_errors
    assert recovery_results == [False]
    loaded = chat_store.get_session(session_id)
    assert [event["data"]["event_id"] for event in loaded.events] == [
        events[0]["data"]["event_id"]
    ]


def test_deterministic_terminal_append_exception_recovers_in_current_transaction(
    tmp_path,
):
    class _PartialAppendChatStore(ChatStore):
        def append_events(self, session_id, events):
            event_list = list(events)
            super().append_events(session_id, event_list[:1])
            raise OSError("simulated interrupted append")

    session_id = "sess-deterministic-partial-terminal"
    chat_store = _PartialAppendChatStore(str(tmp_path / "partial-chats"))
    chat_store.create_session(session_id=session_id)
    execution_store = ExecutionStore(str(tmp_path / "partial-executions"))
    execution_store.start_turn(
        session_id=session_id,
        turn_id=1,
        run_id="run-partial-terminal",
        user_request="query",
    )
    events = [
        build_agent_event(
            run_id="run-partial-terminal",
            session_id=session_id,
            turn_id=1,
            event_type="tool.finished",
            source={"node": "deterministic_execution", "component": "service"},
            payload={"status": "completed"},
        ),
        build_agent_event(
            run_id="run-partial-terminal",
            session_id=session_id,
            turn_id=1,
            event_type="turn.finished",
            source={"node": "deterministic_execution", "component": "service"},
            payload={"status": "success"},
        ),
    ]

    orchestrator_module._persist_deterministic_terminal(
        execution_store,
        chat_store,
        session_id=session_id,
        turn_id=1,
        status="success",
        final_output={"status": "success"},
        llm_context=[{"role": "assistant", "content": "ok"}],
        events=events,
        error_message="terminal persistence failed",
    )

    assert execution_store.get_turn(session_id, 1)["status"] == "success"
    assert execution_store.get_pending_deterministic_terminal(session_id) is None
    loaded = chat_store.get_session(session_id)
    event_ids = [event["data"]["event_id"] for event in loaded.events]
    assert event_ids == [event["data"]["event_id"] for event in events]


def test_orchestrator_persists_and_resumes_deterministic_checkpoint(monkeypatch, tmp_path):
    calls = []
    checkpoint = {
        "source": "deterministic_execution",
        "operation_key": "query",
        "arguments": {},
        "missing_fields": ["code"],
        "ontology_id": "demo",
        "ontology_version": "1.0.0",
        "package_revision": "sha256:package",
        "runtime_revision": "runner",
    }

    def execute(_self, request, **_kwargs):
        calls.append(request)
        if len(calls) == 1:
            return DeterministicAwaitingInput(
                interaction={
                    "id": "interaction-det-code",
                    "type": "input",
                    "prompt": "Provide code",
                },
                checkpoint=checkpoint,
                operation_key="query",
            )
        return DeterministicCompleted(
            envelope={
                "status": "success",
                "structured_result": {"value": "ok"},
                "artifacts": [],
                "execution": {"type": "deterministic", "operation_key": "query"},
            },
            operation_key="query",
            elapsed_ms=1,
        )

    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        execute,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    orchestrator = _deterministic_orchestrator(tmp_path)

    first_events = list(
        orchestrator.run(
            user_request="query",
            session_id="sess-deterministic-await",
            requested_run_id="run-deterministic-await-1",
        )
    )
    second_events = list(
        orchestrator.run(
            user_request="CODE-A",
            user_context={
                "interaction_response": {
                    "interaction_id": "interaction-det-code",
                    "type": "input",
                    "value": "CODE-A",
                }
            },
            session_id="sess-deterministic-await",
            requested_run_id="run-deterministic-await-2",
        )
    )

    assert first_events[-1]["data"]["payload"]["status"] == "awaiting_input"
    assert second_events[-1]["data"]["payload"]["status"] == "success"
    assert calls[1].checkpoint == checkpoint
    assert calls[1].interaction_response["value"] == "CODE-A"


def test_orchestrator_fails_closed_for_deterministic_failure(monkeypatch, tmp_path):
    internal_marker = "synthetic-sensitive-argument"
    observer = _EnabledObserver()
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: True,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        lambda *_args, **_kwargs: DeterministicFailed(
            code="output_schema_violation",
            message="invalid output",
            operation_key="query",
            handler_execution_count=1,
            internal_detail=internal_marker,
        ),
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.get_observer_for_run",
        lambda *_args, **_kwargs: observer,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.OperationDirectRouter",
        lambda *_args, **_kwargs: pytest.fail("Legacy Direct must not be constructed"),
    )
    orchestrator = _deterministic_orchestrator(tmp_path)

    events = list(
        orchestrator.run(
            user_request="query",
            session_id="sess-deterministic-failed",
            requested_run_id="run-deterministic-failed",
        )
    )

    final_output = events[-1]["data"]["payload"]["final_output"]
    assert final_output["status"] == "failed"
    assert final_output["error"]["code"] == "output_schema_violation"
    failed_run = orchestrator.run_store.get_run("run-deterministic-failed")
    assert failed_run.status == RunStatus.FAILED
    assert failed_run.result == final_output
    failed_trace = next(event for event in observer.events if event.type == "run.failed")
    assert failed_trace.payload["error_message"] == "invalid output"
    assert internal_marker not in json.dumps(failed_trace.payload, ensure_ascii=False)


def _reset_stack_profiler_state():
    stack_profiler_module._release_process_samples(stack_profiler_module._PROCESS_SAMPLE_COUNT)
    with stack_profiler_module._LOCK:
        stack_profiler_module._RUNS.clear()
    with stack_profiler_module._THREAD_RUN_COUNTS_LOCK:
        stack_profiler_module._THREAD_RUN_COUNTS.clear()
    stack_profiler_module._CONTEXT_BINDINGS.set(())


def _assert_stack_profile_released(run_id, profile):
    assert run_id not in stack_profiler_module._RUNS
    assert stack_profiler_module._THREAD_RUN_COUNTS == {}
    assert stack_profiler_module._PROCESS_SAMPLE_COUNT == 0
    if profile is not None:
        assert profile.sampler_thread is not None
        assert not profile.sampler_thread.is_alive()


def test_run_trace_resources_close_when_generator_closed_after_start(monkeypatch, tmp_path):
    _reset_stack_profiler_state()
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(
        "agent.core.orchestrator.get_observer_for_run",
        lambda run_id, workspace_id: _EnabledObserver(),
    )
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    generator = orchestrator.run(
        user_request="hello",
        session_id="sess-generator-close",
        requested_run_id="run-generator-close",
    )

    try:
        first_event = next(generator)
        assert first_event["data"]["type"] == "turn.started"
        assert "run-generator-close" not in stack_profiler_module._RUNS

        generator.close()

        _assert_stack_profile_released("run-generator-close", None)
    finally:
        generator.close()
        _reset_stack_profiler_state()


def test_run_trace_resources_close_on_pre_agent_loop_exception(monkeypatch, tmp_path):
    _reset_stack_profiler_state()
    monkeypatch.setenv("LOG_DIR", str(tmp_path / "logs"))
    monkeypatch.setattr(
        "agent.core.orchestrator.get_observer_for_run",
        lambda run_id, workspace_id: _EnabledObserver(),
    )
    config = AgentConfig.from_env()
    config.direct_execution.enabled = True

    def _raise_router(*args, **kwargs):
        del args, kwargs
        raise RuntimeError("router initialization failed")

    monkeypatch.setattr("agent.core.orchestrator.OperationDirectRouter", _raise_router)
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        agent_config=config,
        memory_factory=_FakeMemoryFactory(),
    )

    generator = orchestrator.run(
        user_request="hello",
        session_id="sess-pre-loop-error",
        requested_run_id="run-pre-loop-error",
    )

    try:
        first_event = next(generator)
        assert first_event["data"]["type"] == "turn.started"
        assert "run-pre-loop-error" not in stack_profiler_module._RUNS

        with pytest.raises(RuntimeError, match="router initialization failed"):
            next(generator)

        _assert_stack_profile_released("run-pre-loop-error", None)
    finally:
        generator.close()
        _reset_stack_profiler_state()


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

    monkeypatch.setattr("agent.core.orchestrator.ReflectionWorker", _ReflectionRecorder)
    monkeypatch.setattr("agent.core.orchestrator.init_operation_patcher", fake_init_operation_patcher)

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


def test_cancel_run_persists_before_router_signal(monkeypatch):
    run_store = RunStore()
    run_store.create_run("run-cancel-order", "hello")
    observed = []

    class _Router:
        def cancel_run(self, run_id):
            observed.append(("router", run_store.get_run(run_id).status))

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        memory_factory=_FakeMemoryFactory(),
    )
    monkeypatch.setattr("agent.core.orchestrator.get_llm_router", lambda: _Router())

    assert orchestrator.cancel_run("run-cancel-order", reason="user_stop") is True
    assert observed == [("router", RunStatus.CANCELLED)]


def test_cancel_run_rejects_awaiting_run_without_signalling_router(monkeypatch):
    run_store = RunStore()
    run_store.create_run("run-awaiting-cancel", "hello")
    run_store.await_run("run-awaiting-cancel", {"status": "awaiting_input"})
    observed = []

    class _Router:
        def cancel_run(self, run_id):
            observed.append(run_id)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        memory_factory=_FakeMemoryFactory(),
    )
    monkeypatch.setattr("agent.core.orchestrator.get_llm_router", lambda: _Router())

    assert orchestrator.cancel_run("run-awaiting-cancel", reason="user_stop") is False
    assert run_store.get_run("run-awaiting-cancel").status == RunStatus.AWAITING
    assert observed == []


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

    monkeypatch.setattr("agent.core.orchestrator.AgentLoopEngine", _FakeEngine)

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


def test_stream_agent_loop_persists_complete_tool_output_before_emitting_reference(
    tmp_path,
    monkeypatch,
):
    chat_store = ChatStore(str(tmp_path / "artifact-chats"))
    chat_store.create_session(session_id="artifact-session")
    orchestrator = AgenticOrchestrator(
        chat_store=chat_store,
        memory_factory=_FakeMemoryFactory(),
    )
    complete_output = "long-output-🙂" * 600

    class _FakeEngine:
        def __init__(self, *, memory_manager=None, agent_config=None):
            self.agent_config = agent_config
            del memory_manager

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
            del event_writer, iteration_limit, observer, event_source_node
            emit_tool_finished(
                context=ToolContext(
                    memory_manager=None,
                    messages=[],
                    run_id=state["run_id"],
                    session_id=state["session_id"],
                    turn_id=state["current_turn_id"],
                    agent_config=self.agent_config,
                    caller=caller,
                    agent_iteration=1,
                ),
                tool_name="execute_script",
                tool_call_id="call-artifact",
                status="completed",
                raw_output=complete_output,
                error=None,
                elapsed_ms=23,
            )
            snapshot_callback(state)
            return state

    monkeypatch.setattr("agent.core.orchestrator.AgentLoopEngine", _FakeEngine)

    events = list(
        orchestrator._stream_agent_loop(
            {
                "run_id": "artifact-run",
                "session_id": "artifact-session",
                "current_turn_id": 1,
                "workspace_id": "test-workspace",
                "messages": [],
            },
            memory_manager=None,
        )
    )

    tool_event = next(event[1] for event in events if event[0] == "custom")
    payload = tool_event["data"]["payload"]
    artifact = chat_store.get_tool_output(
        "artifact-session",
        "artifact-run",
        payload["output_ref"],
    )
    assert payload["output_size"] == len(complete_output.encode("utf-8"))
    assert payload["output_truncated"] is True
    assert artifact is not None
    assert artifact["output"] == complete_output


def test_direct_execution_persists_complete_tool_output_before_emitting_reference(
    tmp_path,
    monkeypatch,
):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "direct-artifact-chats"))
    execution_store = ExecutionStore(str(tmp_path / "direct-artifact-executions"))
    config = AgentConfig.from_env()
    config.direct_execution.enabled = True
    complete_output = "direct-output-🙂" * 600

    class _FakeRouter:
        def __init__(self, *, config):
            del config

        def route(self, **kwargs):
            del kwargs
            return SimpleNamespace(
                route="direct_script",
                reason="unit test",
                confidence=1.0,
                ontology_id="demo-ontology",
                operation_key="demo-operation",
                missing_business_arguments=[],
                runtime_requirements=[],
            )

    def _run_direct_script(*, decision, runtime_context, project_root, timeout_seconds, observer):
        del decision, project_root, timeout_seconds, observer
        payload = build_tool_finished_payload(
            tool_call_id="direct",
            tool_name="execute_script",
            status="completed",
            raw_output=complete_output,
            error=None,
            elapsed_ms=17,
        )
        event = build_agent_event(
            run_id=runtime_context.run_id,
            session_id=runtime_context.session_id,
            turn_id=runtime_context.turn_id,
            event_type="tool.finished",
            source={"node": "direct_execution", "component": "service"},
            payload=payload,
        )
        result = SimpleNamespace(
            status="success",
            output=complete_output,
            elapsed_ms=17,
            error=None,
        )
        return [event], result

    monkeypatch.setattr("agent.core.orchestrator.OperationDirectRouter", _FakeRouter)
    monkeypatch.setattr("agent.core.orchestrator.run_direct_script", _run_direct_script)
    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        agent_config=config,
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(
        orchestrator.run(
            user_request="run direct operation",
            session_id="direct-artifact-session",
            requested_run_id="direct-artifact-run",
        )
    )

    tool_event = next(event for event in events if event["data"]["type"] == "tool.finished")
    payload = tool_event["data"]["payload"]
    artifact = chat_store.get_tool_output(
        "direct-artifact-session",
        "direct-artifact-run",
        payload["output_ref"],
    )
    assert payload["output_size"] == len(complete_output.encode("utf-8"))
    assert payload["output_truncated"] is True
    assert "output_full" not in payload
    assert artifact is not None
    assert artifact["output"] == complete_output


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

    monkeypatch.setattr("agent.core.orchestrator.workspace_scope", fake_workspace_scope)
    monkeypatch.setattr("agent.core.orchestrator.AgentLoopEngine", _FakeEngine)

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

    monkeypatch.setattr("agent.core.orchestrator.AgentLoopEngine", _FakeEngine)

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

    monkeypatch.setattr("agent.core.orchestrator.AgentLoopEngine", _FailingEngine)

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


def test_terminal_tool_error_fails_run_and_turn(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

    monkeypatch.setattr(
        "agent.tools.business_data_search.handle_business_data_search",
        lambda **_kwargs: (
            '{"success":false,"engine":"graph_search_runtime",'
            '"error_code":"GRAPH_BACKEND_TIMEOUT","retryable":false}'
        ),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.get_ontology_manifest",
        lambda ontology_id, *, workspace_id=None: (
            SimpleNamespace(ontology_dir=str(tmp_path)),
            None,
        ),
    )
    monkeypatch.setattr(
        "agent.tools.business_data_search.resolve_object_reference_result",
        lambda *_args, **_kwargs: ObjectReferenceLookupResult(
            status=ObjectReferenceStatus.RESOLVED,
            resolution=SimpleNamespace(object_key="refinery_unit"),
        ),
    )

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del memory_manager, observer
        state = dict(initial_state)
        terminal_error_signal = _JiuwenTerminalErrorSignal()
        adapter = CurrentRuntimeToolAdapter(
            tool_name="business_data_search",
            state=state,
            context_factory=lambda: ToolContext(
                memory_manager=None,
                messages=list(state.get("messages") or []),
                run_id=state["run_id"],
                session_id=state["session_id"],
                turn_id=state["current_turn_id"],
                user_id=state.get("user_id"),
                agent_config=self.agent_config,
                caller="main",
                agent_iteration=1,
                workspace_id=state.get("workspace_id"),
                selected_ontology_id="demo-ontology",
            ),
            tool_batch_executor=execute_tool_batch,
            terminal_error_callback=terminal_error_signal.record,
        )
        asyncio.run(
            adapter.invoke(
                {
                    "ontology_id": "demo-ontology",
                    "entity": "refinery_unit",
                    "query": "list refinery units",
                }
            )
        )
        terminal_error_signal.raise_if_set()
        yield ("values", state)

    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)
    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    events = list(
        orchestrator.run(
            user_request="list refinery units",
            session_id="sess-terminal-tool-error",
            requested_run_id="run-terminal-tool-error",
        )
    )

    run = run_store.get_run("run-terminal-tool-error")
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error == "Graph query timed out."
    finished = events[-1]["data"]
    assert finished["type"] == "turn.finished"
    assert finished["payload"] == {
        "status": "error",
        "error": "Graph query timed out.",
    }


def test_orchestrator_rechecks_negative_preflight_after_version_resolution(
    monkeypatch,
    tmp_path,
):
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
    preflight_calls = []
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.is_potentially_applicable",
        lambda *_args, **_kwargs: preflight_calls.append(True) or False,
    )
    monkeypatch.setattr(
        "agent.core.orchestrator.DeterministicExecutionService.execute",
        lambda *_args, **_kwargs: pytest.fail(
            "negative deterministic preflight must skip execution"
        ),
    )

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
            deterministic_preflight_result=False,
        )
    )

    turn_start = next(
        event for event in events
        if event.get("event") == "agent.v2" and event.get("data", {}).get("type") == "turn.started"
    )
    assert turn_start["data"]["run_id"] == "client-run-123"

    run = run_store.get_run("client-run-123")
    assert run is not None
    assert preflight_calls == [True]


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
    from integration.observability import RuntimeEventType

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
    monkeypatch.setattr(
        "agent.core.orchestrator.get_observer_for_run",
        lambda run_id, workspace_id: RecordingObserver(),
    )

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
    from integration.observability import RuntimeEventType

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
    monkeypatch.setattr(
        "agent.core.orchestrator.get_observer_for_run",
        lambda run_id, workspace_id: RecordingObserver(),
    )

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
    from agent.sub_agents import manager as manager_module

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
    monkeypatch.setattr(
        "agent.core.orchestrator.get_observer_for_run",
        lambda run_id, workspace_id: RecordingObserver(),
    )

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
    import config.system_config as system_config_module
    from system_config_migration_helpers import apply_test_system_config_migrations

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
    apply_test_system_config_migrations(
        config_path,
        monkeypatch,
        runtime_root=tmp_path / ".onto-runtime",
    )
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
        assert selected_config.profile_id == "global:accurate"
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
    assert run.metadata["model_profile_id"] == "global:accurate"
    assert run.metadata["model_provider"] == "deepseek"


def test_orchestrator_uses_workspace_private_model_profile(monkeypatch, tmp_path):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    update_workspace_llm_profiles(
        "alpha",
        {
            "profiles": [
                {
                    "id": "private",
                    "name": "Private Model",
                    "provider": "openai_compatible",
                    "base_url": "https://private.example/v1",
                    "model": "private-model",
                    "api_key": "private-secret",
                    "enabled": True,
                }
            ]
        },
    )

    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        selected_config = get_llm_router().get_config(
            LLMPurpose.MAIN,
            profile_id=initial_state["user_context"]["model_profile_id"],
            workspace_id=initial_state["workspace_id"],
        )
        assert selected_config.profile_id == "workspace:private"
        assert selected_config.model == "private-model"
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
            session_id="sess-private-model",
            user_context={"model_profile_id": "workspace:private"},
            requested_run_id="run-private-model",
            workspace_id="alpha",
        )
    )

    run = run_store.get_run("run-private-model")
    assert run.metadata["model_profile_id"] == "workspace:private"
    assert run.metadata["model_profile_ref"] == {"scope": "workspace", "id": "private"}


def test_orchestrator_locks_selected_ontology_for_session(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    observed_contexts = []

    class _Registry:
        blocked = False

        def get_ontology(self, ontology_id):
            if self.blocked:
                raise AssertionError("locked ontology should not be reloaded from registry")
            if ontology_id in {"quality-analysis", "other-ontology"}:
                return SimpleNamespace(
                    id=ontology_id,
                    name="Quality Analysis" if ontology_id == "quality-analysis" else "Other Ontology",
                    enabled=True,
                    active_version="1.0.0" if ontology_id == "quality-analysis" else "2.0.0",
                    version="1.0.0" if ontology_id == "quality-analysis" else "2.0.0",
                )
            return None

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        observed_contexts.append(dict(initial_state.get("user_context") or {}))
        state = dict(initial_state)
        state["messages"] = list(state.get("messages") or [])
        state["messages"].append({"role": "assistant", "content": "done"})
        state["final_output"] = "done"
        yield ("values", state)

    registry = _Registry()
    monkeypatch.setattr("agent.core.orchestrator.get_ontology_registry", lambda: registry)
    monkeypatch.setattr(
        "agent.core.orchestrator.get_runtime_ontology_manifest",
        lambda ontology_id, *, version=None, workspace_id=None: (
            SimpleNamespace(
                id=ontology_id,
                name="Quality Analysis" if ontology_id == "quality-analysis" else "Other Ontology",
                enabled=True,
                version=str(version or ("1.0.0" if ontology_id == "quality-analysis" else "2.0.0")),
                active_version="2.0.0" if ontology_id == "quality-analysis" else "2.0.0",
            )
            if ontology_id in {"quality-analysis", "other-ontology"}
            else None
        ),
    )
    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="first",
            session_id="sess-ontology-lock",
            user_context={"selected_ontology_id": "quality-analysis"},
        )
    )

    session = chat_store.get_session("sess-ontology-lock")
    assert session.metadata["selected_ontology_id"] == "quality-analysis"
    assert session.metadata["selected_ontology_name"] == "Quality Analysis"
    assert session.metadata["selected_ontology_version"] == "1.0.0"
    assert session.messages[0].metadata["selected_ontology_id"] == "quality-analysis"
    assert session.messages[0].metadata["selected_ontology_name"] == "Quality Analysis"
    assert session.messages[0].metadata["selected_ontology_version"] == "1.0.0"

    registry.blocked = True
    list(
        orchestrator.run(
            user_request="second",
            session_id="sess-ontology-lock",
            user_context={},
        )
    )
    assert observed_contexts[-1]["selected_ontology_id"] == "quality-analysis"
    assert observed_contexts[-1]["selected_ontology_version"] == "1.0.0"

    with pytest.raises(ValueError, match="selected_ontology_id is locked"):
        list(
            orchestrator.run(
                user_request="third",
                session_id="sess-ontology-lock",
                user_context={"selected_ontology_id": "other-ontology"},
            )
        )


def test_orchestrator_honors_and_locks_explicit_selected_ontology_version(
    monkeypatch,
    tmp_path,
):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    observed_contexts = []
    requested_versions = []

    class _Registry:
        @staticmethod
        def get_ontology(ontology_id):
            if ontology_id != "quality-analysis":
                return None
            return SimpleNamespace(
                id=ontology_id,
                name="Quality Analysis",
                enabled=True,
                active_version="2.0.0",
                version="2.0.0",
            )

    def fake_runtime_manifest(ontology_id, *, version=None, workspace_id=None):
        del workspace_id
        requested_versions.append(version)
        if ontology_id != "quality-analysis" or version not in {"1.0.0", "2.0.0"}:
            return None
        return SimpleNamespace(
            id=ontology_id,
            name="Quality Analysis",
            enabled=True,
            version=version,
            active_version="2.0.0",
        )

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        observed_contexts.append(dict(initial_state.get("user_context") or {}))
        state = dict(initial_state)
        state["messages"] = list(state.get("messages") or [])
        state["messages"].append({"role": "assistant", "content": "done"})
        state["final_output"] = "done"
        yield ("values", state)

    monkeypatch.setattr("agent.core.orchestrator.get_ontology_registry", lambda: _Registry())
    monkeypatch.setattr(
        "agent.core.orchestrator.get_runtime_ontology_manifest",
        fake_runtime_manifest,
    )
    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="use the historical package",
            session_id="sess-explicit-version",
            user_context={
                "selected_ontology_id": "quality-analysis",
                "selected_ontology_version": "1.0.0",
            },
        )
    )

    assert requested_versions[0] == "1.0.0"
    assert observed_contexts[-1]["selected_ontology_version"] == "1.0.0"
    session = chat_store.get_session("sess-explicit-version")
    assert session.metadata["selected_ontology_version"] == "1.0.0"

    with pytest.raises(ValueError, match="selected_ontology_version is locked"):
        list(
            orchestrator.run(
                user_request="switch version",
                session_id="sess-explicit-version",
                user_context={
                    "selected_ontology_id": "quality-analysis",
                    "selected_ontology_version": "2.0.0",
                },
            )
        )


def test_orchestrator_uses_session_locked_version_when_active_changes_before_first_turn(
    monkeypatch,
    tmp_path,
):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    observed_contexts = []

    active_version = {"value": "2.0.0"}

    def fake_runtime_manifest(ontology_id, *, version=None, workspace_id=None):
        del workspace_id
        if ontology_id != "quality-analysis":
            return None
        resolved_version = str(version or active_version["value"])
        return SimpleNamespace(
            id="quality-analysis",
            name="Quality Analysis",
            enabled=True,
            version=resolved_version,
            active_version=active_version["value"],
        )

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        observed_contexts.append(dict(initial_state.get("user_context") or {}))
        state = dict(initial_state)
        state["messages"] = list(state.get("messages") or [])
        state["messages"].append({"role": "assistant", "content": "done"})
        state["final_output"] = "done"
        yield ("values", state)

    monkeypatch.setattr("agent.core.orchestrator.get_runtime_ontology_manifest", fake_runtime_manifest)
    monkeypatch.setattr(
        "agent.core.orchestrator.get_ontology_registry",
        lambda: (_ for _ in ()).throw(AssertionError("pre-created locked session must not re-read registry")),
    )
    monkeypatch.setattr(AgenticOrchestrator, "_stream_agent_loop", _stream_agent_loop)

    chat_store.create_session(
        session_id="sess-created-before-turn",
        metadata={
            "selected_ontology_id": "quality-analysis",
            "selected_ontology_name": "Quality Analysis",
            "selected_ontology_version": "1.0.0",
        },
    )

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="first turn after active switch",
            session_id="sess-created-before-turn",
            user_context={},
        )
    )

    assert observed_contexts[-1]["selected_ontology_id"] == "quality-analysis"
    assert observed_contexts[-1]["selected_ontology_version"] == "1.0.0"
    session = chat_store.get_session("sess-created-before-turn")
    assert session.metadata["selected_ontology_version"] == "1.0.0"
    assert execution_store.get_session_history("sess-created-before-turn")["turns"][0]["ontology_version"] == "1.0.0"


def test_orchestrator_rejects_selected_ontology_after_unselected_first_turn(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))

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

    orchestrator = AgenticOrchestrator(
        run_store=run_store,
        chat_store=chat_store,
        execution_store=execution_store,
        memory_factory=_FakeMemoryFactory(),
    )

    list(
        orchestrator.run(
            user_request="first",
            session_id="sess-no-ontology-lock",
            user_context={},
        )
    )

    with pytest.raises(ValueError, match="selected_ontology_id can only be set before the first user turn"):
        list(
            orchestrator.run(
                user_request="second",
                session_id="sess-no-ontology-lock",
                user_context={"selected_ontology_id": "quality-analysis"},
            )
        )


def test_orchestrator_locks_agent_mode_for_session(monkeypatch, tmp_path):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    observed_contexts = []

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        observed_contexts.append(dict(initial_state.get("user_context") or {}))
        state = dict(initial_state)
        state["messages"] = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": state.get("user_request", "")},
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
            user_request="first",
            session_id="sess-agent-mode-lock",
            requested_run_id="run-agent-mode-lock-first",
            user_context={"agent_mode": "multi_agent"},
        )
    )

    session = chat_store.get_session("sess-agent-mode-lock")
    assert session.metadata["agent_mode"] == "multi_agent"
    assert run_store.get_run("run-agent-mode-lock-first").metadata["agent_mode"] == "multi_agent"

    list(
        orchestrator.run(
            user_request="second",
            session_id="sess-agent-mode-lock",
            user_context={},
        )
    )
    assert observed_contexts[-1]["agent_mode"] == "multi_agent"

    with pytest.raises(ValueError, match="agent_mode is locked"):
        list(
            orchestrator.run(
                user_request="third",
                session_id="sess-agent-mode-lock",
                user_context={"agent_mode": "quick"},
            )
        )


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
    interaction = {
        "id": "interaction_call_user",
        "type": "single_choice",
        "prompt": "请选择处理方式",
        "options": [{"id": "a", "label": "方案A", "value": "A"}],
    }
    execution_store.start_turn(
        session_id="sess-resume",
        turn_id=1,
        run_id="run-await",
        user_request="请选择",
    )
    execution_store.await_turn(
        session_id="sess-resume",
        turn_id=1,
        final_output={"type": "user_interaction", "interaction": interaction},
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
    history = execution_store.get_session_history("sess-resume")
    assert history["turns"][0]["interaction_consumed_at"]


def test_orchestrator_merges_cancelled_interaction_into_next_user_message_once(
    monkeypatch,
    tmp_path,
):
    run_store = RunStore()
    chat_store = ChatStore(str(tmp_path / "chats"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    execution_store.set_llm_context(
        "sess-cancel-resume",
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "原始请求"},
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
    interaction = {
        "id": "interaction_call_user",
        "type": "input",
        "prompt": "请补充信息",
    }
    execution_store.start_turn(
        session_id="sess-cancel-resume",
        turn_id=1,
        run_id="run-await",
        user_request="请补充",
    )
    execution_store.await_turn(
        session_id="sess-cancel-resume",
        turn_id=1,
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    cancellation = execution_store.cancel_pending_interaction(
        "sess-cancel-resume",
        interaction["id"],
    )
    captured_messages = []

    def _stream_agent_loop(self, initial_state, *, memory_manager, observer=None):
        del self, memory_manager, observer
        captured_messages.append(list(initial_state["messages"]))
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
            user_request="继续分析其他问题",
            session_id="sess-cancel-resume",
        )
    )

    resumed_messages = captured_messages[0]
    assert resumed_messages[-2]["role"] == "tool"
    assert resumed_messages[-1]["role"] == "user"
    assert all(
        not (
            current.get("role") == "user"
            and following.get("role") == "user"
        )
        for current, following in zip(resumed_messages, resumed_messages[1:])
    )
    assert not any(
        message.get("role") == "user"
        and message.get("content") == INTERACTION_CANCELLED_MESSAGE
        for message in resumed_messages
    )
    resumed_content = resumed_messages[-1]["content"]
    assert resumed_content.endswith("继续分析其他问题")
    assert "<user-interaction-cancellation>" in resumed_content
    assert INTERACTION_CANCELLED_MESSAGE in resumed_content
    assert '"cancelled_by": "user"' in resumed_content
    assert cancellation["interaction_id"] in resumed_content

    history = execution_store.get_session_history("sess-cancel-resume")
    assert history["turns"][0]["interaction_context_consumed_by_turn_id"] == 2
    assert history["turns"][1]["prior_interaction_cancellation"] == cancellation

    list(
        orchestrator.run(
            user_request="再次提问",
            session_id="sess-cancel-resume",
        )
    )
    next_content = captured_messages[1][-1]["content"]
    assert next_content == "再次提问"
    assert "<user-interaction-cancellation>" not in next_content


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
    monkeypatch.setattr("agent.core.orchestrator.get_proposal_buffer", lambda: buffer)

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
