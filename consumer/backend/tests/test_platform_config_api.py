from api.testing import TestApplication
from auth_test_backend import AuthManager
from backend_synthetic_credentials import AUTH_SIGNING_SECRET, BOOTSTRAP_ADMIN_PASSWORD, synthetic_secret
from fastapi.testclient import TestClient


def _build_client(tmp_path, monkeypatch):
    from api.routes.platform_config import platform_config_router

    config_path = tmp_path / "config" / "platform_config.json"
    monkeypatch.setenv("ONTO_PLATFORM_CONFIG_PATH", str(config_path))

    app = TestApplication()
    app.state.services["TESTING"] = True
    refresh_calls = []

    def refresh_platform_connector():
        refresh_calls.append("refresh")

    app.state.services["REFRESH_PLATFORM_CONNECTOR"] = refresh_platform_connector

    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    admin = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert admin is not None
    admin = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, "RootStrong123!")

    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(platform_config_router)
    client = TestClient(app)
    token = auth_manager.issue_token(admin)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers, refresh_calls, config_path


def test_platform_config_api_saves_masked_config_and_refreshes_connector(tmp_path, monkeypatch):
    from integration.platform.config import PlatformConfig

    client, headers, refresh_calls, config_path = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/platform-config",
        json={
            "enabled": True,
            "base_url": "https://platform.example.net",
            "environment_id": "prod",
            "instance_id": "instance-prod-01",
            "api_key": synthetic_secret("platform_config_api-credential-01"),
            "batch_size": 3,
            "timeout_seconds": 4,
            "heartbeat_interval_seconds": 20,
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["config"]["enabled"] is True
    assert payload["config"]["base_url"] == "https://platform.example.net"
    assert payload["config"]["environment_id"] == "prod"
    assert payload["config"]["instance_id"] == "instance-prod-01"
    assert payload["config"]["has_api_key"] is True
    assert "api_key" not in payload["config"]
    assert refresh_calls == ["refresh"]
    assert synthetic_secret("platform_config_api-credential-01") not in config_path.read_text(encoding="utf-8")

    resolved = PlatformConfig.from_env()
    assert resolved.available is True
    assert resolved.api_key == synthetic_secret("platform_config_api-credential-01")

    fetch_response = client.get("/api/platform-config", headers=headers)
    assert fetch_response.status_code == 200
    fetch_payload = fetch_response.json()
    assert fetch_payload["has_api_key"] is True
    assert "api_key" not in fetch_payload


def test_platform_config_api_tests_connection_with_submitted_values(tmp_path, monkeypatch):
    client, headers, _refresh_calls, _config_path = _build_client(tmp_path, monkeypatch)
    captured = []

    def fake_test_platform_config(data):
        captured.append(data)
        return {"success": True, "message": "连接成功", "tested_at": "2026-06-16T10:00:00Z"}

    import api.routes.platform_config as platform_config_routes

    monkeypatch.setattr(platform_config_routes, "test_platform_config_connection", fake_test_platform_config)

    response = client.post(
        "/api/platform-config/test",
        json={
            "enabled": True,
            "base_url": "https://platform.example.net",
            "environment_id": "prod",
            "api_key": synthetic_secret("platform_config_api-credential-01"),
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.json() == {
        "success": True,
        "message": "连接成功",
        "tested_at": "2026-06-16T10:00:00Z",
    }
    assert captured == [
        {
            "enabled": True,
            "base_url": "https://platform.example.net",
            "environment_id": "prod",
            "api_key": synthetic_secret("platform_config_api-credential-01"),
        }
    ]
