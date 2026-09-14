# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""HTTP contract tests for missing workspace requests."""

from __future__ import annotations

from api.auth.auth import AuthManager
from api.routes import initialize_runtime_dependencies, register_routers
from api.routes.auth import auth_router
from api.testing import TestApplication
from backend_synthetic_credentials import AUTH_SIGNING_SECRET, BOOTSTRAP_ADMIN_PASSWORD
from config import AgentConfig, AppConfig
from fastapi.testclient import TestClient
from llm import LLMRouter, configure_llm_router
from workspace.user_store_manager import UserStoreManager
from workspace.workspaces import create_workspace


def _build_client(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("CHAT_STORAGE_DIR", str(tmp_path / "chats"))
    monkeypatch.setenv("EXECUTION_STORAGE_DIR", str(tmp_path / "executions"))
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(runtime_root / "config" / "system_config.json"))
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.net/v1")
    monkeypatch.setenv("LLM_MODEL", "unit-model")

    config = AppConfig.from_env()
    agent_config = AgentConfig.from_env()
    llm_router = LLMRouter(config, agent_config=agent_config)
    configure_llm_router(llm_router)
    runtime = {
        "config": config,
        "agent_config": agent_config,
        "llm_router": llm_router,
        "auth_manager": AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET),
        "user_store_manager": UserStoreManager(
            config.chat_storage_dir,
            config.execution_storage_dir,
        ),
    }
    app = TestApplication()
    app.state.services.update(
        {
            "APP_CONFIG": config,
            "AGENT_CONFIG": agent_config,
            "AUTH_MANAGER": runtime["auth_manager"],
            "LLM_ROUTER": llm_router,
            "USER_STORE_MANAGER": runtime["user_store_manager"],
        }
    )
    app.include_router(auth_router)
    register_routers(app)
    initialize_runtime_dependencies(
        runtime["user_store_manager"],
        agent_config=agent_config,
    )
    client = TestClient(app)
    auth_manager = runtime["auth_manager"]
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert user is not None
    user = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, "RootStrong123!")
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
        ("POST", "/agentic/runs", missing_headers, {"request": "hi", "session_id": "s1"}),
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
        assert response.json() == expected_body, path
