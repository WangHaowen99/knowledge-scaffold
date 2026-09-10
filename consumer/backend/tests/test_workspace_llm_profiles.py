import json

import pytest
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
from system_config_migration_helpers import apply_test_system_config_migrations

SECRET_TEST_KEY = ENCRYPTION_KEY


def _build_workspace_routes_client(tmp_path):
    app = TestApplication()
    app.state.services["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
    assert user is not None
    user = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, "RootStrong123!")
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(workspaces_router)
    client = TestClient(app)
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def _isolate_runtime(tmp_path, monkeypatch):
    import config.system_config as system_config_module
    import workspace.workspaces as workspaces_module

    runtime_root = tmp_path / ".onto-runtime"
    config_dir = runtime_root / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_dir / "system_config.json")
    return runtime_root, workspaces_module


def _write_global_profiles(runtime_root, monkeypatch):
    config_path = runtime_root / "config" / "system_config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "default_profile_id": "fast",
                    "profiles": [
                        {
                            "id": "fast",
                            "name": "Fast Global",
                            "provider": "openai_compatible",
                            "base_url": "https://fast.example.net/v1",
                            "model": "fast-model",
                            "api_key": synthetic_secret("workspace_llm_profiles-credential-02"),
                            "enabled": True,
                        },
                        {
                            "id": "accurate",
                            "name": "Accurate Global",
                            "provider": "deepseek",
                            "base_url": "https://accurate.example.net/v1",
                            "model": "accurate-model",
                            "api_key": synthetic_secret("workspace_llm_profiles-credential-01"),
                            "enabled": True,
                        },
                    ],
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    apply_test_system_config_migrations(
        config_path,
        monkeypatch,
        runtime_root=runtime_root,
    )


def test_model_profile_ref_parses_legacy_and_scoped_ids():
    from config.model_profile_refs import (
        ModelProfileRef,
        encode_model_profile_ref,
        parse_model_profile_ref,
    )

    assert parse_model_profile_ref("fast") == ModelProfileRef(scope="global", id="fast")
    assert parse_model_profile_ref("vendor:model") == ModelProfileRef(
        scope="global",
        id="vendor:model",
    )
    assert parse_model_profile_ref("global:fast") == ModelProfileRef(scope="global", id="fast")
    assert parse_model_profile_ref("workspace:fast") == ModelProfileRef(scope="workspace", id="fast")
    assert parse_model_profile_ref({"scope": "workspace", "id": "fast"}) == ModelProfileRef(
        scope="workspace",
        id="fast",
    )
    assert encode_model_profile_ref(ModelProfileRef(scope="workspace", id="fast")) == "workspace:fast"

    assert parse_model_profile_ref("team:fast") == ModelProfileRef(
        scope="global",
        id="team:fast",
    )


def test_workspace_private_profiles_encrypt_and_publicize_secrets(tmp_path, monkeypatch):
    _runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)

    from config.workspace_llm_profiles import (
        get_public_workspace_llm_profiles,
        load_workspace_llm_profiles,
        update_workspace_llm_profiles,
    )

    update_workspace_llm_profiles(
        "alpha",
        {
            "profiles": [
                {
                    "id": "private",
                    "name": "Private Model",
                    "provider": "openai_compatible",
                    "base_url": "https://private.example.net/v1",
                    "model": "private-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-05"),
                    "enabled": True,
                }
            ]
        },
    )

    raw_path = workspaces_module.get_workspace_paths("alpha").workspace_dir / "llm_profiles.json"
    raw_text = raw_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert synthetic_secret("workspace_llm_profiles-credential-05") not in raw_text
    assert raw["profiles"][0]["api_key"].startswith("enc:v1:")

    private_config = load_workspace_llm_profiles("alpha")
    assert private_config["profiles"][0]["api_key"] == synthetic_secret("workspace_llm_profiles-credential-05")

    public_profiles = get_public_workspace_llm_profiles("alpha")
    assert public_profiles == [
        {
            "id": "private",
            "name": "Private Model",
            "provider": "openai_compatible",
            "model": "private-model",
            "enabled": True,
            "has_api_key": True,
            "base_url": "https://private.example.net/v1",
            "timeout": 600,
            "max_tokens": 32768,
            "temperature": 0.7,
        }
    ]
    assert "api_key" not in public_profiles[0]


def test_workspace_private_profiles_reject_colon_ids_on_save(tmp_path, monkeypatch):
    _runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)

    from config.workspace_llm_profiles import update_workspace_llm_profiles

    with pytest.raises(Exception, match="不能包含冒号"):
        update_workspace_llm_profiles(
            "alpha",
            {
                "profiles": [
                    {
                        "id": "vendor:model",
                        "name": "Private Model",
                        "provider": "openai_compatible",
                        "base_url": "https://private.example.net/v1",
                        "model": "private-model",
                        "api_key": synthetic_secret("workspace_llm_profiles-credential-05"),
                    }
                ]
            },
        )


def test_workspace_private_profiles_generate_missing_ids_on_save(tmp_path, monkeypatch):
    _runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)

    from config.workspace_llm_profiles import update_workspace_llm_profiles

    payload = update_workspace_llm_profiles(
        "alpha",
        {
            "profiles": [
                {
                    "name": "Private A",
                    "provider": "openai_compatible",
                    "base_url": "https://private-a.example.net/v1",
                    "model": "private-a-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-03"),
                },
                {
                    "name": "Private B",
                    "provider": "deepseek",
                    "base_url": "https://private-b.example.net/v1",
                    "model": "private-b-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-04"),
                },
            ]
        },
    )

    profile_ids = [profile["id"] for profile in payload["profiles"]]
    assert len(profile_ids) == 2
    assert len(set(profile_ids)) == 2
    assert all(profile_id.startswith("workspace_model_") for profile_id in profile_ids)
    assert "default" not in profile_ids
    assert [profile["model"] for profile in payload["profiles"]] == [
        "private-a-model",
        "private-b-model",
    ]


def test_workspace_runtime_config_accepts_default_profile_ref(tmp_path, monkeypatch):
    _runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)

    config = workspaces_module.update_workspace_runtime_config(
        "alpha",
        {
            "llm": {
                "default_main_profile_ref": {
                    "scope": "workspace",
                    "id": "private",
                }
            }
        },
    )

    assert config["llm"]["default_main_profile_ref"] == {
        "scope": "workspace",
        "id": "private",
    }

    with pytest.raises(ValueError, match="scope"):
        workspaces_module.update_workspace_runtime_config(
            "alpha",
            {"llm": {"default_main_profile_ref": {"scope": "team", "id": "private"}}},
        )


def test_effective_profiles_merge_global_and_workspace_private(tmp_path, monkeypatch):
    runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    _write_global_profiles(runtime_root, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    workspaces_module.create_workspace("Beta", workspace_id="beta")

    from config.system_config import (
        get_public_llm_profiles,
        resolve_effective_llm_profile_config,
    )
    from config.workspace_llm_profiles import update_workspace_llm_profiles

    update_workspace_llm_profiles(
        "alpha",
        {
            "profiles": [
                {
                    "id": "private-a",
                    "name": "Private A",
                    "provider": "openai_compatible",
                    "base_url": "https://private-a.example.net/v1",
                    "model": "private-a-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-03"),
                    "enabled": True,
                }
            ]
        },
    )
    update_workspace_llm_profiles(
        "beta",
        {
            "profiles": [
                {
                    "id": "private-b",
                    "name": "Private B",
                    "provider": "openai_compatible",
                    "base_url": "https://private-b.example.net/v1",
                    "model": "private-b-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-04"),
                    "enabled": True,
                }
            ]
        },
    )
    workspaces_module.update_workspace_runtime_config(
        "alpha",
        {"llm": {"default_main_profile_ref": {"scope": "workspace", "id": "private-a"}}},
    )

    alpha_profiles = get_public_llm_profiles(workspace_id="alpha")
    beta_profiles = get_public_llm_profiles(workspace_id="beta")

    assert [profile["id"] for profile in alpha_profiles["profiles"]] == [
        "global:fast",
        "global:accurate",
        "workspace:private-a",
    ]
    assert alpha_profiles["default_profile_id"] == "workspace:private-a"
    assert alpha_profiles["default_main_profile_ref"] == {"scope": "workspace", "id": "private-a"}
    assert [profile["id"] for profile in beta_profiles["profiles"]] == [
        "global:fast",
        "global:accurate",
        "workspace:private-b",
    ]
    assert beta_profiles["default_profile_id"] == "global:fast"

    private_config = resolve_effective_llm_profile_config("workspace:private-a", workspace_id="alpha")
    assert private_config["id"] == "workspace:private-a"
    assert private_config["profile_id"] == "private-a"
    assert private_config["model"] == "private-a-model"
    assert private_config["api_key"] == synthetic_secret("workspace_llm_profiles-credential-03")

    legacy_global = resolve_effective_llm_profile_config("fast", workspace_id="alpha")
    assert legacy_global["id"] == "global:fast"
    assert legacy_global["profile_id"] == "fast"
    assert legacy_global["model"] == "fast-model"

    with pytest.raises(Exception, match="private-a"):
        resolve_effective_llm_profile_config("workspace:private-a", workspace_id="beta")


def test_workspace_llm_profiles_routes_manage_private_profiles(tmp_path, monkeypatch):
    _runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    client, headers = _build_workspace_routes_client(tmp_path)

    empty_response = client.get("/agentic/workspaces/alpha/llm-profiles", headers=headers)
    assert empty_response.status_code == 200
    assert empty_response.json() == {"profiles": []}

    response = client.patch(
        "/agentic/workspaces/alpha/llm-profiles",
        json={
            "profiles": [
                {
                    "id": "private",
                    "name": "Private Model",
                    "provider": "openai_compatible",
                    "base_url": "https://private.example.net/v1",
                    "model": "private-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-05"),
                    "enabled": True,
                }
            ]
        },
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["profiles"][0]["id"] == "private"
    assert payload["profiles"][0]["has_api_key"] is True
    assert "api_key" not in payload["profiles"][0]


def test_workspace_llm_profiles_test_route_uses_workspace_private_payload(tmp_path, monkeypatch):
    _runtime_root, workspaces_module = _isolate_runtime(tmp_path, monkeypatch)
    workspaces_module.create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
    client, headers = _build_workspace_routes_client(tmp_path)
    calls = []

    class _FakeResponse:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def raise_for_status(self):
            return None

    def fake_post(url, *, headers=None, timeout=None, **kwargs):
        request_json = kwargs.get("json")
        calls.append(
            {
                "url": url,
                "headers": headers or {},
                "json": request_json or {},
                "timeout": timeout,
            }
        )
        return _FakeResponse()

    monkeypatch.setattr("config.system_config.requests.post", fake_post)

    response = client.post(
        "/agentic/workspaces/alpha/llm-profiles/test",
        headers=headers,
        json={
            "profile_id": "private",
            "profiles": [
                {
                    "id": "private",
                    "name": "Private Model",
                    "provider": "openai_compatible",
                    "base_url": "https://private.example.net/v1",
                    "model": "private-model",
                    "api_key": synthetic_secret("workspace_llm_profiles-credential-05"),
                    "enabled": True,
                }
            ],
        },
    )

    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == [
        {
            "url": "https://private.example.net/v1/chat/completions",
            "headers": {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {synthetic_secret('workspace_llm_profiles-credential-05')}",
            },
            "json": {
                "model": "private-model",
                "messages": [{"role": "user", "content": "Reply with OK only."}],
                "temperature": 0,
                "max_tokens": 1,
                "stream": False,
            },
            "timeout": 15,
        }
    ]
