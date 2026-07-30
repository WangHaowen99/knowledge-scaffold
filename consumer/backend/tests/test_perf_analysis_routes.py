"""HTTP route tests for local observability performance analysis."""

from __future__ import annotations

from flask import Flask

from api.routes.performance import performance_bp
from api.auth.auth import AuthManager


class _FakeChatStore:
    def __init__(self, session_ids=None):
        self._session_ids = set(session_ids or ())

    def get_session(self, session_id):
        return {"id": session_id} if session_id in self._session_ids else None


class _FakeExecutionStore:
    def __init__(self, session_runs=None):
        self._session_runs = dict(session_runs or {})

    def get_session_history(self, session_id):
        runs = self._session_runs.get(session_id)
        if runs is None:
            return None
        return {"turns": [{"run_id": run_id} for run_id in runs]}


def _build_test_client(tmp_path, *, role="admin"):
    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    if role == "admin":
        user = auth_manager.authenticate("admin", "admin123")
        assert user is not None
        user = auth_manager.change_password("admin", "admin123", "RootStrong123!")
    else:
        user = auth_manager.create_user("perf-user", "PerfStrong123!", role=role)
        user = auth_manager.change_password("perf-user", "PerfStrong123!", "ChangedStrong123!")
    assert user is not None

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(performance_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def test_session_performance_requires_existing_current_workspace_session(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, role="user")
    monkeypatch.setattr(
        "api.routes.performance.get_request_stores",
        lambda: (_FakeChatStore(), object()),
    )

    response = client.get("/agentic/sessions/missing/performance", headers=headers)

    assert response.status_code == 404


def test_session_performance_returns_current_session_analysis(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, role="user")
    captured = {}
    monkeypatch.setattr(
        "api.routes.performance.get_request_stores",
        lambda: (_FakeChatStore({"sess-a"}), object()),
    )
    monkeypatch.setattr(
        "api.routes.performance.analyze_session",
        lambda session_id, **kwargs: captured.update(kwargs) or {"session_id": session_id, "summary": {"run_count": 1}},
    )
    monkeypatch.setattr("api.routes.performance.get_request_workspace_id", lambda: "ws-a")
    monkeypatch.setattr(
        "utils.perf_analysis.service.current_epoch_ms",
        lambda: 1_000_000_000,
    )

    response = client.get("/agentic/sessions/sess-a/performance", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["summary"]["run_count"] == 1
    assert captured["end_ms"] == 1_000_000_000
    assert captured["start_ms"] == 1_000_000_000 - (7 * 24 * 60 * 60 * 1000)
    assert captured["workspace_id"] == "ws-a"


def test_session_performance_availability_uses_lightweight_probe(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, role="user")
    captured = {}
    monkeypatch.setattr(
        "api.routes.performance.get_request_stores",
        lambda: (_FakeChatStore({"sess-a"}), object()),
    )
    monkeypatch.setattr(
        "api.routes.performance.check_session_availability",
        lambda session_id, **kwargs: captured.update(kwargs)
        or {"session_id": session_id, "available": True},
    )
    monkeypatch.setattr("api.routes.performance.get_request_workspace_id", lambda: "ws-a")
    monkeypatch.setattr(
        "utils.perf_analysis.service.current_epoch_ms",
        lambda: 1_000_000_000,
    )

    response = client.get("/agentic/sessions/sess-a/performance/availability", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["available"] is True
    assert captured["end_ms"] == 1_000_000_000
    assert captured["start_ms"] == 1_000_000_000 - (7 * 24 * 60 * 60 * 1000)
    assert captured["workspace_id"] == "ws-a"


def test_session_performance_availability_requires_existing_session(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, role="user")
    monkeypatch.setattr(
        "api.routes.performance.get_request_stores",
        lambda: (_FakeChatStore(), object()),
    )

    response = client.get("/agentic/sessions/missing/performance/availability", headers=headers)

    assert response.status_code == 404


def test_user_run_detail_requires_current_session_ownership(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, role="user")
    captured = {}
    monkeypatch.setattr(
        "api.routes.performance.get_request_stores",
        lambda: (
            _FakeChatStore({"sess-a"}),
            _FakeExecutionStore({"sess-a": {"run-a"}}),
        ),
    )
    monkeypatch.setattr(
        "api.routes.performance.analyze_run_detail",
        lambda run_id, **kwargs: captured.update(kwargs) or {"run": {"run_id": run_id}, "timeline": [{"event": "run.finished"}]},
    )
    monkeypatch.setattr("api.routes.performance.get_request_workspace_id", lambda: "ws-a")
    monkeypatch.setattr(
        "utils.perf_analysis.service.current_epoch_ms",
        lambda: 1_000_000_000,
    )

    missing_owner = client.get("/agentic/performance/runs/run-a", headers=headers)
    owned = client.get(
        "/agentic/performance/runs/run-a?session_id=sess-a",
        headers=headers,
    )

    assert missing_owner.status_code == 404
    assert owned.status_code == 200
    assert owned.get_json()["run"]["run_id"] == "run-a"
    assert captured["start_ms"] == 1_000_000_000 - (7 * 24 * 60 * 60 * 1000)
    assert captured["workspace_id"] == "ws-a"


def test_modeling_service_can_read_run_trace_without_user_access_token(tmp_path, monkeypatch):
    client, _headers = _build_test_client(tmp_path, role="user")
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)
    captured = {}
    monkeypatch.setattr(
        "api.routes.performance.analyze_run_detail",
        lambda run_id, **kwargs: captured.update(kwargs)
        or {"run": {"run_id": run_id}, "timeline": [{"event": "run.finished"}], "tools": []},
    )
    monkeypatch.setattr("api.routes.performance.get_request_workspace_id", lambda: "mw-shadow-project-1")

    response = client.get(
        "/agentic/internal/modeling/performance/runs/run-a?session_id=session-a",
        headers={"Authorization": "Bearer {}".format("s" * 32), "X-Workspace-Id": "mw-shadow-project-1"},
    )

    assert response.status_code == 200
    assert captured["workspace_id"] == "mw-shadow-project-1"


def test_modeling_trace_endpoint_rejects_an_invalid_service_token(tmp_path, monkeypatch):
    client, _headers = _build_test_client(tmp_path, role="user")
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)

    response = client.get(
        "/agentic/internal/modeling/performance/runs/run-a",
        headers={"Authorization": "Bearer invalid", "X-Workspace-Id": "mw-shadow-project-1"},
    )

    assert response.status_code == 403


def test_local_observability_overview_requires_admin(tmp_path):
    client, headers = _build_test_client(tmp_path, role="user")

    response = client.get("/api/local-observability/overview", headers=headers)

    assert response.status_code == 403


def test_local_observability_overview_passes_workspace_filter(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path)
    captured = {}
    monkeypatch.setattr(
        "api.routes.performance.analyze_global_overview",
        lambda **kwargs: captured.update(kwargs) or {"summary": {"run_count": 0}},
    )

    response = client.get(
        "/api/local-observability/overview?workspace_id=ws-a",
        headers=headers,
    )

    assert response.status_code == 200
    assert captured["workspace_id"] == "ws-a"
