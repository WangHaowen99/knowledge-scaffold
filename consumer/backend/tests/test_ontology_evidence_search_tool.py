"""Feature-gated Agent evidence search tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from agent.tools.base import ToolContext
from agent.tools.ontology_evidence_search import OntologyEvidenceSearchTool
from agent.tools.registry import get_tools_schema
from config import AgentConfig
from config.evidence_config import EvidenceConfig
from ontology.evidence.manager import EvidenceIndexManager
from utils.read_docs import (
    build_grounding_subject_key,
    merge_read_documents_registry,
)


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    (package / "operations").mkdir(parents=True)
    (package / "index.md").write_text("# ECO\n", encoding="utf-8")
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


def _context(read_documents) -> ToolContext:
    return ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-search",
        session_id="session-search",
        turn_id=1,
        agent_config=AgentConfig.from_env(),
        caller="main",
        workspace_id="workspace-a",
        selected_ontology_id="eco",
        read_documents=read_documents,
    )


def _ready(tmp_path: Path, monkeypatch):
    runtime_dir = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime_dir)
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=4,
    )
    paths = SimpleNamespace(runtime_dir=runtime_dir)
    monkeypatch.setattr("agent.tools.ontology_evidence_search.get_workspace_paths", lambda _workspace: paths)
    return manager, pointer


def _root(pointer, *, epoch=None, revision=None):
    return merge_read_documents_registry(
        None,
        [
            {
                "path": "ontologies/eco/manifest.yaml",
                "doc_type": "manifest",
                "ontology_id": "eco",
                "source": "preload",
                "ontology_epoch": pointer["expected_epoch"] if epoch is None else epoch,
                "mutation_revision": pointer["mutation_revision"] if revision is None else revision,
            }
        ],
    )


def _enable(monkeypatch, mode="all"):
    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ENABLED", "true")
    monkeypatch.setenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", mode)
    monkeypatch.setattr(
        "agent.tools.ontology_evidence_search.EvidenceConfig.load",
        classmethod(lambda cls: cls.from_env()),
    )


def test_schema_is_adaptive_and_legacy_context_schema_is_preserved_off(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    monkeypatch.delenv("ONTOLOGY_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", raising=False)
    off_schemas = get_tools_schema(
        "main",
        workspace_id="workspace-a",
        selected_ontology_id="eco",
    )
    off_by_name = {item["function"]["name"]: item for item in off_schemas}
    assert "ontology_evidence_search" not in off_by_name
    off_context = off_by_name["ontology_context"]["function"]["parameters"]["properties"]
    assert off_context["resource"]["enum"] == ["root", "operation", "object", "rule", "metric", "script"]
    assert "response_format" not in off_context

    _enable(monkeypatch)
    _ready(tmp_path, monkeypatch)
    on_schemas = get_tools_schema(
        "main",
        workspace_id="workspace-a",
        selected_ontology_id="eco",
    )
    on_by_name = {item["function"]["name"]: item for item in on_schemas}
    assert "ontology_evidence_search" in on_by_name
    on_context = on_by_name["ontology_context"]["function"]["parameters"]["properties"]
    assert on_context["response_format"]["enum"] == ["text", "structured"]
    assert "asset" in on_context["resource"]["enum"]

    _enable(monkeypatch, mode="shadow")
    shadow = get_tools_schema(
        "main",
        workspace_id="workspace-a",
        selected_ontology_id="eco",
    )
    assert "ontology_evidence_search" not in {item["function"]["name"] for item in shadow}


def test_search_requires_current_root_and_returns_structured_next_action(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    _manager, pointer = _ready(tmp_path, monkeypatch)
    tool = OntologyEvidenceSearchTool()

    missing = tool.execute(
        args={"ontology_id": "eco", "query": "设计变更影响制造物料"},
        context=_context(None),
    )
    assert json.loads(missing.content)["error"]["code"] == "ROOT_REQUIRED"

    stale = tool.execute(
        args={"ontology_id": "eco", "query": "设计变更影响制造物料"},
        context=_context(_root(pointer, epoch="0" * 64)),
    )
    assert json.loads(stale.content)["error"]["code"] == "CONTEXT_STALE"

    outcome = tool.execute(
        args={"ontology_id": "eco", "query": "设计变更影响制造物料", "limit": 5},
        context=_context(_root(pointer)),
    )
    payload = json.loads(outcome.content)
    assert payload["contract"] == "OntologyEvidenceSearch/v1"
    assert payload["success"] is True
    assert payload["status"] == "resolved"
    assert payload["ontology_epoch"] == pointer["expected_epoch"]
    assert payload["hits"]
    hit = payload["hits"][0]
    assert "EXACT_ALIAS" in hit["reason_codes"]
    assert hit["loaded_before"] is False
    assert hit["next_action"]["tool"] == "ontology_context"
    assert hit["next_action"]["arguments"]["response_format"] == "structured"


def test_loaded_before_uses_only_current_epoch_grounding(tmp_path: Path, monkeypatch):
    _enable(monkeypatch)
    _manager, pointer = _ready(tmp_path, monkeypatch)
    tool = OntologyEvidenceSearchTool()
    root = _root(pointer)
    first = json.loads(
        tool.execute(
            args={"ontology_id": "eco", "query": "设计变更影响制造物料"},
            context=_context(root),
        ).content
    )
    evidence_id = first["hits"][0]["evidence_id"]
    subject = build_grounding_subject_key(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_epoch=pointer["expected_epoch"],
        resource=first["hits"][0]["resource"],
        resource_key=first["hits"][0]["key"],
    )
    loaded = merge_read_documents_registry(
        root,
        [
            {
                "path": "ontologies/eco/operations/impact.md",
                "_grounding_bindings": [
                    {
                        "evidence_id": evidence_id,
                        "subject_key": subject,
                        "ontology_epoch": pointer["expected_epoch"],
                    }
                ],
            }
        ],
    )
    second = json.loads(
        tool.execute(
            args={"ontology_id": "eco", "query": "设计变更影响制造物料"},
            context=_context(loaded),
        ).content
    )
    assert second["hits"][0]["loaded_before"] is True


def test_search_fails_closed_when_feature_is_disabled(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    monkeypatch.delenv("ONTOLOGY_EVIDENCE_ENABLED", raising=False)
    monkeypatch.delenv("ONTOLOGY_EVIDENCE_ROLLOUT_MODE", raising=False)
    _manager, pointer = _ready(tmp_path, monkeypatch)
    outcome = OntologyEvidenceSearchTool().execute(
        args={"ontology_id": "eco", "query": "设计变更影响制造物料"},
        context=_context(_root(pointer)),
    )
    assert json.loads(outcome.content)["error"]["code"] == "EVIDENCE_FEATURE_DISABLED"
