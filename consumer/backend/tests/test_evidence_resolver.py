"""Unified static and Runtime Fact evidence lookup tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from ontology.evidence.manager import EvidenceIndexManager
from ontology.evidence.resolver import EvidenceResolver, EvidenceResolverError
from ontology.evidence.service import EvidenceSearchService
from persistence.runtime_facts import RuntimeFactsStore


def _package(tmp_path: Path) -> Path:
    package = tmp_path / "package"
    (package / "objects").mkdir(parents=True)
    (package / "index.md").write_text("# ECO\n", encoding="utf-8")
    (package / "objects" / "MPart.md").write_text(
        """---
key: MPart
title: 制造物料
aliases: [物料]
---
# 制造物料
## 字段
物料编号与状态。
""",
        encoding="utf-8",
    )
    return package


def _manifest():
    return {
        "kind": "ontology-package",
        "schema_version": "0.1",
        "id": "eco",
        "name": "ECO",
        "description": "变更影响",
        "language": "zh-CN",
    }


def test_resolver_returns_active_static_evidence_and_historical_validity(tmp_path: Path):
    runtime = tmp_path / "runtime"
    manager = EvidenceIndexManager(runtime)
    pointer = manager.build(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_version="1.0.0",
        ontology_dir=_package(tmp_path),
        manifest=_manifest(),
        mutation_revision=1,
    )
    response = EvidenceSearchService(
        manager.open_active("eco"),
        workspace_id="workspace-a",
    ).search(
        ontology_id="eco",
        query="物料",
    )
    evidence_id = response["hits"][0]["evidence_id"]
    resolver = EvidenceResolver(runtime, workspace_id="workspace-a")

    active = resolver.lookup(evidence_id)
    assert active["contract"] == "EvidenceLookup/v1"
    assert active["kind"] == "ontology_evidence"
    assert active["ontology_epoch"] == pointer["expected_epoch"]
    assert active["source"]["path"] == "objects/MPart.md"
    assert active["validity"] == {
        "class": "ontology_evidence",
        "active": True,
        "inactive_reason": None,
    }

    manager.revoke("eco", mutation_revision=2)
    historical = resolver.lookup(evidence_id)
    assert historical["content"] == active["content"]
    assert historical["validity"]["active"] is False
    assert historical["validity"]["inactive_reason"] == "index_stale"

    with pytest.raises(EvidenceResolverError) as exc_info:
        EvidenceResolver(runtime, workspace_id="workspace-b").lookup(evidence_id)
    assert exc_info.value.code == "EVIDENCE_INTEGRITY_FAILED"


def test_resolver_returns_runtime_fact_and_enforces_workspace_scope(tmp_path: Path):
    runtime = tmp_path / "runtime"
    store = RuntimeFactsStore(runtime / "ontology-evidence" / "runtime-facts.sqlite3")
    fact = store.put(
        {
            "workspace_id": "workspace-a",
            "run_id": "run-1",
            "tool_call_id": "call-1",
            "kind": "provider_query",
            "ontology_id": "eco",
            "ontology_epoch": "a" * 64,
            "subject": "MPart",
            "status": "success",
            "summary": "命中 2 条记录。",
            "provider": "neo4j",
            "query_digest": "sha256:" + "b" * 64,
            "row_count": 2,
            "grounding_status": "grounded",
            "grounding_warnings": [],
            "provider_evidence": [
                {
                    "provider_evidence_ref": "graph_ev_001",
                    "provider": "neo4j",
                    "kind": "provider_query_result",
                }
            ],
            "derived_from": ["ont_ev_" + "1" * 40],
            "validity_class": "current_fact",
            "read_at": "2026-07-16T12:00:00+08:00",
        }
    )

    payload = EvidenceResolver(runtime, workspace_id="workspace-a").lookup(fact["evidence_id"])
    assert payload["kind"] == "provider_query"
    assert payload["summary"] == "命中 2 条记录。"
    assert payload["grounding_status"] == "grounded"
    assert payload["provenance"]["provider_evidence"][0]["provider_evidence_ref"] == "graph_ev_001"
    assert payload["redacted_fields"] == []

    with pytest.raises(EvidenceResolverError) as exc_info:
        EvidenceResolver(runtime, workspace_id="workspace-b").lookup(fact["evidence_id"])
    assert exc_info.value.code == "EVIDENCE_NOT_FOUND"


def test_resolver_rejects_invalid_or_unknown_ids(tmp_path: Path):
    resolver = EvidenceResolver(tmp_path / "runtime", workspace_id="workspace-a")
    with pytest.raises(EvidenceResolverError) as invalid:
        resolver.lookup("graph_ev_001")
    assert invalid.value.code == "INVALID_EVIDENCE_ID"

    with pytest.raises(EvidenceResolverError) as missing:
        resolver.lookup("ev_" + "0" * 32)
    assert missing.value.code == "EVIDENCE_NOT_FOUND"
