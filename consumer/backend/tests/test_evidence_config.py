import json

import pytest
from config import system_config
from config.evidence_config import EvidenceConfig


def _use_config_path(monkeypatch, tmp_path):
    path = tmp_path / "system_config.json"
    monkeypatch.setattr(system_config, "CONFIG_PATH", path)
    monkeypatch.setattr(system_config, "CONFIG_DIR", path.parent)
    return path


def test_saved_evidence_config_overrides_environment(monkeypatch, tmp_path):
    path = _use_config_path(monkeypatch, tmp_path)
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ENABLED", "false")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", "off")
    path.write_text(
        json.dumps(
            {
                "ontology_evidence": {
                    "enabled": True,
                    "rollout_mode": "allowlist",
                    "rollout_workspace_ids": "workspace-a,workspace-b",
                    "semantic_mode": "off",
                    "auto_ground_business_data": False,
                }
            }
        ),
        encoding="utf-8",
    )

    config = EvidenceConfig.load()

    assert config.enabled is True
    assert config.workspace_mode("workspace-a") == "allowlist"
    assert config.workspace_mode("workspace-c") == "off"
    assert config.semantic_enabled is False
    assert config.auto_ground_business_data is False


def test_auto_ground_business_data_can_be_saved_from_system_config(monkeypatch, tmp_path):
    _use_config_path(monkeypatch, tmp_path)

    saved = system_config.update_system_config_section(
        "ontology_evidence",
        {"auto_ground_business_data": False},
    )

    assert saved["auto_ground_business_data"] is False
    assert EvidenceConfig.load().auto_ground_business_data is False


def test_system_config_exposes_only_executable_evidence_switches(monkeypatch, tmp_path):
    _use_config_path(monkeypatch, tmp_path)
    section = system_config.get_public_system_config()["ontology_evidence"]

    assert section["enabled"] is False
    assert section["auto_build_enabled"] is False
    assert section["semantic_mode"] == "off"
    assert section["auto_ground_business_data"] is True
    assert section["runtime_fact_write_mode"] == "sync"
    assert "epoch_compute_mode" not in section
    assert "perf_guard_enabled" not in section
    assert not any(name.startswith("perf_max_") for name in section)


def test_system_config_rejects_unimplemented_evidence_switches(monkeypatch, tmp_path):
    _use_config_path(monkeypatch, tmp_path)

    with pytest.raises(system_config.SystemConfigValidationError, match="不支持字段"):
        system_config.update_system_config_section(
            "ontology_evidence",
            {"perf_guard_enabled": True},
        )


def test_search_requires_structured_context(monkeypatch, tmp_path):
    _use_config_path(monkeypatch, tmp_path)
    with pytest.raises(system_config.SystemConfigValidationError, match="structured_context_enabled"):
        system_config.build_effective_section_config(
            "ontology_evidence",
            {"search_enabled": True, "structured_context_enabled": False},
        )


def test_shadow_mode_builds_but_never_exposes_search():
    config = EvidenceConfig(
        enabled=True,
        rollout_mode="shadow",
        auto_build_enabled=True,
    )
    assert config.builds_indexes("workspace-a") is True
    assert config.exposes_search_tool("workspace-a") is False
