# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""RemoteAuthBackend 集成测试：进程内起真实 auth server（真 HTTP，无 mock）。

覆盖施工步骤 4（docs/decisions/2026-07-09-decoupled-auth-architecture.md 方案 a）：
密码登录、JWKS 本地验签、refresh 轮换、改密后的吊销语义（D2：旧 refresh 立即失效、
旧 access 在 ≤TTL 窗口内仍可验签——本文件显式固化该放宽语义）、
用户管理 Admin API 翻译、故障路径、以及 build_runtime_services 的 remote 装配。
"""

from __future__ import annotations

import socket
import threading
import importlib
import importlib.util
import sys
from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace

import pytest
from api import workspace_acl_service
from api.auth.auth import PasswordPolicyError
from api.auth.remote_auth import RemoteAuthBackend
from api.routes.auth import auth_router
from api.testing import TestApplication
from api.workspace_acl_service import WorkspaceIdentityNotFoundError
from auth_test_backend import AuthManager
from backend_synthetic_credentials import (
    ACCESS_KEY_SECRET,
    ADMIN_API_TOKEN,
    API_KEY,
    AUTH_SIGNING_SECRET,
    BOOTSTRAP_ADMIN_PASSWORD,
    INVALID_PASSWORD,
    STRONG_PASSWORD,
    STRONG_PASSWORD_2,
    STRONG_PASSWORD_3,
)
from fastapi.testclient import TestClient
from werkzeug.serving import make_server
from internal_access import internal

# 测试专用例外：决策 D4 禁止 consumer 与 auth-server 之间任何生产代码 Python import
# （唯一接口是 HTTP）。集成测试为了进程内起真实 server 而 import auth_server，
# 属于测试基础设施，不构成生产依赖；生产代码严禁效仿。
AUTH_SERVER_DIR = Path(__file__).resolve().parents[3] / "auth-server"
_AUTH_SERVER_PACKAGE = AUTH_SERVER_DIR / "auth_server"
_AUTH_SERVER_SPEC = importlib.util.spec_from_file_location(
    "auth_server",
    _AUTH_SERVER_PACKAGE / "__init__.py",
    submodule_search_locations=[str(_AUTH_SERVER_PACKAGE)],
)
if _AUTH_SERVER_SPEC is None or _AUTH_SERVER_SPEC.loader is None:
    raise ImportError("auth_server package cannot be loaded")
_AUTH_SERVER_MODULE = importlib.util.module_from_spec(_AUTH_SERVER_SPEC)
sys.modules.setdefault("auth_server", _AUTH_SERVER_MODULE)
_AUTH_SERVER_SPEC.loader.exec_module(_AUTH_SERVER_MODULE)
create_auth_server_app = _AUTH_SERVER_MODULE.create_app
AuthServerConfig = importlib.import_module("auth_server.config").AuthServerConfig


@pytest.fixture(autouse=True)
def _bypass_local_proxy(monkeypatch):
    """开发机可能配置系统 HTTP 代理（会拦截 127.0.0.1）：测试流量强制直连。"""
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def auth_server(tmp_path):
    """随机端口起真实 auth server：开启注册（一步建号，无邮件）、admin token、tmp 数据目录。"""
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    config = AuthServerConfig(
        data_dir=tmp_path / "auth-server-data",
        # issuer 与 RemoteAuthBackend 的默认约定（issuer 留空 = base_url）对齐
        issuer=base_url,
        audience="knowledge-scaffold",
        access_ttl_seconds=60,  # server 允许的最小值；测试内旧 access 始终处于 TTL 窗口
        admin_token=ADMIN_API_TOKEN,
        bootstrap_admin_password=BOOTSTRAP_ADMIN_PASSWORD,
        enable_registration=True,
    )
    app = create_auth_server_app(config)
    server = make_server("127.0.0.1", port, app, threaded=True)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield SimpleNamespace(base_url=base_url)
    finally:
        server.shutdown()
        thread.join(timeout=5)


@pytest.fixture()
def backend(auth_server):
    return RemoteAuthBackend(auth_server.base_url, admin_token=ADMIN_API_TOKEN)


def _build_consumer_app(auth_manager):
    """与 test_auth_contract.py 相同的装配打桩方式，只替换 auth_manager。"""
    app = TestApplication()
    app.state.services.update(
        {
            "APP_CONFIG": SimpleNamespace(auth_server_url=""),
            "AUTH_MANAGER": auth_manager,
        }
    )
    app.include_router(auth_router)
    return app


class _StubResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class _StubSession:
    def __init__(self, token_payload=None, jwks_payload=None):
        self.token_payload = token_payload or {}
        self.jwks_payload = jwks_payload or {"keys": []}
        self.jwks_get_count = 0

    def post(self, *_args, **_kwargs):
        return _StubResponse(200, self.token_payload)

    def get(self, *_args, **_kwargs):
        self.jwks_get_count += 1
        return _StubResponse(200, self.jwks_payload)


# ---------------------------------------------------------- 登录 / 验签 / 刷新


def test_password_login_issue_and_verify(backend):
    result = backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert result is not None
    user = result.user
    assert user.username == "admin"
    assert user.role == "admin"
    assert user.password_change_required is False

    token = result.access_token
    assert isinstance(token, str) and token.count(".") == 2  # RS256 JWT
    assert result.refresh_token
    assert result.expires_in == 60

    # 本地验签（JWKS）同一条代码路径
    verified = backend.verify_token(token)
    assert verified is not None
    assert verified.username == "admin"
    assert verified.role == "admin"
    assert verified.password_change_required is False


def test_login_rejects_bad_credentials(backend):
    assert backend.login("admin", INVALID_PASSWORD) is None
    assert backend.login("no-such-user", "whatever") is None
    assert backend.login("", "") is None


def test_concurrent_same_username_logins_keep_token_pairs_isolated(backend):
    """同用户名并发登录必须各自拿到本次请求的 token 对，不能经共享缓存串号。"""
    barrier = threading.Barrier(3)

    def login_once():
        barrier.wait(timeout=5)
        return backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(login_once) for _ in range(2)]
        barrier.wait(timeout=5)
        results = [future.result(timeout=10) for future in futures]

    assert all(result is not None for result in results)
    assert len({result.refresh_token for result in results}) == 2
    for result in results:
        assert result.refresh_token
        verified = backend.verify_token(result.access_token)
        assert verified is not None and verified.username == "admin"


def test_token_endpoint_200_requires_complete_verified_token_pair(monkeypatch):
    incomplete = RemoteAuthBackend(
        "https://auth.example.net",
        session=_StubSession(token_payload={"refresh_token": ACCESS_KEY_SECRET, "expires_in": 60}),
    )
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        incomplete.login("admin", INVALID_PASSWORD)

    malformed = RemoteAuthBackend(
        "https://auth.example.net",
        session=_StubSession(
            token_payload={
                "access_token": API_KEY,
                "refresh_token": ACCESS_KEY_SECRET,
                "expires_in": 60,
            }
        ),
    )
    monkeypatch.setattr(malformed, "_verify_access_token_locally", lambda _token: None)
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        malformed.login("admin", INVALID_PASSWORD)
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        malformed.refresh_tokens(ACCESS_KEY_SECRET)


def test_jwks_refresh_throttles_unknown_kids_and_drops_removed_key(monkeypatch):
    session = _StubSession(jwks_payload={"keys": []})
    backend = RemoteAuthBackend("https://auth.example.net", session=session)

    assert internal(backend).signing_key("forged-a") is None
    assert internal(backend).signing_key("forged-b") is None
    assert session.jwks_get_count == 1

    old_key = object()
    with internal(backend).jwks_lock:
        internal(backend).jwks_keys = {"old": old_key}
        internal(backend).jwks_fetched_at = 0
        internal(backend).jwks_last_attempt_at = 0

    def concurrent_successful_rotation():
        with internal(backend).jwks_lock:
            internal(backend).jwks_keys = {}
            internal(backend).jwks_fetched_at = 1
        # 模拟另一线程已成功刷新，本线程因节流未再次请求。
        return False

    monkeypatch.setattr(backend, "_refresh_jwks", concurrent_successful_rotation)
    assert internal(backend).signing_key("old") is None


def test_cold_jwks_failure_reports_throttling_and_recovers(auth_server, monkeypatch, caplog):
    import requests
    from api.auth import remote_auth as remote_auth_module

    # Keep the refresh interval deterministic without changing JWT/server clocks.
    monkeypatch.setattr(remote_auth_module, "time", SimpleNamespace(time=lambda: 1000.0))
    backend = RemoteAuthBackend(auth_server.base_url)
    real_get = internal(backend).session.get
    attempts = []

    def fail_first_get(*args, **kwargs):
        attempts.append(1)
        if len(attempts) == 1:
            raise requests.ReadTimeout("synthetic JWKS timeout")
        return real_get(*args, **kwargs)

    monkeypatch.setattr(internal(backend).session, "get", fail_first_get)
    client = TestClient(_build_consumer_app(backend))
    credentials = {"username": "admin", "password": BOOTSTRAP_ADMIN_PASSWORD}
    with client:
        assert client.post("/auth/login", json=credentials).status_code == 503
        assert client.post("/auth/login", json=credentials).status_code == 503
        assert len(attempts) == 1
        # Advance only the refresh gate; JWT validity still uses the real clock.
        with internal(backend).jwks_lock:
            internal(backend).jwks_last_attempt_at = 0
        assert client.post("/auth/login", json=credentials).status_code == 200
    records = [record for record in caplog.records if record.name == "auth.diagnostics"]
    assert [(record.auth_stage, record.auth_reason) for record in records] == [
        ("jwks", "request_failed"), ("jwks", "refresh_throttled"),
    ]
    assert records[0].auth_attempt_id != records[1].auth_attempt_id
    assert len(attempts) == 2


def test_verify_token_rejects_non_access_tokens(backend):
    result = backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert result is not None and result.refresh_token

    # refresh token 不能当访问凭据（typ != access）
    assert backend.verify_token(result.refresh_token) is None
    # 篡改签名
    assert backend.verify_token(result.access_token[:-3] + "abc") is None
    assert backend.verify_token(API_KEY) is None
    assert backend.verify_token("") is None


def test_refresh_tokens_rotates_pair(backend):
    result = backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert result is not None and result.refresh_token

    renewed = backend.refresh_tokens(result.refresh_token)
    assert renewed is not None
    assert renewed["access_token"] and renewed["refresh_token"]
    assert renewed["expires_in"] == 60
    verified = backend.verify_token(renewed["access_token"])
    assert verified is not None and verified.username == "admin"

    assert backend.refresh_tokens(API_KEY) is None
    assert backend.refresh_tokens("") is None


def test_change_password_revokes_refresh_but_old_access_survives_ttl(backend):
    old_login = backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert old_login is not None and old_login.refresh_token

    changed = backend.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, STRONG_PASSWORD_3)
    assert changed.username == "admin"
    assert changed.password_change_required is False

    # D2 固化语义：改密吊销全部 refresh token（等价 password_version+1）……
    assert backend.refresh_tokens(old_login.refresh_token) is None
    # ……但 verify 是 JWKS 本地验签、不回查 ver：旧 access 在 ≤TTL 窗口内仍可验签。
    # 这是决策 D2 显式放宽的吊销窗口，不是缺陷；此断言防止后人“顺手修复”改回逐请求回查。
    still_valid = backend.verify_token(old_login.access_token)
    assert still_valid is not None and still_valid.username == "admin"

    assert backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD) is None
    renewed = backend.login("admin", STRONG_PASSWORD_3)
    assert renewed is not None and renewed.user.password_change_required is False


# ---------------------------------------------------------------- 用户管理


def test_user_management_via_admin_api(backend):
    created = backend.create_user("alice", STRONG_PASSWORD)
    assert created.username == "alice"
    assert created.role == "user"
    assert created.password_change_required is False

    users = backend.list_users()
    assert {"username": "alice", "role": "user", "password_change_required": False} in users

    fetched = backend.get_user("alice")
    assert fetched is not None and fetched.role == "user"
    assert backend.get_user("nobody") is None

    updated = backend.update_user("alice", password=STRONG_PASSWORD_2)
    assert updated.password_change_required is False

    changed = backend.change_password("alice", STRONG_PASSWORD_2, STRONG_PASSWORD_3)
    assert changed.password_change_required is False

    backend.delete_user("alice")
    assert backend.get_user("alice") is None


def test_admin_api_error_translation(backend):
    # 密码策略违规 → PasswordPolicyError(violations)，routes/auth.py 零改动翻译
    with pytest.raises(PasswordPolicyError) as policy_exc:
        backend.create_user("bob", "123456")
    assert "min_length" in policy_exc.value.violations

    backend.create_user("carol", STRONG_PASSWORD)
    with pytest.raises(ValueError, match="已存在"):
        backend.create_user("carol", STRONG_PASSWORD)
    with pytest.raises(ValueError, match="不存在"):
        backend.delete_user("nobody")
    with pytest.raises(ValueError, match="原密码错误"):
        backend.change_password("carol", "wrong-old", STRONG_PASSWORD_2)


def test_workspace_acl_identity_lookup_accepts_real_remote_directory_contract(
    backend,
    monkeypatch,
):
    backend.create_user("alice", STRONG_PASSWORD)
    monkeypatch.setattr(
        workspace_acl_service,
        "identity_reference_guard",
        lambda usernames: nullcontext(tuple(usernames)),
    )

    assert workspace_acl_service.validate_workspace_member(backend, "alice") == "alice"
    with pytest.raises(WorkspaceIdentityNotFoundError) as exc_info:
        workspace_acl_service.validate_workspace_member(backend, "missing-user")
    assert exc_info.value.code == "WORKSPACE_MEMBER_NOT_FOUND"


def test_admin_methods_require_admin_token(auth_server):
    backend = RemoteAuthBackend(auth_server.base_url)  # 未配置 admin token
    expected = "未配置 AUTH_SERVER_ADMIN_TOKEN，无法管理用户"
    with pytest.raises(ValueError, match=expected):
        backend.list_users()
    with pytest.raises(ValueError, match=expected):
        backend.create_user("x", STRONG_PASSWORD)


# ---------------------------------------------------------------- 故障路径


def test_auth_server_down_raises_unavailable(tmp_path):
    dead_url = f"http://127.0.0.1:{_free_port()}"  # 无人监听的端口
    backend = RemoteAuthBackend(dead_url, admin_token=ADMIN_API_TOKEN)

    with pytest.raises(ValueError, match="认证服务暂不可用"):
        backend.login("admin", BOOTSTRAP_ADMIN_PASSWORD)
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        backend.refresh_tokens(ACCESS_KEY_SECRET)
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        backend.list_users()
    # verify_token 不抛错：拿不到 JWKS → None（require_auth 走 401）
    assert backend.verify_token("x.y.z") is None


# ---------------------------------------------------------------- 路由集成


def test_login_and_refresh_routes_remote_mode(auth_server):
    backend = RemoteAuthBackend(auth_server.base_url, admin_token=ADMIN_API_TOKEN)
    client = TestClient(_build_consumer_app(backend))

    login = client.post("/auth/login", json={"username": "admin", "password": BOOTSTRAP_ADMIN_PASSWORD})
    assert login.status_code == 200
    data = login.json()
    # access token remains in the response; refresh token is HttpOnly cookie only.
    assert set(data.keys()) == {"token", "user", "expires_in"}
    assert data["user"] == {
        "username": "admin",
        "role": "admin",
        "password_change_required": False,
    }
    assert data["expires_in"] == 60
    set_cookie = login.headers["set-cookie"]
    assert "onto_refresh_token=" in set_cookie
    assert "HttpOnly" in set_cookie
    assert "SameSite=lax" in set_cookie
    assert " Secure" not in set_cookie

    # 拿到的 access token 能直接访问受保护接口。
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.status_code == 200
    assert me.json()["user"]["username"] == "admin"

    refreshed = client.post("/auth/refresh")
    assert refreshed.status_code == 200
    refreshed_data = refreshed.json()
    assert set(refreshed_data.keys()) == {"token", "expires_in"}
    me2 = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {refreshed_data['token']}"}
    )
    assert me2.status_code == 200

    logout = client.post("/auth/logout")
    assert logout.status_code == 200
    assert logout.json() == {"success": True}
    revoked = client.post("/auth/refresh")
    assert revoked.status_code == 401
    assert revoked.json() == {"error": "Unauthorized"}

    missing_logout_token = client.post("/auth/logout")
    assert missing_logout_token.status_code == 200

    # 上游拒绝（无效 refresh）→ 401，前端据此登出
    rejected = client.post(
        "/auth/refresh", headers={"Cookie": "onto_refresh_token=invalid-refresh"}
    )
    assert rejected.status_code == 401
    assert rejected.json() == {"error": "Unauthorized"}


def test_login_wrong_password_remote_mode_contract(auth_server):
    backend = RemoteAuthBackend(auth_server.base_url)
    client = TestClient(_build_consumer_app(backend))
    response = client.post("/auth/login", json={"username": "admin", "password": INVALID_PASSWORD})
    assert response.status_code == 401
    assert response.json() == {"error": "Invalid credentials"}


def test_refresh_cookie_uses_forwarded_browser_scheme_and_rejects_cross_origin(auth_server):
    backend = RemoteAuthBackend(auth_server.base_url, admin_token=ADMIN_API_TOKEN)
    client = TestClient(_build_consumer_app(backend))

    secure_login = client.post(
        "/auth/login",
        json={"username": "admin", "password": BOOTSTRAP_ADMIN_PASSWORD},
        headers={"X-Forwarded-Proto": "https"},
    )
    assert secure_login.status_code == 200
    assert " Secure" in secure_login.headers["set-cookie"]

    forwarded_origin_login = client.post(
        "/auth/login",
        json={"username": "admin", "password": BOOTSTRAP_ADMIN_PASSWORD},
        headers={
            "Origin": "http://localhost:6600",
            "Host": "127.0.0.1:5055",
            "X-Forwarded-Host": "localhost:6600",
            "X-Forwarded-Proto": "http",
        },
    )
    assert forwarded_origin_login.status_code == 200
    assert " Secure" not in forwarded_origin_login.headers["set-cookie"]

    rejected = client.post(
        "/auth/refresh",
        headers={"Origin": "https://example.com"},
    )
    assert rejected.status_code == 403
    assert rejected.json() == {"error": "跨站认证请求被拒绝"}


def test_refresh_route_backend_without_refresh_returns_503(tmp_path):
    # 后端未实现 refresh_tokens（如 AuthManager 契约测试后端）时，/auth/refresh 返回 503
    backend_without_refresh = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    client = TestClient(_build_consumer_app(backend_without_refresh))
    response = client.post("/auth/refresh")
    assert response.status_code == 503
    assert response.json() == {"error": "当前认证模式不支持刷新"}


# ---------------------------------------------------------------- 装配


def test_build_runtime_services_assembles_remote_backend(auth_server, tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setenv("AUTH_SERVER_URL", auth_server.base_url)
    monkeypatch.setenv("AUTH_SERVER_ADMIN_TOKEN", ADMIN_API_TOKEN)
    monkeypatch.setenv("AUTH_SERVER_ISSUER", auth_server.base_url)
    monkeypatch.setenv("AUTH_SERVER_AUDIENCE", "knowledge-scaffold")
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))

    # remote 是唯一路径：无需 AUTH_MODE，装配即得 RemoteAuthBackend
    runtime = app_module.build_runtime_services()
    remote = runtime["auth_manager"]
    assert isinstance(remote, RemoteAuthBackend)

    # 装配后端可用：真 HTTP 打到 fixture 起的 auth server
    result = remote.login("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert result is not None and result.user.role == "admin"


def test_build_runtime_services_requires_auth_server_url(tmp_path, monkeypatch):
    import app as app_module

    # 默认配置（未设置 AUTH_SERVER_URL）即 remote 唯一路径：缺 auth server 地址必须抛错
    monkeypatch.delenv("AUTH_SERVER_URL", raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))
    with pytest.raises(RuntimeError, match="AUTH_SERVER_URL"):
        app_module.build_runtime_services()


def test_create_app_requires_auth_server_url(tmp_path, monkeypatch):
    import app as app_module

    # create_app 不打桩 build_runtime_services 时，缺 AUTH_SERVER_URL 同样抛错
    monkeypatch.delenv("AUTH_SERVER_URL", raising=False)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))
    with pytest.raises(RuntimeError, match="AUTH_SERVER_URL"):
        with TestClient(app_module.create_app()):
            pass


def test_remote_backend_requires_base_url():
    with pytest.raises(ValueError):
        RemoteAuthBackend("")
