"""Real-stack regression gates for the synchronous compatibility Run API."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import dataclass
import importlib.util
from pathlib import Path
import sys
from typing import Any

import httpx
from fastapi import FastAPI
import pytest

from agent.runtime.manager import AsyncRunManager
from agent.runtime.mock import FixedAsyncAgentRuntime
from agent.runtime.runtime import RuntimeState
from agent.runtime.service import AsyncExecutionService, OrchestratorSessionAdapter
from api.auth.auth import AuthUser
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore


# Load only the focused router. Importing ``api.routes`` would also import
# unrelated route modules and make this regression depend on their state.
_WORKFLOWS_PATH = Path(__file__).parents[1] / "api" / "routes" / "workflows.py"
_WORKFLOWS_SPEC = importlib.util.spec_from_file_location(
    "sync_run_real_stack_workflows_under_test",
    _WORKFLOWS_PATH,
)
assert _WORKFLOWS_SPEC is not None and _WORKFLOWS_SPEC.loader is not None
workflows = importlib.util.module_from_spec(_WORKFLOWS_SPEC)
sys.modules[_WORKFLOWS_SPEC.name] = workflows
_WORKFLOWS_SPEC.loader.exec_module(workflows)


_USER = AuthUser(username="operator_a", role="user")
_WORKSPACE_ID = "workspace_demo"


class _InlineOffloadPool:
    def __init__(self) -> None:
        self.block_create_turn = False
        self.create_started = asyncio.Event()
        self.create_release = asyncio.Event()
        self.create_finished = asyncio.Event()
        self.block_mark_turn_start_failed = False
        self.mark_started = asyncio.Event()
        self.mark_release = asyncio.Event()
        self.mark_finished = asyncio.Event()

    async def run(self, function, *args, **kwargs):
        function_name = getattr(function, "__name__", "")
        if function_name == "create_turn_for_run" and self.block_create_turn:
            self.create_started.set()
            await self.create_release.wait()
            try:
                return function(*args, **kwargs)
            finally:
                self.create_finished.set()
        if function_name == "mark_turn_task_start_failed":
            self.mark_started.set()
            if self.block_mark_turn_start_failed:
                await self.mark_release.wait()
            try:
                return function(*args, **kwargs)
            finally:
                self.mark_finished.set()
        return function(*args, **kwargs)


class _RecordingFixedRuntime(FixedAsyncAgentRuntime):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()
        self.cancel_reasons: list[str] = []

    async def astream(self, context):
        async for event in super().astream(context):
            self.started.set()
            yield event

    async def cancel(self, run_id: str, reason: str) -> bool:
        self.cancel_reasons.append(reason)
        return await super().cancel(run_id, reason)


class _ImmediateAwaitingInputRuntime(_RecordingFixedRuntime):
    async def _astream_impl(self, context):
        interaction = {
            "id": "interaction_sync_unsupported",
            "type": "input",
            "prompt": "Provide synthetic follow-up input",
            "options": [],
        }
        yield self._event(
            context,
            "interaction.requested",
            "awaiting_input",
            {
                "interaction_id": interaction["id"],
                "type": interaction["type"],
                "prompt": interaction["prompt"],
                "options": [],
            },
            "interaction",
        )
        yield self._event(
            context,
            "turn.finished",
            "awaiting_input",
            {
                "status": "awaiting_input",
                "content": interaction["prompt"],
                "structured_content": {
                    "type": "user_interaction",
                    "text": interaction["prompt"],
                    "interaction": interaction,
                    "options": [],
                },
                "history_messages": [
                    {
                        "role": "user",
                        "content": "Analyze synthetic interactive input",
                    },
                    {
                        "role": "assistant",
                        "content": interaction["prompt"],
                    },
                ],
            },
            "agent",
        )


class _RecordingRunManager(AsyncRunManager):
    def __init__(self, execution_service: AsyncExecutionService) -> None:
        super().__init__(execution_service)
        self.cancel_reasons: list[str] = []
        self.block_cancel = False
        self.cancel_started = asyncio.Event()
        self.cancel_release = asyncio.Event()
        self.cancel_finished = asyncio.Event()
        self.block_create_before_registration = False
        self.create_run_started = asyncio.Event()
        self.create_run_release = asyncio.Event()

    async def create_run(self, *args, **kwargs):
        if self.block_create_before_registration:
            self.create_run_started.set()
            await self.create_run_release.wait()
        return await super().create_run(*args, **kwargs)

    async def cancel_run(self, run_id, scope, reason="user_requested"):
        self.cancel_reasons.append(reason)
        if self.block_cancel:
            self.cancel_started.set()
            await self.cancel_release.wait()
        try:
            return await super().cancel_run(run_id, scope, reason=reason)
        finally:
            self.cancel_finished.set()


class _SyntheticOrchestrator:
    def __init__(self, tmp_path: Path) -> None:
        self.chat_store = ChatStore(str(tmp_path / "synthetic-chat"))
        self.execution_store = ExecutionStore(
            str(tmp_path / "synthetic-executions")
        )

    def _get_stores(self, username: str, workspace_id: str):
        assert username == _USER.username
        assert workspace_id == _WORKSPACE_ID
        return self.chat_store, self.execution_store


@dataclass
class _Harness:
    manager: _RecordingRunManager
    runtime: _RecordingFixedRuntime
    orchestrator: _SyntheticOrchestrator
    execution_store: ExecutionStore
    short_io: _InlineOffloadPool


def _install_harness(
    monkeypatch,
    tmp_path,
    *,
    synthetic_runtime: _RecordingFixedRuntime | None = None,
) -> tuple[FastAPI, _Harness]:
    orchestrator = _SyntheticOrchestrator(tmp_path)
    selected_runtime = synthetic_runtime or _RecordingFixedRuntime()
    service = AsyncExecutionService(
        selected_runtime,
        session_adapter=OrchestratorSessionAdapter(lambda: orchestrator),
    )
    manager = _RecordingRunManager(service)
    short_io = _InlineOffloadPool()
    runtime_container = type(
        "SyntheticRuntimeContainer",
        (),
        {
            "manager": manager,
            "event_hub": manager.event_bus,
            "short_io": short_io,
            "state": RuntimeState.ACCEPTING,
        },
    )()
    harness = _Harness(
        manager=manager,
        runtime=selected_runtime,
        orchestrator=orchestrator,
        execution_store=orchestrator.execution_store,
        short_io=short_io,
    )

    monkeypatch.setattr(workflows, "_get_runtime", lambda: runtime_container)
    monkeypatch.setattr(
        workflows,
        "_get_orchestrator",
        lambda: orchestrator,
    )
    monkeypatch.setattr(
        workflows,
        "_get_stores",
        lambda: (orchestrator.chat_store, orchestrator.execution_store),
    )
    monkeypatch.setattr(
        workflows,
        "resolve_effective_llm_profile_config",
        lambda *_args, **_kwargs: {"id": "profile_demo"},
    )

    app = FastAPI()
    app.include_router(workflows.workflows_router)

    async def authenticated_user_override() -> AuthUser:
        return _USER

    async def workspace_override() -> str:
        return _WORKSPACE_ID

    app.dependency_overrides[
        workflows.require_authenticated_user
    ] = authenticated_user_override
    app.dependency_overrides[
        workflows.require_workspace_id
    ] = workspace_override
    return app, harness


async def _client_for(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    )


def _assert_no_sync_subscriber_leak(harness: _Harness) -> None:
    assert harness.manager.event_bus.active_subscriber_count == 0
    assert harness.manager.event_bus.reserved_subscriber_count == 0


def test_sync_run_real_stack_returns_success_and_public_trace(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        try:
            async with await _client_for(app) as client:
                response = await client.post(
                    "/agentic/runs/sync",
                    json={
                        "request": "Analyze synthetic input",
                        "session_id": "session_sync_real_success",
                        "context": {"llm_request_mode": "sync"},
                        "arguments": {"mock_scenario": "success"},
                        "include_trace": True,
                    },
                )

            assert response.status_code == 200, response.text
            body: dict[str, Any] = response.json()
            assert body == {
                "status": "success",
                "final_answer": "mock answer",
                "session_id": "session_sync_real_success",
                "run_id": body["run_id"],
                "execution_trace": {"tool_calls": ["mock_tool"]},
            }

            snapshot = await harness.manager.repository.get(body["run_id"])
            assert snapshot is not None
            assert snapshot.status.value == "completed"
            assert snapshot.context.llm_request_mode == "sync"
            assert snapshot.context.supports_user_interaction is False

            events = await harness.manager.repository.list_after(
                snapshot.run_id,
                0,
            )
            assert [event.event_type for event in events] == [
                "run.started",
                "turn.started",
                "model.started",
                "tool.started",
                "tool.finished",
                "token.delta",
                "turn.finished",
            ]
            assert [event.seq for event in events] == list(
                range(1, len(events) + 1)
            )

            history = harness.execution_store.get_session_history(
                "session_sync_real_success"
            )
            assert history["turns"][0]["protocol_metadata"] == {
                "protocol": "agentic_sync"
            }
            durable_turn = harness.execution_store.get_turn(
                "session_sync_real_success",
                snapshot.turn_id,
                run_id=body["run_id"],
            )
            assert durable_turn is not None
            assert durable_turn["status"] == "success"
            assert durable_turn["final_output"] == {"text": "mock answer"}
            _assert_no_sync_subscriber_leak(harness)
        finally:
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_subscriber_capacity_retry_does_not_duplicate_history(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        harness.manager.event_bus.max_subscribers_total = 1
        holder = await harness.manager.event_bus.reserve_subscriber(
            "agentic-capacity-holder"
        )
        try:
            async with await _client_for(app) as client:
                limited = await client.post(
                    "/agentic/runs/sync",
                    json={
                        "request": "Analyze synthetic retry input",
                        "session_id": "session_sync_capacity_retry",
                    },
                )
                assert limited.status_code == 429, limited.text
                assert "run_id" not in limited.json()
                history = harness.execution_store.get_session_history(
                    "session_sync_capacity_retry"
                )
                assert history is None

                await harness.manager.event_bus.release_subscriber_reservation(
                    holder
                )
                holder = ""
                retried = await client.post(
                    "/agentic/runs/sync",
                    json={
                        "request": "Analyze synthetic retry input",
                        "session_id": "session_sync_capacity_retry",
                    },
                )

            assert retried.status_code == 200, retried.text
            assert retried.json()["status"] == "success"
            history = harness.execution_store.get_session_history(
                "session_sync_capacity_retry"
            )
            assert len(history["turns"]) == 1
            assert [
                message["content"]
                for message in harness.execution_store.get_llm_context(
                    "session_sync_capacity_retry"
                )
                if message["role"] == "user"
            ] == ["Analyze synthetic retry input"]
            _assert_no_sync_subscriber_leak(harness)
        finally:
            if holder:
                await harness.manager.event_bus.release_subscriber_reservation(
                    holder
                )
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_real_stack_has_no_total_timeout_and_request_cancel_cleans_up(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        request_task = None
        try:
            assert not hasattr(workflows, "_SYNC_RUN_TIMEOUT_SECONDS")
            async with await _client_for(app) as client:
                request_task = asyncio.create_task(
                    client.post(
                        "/agentic/runs/sync",
                        json={
                            "request": "Analyze synthetic blocking input",
                            "session_id": "session_sync_no_total_timeout",
                            "arguments": {"mock_scenario": "awaiting_input"},
                        },
                    )
                )
                await asyncio.wait_for(harness.runtime.started.wait(), timeout=1.0)
                await asyncio.sleep(0.03)
                assert request_task.done() is False
                request_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request_task

            snapshots = await harness.manager.repository.list_snapshots()
            assert len(snapshots) == 1
            snapshot = snapshots[0]
            assert snapshot.status.value == "cancelled"
            assert snapshot.cancel_reason == "sync_request_cancelled"
            assert harness.runtime.cancel_reasons == [
                "sync_request_cancelled"
            ]
            assert harness.manager.cancel_reasons == [
                "sync_request_cancelled"
            ]

            durable_turn = harness.execution_store.get_turn(
                "session_sync_no_total_timeout",
                snapshot.turn_id,
                run_id=snapshot.run_id,
            )
            assert durable_turn is not None
            assert durable_turn["status"] == "cancelled"

            events = await harness.manager.repository.list_after(
                snapshot.run_id,
                0,
            )
            assert [event.event_type for event in events] == [
                "run.started",
                "turn.started",
                "turn.finished",
            ]
            _assert_no_sync_subscriber_leak(harness)
        finally:
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_real_stack_persists_terminal_failure(monkeypatch, tmp_path):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        try:
            async with await _client_for(app) as client:
                response = await client.post(
                    "/agentic/runs/sync",
                    json={
                        "request": "Analyze synthetic failing input",
                        "session_id": "session_sync_real_failure",
                        "arguments": {"mock_scenario": "tool_failure"},
                        "include_trace": True,
                    },
                )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "error"
            assert body["error"] == "Mock tool failed"
            assert body["final_answer"] == ""
            assert body["execution_trace"] == {
                "tool_calls": ["mock_tool"],
                "tool_failures": [
                    {
                        "tool_name": "mock_tool",
                        "error": "Mock tool failed",
                    }
                ],
            }

            snapshot = await harness.manager.repository.get(body["run_id"])
            assert snapshot is not None
            assert snapshot.status.value == "failed"
            durable_turn = harness.execution_store.get_turn(
                "session_sync_real_failure",
                snapshot.turn_id,
                run_id=body["run_id"],
            )
            assert durable_turn is not None
            assert durable_turn["status"] == "failed"
            assert durable_turn["final_output"] == {
                "error": "Mock tool failed"
            }
            _assert_no_sync_subscriber_leak(harness)
        finally:
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_rejects_immediate_interaction_without_pending_checkpoint(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(
            monkeypatch,
            tmp_path,
            synthetic_runtime=_ImmediateAwaitingInputRuntime(),
        )
        try:
            async with await _client_for(app) as client:
                response = await client.post(
                    "/agentic/runs/sync",
                    json={
                        "request": "Analyze synthetic interactive input",
                        "session_id": "session_sync_interaction_unsupported",
                    },
                )

            assert response.status_code == 200, response.text
            body = response.json()
            assert body["status"] == "error"
            assert body["error"] == "当前协议不支持用户交互"
            assert body["final_answer"] == ""
            assert "pending_user_interaction" not in body

            snapshot = await harness.manager.repository.get(body["run_id"])
            assert snapshot is not None
            assert snapshot.status.value == "failed"
            assert snapshot.error is not None
            assert snapshot.error.error_code == "INTERACTION_NOT_SUPPORTED"
            assert snapshot.error.message == "当前协议不支持用户交互"
            assert snapshot.pending_interaction is None
            assert snapshot.result is not None
            assert snapshot.result.content is None
            assert snapshot.result.structured_content is None
            assert snapshot.result.history_messages == []

            durable_turn = harness.execution_store.get_turn(
                "session_sync_interaction_unsupported",
                snapshot.turn_id,
                run_id=body["run_id"],
            )
            assert durable_turn is not None
            assert durable_turn["status"] == "failed"
            assert durable_turn["final_output"] == {
                "error": "当前协议不支持用户交互"
            }
            assert durable_turn.get("pending_user_interaction") is None
            assert harness.execution_store.get_llm_context(
                "session_sync_interaction_unsupported"
            ) == [
                {
                    "role": "user",
                    "content": "Analyze synthetic interactive input",
                }
            ]

            follow_up, created = harness.execution_store.create_turn_for_run(
                session_id="session_sync_interaction_unsupported",
                user_request="Analyze synthetic follow-up input",
                protocol_metadata={"protocol": "agentic_sync"},
            )
            assert created is True
            assert follow_up["turn_id"] == snapshot.turn_id + 1
            assert follow_up["status"] == "running"
            _assert_no_sync_subscriber_leak(harness)
        finally:
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_admission_exit_cancellation_cancels_run_and_reservation(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        harness.manager.block_cancel = True
        original_session_guard = harness.manager.session_guard
        admission_exit_started = asyncio.Event()
        release_admission_exit = asyncio.Event()

        @asynccontextmanager
        async def blocking_session_guard(scope, session_id):
            async with original_session_guard(scope, session_id):
                yield
                admission_exit_started.set()
                await release_admission_exit.wait()

        monkeypatch.setattr(
            harness.manager,
            "session_guard",
            blocking_session_guard,
        )
        request_task = None
        try:
            async with await _client_for(app) as client:
                request_task = asyncio.create_task(
                    client.post(
                        "/agentic/runs/sync",
                        json={
                            "request": "Analyze synthetic cancellation input",
                            "session_id": "session_sync_admission_cancel",
                            "arguments": {"mock_scenario": "awaiting_input"},
                        },
                    )
                )
                await asyncio.wait_for(
                    admission_exit_started.wait(),
                    timeout=1.0,
                )
                await asyncio.wait_for(harness.runtime.started.wait(), timeout=1.0)
                request_task.cancel()
                await asyncio.wait_for(
                    harness.manager.cancel_started.wait(),
                    timeout=1.0,
                )
                request_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request_task

            _assert_no_sync_subscriber_leak(harness)
            harness.manager.cancel_release.set()
            await asyncio.wait_for(
                harness.manager.cancel_finished.wait(),
                timeout=1.0,
            )

            snapshots = await harness.manager.repository.list_snapshots()
            assert len(snapshots) == 1
            snapshot = snapshots[0]
            assert snapshot.status.value == "cancelled"
            assert snapshot.cancel_reason == "sync_request_cancelled"
            assert harness.manager.cancel_reasons == [
                "sync_request_cancelled"
            ]
            assert harness.runtime.cancel_reasons == [
                "sync_request_cancelled"
            ]
        finally:
            release_admission_exit.set()
            harness.manager.cancel_release.set()
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_cancelled_during_reservation_leaves_no_durable_turn(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        reserve_started = asyncio.Event()

        async def blocking_reserve(_run_id):
            reserve_started.set()
            await asyncio.Event().wait()

        monkeypatch.setattr(
            harness.manager.event_bus,
            "reserve_subscriber",
            blocking_reserve,
        )
        request_task = None
        try:
            async with await _client_for(app) as client:
                request_task = asyncio.create_task(
                    client.post(
                        "/agentic/runs/sync",
                        json={
                            "request": "Analyze synthetic admission input",
                            "session_id": "session_sync_reserve_cancel",
                        },
                    )
                )
                await asyncio.wait_for(reserve_started.wait(), timeout=1.0)
                request_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request_task

            history = harness.execution_store.get_session_history(
                "session_sync_reserve_cancel"
            )
            assert history is None
            assert await harness.manager.repository.list_snapshots() == []
            _assert_no_sync_subscriber_leak(harness)
        finally:
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_cancelled_during_turn_allocation_compensates_durable_turn(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        harness.short_io.block_create_turn = True
        request_task = None
        try:
            async with await _client_for(app) as client:
                request_task = asyncio.create_task(
                    client.post(
                        "/agentic/runs/sync",
                        json={
                            "request": "Analyze synthetic allocation input",
                            "session_id": "session_sync_allocation_cancel",
                        },
                    )
                )
                await asyncio.wait_for(
                    harness.short_io.create_started.wait(),
                    timeout=1.0,
                )
                request_task.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await request_task

            harness.short_io.create_release.set()
            await asyncio.wait_for(
                harness.short_io.create_finished.wait(),
                timeout=1.0,
            )
            await asyncio.wait_for(
                harness.short_io.mark_finished.wait(),
                timeout=1.0,
            )
            history = harness.execution_store.get_session_history(
                "session_sync_allocation_cancel"
            )
            assert len(history["turns"]) == 1
            durable_turn = history["turns"][0]
            assert durable_turn["status"] == "failed"
            assert durable_turn["error_code"] == "TASK_START_FAILED"
            assert await harness.manager.repository.list_snapshots() == []
            _assert_no_sync_subscriber_leak(harness)
        finally:
            harness.short_io.create_release.set()
            harness.short_io.mark_release.set()
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())


def test_sync_run_create_cancel_then_release_cancel_still_fails_turn(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        harness.manager.block_create_before_registration = True
        original_release = (
            harness.manager.event_bus.release_subscriber_reservation
        )
        release_started = asyncio.Event()
        release_gate = asyncio.Event()

        async def blocking_release(reservation_id):
            release_started.set()
            await release_gate.wait()
            await original_release(reservation_id)

        monkeypatch.setattr(
            harness.manager.event_bus,
            "release_subscriber_reservation",
            blocking_release,
        )
        request_task = None
        try:
            async with await _client_for(app) as client:
                request_task = asyncio.create_task(
                    client.post(
                        "/agentic/runs/sync",
                        json={
                            "request": "Analyze synthetic pre-registration input",
                            "session_id": "session_sync_create_cancel",
                        },
                    )
                )
                await asyncio.wait_for(
                    harness.manager.create_run_started.wait(),
                    timeout=1.0,
                )
                request_task.cancel()
                await asyncio.wait_for(release_started.wait(), timeout=1.0)
                request_task.cancel()
                await asyncio.sleep(0)
                release_gate.set()
                with pytest.raises(asyncio.CancelledError):
                    await request_task

            history = harness.execution_store.get_session_history(
                "session_sync_create_cancel"
            )
            assert len(history["turns"]) == 1
            durable_turn = history["turns"][0]
            assert durable_turn["status"] == "failed"
            assert durable_turn["error_code"] == "TASK_START_FAILED"
            assert await harness.manager.repository.list_snapshots() == []
            _assert_no_sync_subscriber_leak(harness)
        finally:
            harness.manager.create_run_release.set()
            release_gate.set()
            if request_task is not None and not request_task.done():
                request_task.cancel()
                await asyncio.gather(request_task, return_exceptions=True)
            await harness.manager.shutdown(
                deadline=asyncio.get_running_loop().time() + 1.0
            )

    asyncio.run(scenario())
