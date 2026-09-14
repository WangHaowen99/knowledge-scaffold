"""Tests for process-wide LLM routing."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace

from backend_synthetic_credentials import synthetic_secret
from config import AgentConfig, AppConfig
from config.workspace_llm_profiles import update_workspace_llm_profiles
from llm import LLMPurpose, LLMRouter
from llm.service import LLMService
from system_config_migration_helpers import apply_test_system_config_migrations
from workspace.workspaces import create_workspace


def _agent_config() -> AgentConfig:
    return AgentConfig(
        llm_connection_max_retries=1,
        main_agent_max_iterations=3,
        sub_agent_max_iterations=2,
        sub_agent_max_concurrency=2,
        sub_agent_max_depth=2,
        parallel_default_max_workers=2,
        context_max_rounds=5,
    )


def _write_system_config(tmp_path, monkeypatch):
    import config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        json.dumps(
            {
                "llm_profiles": {
                    "default_profile_id": "fast",
                    "profiles": [
                        {
                            "id": "fast",
                            "name": "Fast Model",
                            "provider": "openai_compatible",
                            "base_url": "https://fast.example.net/v1",
                            "model": "fast-model",
                            "api_key": synthetic_secret("llm_router-credential-02"),
                            "enabled": True,
                        },
                        {
                            "id": "accurate",
                            "name": "Accurate Model",
                            "provider": "deepseek",
                            "base_url": "https://accurate.example.net/v1",
                            "model": "accurate-model",
                            "api_key": synthetic_secret("llm_router-credential-01"),
                            "enabled": True,
                        },
                    ],
                },
                "task_llm": {
                    "base_url": "https://task.example.net/v1",
                    "model": "task-model",
                    "api_key": synthetic_secret("llm_router-credential-04"),
                    "timeout": 45,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    apply_test_system_config_migrations(config_path, monkeypatch)


def test_main_uses_requested_profile(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    config = router.get_config(LLMPurpose.MAIN, profile_id="accurate")

    assert config.profile_id == "global:accurate"
    assert config.provider == "deepseek"
    assert config.model == "accurate-model"


def test_task_and_summary_use_task_config_memory_uses_main_fast_config(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    task_config = router.get_config(LLMPurpose.TASK)
    memory_config = router.get_config(LLMPurpose.MEMORY)

    assert task_config.model == "task-model"
    assert router.get_config(LLMPurpose.SUMMARY).model == task_config.model
    assert memory_config.profile_id == "global:fast"
    assert memory_config.model == "fast-model"


def test_main_can_use_workspace_private_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_system_config(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
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
                    "api_key": synthetic_secret("llm_router-credential-03"),
                    "enabled": True,
                }
            ]
        },
    )
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    config = router.get_config(LLMPurpose.MAIN, profile_id="workspace:private", workspace_id="alpha")

    assert config.profile_id == "workspace:private"
    assert config.profile_name == "Private Model"
    assert config.model == "private-model"
    assert config.api_key == synthetic_secret("llm_router-credential-03")


def test_main_without_profile_uses_workspace_default_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_system_config(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
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
                    "api_key": synthetic_secret("llm_router-credential-03"),
                    "enabled": True,
                }
            ]
        },
    )
    from workspace.workspaces import update_workspace_runtime_config

    update_workspace_runtime_config(
        "alpha",
        {"llm": {"default_main_profile_ref": {"scope": "workspace", "id": "private"}}},
    )
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    config = router.get_config(LLMPurpose.MAIN, workspace_id="alpha")

    assert config.profile_id == "workspace:private"
    assert config.model == "private-model"
    assert config.api_key == synthetic_secret("llm_router-credential-03")


def test_task_ignores_workspace_private_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    _write_system_config(tmp_path, monkeypatch)
    create_workspace("Alpha", workspace_id="alpha", set_as_default=True)
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
                    "api_key": synthetic_secret("llm_router-credential-03"),
                    "enabled": True,
                }
            ]
        },
    )
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    task_config = router.get_config(LLMPurpose.TASK, profile_id="workspace:private", workspace_id="alpha")

    assert task_config.profile_id == "global:legacy-task-llm"
    assert task_config.model == "task-model"


def test_router_caches_services_by_config(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    first = router.get_service(LLMPurpose.MAIN)
    second = router.get_service(LLMPurpose.MAIN)
    accurate = router.get_service(LLMPurpose.MAIN, profile_id="accurate")

    assert first is second
    assert accurate is not first
    assert first.summary_llm is router.get_service(LLMPurpose.SUMMARY)


def test_router_cache_key_distinguishes_each_config_field(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())
    base_config = router.get_config(LLMPurpose.SUMMARY)
    current_config = SimpleNamespace(value=base_config)

    def get_current_config(*_args, **_kwargs):
        return current_config.value

    monkeypatch.setattr(router, "get_config", get_current_config)
    base_service = router.get_service(LLMPurpose.SUMMARY)
    distinct_values = {
        "base_url": "https://alternate.example.net/v1",
        "model": "alternate-model",
        "api_key": synthetic_secret("llm-router-alternate-credential"),
        "timeout": base_config.timeout + 1,
        "max_tokens": base_config.max_tokens + 1,
        "temperature": base_config.temperature + 0.1,
        "provider": "alternate-provider",
        "profile_id": "global:alternate",
        "profile_name": "Alternate Model",
    }
    distinct_services = []

    for field_name, value in distinct_values.items():
        current_config.value = replace(base_config, **{field_name: value})
        distinct_services.append(router.get_service(LLMPurpose.SUMMARY))

    current_config.value = replace(base_config)
    assert router.get_service(LLMPurpose.SUMMARY) is base_service
    assert all(service is not base_service for service in distinct_services)
    assert len({id(service) for service in distinct_services}) == len(distinct_values)


def test_router_cache_reuses_service_for_concurrent_requests(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())
    config = router.get_config(LLMPurpose.SUMMARY)

    def get_config(*_args, **_kwargs):
        return config

    monkeypatch.setattr(router, "get_config", get_config)

    def get_summary_service(_index):
        return router.get_service(LLMPurpose.SUMMARY)

    with ThreadPoolExecutor(max_workers=8) as executor:
        services = list(executor.map(get_summary_service, range(32)))

    assert all(service is services[0] for service in services)


def test_call_routes_memory_with_defaults(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())
    calls = []

    def fake_call(self, messages, **kwargs):
        calls.append(
            {
                "model": self.config.model,
                "messages": messages,
                "kwargs": kwargs,
            }
        )
        return SimpleNamespace(content="ok")

    monkeypatch.setattr(LLMService, "call", fake_call)

    response = router.call(
        purpose=LLMPurpose.MEMORY,
        messages=[{"role": "user", "content": "remember this"}],
        request_label="memory_route",
    )

    assert response.content == "ok"
    assert calls[0]["model"] == "fast-model"
    assert calls[0]["kwargs"]["stream"] is False
    assert calls[0]["kwargs"]["enable_reasoning"] is False


def test_refreshed_router_shares_cancellation_state(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    first = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())
    second = LLMRouter(
        AppConfig.from_env(),
        agent_config=_agent_config(),
        previous_router=first,
    )

    first.cancel_run("run-shared")
    service = second.get_service(LLMPurpose.MAIN)

    event = second.get_cancel_event("run-shared")
    assert event is service.get_cancel_event_for_test("run-shared")
    assert event.is_set()
