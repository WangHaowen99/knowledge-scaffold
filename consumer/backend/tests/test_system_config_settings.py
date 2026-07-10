import json

import pytest

from agentic.config.settings import AgentConfig, AppConfig, LimitsConfig
from agentic.config.system_config import SystemConfigValidationError, load_system_config


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def test_limits_config_uses_current_builtin_defaults(tmp_path, monkeypatch):
    monkeypatch.setenv("OPS_CONFIG_PATH", str(tmp_path / "missing-ops-config.json"))
    monkeypatch.delenv("AGENT_RUN_TPS", raising=False)
    monkeypatch.delenv("ONTOLOGY_OPERATION_MAX_CHARS", raising=False)

    limits = LimitsConfig.from_env()

    assert limits.run_tps == 50
    assert limits.operation_max_chars == 15000


def test_resolve_default_runtime_root_handles_flat_container_layout():
    import agentic.config.system_config as system_config_module

    assert system_config_module._resolve_default_runtime_root(
        system_config_module.Path("/app")
    ) == system_config_module.Path("/app/.onto-runtime")
    assert system_config_module._resolve_default_runtime_root(
        system_config_module.Path("/repo/backend")
    ) == system_config_module.Path("/repo/.onto-runtime")


def test_config_runtime_root_can_follow_project_root_env(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    runtime_root = tmp_path / "custom-runtime"
    monkeypatch.setenv(system_config_module.PROJECT_ROOT_ENV, str(runtime_root))

    assert system_config_module._resolve_config_runtime_root() == runtime_root


def test_system_config_migrates_legacy_backend_config_to_runtime_config(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_path = tmp_path / ".onto-runtime" / "config" / "system_config.json"
    legacy_config_path = tmp_path / "backend" / "config" / "system_config.json"
    legacy_config_path.parent.mkdir(parents=True, exist_ok=True)
    legacy_config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://legacy.example/v1",
    "model": "legacy-model",
    "api_key": "legacy-secret"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.delenv(system_config_module.SYSTEM_CONFIG_PATH_ENV, raising=False)
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setattr(system_config_module, "DEFAULT_CONFIG_PATH", config_path)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_path.parent)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(system_config_module, "LEGACY_CONFIG_PATH", legacy_config_path)

    config = system_config_module.load_system_config()

    assert config["llm"]["base_url"] == "https://legacy.example/v1"
    assert config["llm"]["model"] == "legacy-model"
    assert config["llm"]["api_key"] == "legacy-secret"
    assert config_path.exists()
    raw_text = config_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert "legacy-secret" not in raw_text
    assert raw["llm"]["api_key"].startswith("enc:v1:")


def test_system_config_secret_fields_are_encrypted_at_rest(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    system_config_module.update_system_config_section(
        "llm",
        {
            "base_url": "https://llm.example/v1",
            "model": "main-model",
            "api_key": "main-secret",
        },
    )
    system_config_module.update_system_config_section(
        "llm_profiles",
        {
            "default_profile_id": "main",
            "profiles": [
                {
                    "id": "main",
                    "name": "Main",
                    "provider": "openai_compatible",
                    "base_url": "https://llm.example/v1",
                    "model": "main-model",
                    "api_key": "profile-secret",
                    "enabled": True,
                }
            ],
        },
    )

    raw_text = config_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)

    assert "main-secret" not in raw_text
    assert "profile-secret" not in raw_text
    assert raw["llm"]["api_key"].startswith("enc:v1:")
    assert raw["llm_profiles"]["profiles"][0]["api_key"].startswith("enc:v1:")

    effective = system_config_module.load_system_config()
    assert effective["llm"]["api_key"] == "main-secret"
    assert effective["llm_profiles"]["profiles"][0]["api_key"] == "profile-secret"

    public = system_config_module.get_public_system_config()
    assert public["llm"]["has_api_key"] is True
    assert public["llm_profiles"]["profiles"][0]["has_api_key"] is True
    assert "api_key" not in public["llm"]
    assert "api_key" not in public["llm_profiles"]["profiles"][0]


def test_existing_runtime_system_config_plaintext_secrets_are_rewritten(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://llm.example/v1",
    "model": "main-model",
    "api_key": "runtime-secret"
  },
  "observability": {
    "enabled": true,
    "mode": "langfuse",
    "langfuse": {
      "public_key": "pk-test",
      "secret_key": "langfuse-secret",
      "base_url": "https://langfuse.example"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    config = system_config_module.load_system_config()

    assert config["llm"]["api_key"] == "runtime-secret"
    assert config["observability"]["langfuse"]["secret_key"] == "langfuse-secret"
    raw_text = config_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert "runtime-secret" not in raw_text
    assert "langfuse-secret" not in raw_text
    assert raw["llm"]["api_key"].startswith("enc:v1:")
    assert raw["observability"]["langfuse"]["secret_key"].startswith("enc:v1:")


def test_runtime_settings_read_from_saved_system_config(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://llm.example/v1",
    "model": "main-model",
    "api_key": "main-secret",
    "timeout": 321,
    "max_tokens": 2048,
    "temperature": 0.3
  },
  "task_llm": {
    "base_url": "https://task.example/v1",
    "model": "task-model",
    "api_key": "task-secret",
    "timeout": 45
  },
  "storage": {
    "project_root": "/tmp/project"
  },
  "agent": {
    "llm_connection_max_retries": 8,
    "main_agent_max_iterations": 77,
    "sub_agent_max_iterations": 9,
    "sub_agent_max_concurrency": 13,
    "sub_agent_max_depth": 3,
    "parallel_default_max_workers": 12,
    "context_max_rounds": 14,
    "context_prune_tools": "read_file,list_files"
  },
  "web_search": {
    "enabled": true,
    "provider": "tavily",
    "base_url": "https://search.example",
    "api_key": "search-secret",
    "timeout_seconds": 12,
    "default_max_results": 4,
    "max_results_limit": 8,
    "search_depth": "fast",
    "allowed_domains": "docs.example.com",
    "blocked_domains": "blocked.example.com"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    app_config = AppConfig.from_env()
    agent_config = AgentConfig.from_env()

    assert app_config.llm.base_url == "https://llm.example/v1"
    assert app_config.llm.model == "main-model"
    assert app_config.task_llm.base_url == "https://task.example/v1"
    assert app_config.task_llm.timeout == 45
    assert app_config.task_llm.max_tokens == 2048
    assert app_config.task_llm.temperature == 0.3
    assert app_config.project_root == "/tmp/project"
    assert not hasattr(app_config, "ontologies_root")
    assert not hasattr(app_config, "skills_root")

    assert agent_config.llm_connection_max_retries == 8
    assert agent_config.main_agent_max_iterations == 77
    assert agent_config.sub_agent_max_concurrency == 13
    assert agent_config.sub_agent_max_depth == 3
    assert agent_config.context_prune_tools == {"read_file", "list_files"}
    assert agent_config.web_search.enabled is True
    assert agent_config.web_search.provider == "tavily"
    assert agent_config.web_search.base_url == "https://search.example"
    assert agent_config.web_search.api_key == "search-secret"
    assert agent_config.web_search.timeout_seconds == 12
    assert agent_config.web_search.default_max_results == 4
    assert agent_config.web_search.max_results_limit == 8
    assert agent_config.web_search.search_depth == "fast"
    assert agent_config.web_search.allowed_domains == "docs.example.com"


def test_saved_system_config_fields_override_environment_variables(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://saved.example/v1",
    "model": "saved-model",
    "api_key": "saved-secret"
  },
  "storage": {
    "project_root": "/saved/project"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("LLM_BASE_URL", "https://env.example/v1")
    monkeypatch.setenv("LLM_MODEL", "env-model")
    monkeypatch.setenv("LLM_API_KEY", "env-secret")
    monkeypatch.setenv("PROJECT_ROOT", "/env/project")
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)

    config = system_config_module.load_system_config()
    app_config = AppConfig.from_env()

    assert config["llm"]["base_url"] == "https://saved.example/v1"
    assert config["llm"]["model"] == "saved-model"
    assert config["llm"]["api_key"] == "saved-secret"
    assert config["storage"]["project_root"] == "/saved/project"
    assert "ontology_search" not in config
    assert app_config.llm.base_url == "https://saved.example/v1"
    assert app_config.llm.model == "saved-model"


def test_legacy_llm_config_is_exposed_as_default_model_profile(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://legacy.example/v1",
    "model": "legacy-model",
    "api_key": "legacy-secret",
    "timeout": 222,
    "max_tokens": 3333,
    "temperature": 0.2
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    effective = system_config_module.load_system_config()
    public_profiles = system_config_module.get_public_llm_profiles()
    app_config = AppConfig.from_env()

    assert effective["llm_profiles"]["default_profile_id"] == "default"
    assert effective["llm_profiles"]["profiles"][0]["model"] == "legacy-model"
    assert public_profiles["profiles"][0]["has_api_key"] is True
    assert "api_key" not in public_profiles["profiles"][0]
    assert "supports_reasoning" not in effective["llm_profiles"]["profiles"][0]
    assert "extra_body" not in effective["llm_profiles"]["profiles"][0]
    assert "supports_reasoning" not in public_profiles["profiles"][0]
    assert "extra_body" not in public_profiles["profiles"][0]
    assert app_config.llm.model == "legacy-model"
    assert app_config.llm.provider == "openai_compatible"


def test_empty_system_config_has_no_default_llm_profile(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    effective = system_config_module.load_system_config()
    public_profiles = system_config_module.get_public_llm_profiles()
    app_config = AppConfig.from_env()

    assert effective["llm"]["base_url"] == ""
    assert effective["llm"]["model"] == ""
    assert effective["llm_profiles"]["default_profile_id"] == ""
    assert effective["llm_profiles"]["profiles"] == []
    assert "ontology_search" not in effective
    assert public_profiles["default_profile_id"] == ""
    assert public_profiles["profiles"] == []
    assert app_config.llm.base_url is None
    assert app_config.llm.model is None
    assert app_config.llm.provider == "openai_compatible"


def test_empty_system_config_readiness_requires_model(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    readiness = system_config_module.get_system_config_readiness()

    assert readiness["ready"] is False
    assert readiness["model"]["ready"] is False
    assert readiness["model"]["missing_fields"] == ["llm_profiles.profiles"]
    assert "ontology_search" not in readiness


def test_system_config_readiness_is_ready_when_model_profile_exists(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm_profiles": {
    "default_profile_id": "main",
    "profiles": [
      {
        "id": "main",
        "name": "Main",
        "provider": "openai_compatible",
        "base_url": "https://llm.example/v1",
        "model": "main-model",
        "api_key": "secret",
        "timeout": 60,
        "max_tokens": 1024,
        "temperature": 0.2,
        "enabled": true
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    readiness = system_config_module.get_system_config_readiness()

    assert readiness["model"]["ready"] is True
    assert readiness["ready"] is True
    assert "ontology_search" not in readiness


def test_default_model_profile_overrides_legacy_llm_for_runtime(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://legacy.example/v1",
    "model": "legacy-model",
    "api_key": "legacy-secret"
  },
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
        "timeout": 45,
        "max_tokens": 1024,
        "temperature": 0.1,
        "enabled": true
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    app_config = AppConfig.from_env()

    assert app_config.llm.profile_id == "fast"
    assert app_config.llm.model == "fast-model"
    assert app_config.llm.provider == "openai_compatible"


def test_agent_config_ignores_removed_alias_envs(monkeypatch):
    monkeypatch.delenv("AGENT_MAIN_LOOP_MAX_ITERATIONS", raising=False)
    monkeypatch.delenv("AGENT_SUB_AGENT_MAX_ITERATIONS", raising=False)
    monkeypatch.setenv("AGENT_GRAPH_RECURSION_LIMIT", "99")
    monkeypatch.setenv("AGENT_SUB_AGENT_MAX_TOOL_ROUNDS", "77")

    config = AgentConfig.from_env()

    assert config.main_agent_max_iterations == 50
    assert config.sub_agent_max_iterations == 20


def test_agent_config_exposes_only_runtime_knobs(monkeypatch):
    config = AgentConfig.from_env()
    assert set(load_system_config()["agent"].keys()) == {
        "llm_connection_max_retries",
        "main_agent_max_iterations",
        "sub_agent_max_iterations",
        "sub_agent_max_concurrency",
        "sub_agent_max_depth",
        "parallel_default_max_workers",
        "context_max_rounds",
        "context_prune_tools",
        "evolution_min_occurrences",
        "evolution_min_applied",
        "evolution_max_rejection",
        "evolution_passive_reflection_enabled",
        "evolution_patch_auto_apply",
        "evolution_patch_cooldown_sec",
        "evolution_sweep_interval_sec",
        "execute_script_timeout",
        "runtime_warmup_enabled",
    }
    assert config.main_agent_max_iterations == 50


def test_import_system_config_discards_unknown_runtime_fields(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    imported_sections, public_config = system_config_module.import_system_config(
        {
            "agent": {
                "main_agent_max_iterations": 33,
                "jiuwen_unknown_field": "discard-me",
            },
            "observability": {
                "enabled": True,
                "mode": "local",
                "queue_size": 1024,
                "local": {
                    "retention_days": 3,
                    "file_name": "discard-me.log",
                },
            },
        }
    )

    assert imported_sections == ["agent", "observability"]
    assert public_config["agent"]["main_agent_max_iterations"] == 33
    assert "jiuwen_unknown_field" not in public_config["agent"]
    assert public_config["observability"]["enabled"] is True
    assert public_config["observability"]["local"]["retention_days"] == 3
    assert "queue_size" not in public_config["observability"]
    assert "file_name" not in public_config["observability"]["local"]


def test_agent_config_connection_check_runs_jiuwen_smoke(monkeypatch):
    import agentic.config.system_config as system_config_module

    calls = []

    def fake_run_jiuwen_smoke(*, agent_config=None, agent_mode="quick"):
        calls.append(
            {
                "main_agent_max_iterations": agent_config.main_agent_max_iterations,
                "agent_mode": agent_mode,
            }
        )
        return {"agent_id": "agent-1", "registered_tools": ["ontology_context"]}

    monkeypatch.setattr(
        "agentic.loop.jiuwen.smoke.run_jiuwen_smoke",
        fake_run_jiuwen_smoke,
    )

    result = system_config_module.test_system_config_connection(
        "agent",
        {"main_agent_max_iterations": 7},
    )

    assert result["success"] is True
    assert "Jiuwen" in result["message"]
    assert calls == [{"main_agent_max_iterations": 7, "agent_mode": "quick"}]


def test_agent_config_connection_check_reports_jiuwen_smoke_failure(monkeypatch):
    import agentic.config.system_config as system_config_module

    def fake_run_jiuwen_smoke(*, agent_config=None, agent_mode="quick"):
        raise RuntimeError("Missing module: jsonschema_path")

    monkeypatch.setattr(
        "agentic.loop.jiuwen.smoke.run_jiuwen_smoke",
        fake_run_jiuwen_smoke,
    )

    result = system_config_module.test_system_config_connection(
        "agent",
        {"main_agent_max_iterations": 7},
    )

    assert result["success"] is False
    assert "Jiuwen" in result["message"]
    assert "jsonschema_path" in result["message"]


def test_system_config_exposes_memory_defaults(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    config = system_config_module.load_system_config()

    assert config["memory"]["mode"] == "filesystem"
    assert config["memory"]["recall_topk"] == 3
    assert config["memory"]["recall_threshold"] == ""
    assert config["memory"]["workspace_root"] == ""


def test_system_config_exposes_observability_defaults(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    config = system_config_module.load_system_config()
    public_config = system_config_module.get_public_system_config()

    assert config["observability"] == {
        "enabled": False,
        "mode": "local",
        "local": {
            "retention_days": 30,
        },
        "langfuse": {
            "public_key": "",
            "secret_key": "",
            "base_url": "",
            "debug": False,
        },
    }
    assert public_config["observability"]["langfuse"]["has_secret_key"] is False
    assert "secret_key" not in public_config["observability"]["langfuse"]


def test_system_config_does_not_expose_ops_limits(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    public_config = system_config_module.get_public_system_config()
    template = system_config_module.get_system_config_template()
    config = system_config_module.load_system_config()

    assert "limits" not in config
    assert "limits" not in public_config
    assert "limits" not in template


def test_limits_config_reads_ops_config_instead_of_system_config(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    system_config_path = config_dir / "system_config.json"
    ops_config_path = config_dir / "ops_config.json"
    system_config_path.write_text(
        """
{
  "limits": {
    "run_tps": 99,
    "workspace_max_count": 99
  }
}
""".strip(),
        encoding="utf-8",
    )
    ops_config_path.write_text(
        """
{
  "limits": {
    "run_tps": 12,
    "workspace_max_count": 7,
    "data_connection_configs_max_per_workspace": 8,
    "user_max_count": 9,
    "ontology_max_count_per_workspace": 6,
    "ontology_object_max_count": 201,
    "ontology_operation_max_count": 101,
    "entity_attribute_max_count": 88,
    "model_profile_max_count": 11,
    "model_input_token_limit": 40000,
    "model_output_token_limit": 90000
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", system_config_path)
    monkeypatch.setenv("OPS_CONFIG_PATH", str(ops_config_path))

    limits = LimitsConfig.from_env()
    agent_config = AgentConfig.from_env()

    assert limits.run_tps == 12
    assert limits.workspace_max_count == 7
    assert limits.data_connection_configs_max_per_workspace == 8
    assert limits.user_max_count == 9
    assert limits.ontology_max_count_per_workspace == 6
    assert limits.ontology_object_max_count == 201
    assert limits.ontology_operation_max_count == 101
    assert limits.entity_attribute_max_count == 88
    assert limits.model_profile_max_count == 11
    assert limits.model_input_token_limit == 40000
    assert limits.model_output_token_limit == 90000
    assert agent_config.limits.run_tps == 12


def test_limits_config_uses_env_when_ops_config_field_is_missing(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    system_config_path = config_dir / "system_config.json"
    ops_config_path = config_dir / "ops_config.json"
    system_config_path.write_text(
        """
{
  "limits": {
    "run_tps": 99
  }
}
""".strip(),
        encoding="utf-8",
    )
    ops_config_path.write_text(
        """
{
  "limits": {
    "workspace_max_count": 6
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", system_config_path)
    monkeypatch.setenv("OPS_CONFIG_PATH", str(ops_config_path))
    monkeypatch.setenv("AGENT_RUN_TPS", "11")
    monkeypatch.setenv("DATA_CONNECTION_CONFIGS_MAX_PER_WORKSPACE", "9")
    monkeypatch.setenv("USER_MAX_COUNT", "4")
    monkeypatch.setenv("ONTOLOGY_MAX_COUNT_PER_WORKSPACE", "3")
    monkeypatch.setenv("ONTOLOGY_OBJECT_MAX_COUNT", "202")
    monkeypatch.setenv("ONTOLOGY_OPERATION_MAX_COUNT", "102")
    monkeypatch.setenv("ENTITY_ATTRIBUTE_MAX_COUNT", "77")
    monkeypatch.setenv("MODEL_PROFILE_MAX_COUNT", "12")
    monkeypatch.setenv("MODEL_INPUT_TOKEN_LIMIT", "30000")
    monkeypatch.setenv("MODEL_OUTPUT_TOKEN_LIMIT", "80000")

    limits = LimitsConfig.from_env()

    assert limits.run_tps == 11
    assert limits.workspace_max_count == 6
    assert limits.data_connection_configs_max_per_workspace == 9
    assert limits.user_max_count == 4
    assert limits.ontology_max_count_per_workspace == 3
    assert limits.ontology_object_max_count == 202
    assert limits.ontology_operation_max_count == 102
    assert limits.entity_attribute_max_count == 77
    assert limits.model_profile_max_count == 12
    assert limits.model_input_token_limit == 30000
    assert limits.model_output_token_limit == 80000


def test_model_profiles_do_not_expose_or_accept_token_hard_limits(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm_profiles": {
    "default_profile_id": "main",
    "profiles": [
      {
        "id": "main",
        "name": "Main",
        "provider": "openai_compatible",
        "base_url": "https://llm.example/v1",
        "model": "main-model",
        "api_key": "secret",
        "timeout": 60,
        "max_tokens": 1024,
        "input_token_limit": 32000,
        "output_token_limit": 64000,
        "temperature": 0.2,
        "enabled": true
      }
    ]
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    app_config = AppConfig.from_env()
    public_profiles = system_config_module.get_public_llm_profiles()
    effective = system_config_module.load_system_config()

    assert not hasattr(app_config.llm, "input_token_limit")
    assert not hasattr(app_config.llm, "output_token_limit")
    assert "input_token_limit" not in effective["llm_profiles"]["profiles"][0]
    assert "output_token_limit" not in effective["llm_profiles"]["profiles"][0]
    assert "input_token_limit" not in public_profiles["profiles"][0]
    assert "output_token_limit" not in public_profiles["profiles"][0]


def test_model_profile_count_is_limited_by_ops_config(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    ops_config_path = config_dir / "ops_config.json"
    ops_config_path.write_text(
        """
{
  "limits": {
    "model_profile_max_count": 2
  }
}
""".strip(),
        encoding="utf-8",
    )
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setenv("OPS_CONFIG_PATH", str(ops_config_path))

    profiles = []
    for index in range(3):
        profiles.append({
            "id": "model-{}".format(index),
            "name": "Model {}".format(index),
            "provider": "openai_compatible",
            "base_url": "https://llm.example/v1",
            "model": "main-model",
            "api_key": "secret",
            "timeout": 60,
            "max_tokens": 1024,
            "temperature": 0.2,
            "enabled": True,
        })

    try:
        system_config_module.update_system_config_section(
            "llm_profiles",
            {"default_profile_id": "model-0", "profiles": profiles},
        )
    except system_config_module.SystemConfigValidationError as exc:
        assert "模型数量已达上限" in str(exc)
    else:
        raise AssertionError("expected model profile count validation error")


def test_system_config_defaults_observability_to_disabled_local_mode(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    config = system_config_module.load_system_config()

    assert config["observability"]["enabled"] is False
    assert config["observability"]["mode"] == "local"
    assert config["observability"]["local"] == {
        "retention_days": 30,
    }


def test_system_config_loads_observability_from_saved_config(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": false,
    "mode": "langfuse",
    "langfuse": {
      "public_key": "pk-test",
      "secret_key": "sk-test",
      "base_url": "https://langfuse.example",
      "debug": true
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    config = system_config_module.load_system_config()
    public_config = system_config_module.get_public_system_config()

    assert config["observability"]["enabled"] is False
    assert config["observability"]["mode"] == "langfuse"
    assert config["observability"]["langfuse"]["secret_key"] == "sk-test"
    assert public_config["observability"]["langfuse"]["has_secret_key"] is True
    assert "secret_key" not in public_config["observability"]["langfuse"]


def test_observability_connection_test_calls_langfuse_public_api(monkeypatch):
    import agentic.config.system_config as system_config_module

    calls = []

    class FakeResponse:
        def raise_for_status(self):
            pass

    def fake_get(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(system_config_module.requests, "get", fake_get)

    result = system_config_module.test_system_config_connection(
        "observability",
        {
            "enabled": True,
            "mode": "langfuse",
            "langfuse": {
                "public_key": "pk-test",
                "secret_key": "sk-test",
                "base_url": "https://langfuse.example",
            },
        },
    )

    assert result["success"] is True
    assert "Langfuse 连接成功" in result["message"]
    assert calls == [
        (
            "https://langfuse.example/api/public/projects",
            {
                "auth": ("pk-test", "sk-test"),
                "timeout": 10,
            },
        )
    ]


def test_observability_connection_test_reports_langfuse_http_error(monkeypatch):
    import requests
    import agentic.config.system_config as system_config_module

    class FakeResponse:
        status_code = 401
        text = "Unauthorized"

        def json(self):
            return {"message": "bad credentials"}

        def raise_for_status(self):
            raise requests.exceptions.HTTPError(response=self)

    monkeypatch.setattr(
        system_config_module.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(),
    )

    result = system_config_module.test_system_config_connection(
        "observability",
        {
            "enabled": True,
            "mode": "langfuse",
            "langfuse": {
                "public_key": "pk-test",
                "secret_key": "wrong",
                "base_url": "https://langfuse.example",
            },
        },
    )

    assert result["success"] is False
    assert "Langfuse 连接失败（HTTP 401）" in result["message"]
    assert "bad credentials" in result["message"]


def test_web_search_connection_test_calls_tavily_search(monkeypatch):
    import agentic.config.system_config as system_config_module

    captured = {}

    class FakeResponse:
        def raise_for_status(self):
            return None

    def fake_post(url, headers=None, json=None, timeout=None):
        captured.update(
            {
                "url": url,
                "headers": headers,
                "json": json,
                "timeout": timeout,
            }
        )
        return FakeResponse()

    monkeypatch.setattr(system_config_module.requests, "post", fake_post)

    result = system_config_module.test_system_config_connection(
        "web_search",
        {
            "enabled": True,
            "provider": "tavily",
            "base_url": "https://search.example",
            "api_key": "tvly-test",
            "timeout_seconds": 8,
            "default_max_results": 2,
            "max_results_limit": 5,
            "search_depth": "basic",
        },
    )

    assert result["success"] is True
    assert "联网搜索连接成功" in result["message"]
    assert captured["url"] == "https://search.example/search"
    assert captured["headers"]["Authorization"] == "Bearer tvly-test"
    assert captured["json"]["max_results"] == 1
    assert captured["json"]["include_raw_content"] is False
    assert captured["timeout"] == 8


def test_system_config_normalizes_legacy_flat_container_runtime_root(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "storage": {
    "project_root": "/.onto-runtime"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setattr(system_config_module, "REPO_ROOT", system_config_module.Path("/"))
    monkeypatch.setattr(system_config_module, "ONTO_RUNTIME_ROOT", system_config_module.Path("/app/.onto-runtime"))

    config = system_config_module.load_system_config()

    assert config["storage"]["project_root"] == "/app/.onto-runtime"


def test_system_config_ignores_legacy_status_metadata(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "llm": {
    "base_url": "https://legacy.example/v1",
    "model": "legacy-model"
  },
  "_status": {
    "llm": {
      "success": true,
      "message": "legacy status",
      "tested_at": "2026-04-25T00:00:00Z"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    config = system_config_module.get_public_system_config()

    assert config["llm"]["model"] == "legacy-model"
    assert "_status" not in config
