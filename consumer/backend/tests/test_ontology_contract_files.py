"""Versioned JSON Schema, fixture, and independent HashFrame checks."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from operator import getitem
from pathlib import Path

from jsonschema import Draft202012Validator, FormatChecker
from ontology.change_events import build_digest_hints, build_ontology_changed_event, validate_ontology_changed_event
from ontology.evidence.epoch_state import (
    EpochStateError,
    build_epoch_state,
    read_epoch_state,
    validate_epoch_state,
    write_epoch_state,
)
from ontology.hash_contract import field_i, field_n, field_s, hash_frames
from ontology.resource_parser import ResourceParser
from ontology.resource_types import ResourceSource

CONTRACTS = Path(__file__).parents[1] / "ontology" / "contracts"


def _schema(name: str) -> dict:
    return json.loads((CONTRACTS / name).read_text(encoding="utf-8"))


def test_versioned_schemas_validate_production_contracts(tmp_path: Path):
    event = build_ontology_changed_event(
        workspace_id="workspace-a",
        ontology_id="eco",
        mutation_type="ONTOLOGY_FILE_CHANGED",
        mutation_revision=2,
        before_version="1.0.0",
        after_version="1.0.0",
        known_file_digests=build_digest_hints(
            scope_version="1.0.0",
            completeness="partial",
            mutation_revision=2,
            entries={"objects/eco.md": {"sha256": "a" * 64, "size_bytes": 12}},
        ),
    )
    Draft202012Validator(
        _schema("ontology_changed.v1.schema.json"),
        format_checker=FormatChecker(),
    ).validate(event)

    text = "---\nkey: eco\ntitle: ECO\n---\n# ECO\n## 字段\n编号。\n"
    parsed = ResourceParser().parse(
        ResourceSource(
            ontology_id="eco",
            ontology_version="1.0.0",
            resource="object",
            source_path="objects/eco.md",
            media_type="text/markdown",
            source_text=text,
            source_content_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        )
    )
    serialized = json.loads(json.dumps(parsed.to_dict(), ensure_ascii=False))
    Draft202012Validator(_schema("parsed_resource.v1.schema.json")).validate(serialized)
    Draft202012Validator(_schema("resource_section.v1.schema.json")).validate(serialized["sections"][0])

    state = build_epoch_state(
        workspace_id="workspace-a",
        ontology_id="eco",
        active_version="1.0.0",
        mutation_revision=2,
        last_event_id=event["event_id"],
        semantic_manifest_digest="b" * 64,
        file_digests={"objects/eco.md": "a" * 64},
        file_sizes={"objects/eco.md": 12},
    )
    Draft202012Validator(
        _schema("ontology_epoch_state.v1.schema.json"),
        format_checker=FormatChecker(),
    ).validate(state)
    path = tmp_path / "epoch-state.json"
    write_epoch_state(path, state)
    assert read_epoch_state(path) == state

    corrupted = dict(state)
    corrupted["file_set_digest"] = "0" * 64
    path.write_text(json.dumps(corrupted), encoding="utf-8")
    try:
        read_epoch_state(path)
    except EpochStateError as exc:
        assert exc.code == "EPOCH_STATE_INTEGRITY_FAILED"
    else:
        raise AssertionError("corrupted epoch state must fail closed")


def test_hash_vectors_match_production_and_independent_encoder():
    document = json.loads((CONTRACTS / "hash_frame.v1.vectors.json").read_text(encoding="utf-8"))
    assert document["contract"] == "HashFrameVectors/v1"
    for vector in document["vectors"]:
        production_fields = []
        for item in vector["fields"]:
            if item["type"] == "s":
                production_fields.append(field_s(item["name"], item["value"]))
            elif item["type"] == "n":
                production_fields.append(field_n(item["name"]))
            elif item["type"] == "i":
                production_fields.append(field_i(item["name"], item["value"]))
            else:
                raise AssertionError("unsupported vector type")
        assert hash_frames(vector["domain"], production_fields) == vector["sha256"]
        assert _independent_hash(vector["domain"], vector["fields"]) == vector["sha256"]


def test_normative_fixtures_validate_schemas_and_production_readers():
    fixtures = CONTRACTS / "fixtures"
    pairs = (
        ("ontology_changed.v1.json", "ontology_changed.v1.schema.json"),
        ("ontology_epoch_state.v1.json", "ontology_epoch_state.v1.schema.json"),
        ("parsed_resource.v1.json", "parsed_resource.v1.schema.json"),
        ("resource_section.v1.json", "resource_section.v1.schema.json"),
    )
    loaded = {}
    for fixture_name, schema_name in pairs:
        fixture = json.loads((fixtures / fixture_name).read_text(encoding="utf-8"))
        Draft202012Validator(
            _schema(schema_name),
            format_checker=FormatChecker(),
        ).validate(fixture)
        loaded[fixture_name] = fixture
    validate_ontology_changed_event(getitem(loaded, "ontology_changed.v1.json"))
    validate_epoch_state(getitem(loaded, "ontology_epoch_state.v1.json"))

    parsed_fixture = getitem(loaded, "parsed_resource.v1.json")
    source_text = parsed_fixture["display_text"]
    parsed = ResourceParser().parse(
        ResourceSource(
            ontology_id="eco",
            ontology_version="1.0.0",
            resource="object",
            source_path="objects/eco.md",
            media_type="text/markdown",
            source_text=source_text,
            source_content_sha256=hashlib.sha256(source_text.encode("utf-8")).hexdigest(),
        )
    )
    assert json.loads(json.dumps(parsed.to_dict(), ensure_ascii=False)) == parsed_fixture


def _independent_hash(domain: str, fields: list[dict]) -> str:
    payload = bytearray(b"OntologyEvidenceHash\x1ev1\x1e")
    payload.extend(domain.encode("ascii"))
    payload.extend(b"\x1e")
    for field in fields:
        name = unicodedata.normalize("NFC", field["name"]).encode("utf-8")
        kind = field["type"].encode("ascii")
        if field["type"] == "s":
            value = unicodedata.normalize("NFC", field["value"]).encode("utf-8")
        elif field["type"] == "n":
            value = b""
        elif field["type"] == "i":
            value = str(field["value"]).encode("ascii")
        else:
            raise AssertionError("unsupported vector type")
        payload.extend(str(len(name)).encode("ascii") + b"\x1f" + name + b"\x1f")
        payload.extend(kind + b"\x1f" + str(len(value)).encode("ascii") + b"\x1f")
        payload.extend(value + b"\x1e")
    return hashlib.sha256(payload).hexdigest()
