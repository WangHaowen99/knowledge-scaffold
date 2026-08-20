"""Contract gates for the canonical native-ASGI Run API.

The tests intentionally combine the real durable ExecutionStore with a small
in-process manager fake.  This keeps the API contract deterministic while
still proving that admission retries and HITL answers mutate durable state
exactly once.
"""

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
from starlette.requests import Request

from agent.runtime.contracts import (
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.errors import SessionBusyError
from agent.runtime.event_bus import AsyncEventBus
from agent.runtime.runtime import RuntimeState
from agent.runtime.store import InMemoryRunRepository, utc_now_iso
from api.auth.auth import AuthUser
from persistence.execution_store import ExecutionStore


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
            self._active_by_session[
                (scope.username, scope.workspace_id, context.session_id)
            ] = context.run_id
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

    app.dependency_overrides[
        workflows.require_authenticated_user
    ] = authenticated_user_override
    app.dependency_overrides[
        workflows.require_workspace_id
    ] = workspace_override
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
        "include_trace": False,
        # Client-provided identities must never become Run authority.
        "run_id": "client_supplied_run_id",
        "turn_id": 9001,
    }


async def _client_for(app: FastAPI):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://testserver",
    )


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
            history = harness.execution_store.get_session_history(
                "session_api_contract"
            )
            assert len(history["turns"]) == 1

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
            history = harness.execution_store.get_session_history(
                "session_api_contract"
            )
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
                        json=_ordinary_payload(
                            request=f"Synthetic concurrent input {index}"
                        ),
                        headers=_headers(f"same-session-key-{index}"),
                    )
                    for index in range(20)
                )
            )

        accepted = [response for response in responses if response.status_code == 202]
        rejected = [response for response in responses if response.status_code == 409]
        assert len(accepted) == 1
        assert len(rejected) == 19
        assert all(
            response.json()["error"]["error_code"] == "SESSION_BUSY"
            for response in rejected
        )
        assert {
            response.json()["run_id"] for response in rejected
        } == {accepted[0].json()["run_id"]}
        assert harness.manager.task_start_count == 1
        history = harness.execution_store.get_session_history(
            "session_api_contract"
        )
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
            assert (
                conflicting.json()["error"]["error_code"]
                == "INTERACTION_ALREADY_CONSUMED"
            )
            assert conflicting.json()["run_id"] == answer_run["run_id"]

        history = harness.execution_store.get_session_history(
            "session_hitl_api"
        )
        assert len(history["turns"]) == 2
        persisted_original, persisted_answer = history["turns"]
        assert persisted_original["run_id"] == original["run_id"]
        assert persisted_original["status"] == "awaiting_input"
        assert persisted_original["interaction_consumed_by_turn_id"] == 2
        assert persisted_answer["run_id"] == answer_run["run_id"]

    asyncio.run(scenario())


def test_removed_run_routes_are_hard_404(monkeypatch, tmp_path):
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
                await client.post(
                    "/agentic/runs/" + "sync",
                    json=_ordinary_payload(),
                    headers=_headers("removed-sync-key"),
                ),
                await client.get("/agentic/runs/run_demo/" + "status"),
            ]
        assert [response.status_code for response in responses] == [
            404,
            404,
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
