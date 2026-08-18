"""Minimal route tests for A2A adapter endpoints."""

import asyncio
import queue

from flask import Flask

from agent.runtime.contracts import (
    PublicEvent,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
)
from agent.runtime.event_bus import BlockingSubscription
from api.protocols.a2a.models import A2ATaskSnapshot, A2ATaskState
from api.routes.a2a import a2a_bp
from api.auth.auth import AuthManager
from workspace.workspaces import create_workspace


def _call(coroutine):
    return asyncio.run(coroutine)


class _FakeAsyncManager:
    def __init__(self, snapshot=None, events=None):
        self.snapshot = snapshot
        self.events = list(events or [])
        self.unsubscribed = []

    async def get_run(self, task_id, scope):
        if (
            self.snapshot is None
            or self.snapshot.run_id != task_id
            or not scope.owns(self.snapshot)
        ):
            return None
        return self.snapshot

    async def subscribe(self, task_id, scope, after_seq=0):
        snapshot = await self.get_run(task_id, scope)
        if snapshot is None:
            raise KeyError(task_id)
        return BlockingSubscription(
            run_id=task_id,
            subscriber_id="subscriber_demo",
            queue=queue.Queue(),
            replay=[event for event in self.events if event.seq > after_seq],
            closed=True,
        )

    async def unsubscribe(self, subscription):
        self.unsubscribed.append(subscription.subscriber_id)


class _FakeTaskService:
    def __init__(self, snapshot=None, events=None):
        self.snapshot = snapshot or A2ATaskSnapshot(
            id="task-not-exists",
            session_id=None,
            state=A2ATaskState.NOT_FOUND,
            error="Task not found",
        )
        self.user_id = "admin"
        self.workspace_id = "a2a-test"
        runtime_snapshot = None
        if self.snapshot.state != A2ATaskState.NOT_FOUND:
            scope = RunScope(
                username=self.user_id,
                workspace_id=self.workspace_id,
                scopes=("agent:read",),
            )
            context = RuntimeInvocationContext(
                run_id=self.snapshot.id,
                session_id=self.snapshot.session_id,
                turn_id=1,
                workspace_id=self.workspace_id,
                username=self.user_id,
                query="synthetic question",
            )
            runtime_snapshot = RunSnapshot(
                run_id=self.snapshot.id,
                session_id=self.snapshot.session_id,
                turn_id=1,
                status=RunStatus.COMPLETED,
                created_at="2026-01-01T00:00:00Z",
                updated_at="2026-01-01T00:00:01Z",
                last_seq=max((event.seq for event in events or []), default=0),
                scope=scope,
                context=context,
            )
        self.async_manager = _FakeAsyncManager(runtime_snapshot, events=events)
        self.async_call = _call

    def get_task(self, task_id, session_id=None):
        del task_id, session_id
        return self.snapshot


def _build_test_client(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace(
        "A2A Test Workspace",
        workspace_id="a2a-test",
        owner=None,
        set_as_default=True,
    )

    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(a2a_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    headers = {"Authorization": "Bearer {}".format(token)}
    return client, headers


def test_agent_card_is_public(tmp_path, monkeypatch):
    client, _headers = _build_test_client(tmp_path, monkeypatch)

    resp = client.get("/.well-known/a2a/agent-card.json")
    assert resp.status_code == 200
    payload = resp.get_json()
    assert payload["name"] == "onto-agent"
    assert payload["capabilities"]["streaming"] is True
    assert "input_required" not in payload["capabilities"]
    assert "send_message" not in payload["endpoints"]


def test_get_task_returns_404_for_not_found(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    monkeypatch.setattr("api.routes.a2a._build_task_service", lambda: _FakeTaskService())

    resp = client.get("/a2a/tasks/task-not-exists", headers=headers)
    assert resp.status_code == 404
    payload = resp.get_json()
    assert payload["task"]["state"] == "not_found"


def test_replay_events_can_resolve_session_id_from_task_metadata(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    event = PublicEvent(
        schema_version="1.0",
        event_id="task-1:1",
        seq=1,
        run_id="task-1",
        session_id="session-1",
        turn_id=1,
        event_type="turn.finished",
        status="completed",
        source={"node": "runtime"},
        payload={"status": "completed", "content": "synthetic answer"},
        created_at="2026-01-01T00:00:01Z",
    )
    snapshot = A2ATaskSnapshot(
        id="task-1",
        session_id="session-1",
        state=A2ATaskState.COMPLETED,
    )

    monkeypatch.setattr(
        "api.routes.a2a._build_task_service",
        lambda: _FakeTaskService(snapshot=snapshot, events=[event]),
    )

    resp = client.get("/a2a/tasks/task-1/events", headers=headers)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "event: task.update" in body
    assert '"task_id": "task-1"' in body
    assert '"session_id": "session-1"' in body
