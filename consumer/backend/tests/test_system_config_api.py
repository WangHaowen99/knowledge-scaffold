import hashlib
import json

from flask import Flask

from api.routes.system_config import system_config_bp
from api.auth.auth import AuthManager


def _build_client(tmp_path, monkeypatch):
    import config.system_config as system_config_module

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
    assert "ontologies_root" not in payload["storage"]
    assert "skills_root" not in payload["storage"]
    assert payload["auth"]["has_auth_secret"] is False
    assert payload["llm_profiles"]["profiles"] == []
    assert "observability" not in payload
    assert payload["web_search"]["enabled"] is False
    assert payload["web_search"]["provider"] == "tavily"
    assert payload["web_search"]["has_api_key"] is False
    assert "api_key" not in payload["web_search"]


def test_get_readiness_and_template_leave_config_tree_unchanged(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)
    response = client.patch(
        "/api/system-config",
        json={
            "section": "llm",
            "data": {
                "base_url": "https://example.com/v1",
                "model": "test-model",
                "api_key": "read-only-api-secret",
            },
        },
        headers=headers,
    )
    assert response.status_code == 200

    def snapshot():
        return {
            str(path.relative_to(tmp_path)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in sorted(tmp_path.rglob("*"))
            if path.is_file()
        }

    before = snapshot()
    assert client.get("/api/system-config", headers=headers).status_code == 200
    assert client.get("/api/system-config/readiness", headers=headers).status_code == 200
    assert client.get("/api/system-config/template", headers=headers).status_code == 200
    assert snapshot() == before


def test_get_system_config_returns_sanitized_migration_required_error(
    tmp_path,
    monkeypatch,
):
    client, headers = _build_client(tmp_path, monkeypatch)
    config_path = tmp_path / "config" / "system_config.json"
    config_path.write_text(
        json.dumps({"llm": {"api_key": "plaintext-api-secret"}}),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    response = client.get("/api/system-config", headers=headers)

    assert response.status_code == 409
    assert response.get_json() == {
        "code": "SYSTEM_CONFIG_SECRET_MIGRATION_REQUIRED",
        "detail": "系统配置包含明文 secret，需要先完成安全迁移",
    }
    assert "plaintext-api-secret" not in response.get_data(as_text=True)
    assert config_path.read_bytes() == original


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
    assert template_payload["auth"]["auth_secret"] == ""
    assert template_payload["llm"]["base_url"] == ""
    assert template_payload["llm"]["model"] == ""
    assert template_payload["llm_profiles"]["default_main_profile_ref"] is None
    assert template_payload["llm_profiles"]["default_task_profile_ref"] is None
    assert "ontologies_root" not in template_payload["storage"]
    assert "skills_root" not in template_payload["storage"]
    assert template_payload["llm_profiles"]["profiles"] == []
    assert "observability" not in template_payload
    assert template_payload["web_search"]["enabled"] is False
    assert template_payload["web_search"]["provider"] == "tavily"
    assert template_payload["web_search"]["api_key"] == ""

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


def test_patch_rejects_global_observability_section(tmp_path, monkeypatch):
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

    assert response.status_code == 400
    assert "section 非法" in response.get_json()["detail"]


def test_system_config_connection_test_rejects_global_observability_section(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.post(
        "/api/system-config/test",
        json={
            "section": "observability",
            "data": {
                "enabled": True,
            },
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert "section 非法" in response.get_json()["detail"]


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
