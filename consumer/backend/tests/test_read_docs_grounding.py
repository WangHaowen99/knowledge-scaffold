"""Grounding index compatibility and atomic merge tests."""

from __future__ import annotations

from utils.read_docs import (
    build_grounding_subject_key,
    merge_read_documents_registry,
    normalize_read_documents_registry,
)

EPOCH = "a" * 64


def _binding(evidence_id: str, subject_key: str):
    return {
        "evidence_id": evidence_id,
        "subject_key": subject_key,
        "workspace_id": "workspace-a",
        "ontology_id": "eco",
        "ontology_version": "1.0.0",
        "ontology_epoch": EPOCH,
        "mutation_revision": 2,
        "index_generation": "b" * 64,
        "resource": "object",
        "resource_key": "eco_object",
        "section_anchor": "eco对象/字段",
        "source_content_sha256": "sha256:" + "c" * 64,
        "loaded_turn_id": 1,
        "loaded_iteration": 2,
    }


def test_historical_registry_gets_empty_grounding_without_backfill():
    registry = normalize_read_documents_registry(
        {"by_path": {"objects/a.md": {"path": "objects/a.md"}}, "recent_paths": ["objects/a.md"]}
    )
    assert registry["grounding_index"] == {"by_evidence_id": {}, "by_subject": {}}


def test_record_and_binding_merge_atomically_and_preserve_multiple_sections():
    subject = build_grounding_subject_key(
        workspace_id="workspace-a",
        ontology_id="eco",
        ontology_epoch=EPOCH,
        resource="object",
        resource_key="eco_object",
    )
    first_id = "ont_ev_" + "1" * 40
    second_id = "ont_ev_" + "2" * 40
    registry = merge_read_documents_registry(
        None,
        [
            {
                "path": "objects/eco_object.md",
                "doc_type": "object_doc",
                "_grounding_bindings": [_binding(first_id, subject), _binding(second_id, subject)],
            }
        ],
    )

    assert set(registry["grounding_index"]["by_evidence_id"]) == {first_id, second_id}
    assert registry["grounding_index"]["by_subject"][subject] == [first_id, second_id]
    assert "_grounding_bindings" not in registry["by_path"]["objects/eco_object.md"]

    merged = merge_read_documents_registry(
        registry,
        [{"path": "objects/eco_object.md", "_grounding_bindings": [_binding(first_id, subject)]}],
    )
    assert merged["grounding_index"]["by_evidence_id"][first_id]["read_count"] == 2
    assert merged["grounding_index"]["by_subject"][subject] == [second_id, first_id]
