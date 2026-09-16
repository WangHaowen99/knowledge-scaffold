# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""认证对外契约测试。

锁定 docs/decisions/2026-07-09-decoupled-auth-architecture.md D4 冻结的对外契约：
前端（useAuth/UserManagement）、SDK（py/ts）和外部登录适配器都依赖这些精确形状。
任何 AuthBackend 实现（local/remote）都必须让本文件全绿；字段名、错误码、中文错误
消息一旦改变即破坏三类外部消费方。
"""

from types import SimpleNamespace

import pytest
from api.routes.auth import auth_router
from api.testing import TestApplication
from auth_test_backend import AuthManager
from backend_synthetic_credentials import (
    AUTH_SIGNING_SECRET,
    BOOTSTRAP_ADMIN_PASSWORD,
    INVALID_PASSWORD,
    MCP_TOKEN_SECRET,
    STRONG_PASSWORD,
    STRONG_PASSWORD_2,
    STRONG_PASSWORD_3,
    synthetic_secret,
)
from fastapi.testclient import TestClient
from internal_access import internal


def _build_app(auth_manager):
    app = TestApplication()
    app.include_router(auth_router)
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.state.services["APP_CONFIG"] = SimpleNamespace(auth_server_url="")
    return app


@pytest.fixture()
def auth_manager(tmp_path):
    return AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)


@pytest.fixture()
def app(auth_manager, tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("MEMORY_WORKSPACE_ROOT", str(runtime_root / "memory"))
    monkeypatch.setenv("MCP_TOKEN_STORE_PATH", str(runtime_root / "mcp-tokens.json"))
    monkeypatch.setenv("MCP_TOKEN_SECRET", MCP_TOKEN_SECRET)
    return _build_app(auth_manager)


@pytest.fixture()
def client(app):
    return TestClient(app)


def _login(client, username="admin", password=BOOTSTRAP_ADMIN_PASSWORD):
    return client.post("/auth/login", json={"username": username, "password": password})


def _admin_headers(client, auth_manager):
    del auth_manager
    response = _login(client)
    token = response.json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---- 登录契约 ----


def test_login_response_shape(client):
    response = _login(client)
    assert response.status_code == 200
    data = response.json()
    # 字段名必须是 token（不是 access_token）：外部适配器的 token_path 依赖它
    assert set(data.keys()) == {"token", "user"}
    assert isinstance(data["token"], str) and data["token"]
    assert data["user"] == {
        "username": "admin",
        "role": "admin",
        "password_change_required": False,
    }


def test_login_invalid_credentials(client):
    response = _login(client, password=INVALID_PASSWORD)
    assert response.status_code == 401
    assert response.json() == {"error": "Invalid credentials"}


# ---- 鉴权 HTTP 语义 ----


def test_missing_token_returns_401(client):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}


def test_garbage_token_returns_401(client):
    response = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {synthetic_secret('auth_contract-credential-01')}"}
    )
    assert response.status_code == 401
    assert response.json() == {"error": "Unauthorized"}


def test_cors_does_not_allow_cross_origin_credentials(client):
    origin = "http://localhost:6600"

    response = client.options(
        "/auth/login",
        headers={
            "Origin": origin,
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "Authorization,X-Workspace-Id",
        },
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == origin
    assert "access-control-allow-credentials" not in response.headers
    assert "Authorization" in response.headers["access-control-allow-headers"]
    assert "X-Workspace-Id" in response.headers["access-control-allow-headers"]


def test_app_configures_request_content_limit(app):
    from api.middleware import MAX_REQUEST_BODY_BYTES, RequestBodyLimitMiddleware
    from server_config import MAX_CONTENT_LENGTH

    assert MAX_REQUEST_BODY_BYTES == MAX_CONTENT_LENGTH
    assert any(middleware.cls is RequestBodyLimitMiddleware for middleware in app.user_middleware)


def test_non_admin_on_admin_route_returns_403(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    client.post("/auth/users", headers=headers, json={"username": "alice", "password": STRONG_PASSWORD})
    token = _login(client, "alice", STRONG_PASSWORD).json()["token"]
    auth_manager.change_password("alice", STRONG_PASSWORD, STRONG_PASSWORD_2)
    token = _login(client, "alice", STRONG_PASSWORD_2).json()["token"]
    response = client.get("/auth/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.json() == {"error": "Forbidden"}


# ---- 存量强制改密状态兼容 ----


def test_password_change_required_state_does_not_block_routes(client, auth_manager):
    users = internal(auth_manager).load_users()
    users["admin"]["must_change_password"] = True
    internal(auth_manager).save_users(users)
    token = _login(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    assert client.get("/auth/users", headers=headers).status_code == 200
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_change_password_contract_and_token_invalidation(client, auth_manager):
    token = _login(client).json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/auth/change-password",
        headers=headers,
        json={"old_password": BOOTSTRAP_ADMIN_PASSWORD, "new_password": STRONG_PASSWORD},
    )
    assert response.status_code == 200
    assert response.json() == {"success": True, "requires_relogin": True}
    # 旧 token 立即失效（AuthBackend 实现必须保持的失效语义）
    assert auth_manager.verify_token(token) is None
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_change_password_requires_both_fields(client):
    token = _login(client).json()["token"]
    response = client.put(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": BOOTSTRAP_ADMIN_PASSWORD},
    )
    assert response.status_code == 400
    assert response.json() == {"error": "原密码和新密码不能为空"}


# ---- 用户管理契约 ----


def test_create_user_password_policy_violation_codes(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    response = client.post("/auth/users", headers=headers, json={"username": "bob", "password": INVALID_PASSWORD})
    assert response.status_code == 400
    data = response.json()
    assert data["code"] == "PASSWORD_POLICY_FAILED"
    assert isinstance(data["violations"], list) and data["violations"]
    known_codes = {
        "surrounding_whitespace",
        "min_length",
        "max_length",
        "contains_username",
        "character_classes",
        "common_password",
        "same_as_old",
    }
    assert set(data["violations"]) <= known_codes
    assert isinstance(data["error"], str)


def test_user_crud_shapes(client, auth_manager):
    headers = _admin_headers(client, auth_manager)

    created = client.post("/auth/users", headers=headers, json={"username": "carol", "password": STRONG_PASSWORD})
    assert created.status_code == 201
    assert created.json() == {
        "user": {"username": "carol", "role": "user", "password_change_required": False},
    }

    listed = client.get("/auth/users", headers=headers)
    assert listed.status_code == 200
    users = listed.json()["users"]
    assert {"username": "carol", "role": "user", "password_change_required": False} in users

    deleted = client.delete("/auth/users/carol", headers=headers)
    assert deleted.status_code == 200
    assert deleted.json() == {"success": True}


def test_admin_reset_allows_direct_login_and_invalidates_tokens(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    client.post("/auth/users", headers=headers, json={"username": "dave", "password": STRONG_PASSWORD})
    dave_token = _login(client, "dave", STRONG_PASSWORD).json()["token"]

    reset = client.put("/auth/users/dave", headers=headers, json={"password": STRONG_PASSWORD_2})
    assert reset.status_code == 200
    assert reset.json()["user"]["password_change_required"] is False
    # 管理员重置必须使被重置用户的旧 token 立即失效
    assert auth_manager.verify_token(dave_token) is None


def test_delete_protections_chinese_messages(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    self_delete = client.delete("/auth/users/admin", headers=headers)
    assert self_delete.status_code == 400
    assert self_delete.json() == {"error": "不能删除自己"}

    whitespace_self_delete = client.delete(
        "/auth/users/%20admin%20",
        headers=headers,
    )
    assert whitespace_self_delete.status_code == 400
    assert whitespace_self_delete.json() == {"error": "不能删除自己"}
    assert auth_manager.get_user("admin") is not None

    reset_self = client.put("/auth/users/admin", headers=headers, json={"password": STRONG_PASSWORD_3})
    assert reset_self.status_code == 400
    assert reset_self.json() == {
        "error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口",
    }

    whitespace_reset_self = client.put(
        "/auth/users/%20admin%20",
        headers=headers,
        json={"password": STRONG_PASSWORD_3},
    )
    assert whitespace_reset_self.status_code == 400
    assert whitespace_reset_self.json() == {
        "error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口",
    }
