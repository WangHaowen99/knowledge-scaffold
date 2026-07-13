"""HTTP contract tests for missing workspace requests."""

from __future__ import annotations

from app import create_app
from api.auth.auth import get_auth_manager
from workspace.workspaces import create_workspace


def _build_client(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("AUTH_USERS_FILE", str(tmp_path / "users.json"))
    monkeypatch.setenv("CHAT_STORAGE_DIR", str(tmp_path / "chats"))
    monkeypatch.setenv("EXECUTION_STORAGE_DIR", str(tmp_path / "executions"))
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(runtime_root / "config" / "system_config.json"))
    monkeypatch.setenv("AUTH_SECRET", "test-secret")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example/v1")
    monkeypatch.setenv("LLM_MODEL", "unit-model")

    app = create_app()
    app.testing = False
    client = app.test_client()
    with app.app_context():
        auth_manager = get_auth_manager()
        user = auth_manager.authenticate("admin", "admin123")
        assert user is not None
        user = auth_manager.change_password("admin", "admin123", "RootStrong123!")
        create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
        token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def test_missing_workspace_returns_consistent_404_contract(tmp_path, monkeypatch):
    client, auth_headers = _build_client(tmp_path, monkeypatch)
    missing_headers = {
        **auth_headers,
        "X-Workspace-Id": "missing-ws",
    }
    expected_body = {
        "code": "WORKSPACE_NOT_FOUND",
        "detail": "工作空间 'missing-ws' 不存在",
        "workspace_id": "missing-ws",
    }

    cases = [
        ("GET", "/agentic/workspaces/missing-ws", auth_headers, None),
        ("GET", "/agentic/workspaces/missing-ws/runtime-config", auth_headers, None),
        ("GET", "/agentic/workspaces/missing-ws/observability-config", auth_headers, None),
        ("GET", "/agentic/workspaces/missing-ws/llm-profiles", auth_headers, None),
        ("GET", "/agentic/sessions", missing_headers, None),
        ("POST", "/agentic/runs/stream", missing_headers, {"request": "hi", "session_id": "s1"}),
        ("GET", "/agentic/model-profiles", missing_headers, None),
        ("GET", "/agentic/ontologies/demo-ontology/data-source", missing_headers, None),
    ]

    for method, path, headers, json_body in cases:
        response = client.open(path, method=method, headers=headers, json=json_body)
        assert response.status_code == 404, path
        assert response.get_json() == expected_body, path
