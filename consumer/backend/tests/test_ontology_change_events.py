import json
from pathlib import Path

import pytest
from ontology.change_events import (
    OntologyChangedValidationError,
    build_digest_hints,
    build_ontology_changed_event,
    canonical_event_json,
    empty_digest_hints,
    load_event_json,
    new_ulid,
    validate_ontology_changed_event,
)

SHA_A = "a" * 64
SHA_B = "b" * 64
EVENT_ID = "01JZP7M4H0R6F5T8X2C9K3NQWB"


def test_build_complete_event_is_canonical_and_round_trips():
    hints = build_digest_hints(
        scope_version="0.2.0",
        completeness="complete",
        mutation_revision=43,
        semantic_manifest_digest=SHA_B,
        entries={
            "objects/z.md": {"sha256": SHA_B, "size_bytes": 22},
            "index.md": {"sha256": SHA_A, "size_bytes": 11},
        },
    )
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="aviation-plm-change-impact",
        mutation_type="ACTIVE_VERSION_CHANGED",
        mutation_revision=43,
        before_version="0.1.0",
        after_version="0.2.0",
        known_file_digests=hints,
        operator={"type": "user", "id": "usr-1"},
        request_id="req-1",
        trigger="api",
        event_id=EVENT_ID,
        occurred_at="2026-07-15T15:42:18+08:00",
    )

    assert list(event["known_file_digests"]["entries"]) == ["index.md", "objects/z.md"]
    payload_json = canonical_event_json(event)
    assert load_event_json(payload_json) == event
    assert payload_json.index('"index.md"') < payload_json.index('"objects/z.md"')


def test_partial_event_requires_previous_revision_and_sorts_deleted_paths():
    hints = build_digest_hints(
        scope_version="1.0.0",
        completeness="partial",
        mutation_revision=8,
        entries={"objects/a.md": {"sha256": SHA_A, "size_bytes": 3}},
    )
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        mutation_type="ONTOLOGY_FILE_CHANGED",
        mutation_revision=8,
        before_version="1.0.0",
        after_version="1.0.0",
        known_file_digests=hints,
        deleted_paths=["operations/z.md", "operations/b.md"],
        event_id=EVENT_ID,
        occurred_at="2026-07-15T07:42:18Z",
    )

    assert event["known_file_digests"]["base_mutation_revision"] == 7
    assert event["deleted_paths"] == ["operations/b.md", "operations/z.md"]


@pytest.mark.parametrize(
    ("mutation_type", "before", "after"),
    [
        ("ACTIVE_VERSION_CHANGED", "1", "1"),
        ("ONTOLOGY_FILE_CHANGED", None, None),
        ("ONTOLOGY_DELETED", "1", "1"),
        ("EXTERNAL_DRIFT_DETECTED", "1", None),
    ],
)
def test_invalid_version_combinations_are_rejected(mutation_type, before, after):
    with pytest.raises(OntologyChangedValidationError):
        build_ontology_changed_event(
            workspace_id="workspace-a",
            ontology_id="onto-a",
            mutation_type=mutation_type,
            mutation_revision=1,
            before_version=before,
            after_version=after,
            known_file_digests=empty_digest_hints(scope_version=after),
            event_id=EVENT_ID,
            occurred_at="2026-07-15T07:42:18Z",
        )


def test_none_hints_reject_deleted_paths():
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        mutation_type="ONTOLOGY_VERSION_DELETED",
        mutation_revision=1,
        before_version="1.0.0",
        after_version="1.0.0",
        event_id=EVENT_ID,
        occurred_at="2026-07-15T07:42:18Z",
    )
    event["deleted_paths"] = ["objects/deleted.md"]
    with pytest.raises(OntologyChangedValidationError, match="empty deleted_paths"):
        validate_ontology_changed_event(event)


def test_duplicate_json_keys_are_rejected():
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        mutation_type="ONTOLOGY_VERSION_DELETED",
        mutation_revision=1,
        before_version="1.0.0",
        after_version="1.0.0",
        event_id=EVENT_ID,
        occurred_at="2026-07-15T07:42:18Z",
    )
    payload = canonical_event_json(event)
    duplicate = payload[:-1] + ',"event_id":"' + EVENT_ID + '"}'
    with pytest.raises(OntologyChangedValidationError) as exc_info:
        load_event_json(duplicate)
    assert exc_info.value.code == "DUPLICATE_JSON_KEY"


def test_ulid_has_contract_shape_and_fixed_vector():
    assert new_ulid(timestamp_ms=0, randomness=b"\0" * 10) == "0" * 26
    assert len(new_ulid()) == 26


def test_unknown_fields_are_rejected():
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        mutation_type="ONTOLOGY_VERSION_DELETED",
        mutation_revision=1,
        before_version="1.0.0",
        after_version="1.0.0",
        event_id=EVENT_ID,
        occurred_at="2026-07-15T07:42:18Z",
    )
    payload = json.loads(canonical_event_json(event))
    payload["unexpected"] = True
    with pytest.raises(OntologyChangedValidationError) as exc_info:
        validate_ontology_changed_event(payload)
    assert exc_info.value.code == "UNKNOWN_FIELDS"


def test_json_schema_accepts_the_canonical_event():
    jsonschema = pytest.importorskip("jsonschema")
    schema_path = Path(__file__).parents[1] / "ontology" / "contracts" / "ontology_changed.v1.schema.json"
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="onto-a",
        mutation_type="ONTOLOGY_VERSION_DELETED",
        mutation_revision=1,
        before_version="1.0.0",
        after_version="1.0.0",
        event_id=EVENT_ID,
        occurred_at="2026-07-15T07:42:18Z",
    )
    jsonschema.Draft202012Validator(schema, format_checker=jsonschema.FormatChecker()).validate(event)
