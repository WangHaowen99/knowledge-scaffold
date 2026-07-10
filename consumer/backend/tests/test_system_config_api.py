from flask import Flask

from agentic.routes.system_config import system_config_bp
from agentic.services.auth import AuthManager


def _build_client(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_dir / "system_config.json")

    app = Flask(__name__)
    app.config["TESTING"] = True
    app.config["REFRESH_RUNTIME_SERVICES"] = lambda current_app: None

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    admin = auth_manager.authenticate("admin", "admin123")
    assert admin is not None
    admin = auth_manager.change_password("admin", "admin123", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(system_config_bp)
    client = app.test_client()
    token = auth_manager.issue_token(admin)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def _issue_token(tmp_path, username, role):
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.create_user(username, "UserStrong123!", role=role)
    user = auth_manager.change_password(username, "UserStrong123!", "ChangedStrong123!")
    return auth_manager.issue_token(user)


def test_get_system_config_hides_secrets(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.get("/api/system-config", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["llm"]["base_url"] == ""
    assert payload["llm"]["model"] == ""
    assert payload["llm"]["has_api_key"] is False
    assert "api_key" not in payload["llm"]
    assert "max_tokens" not in payload["task_llm"]
    assert "temperature" not in payload["task_llm"]
    assert "ontologies_root" not in payload["storage"]
    assert "skills_root" not in payload["storage"]
    assert "ontology_search" not in payload
    assert payload["auth"]["has_auth_server_admin_token"] is False
    assert payload["llm_profiles"]["profiles"] == []
    assert payload["observability"]["enabled"] is False
    assert payload["observability"]["mode"] == "local"
    assert payload["observability"]["local"] == {"retention_days": 30}
    assert payload["observability"]["langfuse"]["has_secret_key"] is False
    assert "secret_key" not in payload["observability"]["langfuse"]
    assert payload["web_search"]["enabled"] is False
    assert payload["web_search"]["provider"] == "tavily"
    assert payload["web_search"]["has_api_key"] is False
    assert "api_key" not in payload["web_search"]
    assert payload["agent"]["runtime_warmup_enabled"] is True
    assert "_status" not in payload


def test_get_system_config_readiness_is_available_to_authenticated_users(tmp_path, monkeypatch):
    client, _headers = _build_client(tmp_path, monkeypatch)
    user_token = _issue_token(tmp_path, "user1", "user")
    user_headers = {"Authorization": f"Bearer {user_token}"}

    response = client.get("/api/system-config/readiness", headers=user_headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["ready"] is False
    assert payload["model"]["missing_fields"] == ["llm_profiles.profiles"]
    assert "ontology_search" not in payload


def test_patch_updates_section_and_preserves_secret_masking(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "llm",
            "data": {
                "base_url": "https://example.com/v1",
                "model": "test-model",
                "api_key": "sk-test",
                "timeout": 120,
                "max_tokens": 1024,
                "temperature": 0.2,
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["section"] == "llm"
    assert payload["config"]["base_url"] == "https://example.com/v1"
    assert payload["config"]["model"] == "test-model"
    assert payload["config"]["has_api_key"] is True
    assert "api_key" not in payload["config"]

    fetch_response = client.get("/api/system-config", headers=headers)
    fetch_payload = fetch_response.get_json()
    assert fetch_payload["llm"]["has_api_key"] is True
    assert fetch_payload["llm"]["timeout"] == 120


def test_template_and_import_round_trip(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    template_response = client.get("/api/system-config/template", headers=headers)
    assert template_response.status_code == 200
    template_payload = template_response.get_json()
    assert template_payload["agent"]["main_agent_max_iterations"] == 50
    assert template_payload["auth"]["auth_server_url"] == ""
    assert template_payload["llm"]["base_url"] == ""
    assert template_payload["llm"]["model"] == ""
    assert "max_tokens" not in template_payload["task_llm"]
    assert "temperature" not in template_payload["task_llm"]
    assert "ontologies_root" not in template_payload["storage"]
    assert "skills_root" not in template_payload["storage"]
    assert template_payload["llm_profiles"]["profiles"] == []
    assert template_payload["observability"]["enabled"] is False
    assert template_payload["observability"]["mode"] == "local"
    assert template_payload["observability"]["langfuse"]["secret_key"] == ""
    assert template_payload["web_search"]["enabled"] is False
    assert template_payload["web_search"]["provider"] == "tavily"
    assert template_payload["web_search"]["api_key"] == ""
    assert template_payload["agent"]["runtime_warmup_enabled"] is True

    import_response = client.post(
        "/api/system-config/import",
        json={
            "config": {
                "llm": {
                    "base_url": "https://example.com/v1",
                    "model": "test-model",
                    "api_key": "sk-test",
                }
            }
        },
        headers=headers,
    )

    assert import_response.status_code == 200
    payload = import_response.get_json()
    assert payload["success"] is True
    assert payload["config"]["llm"]["base_url"] == "https://example.com/v1"
    assert payload["config"]["llm"]["has_api_key"] is True


def test_patch_updates_agent_runtime_warmup_enabled(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "agent",
            "data": {
                "runtime_warmup_enabled": False,
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["section"] == "agent"
    assert payload["config"]["runtime_warmup_enabled"] is False

    fetch_response = client.get("/api/system-config", headers=headers)
    fetch_payload = fetch_response.get_json()
    assert fetch_payload["agent"]["runtime_warmup_enabled"] is False


def test_patch_updates_web_search_and_preserves_secret_masking(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "web_search",
            "data": {
                "enabled": True,
                "provider": "tavily",
                "base_url": "https://api.tavily.com",
                "api_key": "tvly-test",
                "timeout_seconds": 10,
                "default_max_results": 3,
                "max_results_limit": 6,
                "search_depth": "basic",
                "allowed_domains": "docs.example.com",
                "blocked_domains": "spam.example.com",
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["section"] == "web_search"
    assert payload["config"]["enabled"] is True
    assert payload["config"]["provider"] == "tavily"
    assert payload["config"]["base_url"] == "https://api.tavily.com"
    assert payload["config"]["has_api_key"] is True
    assert "api_key" not in payload["config"]

    update_response = client.patch(
        "/api/system-config",
        json={
            "section": "web_search",
            "data": {
                "enabled": True,
                "provider": "tavily",
                "base_url": "https://api.tavily.com",
                "timeout_seconds": 11,
                "default_max_results": 2,
                "max_results_limit": 5,
                "search_depth": "fast",
            },
        },
        headers=headers,
    )

    assert update_response.status_code == 200
    update_payload = update_response.get_json()
    assert update_payload["config"]["timeout_seconds"] == 11
    assert update_payload["config"]["search_depth"] == "fast"
    assert update_payload["config"]["has_api_key"] is True


def test_patch_updates_observability_and_preserves_secret_masking(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "observability",
            "data": {
                "enabled": True,
                "mode": "langfuse",
                "local": {
                    "retention_days": 14,
                },
                "langfuse": {
                    "public_key": "pk-test",
                    "secret_key": "sk-test",
                    "base_url": "https://langfuse.example",
                    "debug": True,
                },
            },
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["section"] == "observability"
    assert payload["config"]["enabled"] is True
    assert payload["config"]["mode"] == "langfuse"
    assert payload["config"]["local"] == {"retention_days": 14}
    assert "queue_size" not in payload["config"]
    assert "flush_interval_seconds" not in payload["config"]
    assert payload["config"]["langfuse"]["public_key"] == "pk-test"
    assert payload["config"]["langfuse"]["has_secret_key"] is True
    assert "secret_key" not in payload["config"]["langfuse"]

    update_response = client.patch(
        "/api/system-config",
        json={
            "section": "observability",
            "data": {
                "enabled": False,
                "mode": "langfuse",
                "langfuse": {
                    "public_key": "pk-updated",
                    "base_url": "https://langfuse.updated",
                    "debug": False,
                },
            },
        },
        headers=headers,
    )

    assert update_response.status_code == 200
    update_payload = update_response.get_json()
    assert update_payload["config"]["enabled"] is False
    assert update_payload["config"]["langfuse"]["public_key"] == "pk-updated"
    assert update_payload["config"]["langfuse"]["has_secret_key"] is True

    clear_response = client.patch(
        "/api/system-config",
        json={
            "section": "observability",
            "data": {
                "clear_langfuse_secret_key": True,
            },
        },
        headers=headers,
    )

    assert clear_response.status_code == 200
    assert clear_response.get_json()["config"]["langfuse"]["has_secret_key"] is False


def test_patch_rejects_removed_fields(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "task_llm",
            "data": {
                "max_tokens": 256,
                "temperature": 0.1,
            },
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert "不支持字段" in response.get_json()["detail"]


def test_patch_rejects_internal_observability_fields(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "observability",
            "data": {
                "queue_size": 64,
                "local": {
                    "file_name": "runtime_perf.log",
                },
            },
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert "不支持字段" in response.get_json()["detail"]


def test_user_can_view_but_cannot_modify_system_config(tmp_path, monkeypatch):
    client, _headers = _build_client(tmp_path, monkeypatch)
    user_token = _issue_token(tmp_path, "user1", "user")
    user_headers = {"Authorization": f"Bearer {user_token}"}

    get_response = client.get("/api/system-config", headers=user_headers)
    assert get_response.status_code == 200

    patch_response = client.patch(
        "/api/system-config",
        json={"section": "task_llm", "data": {"timeout": 123}},
        headers=user_headers,
    )
    assert patch_response.status_code == 403


def test_new_llm_profile_does_not_inherit_legacy_or_other_profile_api_key(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    legacy_response = client.patch(
        "/api/system-config",
        json={
            "section": "llm",
            "data": {
                "base_url": "https://legacy.example/v1",
                "model": "legacy-model",
                "api_key": "legacy-secret",
            },
        },
        headers=headers,
    )
    assert legacy_response.status_code == 200

    first_profile_response = client.patch(
        "/api/system-config",
        json={
            "section": "llm_profiles",
            "data": {
                "default_profile_id": "p1",
                "profiles": [
                    {
                        "id": "p1",
                        "name": "Model P1",
                        "provider": "openai_compatible",
                        "base_url": "https://p1.example/v1",
                        "model": "p1-model",
                        "api_key": "p1-secret",
                        "timeout": 600,
                        "max_tokens": 4096,
                        "temperature": 0.7,
                        "enabled": True,
                    }
                ],
            },
        },
        headers=headers,
    )
    assert first_profile_response.status_code == 200

    second_profile_response = client.patch(
        "/api/system-config",
        json={
            "section": "llm_profiles",
            "data": {
                "default_profile_id": "p1",
                "profiles": [
                    {
                        "id": "p1",
                        "name": "Model P1",
                        "provider": "openai_compatible",
                        "base_url": "https://p1.example/v1",
                        "model": "p1-model",
                        "timeout": 600,
                        "max_tokens": 4096,
                        "temperature": 0.7,
                        "enabled": True,
                    },
                    {
                        "id": "p2",
                        "name": "Model P2",
                        "provider": "openai_compatible",
                        "base_url": "https://p2.example/v1",
                        "model": "p2-model",
                        "timeout": 600,
                        "max_tokens": 4096,
                        "temperature": 0.7,
                        "enabled": True,
                    },
                ],
            },
        },
        headers=headers,
    )
    assert second_profile_response.status_code == 200

    payload = second_profile_response.get_json()
    profiles = payload["config"]["profiles"]
    profile_map = {item["id"]: item for item in profiles}

    # 既有 profile 在未传 api_key 时应继续保留已保存密钥。
    assert profile_map["p1"]["has_api_key"] is True
    # 新增且未传 api_key 的 profile 不应继承 legacy 或其他 profile 的密钥。
    assert profile_map["p2"]["has_api_key"] is False
