"""RemoteAuthBackend 集成测试：进程内起真实 auth server（真 HTTP，无 mock）。

覆盖施工步骤 4（docs/decisions/2026-07-09-decoupled-auth-architecture.md 方案 a）：
密码登录、JWKS 本地验签、refresh 轮换、改密后的吊销语义（D2：旧 refresh 立即失效、
旧 access 在 ≤TTL 窗口内仍可验签——本文件显式固化该放宽语义）、
用户管理 Admin API 翻译、故障路径、以及 build_runtime_services 的 remote 装配。
"""

from __future__ import annotations

import socket
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest
from werkzeug.serving import make_server

from api.auth.auth import AuthManager, PasswordPolicyError
from api.auth.remote_auth import RemoteAuthBackend

# 测试专用例外：决策 D4 禁止 consumer 与 auth-server 之间任何生产代码 Python import
# （唯一接口是 HTTP）。集成测试为了进程内起真实 server 而 import auth_server，
# 属于测试基础设施，不构成生产依赖；生产代码严禁效仿。
AUTH_SERVER_DIR = Path(__file__).resolve().parents[3] / "auth-server"
sys.path.insert(0, str(AUTH_SERVER_DIR))

from auth_server import create_app as create_auth_server_app  # noqa: E402
from auth_server.config import AuthServerConfig  # noqa: E402

ADMIN_TOKEN = "test-admin-token"
STRONG_PASSWORD = "Zq7#Wmx2Lp8!"
STRONG_PASSWORD_2 = "Kv3$Ntb6Rd5?"


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
        audience="meta-wise",
        access_ttl_seconds=60,  # server 允许的最小值；测试内旧 access 始终处于 TTL 窗口
        admin_token=ADMIN_TOKEN,
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
    return RemoteAuthBackend(auth_server.base_url, admin_token=ADMIN_TOKEN)


def _build_consumer_app(auth_manager):
    """与 test_auth_contract.py 相同的装配打桩方式，只替换 auth_manager。"""
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


# ---------------------------------------------------------- 登录 / 验签 / 刷新


def test_password_login_issue_and_verify(backend):
    result = backend.login("admin", "admin123")
    assert result is not None
    user = result.user
    assert user.username == "admin"
    assert user.role == "admin"
    assert user.password_change_required is True

    token = result.access_token
    assert isinstance(token, str) and token.count(".") == 2  # RS256 JWT
    assert result.refresh_token
    assert result.expires_in == 60

    # 本地验签（JWKS）同一条代码路径
    verified = backend.verify_token(token)
    assert verified is not None
    assert verified.username == "admin"
    assert verified.role == "admin"
    assert verified.password_change_required is True


def test_login_rejects_bad_credentials(backend):
    assert backend.login("admin", "wrong-password") is None
    assert backend.login("no-such-user", "whatever") is None
    assert backend.login("", "") is None


def test_concurrent_same_username_logins_keep_token_pairs_isolated(backend):
    """同用户名并发登录必须各自拿到本次请求的 token 对，不能经共享缓存串号。"""
    barrier = threading.Barrier(3)

    def login_once():
        barrier.wait(timeout=5)
        return backend.login("admin", "admin123")

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


def test_verify_token_rejects_non_access_tokens(backend):
    result = backend.login("admin", "admin123")
    assert result is not None and result.refresh_token

    # refresh token 不能当访问凭据（typ != access）
    assert backend.verify_token(result.refresh_token) is None
    # 篡改签名
    assert backend.verify_token(result.access_token[:-3] + "abc") is None
    assert backend.verify_token("not-a-token") is None
    assert backend.verify_token("") is None


def test_refresh_tokens_rotates_pair(backend):
    result = backend.login("admin", "admin123")
    assert result is not None and result.refresh_token

    renewed = backend.refresh_tokens(result.refresh_token)
    assert renewed is not None
    assert renewed["access_token"] and renewed["refresh_token"]
    assert renewed["expires_in"] == 60
    verified = backend.verify_token(renewed["access_token"])
    assert verified is not None and verified.username == "admin"

    assert backend.refresh_tokens("garbage") is None
    assert backend.refresh_tokens("") is None


def test_change_password_revokes_refresh_but_old_access_survives_ttl(backend):
    old_login = backend.login("admin", "admin123")
    assert old_login is not None and old_login.refresh_token

    changed = backend.change_password("admin", "admin123", "NewRemote#2026")
    assert changed.username == "admin"
    assert changed.password_change_required is False

    # D2 固化语义：改密吊销全部 refresh token（等价 password_version+1）……
    assert backend.refresh_tokens(old_login.refresh_token) is None
    # ……但 verify 是 JWKS 本地验签、不回查 ver：旧 access 在 ≤TTL 窗口内仍可验签。
    # 这是决策 D2 显式放宽的吊销窗口，不是缺陷；此断言防止后人“顺手修复”改回逐请求回查。
    still_valid = backend.verify_token(old_login.access_token)
    assert still_valid is not None and still_valid.username == "admin"

    assert backend.login("admin", "admin123") is None
    renewed = backend.login("admin", "NewRemote#2026")
    assert renewed is not None and renewed.user.password_change_required is False


# ---------------------------------------------------------------- 用户管理


def test_user_management_via_admin_api(backend):
    created = backend.create_user("alice", STRONG_PASSWORD)
    assert created.username == "alice"
    assert created.role == "user"
    assert created.password_change_required is True

    users = backend.list_users()
    assert {"username": "alice", "role": "user", "password_change_required": True} in users

    fetched = backend.get_user("alice")
    assert fetched is not None and fetched.role == "user"
    assert backend.get_user("nobody") is None

    updated = backend.update_user("alice", password=STRONG_PASSWORD_2)
    assert updated.password_change_required is True

    changed = backend.change_password("alice", STRONG_PASSWORD_2, "Alz9$Pwd7Xy1!")
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
    backend = RemoteAuthBackend(dead_url, admin_token=ADMIN_TOKEN)

    with pytest.raises(ValueError, match="认证服务暂不可用"):
        backend.login("admin", "admin123")
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        backend.refresh_tokens("some-refresh-token")
    with pytest.raises(ValueError, match="认证服务暂不可用"):
        backend.list_users()
    # verify_token 不抛错：拿不到 JWKS → None（require_auth 走 401）
    assert backend.verify_token("x.y.z") is None


# ---------------------------------------------------------------- 路由集成


def test_login_and_refresh_routes_remote_mode(auth_server):
    backend = RemoteAuthBackend(auth_server.base_url, admin_token=ADMIN_TOKEN)
    client = _build_consumer_app(backend).test_client()

    login = client.post("/auth/login", json={"username": "admin", "password": "<REDACTED_WEAK_PASSWORD>"})
    assert login.status_code == 200
    data = login.get_json()
    # 冻结契约 {token, user} 之上的加法扩展
    assert set(data.keys()) == {"token", "user", "refresh_token", "expires_in"}
    assert data["user"] == {
        "username": "admin",
        "role": "admin",
        "password_change_required": True,
    }
    assert data["expires_in"] == 60

    # 拿到的 access token 能通过 require_auth（/auth/me 在强制改密白名单内）
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {data['token']}"})
    assert me.status_code == 200
    assert me.get_json()["user"]["username"] == "admin"

    refreshed = client.post("/auth/refresh", json={"refresh_token": data["refresh_token"]})
    assert refreshed.status_code == 200
    refreshed_data = refreshed.get_json()
    assert set(refreshed_data.keys()) == {"token", "refresh_token", "expires_in"}
    me2 = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {refreshed_data['token']}"}
    )
    assert me2.status_code == 200

    logout = client.post(
        "/auth/logout", json={"refresh_token": refreshed_data["refresh_token"]}
    )
    assert logout.status_code == 200
    assert logout.get_json() == {"success": True}
    revoked = client.post(
        "/auth/refresh", json={"refresh_token": refreshed_data["refresh_token"]}
    )
    assert revoked.status_code == 401
    assert revoked.get_json() == {"error": "Unauthorized"}

    missing_logout_token = client.post("/auth/logout", json={})
    assert missing_logout_token.status_code == 400

    # 上游拒绝（无效 refresh）→ 401，前端据此登出
    rejected = client.post("/auth/refresh", json={"refresh_token": "garbage"})
    assert rejected.status_code == 401
    assert rejected.get_json() == {"error": "Unauthorized"}


def test_login_wrong_password_remote_mode_contract(auth_server):
    backend = RemoteAuthBackend(auth_server.base_url)
    client = _build_consumer_app(backend).test_client()
    response = client.post("/auth/login", json={"username": "admin", "password": "nope"})
    assert response.status_code == 401
    assert response.get_json() == {"error": "Invalid credentials"}


def test_refresh_route_backend_without_refresh_returns_503(tmp_path):
    # 后端未实现 refresh_tokens（如 AuthManager 契约测试后端）时，/auth/refresh 返回 503
    backend_without_refresh = AuthManager(str(tmp_path / "users.json"), "test-secret")
    client = _build_consumer_app(backend_without_refresh).test_client()
    response = client.post("/auth/refresh", json={"refresh_token": "anything"})
    assert response.status_code == 503
    assert response.get_json() == {"error": "当前认证模式不支持刷新"}


# ---------------------------------------------------------------- 装配


def test_build_runtime_services_assembles_remote_backend(auth_server, tmp_path, monkeypatch):
    import app as app_module

    monkeypatch.setenv("AUTH_SERVER_URL", auth_server.base_url)
    monkeypatch.setenv("AUTH_SERVER_ADMIN_TOKEN", ADMIN_TOKEN)
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / "project"))

    # remote 是唯一路径：无需 AUTH_MODE，装配即得 RemoteAuthBackend
    runtime = app_module.build_runtime_services()
    remote = runtime["auth_manager"]
    assert isinstance(remote, RemoteAuthBackend)

    # 装配后端可用：真 HTTP 打到 fixture 起的 auth server
    result = remote.login("admin", "admin123")
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
        app_module.create_app()


def test_remote_backend_requires_base_url():
    with pytest.raises(ValueError):
        RemoteAuthBackend("")
