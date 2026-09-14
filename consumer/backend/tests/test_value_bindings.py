# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for value_binding_ref validation and runtime helpers."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from ontology.ontology_catalog import (
    ValueBindingValidationResult,
    get_object_field_bindings,
    get_object_mapping_field_keys,
    get_object_value_bound_fields,
    load_value_bindings,
    validate_value_bindings,
)
from ontology.package_import import validate_imported_object_aliases


def _write_mapping(tmp_path: Path, payload: dict) -> None:
    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir(exist_ok=True)
    (mappings_dir / "object-data-mappings.json").write_text(
        json.dumps(payload, ensure_ascii=False),
        encoding="utf-8",
    )


def _full_mapping_payload() -> dict:
    return {
        "schema_version": "0.1",
        "default_provider_alias": "data",
        "objects": {
            "flight": {
                "provider_alias": "data",
                "resource": "flight",
                "primary_key": ["FLT_ID"],
                "fields": {
                    "FLT_ID": {"source": "FLT_ID", "type": "number", "required": True},
                    "LATEST_DEP_ARP_CD": {
                        "source": "LATEST_DEP_ARP_CD",
                        "type": "string",
                        "required": True,
                        "value_binding_ref": "airport-code",
                    },
                    "LATEST_ARV_ARP_CD": {
                        "source": "LATEST_ARV_ARP_CD",
                        "type": "string",
                        "required": True,
                        "value_binding_ref": "airport-code",
                    },
                    "AIRCRAFT_TYPE_CD": {
                        "source": "AIRCRAFT_TYPE_CD",
                        "type": "string",
                        "required": False,
                        "value_binding_ref": "aircraft-type",
                    },
                    "INT_MK": {"source": "INT_MK", "type": "string", "required": True},
                },
                "policy": {"read_only": True, "max_rows": 1000},
            }
        },
        "value_bindings": {
            "airport-code": {
                "resource": "机场中文代号",
                "value_field": "ARP_CD",
                "semantic_field": "机场名",
                "alias_fields": ["机场简称", "英文名"],
            },
            "aircraft-type": {
                "resource": "机型代码映射",
                "value_field": "AIRCRAFT_TYPE_CD",
                "semantic_field": "AIRCRAFT_TYPE_NAME_ZH",
                "alias_fields": ["AIRCRAFT_TYPE_ALIAS"],
            },
        },
    }


# -- validate_value_bindings -------------------------------------------------


def test_validate_value_bindings_passes_for_valid_refs(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    result = validate_value_bindings(tmp_path)
    assert result.errors == ()
    assert result.warnings == ()


def test_validate_value_bindings_blocks_unknown_ref(tmp_path):
    payload = _full_mapping_payload()
    payload["objects"]["flight"]["fields"]["LATEST_DEP_ARP_CD"]["value_binding_ref"] = "nonexistent"
    _write_mapping(tmp_path, payload)
    result = validate_value_bindings(tmp_path)
    assert any("unknown binding 'nonexistent'" in e for e in result.errors)
    assert result.warnings == ()


def test_validate_value_bindings_warns_on_unreferenced_binding(tmp_path):
    payload = _full_mapping_payload()
    payload["value_bindings"]["unused-binding"] = {
        "resource": "test",
        "value_field": "T",
        "semantic_field": "S",
    }
    _write_mapping(tmp_path, payload)
    result = validate_value_bindings(tmp_path)
    assert result.errors == ()
    assert any("unused-binding" in w for w in result.warnings)


def test_validate_value_bindings_blocks_refs_when_no_value_bindings(tmp_path):
    payload = _full_mapping_payload()
    del payload["value_bindings"]
    _write_mapping(tmp_path, payload)
    result = validate_value_bindings(tmp_path)
    assert any("unknown binding 'airport-code'" in error for error in result.errors)
    assert any("unknown binding 'aircraft-type'" in error for error in result.errors)
    assert result.warnings == ()


def test_validate_value_bindings_passes_when_no_bindings_or_refs(tmp_path):
    payload = _full_mapping_payload()
    del payload["value_bindings"]
    for field in payload["objects"]["flight"]["fields"].values():
        field.pop("value_binding_ref", None)
    _write_mapping(tmp_path, payload)
    result = validate_value_bindings(tmp_path)
    assert result.errors == ()
    assert result.warnings == ()


def test_validate_value_bindings_validates_legacy_top_level_object_format(tmp_path):
    """Legacy single-object format (fields at payload root) must also be."""
    # Details: Legacy single-object format (fields at payload root) must also be validated, not silently skipped.
    payload = _full_mapping_payload()
    flight = payload["objects"]["flight"]
    top_level = {
        "schema_version": payload.get("schema_version"),
        "default_provider_alias": payload.get("default_provider_alias"),
        "object_key": "flight",
        "provider_alias": flight.get("provider_alias"),
        "resource": flight.get("resource"),
        "primary_key": flight.get("primary_key"),
        "fields": flight["fields"],
        "policy": flight.get("policy"),
        "value_bindings": payload["value_bindings"],
    }
    # Introduce a dangling reference in the top-level fields
    top_level["fields"]["LATEST_DEP_ARP_CD"]["value_binding_ref"] = "dangling-ref"
    _write_mapping(tmp_path, top_level)
    result = validate_value_bindings(tmp_path)
    assert any("unknown binding 'dangling-ref'" in e for e in result.errors)


@pytest.mark.parametrize("invalid_bindings", [[], None])
def test_validate_value_bindings_rejects_non_object_binding_map(
    tmp_path,
    invalid_bindings,
):
    payload = _full_mapping_payload()
    payload["value_bindings"] = invalid_bindings
    _write_mapping(tmp_path, payload)
    result = validate_value_bindings(tmp_path)
    assert result.errors == ("value_bindings must be a JSON object",)


def test_validate_value_bindings_passes_when_no_mapping_file(tmp_path):
    result = validate_value_bindings(tmp_path)
    assert result.errors == ()
    assert result.warnings == ()


def test_validate_value_bindings_blocks_invalid_json(tmp_path):
    (tmp_path / "mappings").mkdir()
    (tmp_path / "mappings" / "object-data-mappings.json").write_bytes(b"\xff\xfe")
    result = validate_value_bindings(tmp_path)
    assert len(result.errors) == 1


# -- import integration -------------------------------------------------------


def test_import_validation_blocks_unknown_binding_ref(tmp_path):
    payload = _full_mapping_payload()
    payload["objects"]["flight"]["fields"]["LATEST_DEP_ARP_CD"]["value_binding_ref"] = "bad-ref"
    _write_mapping(tmp_path, payload)
    result = validate_imported_object_aliases(tmp_path)
    assert any("unknown binding 'bad-ref'" in e for e in result.errors)


def test_import_validation_blocks_missing_binding_map_with_refs(tmp_path):
    payload = _full_mapping_payload()
    del payload["value_bindings"]
    _write_mapping(tmp_path, payload)
    result = validate_imported_object_aliases(tmp_path)
    assert any("unknown binding 'airport-code'" in error for error in result.errors)


def test_import_validation_warns_on_unreferenced_binding(tmp_path):
    payload = _full_mapping_payload()
    payload["value_bindings"]["unused"] = {
        "resource": "r",
        "value_field": "v",
        "semantic_field": "s",
    }
    _write_mapping(tmp_path, payload)
    result = validate_imported_object_aliases(tmp_path)
    assert result.errors == ()
    assert any("unused" in w for w in result.warnings)


def test_import_validation_passes_for_valid_bindings(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    result = validate_imported_object_aliases(tmp_path)
    assert result.errors == ()


# -- runtime helpers ----------------------------------------------------------


def test_load_value_bindings(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    bindings = load_value_bindings(tmp_path)
    assert "airport-code" in bindings
    assert bindings["airport-code"]["resource"] == "机场中文代号"


def test_load_value_bindings_empty_when_no_file(tmp_path):
    assert load_value_bindings(tmp_path) == {}


def test_get_object_field_bindings(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    result = get_object_field_bindings(tmp_path, "flight")
    assert result == {
        "LATEST_DEP_ARP_CD": "airport-code",
        "LATEST_ARV_ARP_CD": "airport-code",
        "AIRCRAFT_TYPE_CD": "aircraft-type",
    }


def test_get_object_field_bindings_empty_for_unknown_object(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    assert get_object_field_bindings(tmp_path, "nonexistent") == {}


def test_get_object_mapping_field_keys(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    keys = get_object_mapping_field_keys(tmp_path, "flight")
    assert set(keys) == {
        "FLT_ID",
        "LATEST_DEP_ARP_CD",
        "LATEST_ARV_ARP_CD",
        "AIRCRAFT_TYPE_CD",
        "INT_MK",
    }


def test_get_object_value_bound_fields(tmp_path):
    _write_mapping(tmp_path, _full_mapping_payload())
    fields = get_object_value_bound_fields(tmp_path, "flight")
    assert set(fields) == {"LATEST_DEP_ARP_CD", "LATEST_ARV_ARP_CD", "AIRCRAFT_TYPE_CD"}


def test_get_object_value_bound_fields_excludes_dangling_ref(tmp_path):
    payload = _full_mapping_payload()
    # Remove the binding that LATEST_DEP_ARP_CD references
    del payload["value_bindings"]["airport-code"]
    _write_mapping(tmp_path, payload)
    fields = get_object_value_bound_fields(tmp_path, "flight")
    assert "LATEST_DEP_ARP_CD" not in fields
    assert "LATEST_ARV_ARP_CD" not in fields
    assert "AIRCRAFT_TYPE_CD" in fields
