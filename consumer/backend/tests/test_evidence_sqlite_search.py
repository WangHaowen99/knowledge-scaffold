"""SQLite evidence generation and retrieval tests."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from ontology.evidence.chunker import EvidenceChunker
from ontology.evidence.identity import (
    CHUNKER_VERSION,
    LEXICAL_ANALYZER_VERSION,
    RELATION_EXTRACTOR_VERSION,
    build_index_generation,
)
from ontology.evidence.service import EvidenceSearchError, EvidenceSearchService
from ontology.evidence.sqlite_index import SQLiteEvidenceIndex
from ontology.resource_parser import PARSER_VERSION, ResourceParser
from ontology.resource_types import ResourceSource


EPOCH = "a" * 64


def _parsed(text: str, path: str, resource: str):
    return ResourceParser().parse(
        ResourceSource(
            ontology_id="eco",
            ontology_version="1.0.0",
            resource=resource,
            source_path=path,
            media_type="text/markdown",
            source_text=text,
            source_content_sha256=hashlib.sha256(text.encode()).hexdigest(),
        )
    )


def _build_index(path: Path) -> SQLiteEvidenceIndex:
    generation = build_index_generation(ontology_epoch=EPOCH, parser_version=PARSER_VERSION)
    operation = _parsed(
        """---
key: eco_change_impact_analysis
title: ECO变更影响分析
aliases: [设计变更影响制造物料, "ECO_100%", "引号'别名", 甲]
---
# ECO变更影响分析
## 流程
从 [[object:eco_affected_object|uses]] 出发追溯 EPart 版本和 MPart。
""",
        "operations/eco_change_impact_analysis.md",
        "operation",
    )
    object_resource = _parsed(
        """---
key: eco_affected_object
title: ECO受影响对象
---
# ECO受影响对象
## 字段
ECO编号、对象类型、变更前版本和变更后版本。
""",
        "objects/eco_affected_object.md",
        "object",
    )
    chunker = EvidenceChunker()
    chunks = [
        *chunker.chunk(operation, ontology_epoch=EPOCH, index_generation=generation),
        *chunker.chunk(object_resource, ontology_epoch=EPOCH, index_generation=generation),
    ]
    SQLiteEvidenceIndex.build(
        path,
        metadata={
            "workspace_id": "workspace-a",
            "ontology_id": "eco",
            "ontology_version": "1.0.0",
            "ontology_epoch": EPOCH,
            "index_generation": generation,
            "parser_version": PARSER_VERSION,
            "chunker_version": CHUNKER_VERSION,
            "lexical_analyzer_version": LEXICAL_ANALYZER_VERSION,
            "relation_extractor_version": RELATION_EXTRACTOR_VERSION,
            "file_count": 2,
            "built_at": datetime.now(timezone.utc).isoformat(),
            "build_id": "test-build",
        },
        chunks=chunks,
    )
    return SQLiteEvidenceIndex(path)


def test_search_resolves_exact_alias_and_returns_structured_next_action(tmp_path: Path):
    service = EvidenceSearchService(
        _build_index(tmp_path / "index.sqlite3"),
        workspace_id="workspace-a",
    )

    response = service.search(ontology_id="eco", query="设计变更影响制造物料", limit=5)

    assert response["status"] == "resolved"
    assert response["hits"][0]["key"] == "eco_change_impact_analysis"
    assert response["hits"][0]["next_action"]["arguments"]["response_format"] == "structured"
    assert response["hits"][0]["source_path"] == "operations/eco_change_impact_analysis.md"


@pytest.mark.parametrize("query", ["ECO_100%", "引号'别名", "设计变更影响制造物料"])
def test_search_resolves_special_character_and_unicode_aliases(tmp_path: Path, query: str):
    service = EvidenceSearchService(
        _build_index(tmp_path / "index.sqlite3"),
        workspace_id="workspace-a",
    )

    response = service.search(ontology_id="eco", query=query, limit=5)

    assert response["status"] == "resolved"
    assert response["hits"][0]["key"] == "eco_change_impact_analysis"
    assert "EXACT_ALIAS" in response["hits"][0]["reason_codes"]


@pytest.mark.parametrize("query", ["%", "_"])
def test_search_does_not_treat_sql_wildcards_as_exact_aliases(tmp_path: Path, query: str):
    service = EvidenceSearchService(
        _build_index(tmp_path / "index.sqlite3"),
        workspace_id="workspace-a",
    )

    response = service.search(ontology_id="eco", query=query, limit=5)

    assert response["status"] == "no_match"
    assert response["hits"] == []


def test_search_uses_chinese_bigrams_and_lookup_is_stable(tmp_path: Path):
    index = _build_index(tmp_path / "index.sqlite3")
    response = EvidenceSearchService(index, workspace_id="workspace-a").search(
        ontology_id="eco", query="变更后版本字段", limit=3
    )

    assert response["hits"]
    hit = response["hits"][0]
    assert hit["key"] == "eco_affected_object"
    assert index.lookup(hit["evidence_id"])["content_hash"] == hit["content_hash"].removeprefix("sha256:")


def test_search_returns_no_match_without_query_rewrite(tmp_path: Path):
    service = EvidenceSearchService(
        _build_index(tmp_path / "index.sqlite3"),
        workspace_id="workspace-a",
    )
    response = service.search(ontology_id="eco", query="量子天气预测", limit=5)

    assert response["status"] == "no_match"
    assert response["hits"] == []
    assert response["guidance"]["action"] == "refine_query_or_load_root"


def test_graph_neighbor_expansion_is_explicit_and_response_is_budgeted(tmp_path: Path):
    index = _build_index(tmp_path / "index.sqlite3")
    response = EvidenceSearchService(
        index,
        workspace_id="workspace-a",
        graph_enabled=True,
    ).search(
        ontology_id="eco",
        query="设计变更影响制造物料",
        limit=10,
    )

    assert response["retrieval"]["mode"] == "lexical_graph"
    assert any("GRAPH_NEIGHBOR" in hit["reason_codes"] for hit in response["hits"])
    assert len(json.dumps(response, ensure_ascii=False, separators=(",", ":"))) <= 6000


def test_search_rejects_an_index_from_another_workspace(tmp_path: Path):
    index = _build_index(tmp_path / "index.sqlite3")
    metadata = index.metadata()
    assert metadata["workspace_id"] == "workspace-a"
    assert metadata["index_schema_version"] == "2"

    service = EvidenceSearchService(index, workspace_id="workspace-b")
    with pytest.raises(EvidenceSearchError) as exc_info:
        service.search(ontology_id="eco", query="物料", limit=3)

    assert getattr(exc_info.value, "code", None) == "ONTOLOGY_NOT_FOUND"
