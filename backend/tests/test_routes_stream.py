import os

from flask import Flask
import pytest

from agentic.routes.workflows import workflows_bp
from agentic.services.auth import AuthManager
from agentic.stores import RunSlotManager, RunStatus
from agentic.stores.run_queue import TokenBucketRateLimiter
from agentic.stores.run_store import Run
from agentic.workspaces import create_workspace


@pytest.fixture(autouse=True)
def _restore_project_root_env(monkeypatch):
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "unit-model")
    previous = os.environ.get("PROJECT_ROOT")
    try:
        yield
    finally:
        if previous is None:
            os.environ.pop("PROJECT_ROOT", None)
        else:
            os.environ["PROJECT_ROOT"] = previous


def _build_test_client(tmp_path):
    os.environ["PROJECT_ROOT"] = str(tmp_path / ".onto-runtime")
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)

    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "admin123")
    assert user is not None
    user = auth_manager.change_password("admin", "admin123", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(workflows_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def test_run_stream_requires_session_id(tmp_path):
    client, headers = _build_test_client(tmp_path)

    response = client.post(
        "/agentic/runs/stream",
        json={"request": "hello"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "session_id is required"}


def test_run_stream_forwards_requested_run_id(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)
    captured = {}

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            captured.update(kwargs)
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "client-run-123:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sess-1",
                    "run_id": "client-run-123",
                    "turn_id": 1,
                    "type": "turn.started",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"user_preview": "hello"},
                },
            }

    monkeypatch.setattr("agentic.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/stream",
        json={
            "request": "hello",
            "session_id": "sess-1",
            "run_id": "client-run-123",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert captured["requested_run_id"] == "client-run-123"


def test_token_bucket_allows_burst_then_refills_by_rate():
    now = [0.0]
    limiter = TokenBucketRateLimiter(
        rate_per_second=50,
        burst=50,
        clock=lambda: now[0],
    )

    assert [limiter.try_acquire() for _ in range(50)] == [True] * 50
    assert limiter.try_acquire() is False

    now[0] = 0.01
    assert limiter.try_acquire() is False

    now[0] = 0.02
    assert limiter.try_acquire() is True


def test_run_sync_returns_429_when_global_tps_is_exhausted(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _RejectingLimiter:
        def try_acquire(self):
            return False

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()
        run_rate_limiter = _RejectingLimiter()

        def run(self, **kwargs):
            raise AssertionError("run should not start when global TPS is exhausted")

    monkeypatch.setattr("agentic.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session"},
        headers=headers,
    )

    assert response.status_code == 429
    assert response.get_json() == {
        "error": "请求过于频繁，请稍后重试",
        "limit": 50,
    }


def test_run_sync_returns_final_answer(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            assert kwargs["user_request"] == "hello"
            assert kwargs["session_id"] == "sync-session"
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-sync-1:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sync-session",
                    "run_id": "run-sync-1",
                    "turn_id": 1,
                    "type": "turn.started",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"user_preview": "hello"},
                },
            }
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-sync-1:2",
                    "seq": 2,
                    "ts": 1700000000001,
                    "session_id": "sync-session",
                    "run_id": "run-sync-1",
                    "turn_id": 1,
                    "type": "turn.finished",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"status": "success", "final_output": "done"},
                },
            }

    monkeypatch.setattr("agentic.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={
            "request": "hello",
            "session_id": "sync-session",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "status": "success",
        "final_answer": "done",
        "session_id": "sync-session",
        "run_id": "run-sync-1",
    }


def test_run_sync_requires_request(tmp_path):
    client, headers = _build_test_client(tmp_path)

    response = client.post(
        "/agentic/runs/sync",
        json={"session_id": "sync-session"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json() == {"error": "Request is required"}


def test_run_sync_queues_when_concurrent_limit_exceeded(monkeypatch, tmp_path):
    """With queueing, the request passes through (queue gate) instead of 429."""
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return [
                    Run(
                        run_id="run-busy-0",
                        user_request="busy",
                        status=RunStatus.RUNNING,
                        metadata={"user_id": "admin"},
                    ),
                    Run(
                        run_id="run-busy-1",
                        user_request="busy",
                        status=RunStatus.RUNNING,
                        metadata={"user_id": "admin"},
                    ),
                ]

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            assert kwargs["user_request"] == "hello"
            yield {
                "event": "agent.v2",
                "data": {
                    "protocol": "agent-stream.v2",
                    "event_id": "run-queued-1:1",
                    "seq": 1,
                    "ts": 1700000000000,
                    "session_id": "sync-session-queued",
                    "run_id": "run-queued-1",
                    "turn_id": 1,
                    "type": "turn.finished",
                    "source": {"node": "test", "component": "unit"},
                    "payload": {"status": "success", "final_output": "done"},
                },
            }

    monkeypatch.setattr("agentic.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session-queued"},
        headers=headers,
    )

    # Request passes through the queue gate (fresh semaphore)
    assert response.status_code == 200


def test_run_sync_returns_500_when_orchestrator_raises(monkeypatch, tmp_path):
    client, headers = _build_test_client(tmp_path)

    class _FakeOrchestrator:
        class _RunStore:
            @staticmethod
            def list_runs(limit=1000):
                del limit
                return []

        run_store = _RunStore()
        slot_manager = RunSlotManager()

        def run(self, **kwargs):
            del kwargs
            raise RuntimeError("boom")
            yield

    monkeypatch.setattr("agentic.routes.workflows._get_orchestrator", lambda: _FakeOrchestrator())

    response = client.post(
        "/agentic/runs/sync",
        json={"request": "hello", "session_id": "sync-session"},
        headers=headers,
    )

    assert response.status_code == 500
    assert response.get_json() == {
        "status": "error",
        "error": "boom",
        "final_answer": "",
        "session_id": "sync-session",
        "run_id": None,
    }
