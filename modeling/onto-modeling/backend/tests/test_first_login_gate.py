"""首登改密门禁与账号状态核对（ADR-0107、契约 2.2 password_change_required）：全部合成数据。"""

import json
import secrets

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import admin
from app.services import auth

TOKEN = "synthetic-token"
HEADERS = {"Authorization": f"Bearer {TOKEN}"}


class UserInfoResponse:
    def __init__(self, body: dict):
        self.body = json.dumps(body).encode()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def read(self, size):
        assert size == 16385
        return self.body


@pytest.fixture
def login(monkeypatch):
    """开启鉴权、认合成令牌；返回可改的账号状态（auth.current_user 照它回）。"""
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token",
                        lambda token: {"sub": "operator_a", "role": "admin"} if token == TOKEN else None)
    state = {"sub": "operator_a", "role": "admin", "password_change_required": True}
    monkeypatch.setattr(auth, "current_user", lambda token, claims: dict(state))
    return state


def test_userinfo_is_authoritative(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_URL", "https://auth.example.com")

    def urlopen(request, timeout):
        assert request.full_url == "https://auth.example.com/userinfo"
        assert request.get_header("Authorization") == f"Bearer {TOKEN}"
        return UserInfoResponse({"sub": "operator_a", "role": "user", "password_change_required": True})

    monkeypatch.setattr(auth.urllib.request, "urlopen", urlopen)
    user = auth.current_user(TOKEN, {"sub": "operator_a", "role": "admin", "pwd_cr": False})
    assert user["password_change_required"] is True
    assert user["role"] == "user"


def test_userinfo_accepts_ops_and_rejects_mismatch(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_URL", "https://auth.example.com")
    body = {"sub": "operator_a", "role": "ops", "password_change_required": False}
    monkeypatch.setattr(auth.urllib.request, "urlopen", lambda request, timeout: UserInfoResponse(body))
    assert auth.current_user(TOKEN, {"sub": "operator_a"})["role"] == "ops"

    for bad in ({**body, "sub": "other"}, {**body, "role": "viewer"}, {**body, "password_change_required": "yes"}):
        monkeypatch.setattr(auth.urllib.request, "urlopen", lambda request, timeout, b=bad: UserInfoResponse(b))
        with pytest.raises(auth.AuthError) as exc:
            auth.current_user(TOKEN, {"sub": "operator_a"})
        assert exc.value.status == 401


def test_userinfo_failure_does_not_grant_access(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_URL", "https://auth.example.com")
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: {"sub": "operator_a"})

    def unavailable(request, timeout):
        raise auth.urllib.error.URLError("synthetic unavailable")

    monkeypatch.setattr(auth.urllib.request, "urlopen", unavailable)
    response = TestClient(app).get("/api/v1/projects", headers=HEADERS)
    assert response.status_code == 503
    assert response.json()["code"] == "service_unavailable"
    assert response.json()["details"] == {"dependency": "auth_server"}


def test_userinfo_rejection_is_unauthenticated(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: {"sub": "operator_a"})

    def rejected(token, claims):
        raise auth.AuthError("账号状态无效", 401)

    monkeypatch.setattr(auth, "current_user", rejected)
    response = TestClient(app).get("/api/v1/projects", headers=HEADERS)
    assert response.status_code == 401
    assert response.json()["code"] == "unauthenticated"


def test_first_login_blocks_business_but_allows_profile(login):
    client = TestClient(app)
    profile = client.get("/api/v1/me", headers=HEADERS)
    assert profile.status_code == 200
    assert profile.json()["password_change_required"] is True

    blocked = client.get("/api/v1/projects", headers=HEADERS)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"
    # 旧路径改写成 v1 之后同样拦
    assert client.get("/api/projects", headers=HEADERS).status_code == 403
    assert client.get("/api/v1/projects").status_code == 401

    login["password_change_required"] = False
    assert client.get("/api/v1/projects", headers=HEADERS).status_code != 403
    assert client.get("/api/v1/me", headers=HEADERS).json()["password_change_required"] is False


def test_role_follows_auth_not_token(login):
    login.update(role="user", password_change_required=False)
    client = TestClient(app)
    assert client.get("/api/v1/me", headers=HEADERS).json()["role"] == "user"


def test_password_change_allowed_under_gate(login, monkeypatch):
    monkeypatch.setattr(config, "AUTH_ADMIN_TOKEN", "synthetic-admin-token")
    calls = []

    def admin_request(method, path, payload):
        calls.append((method, path, payload))
        return 200, {"success": True}

    monkeypatch.setattr(admin, "_auth_admin_request", admin_request)
    old_secret = secrets.token_urlsafe(24)
    new_secret = secrets.token_urlsafe(24)
    response = TestClient(app).put("/api/v1/me/password", headers=HEADERS,
                                   json={"old_password": old_secret, "new_password": new_secret})
    assert response.status_code == 204
    assert calls == [("PUT", "/admin/change-password", {
        "username": "operator_a", "old_password": old_secret, "new_password": new_secret})]
