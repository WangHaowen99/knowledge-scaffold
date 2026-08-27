"""ASGI contract tests for ACP canonical Run admission and HITL answers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.runtime.contracts import (
    PublicEvent,
    RunSnapshot,
    RunStatus,
    RuntimeResult,
)
from api.auth.auth import AuthUser
from api.protocols.acp import server as acp_server
from api.testing import TestApplication
from persistence.execution_store import ExecutionStore


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _Manager:
    def __init__(self):
        self.snapshots = {}

    @asynccontextmanager
    async def session_guard(self, _scope, _session_id):
        yield

    async def assert_session_available(self, _scope, _session_id):
        return None

    async def create_run(self, context, scope, *, session_guarded=False):
        assert session_guarded is True
        snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.QUEUED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            last_seq=0,
            scope=scope,
            context=context,
        )
        self.snapshots[context.run_id] = snapshot
        return snapshot, True

    async def get_run(self, run_id, scope):
        snapshot = self.snapshots.get(run_id)
        return snapshot if snapshot is not None and scope.owns(snapshot) else None

    async def cancel_run(self, run_id, scope, reason):
        del reason
        snapshot = await self.get_run(run_id, scope)
        snapshot = replace(
            snapshot,
            status=RunStatus.CANCELLED,
            result=RuntimeResult(status="cancelled"),
        )
        self.snapshots[run_id] = snapshot
        return snapshot


class _EventHub:
    def __init__(self, manager):
        self.manager = manager

    def stream(self, run_id, *, after_seq=0):
        async def generate():
            snapshot = self.manager.snapshots[run_id]
            completed = replace(
                snapshot,
                status=RunStatus.COMPLETED,
                result=RuntimeResult(status="completed", content="synthetic answer"),
                last_seq=1,
                updated_at="2026-01-01T00:00:01Z",
            )
            self.manager.snapshots[run_id] = completed
            if after_seq < 1:
                yield PublicEvent(
                    event_id="event_demo",
                    seq=1,
                    run_id=run_id,
                    session_id=completed.session_id,
                    turn_id=completed.turn_id,
                    event_type="turn.finished",
                    status="completed",
                    source={"component": "synthetic"},
                    payload={"status": "completed", "content": "synthetic answer"},
                    created_at="2026-01-01T00:00:01Z",
                )

        return generate()


def _app(store, manager):
    app = TestApplication()
    app.state.runtime_container = SimpleNamespace(
        manager=manager,
        short_io=_InlinePool(),
        event_hub=_EventHub(manager),
    )
    app.include_router(acp_server.acp_router)
    user = AuthUser(username="operator_a", role="user")
    app.dependency_overrides[acp_server._acp_principal] = lambda: user
    app.dependency_overrides[acp_server._acp_workspace_id] = lambda: "workspace_demo"
    return app


def test_acp_create_uses_server_generated_canonical_run(monkeypatch, tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    manager = _Manager()

    async def resolve_stores(*_args):
        return object(), store

    monkeypatch.setattr(acp_server, "_stores", resolve_stores)

    response = TestClient(_app(store, manager)).post(
        "/acp/runs",
        json={
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "synthetic question"}]}
            ],
            "context": {"session_id": "session_demo"},
        },
    )

    assert response.status_code == 202
    body = response.json()
    assert body["run_id"].startswith("run_")
    assert body["metadata"]["canonical_run_id"].startswith("agentic-")
    assert body["run_id"] != body["metadata"]["canonical_run_id"]
    _session_id, turn = store.find_turn_by_protocol_id("acp", body["run_id"])
    assert turn["run_id"] == body["metadata"]["canonical_run_id"]


def test_acp_await_creates_new_run_and_keeps_external_id(monkeypatch, tmp_path):
    store = ExecutionStore(str(tmp_path / "executions"))
    manager = _Manager()

    async def resolve_stores(*_args):
        return object(), store

    monkeypatch.setattr(acp_server, "_stores", resolve_stores)
    client = TestClient(_app(store, manager))
    created = client.post(
        "/acp/runs",
        json={
            "messages": [
                {"role": "user", "parts": [{"type": "text", "content": "synthetic question"}]}
            ],
            "context": {"session_id": "session_demo"},
        },
    ).json()
    external_run_id = created["run_id"]
    first_run_id = created["metadata"]["canonical_run_id"]
    _session_id, first_turn = store.find_turn_by_protocol_id("acp", external_run_id)
    interaction = {
        "id": "interaction_demo",
        "type": "single_choice",
        "prompt": "Choose a synthetic option",
        "options": [{"id": "option_a", "label": "Option A", "value": "value_a"}],
    }
    store.await_turn(
        "session_demo",
        first_turn["turn_id"],
        final_output={"type": "user_interaction", "interaction": interaction},
    )
    first_snapshot = manager.snapshots[first_run_id]
    manager.snapshots[first_run_id] = replace(
        first_snapshot,
        status=RunStatus.AWAITING_INPUT,
        result=RuntimeResult(status="awaiting_input"),
        pending_interaction=interaction,
    )

    response = client.post(
        "/acp/runs/{}/await".format(external_run_id),
        json={
            "await_id": "interaction_demo",
            "option_id": "option_a",
            "response_text": "Option A",
        },
    )

    assert response.status_code == 200
    body = response.json()
    assert body["run_id"] == external_run_id
    assert body["status"] == "completed"
    assert body["metadata"]["canonical_run_id"] != first_run_id
    history = store.get_session_history("session_demo")
    assert len(history["turns"]) == 2
    assert history["turns"][0]["status"] == "awaiting_input"
    assert history["turns"][1]["run_id"] == body["metadata"]["canonical_run_id"]
