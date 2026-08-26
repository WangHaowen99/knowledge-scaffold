import hashlib

from fastapi.testclient import TestClient

from api.testing import TestApplication
import json

import pytest

from api.routes.system_config import system_config_router
from auth_test_backend import AuthManager


def _build_client(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_dir / "system_config.json")

    app = TestApplication()
    app.state.services["TESTING"] = True
    app.state.services["REFRESH_RUNTIME_SERVICES"] = lambda asgi_app: None

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    admin = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert admin is not None
    admin = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")

    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(system_config_router)
    client = TestClient(app)
    token = auth_manager.issue_token(admin)
    headers = {"Authorization": f"Bearer {token}"}
    return client, headers


def _issue_token(tmp_path, username, role):
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.create_user(username, "UserStrong123!", role=role)
    user = auth_manager.change_password(username, "UserStrong123!", "ChangedStrong123!")
    return auth_manager.issue_token(user)


def _persisted_profile(index):
    return {
        "id": "profile-{}".format(index),
        "name": "Profile {}".format(index),
        "provider": "openai_compatible",
        "base_url": "https://llm.example/v1",
        "model": "model-{}".format(index),
        "api_key": "",
        "timeout": 600,
        "max_tokens": 4096,
        "temperature": 0.7,
        "enabled": True,
    }


def test_get_system_config_hides_secrets(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.get("/api/system-config", headers=headers)

    assert response.status_code == 200
    payload = response.json()
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
    assert response.json() == {
        "code": "SYSTEM_CONFIG_SECRET_MIGRATION_REQUIRED",
        "detail": "系统配置包含明文 secret，需要先完成安全迁移",
    }
    assert "plaintext-api-secret" not in response.text
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    "document",
    [
        {
            "llm_profiles": {
                "profiles": [
                    {"id": "bad id", "provider": "openai_compatible"},
                ]
            }
        },
        {
            "llm_profiles": {
                "profiles": [
                    {"id": "valid", "provider": "private-unsupported-provider"},
                ]
            }
        },
        {"web_search": {"provider": "private-unsupported-provider"}},
        {"skill_evolution": {"max_iterations": 99}},
    ],
    ids=(
        "profile-id",
        "profile-provider",
        "web-search-provider",
        "skill-evolution-max-iterations",
    ),
)
def test_get_system_config_rejects_semantically_invalid_persisted_config(
    tmp_path,
    monkeypatch,
    document,
):
    import config.system_config as system_config_module

    client, headers = _build_client(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_PROFILE_MAX_COUNT", "10")
    config_path = tmp_path / "config" / "system_config.json"
    config_path.write_text(json.dumps(document), encoding="utf-8")
    original = config_path.read_bytes()

    for loader in (
        system_config_module.load_raw_system_config,
        system_config_module.load_system_config,
    ):
        with pytest.raises(system_config_module.SystemConfigReadError) as exc_info:
            loader()
        assert exc_info.value.code == "SYSTEM_CONFIG_INVALID"

    response = client.get("/api/system-config", headers=headers)
    readiness_response = client.get("/api/system-config/readiness", headers=headers)

    for read_response in (response, readiness_response):
        assert read_response.status_code == 409
        assert read_response.json()["code"] == "SYSTEM_CONFIG_INVALID"
        assert "private-unsupported" not in read_response.text
        assert "bad id" not in read_response.text
    assert config_path.read_bytes() == original


@pytest.mark.parametrize(
    ("persisted_web_search", "environment"),
    [
        (
            {"default_max_results": 20},
            {"WEB_SEARCH_MAX_RESULTS_LIMIT": "30"},
        ),
        (
            {"max_results_limit": 30},
            {"WEB_SEARCH_DEFAULT_MAX_RESULTS": "20"},
        ),
    ],
    ids=("environment-fills-limit", "environment-fills-default"),
)
def test_web_search_cross_field_validation_uses_effective_precedence(
    tmp_path,
    monkeypatch,
    persisted_web_search,
    environment,
):
    import config.system_config as system_config_module

    client, headers = _build_client(tmp_path, monkeypatch)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    config_path = tmp_path / "config" / "system_config.json"
    config_path.write_text(
        json.dumps({"web_search": persisted_web_search}),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    raw = system_config_module.load_raw_system_config()
    effective = system_config_module.load_system_config()
    response = client.get("/api/system-config", headers=headers)
    readiness_response = client.get("/api/system-config/readiness", headers=headers)

    assert raw["web_search"] == persisted_web_search
    assert effective["web_search"]["default_max_results"] == 20
    assert effective["web_search"]["max_results_limit"] == 30
    assert response.status_code == 200
    assert readiness_response.status_code == 200
    assert config_path.read_bytes() == original


def test_reads_allow_persisted_profiles_above_write_admission_limit(
    tmp_path,
    monkeypatch,
):
    import config.system_config as system_config_module

    client, headers = _build_client(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_PROFILE_MAX_COUNT", "10")
    profiles = [_persisted_profile(index) for index in range(11)]
    config_path = tmp_path / "config" / "system_config.json"
    config_path.write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "default_main_profile_ref": {
                        "scope": "global",
                        "id": "profile-0",
                    },
                    "profiles": profiles,
                }
            }
        ),
        encoding="utf-8",
    )
    original = config_path.read_bytes()

    raw = system_config_module.load_raw_system_config()
    effective = system_config_module.load_system_config()
    response = client.get("/api/system-config", headers=headers)
    readiness_response = client.get("/api/system-config/readiness", headers=headers)

    assert len(raw["llm_profiles"]["profiles"]) == 11
    assert len(effective["llm_profiles"]["profiles"]) == 11
    assert response.status_code == 200
    assert len(response.json()["llm_profiles"]["profiles"]) == 11
    assert readiness_response.status_code == 200
    assert readiness_response.json()["ready"] is True
    assert config_path.read_bytes() == original


def test_over_limit_profiles_remain_manageable_but_cannot_grow(
    tmp_path,
    monkeypatch,
):
    client, headers = _build_client(tmp_path, monkeypatch)
    monkeypatch.setenv("MODEL_PROFILE_MAX_COUNT", "10")
    profiles = [_persisted_profile(index) for index in range(11)]
    config_path = tmp_path / "config" / "system_config.json"
    config_path.write_text(
        json.dumps({"llm_profiles": {"profiles": profiles}}),
        encoding="utf-8",
    )

    unrelated_update = client.patch(
        "/api/system-config",
        json={"section": "web_search", "data": {"enabled": True}},
        headers=headers,
    )
    same_count_profiles = [dict(profile) for profile in profiles]
    same_count_profiles[0]["name"] = "Updated Profile"
    same_count_update = client.patch(
        "/api/system-config",
        json={
            "section": "llm_profiles",
            "data": {"profiles": same_count_profiles},
        },
        headers=headers,
    )
    growth_import = client.post(
        "/api/system-config/import",
        json={
            "config": {
                "llm_profiles": {
                    "profiles": [*same_count_profiles, _persisted_profile(11)],
                }
            }
        },
        headers=headers,
    )
    reduction_update = client.patch(
        "/api/system-config",
        json={
            "section": "llm_profiles",
            "data": {"profiles": same_count_profiles[:10]},
        },
        headers=headers,
    )

    assert unrelated_update.status_code == 200
    assert same_count_update.status_code == 200
    assert len(same_count_update.json()["config"]["profiles"]) == 11
    assert growth_import.status_code == 400
    assert reduction_update.status_code == 200
    assert len(reduction_update.json()["config"]["profiles"]) == 10


def test_load_system_config_sanitizes_effective_normalization_errors(
    tmp_path,
    monkeypatch,
):
    import config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    def fail_normalization(_section):
        raise system_config_module.SystemConfigValidationError(
            "private-normalization-detail"
        )

    monkeypatch.setattr(
        system_config_module,
        "_normalize_web_search_effective",
        fail_normalization,
    )

    with pytest.raises(system_config_module.SystemConfigReadError) as exc_info:
        system_config_module.load_system_config()

    assert exc_info.value.code == "SYSTEM_CONFIG_INVALID"
    assert "private-normalization-detail" not in str(exc_info.value)
    assert config_path.read_text(encoding="utf-8") == "{}\n"


def test_get_system_config_readiness_is_available_to_authenticated_users(tmp_path, monkeypatch):
    client, _headers = _build_client(tmp_path, monkeypatch)
    user_token = _issue_token(tmp_path, "user1", "user")
    user_headers = {"Authorization": f"Bearer {user_token}"}

    response = client.get("/api/system-config/readiness", headers=user_headers)

    assert response.status_code == 200
    payload = response.json()
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
    payload = response.json()
    assert payload["section"] == "llm"
    assert payload["config"]["base_url"] == "https://example.com/v1"
    assert payload["config"]["model"] == "test-model"
    assert payload["config"]["has_api_key"] is True
    assert "api_key" not in payload["config"]

    fetch_response = client.get("/api/system-config", headers=headers)
    fetch_payload = fetch_response.json()
    assert fetch_payload["llm"]["has_api_key"] is True
    assert fetch_payload["llm"]["timeout"] == 120


def test_template_and_import_round_trip(tmp_path, monkeypatch):
    client, headers = _build_client(tmp_path, monkeypatch)

    template_response = client.get("/api/system-config/template", headers=headers)
    assert template_response.status_code == 200
    template_payload = template_response.json()
    assert template_payload["agent"]["main_agent_max_iterations"] == 50
    assert template_payload["agent"]["sse_max_subscribers_per_run"] == 3
    assert template_payload["agent"]["sse_max_subscribers_total"] == 100
    assert template_payload["agent"]["short_io_operation_timeout_seconds"] == 30
    assert template_payload["agent"]["external_io_operation_timeout_seconds"] == 300
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
    payload = import_response.json()
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
    payload = response.json()
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
    update_payload = update_response.json()
    assert update_payload["config"]["timeout_seconds"] == 11
    assert update_payload["config"]["search_depth"] == "fast"
    assert update_payload["config"]["has_api_key"] is True


@pytest.mark.parametrize(
    ("method", "payload"),
    [
        (
            "patch",
            {
                "section": "web_search",
                "data": {"provider": "unsupported"},
            },
        ),
        (
            "post",
            {
                "config": {
                    "skill_evolution": {"max_iterations": 99},
                }
            },
        ),
    ],
    ids=("patch", "import"),
)
def test_explicit_writes_keep_invalid_payload_contract_at_400(
    tmp_path,
    monkeypatch,
    method,
    payload,
):
    client, headers = _build_client(tmp_path, monkeypatch)
    path = "/api/system-config" if method == "patch" else "/api/system-config/import"

    response = client.request(method.upper(), path, json=payload, headers=headers)

    assert response.status_code == 400
    assert "SYSTEM_CONFIG_INVALID" not in response.text


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
    assert "section 非法" in response.json()["detail"]


def test_patch_rejects_process_subscriber_limit_below_run_limit(
    tmp_path,
    monkeypatch,
):
    client, headers = _build_client(tmp_path, monkeypatch)

    response = client.patch(
        "/api/system-config",
        json={
            "section": "agent",
            "data": {
                "sse_max_subscribers_per_run": 4,
                "sse_max_subscribers_total": 3,
            },
        },
        headers=headers,
    )

    assert response.status_code == 400
    assert (
        response.json()["detail"]
        == "sse_max_subscribers_total 不能小于 sse_max_subscribers_per_run"
    )


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
    assert "section 非法" in response.json()["detail"]


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

    payload = second_profile_response.json()
    profiles = payload["config"]["profiles"]
    profile_map = {item["id"]: item for item in profiles}

    # 既有 profile 在未传 api_key 时应继续保留已保存密钥。
    assert profile_map["p1"]["has_api_key"] is True
    # 新增且未传 api_key 的 profile 不应继承 legacy 或其他 profile 的密钥。
    assert profile_map["p2"]["has_api_key"] is False
