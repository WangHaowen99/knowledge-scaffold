"""Synthetic first-login contract for Onto Modeling and independent Auth."""

import json

from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import admin, auth_routes
from app.services import auth


def test_auth_userinfo_is_authoritative(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_URL", "http://auth.example.com")

    class UserInfoResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            assert size == 16385
            return json.dumps({"sub": "operator_a", "role": "user",
                               "password_change_required": True}).encode()

    def urlopen(request, timeout):
        assert request.full_url == "http://auth.example.com/userinfo"
        assert request.get_header("Authorization") == "Bearer synthetic-token"
        return UserInfoResponse()

    monkeypatch.setattr(auth.urllib.request, "urlopen", urlopen)
    user = auth.current_user("synthetic-token", {"sub": "operator_a", "role": "admin", "pwd_cr": False})
    assert user["password_change_required"] is True
    assert user["role"] == "user"


def test_auth_userinfo_failure_does_not_grant_access(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_URL", "http://auth.example.com")

    def unavailable(request, timeout):
        raise auth.urllib.error.URLError("synthetic unavailable")

    monkeypatch.setattr(auth.urllib.request, "urlopen", unavailable)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: {"sub": "operator_a"})
    response = TestClient(app).get("/api/projects", headers={"Authorization": "Bearer synthetic-token"})
    assert response.status_code == 503


def test_auth_userinfo_rejects_mismatched_identity(monkeypatch):
    monkeypatch.setattr(auth, "AUTH_URL", "http://auth.example.com")

    class UserInfoResponse:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return None

        def read(self, size):
            return b'{"sub":"other","role":"admin","password_change_required":false}'

    monkeypatch.setattr(auth.urllib.request, "urlopen", lambda request, timeout: UserInfoResponse())
    try:
        auth.current_user("synthetic-token", {"sub": "operator_a"})
    except auth.AuthError as exc:
        assert exc.status == 401
    else:
        raise AssertionError("mismatched userinfo must be rejected")


def test_first_login_blocks_business_but_allows_account_state(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: {"sub": "operator_a", "role": "admin"}
                        if token == "synthetic-token" else None)
    pending = {"sub": "operator_a", "role": "admin", "password_change_required": True}
    monkeypatch.setattr(auth, "current_user", lambda token, claims: pending.copy())
    client = TestClient(app)
    headers = {"Authorization": "Bearer synthetic-token"}

    assert client.get("/api/auth/me", headers=headers).json()["user"]["password_change_required"] is True
    blocked = client.get("/api/projects", headers=headers)
    assert blocked.status_code == 403
    assert blocked.json()["code"] == "password_change_required"
    assert client.get("/api/projects").status_code == 401

    pending["password_change_required"] = False
    assert client.get("/api/projects", headers=headers).status_code != 403


def test_self_password_change_uses_auth_change_endpoint(monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(config, "AUTH_ADMIN_TOKEN", "synthetic-admin-token")
    monkeypatch.setattr(auth, "token_request", lambda payload: {"access_token": "synthetic-token"})
    calls = []

    def admin_request(method, path, payload):
        calls.append((method, path, payload))
        return 200, {"success": True}

    monkeypatch.setattr(admin, "_auth_admin_request", admin_request)
    response = auth_routes.change_password(auth_routes.PasswordBody(
        username="operator_a", old_password="OldSynthetic123!", new_password="NewSynthetic123!"))
    assert response.status_code == 200
    assert calls == [("PUT", "/admin/change-password", {
        "username": "operator_a", "old_password": "OldSynthetic123!",
        "new_password": "NewSynthetic123!"})]
