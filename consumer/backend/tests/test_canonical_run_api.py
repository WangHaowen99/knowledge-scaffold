# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Contract gates for the canonical native-ASGI Run API.

The tests intentionally combine the real durable ExecutionStore with a small
in-process manager fake.  This keeps the API contract deterministic while
still proving that admission retries and HITL answers mutate durable state
exactly once.
"""

from __future__ import annotations

from internal_access import internal
import asyncio
import importlib.util
import sys
import threading
from contextlib import asynccontextmanager
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import httpx
from agent.runtime.contracts import (
    TERMINAL_RUN_STATUSES,
    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
    PublicEventDraft,
    RunPreflightSnapshot,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeInvocationContext,
    RuntimeResult,
)
from agent.runtime.errors import SessionBusyError
from agent.runtime.event_bus import AsyncEventBus
from agent.runtime.runtime import RuntimeState
from agent.runtime.store import InMemoryRunRepository, utc_now_iso
from api.auth.auth import AuthUser
from fastapi import FastAPI
from persistence.execution_store import ExecutionStore
from starlette.requests import Request

# Load the focused router without importing the aggregate ``api.routes``
# package.  During the staged ASGI migration, an unrelated router can be
# temporarily incomplete; that must not hide failures in this Run contract.
_WORKFLOWS_PATH = Path(__file__).parents[1] / "api" / "routes" / "workflows.py"
_WORKFLOWS_SPEC = importlib.util.spec_from_file_location(
    "canonical_workflows_contract_under_test",
    _WORKFLOWS_PATH,
)
assert _WORKFLOWS_SPEC is not None and _WORKFLOWS_SPEC.loader is not None
workflows = importlib.util.module_from_spec(_WORKFLOWS_SPEC)
sys.modules[_WORKFLOWS_SPEC.name] = workflows
_WORKFLOWS_SPEC.loader.exec_module(workflows)


_USER = AuthUser(username="operator_a", role="user")
_WORKSPACE_ID = "workspace_demo"


class _InlineOffloadPool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _PermitAllLimiter:
    @staticmethod
    def try_acquire() -> bool:
        return True


class _SyntheticOrchestrator:
    run_rate_limiter = _PermitAllLimiter()


class _CountingManager:
    """Minimal live Run authority with observable execution registration."""

    def __init__(self) -> None:
        self.repository = InMemoryRunRepository()
        self.event_bus = AsyncEventBus(self.repository)
        self.started_contexts: list[RuntimeInvocationContext] = []
        self.completion_result: RuntimeResult | None = None
        self.completion_events: list[tuple[str, str, dict[str, Any]]] = []
        self.completion_pending_interaction: dict[str, Any] | None = None
        self.cancel_reasons: list[str] = []
        self._active_by_session: dict[tuple[str, str, str], str] = {}
        self._session_lock = asyncio.Lock()

    @property
    def task_start_count(self) -> int:
        return len(self.started_contexts)

    @asynccontextmanager
    async def session_guard(self, _scope: RunScope, _session_id: str):
        async with self._session_lock:
            yield

    async def assert_session_available(
        self,
        scope: RunScope,
        session_id: str,
    ) -> None:
        key = (scope.username, scope.workspace_id, session_id)
        run_id = self._active_by_session.get(key)
        if run_id is None:
            return
        snapshot = await self.repository.get(run_id)
        if snapshot is not None and snapshot.status not in TERMINAL_RUN_STATUSES:
            raise SessionBusyError(run_id)
        self._active_by_session.pop(key, None)

    async def create_run(
        self,
        context: RuntimeInvocationContext,
        scope: RunScope,
        *,
        session_guarded: bool = False,
        **_kwargs: Any,
    ) -> tuple[RunSnapshot, bool]:
        assert session_guarded is True
        now = utc_now_iso()
        snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.QUEUED,
            created_at=now,
            updated_at=now,
            last_seq=0,
            scope=scope,
            context=context,
        )
        stored, created = await self.repository.create(snapshot)
        if created:
            self.started_contexts.append(context)
            self._active_by_session[(scope.username, scope.workspace_id, context.session_id)] = context.run_id
            if self.completion_result is not None:
                for event_type, status, payload in self.completion_events:
                    await self.event_bus.publish(
                        PublicEventDraft(
                            run_id=context.run_id,
                            session_id=context.session_id,
                            turn_id=context.turn_id,
                            event_type=event_type,
                            status=status,
                            source={"node": "synthetic", "component": "test"},
                            payload=payload,
                        )
                    )
                await internal(self).finish(
                    stored,
                    result=self.completion_result,
                    pending_interaction=self.completion_pending_interaction,
                )
        return stored, created

    async def get_run(
        self,
        run_id: str,
        scope: RunScope,
    ) -> RunSnapshot | None:
        snapshot = await self.repository.get(run_id)
        if snapshot is None or not scope.owns(snapshot):
            return None
        return snapshot

    async def cancel_run(
        self,
        run_id: str,
        scope: RunScope,
        reason: str = "user_requested",
    ) -> RunSnapshot:
        snapshot = await self.get_run(run_id, scope)
        assert snapshot is not None
        if snapshot.status in TERMINAL_RUN_STATUSES:
            return snapshot
        self.cancel_reasons.append(reason)
        return await internal(self).finish(
            snapshot,
            result=RuntimeResult(status="cancelled"),
            cancel_reason=reason,
        )

    async def _finish(
        self,
        snapshot: RunSnapshot,
        *,
        result: RuntimeResult,
        pending_interaction: dict[str, Any] | None = None,
        cancel_reason: str | None = None,
    ) -> RunSnapshot:
        status = RunStatus(result.status)
        payload: dict[str, Any] = {"status": status.value}
        if result.content is not None:
            payload["final_output"] = result.content
        elif result.structured_content is not None:
            payload["final_output"] = result.structured_content
        if result.error is not None:
            payload["error"] = result.error.message
            payload["error_code"] = result.error.error_code
        if cancel_reason:
            payload["cancel_reason"] = cancel_reason

        def update(current: RunSnapshot) -> RunSnapshot:
            return replace(
                current,
                status=status,
                result=result,
                error=result.error,
                pending_interaction=(dict(pending_interaction) if pending_interaction is not None else None),
                cancel_reason=cancel_reason,
                updated_at=utc_now_iso(),
            )

        _event, updated = await self.event_bus.publish(
            PublicEventDraft(
                run_id=snapshot.run_id,
                session_id=snapshot.session_id,
                turn_id=snapshot.turn_id,
                event_type="turn.finished",
                status=status.value,
                source={"node": "synthetic", "component": "test"},
                payload=payload,
            ),
            snapshot_update=update,
        )
        await self.event_bus.close(snapshot.run_id)
        return updated

    async def add_live_run(
        self,
        *,
        run_id: str,
        session_id: str,
        turn_id: int,
        status: RunStatus = RunStatus.QUEUED,
    ) -> RunSnapshot:
        scope = RunScope(
            username=_USER.username,
            workspace_id=_WORKSPACE_ID,
            scopes=("agent:read",),
        )
        context = RuntimeInvocationContext(
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            workspace_id=_WORKSPACE_ID,
            username=_USER.username,
            query="Synthetic request",
        )
        now = utc_now_iso()
        snapshot = RunSnapshot(
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            status=status,
            created_at=now,
            updated_at=now,
            last_seq=0,
            scope=scope,
            context=context,
        )
        stored, created = await self.repository.create(snapshot)
        assert created is True
        return stored


@dataclass
class _Runtime:
    manager: _CountingManager
    short_io: _InlineOffloadPool
    state: RuntimeState = RuntimeState.ACCEPTING


@dataclass
class _Harness:
    storage_dir: Path
    execution_store: ExecutionStore
    manager: _CountingManager
    runtime: _Runtime


def _install_harness(monkeypatch, tmp_path) -> tuple[FastAPI, _Harness]:
    storage_dir = tmp_path / "synthetic-executions"
    manager = _CountingManager()
    runtime = _Runtime(manager=manager, short_io=_InlineOffloadPool())
    harness = _Harness(
        storage_dir=storage_dir,
        execution_store=ExecutionStore(str(storage_dir)),
        manager=manager,
        runtime=runtime,
    )
    orchestrator = _SyntheticOrchestrator()

    monkeypatch.setattr(workflows, "_get_runtime", lambda: harness.runtime)
    monkeypatch.setattr(workflows, "_get_orchestrator", lambda: orchestrator)
    monkeypatch.setattr(
        workflows,
        "_get_stores",
        lambda: (None, harness.execution_store),
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

    app.dependency_overrides[workflows.require_authenticated_user] = authenticated_user_override
    app.dependency_overrides[workflows.require_workspace_id] = workspace_override
    return app, harness


def _headers(key: str) -> dict[str, str]:
    return {"Idempotency-Key": key}


def _ordinary_payload(
    *,
    request: str = "Analyze synthetic input",
    session_id: str = "session_api_contract",
) -> dict[str, Any]:
    return {
        "request": request,
        "session_id": session_id,
        "agent_mode": "quick",
        "llm_request_mode": "stream",
        "requested_operation_key": "operation_demo",
        "include_trace": False,
        # Client-provided identities must never become Run authority.
        "run_id": "client_supplied_run_id",
        "turn_id": 9001,
    }


async def _client_for(app: FastAPI):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="https://testserver",
    )


def test_store_resolution_uses_short_io_without_blocking_event_loop(monkeypatch):
    async def scenario() -> None:
        started = threading.Event()
        release = threading.Event()
        worker_thread_ids: list[int] = []

        def slow_get_stores():
            worker_thread_ids.append(threading.get_ident())
            started.set()
            if not release.wait(timeout=2.0):
                raise TimeoutError("test did not release slow store construction")
            return "chat-store", "execution-store"

        class _ThreadedOffloadPool:
            async def run(self, function, *args, **kwargs):
                return await asyncio.to_thread(function, *args, **kwargs)

        monkeypatch.setattr(workflows, "_get_stores", slow_get_stores)
        runtime = type("Runtime", (), {"short_io": _ThreadedOffloadPool()})()
        resolution = asyncio.create_task(internal(workflows).resolve_stores(runtime))

        try:
            deadline = asyncio.get_running_loop().time() + 1.0
            while not started.is_set():
                assert asyncio.get_running_loop().time() < deadline
                await asyncio.sleep(0)

            await asyncio.sleep(0)
            assert resolution.done() is False
            assert len(worker_thread_ids) == 1
            assert worker_thread_ids[0] != threading.get_ident()
        finally:
            release.set()
        assert await resolution == ("chat-store", "execution-store")

    asyncio.run(scenario())


def test_post_run_allocates_server_ids_and_exact_retry_starts_one_task(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        async with await _client_for(app) as client:
            first = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(),
                headers=_headers("ordinary-request-key"),
            )
            assert first.status_code == 202, first.text
            created = first.json()
            assert created["created"] is True
            assert created["run_id"].startswith("agentic-")
            assert created["run_id"] != "client_supplied_run_id"
            assert created["turn_id"] == 1
            assert created["turn_id"] != 9001
            assert created["session_id"] == "session_api_contract"
            assert created["status"] == "queued"
            assert harness.manager.started_contexts[0].requested_operation_key == "operation_demo"
            assert harness.manager.started_contexts[0].concurrency_limit == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
            assert created["links"] == {
                "self": "/agentic/runs/{}".format(created["run_id"]),
                "events": "/agentic/runs/{}/events".format(created["run_id"]),
                "cancel": "/agentic/runs/{}/cancel".format(created["run_id"]),
            }
            assert harness.manager.task_start_count == 1

            retry = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(),
                headers=_headers("ordinary-request-key"),
            )
            assert retry.status_code == 200, retry.text
            retried = retry.json()
            assert retried["created"] is False
            assert retried["run_id"] == created["run_id"]
            assert retried["turn_id"] == created["turn_id"]
            assert harness.manager.task_start_count == 1
            history = harness.execution_store.get_session_history("session_api_contract")
            assert len(history["turns"]) == 1

    asyncio.run(scenario())


def test_post_run_attaches_one_protocol_preflight_snapshot(monkeypatch, tmp_path):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        expected = RunPreflightSnapshot(
            workspace_id=_WORKSPACE_ID,
            workspace_config_generation=1,
            system_config_generation=2,
            deterministic_candidate=False,
            model_profile={"id": "profile_demo"},
        )
        calls = []

        class _OptimizedOrchestrator(_SyntheticOrchestrator):
            @staticmethod
            def build_run_preflight_snapshot(**kwargs):
                calls.append(kwargs)
                return expected

        monkeypatch.setattr(
            workflows,
            "_get_orchestrator",
            lambda: _OptimizedOrchestrator(),
        )
        monkeypatch.setattr(
            workflows,
            "resolve_effective_llm_profile_config",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(
                AssertionError("optimized protocol path must resolve once")
            ),
        )

        async with await _client_for(app) as client:
            response = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(session_id="session_preflight_contract"),
                headers=_headers("preflight-request-key"),
            )

        assert response.status_code == 202, response.text
        assert len(calls) == 1
        assert calls[0]["workspace_id"] == _WORKSPACE_ID
        assert harness.manager.started_contexts[0].preflight_snapshot is expected

    asyncio.run(scenario())


def test_post_run_has_no_whole_run_timeout(monkeypatch, tmp_path):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        async with await _client_for(app) as client:
            response = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(session_id="session_no_total_timeout"),
                headers=_headers("no-total-timeout-key"),
            )

        assert response.status_code == 202, response.text
        assert harness.manager.started_contexts[0].timeout_seconds is None

    asyncio.run(scenario())


def test_sync_run_waits_for_terminal_and_returns_legacy_trace(monkeypatch, tmp_path):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        harness.manager.completion_events = [
            (
                "tool.started",
                "running",
                {"name": "synthetic_tool", "tool_call_id": "call_demo"},
            ),
            (
                "tool.finished",
                "failed",
                {
                    "name": "synthetic_tool",
                    "tool_call_id": "call_demo",
                    "status": "failed",
                    "error": {"message": "Synthetic tool failure"},
                },
            ),
        ]
        harness.manager.completion_result = RuntimeResult(
            status="completed",
            structured_content={"text": "Synthetic structured answer"},
        )

        async with await _client_for(app) as client:
            response = await client.post(
                "/agentic/runs/sync",
                json={
                    "request": "Analyze synthetic input",
                    "session_id": "session_sync_contract",
                    "context": {
                        "agent_mode": "thinking",
                        "llm_request_mode": "sync",
                    },
                    "requested_operation_key": "operation_demo",
                    "include_trace": True,
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body == {
            "status": "success",
            "final_answer": "Synthetic structured answer",
            "session_id": "session_sync_contract",
            "run_id": body["run_id"],
            "execution_trace": {
                "tool_calls": ["synthetic_tool"],
                "tool_failures": [
                    {
                        "tool_name": "synthetic_tool",
                        "error": "Synthetic tool failure",
                    }
                ],
            },
        }
        context = harness.manager.started_contexts[0]
        assert context.supports_user_interaction is False
        assert context.agent_mode == "thinking"
        assert context.llm_request_mode == "sync"
        assert context.requested_operation_key == "operation_demo"
        assert context.concurrency_limit == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
        assert context.timeout_seconds is None
        history = harness.execution_store.get_session_history("session_sync_contract")
        assert len(history["turns"]) == 1
        assert history["turns"][0]["protocol_metadata"] == {"protocol": "agentic_sync"}
        assert harness.manager.event_bus.active_subscriber_count == 0
        assert harness.manager.event_bus.reserved_subscriber_count == 0

    asyncio.run(scenario())


def test_sync_run_projects_public_terminal_failure(monkeypatch, tmp_path):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        error = RuntimeErrorInfo(
            error_code="MODEL_PROVIDER_ERROR",
            message="Synthetic provider failure",
            source="model",
        )
        harness.manager.completion_result = RuntimeResult(
            status="failed",
            error=error,
        )

        async with await _client_for(app) as client:
            response = await client.post(
                "/agentic/runs/sync",
                json={
                    "request": "Analyze synthetic input",
                    "session_id": "session_sync_failure",
                },
            )

        assert response.status_code == 200, response.text
        body = response.json()
        assert body["status"] == "error"
        assert body["final_answer"] == ""
        assert body["error"] == "Synthetic provider failure"
        assert body["session_id"] == "session_sync_failure"
        assert body["run_id"].startswith("agentic-")
        assert "execution_trace" not in body

    asyncio.run(scenario())


def test_sync_run_rate_limit_and_input_validation_do_not_start_runs(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)

        class _RejectingLimiter:
            @staticmethod
            def try_acquire() -> bool:
                return False

        monkeypatch.setattr(
            workflows,
            "_get_orchestrator",
            lambda: type(
                "SyntheticLimitedOrchestrator",
                (),
                {"run_rate_limiter": _RejectingLimiter()},
            )(),
        )
        async with await _client_for(app) as client:
            invalid = await client.post(
                "/agentic/runs/sync",
                json={
                    "request": "Analyze synthetic input",
                    "session_id": "session_sync_invalid",
                    "arguments": [],
                },
            )
            limited = await client.post(
                "/agentic/runs/sync",
                json={
                    "request": "Analyze synthetic input",
                    "session_id": "session_sync_limited",
                },
            )

        assert invalid.status_code == 400
        assert invalid.json()["error"] == "arguments must be an object or null"
        assert limited.status_code == 429
        assert limited.json()["error"] == "请求过于频繁，请稍后重试"
        assert limited.json()["limit"] == 50
        assert harness.manager.task_start_count == 0

    asyncio.run(scenario())


def test_post_run_rejects_idempotency_conflict_and_same_session_busy(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        async with await _client_for(app) as client:
            first = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(),
                headers=_headers("conflict-request-key"),
            )
            assert first.status_code == 202, first.text
            first_run_id = first.json()["run_id"]

            operation_conflict_payload = _ordinary_payload()
            operation_conflict_payload["requested_operation_key"] = "operation_other"
            operation_conflict = await client.post(
                "/agentic/runs",
                json=operation_conflict_payload,
                headers=_headers("conflict-request-key"),
            )
            assert operation_conflict.status_code == 409, operation_conflict.text
            assert operation_conflict.json()["error"]["error_code"] == "IDEMPOTENCY_CONFLICT"

            conflict = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(request="Different synthetic input"),
                headers=_headers("conflict-request-key"),
            )
            assert conflict.status_code == 409, conflict.text
            assert conflict.json()["error"]["error_code"] == "IDEMPOTENCY_CONFLICT"

            busy = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(request="Another synthetic input"),
                headers=_headers("different-request-key"),
            )
            assert busy.status_code == 409, busy.text
            assert busy.json()["error"]["error_code"] == "SESSION_BUSY"
            assert busy.json()["run_id"] == first_run_id
            assert harness.manager.task_start_count == 1
            history = harness.execution_store.get_session_history("session_api_contract")
            assert len(history["turns"]) == 1

    asyncio.run(scenario())


def test_same_session_concurrent_admission_accepts_exactly_one(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        async with await _client_for(app) as client:
            responses = await asyncio.gather(
                *(
                    client.post(
                        "/agentic/runs",
                        json=_ordinary_payload(request=f"Synthetic concurrent input {index}"),
                        headers=_headers(f"same-session-key-{index}"),
                    )
                    for index in range(20)
                )
            )

        accepted = [response for response in responses if response.status_code == 202]
        rejected = [response for response in responses if response.status_code == 409]
        assert len(accepted) == 1
        assert len(rejected) == 19
        assert all(response.json()["error"]["error_code"] == "SESSION_BUSY" for response in rejected)
        assert {response.json()["run_id"] for response in rejected} == {accepted[0].json()["run_id"]}
        assert harness.manager.task_start_count == 1
        history = harness.execution_store.get_session_history("session_api_contract")
        assert len(history["turns"]) == 1

    asyncio.run(scenario())


def test_hitl_answer_creates_new_run_and_exact_repeat_reuses_it(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        original, created = harness.execution_store.create_turn_for_run(
            session_id="session_hitl_api",
            user_request="Start synthetic choice",
            idempotency_key_hash="initial-key-hash",
            request_fingerprint="initial-fingerprint",
        )
        assert created is True
        interaction = {
            "id": "interaction_synthetic_choice",
            "type": "single_choice",
            "prompt": "Choose a synthetic option",
            "options": [
                {
                    "id": "option_a",
                    "label": "Option A",
                    "value": "value_a",
                },
                {
                    "id": "option_b",
                    "label": "Option B",
                    "value": "value_b",
                },
            ],
        }
        harness.execution_store.await_turn(
            "session_hitl_api",
            original["turn_id"],
            final_output={
                "status": "awaiting_input",
                "interaction": interaction,
            },
        )
        answer_a = {
            "interaction_id": interaction["id"],
            "type": interaction["type"],
            "option_id": "option_a",
            "value": "value_a",
        }
        payload_a = {
            "request": "Use synthetic option A",
            "session_id": "session_hitl_api",
            "interaction_response": answer_a,
        }

        async with await _client_for(app) as client:
            answered = await client.post(
                "/agentic/runs",
                json=payload_a,
                headers=_headers("hitl-answer-key"),
            )
            assert answered.status_code == 202, answered.text
            answer_run = answered.json()
            assert answer_run["created"] is True
            assert answer_run["run_id"] != original["run_id"]
            assert answer_run["turn_id"] == original["turn_id"] + 1
            assert harness.manager.task_start_count == 1

            repeated = await client.post(
                "/agentic/runs",
                json=payload_a,
                headers=_headers("hitl-answer-retry-with-new-key"),
            )
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["created"] is False
            assert repeated.json()["run_id"] == answer_run["run_id"]
            assert repeated.json()["turn_id"] == answer_run["turn_id"]
            assert harness.manager.task_start_count == 1

            answer_b = {
                "interaction_id": interaction["id"],
                "type": interaction["type"],
                "option_id": "option_b",
                "value": "value_b",
            }
            conflicting = await client.post(
                "/agentic/runs",
                json={
                    "request": "Use synthetic option B",
                    "session_id": "session_hitl_api",
                    "interaction_response": answer_b,
                },
                headers=_headers("hitl-conflicting-answer-key"),
            )
            assert conflicting.status_code == 409, conflicting.text
            assert conflicting.json()["error"]["error_code"] == "INTERACTION_ALREADY_CONSUMED"
            assert conflicting.json()["run_id"] == answer_run["run_id"]

        history = harness.execution_store.get_session_history("session_hitl_api")
        assert len(history["turns"]) == 2
        persisted_original, persisted_answer = history["turns"]
        assert persisted_original["run_id"] == original["run_id"]
        assert persisted_original["status"] == "awaiting_input"
        assert persisted_original["interaction_consumed_by_turn_id"] == 2
        assert persisted_answer["run_id"] == answer_run["run_id"]

    asyncio.run(scenario())


def test_retired_stream_is_gone_and_other_removed_routes_are_hard_404(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, _harness = _install_harness(monkeypatch, tmp_path)
        async with await _client_for(app) as client:
            responses = [
                await client.post(
                    "/agentic/runs/" + "stream",
                    json=_ordinary_payload(),
                    headers=_headers("removed-stream-key"),
                ),
                await client.post(
                    "/agentic/runs/run_demo/" + "interact",
                    json={"interaction_response": {}},
                    headers=_headers("removed-interact-key"),
                ),
                await client.get("/agentic/runs/run_demo/" + "status"),
            ]
        retired_stream = responses[0]
        assert retired_stream.status_code == 410
        assert retired_stream.json() == {
            "error": {
                "type": "async_run_error",
                "error_code": "ENDPOINT_RETIRED",
                "detail": (
                    "POST /agentic/runs/stream has been retired; create a Run "
                    "with POST /agentic/runs and follow links.events"
                ),
                "retriable": False,
            },
            "replacement": {
                "create": {
                    "method": "POST",
                    "path": "/agentic/runs",
                },
                "events": {
                    "method": "GET",
                    "path": "/agentic/runs/{run_id}/events",
                },
            },
        }
        assert [response.status_code for response in responses] == [
            410,
            404,
            404,
        ]

    asyncio.run(scenario())


def test_get_run_prefers_live_state_then_uses_interrupted_durable_fallback(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        app, harness = _install_harness(monkeypatch, tmp_path)
        async with await _client_for(app) as client:
            created = await client.post(
                "/agentic/runs",
                json=_ordinary_payload(session_id="session_restart_contract"),
                headers=_headers("restart-request-key"),
            )
            assert created.status_code == 202, created.text
            run_id = created.json()["run_id"]

            live = await client.get("/agentic/runs/{}".format(run_id))
            assert live.status_code == 200, live.text
            assert live.json()["run_id"] == run_id
            assert live.json()["status"] == "queued"

            prior_manager = harness.manager
            harness.execution_store = ExecutionStore(str(harness.storage_dir))
            harness.manager = _CountingManager()
            harness.runtime.manager = harness.manager

            durable = await client.get("/agentic/runs/{}".format(run_id))
            assert durable.status_code == 200, durable.text
            assert durable.json()["run_id"] == run_id
            assert durable.json()["status"] == "failed"
            assert durable.json()["error"]["error_code"] == "SERVICE_RESTARTED"
            assert prior_manager.task_start_count == 1
            assert harness.manager.task_start_count == 0

    asyncio.run(scenario())


def test_sse_lazy_reservation_is_released_when_client_breaks_before_events(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        _app, harness = _install_harness(monkeypatch, tmp_path)
        run_id = "agentic-synthetic-live-run"
        await harness.manager.add_live_run(
            run_id=run_id,
            session_id="session_sse_contract",
            turn_id=1,
        )
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/agentic/runs/{}/events".format(run_id),
                "raw_path": b"/agentic/runs/events",
                "query_string": b"after_seq=0",
                "root_path": "",
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
        )

        response = await workflows.stream_run_events(
            run_id,
            request,
            current_user=_USER,
            workspace_id=_WORKSPACE_ID,
        )
        assert response.status_code == 200
        assert harness.manager.event_bus.active_subscriber_count == 0
        assert harness.manager.event_bus.reserved_subscriber_count == 1

        iterator = response.body_iterator
        first_chunk = await anext(iterator)
        assert str(first_chunk).startswith("retry: 2000")
        assert harness.manager.event_bus.active_subscriber_count == 0
        assert harness.manager.event_bus.reserved_subscriber_count == 1

        await iterator.aclose()
        await asyncio.sleep(0)
        assert harness.manager.event_bus.active_subscriber_count == 0
        assert harness.manager.event_bus.reserved_subscriber_count == 0

    asyncio.run(scenario())


def test_sse_replays_terminal_event_when_reservation_expires_before_iteration(
    monkeypatch,
    tmp_path,
):
    async def scenario() -> None:
        _app, harness = _install_harness(monkeypatch, tmp_path)
        run_id = "agentic-synthetic-fast-terminal"
        snapshot = await harness.manager.add_live_run(
            run_id=run_id,
            session_id="session_sse_fast_terminal",
            turn_id=1,
        )
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0"},
                "http_version": "1.1",
                "method": "GET",
                "scheme": "http",
                "path": "/agentic/runs/{}/events".format(run_id),
                "raw_path": b"/agentic/runs/events",
                "query_string": b"after_seq=0",
                "root_path": "",
                "headers": [],
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
            }
        )

        response = await workflows.stream_run_events(
            run_id,
            request,
            current_user=_USER,
            workspace_id=_WORKSPACE_ID,
        )
        await internal(harness.manager).finish(
            snapshot,
            result=RuntimeResult(status="completed", content="done"),
        )

        iterator = response.body_iterator
        assert str(await anext(iterator)).startswith("retry: 2000")
        terminal_chunk = str(await anext(iterator))
        assert "event: turn.finished" in terminal_chunk
        assert '"status":"completed"' in terminal_chunk
        await iterator.aclose()
        assert harness.manager.event_bus.reserved_subscriber_count == 0

    asyncio.run(scenario())
