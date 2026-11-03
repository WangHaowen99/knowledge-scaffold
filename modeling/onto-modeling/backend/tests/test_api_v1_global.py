"""非项目级 /api/v1 接口（API 契约 3.1、3.2、3.23～3.25）：形状、角色、本地模式，以及旧路径经兼容中间件。

auth-server 用内存里的假实现代替；用户名、项目、令牌均为合成数据。
"""

from __future__ import annotations

import json
import re
import shutil
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import compat, deps
from app.main import app
from app.routers import admin, auth_routes, ops
from app.services import audit, auth, error_log, model_settings, personal_model_settings
from app.services.auth import AuthError
from app.services.onto_mcp import tokens as mcp_tokens
from app.services.workspace import WorkspaceManager

ADMIN, USER, OTHER, OPS = "operator_admin", "operator_a", "operator_b", "operator_ops"
TOKENS = {
    "admin-token": {"sub": ADMIN, "role": "admin"},
    "user-token": {"sub": USER, "role": "user"},
    "other-token": {"sub": OTHER, "role": "user"},
    "ops-token": {"sub": OPS, "role": "ops"},
}
RFC3339 = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z$")
SYNTHETIC_KEY = "synthetic-user-key-0001"
REAL_AUTH_ADMIN_REQUEST = admin._auth_admin_request


class FakeAuthServer:
    """auth-server 管理接口的内存替身：响应形状与中文错误消息同 auth-server/auth_server/routes.py。"""

    def __init__(self):
        self.users = {
            ADMIN: {"role": "admin", "password": "Synthetic#Admin1"},
            USER: {"role": "user", "password": "Synthetic#User1"},
            OTHER: {"role": "user", "password": "Synthetic#Other1"},
        }
        self.calls: list[tuple[str, str, dict | None]] = []

    @staticmethod
    def _policy(username: str, password: str, old: str | None = None):
        violations = []
        if len(password) < 8:
            violations.append("min_length")
        if username.lower() in password.lower():
            violations.append("contains_username")
        if old is not None and password == old:
            violations.append("same_as_old")
        if violations:
            return 400, {"error": "密码复杂度不满足要求", "code": "PASSWORD_POLICY_FAILED", "violations": violations}
        return None

    def __call__(self, method: str, path: str, payload: dict | None = None):
        self.calls.append((method, path, payload))
        if method == "GET" and path == "/admin/users":
            return 200, {"users": [
                {"username": name, "role": user["role"], "password_change_required": False}
                for name, user in self.users.items()
            ]}
        if method == "PUT" and path == "/admin/change-password":
            user = self.users.get(payload["username"])
            if not user or user["password"] != payload["old_password"]:
                return 400, {"error": "原密码错误"}
            failed = self._policy(payload["username"], payload["new_password"], payload["old_password"])
            if failed:
                return failed
            user["password"] = payload["new_password"]
            return 200, {"success": True}
        name = path.removeprefix("/admin/users/")
        if name not in self.users:
            return 400, {"error": f"用户 {name} 不存在"}
        if method == "PUT":
            if "role" in payload:
                if payload["role"] not in ("admin", "user", "ops"):
                    return 400, {"error": "角色必须是 admin、user 或 ops"}
                self.users[name]["role"] = payload["role"]
            if "password" in payload:
                failed = self._policy(name, payload["password"])
                if failed:
                    return failed
                self.users[name]["password"] = payload["password"]
            return 200, {"user": {"username": name, "role": self.users[name]["role"], "password_change_required": False}}
        if method == "DELETE":
            del self.users[name]
            return 200, {"success": True}
        raise AssertionError(f"unexpected {method} {path}")


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(config, "BACKUP_DIR", "")
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    # 这些用例经业务面调运维接口：打开 ONTO_WEB_OPS_ON_MAIN（默认运维面只在独立监听上，见 test_governance）
    monkeypatch.setattr(config, "OPS_ON_MAIN", True)
    assets = tmp_path / "assets"
    (assets / "resource-template").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    instance = WorkspaceManager(data, assets)
    monkeypatch.setattr(deps, "workspaces", instance)
    return instance


@pytest.fixture()
def fake_auth(monkeypatch) -> FakeAuthServer:
    server = FakeAuthServer()
    monkeypatch.setattr(admin, "_auth_admin_request", server)
    monkeypatch.setattr(admin, "registration_enabled", lambda: True)
    monkeypatch.setattr(config, "AUTH_ADMIN_TOKEN", "synthetic-admin-token")
    return server


@pytest.fixture()
def client(manager, fake_auth, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (TOKENS.get(token), "" if token in TOKENS else "invalid"))
    return TestClient(app)


@pytest.fixture()
def local_client(manager, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ENABLED", False)
    return TestClient(app)


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _assert_legacy(response, successor: str):
    assert response.headers["Deprecation"] == "true"
    assert response.headers["Sunset"] == compat.SUNSET
    assert response.headers["Link"] == f'<{successor}>; rel="successor-version"'


def _assert_error(response, status: int, code: str):
    assert response.status_code == status, response.text
    body = response.json()
    assert body["code"] == code and body["message"] and "trace_id" in body and "details" in body
    return body


# ---- C-01 客户端配置 ----


def test_client_config_shape_and_legacy_status(client):
    body = client.get("/api/v1/client-config").json()
    assert body["api_version"] == "1" and body["page_api"] == "1.0"
    assert body["auth"] == {"enabled": True, "registration_enabled": True, "local_user": None}
    assert body["ops_base_url"] == "" and body["maintenance"] is None
    assert body["limits"]["material_upload_bytes"] == 20 * 1024 * 1024
    assert body["limits"]["page_size_max"] == 200 and body["limits"]["attachments_per_message"] == 10
    assert ".docx" in body["material_types"] and ".exe" not in body["material_types"]
    assert isinstance(body["features"]["drawio"], bool) and body["billing"]["currency"]

    legacy = client.get("/api/auth/status")  # 免鉴权：改写后落在 /api/v1/client-config
    assert legacy.status_code == 200 and legacy.json() == body
    _assert_legacy(legacy, "/api/v1/client-config")


def test_client_config_local_mode_and_local_entry(local_client):
    body = local_client.get("/api/v1/client-config").json()
    assert body["auth"] == {"enabled": False, "registration_enabled": False, "local_user": "local"}
    entered = local_client.post("/api/v1/auth/local").json()["user"]
    assert entered["username"] == "local" and entered["auth_source"] == "local"
    assert set(entered["roles"]) == {"user", "admin", "ops"}


def test_local_entry_is_404_when_auth_enabled(client):
    _assert_error(client.post("/api/v1/auth/local"), 404, "not_found")


# ---- A-01～A-05 登录、注册、刷新、退出 ----


def test_login_returns_profile_and_audits_without_password(client, monkeypatch):
    calls = []

    def token_request(payload):
        calls.append(payload)
        if payload.get("password") != "Synthetic#User1":
            raise AuthError("用户名或密码错误，或登录已失效")
        return {"access_token": "user-token", "refresh_token": "synthetic-refresh", "token_type": "Bearer", "expires_in": 900}

    monkeypatch.setattr(auth, "token_request", token_request)
    ok = client.post("/api/v1/auth/login", json={"username": USER, "password": "Synthetic#User1"})
    assert ok.status_code == 200
    body = ok.json()
    assert body["access_token"] == "user-token" and body["expires_in"] == 900
    assert body["user"] == {
        "username": USER, "display_name": USER, "role": "user", "role_label": "建模者",
        "roles": ["user"], "auth_source": "auth-server",
    }
    _assert_error(client.post("/api/v1/auth/login", json={"username": USER, "password": "wrong-pass"}), 401, "invalid_credentials")
    _assert_error(client.post("/api/v1/auth/login", json={"username": "", "password": "x"}), 400, "invalid_argument")

    entries = [entry for entry in audit.read(50) if (entry.get("detail") or {}).get("action") == "auth.login"]
    assert [entry["detail"]["result"] for entry in entries] == ["failed", "ok"]
    assert "Synthetic#User1" not in audit._file().read_text(encoding="utf-8")

    monkeypatch.setattr(auth, "token_request", lambda payload: (_ for _ in ()).throw(AuthError("认证服务不可达", 502)))
    _assert_error(client.post("/api/v1/auth/login", json={"username": USER, "password": "x"}), 502, "upstream_unavailable")

    legacy = client.post("/api/auth/login", json={"username": USER, "password": "wrong"})
    assert legacy.status_code == 502 and legacy.json()["error"] == legacy.json()["message"]
    _assert_legacy(legacy, "/api/v1/auth/login")


def test_register_logs_in_and_maps_errors(client, monkeypatch):
    registered = []
    responses = iter([
        (200, {"success": True}),
        (503, {"error": "注册功能未开启"}),
        (400, {"error": "用户 new_user 已存在"}),
        (400, {"error": "密码复杂度不满足要求", "code": "PASSWORD_POLICY_FAILED", "violations": ["min_length", "character_classes"]}),
        (429, {"error": "请求过于频繁，请稍后再试"}),
    ])

    def register_request(payload):
        registered.append(payload)
        return next(responses)

    monkeypatch.setattr(auth, "register_request", register_request)
    monkeypatch.setattr(auth, "token_request", lambda payload: {"access_token": "other-token", "refresh_token": "r", "expires_in": 60})
    body = {"username": "new_user", "password": "Synthetic#Pass1"}
    created = client.post("/api/v1/auth/register", json=body)
    assert created.status_code == 201 and created.json()["user"]["username"] == OTHER
    assert registered[0]["email"].endswith("@users.example.com")
    _assert_error(client.post("/api/v1/auth/register", json=body), 403, "registration_disabled")
    _assert_error(client.post("/api/v1/auth/register", json=body), 409, "username_taken")
    weak = _assert_error(client.post("/api/v1/auth/register", json=body), 400, "weak_password")
    assert weak["details"]["failed_rules"] == ["len", "classes"]
    _assert_error(client.post("/api/v1/auth/register", json=body), 429, "rate_limited")
    _assert_error(client.post("/api/v1/auth/register", json={"username": "a b", "password": "Synthetic#Pass1"}), 400, "invalid_argument")
    assert len(registered) == 5


def test_refresh_and_logout(client, monkeypatch):
    monkeypatch.setattr(auth, "token_request", lambda payload: (_ for _ in ()).throw(AuthError("登录已失效")))
    _assert_error(client.post("/api/v1/auth/refresh", json={"refresh_token": "expired"}), 401, "refresh_invalid")
    monkeypatch.setattr(auth, "token_request", lambda payload: {"access_token": "admin-token", "refresh_token": "r2", "expires_in": 60})
    refreshed = client.post("/api/v1/auth/refresh", json={"refresh_token": "r1"}).json()
    assert refreshed["refresh_token"] == "r2" and refreshed["user"]["role"] == "admin"

    revoked = []
    monkeypatch.setattr(auth_routes, "revoke_refresh_token", lambda token: revoked.append(token) or True)
    response = client.post("/api/v1/auth/logout", headers=_h("user-token"), json={"refresh_token": "r2"})
    assert response.status_code == 204 and revoked == ["r2"]
    logout = [entry for entry in audit.read(10) if (entry.get("detail") or {}).get("action") == "auth.logout"]
    assert logout and logout[0]["actor"] == USER


def test_expired_token_is_distinguished(client, monkeypatch):
    monkeypatch.setattr(auth, "check_access_token", lambda token: (None, "expired"))
    _assert_error(client.get("/api/v1/me", headers=_h("stale-token")), 401, "token_expired")
    _assert_error(client.get("/api/v1/me"), 401, "unauthenticated")


# ---- A-06、A-07 ----


def test_change_password_uses_token_username(client, fake_auth):
    response = client.put(
        "/api/v1/me/password", headers=_h("user-token"),
        json={"old_password": "Synthetic#User1", "new_password": "Synthetic#User2"},
    )
    assert response.status_code == 204
    assert fake_auth.users[USER]["password"] == "Synthetic#User2"
    _assert_error(
        client.put("/api/v1/me/password", headers=_h("user-token"),
                   json={"old_password": "wrong", "new_password": "Synthetic#User3"}),
        400, "old_password_incorrect",
    )
    same = _assert_error(
        client.put("/api/v1/me/password", headers=_h("user-token"),
                   json={"old_password": "Synthetic#User2", "new_password": "Synthetic#User2"}),
        400, "weak_password",
    )
    assert same["details"]["failed_rules"] == ["same_as_old"]

    # 旧接口请求体里的 username 不再认：改的是令牌里的用户
    legacy = client.post(
        "/api/auth/password", headers=_h("other-token"),
        json={"username": USER, "old_password": "Synthetic#Other1", "new_password": "Synthetic#Other2"},
    )
    assert legacy.status_code == 204
    _assert_legacy(legacy, "/api/v1/me/password")
    assert fake_auth.users[OTHER]["password"] == "Synthetic#Other2"
    assert fake_auth.users[USER]["password"] == "Synthetic#User2"


def test_change_password_unavailable_without_admin_token(client, monkeypatch):
    monkeypatch.setattr(admin, "_auth_admin_request", REAL_AUTH_ADMIN_REQUEST)
    monkeypatch.setattr(config, "AUTH_ADMIN_TOKEN", "")
    response = client.put("/api/v1/me/password", headers=_h("user-token"), json={"old_password": "a", "new_password": "b"})
    _assert_error(response, 503, "user_admin_unavailable")


def test_me_profile_and_usage(client, manager):
    manager.create_project("project_alpha", owner=USER)
    manager.create_project("project_beta", owner=OTHER)
    body = client.get("/api/v1/me", headers=_h("user-token")).json()
    assert body["username"] == USER and body["role_label"] == "建模者"
    assert body["project_count"] == 1
    assert body["usage_this_month"]["output_tokens"] == 0
    assert body["usage_this_month"]["cost"]["currency"]


def test_me_in_local_mode(local_client, manager):
    manager.create_project("project_alpha")
    body = local_client.get("/api/v1/me").json()
    assert body["username"] == "local" and body["project_count"] == 1 and body["last_login"] is None
    _assert_error(local_client.put("/api/v1/me/password", json={"old_password": "a", "new_password": "b"}), 403, "forbidden")


# ---- A-09～A-11 我的模型配置 ----


def test_my_model_settings_shape_and_switching(client):
    model_settings.save("https://gateway.example.com/v1", "synthetic-admin-key-000", "synthetic-model")
    body = client.get("/api/v1/me/model-settings", headers=_h("user-token")).json()
    assert body["personal_allowed"] is True and set(body["engines"]) == {"claude", "jiuwen"}
    claude = body["engines"]["claude"]
    assert claude["source"] == "admin" and claude["api_key_set"] is False
    assert claude["admin_summary"] == {"gateway_host": "gateway.example.com", "model": "synthetic-model"}
    assert set(claude["context"]) == {"window", "max_output", "compact_at_pct"}
    assert body["engines"]["jiuwen"]["source"] == "deployment"
    assert "synthetic-admin-key" not in json.dumps(body)

    _assert_error(client.put("/api/v1/me/model-settings/claude", headers=_h("user-token"), json={"source": "personal"}), 400, "invalid_argument")
    saved = client.put(
        "/api/v1/me/model-settings/claude", headers=_h("user-token"),
        json={"source": "personal", "api_key": SYNTHETIC_KEY, "model": "synthetic-personal"},
    )
    assert saved.status_code == 200 and saved.json()["source"] == "personal"
    assert SYNTHETIC_KEY not in saved.text and saved.json()["api_key_set"] is True
    # 其他用户看不到
    assert client.get("/api/v1/me/model-settings", headers=_h("other-token")).json()["engines"]["claude"]["source"] == "admin"
    # 只改模型，Key 不重填
    again = client.put("/api/v1/me/model-settings/claude", headers=_h("user-token"), json={"source": "personal", "model": "m2"})
    assert again.json()["model"] == "m2" and again.json()["source"] == "personal"
    back = client.put("/api/v1/me/model-settings/claude", headers=_h("user-token"), json={"source": "admin"})
    assert back.json()["source"] == "admin" and back.json()["api_key_set"] is False

    client.put("/api/v1/me/model-settings/claude", headers=_h("user-token"), json={"source": "personal", "api_key": SYNTHETIC_KEY})
    cleared = client.delete("/api/v1/me/model-settings/claude", headers=_h("user-token"))
    assert cleared.status_code == 200 and cleared.json()["source"] == "admin"
    _assert_error(client.delete("/api/v1/me/model-settings/unknown", headers=_h("user-token")), 400, "invalid_argument")

    model_settings.save_personal_policy(False)
    assert client.get("/api/v1/me/model-settings", headers=_h("user-token")).json()["personal_allowed"] is False
    _assert_error(
        client.put("/api/v1/me/model-settings/claude", headers=_h("user-token"), json={"source": "personal", "api_key": SYNTHETIC_KEY}),
        403, "forbidden",
    )

    legacy = client.get("/api/settings/model/me", headers=_h("user-token"))
    assert legacy.status_code == 200 and "engines" in legacy.json()
    _assert_legacy(legacy, "/api/v1/me/model-settings")


# ---- SY 系统设置 ----


def test_system_settings_require_admin(client):
    for method, path in [
        ("get", "/api/v1/admin/settings/model"),
        ("get", "/api/v1/admin/settings/context-presets"),
        ("get", "/api/v1/admin/settings/session"),
        ("get", "/api/v1/admin/settings/sandbox"),
        ("get", "/api/v1/admin/settings/mcp-servers"),
    ]:
        for token in ("user-token", "ops-token"):
            body = _assert_error(getattr(client, method)(path, headers=_h(token)), 403, "forbidden")
            assert body["details"] == {"required_role": "admin"}
    legacy = client.put("/api/settings/model/personal-policy", headers=_h("user-token"), json={"allow": False})
    assert legacy.status_code == 403 and legacy.json()["error"]
    assert model_settings.personal_override_allowed() is True


def test_model_settings_shape_and_partial_update(client):
    body = client.get("/api/v1/admin/settings/model", headers=_h("admin-token")).json()
    assert set(body["engines"]) == {"claude", "jiuwen"} and body["default_engine"] == "claude"
    claude = body["engines"]["claude"]
    assert claude["label"] == "稳定引擎" and claude["channel"] == "stable"
    assert claude["context"]["window"] is None and claude["context_defaults"]["window"]["value"] == 200000
    assert claude["api_key_set"] is False and body["personal_key_allowed"] is True

    saved = client.put(
        "/api/v1/admin/settings/model/claude", headers=_h("admin-token"),
        json={"base_url": "https://gateway.example.com", "api_key": "sk-synthetic-0123456789abcdef", "model": "m1",
              "context": {"window": 128000, "max_output": 16000}},
    ).json()
    assert saved["model"] == "m1" and saved["api_key_set"] is True and "0123456789" not in json.dumps(saved)
    assert saved["context"]["window"] == 128000 and saved["context"]["max_output"] == 16000
    assert isinstance(saved["context"]["compact_at_pct"], int)
    # 只改窗口：模型与最大输出不变
    partial = client.put("/api/v1/admin/settings/model/claude", headers=_h("admin-token"), json={"context": {"window": 256000}}).json()
    assert partial["model"] == "m1" and partial["context"] == {**partial["context"], "window": 256000, "max_output": 16000}
    invalid = client.put("/api/v1/admin/settings/model/claude", headers=_h("admin-token"), json={"context": {"max_output": 300000}})
    _assert_error(invalid, 400, "invalid_argument")
    assert model_settings.context_config()["max_output_tokens"] == 16000

    jiuwen = client.put(
        "/api/v1/admin/settings/model/jiuwen", headers=_h("admin-token"),
        json={"base_url": "https://api.example.com/v1", "model": "j1", "context": {"window": 8000, "max_output": 4000}},
    ).json()
    assert jiuwen["model"] == "j1" and jiuwen["context"]["compact_at_pct"] >= 30
    _assert_error(client.put("/api/v1/admin/settings/model/other", headers=_h("admin-token"), json={}), 400, "invalid_argument")

    legacy = client.get("/api/settings/model", headers=_h("admin-token"))
    assert legacy.json()["engines"]["claude"]["model"] == "m1"
    _assert_legacy(legacy, "/api/v1/admin/settings/model")
    legacy_put = client.put("/api/settings/model/jiuwen", headers=_h("admin-token"), json={"model": "j2"})
    assert legacy_put.json()["model"] == "j2"


def test_default_engine_personal_policy_and_session(client, monkeypatch):
    from app.services import engines

    _assert_error(client.put("/api/v1/admin/settings/default-engine", headers=_h("admin-token"), json={"engine": "x"}), 400, "invalid_argument")
    monkeypatch.setattr(engines, "available_engines", lambda: {"claude": True, "jiuwen": False})
    unavailable = _assert_error(
        client.put("/api/v1/admin/settings/default-engine", headers=_h("admin-token"), json={"engine": "jiuwen"}),
        409, "engine_unavailable",
    )
    assert unavailable["details"] == {"engine": "jiuwen"}
    assert client.put("/api/v1/admin/settings/default-engine", headers=_h("admin-token"), json={"engine": "claude"}).json() == {"default_engine": "claude"}

    assert client.put("/api/v1/admin/settings/personal-key-policy", headers=_h("admin-token"), json={"allow": False}).json() == {"allow": False}
    assert model_settings.personal_override_allowed() is False

    session = client.get("/api/v1/admin/settings/session", headers=_h("admin-token")).json()
    assert session["permission_mode"] in session["allowed"] and session["pool"]["source"] == "env"
    assert set(session["pool"]) == {"max_sessions", "idle_ttl_s", "permission_timeout_s", "source"}
    changed = client.put("/api/v1/admin/settings/session", headers=_h("admin-token"), json={"permission_mode": "default"}).json()
    assert changed["permission_mode"] == "default"
    _assert_error(client.put("/api/v1/admin/settings/session", headers=_h("admin-token"), json={"permission_mode": "yolo"}), 400, "invalid_argument")
    legacy = client.get("/api/settings/runtime", headers=_h("admin-token"))
    assert legacy.json()["permission_mode"] == "default"
    _assert_legacy(legacy, "/api/v1/admin/settings/session")


def test_context_presets_shape_save_conflict_and_disable(client):
    body = client.get("/api/v1/admin/settings/context-presets", headers=_h("admin-token")).json()
    assert body["rows"] and {"model", "vendor", "window", "max_output", "note", "origin", "builtin"} <= set(body["rows"][0])
    assert body["custom"]["status"] == "disabled" and body["custom"]["version"] == ""
    builtin = body["rows"][0]
    saved = client.put(
        "/api/v1/admin/settings/context-presets/custom", headers=_h("admin-token"),
        json={"version": "", "presets": [
            {"model": builtin["model"], "window": builtin["window"] + 1000, "max_output": builtin["max_output"]},
            {"model": "example-model-pro", "vendor": "example", "window": 64000, "max_output": 8000},
        ]},
    )
    assert saved.status_code == 200, saved.text
    custom = saved.json()["custom"]
    assert custom["status"] == "ok" and custom["overrides"] == 1 and custom["added"] == 1
    override = next(row for row in saved.json()["rows"] if row["model"] == builtin["model"])
    assert override["origin"] == "override" and override["builtin"]["window"] == builtin["window"]

    stale = client.put("/api/v1/admin/settings/context-presets/custom", headers=_h("admin-token"), json={"version": "", "presets": []})
    conflict = _assert_error(stale, 409, "revision_conflict")
    assert conflict["details"]["actual"] == custom["version"]
    bad = client.put(
        "/api/v1/admin/settings/context-presets/custom", headers=_h("admin-token"),
        json={"version": custom["version"], "presets": [{"model": "example-model-pro", "window": 8000, "max_output": 8000}]},
    )
    assert _assert_error(bad, 400, "invalid_argument")["details"]["error"]["row"] == 1

    match = client.get("/api/v1/admin/settings/context-presets/match", headers=_h("admin-token"), params={"model": "example-model-pro"}).json()
    assert match == {"model": "example-model-pro", "preset": {"window": 64000, "max_output": 8000, "origin": "custom"}}
    miss = client.get("/api/v1/admin/settings/context-presets/match", headers=_h("admin-token"), params={"model": "unknown-model"}).json()
    assert miss["preset"] is None

    disabled = client.post("/api/v1/admin/settings/context-presets/custom/disable", headers=_h("admin-token")).json()
    assert disabled["custom"]["status"] == "disabled" and disabled["custom"]["backup_available"] is True


def test_sandbox_status_shape(client, monkeypatch):
    from app.services import sandbox_status

    monkeypatch.setattr(sandbox_status.client, "gateway_url", lambda: "")
    for path, method in (("/api/v1/admin/settings/sandbox", "get"), ("/api/v1/admin/settings/sandbox/probe", "post")):
        body = getattr(client, method)(path, headers=_h("admin-token")).json()
        assert body["connected"] is False and body["packages"] == [] and RFC3339.match(body["checked_at"])
        assert body["isolation"] == {"non_root": True, "readonly_root": True, "memory_mb": 256, "max_procs": 64, "network": "denied"}
    legacy = client.get("/api/settings/sandbox", headers=_h("admin-token"))
    _assert_legacy(legacy, "/api/v1/admin/settings/sandbox")


def test_mcp_servers_roundtrip_and_test(client):
    saved = client.put(
        "/api/v1/admin/settings/mcp-servers", headers=_h("admin-token"),
        json={"items": [{"name": "synthetic_http", "transport": "http", "url": "http://127.0.0.1:9/mcp"}]},
    ).json()
    item = saved["items"][0]
    assert item["name"] == "synthetic_http" and item["auth"] == {"kind": "none"} and item["status"] == "unknown"
    assert client.get("/api/v1/admin/settings/mcp-servers", headers=_h("admin-token")).json() == saved
    _assert_error(
        client.put("/api/v1/admin/settings/mcp-servers", headers=_h("admin-token"), json={"items": [{"name": "bad name"}]}),
        400, "invalid_argument",
    )
    _assert_error(
        client.put("/api/v1/admin/settings/mcp-servers", headers=_h("admin-token"),
                   json={"items": [{"name": "s", "transport": "http", "url": "http://127.0.0.1:9", "auth": {"kind": "bearer"}}]}),
        400, "invalid_argument",
    )
    tested = client.post(
        "/api/v1/admin/settings/mcp-servers/test", headers=_h("admin-token"),
        json={"server": {"name": "synthetic_stdio", "transport": "stdio", "command": "definitely-not-a-command-0001"}},
    ).json()
    assert tested["ok"] is False and tested["error"] and isinstance(tested["latency_ms"], int) and tested["tools"] == []
    legacy = client.get("/api/settings/mcp", headers=_h("admin-token"))
    assert legacy.json() == saved
    _assert_legacy(legacy, "/api/v1/admin/settings/mcp-servers")


def test_removed_assets_listing_is_410(client):
    gone = _assert_error(client.get("/api/settings/assets", headers=_h("admin-token")), 410, "endpoint_removed")
    assert gone["details"]["successor"] == "/api/v1/ops/core-default"


# ---- AD 用户管理 ----


def test_users_listing_shape_roles_and_unavailable(client, manager, monkeypatch):
    manager.create_project("project_alpha", owner=USER)
    body = client.get("/api/v1/admin/users", headers=_h("admin-token")).json()
    assert body["available"] is True and body["stats"]["accounts"] == 3
    assert body["stats"]["by_role"] == {"user": 2, "admin": 1, "ops": 0}
    alpha = next(item for item in body["items"] if item["username"] == USER)
    assert alpha == {**alpha, "role": "user", "project_count": 1, "locked_until": None}
    assert set(alpha) == {"username", "display_name", "role", "online", "last_seen_at", "project_count", "locked_until"}
    filtered = client.get("/api/v1/admin/users", headers=_h("admin-token"), params={"q": "admin", "limit": 1}).json()
    assert [item["username"] for item in filtered["items"]] == [ADMIN] and filtered["next_cursor"] is None
    # ops 只读
    assert client.get("/api/v1/admin/users", headers=_h("ops-token")).status_code == 200
    _assert_error(client.patch(f"/api/v1/admin/users/{USER}", headers=_h("ops-token"), json={"role": "admin"}), 403, "forbidden")
    _assert_error(client.get("/api/v1/admin/users", headers=_h("user-token")), 403, "forbidden")

    legacy = client.get("/api/admin/users", headers=_h("admin-token"))
    _assert_legacy(legacy, "/api/v1/admin/users")

    monkeypatch.setattr(config, "AUTH_ADMIN_TOKEN", "")
    unavailable = client.get("/api/v1/admin/users", headers=_h("admin-token")).json()
    assert unavailable["available"] is False and unavailable["items"] == []


def test_change_role_and_last_admin_guard(client, fake_auth):
    changed = client.patch(f"/api/v1/admin/users/{USER}", headers=_h("admin-token"), json={"role": "admin"})
    assert changed.status_code == 200 and changed.json()["role"] == "admin"
    client.patch(f"/api/v1/admin/users/{USER}", headers=_h("admin-token"), json={"role": "user"})
    last = _assert_error(client.patch(f"/api/v1/admin/users/{ADMIN}", headers=_h("admin-token"), json={"role": "user"}), 403, "forbidden")
    assert last["details"]["reason"] == "last_admin"
    _assert_error(client.patch("/api/v1/admin/users/nobody", headers=_h("admin-token"), json={"role": "user"}), 404, "user_not_found")
    _assert_error(client.patch(f"/api/v1/admin/users/{USER}", headers=_h("admin-token"), json={"role": "root"}), 400, "invalid_argument")
    # auth-server 已有 ops 角色（方案第 6 章第 2 项）：可以设成运维，再改回
    to_ops = client.patch(f"/api/v1/admin/users/{USER}", headers=_h("admin-token"), json={"role": "ops"})
    assert to_ops.status_code == 200 and to_ops.json()["role"] == "ops"
    client.patch(f"/api/v1/admin/users/{USER}", headers=_h("admin-token"), json={"role": "user"})
    roles = [entry["detail"] for entry in audit.read(50) if (entry.get("detail") or {}).get("action") == "user.role"]
    assert roles[0] == {"action": "user.role", "username": USER, "from": "ops", "to": "user"}
    assert roles[1] == {"action": "user.role", "username": USER, "from": "user", "to": "ops"}

    # 旧接口 PUT 不带 password 改写到 PATCH
    legacy = client.put(f"/api/admin/users/{USER}", headers=_h("admin-token"), json={"role": "admin"})
    assert legacy.status_code == 200 and fake_auth.users[USER]["role"] == "admin"
    _assert_legacy(legacy, f"/api/v1/admin/users/{USER}")


def test_reset_password_generate_and_legacy_rewrite(client, fake_auth):
    generated = client.post(f"/api/v1/admin/users/{USER}/reset-password", headers=_h("admin-token"), json={"generate": True})
    password = generated.json()["generated_password"]
    assert len(password) == 16 and fake_auth.users[USER]["password"] == password
    assert password not in audit._file().read_text(encoding="utf-8")
    explicit = client.post(f"/api/v1/admin/users/{USER}/reset-password", headers=_h("admin-token"), json={"password": "Synthetic#Reset1"})
    assert explicit.status_code == 200 and explicit.json() == {}
    weak = _assert_error(
        client.post(f"/api/v1/admin/users/{USER}/reset-password", headers=_h("admin-token"), json={"password": "short"}),
        400, "weak_password",
    )
    assert weak["details"]["failed_rules"] == ["len"]
    _assert_error(client.post(f"/api/v1/admin/users/{USER}/reset-password", headers=_h("admin-token"), json={}), 400, "invalid_argument")

    # 旧接口 PUT 带 password 改写到 reset-password
    legacy = client.put(f"/api/admin/users/{OTHER}", headers=_h("admin-token"), json={"password": "Synthetic#Legacy1"})
    assert legacy.status_code == 200 and fake_auth.users[OTHER]["password"] == "Synthetic#Legacy1"
    _assert_legacy(legacy, f"/api/v1/admin/users/{OTHER}/reset-password")


def test_delete_user_transfers_projects_and_revokes_tokens(client, manager, fake_auth):
    first = manager.create_project("project_alpha", owner=USER)
    second = manager.create_project("project_beta", owner=USER)
    with mcp_tokens.database(manager) as conn:
        conn.execute(
            "INSERT INTO tokens VALUES ('t1', ?, 'digest', 'synthetic', ?, ?, '[]', 0, NULL, NULL, NULL)",
            (first["id"], USER, USER),
        )
    personal_model_settings._write({"users": {USER: {"claude": {"api_key": "enc:v1:synthetic"}}}})

    projects = client.get(f"/api/v1/admin/users/{USER}/projects", headers=_h("admin-token")).json()["items"]
    assert sorted(item["id"] for item in projects) == sorted([first["id"], second["id"]])
    assert set(projects[0]) >= {"id", "name", "owner", "stage_label", "build_mode", "archived", "created_at", "last_activity_at"}
    assert "background" not in projects[0] and "goal" not in projects[0]

    missing = _assert_error(client.request("DELETE", f"/api/v1/admin/users/{USER}", headers=_h("admin-token")), 400, "invalid_argument")
    assert missing["details"]["fields"]["projects"] == "required"
    _assert_error(
        client.request("DELETE", f"/api/v1/admin/users/{USER}", headers=_h("admin-token"), json={"projects": "trash"}),
        400, "invalid_argument",
    )
    _assert_error(
        client.request("DELETE", f"/api/v1/admin/users/{USER}", headers=_h("admin-token"),
                       json={"projects": "transfer", "transfer_to": "nobody"}),
        400, "transfer_target_invalid",
    )
    assert USER in fake_auth.users

    deleted = client.request(
        "DELETE", f"/api/v1/admin/users/{USER}", headers=_h("admin-token"),
        json={"projects": "transfer", "transfer_to": OTHER},
    )
    assert deleted.status_code == 200 and deleted.json() == {"deleted": USER, "transferred": 2, "trashed": 0}
    assert USER not in fake_auth.users
    assert {manager.get_project(pid)["owner"] for pid in (first["id"], second["id"])} == {OTHER}
    with mcp_tokens.database(manager) as conn:
        assert conn.execute("SELECT revoked FROM tokens WHERE id='t1'").fetchone()[0] is not None
    assert USER not in (personal_model_settings._load().get("users") or {})

    # 名下没有项目时不用给去向；旧接口改写过来，ops 不能删
    fake_auth.users["operator_c"] = {"role": "user", "password": "Synthetic#C1"}
    denied = client.delete("/api/admin/users/operator_c", headers=_h("ops-token"))
    _assert_error(denied, 403, "forbidden")
    legacy = client.delete("/api/admin/users/operator_c", headers=_h("admin-token"))
    assert legacy.status_code == 200 and legacy.json()["transferred"] == 0
    _assert_legacy(legacy, "/api/v1/admin/users/operator_c")
    assert "operator_c" not in fake_auth.users


def test_unowned_projects_and_set_owner(client, manager):
    owned = manager.create_project("project_alpha", owner=USER)
    unowned = manager.create_project("project_legacy")
    listed = client.get("/api/v1/admin/projects/unowned", headers=_h("admin-token")).json()["items"]
    assert [item["id"] for item in listed] == [unowned["id"]] and listed[0]["owner"] is None
    path = f"/api/v1/admin/projects/{unowned['id']}/owner"
    _assert_error(client.put(path, headers=_h("user-token"), json={"owner": USER}), 403, "forbidden")
    _assert_error(client.put(path, headers=_h("admin-token"), json={"owner": "has space"}), 400, "invalid_owner")
    assigned = client.put(path, headers=_h("admin-token"), json={"owner": f" {OTHER} "})
    assert assigned.status_code == 200 and assigned.json()["owner"] == OTHER
    assert RFC3339.match(assigned.json()["created_at"])
    details = [entry["detail"] for entry in audit.read(20) if (entry.get("detail") or {}).get("action") == "project.set_owner"]
    assert details == [{"action": "project.set_owner", "project": unowned["id"], "from": "", "to": OTHER}]
    legacy = client.put(f"/api/admin/projects/{owned['id']}/owner", headers=_h("admin-token"), json={"owner": OTHER})
    assert legacy.status_code == 200
    _assert_legacy(legacy, f"/api/v1/admin/projects/{owned['id']}/owner")


# ---- O 运维管理面 ----


@pytest.mark.parametrize("path", [
    "/api/v1/ops/overview", "/api/v1/ops/health", "/api/v1/ops/sessions", "/api/v1/ops/startup-report",
    "/api/v1/ops/backups", "/api/v1/ops/audit", "/api/v1/ops/errors", "/api/v1/ops/trash",
    "/api/v1/ops/core-default", "/api/v1/ops/pack-repo/backups",
])
def test_ops_reads_allow_ops_and_admin_only(client, path):
    assert client.get(path, headers=_h("ops-token")).status_code == 200
    assert client.get(path, headers=_h("admin-token")).status_code == 200
    body = _assert_error(client.get(path, headers=_h("user-token")), 403, "forbidden")
    assert body["details"] == {"required_role": "ops"}


def test_ops_overview_shape_and_legacy(client, manager):
    manager.create_project("project_alpha", owner=USER, background="合成背景说明")
    body = client.get("/api/v1/ops/overview", headers=_h("ops-token"), params={"range": "7d"}).json()
    stats = body["stats"]
    assert stats["projects"] == {"total": 1, "active": 1, "archived": 0}
    assert stats["accounts"] == 3 and set(stats["sessions"]) == {"active", "max", "queued", "idle_ttl_s"}
    assert stats["cost"]["total"]["currency"] and set(stats["disk"]) == {"free_pct", "free_bytes", "threshold_pct", "warn"}
    assert len(body["usage_daily"]["days"]) == 7 and body["usage_daily"]["peak"] is None
    project = body["projects"][0]
    assert project["owner"] == USER and project["disk_bytes"] > 0 and "background" not in project
    assert body["alerts"] == [] and body["autobuild_runs"] == []
    _assert_error(client.get("/api/v1/ops/overview", headers=_h("ops-token"), params={"range": "1y"}), 400, "invalid_argument")

    legacy = client.get("/api/admin/overview", headers=_h("admin-token"))
    assert legacy.status_code == 200 and legacy.json()["stats"]["projects"]["total"] == 1
    _assert_legacy(legacy, "/api/v1/ops/overview")


def test_usage_report_compares_with_previous_window(manager, monkeypatch):
    from datetime import date

    events = [
        ("p1", time.mktime((2026, 10, 30, 12, 0, 0, 0, 0, -1)), True, 0.5, 100, 10),
        ("p1", time.mktime((2026, 10, 31, 12, 0, 0, 0, 0, -1)), True, 1.0, 300, 10),
        ("p1", time.mktime((2026, 10, 28, 12, 0, 0, 0, 0, -1)), True, 1.0, 200, 10),
    ]
    monkeypatch.setattr(ops, "usage_events", lambda project_ids=None: iter(events))
    report = ops.usage_report(2, today=date(2026, 10, 31))
    assert [day["date"] for day in report["days"]] == ["2026-10-30", "2026-10-31"]
    assert report["totals"]["calls"] == 2 and report["totals"]["output_tokens"] == 400
    assert report["peak"]["date"] == "2026-10-31"
    assert report["vs_previous_pct"] == {"calls": 100.0, "cost": 50.0, "output_tokens": 100.0}


def test_ops_health_shape(client, monkeypatch):
    # 依赖探测与就绪检查共用 governance/health 的探测项
    from app.governance import health

    monkeypatch.setattr(health, "_cache", None)
    monkeypatch.setattr(health, "_checks", {
        "disk": ("数据目录可用空间", lambda: ("ok", None), True),
        "auth_server": ("GET /jwks.json", lambda: ("fail", "不可达"), True),
    })
    body = client.get("/api/v1/ops/health", headers=_h("admin-token")).json()
    assert body["live"]["status"] == "ok" and RFC3339.match(body["live"]["started_at"])
    assert body["ready"] == {**body["ready"], "status": "not_ready", "passed": 1, "total": 2}
    assert [item["name"] for item in body["dependencies"]] == ["disk", "auth_server"]
    assert {engine["id"] for engine in body["runtime"]["engines"]} == {"claude", "jiuwen"}
    legacy = client.get("/api/admin/health", headers=_h("admin-token"))
    _assert_legacy(legacy, "/api/v1/ops/health")


def test_ops_sessions_and_recycle(client):
    body = client.get("/api/v1/ops/sessions", headers=_h("ops-token")).json()
    assert body["config"]["overflow_policy"] == "evict_idle_then_queue" and body["items"] == []
    _assert_error(client.post("/api/v1/ops/sessions/no-such-project/recycle", headers=_h("ops-token")), 404, "session_not_found")
    _assert_error(client.post("/api/v1/ops/sessions/no-such-project/recycle", headers=_h("user-token")), 403, "forbidden")
    # 运维写操作只给 ops（契约 3.25；auth-server 已有 ops 角色，过渡期放行 admin 的做法已收紧）
    _assert_error(client.post("/api/v1/ops/sessions/no-such-project/recycle", headers=_h("admin-token")), 403, "forbidden")
    legacy = client.post("/api/admin/sessions/no-such-project/recycle", headers=_h("ops-token"))
    assert legacy.status_code == 404 and legacy.json()["error"]
    _assert_legacy(legacy, "/api/v1/ops/sessions/no-such-project/recycle")


def test_startup_report(client, manager):
    empty = client.get("/api/v1/ops/startup-report", headers=_h("ops-token")).json()
    assert empty == {"at": None, "last_exit": None, "items": []}
    from app.services import startup_report

    startup_report.run(manager)
    body = client.get("/api/v1/ops/startup-report", headers=_h("ops-token")).json()
    assert RFC3339.match(body["at"]) and [item["id"] for item in body["items"]] == [
        "index_lock", "write_intents", "uncommitted", "assembly", "history_repair", "interrupted_execs",
        "dependencies", "purge", "material_text",
    ]


def test_backups_list_and_run(client, manager):
    manager.create_project("project_alpha")
    created = client.post("/api/v1/ops/backups", headers=_h("ops-token"))
    assert created.status_code == 202 and created.json()["task_id"].startswith("backup-")
    body = client.get("/api/v1/ops/backups", headers=_h("ops-token")).json()
    assert [item["name"] for item in body["items"]] == [created.json()["name"]]
    assert body["items"][0]["scope"] == "full" and RFC3339.match(body["items"][0]["created_at"])
    assert body["schedule"]["keep"] == 3 and "workspaces" in body["scope"]
    _assert_error(client.post("/api/v1/ops/backups", headers=_h("user-token")), 403, "forbidden")
    legacy = client.post("/api/admin/backup", headers=_h("admin-token"))
    assert legacy.status_code == 202
    _assert_legacy(legacy, "/api/v1/ops/backups")


def test_backup_in_progress_is_409(client, monkeypatch):
    assert ops._backup_lock.acquire(blocking=False)
    try:
        _assert_error(client.post("/api/v1/ops/backups", headers=_h("ops-token")), 409, "backup_in_progress")
    finally:
        ops._backup_lock.release()


def test_audit_projection_filters_and_cursor(client):
    audit.log(ADMIN, "PUT", "/api/v1/admin/projects/project-a/owner",
              detail={"action": "project.set_owner", "project": "project-a", "from": "", "to": USER})
    audit.log(USER, "POST", "/api/v1/projects/project-a/files/content")
    audit.log("service:synthetic", "POST", "/api/v1/projects/project-a/feedback", {"contract.feedback": "fb-1"})
    audit.log(USER, "MCP", "/projects/project-a/ontology/object:x")
    audit.log(OPS, "POST", "/api/v1/ops/backups")

    page = client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"limit": 2}).json()
    assert len(page["items"]) == 2 and page["total"] == 5 and page["next_cursor"]
    first = page["items"][0]
    assert first["type"] == "ops" and first["actor"] == OPS and RFC3339.match(first["ts"])
    assert page["items"][1]["type"] == "mcp"
    assert set(first) == {"id", "ts", "actor", "actor_kind", "type", "action", "method", "path", "status",
                          "project_id", "summary", "trailer", "ip", "trace_id"}
    # 翻页期间新写入不让条目错位
    audit.log(OPS, "POST", "/api/v1/ops/backups")
    rest = client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"limit": 2, "cursor": page["next_cursor"]}).json()
    assert [item["type"] for item in rest["items"]] == ["contract", "write"]

    by_type = client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"type": "user,mcp"}).json()["items"]
    assert [(item["type"], item["actor_kind"]) for item in by_type] == [("mcp", "mcp"), ("user", "user")]
    assert by_type[1]["action"] == "project.set_owner" and by_type[1]["project_id"] == "project-a"
    assert "from=" in by_type[1]["summary"]
    searched = client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"q": "files/content"}).json()["items"]
    assert [item["action"] for item in searched] == ["http.post"]
    future = client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"from": "2999-01-01T00:00:00.000Z"}).json()
    assert future["items"] == []
    _assert_error(client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"type": "nope"}), 400, "invalid_argument")
    _assert_error(client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"cursor": page["next_cursor"], "q": "x"}), 400, "invalid_cursor")
    _assert_error(client.get("/api/v1/ops/audit", headers=_h("ops-token"), params={"from": "yesterday"}), 400, "invalid_argument")
    legacy = client.get("/api/admin/audit", headers=_h("admin-token"))
    _assert_legacy(legacy, "/api/v1/ops/audit")


def test_error_records_are_redacted(client, manager):
    meta = manager.create_project("project_alpha", owner=USER)
    error_log.log(meta["id"], "session_connect", "API Error: 401 Authorization: Bearer abcdefghijklmnop api_key=sk-synthetic-000000")
    error_log.log("", "kernel", "[err-1a2b3c] RuntimeError: postgres://reader:synthetic-pass@db.example.invalid/x")
    body = client.get("/api/v1/ops/errors", headers=_h("ops-token")).json()
    assert body["total"] == 2
    kernel, session = body["items"]
    assert kernel["id"] == "err-1a2b3c" and kernel["origin"] == "kernel" and kernel["project"] is None
    assert "synthetic-pass" not in kernel["raw_message"] and "[err-" not in kernel["raw_message"]
    assert session["origin"] == "session" and session["project"]["id"] == meta["id"]
    assert "abcdefghijklmnop" not in session["raw_message"] and "sk-synthetic" not in session["raw_message"]
    assert re.match(r"^err-[0-9a-f]{6}$", session["id"]) and session["friendly_message"]
    paged = client.get("/api/v1/ops/errors", headers=_h("ops-token"), params={"limit": 1}).json()
    assert len(paged["items"]) == 1 and paged["next_cursor"]


def _trash_project(manager, name: str, owner: str) -> str:
    meta = manager.create_project(name, owner=owner)
    entry = f"{meta['id']}-{int(time.time())}"
    manager.trash_dir().mkdir(parents=True, exist_ok=True)
    shutil.move(str(manager.project_root(meta["id"])), str(manager.trash_dir() / entry))
    return entry


def test_trash_listing_and_restore(client, manager):
    entry = _trash_project(manager, "project_alpha", USER)
    body = client.get("/api/v1/ops/trash", headers=_h("ops-token")).json()
    item = body["items"][0]
    assert item["entry"] == entry and item["project"]["owner"] == USER and item["size"] > 0
    assert RFC3339.match(item["deleted_at"]) and "background" not in item["project"]

    _assert_error(client.post("/api/v1/ops/trash/no-such-entry/restore", headers=_h("ops-token")), 404, "trash_entry_not_found")
    restored = client.post(f"/api/v1/ops/trash/{entry}/restore", headers=_h("admin-token"))
    assert restored.status_code == 200 and restored.json()["owner"] == USER
    assert manager.get_project(restored.json()["id"])["owner"] == USER


def test_legacy_trash_restore_moves_name_into_path(client, manager):
    entry = _trash_project(manager, "project_beta", OTHER)
    missing = client.post("/api/admin/trash/restore", headers=_h("admin-token"), json={"name": "no-such-entry"})
    body = _assert_error(missing, 404, "trash_entry_not_found")
    assert body["error"] == body["message"]
    _assert_legacy(missing, "/api/v1/ops/trash/no-such-entry/restore")
    restored = client.post("/api/admin/trash/restore", headers=_h("admin-token"), json={"name": entry})
    assert restored.status_code == 200 and restored.json()["owner"] == OTHER
    _assert_legacy(restored, f"/api/v1/ops/trash/{entry}/restore")
    listing = client.get("/api/admin/trash", headers=_h("admin-token"))
    assert listing.json()["items"] == []
    _assert_legacy(listing, "/api/v1/ops/trash")


def test_core_default_rollback_and_pack_repo_backups(client, manager, tmp_path):
    """O-30～O-34 由包仓库承接原资产包（方案 2.3 第 4、5 步）：默认版本是 stable 指针，回滚先备份包仓库。"""
    from tests.pack_helpers import make_pack

    repo = manager.packs.repository
    for version in ("0.1.0", "0.2.0"):
        repo.install(make_pack(tmp_path / version, "core", version, required=True,
                               files={"commands/ontology.md": f"core {version}\n"}), origin="upload")
    repo.set_channel("core", "stable", "0.2.0")

    view = client.get("/api/v1/ops/core-default", headers=_h("ops-token")).json()
    assert view["stable"] == "0.2.0" and view["beta"] is None
    assert [(v["version"], v["channel"], v["is_default"]) for v in view["versions"]] == [
        ("0.2.0", "stable", True), ("0.1.0", "retired", False)]
    assert RFC3339.match(view["versions"][0]["released_at"])

    _assert_error(client.post("/api/v1/ops/core-default/rollback", headers=_h("ops-token"), json={"version": "../x"}), 400, "invalid_argument")
    _assert_error(client.post("/api/v1/ops/core-default/rollback", headers=_h("ops-token"), json={"version": "9.9.9"}), 404, "not_found")
    rolled = client.post("/api/v1/ops/core-default/rollback", headers=_h("ops-token"), json={"version": "0.1.0"})
    assert rolled.status_code == 200 and rolled.json()["stable"] == "0.1.0"
    assert repo.has("core", "0.2.0")  # 旧版本一直在包仓库里

    backups = client.get("/api/v1/ops/pack-repo/backups", headers=_h("ops-token")).json()
    assert backups["keep"] == 3 and len(backups["items"]) == 1  # 回滚前自动备份
    assert backups["items"][0]["size"] > 0 and RFC3339.match(backups["items"][0]["created_at"])
    made = client.post("/api/v1/ops/pack-repo/backups", headers=_h("ops-token"))
    assert made.status_code == 202 and made.json()["name"].startswith("packs-")
    promoted = client.post("/api/v1/ops/core-default/promote", headers=_h("ops-token"), json={"version": "0.2.0"})
    assert promoted.json()["stable"] == "0.2.0"

    legacy = client.get("/api/settings/assets/backups", headers=_h("admin-token"))
    _assert_legacy(legacy, "/api/v1/ops/pack-repo/backups")


def test_assets_upload_moves_to_pack_uploads(client):
    """旧资产包上传改写到 PL-13（上传的资产包当作 core 新版本，走静态检查）。"""
    response = client.post(
        "/api/settings/assets/upload", headers=_h("user-token"),
        files={"file": ("assets.zip", b"not-a-zip", "application/zip")},
    )
    _assert_error(response, 403, "forbidden")
    _assert_legacy(response, "/api/v1/admin/packs/uploads")


# ---- 本地模式 ----


def test_local_mode_has_all_roles(local_client, manager):
    assert local_client.get("/api/v1/admin/settings/model").status_code == 200
    assert local_client.get("/api/v1/ops/overview").status_code == 200
    users = local_client.get("/api/v1/admin/users").json()
    assert users["available"] is False
    _assert_error(local_client.patch(f"/api/v1/admin/users/{USER}", json={"role": "user"}), 503, "user_admin_unavailable")
