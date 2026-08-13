"""HTTP contract tests for missing workspace requests."""

from __future__ import annotations

import app as app_module
from api.auth.auth import AuthManager, get_auth_manager
from config import AgentConfig, AppConfig
from llm import LLMRouter, configure_llm_router
from workspace.user_store_manager import UserStoreManager
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

    config = AppConfig.from_env()
    agent_config = AgentConfig.from_env()
    llm_router = LLMRouter(config, agent_config=agent_config)
    configure_llm_router(llm_router)
    runtime = {
        "config": config,
        "agent_config": agent_config,
        "llm_router": llm_router,
        "auth_manager": AuthManager(str(tmp_path / "users.json"), "test-secret"),
        "user_store_manager": UserStoreManager(
            config.chat_storage_dir,
            config.execution_storage_dir,
        ),
    }
    monkeypatch.setattr(
        app_module,
        "build_runtime_services",
        lambda previous_llm_router=None: runtime,
    )
    monkeypatch.setattr(app_module, "start_runtime_warmup", lambda **_kwargs: None)

    app = app_module.create_app()
    app.testing = False
    client = app.test_client()
    with app.app_context():
        auth_manager = get_auth_manager()
        user = auth_manager.authenticate("admin", "TestBootstrap#2026")
        assert user is not None
        user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")
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
        if method == "GET":
            response = client.get(path, headers=headers)
        elif method == "POST":
            response = client.post(path, headers=headers, json=json_body)
        else:
            raise AssertionError(f"Unsupported method in test case: {method}")
        assert response.status_code == 404, path
        assert response.get_json() == expected_body, path
