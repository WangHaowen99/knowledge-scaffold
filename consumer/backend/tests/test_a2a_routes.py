"""Minimal route tests for A2A adapter endpoints."""

from types import SimpleNamespace

from flask import Flask

from api.protocols.a2a.models import A2ATaskSnapshot, A2ATaskState
from api.routes.a2a import a2a_bp
from api.auth.auth import AuthManager
from persistence.run_store import RunStore
from workspace.workspaces import create_workspace


class _FakeTaskService:
    def __init__(self, snapshot=None):
        self.snapshot = snapshot or A2ATaskSnapshot(
            id="task-not-exists",
            session_id=None,
            state=A2ATaskState.NOT_FOUND,
            error="Task not found",
        )
        self.orchestrator = SimpleNamespace(run_store=RunStore())
        self.user_id = "admin"
        self.workspace_id = "a2a-test"
        if self.snapshot.state != A2ATaskState.NOT_FOUND:
            self.orchestrator.run_store.create_run(
                run_id=self.snapshot.id,
                user_request="test",
                metadata={
                    "user_id": self.user_id,
                    "workspace_id": self.workspace_id,
                    "session_id": self.snapshot.session_id,
                },
            )

    def get_task(self, task_id, session_id=None):
        del task_id, session_id
        return self.snapshot


class _FakeChatStore:
    def __init__(self, session=None):
        self.session = session

    def get_session(self, session_id):
        del session_id
        return self.session


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
    user = auth_manager.authenticate("admin", "admin123")
    assert user is not None
    user = auth_manager.change_password("admin", "admin123", "RootStrong123!")

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

    event = {
        "event": "agent.v2",
        "data": {
            "run_id": "task-1",
            "session_id": "session-1",
            "type": "turn.finished",
            "ts": 123,
            "source": {"node": "orchestrator"},
            "payload": {"status": "success"},
        },
    }
    session = SimpleNamespace(events=[event])
    snapshot = A2ATaskSnapshot(
        id="task-1",
        session_id="session-1",
        state=A2ATaskState.COMPLETED,
    )

    monkeypatch.setattr(
        "api.routes.a2a._build_task_service",
        lambda: _FakeTaskService(snapshot=snapshot),
    )
    monkeypatch.setattr(
        "api.routes.a2a._get_user_stores",
        lambda: (_FakeChatStore(session=session), None),
    )

    resp = client.get("/a2a/tasks/task-1/events", headers=headers)
    assert resp.status_code == 200
    body = resp.get_data(as_text=True)
    assert "event: task.update" in body
    assert '"task_id": "task-1"' in body
    assert '"session_id": "session-1"' in body
