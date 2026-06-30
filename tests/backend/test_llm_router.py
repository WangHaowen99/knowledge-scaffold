"""Tests for process-wide LLM routing."""

from __future__ import annotations

import json
from types import SimpleNamespace

from agentic.config import AgentConfig, AppConfig, LLMConfig
from agentic.services.llm import LLMPurpose, LLMRouter
from agentic.services.llm.service import LLMService


def _agent_config() -> AgentConfig:
    return AgentConfig(
        llm_connection_max_retries=1,
        main_agent_max_iterations=3,
        sub_agent_max_iterations=2,
        sub_agent_max_concurrency=2,
        sub_agent_max_depth=2,
        parallel_default_max_workers=2,
        context_max_rounds=5,
        context_prune_tools=set(),
    )


def _write_system_config(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

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
                            "base_url": "https://fast.example/v1",
                            "model": "fast-model",
                            "api_key": "fast-secret",
                            "enabled": True,
                        },
                        {
                            "id": "accurate",
                            "name": "Accurate Model",
                            "provider": "deepseek",
                            "base_url": "https://accurate.example/v1",
                            "model": "accurate-model",
                            "api_key": "accurate-secret",
                            "enabled": True,
                        },
                    ],
                },
                "task_llm": {
                    "base_url": "https://task.example/v1",
                    "model": "task-model",
                    "api_key": "task-secret",
                    "timeout": 45,
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)


def test_main_uses_requested_profile(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    config = router.get_config(LLMPurpose.MAIN, profile_id="accurate")

    assert config.profile_id == "accurate"
    assert config.provider == "deepseek"
    assert config.model == "accurate-model"


def test_task_and_summary_use_task_config_memory_uses_main_fast_config(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    task_config = router.get_config(LLMPurpose.TASK)
    memory_config = router.get_config(LLMPurpose.MEMORY)

    assert task_config.model == "task-model"
    assert router.get_config(LLMPurpose.SUMMARY) is task_config
    assert memory_config.profile_id == "fast"
    assert memory_config.model == "fast-model"


def test_router_caches_services_by_config(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())

    first = router.get_service(LLMPurpose.MAIN)
    second = router.get_service(LLMPurpose.MAIN)
    accurate = router.get_service(LLMPurpose.MAIN, profile_id="accurate")

    assert first is second
    assert accurate is not first
    assert first.summary_llm is router.get_service(LLMPurpose.SUMMARY)


def test_call_routes_memory_with_defaults(tmp_path, monkeypatch):
    _write_system_config(tmp_path, monkeypatch)
    router = LLMRouter(AppConfig.from_env(), agent_config=_agent_config())
    calls = []

    def fake_call(self, messages, **kwargs):
        calls.append({
            "model": self.config.model,
            "messages": messages,
            "kwargs": kwargs,
        })
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

    assert service._get_cancel_event("run-shared").is_set()
