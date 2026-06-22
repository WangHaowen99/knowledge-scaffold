import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "backend"
TOOLS = ROOT / "tools"
for item in (str(BACKEND), str(TOOLS)):
    if item not in sys.path:
        sys.path.insert(0, item)

import agent_sim_runner


def _args(**overrides):
    values = {
        "question": "hello",
        "session_id": None,
        "run_id": None,
        "user_id": "sim",
        "workspace_id": "default",
        "output_dir": None,
        "model_profile_id": None,
        "agent_mode": None,
        "llm_request_mode": None,
        "selected_ontology_id": None,
        "context_json": None,
        "context": [],
        "base_url": None,
        "model": None,
        "api_key": None,
        "api_key_env": None,
        "provider": "openai_compatible",
        "temperature": None,
        "max_tokens": None,
        "timeout": None,
    }
    values.update(overrides)
    return argparse.Namespace(**values)


def test_build_context_merges_explicit_fields_and_json_values():
    context = agent_sim_runner.build_context(
        _args(
            context_json='{"foo":"bar"}',
            context=["count=3", "enabled=true", "label=text"],
            model_profile_id="accurate",
            agent_mode="multi_agent",
            llm_request_mode="sync",
            selected_ontology_id="example",
        )
    )

    assert context == {
        "foo": "bar",
        "count": 3,
        "enabled": True,
        "label": "text",
        "model_profile_id": "accurate",
        "agent_mode": "multi_agent",
        "llm_request_mode": "sync",
        "selected_ontology_id": "example",
    }


def test_build_transient_profile_from_direct_model_options(monkeypatch):
    monkeypatch.setenv("SIM_API_KEY", "secret-key")

    profile = agent_sim_runner.build_transient_profile(
        _args(
            base_url="https://example.test/v1",
            model="demo-model",
            api_key_env="SIM_API_KEY",
            temperature=0.2,
            max_tokens=1234,
            timeout=45,
        )
    )

    assert profile["id"] == "sim_transient"
    assert profile["api_key"] == "secret-key"
    assert profile["timeout"] == 45


def test_duration_ms_never_goes_negative():
    assert agent_sim_runner._duration_ms(1000, 1234) == 234
    assert agent_sim_runner._duration_ms(1234, 1000) == 0


def test_prepare_run_writes_public_and_private_artifacts(monkeypatch, tmp_path):
    private_config = {
        "llm": {},
        "task_llm": {},
        "storage": {
            "project_root": str(tmp_path / "runtime"),
            "chat_storage_dir": str(tmp_path / "runtime" / "chats"),
            "execution_storage_dir": str(tmp_path / "runtime" / "executions"),
            "auth_users_file": str(tmp_path / "runtime" / "users.json"),
        },
        "auth": {},
        "agent": {},
        "memory": {},
        "observability": {},
        "llm_profiles": {"default_profile_id": "", "profiles": []},
    }
    public_config = {
        **private_config,
        "llm_profiles": {"default_profile_id": "", "profiles": []},
    }
    monkeypatch.setattr(
        "agentic.config.system_config.load_system_config",
        lambda: private_config,
    )
    monkeypatch.setattr(
        "agentic.config.system_config.get_public_system_config",
        lambda: public_config,
    )

    run_dir, spec = agent_sim_runner.prepare_run(
        _args(
            run_id="sim_test",
            output_dir=str(tmp_path),
            question="test question",
            base_url="https://example.test/v1",
            model="demo-model",
            api_key="secret-key",
        ),
        mode="background",
    )

    assert run_dir == tmp_path / "sim_test"
    assert spec["run_id"] == "sim_test"
    public_spec = json.loads((run_dir / agent_sim_runner.SPEC_FILENAME).read_text(encoding="utf-8"))
    assert "private_config_path" not in public_spec
    private_snapshot = json.loads((run_dir / agent_sim_runner.PRIVATE_CONFIG_FILENAME).read_text(encoding="utf-8"))
    assert private_snapshot["llm_profiles"]["profiles"][0]["api_key"] == "secret-key"
