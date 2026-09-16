# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from api.auth.auth import AuthManager
from api.routes.workspaces import workspaces_router
from api.testing import TestApplication
from backend_synthetic_credentials import (
    AUTH_SIGNING_SECRET,
    BOOTSTRAP_ADMIN_PASSWORD,
    ENCRYPTION_KEY,
    synthetic_secret,
)
from fastapi.testclient import TestClient
from workspace.workspaces import create_workspace

SECRET_TEST_KEY = ENCRYPTION_KEY


def _build_client(tmp_path, monkeypatch, *, role="admin"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)

    app = TestApplication()
    app.state.services["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    if role == "admin":
        user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
        assert user is not None
        user = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, "RootStrong123!")
    else:
        user = auth_manager.create_user("viewer", "AccountStrong123!", role=role)
        user = auth_manager.change_password("viewer", "AccountStrong123!", "ChangedStrong123!")
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(workspaces_router)
    token = auth_manager.issue_token(user)
    return TestClient(app), {"Authorization": f"Bearer {token}"}


def test_get_workspace_observability_config_masks_secret(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch, role="user")

    response = client.get("/agentic/workspaces/demo/observability-config", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is False
    assert payload["mode"] == "local"
    assert payload["local"] == {"retention_days": 30}
    assert payload["speedscope"] == {"retention_days": 30}
    assert payload["langfuse"]["has_secret_key"] is False
    assert "secret_key" not in payload["langfuse"]


def test_get_workspace_observability_config_reports_migration_required_without_writing(
    tmp_path,
    monkeypatch,
):
    client, headers = _build_client(tmp_path, monkeypatch, role="user")
    raw_path = tmp_path / ".onto-runtime" / "workspaces" / "demo" / "observability_config.json"
    raw_path.write_text("{broken", encoding="utf-8")
    before = raw_path.read_bytes()

    response = client.get("/agentic/workspaces/demo/observability-config", headers=headers)

    assert response.status_code == 409
    assert response.json() == {
        "code": "WORKSPACE_OBSERVABILITY_MIGRATION_REQUIRED",
        "detail": "工作空间 'demo' 的观测配置不是规范格式，需要先完成迁移或人工修复",
        "workspace_id": "demo",
    }
    assert raw_path.read_bytes() == before


def test_patch_workspace_observability_config_updates_and_resets_observer(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)
    reset_calls = []
    monkeypatch.setattr(
        "api.routes.workspaces.reset_observer",
        lambda timeout=1.0: reset_calls.append(timeout),
    )

    response = client.patch(
        "/agentic/workspaces/demo/observability-config",
        json={
            "enabled": True,
            "mode": "langfuse",
            "local": {"retention_days": 14},
            "speedscope": {"retention_days": 7},
            "langfuse": {
                "public_key": "pk-test",
                "secret_key": synthetic_secret("workspace_observability_routes-credential-02"),
                "base_url": "https://langfuse.example.net",
                "debug": True,
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["enabled"] is True
    assert payload["mode"] == "langfuse"
    assert payload["local"]["retention_days"] == 14
    assert payload["speedscope"]["retention_days"] == 7
    assert payload["langfuse"]["public_key"] == "pk-test"
    assert payload["langfuse"]["has_secret_key"] is True
    assert "secret_key" not in payload["langfuse"]
    assert reset_calls == [1.0]


def test_workspace_observability_patch_requires_admin(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch, role="user")

    response = client.patch(
        "/agentic/workspaces/demo/observability-config",
        json={"enabled": True},
        headers=headers,
    )

    assert response.status_code == 403


def test_workspace_observability_connection_test_reuses_saved_secret(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)
    client.patch(
        "/agentic/workspaces/demo/observability-config",
        json={
            "enabled": True,
            "mode": "langfuse",
            "langfuse": {
                "public_key": "pk-old",
                "secret_key": synthetic_secret("workspace_observability_routes-credential-01"),
                "base_url": "https://langfuse.old",
            },
        },
        headers=headers,
    )
    calls = []

    class FakeResponse:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def raise_for_status(self):
            return None

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr("integration.observability.workspace_config.requests.get", fake_get)

    response = client.post(
        "/agentic/workspaces/demo/observability-config/test",
        json={
            "enabled": True,
            "mode": "langfuse",
            "langfuse": {
                "public_key": "pk-new",
                "base_url": "https://langfuse.new",
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert "Langfuse 连接成功" in payload["message"]
    assert calls == [
        (
            "https://langfuse.new/api/public/projects",
            {"auth": ("pk-new", synthetic_secret("workspace_observability_routes-credential-01")), "timeout": 10},
        )
    ]
