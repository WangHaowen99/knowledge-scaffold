"""认证对外契约测试。

锁定 docs/decisions/2026-07-09-decoupled-auth-architecture.md D4 冻结的对外契约：
前端（useAuth/UserManagement）、SDK（py/ts）和外部登录适配器都依赖这些精确形状。
任何 AuthBackend 实现（local/remote）都必须让本文件全绿；字段名、错误码、中文错误
消息一旦改变即破坏三类外部消费方。
"""

from types import SimpleNamespace

import pytest
from flask import jsonify

from api.auth.auth import AuthManager, require_auth


def _build_app(auth_manager):
    import app as app_module

    runtime = {
        "config": SimpleNamespace(),
        "agent_config": SimpleNamespace(),
        "llm_router": None,
        "auth_manager": auth_manager,
        "user_store_manager": SimpleNamespace(),
    }
    original_build_runtime = app_module.build_runtime_services
    original_register_routes = app_module.register_routes
    try:
        app_module.build_runtime_services = lambda previous_llm_router=None: runtime
        app_module.register_routes = lambda *args, **kwargs: None
        app = app_module.create_app()
    finally:
        app_module.build_runtime_services = original_build_runtime
        app_module.register_routes = original_register_routes
    return app


@pytest.fixture()
def auth_manager(tmp_path):
    return AuthManager(str(tmp_path / "users.json"), "test-secret")


@pytest.fixture()
def app(auth_manager, tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("MEMORY_WORKSPACE_ROOT", str(runtime_root / "memory"))
    monkeypatch.setenv("MCP_TOKEN_STORE_PATH", str(runtime_root / "mcp-tokens.json"))
    monkeypatch.setenv("MCP_TOKEN_SECRET", "test-mcp-secret")
    return _build_app(auth_manager)


@pytest.fixture()
def client(app):
    return app.test_client()


def _login(client, username="admin", password="TestBootstrap#2026"):
    return client.post("/auth/login", json={"username": username, "password": password})


def _admin_headers(client, auth_manager):
    # bootstrap 的 admin 处于强制改密状态，先改密再登录，得到无限制 token
    auth_manager.change_password("admin", "TestBootstrap#2026", "ContractTest#2026")
    response = _login(client, password="ContractTest#2026")
    token = response.get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


# ---- 登录契约 ----

def test_login_response_shape(client):
    response = _login(client)
    assert response.status_code == 200
    data = response.get_json()
    # 字段名必须是 token（不是 access_token）：外部适配器的 token_path 依赖它
    assert set(data.keys()) == {"token", "user"}
    assert isinstance(data["token"], str) and data["token"]
    assert data["user"] == {
        "username": "admin",
        "role": "admin",
        "password_change_required": True,
    }


def test_login_invalid_credentials(client):
    response = _login(client, password="wrong-password")
    assert response.status_code == 401
    assert response.get_json() == {"error": "Invalid credentials"}


# ---- 装饰器 HTTP 语义 ----

def test_missing_token_returns_401(client):
    response = client.get("/auth/me")
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_garbage_token_returns_401(client):
    response = client.get("/auth/me", headers={"Authorization": "Bearer not-a-token"})
    assert response.status_code == 401
    assert response.get_json() == {"error": "Unauthorized"}


def test_options_preflight_returns_204(app, client):
    @app.route("/contract-probe")
    @require_auth()
    def contract_probe():
        return jsonify({"ok": True})

    response = client.options("/contract-probe")
    # flask-cors 接管 preflight 时返回 200；require_auth wrapper 自身返回 204。
    # 契约点：OPTIONS 预检绝不能被鉴权拦成 401/403。
    assert response.status_code in (200, 204)


def test_app_configures_request_content_limit(app):
    from server_config import MAX_CONTENT_LENGTH

    assert app.config["MAX_CONTENT_LENGTH"] == MAX_CONTENT_LENGTH


def test_non_admin_on_admin_route_returns_403(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    client.post("/auth/users", headers=headers, json={"username": "alice", "password": "Zq7#Wmx2Lp8!"})
    token = _login(client, "alice", "Zq7#Wmx2Lp8!").get_json()["token"]
    auth_manager.change_password("alice", "Zq7#Wmx2Lp8!", "Zq7#Wmx2Lp9!")
    token = _login(client, "alice", "Zq7#Wmx2Lp9!").get_json()["token"]
    response = client.get("/auth/users", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 403
    assert response.get_json() == {"error": "Forbidden"}


# ---- 强制改密流程 ----

def test_password_change_required_blocks_and_whitelists(client):
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    blocked = client.get("/auth/users", headers=headers)
    assert blocked.status_code == 403
    assert blocked.get_json() == {
        "error": "Password change required",
        "code": "PASSWORD_CHANGE_REQUIRED",
    }
    # 白名单：/auth/me 可访问
    assert client.get("/auth/me", headers=headers).status_code == 200


def test_change_password_contract_and_token_invalidation(client, auth_manager):
    token = _login(client).get_json()["token"]
    headers = {"Authorization": f"Bearer {token}"}

    response = client.put(
        "/auth/change-password",
        headers=headers,
        json={"old_password": "TestBootstrap#2026", "new_password": "ContractTest#2026"},
    )
    assert response.status_code == 200
    assert response.get_json() == {"success": True, "requires_relogin": True}
    # 旧 token 立即失效（AuthBackend 实现必须保持的失效语义）
    assert auth_manager.verify_token(token) is None
    assert client.get("/auth/me", headers=headers).status_code == 401


def test_change_password_requires_both_fields(client):
    token = _login(client).get_json()["token"]
    response = client.put(
        "/auth/change-password",
        headers={"Authorization": f"Bearer {token}"},
        json={"old_password": "TestBootstrap#2026"},
    )
    assert response.status_code == 400
    assert response.get_json() == {"error": "原密码和新密码不能为空"}


# ---- 用户管理契约 ----

def test_create_user_password_policy_violation_codes(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    response = client.post("/auth/users", headers=headers, json={"username": "bob", "password": "123456"})
    assert response.status_code == 400
    data = response.get_json()
    assert data["code"] == "PASSWORD_POLICY_FAILED"
    assert isinstance(data["violations"], list) and data["violations"]
    known_codes = {
        "surrounding_whitespace", "min_length", "max_length",
        "contains_username", "character_classes", "common_password", "same_as_old",
    }
    assert set(data["violations"]) <= known_codes
    assert isinstance(data["error"], str)


def test_user_crud_shapes(client, auth_manager):
    headers = _admin_headers(client, auth_manager)

    created = client.post("/auth/users", headers=headers, json={"username": "carol", "password": "Kv3$Ntb6Rd5?"})
    assert created.status_code == 201
    assert created.get_json() == {
        "user": {"username": "carol", "role": "user", "password_change_required": True},
    }

    listed = client.get("/auth/users", headers=headers)
    assert listed.status_code == 200
    users = listed.get_json()["users"]
    assert {"username": "carol", "role": "user", "password_change_required": True} in users

    deleted = client.delete("/auth/users/carol", headers=headers)
    assert deleted.status_code == 200
    assert deleted.get_json() == {"success": True}


def test_admin_reset_forces_change_and_invalidates_tokens(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    client.post("/auth/users", headers=headers, json={"username": "dave", "password": "Hf4%Jsc8Qw1!"})
    dave_token = _login(client, "dave", "Hf4%Jsc8Qw1!").get_json()["token"]

    reset = client.put("/auth/users/dave", headers=headers, json={"password": "Hf4%Jsc8Qw2!"})
    assert reset.status_code == 200
    assert reset.get_json()["user"]["password_change_required"] is True
    # 管理员重置必须使被重置用户的旧 token 立即失效
    assert auth_manager.verify_token(dave_token) is None


def test_delete_protections_chinese_messages(client, auth_manager):
    headers = _admin_headers(client, auth_manager)
    self_delete = client.delete("/auth/users/admin", headers=headers)
    assert self_delete.status_code == 400
    assert self_delete.get_json() == {"error": "不能删除自己"}

    whitespace_self_delete = client.delete(
        "/auth/users/%20admin%20",
        headers=headers,
    )
    assert whitespace_self_delete.status_code == 400
    assert whitespace_self_delete.get_json() == {"error": "不能删除自己"}
    assert auth_manager.get_user("admin") is not None

    reset_self = client.put("/auth/users/admin", headers=headers, json={"password": "SelfReset#123"})
    assert reset_self.status_code == 400
    assert reset_self.get_json() == {
        "error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口",
    }

    whitespace_reset_self = client.put(
        "/auth/users/%20admin%20",
        headers=headers,
        json={"password": "SelfReset#123"},
    )
    assert whitespace_reset_self.status_code == 400
    assert whitespace_reset_self.get_json() == {
        "error": "不能通过用户管理重置当前登录用户密码，请使用修改密码入口",
    }
