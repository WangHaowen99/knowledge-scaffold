import config.settings as settings
import config.system_config as system_config
from config.settings import AgentConfig


def _base_agent_config() -> AgentConfig:
    return AgentConfig(
        llm_connection_max_retries=3,
        main_agent_max_iterations=50,
        sub_agent_max_iterations=20,
        sub_agent_max_concurrency=6,
        sub_agent_max_depth=10,
        parallel_default_max_workers=10,
        context_max_rounds=10,
    )


def _effective_agent_config(**overrides):
    effective = {
        name: spec["default"]
        for name, spec in system_config._SECTION_FIELDS["agent"].items()
    }
    effective.update(overrides)
    return effective


def test_runtime_warmup_env_fallback_can_disable_default(tmp_path, monkeypatch):
    config_path = tmp_path / "system_config.json"
    monkeypatch.setattr(system_config, "CONFIG_PATH", config_path)
    monkeypatch.setattr(system_config, "CONFIG_DIR", config_path.parent)
    monkeypatch.setenv("AGENT_RUNTIME_WARMUP_ENABLED", "false")

    config = system_config.load_system_config()

    assert config["agent"]["runtime_warmup_enabled"] is False


def test_agent_config_defaults_warmup_on_for_legacy_mapping(monkeypatch):
    legacy_agent = _effective_agent_config()
    legacy_agent.pop("runtime_warmup_enabled")
    monkeypatch.setattr(settings, "load_system_config", lambda: {"agent": legacy_agent})
    monkeypatch.setattr(
        settings.DirectExecutionConfig,
        "from_env",
        classmethod(lambda cls: settings.DirectExecutionConfig()),
    )
    monkeypatch.setattr(
        settings.WebSearchConfig,
        "from_env",
        classmethod(lambda cls: settings.WebSearchConfig()),
    )
    monkeypatch.setattr(
        settings.LimitsConfig,
        "from_env",
        classmethod(lambda cls: settings.LimitsConfig()),
    )

    config = AgentConfig.from_env()

    assert config.runtime_warmup_enabled is True


def test_agent_connection_check_uses_jiuwen_smoke_with_effective_config(monkeypatch):
    calls = []

    monkeypatch.setattr(AgentConfig, "from_env", classmethod(lambda cls: _base_agent_config()))
    monkeypatch.setattr(
        system_config,
        "build_effective_section_config",
        lambda section, data: (
            "agent",
            _effective_agent_config(
                main_agent_max_iterations=data["main_agent_max_iterations"],
                runtime_warmup_enabled=False,
            ),
        ),
    )

    def fake_run_jiuwen_smoke(*, agent_config, agent_mode, register_tools):
        calls.append((agent_config, agent_mode, register_tools))
        return {"registered_tool_count": 4}

    monkeypatch.setattr(
        "agent.core.jiuwen.smoke.run_jiuwen_smoke",
        fake_run_jiuwen_smoke,
    )

    result = system_config.test_system_config_connection(
        "agent",
        {"main_agent_max_iterations": 7},
    )

    assert result["success"] is True
    assert "Jiuwen ReAct" in result["message"]
    assert "4" in result["message"]
    assert len(calls) == 1
    checked_config, checked_mode, register_tools = calls[0]
    assert checked_mode == "quick"
    assert register_tools is system_config._validate_jiuwen_tools_without_runtime_delegates
    assert checked_config.main_agent_max_iterations == 7
    assert checked_config.runtime_warmup_enabled is False


def test_agent_connection_check_reports_jiuwen_smoke_failure(monkeypatch):
    monkeypatch.setattr(
        system_config,
        "build_effective_section_config",
        lambda section, data: ("agent", _effective_agent_config()),
    )
    monkeypatch.setattr(
        system_config,
        "_agent_config_for_connection_check",
        lambda effective: _base_agent_config(),
    )

    def fail_smoke(*, agent_config, agent_mode, register_tools):
        raise RuntimeError("Missing module: jsonschema_path")

    monkeypatch.setattr(
        "agent.core.jiuwen.smoke.run_jiuwen_smoke",
        fail_smoke,
    )

    result = system_config.test_system_config_connection("agent")

    assert result["success"] is False
    assert "Jiuwen ReAct" in result["message"]
    assert "jsonschema_path" in result["message"]
