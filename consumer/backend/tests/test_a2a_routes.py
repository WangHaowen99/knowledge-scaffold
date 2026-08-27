"""FastAPI route tests for the A2A canonical Run projection."""

from __future__ import annotations

from types import SimpleNamespace

from fastapi.testclient import TestClient

from agent.runtime.contracts import PublicEvent
from api.auth.auth import AuthUser, require_authenticated_user
from api.dependencies import require_workspace_id
from api.protocols.a2a.models import A2ATaskSnapshot, A2ATaskState
from api.routes import a2a as a2a_routes
from api.testing import TestApplication


class _Service:
    def __init__(self, *, view=None, snapshot=None):
        self.view = view or A2ATaskSnapshot(
            id="missing_task",
            session_id=None,
            state=A2ATaskState.NOT_FOUND,
            error="Task not found",
        )
        self.snapshot = snapshot
        self.created_request = None

    async def create_task(self, request):
        self.created_request = request
        return self.view, self.snapshot

    async def get_task(self, _task_id, *, session_id=None):
        del session_id
        return self.view, self.snapshot

    async def canonical_run_id(self, _task_id):
        return self.view.metadata.get("canonical_run_id")

    async def cancel_task(self, _task_id):
        return self.view


class _Repository:
    async def list_after(self, _run_id, _after_seq):
        return []


class _EventHub:
    def __init__(self, events):
        self.events = events

    async def reserve_subscriber(self, _run_id):
        return "reservation_demo"

    async def release_subscriber_reservation(self, _reservation_id):
        return None

    def stream(self, _run_id, *, after_seq=0, reservation_id=None):
        del reservation_id

        async def generate():
            for event in self.events:
                if event.seq > after_seq:
                    yield event

        return generate()


def _app(events=()):
    app = TestApplication()
    app.include_router(a2a_routes.a2a_router)
    app.dependency_overrides[require_authenticated_user] = lambda: AuthUser(
        username="operator_a", role="user"
    )
    app.dependency_overrides[require_workspace_id] = lambda: "workspace_demo"
    app.state.runtime_container = SimpleNamespace(
        manager=SimpleNamespace(repository=_Repository()),
        event_hub=_EventHub(list(events)),
    )
    return app


def test_agent_card_declares_no_user_interaction_support():
    response = TestClient(_app()).get("/.well-known/a2a/agent-card.json")

    assert response.status_code == 200
    assert response.json()["capabilities"]["supports_user_interaction"] is False


def test_get_task_returns_protocol_not_found(monkeypatch):
    service = _Service()

    async def resolve_service(*_args):
        return service

    monkeypatch.setattr(a2a_routes, "_service", resolve_service)

    response = TestClient(_app()).get("/a2a/tasks/missing_task")

    assert response.status_code == 404
    assert response.json()["task"]["state"] == "not_found"


def test_create_task_returns_external_id_and_canonical_metadata(monkeypatch):
    view = A2ATaskSnapshot(
        id="external_task_demo",
        session_id="session_demo",
        state=A2ATaskState.PENDING,
        metadata={"canonical_run_id": "agentic-canonical-demo"},
    )
    snapshot = SimpleNamespace(run_id="agentic-canonical-demo")
    service = _Service(view=view, snapshot=snapshot)

    async def resolve_service(*_args):
        return service

    monkeypatch.setattr(a2a_routes, "_service", resolve_service)

    response = TestClient(_app()).post(
        "/a2a/tasks",
        json={
            "message": "synthetic question",
            "session_id": "session_demo",
            "task_id": "external_task_demo",
        },
    )

    assert response.status_code == 202
    assert response.json()["task"]["id"] == "external_task_demo"
    assert response.json()["task"]["metadata"]["canonical_run_id"] == "agentic-canonical-demo"


def test_event_stream_projects_external_and_canonical_ids(monkeypatch):
    event = PublicEvent(
        event_id="event_demo",
        seq=1,
        run_id="agentic-canonical-demo",
        session_id="session_demo",
        turn_id=1,
        event_type="turn.finished",
        status="completed",
        source={"component": "synthetic"},
        payload={"status": "completed", "content": "synthetic answer"},
        created_at="2026-01-01T00:00:01Z",
    )
    view = A2ATaskSnapshot(
        id="external_task_demo",
        session_id="session_demo",
        state=A2ATaskState.COMPLETED,
        metadata={"canonical_run_id": "agentic-canonical-demo"},
    )
    snapshot = SimpleNamespace(run_id="agentic-canonical-demo")
    async def resolve_service(*_args):
        return _Service(view=view, snapshot=snapshot)

    monkeypatch.setattr(a2a_routes, "_service", resolve_service)

    response = TestClient(_app([event])).get(
        "/a2a/tasks/external_task_demo/events"
    )

    assert response.status_code == 200
    assert '"task_id": "external_task_demo"' in response.text
    assert '"canonical_run_id": "agentic-canonical-demo"' in response.text
