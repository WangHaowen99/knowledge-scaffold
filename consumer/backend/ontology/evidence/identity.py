"""Stable identities for ontology epochs, generations, and evidence chunks."""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass
from typing import Any, Mapping, Optional

from ontology.hash_contract import (
    field_h,
    field_i,
    field_l,
    field_n,
    field_o,
    field_s,
    hash_frames,
    normalize_relative_path,
    value_o,
    value_s,
)

INDEX_SCHEMA_VERSION = "2"
CHUNKER_VERSION = "evidence-chunker/1"
CHUNK_NORMALIZATION_VERSION = "chunk-text/1"
LEXICAL_ANALYZER_VERSION = "zh-bigram/1"
RELATION_EXTRACTOR_VERSION = "declared-edge/1"


@dataclass(frozen=True)
class EvidenceIdentity:
    evidence_id: str
    ontology_id_hash: str
    chunk_contract_hash: str
    chunk_hash: str
    content_hash: str


def build_semantic_manifest_digest(manifest: Mapping[str, Any]) -> str:
    scope = sorted(
        {str(value).strip() for value in manifest.get("scope") or [] if str(value or "").strip()},
        key=lambda value: unicodedata.normalize("NFC", value).encode("utf-8"),
    )
    fields = [
        field_s("kind", str(manifest.get("kind") or "").strip()),
        field_s("schema_version", str(manifest.get("schema_version") or "").strip()),
        field_s("id", str(manifest.get("id") or "").strip()),
        field_s("name", str(manifest.get("name") or "").strip()),
        field_s("description", str(manifest.get("description") or "").strip()),
        field_s("language", str(manifest.get("language") or "").strip()),
        field_l("scope", *(value_s(value) for value in scope)),
    ]
    return hash_frames("semantic_manifest/v1", fields)


def build_file_set_digest(file_digests: Mapping[str, str]) -> str:
    normalized: dict[str, str] = {}
    for path, digest in file_digests.items():
        normalized_path = normalize_relative_path(path)
        if normalized_path in normalized:
            raise ValueError("duplicate normalized ontology file path")
        normalized[normalized_path] = digest
    entries = [
        value_o(field_s("path", path), field_h("sha256", normalized[path]))
        for path in sorted(normalized, key=lambda value: value.encode("utf-8"))
    ]
    return hash_frames("ontology_file_set/v1", [field_l("files", *entries)])


def build_ontology_epoch(
    *,
    ontology_id: str,
    active_version: str,
    semantic_manifest_digest: str,
    file_set_digest: str,
) -> str:
    return hash_frames(
        "ontology_epoch/v1",
        [
            field_s("ontology_id", ontology_id),
            field_s("active_version", active_version),
            field_h("semantic_manifest_digest", semantic_manifest_digest),
            field_h("file_set_digest", file_set_digest),
        ],
    )


def build_chunk_contract_hash(*, parser_version: str) -> str:
    return hash_frames(
        "chunk_contract/v1",
        [
            field_s("index_schema_version", INDEX_SCHEMA_VERSION),
            field_s("parser_version", parser_version),
            field_s("chunker_version", CHUNKER_VERSION),
            field_s("normalization_version", CHUNK_NORMALIZATION_VERSION),
        ],
    )


def build_index_generation(
    *,
    ontology_epoch: str,
    parser_version: str,
    semantic_profile_digest: Optional[str] = None,
) -> str:
    semantic_field = (
        field_h("semantic_profile_digest", semantic_profile_digest)
        if semantic_profile_digest
        else field_n("semantic_profile_digest")
    )
    return hash_frames(
        "index_generation/v1",
        [
            field_h("ontology_epoch", ontology_epoch),
            field_s("index_schema_version", INDEX_SCHEMA_VERSION),
            field_s("parser_version", parser_version),
            field_s("chunker_version", CHUNKER_VERSION),
            field_s("lexical_analyzer_version", LEXICAL_ANALYZER_VERSION),
            field_s("relation_extractor_version", RELATION_EXTRACTOR_VERSION),
            semantic_field,
        ],
    )


def normalize_chunk_text(text: str) -> str:
    lines = [
        unicodedata.normalize("NFC", line).rstrip()
        for line in str(text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    ]
    result: list[str] = []
    blank = False
    for line in lines:
        if not line:
            if result and not blank:
                result.append("")
            blank = True
            continue
        result.append(line)
        blank = False
    while result and not result[-1]:
        result.pop()
    return "\n".join(result)


def build_evidence_id(
    *,
    ontology_id: str,
    ontology_epoch: str,
    parser_version: str,
    resource: str,
    key: str,
    source_path: str,
    section_anchor: str,
    chunk_type: str,
    ordinal: int,
    text: str,
) -> EvidenceIdentity:
    normalized_path = normalize_relative_path(source_path)
    normalized_text = normalize_chunk_text(text)
    content_hash = hash_frames("chunk_content/v1", [field_s("normalized_text", normalized_text)])
    contract_hash = build_chunk_contract_hash(parser_version=parser_version)
    chunk_hash = hash_frames(
        "chunk_identity/v1",
        [
            field_s("resource", resource),
            field_s("key", key),
            field_s("source_path", normalized_path),
            field_s("section_anchor", section_anchor),
            field_s("chunk_type", chunk_type),
            field_i("ordinal", ordinal),
            field_h("content_hash", content_hash),
        ],
    )
    ontology_id_hash = hash_frames("ontology_id/v1", [field_s("ontology_id", ontology_id)])
    evidence_id = "ont_ev_{}_{}_{}_{}".format(
        ontology_id_hash[:12],
        ontology_epoch[:12],
        contract_hash[:8],
        chunk_hash[:16],
    )
    return EvidenceIdentity(evidence_id, ontology_id_hash, contract_hash, chunk_hash, content_hash)
