from flask import Flask

from api.auth.auth import AuthManager


def _build_client(tmp_path, monkeypatch):
    from api.routes.platform_config import platform_config_bp

    config_path = tmp_path / "config" / "platform_config.json"
    monkeypatch.setenv("ONTO_PLATFORM_CONFIG_PATH", str(config_path))

    app = Flask(__name__)
    app.config["TESTING"] = True
    refresh_calls = []
    app.config["REFRESH_PLATFORM_CONNECTOR"] = lambda: refresh_calls.append("refresh")

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    admin = auth_manager.authenticate("admin", "admin123")
    assert admin is not None
    admin = auth_manager.change_password("admin", "admin123", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(platform_config_bp)
    client = app.test_client()
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
            "base_url": "https://platform.example",
            "environment_id": "prod",
            "instance_id": "instance-prod-01",
            "api_key": "secret-key",
            "batch_size": 3,
            "timeout_seconds": 4,
            "heartbeat_interval_seconds": 20,
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["config"]["enabled"] is True
    assert payload["config"]["base_url"] == "https://platform.example"
    assert payload["config"]["environment_id"] == "prod"
    assert payload["config"]["instance_id"] == "instance-prod-01"
    assert payload["config"]["has_api_key"] is True
    assert "api_key" not in payload["config"]
    assert refresh_calls == ["refresh"]
    assert "secret-key" not in config_path.read_text(encoding="utf-8")

    resolved = PlatformConfig.from_env()
    assert resolved.available is True
    assert resolved.api_key == "secret-key"

    fetch_response = client.get("/api/platform-config", headers=headers)
    assert fetch_response.status_code == 200
    fetch_payload = fetch_response.get_json()
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
            "base_url": "https://platform.example",
            "environment_id": "prod",
            "api_key": "secret-key",
        },
        headers=headers,
    )

    assert response.status_code == 200
    assert response.get_json() == {
        "success": True,
        "message": "连接成功",
        "tested_at": "2026-06-16T10:00:00Z",
    }
    assert captured == [
        {
            "enabled": True,
            "base_url": "https://platform.example",
            "environment_id": "prod",
            "api_key": "secret-key",
        }
    ]
