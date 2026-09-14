from __future__ import annotations

from pathlib import Path

from modeling_agent.settings import Settings


def _settings(**overrides) -> Settings:
    base = dict(
        data_dir=Path("/tmp/modeling-agent-test"),
        core_base_url="http://127.0.0.1:5100",
        service_token="s" * 32,
        llm_base_url="http://127.0.0.1:9/unused",
        llm_api_key="unused",
        llm_model="scripted",
        llm_timeout_seconds=5,
        llm_max_tokens=32_768,
        max_iterations=20,
        host="127.0.0.1",
        port=5110,
        skills_dir=Path("/tmp/modeling-agent-skills"),
    )
    base.update(overrides)
    return Settings(**base)


def test_phase_budget_is_unchanged_without_thinking_mode() -> None:
    settings = _settings()

    assert settings.resolved_output_budget(settings.script_output_tokens) == 8_192
    assert settings.resolved_output_budget(settings.summary_output_tokens) == 1_024


def test_thinking_mode_adds_reasoning_headroom_to_phase_budget() -> None:
    # 推理内容与正文、工具参数共用 max_tokens：只给 8192 时推理会把预算吃光，
    # 正文和工具调用一个 token 都吐不出来。
    settings = _settings(llm_thinking_mode="enabled")

    assert settings.resolved_output_budget(settings.script_output_tokens) == 24_576
    assert settings.resolved_output_budget(settings.summary_output_tokens) == 17_408


def test_reasoning_headroom_never_exceeds_deployment_limit() -> None:
    settings = _settings(llm_thinking_mode="enabled", llm_max_tokens=10_000)

    assert settings.resolved_output_budget(8_192) == 10_000


def test_missing_budget_falls_back_to_deployment_limit() -> None:
    settings = _settings(llm_thinking_mode="enabled")

    assert settings.resolved_output_budget(None) == 32_768


def test_runtime_llm_config_keeps_reasoning_headroom() -> None:
    # 平台下发的运行时配置只覆盖模型与上限，不应该把余量配置丢掉。
    settings = _settings(reasoning_headroom_tokens=4_096).with_runtime_llm({
        "baseUrl": "http://127.0.0.1:9/runtime",
        "apiKey": "unused",
        "model": "runtime-model",
        "maxTokens": 16_384,
        "thinkingMode": "enabled",
    })

    assert settings.reasoning_headroom_tokens == 4_096
    assert settings.resolved_output_budget(8_192) == 12_288
