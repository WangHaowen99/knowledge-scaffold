import json

import pytest
from api.auth.auth import AuthUser, require_authenticated_user
from api.routes.auth import auth_router
from api.testing import TestApplication
from auth_test_backend import AuthManager
from backend_synthetic_credentials import (
    AUTH_SIGNING_SECRET,
    BOOTSTRAP_ADMIN_PASSWORD,
    INVALID_PASSWORD,
    STRONG_PASSWORD,
    STRONG_PASSWORD_2,
    STRONG_PASSWORD_3,
)
from fastapi import APIRouter, Depends
from fastapi.testclient import TestClient


def _build_auth_app(auth_manager):
    app = TestApplication()
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(auth_router)
    return app


def test_bootstrap_requires_explicit_strong_password(tmp_path, monkeypatch):
    monkeypatch.delenv("AUTH_BOOTSTRAP_ADMIN_PASSWORD")
    with pytest.raises(RuntimeError, match="AUTH_BOOTSTRAP_ADMIN_PASSWORD"):
        AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)

    with pytest.raises(ValueError, match="密码复杂度"):
        AuthManager(
            str(tmp_path / "weak-users.json"),
            AUTH_SIGNING_SECRET,
            bootstrap_admin_password=INVALID_PASSWORD,
        )


def test_default_admin_can_login_without_password_change(tmp_path):
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)

    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)

    assert user is not None
    assert user.password_change_required is False
    assert user.to_public_dict()["password_change_required"] is False


def test_user_creation_requires_complex_password(tmp_path):
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)

    with pytest.raises(ValueError, match="密码复杂度"):
        auth_manager.create_user("operator", INVALID_PASSWORD)
    with pytest.raises(ValueError, match="密码复杂度"):
        auth_manager.create_user("operator", INVALID_PASSWORD)

    user = auth_manager.create_user("operator", STRONG_PASSWORD)

    assert user.password_change_required is False


def test_change_password_invalidates_old_token(tmp_path):
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    old_token = auth_manager.issue_token(user)

    changed = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, STRONG_PASSWORD_2)

    assert changed.password_change_required is False
    assert auth_manager.verify_token(old_token) is None
    assert auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD) is None
    assert auth_manager.authenticate("admin", STRONG_PASSWORD_2).password_change_required is False


def test_legacy_plaintext_password_is_migrated_without_forced_change(tmp_path):
    users_file = tmp_path / "users.json"
    users_file.write_text(
        json.dumps(
            {
                "users": [
                    {
                        "username": "legacy",
                        "password": STRONG_PASSWORD,
                        "role": "user",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    auth_manager = AuthManager(str(users_file), AUTH_SIGNING_SECRET)

    user = auth_manager.authenticate("legacy", STRONG_PASSWORD)
    payload = json.loads(users_file.read_text(encoding="utf-8"))
    record = payload["users"][0]

    assert user is not None
    assert user.password_change_required is False
    assert "password" not in record
    assert record["password_hash"].startswith("pbkdf2_sha256$")
    assert record["must_change_password"] is False
    assert record["password_changed_at"]


def test_require_auth_allows_regular_routes_without_password_change(tmp_path):
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    token = auth_manager.issue_token(user)

    app = TestApplication()
    app.state.services["AUTH_MANAGER"] = auth_manager
    probe_router = APIRouter()

    @probe_router.get("/protected")
    def protected(
        _user: AuthUser = Depends(require_authenticated_user),
    ):
        return {"success": True}

    @probe_router.put("/auth/change-password")
    def allowed_change_password(
        _user: AuthUser = Depends(require_authenticated_user),
    ):
        return {"success": True}

    app.include_router(probe_router)
    client = TestClient(app)
    headers = {"Authorization": "Bearer {}".format(token)}

    protected_response = client.get("/protected", headers=headers)
    allowed_response = client.put("/auth/change-password", headers=headers)

    assert protected_response.status_code == 200
    assert protected_response.json() == {"success": True}
    assert allowed_response.status_code == 200


def test_change_password_route_requires_relogin_after_success(tmp_path):
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    token = auth_manager.issue_token(user)

    app = _build_auth_app(auth_manager)
    probe_router = APIRouter()

    @probe_router.get("/protected")
    def protected_after_change(
        _user: AuthUser = Depends(require_authenticated_user),
    ):
        return {"success": True}

    app.include_router(probe_router)
    client = TestClient(app)
    headers = {"Authorization": "Bearer {}".format(token)}

    response = client.put(
        "/auth/change-password",
        headers=headers,
        json={"old_password": BOOTSTRAP_ADMIN_PASSWORD, "new_password": STRONG_PASSWORD_2},
    )
    protected_response = client.get("/protected", headers=headers)

    assert response.status_code == 200
    assert response.json() == {"success": True, "requires_relogin": True}
    assert protected_response.status_code == 401
    assert auth_manager.verify_token(token) is None


def test_admin_self_password_reset_route_is_rejected(tmp_path):
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, STRONG_PASSWORD_2)
    user = auth_manager.authenticate("admin", STRONG_PASSWORD_2)
    token = auth_manager.issue_token(user)

    app = _build_auth_app(auth_manager)

    client = TestClient(app)
    headers = {"Authorization": "Bearer {}".format(token)}

    response = client.put(
        "/auth/users/admin",
        headers=headers,
        json={"password": STRONG_PASSWORD_3},
    )

    assert response.status_code == 400
    assert "不能通过用户管理重置当前登录用户密码" in response.json()["error"]
    assert auth_manager.verify_token(token) is not None
    assert auth_manager.authenticate("admin", STRONG_PASSWORD_3) is None
    assert auth_manager.authenticate("admin", STRONG_PASSWORD_2) is not None
