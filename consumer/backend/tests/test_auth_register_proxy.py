"""注册 BFF 代理测试：consumer 不实现注册，仅向 auth server 透明转发。

注册一步直接建号（无邮箱验证码），BFF 只保留 /auth/register 一个代理端点。
"""

from types import SimpleNamespace

import pytest
import requests

from agentic.services.auth import AuthManager


def _build_app(auth_manager, auth_server_url=""):
    import agentic.app as app_module

    runtime = {
        "config": SimpleNamespace(auth_server_url=auth_server_url),
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


class _StubResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


def test_register_disabled_without_auth_server_url(auth_manager):
    client = _build_app(auth_manager).test_client()
    response = client.post("/auth/register", json={})
    assert response.status_code == 503
    assert response.get_json() == {"error": "注册功能未开启"}


def test_capabilities_disabled_without_auth_server_url(auth_manager):
    client = _build_app(auth_manager).test_client()

    response = client.get("/auth/capabilities")

    assert response.status_code == 200
    assert response.get_json() == {"registration_enabled": False}


def test_capabilities_proxy_registration_setting(auth_manager, monkeypatch):
    import agentic.routes.auth as auth_routes

    monkeypatch.setattr(
        auth_routes.requests,
        "get",
        lambda *args, **kwargs: _StubResponse(200, {"registration_enabled": True}),
    )
    client = _build_app(auth_manager, "http://auth-server:5056/").test_client()

    response = client.get("/auth/capabilities")

    assert response.status_code == 200
    assert response.get_json() == {"registration_enabled": True}


def test_register_proxy_passthrough_success(auth_manager, monkeypatch):
    calls = {}

    def fake_post(url, json=None, headers=None, timeout=None):
        calls["url"] = url
        calls["json"] = json
        calls["headers"] = headers
        return _StubResponse(200, {
            "success": True,
            "user": {
                "username": "eve",
                "role": "user",
                "password_change_required": False,
            },
        })

    import agentic.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes.requests, "post", fake_post)

    client = _build_app(auth_manager, "http://auth-server:5056/").test_client()
    response = client.post(
        "/auth/register",
        json={"username": "eve", "email": "eve@example.com", "password": "Rt5&Yhn8Uj2!"},
    )
    assert response.status_code == 200
    # 一步建号：透传 auth-server 的 {success, user} 响应
    assert response.get_json() == {
        "success": True,
        "user": {
            "username": "eve",
            "role": "user",
            "password_change_required": False,
        },
    }
    # 尾斜杠归一 + 路径拼接正确
    assert calls["url"] == "http://auth-server:5056/register"
    assert calls["json"]["email"] == "eve@example.com"
    # 必须转发真实客户端 IP，否则 auth server 的按 IP 限流会塌缩成 BFF 单一 IP
    assert "X-Forwarded-For" in calls["headers"]


def test_register_proxy_passthrough_password_policy_error(auth_manager, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _StubResponse(400, {
            "error": "密码复杂度不满足要求",
            "code": "PASSWORD_POLICY_FAILED",
            "violations": ["min_length"],
        })

    import agentic.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes.requests, "post", fake_post)

    client = _build_app(auth_manager, "http://auth-server:5056").test_client()
    response = client.post("/auth/register", json={"username": "eve", "email": "e@x.com", "password": "1"})
    assert response.status_code == 400
    data = response.get_json()
    assert data["code"] == "PASSWORD_POLICY_FAILED"
    assert data["violations"] == ["min_length"]


def test_register_proxy_passthrough_rate_limited(auth_manager, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _StubResponse(429, {"error": "请求过于频繁，请稍后再试"})

    import agentic.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes.requests, "post", fake_post)

    client = _build_app(auth_manager, "http://auth-server:5056").test_client()
    response = client.post(
        "/auth/register",
        json={"username": "eve", "email": "e@x.com", "password": "Rt5&Yhn8Uj2!"},
    )
    assert response.status_code == 429
    assert response.get_json() == {"error": "请求过于频繁，请稍后再试"}


def test_register_proxy_upstream_down(auth_manager, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        raise requests.ConnectionError("boom")

    import agentic.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes.requests, "post", fake_post)

    client = _build_app(auth_manager, "http://auth-server:5056").test_client()
    response = client.post(
        "/auth/register",
        json={"username": "eve", "email": "e@x.com", "password": "Rt5&Yhn8Uj2!"},
    )
    assert response.status_code == 502
    assert response.get_json() == {"error": "注册服务暂不可用，请稍后再试"}


def test_register_proxy_non_json_upstream(auth_manager, monkeypatch):
    def fake_post(url, json=None, headers=None, timeout=None):
        return _StubResponse(500, ValueError("not json"))

    import agentic.routes.auth as auth_routes
    monkeypatch.setattr(auth_routes.requests, "post", fake_post)

    client = _build_app(auth_manager, "http://auth-server:5056").test_client()
    response = client.post(
        "/auth/register",
        json={"username": "eve", "email": "e@x.com", "password": "Rt5&Yhn8Uj2!"},
    )
    assert response.status_code == 500
    assert response.get_json() == {"error": "注册服务响应异常"}


def test_register_verify_resend_endpoints_removed(auth_manager):
    """验证码链路已删除：verify/resend 端点不再存在（404）。"""
    client = _build_app(auth_manager, "http://auth-server:5056").test_client()
    for path in ("/auth/register/verify", "/auth/register/resend"):
        response = client.post(path, json={})
        assert response.status_code == 404
