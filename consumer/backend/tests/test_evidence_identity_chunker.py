"""Evidence epoch, identity, source, and chunk contract tests."""

from __future__ import annotations

import hashlib
from pathlib import Path

from ontology.evidence.chunker import EvidenceChunker
from ontology.evidence.identity import (
    build_chunk_contract_hash,
    build_file_set_digest,
    build_index_generation,
    build_ontology_epoch,
    build_semantic_manifest_digest,
)
from ontology.evidence.source_reader import PackageSourceReader
from ontology.resource_parser import PARSER_VERSION, ResourceParser


def test_epoch_and_generation_change_only_for_contract_inputs():
    manifest = {
        "kind": "ontology",
        "schema_version": "1",
        "id": "eco",
        "name": "ECO",
        "description": "change impact",
        "language": "zh-CN",
        "scope": ["mfg", "plm"],
        "enabled": True,
    }
    manifest_digest = build_semantic_manifest_digest(manifest)
    file_digest = build_file_set_digest({"objects/a.md": "a" * 64})
    epoch = build_ontology_epoch(
        ontology_id="eco",
        active_version="1.0.0",
        semantic_manifest_digest=manifest_digest,
        file_set_digest=file_digest,
    )
    generation = build_index_generation(ontology_epoch=epoch, parser_version=PARSER_VERSION)

    assert len(epoch) == 64
    assert len(generation) == 64
    assert build_semantic_manifest_digest({**manifest, "enabled": False}) == manifest_digest
    assert build_index_generation(ontology_epoch=epoch, parser_version=PARSER_VERSION) == generation
    assert build_index_generation(ontology_epoch=epoch, parser_version="resource-parser/2") != generation


def test_chunker_emits_stable_ids_and_never_uses_frontmatter_secret(tmp_path: Path):
    package = tmp_path / "eco"
    (package / "objects").mkdir(parents=True)
    text = """---
key: eco_object
title: ECO对象
aliases: [变更对象]
password: hidden
---
# ECO对象
## 字段
| 字段 | 含义 |
| --- | --- |
| eco_no | ECO编号 |
"""
    (package / "index.md").write_text("# ECO", encoding="utf-8")
    (package / "objects" / "eco_object.md").write_text(text, encoding="utf-8")
    sources = PackageSourceReader().read(
        ontology_dir=package,
        ontology_id="eco",
        ontology_version="1.0.0",
    )
    object_source = next(source for source in sources.sources if source.resource == "object")
    parsed = ResourceParser().parse(object_source)
    manifest_digest = build_semantic_manifest_digest(
        {"kind": "ontology", "schema_version": "1", "id": "eco", "name": "ECO", "scope": []}
    )
    epoch = build_ontology_epoch(
        ontology_id="eco",
        active_version="1.0.0",
        semantic_manifest_digest=manifest_digest,
        file_set_digest=build_file_set_digest(sources.file_digests),
    )
    generation = build_index_generation(ontology_epoch=epoch, parser_version=PARSER_VERSION)
    chunks_a = EvidenceChunker().chunk(parsed, ontology_epoch=epoch, index_generation=generation)
    chunks_b = EvidenceChunker().chunk(parsed, ontology_epoch=epoch, index_generation=generation)

    assert chunks_a == chunks_b
    assert {chunk.chunk_type for chunk in chunks_a} == {"frontmatter", "markdown_section", "table"}
    assert all(chunk.evidence_id.startswith("ont_ev_") for chunk in chunks_a)
    assert all(len(chunk.text) <= 1800 for chunk in chunks_a)
    assert "hidden" not in "\n".join(chunk.text for chunk in chunks_a)
    assert len(build_chunk_contract_hash(parser_version=PARSER_VERSION)) == 64


def test_source_reader_rejects_secret_assets(tmp_path: Path):
    package = tmp_path / "eco"
    (package / "assets").mkdir(parents=True)
    (package / "index.md").write_text("# ECO", encoding="utf-8")
    (package / "assets" / "safe.txt").write_text("public data", encoding="utf-8")
    (package / "assets" / "secret.txt").write_text("api_key = secret", encoding="utf-8")

    result = PackageSourceReader().read(
        ontology_dir=package,
        ontology_id="eco",
        ontology_version="1.0.0",
    )

    assert "assets/safe.txt" in result.file_digests
    assert "assets/secret.txt" not in result.file_digests
    assert [(warning.code, warning.source_path) for warning in result.warnings] == [
        ("SOURCE_SECRET_REJECTED", "assets/secret.txt")
    ]
