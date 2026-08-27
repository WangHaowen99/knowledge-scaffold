"""Structured ontology_context and Grounding registration tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent.tools.base import ToolContext
from agent.tools.ontology_context import _build_structured_context
from config import AgentConfig
from ontology.evidence.manager import EvidenceIndexManager
from utils.read_docs import merge_read_documents_registry


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "ontologies" / "eco" / "versions" / "1.0.0"
    (package / "operations").mkdir(parents=True)
    (package / "assets").mkdir()
    (package / "manifest.yaml").write_text(
        "kind: ontology-package\nid: eco\nversion: 1.0.0\n",
        encoding="utf-8",
    )
    (package / "index.md").write_text("# ECO 本体\n", encoding="utf-8")
    (package / "operations" / "impact.md").write_text(
        """---
key: eco_impact
title: ECO影响分析
aliases: [设计变更影响制造物料]
---
# ECO影响分析
## 流程
追溯变更前后版本与制造物料。
## 约束
只采用当前有效制造映射。
""",
        encoding="utf-8",
    )
    (package / "assets" / "guide.txt").write_text(
        "受控文本资产：已发布工艺优先处置。\n",
        encoding="utf-8",
    )
    return package


def _manifest() -> dict[str, object]:
    return {
        "kind": "ontology-package",
        "schema_version": "0.1",
        "id": "eco",
        "name": "ECO",
        "description": "变更影响",
        "language": "zh-CN",
    }


def _context(tmp_path: Path, *, read_documents=None) -> ToolContext:
    return ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-structured",
        session_id="session-structured",
        turn_id=3,
        agent_config=AgentConfig.from_env(),
        caller="main",
        agent_iteration=2,
        workspace_id="workspace-a",
        project_root=str(tmp_path),
        read_documents=read_documents,
    )


def _enable(monkeypatch) -> None:
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", "all")


def _patch_manifest(monkeypatch, package: Path) -> None:
    ontology = SimpleNamespace(
        id="eco",
        active_version="1.0.0",
        ontology_dir=str(package),
        manifest_path=str(package / "manifest.yaml"),
    )
    monkeypatch.setattr(
        "agent.tools.ontology_context.get_ontology_manifest",
        lambda ontology_id, workspace_id=None: (ontology, None) if ontology_id == "eco" else (None, "Error: not found"),
    )


def test_structured_section_returns_v2_and_atomic_grounding_binding(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    package = _package(tmp_path)
    _patch_manifest(monkeypatch, package)
    manager = EvidenceIndexManager(tmp_path / "runtime")
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
        mutation_revision=7,
    )

    content, record = _build_structured_context(
        args={
            "ontology_id": "eco",
            "resource": "operation",
            "key": "eco_impact",
            "section": "eco影响分析/流程",
            "response_format": "structured",
        },
        context=_context(tmp_path),
        manager=manager,
    )

    payload = json.loads(content)
    assert payload["contract"] == "OntologyContext/v2"
    assert payload["success"] is True
    assert payload["content"] == "## 流程\n追溯变更前后版本与制造物料。"
    assert payload["context_meta"]["mutation_revision"] == 7
    assert payload["context_meta"]["ontology_epoch"] == pointer["expected_epoch"]
    assert payload["context_meta"]["index_generation"] == pointer["active_generation"]
    assert payload["context_meta"]["grounding_recorded"] is True
    assert payload["context_meta"]["source_evidence_ids"]
    assert record is not None
    assert record["_grounding_bindings"]

    registry = merge_read_documents_registry(None, [record])
    evidence_ids = payload["context_meta"]["source_evidence_ids"]
    assert set(evidence_ids).issubset(registry["grounding_index"]["by_evidence_id"])
    stored = registry["by_path"][record["normalized_path"]]
    assert "_grounding_bindings" not in stored
    assert stored["ontology_epoch"] == pointer["expected_epoch"]

    second_content, _ = _build_structured_context(
        args={
            "ontology_id": "eco",
            "resource": "operation",
            "key": "eco_impact",
            "section": "eco影响分析/流程",
            "response_format": "structured",
        },
        context=_context(tmp_path, read_documents=registry),
        manager=manager,
    )
    second_payload = json.loads(second_content)
    assert second_payload["context_meta"]["loaded_before"] is True
    assert second_payload["context_meta"]["read_count_before"] == 1


def test_structured_asset_is_supported_but_missing_section_is_explicit(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    package = _package(tmp_path)
    _patch_manifest(monkeypatch, package)
    manager = EvidenceIndexManager(tmp_path / "runtime")
    manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=package,
        manifest=_manifest(),
    )

    asset_content, asset_record = _build_structured_context(
        args={
            "ontology_id": "eco",
            "resource": "asset",
            "key": "guide.txt",
            "response_format": "structured",
        },
        context=_context(tmp_path),
        manager=manager,
    )
    asset_payload = json.loads(asset_content)
    assert asset_payload["success"] is True
    assert "已发布工艺优先处置" in asset_payload["content"]
    assert asset_payload["context_meta"]["grounding_recorded"] is True
    assert asset_record is not None

    missing_content, missing_record = _build_structured_context(
        args={
            "ontology_id": "eco",
            "resource": "operation",
            "key": "eco_impact",
            "section": "不存在",
            "response_format": "structured",
        },
        context=_context(tmp_path),
        manager=manager,
    )
    missing_payload = json.loads(missing_content)
    assert missing_payload["success"] is False
    assert missing_payload["result_code"] == "SECTION_NOT_FOUND"
    assert "eco影响分析/流程" in missing_payload["available_sections"]
    assert missing_record is None


def test_structured_mode_is_fail_closed_when_feature_is_off(tmp_path: Path, monkeypatch):
    monkeypatch.delenv("ONTOLOGY_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", raising=False)
    package = _package(tmp_path)
    _patch_manifest(monkeypatch, package)

    content, record = _build_structured_context(
        args={
            "ontology_id": "eco",
            "resource": "operation",
            "key": "eco_impact",
            "response_format": "structured",
        },
        context=_context(tmp_path),
        manager=EvidenceIndexManager(tmp_path / "runtime"),
    )
    assert content == "Error: EVIDENCE_FEATURE_DISABLED"
    assert record is None
