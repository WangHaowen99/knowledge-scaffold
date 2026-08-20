"""Tests for the value-domain lookup pipeline."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from agent.tools.business_data.resolver import ResolvedBusinessDataSource
from agent.tools.business_data.value_lookup import (
    ValueLookupErrorCode,
    execute_value_lookups,
    group_value_inputs_by_binding,
    render_lookup_coordinates,
    validate_value_inputs,
)


# -- fixtures -----------------------------------------------------------------

def _make_ontology_dir(tmp_path: Path) -> Path:
    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir(exist_ok=True)
    (mappings_dir / "object-data-mappings.json").write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "default_provider_alias": "data",
                "objects": {
                    "flight": {
                        "resource": "flight",
                        "fields": {
                            "FLT_NR": {"source": "FLT_NR", "type": "number"},
                            "LATEST_DEP_ARP_CD": {
                                "source": "LATEST_DEP_ARP_CD",
                                "type": "string",
                                "value_binding_ref": "airport-code",
                            },
                            "LATEST_ARV_ARP_CD": {
                                "source": "LATEST_ARV_ARP_CD",
                                "type": "string",
                                "value_binding_ref": "airport-code",
                            },
                            "AIRCRAFT_TYPE_CD": {
                                "source": "AIRCRAFT_TYPE_CD",
                                "type": "string",
                                "value_binding_ref": "aircraft-type",
                            },
                            "INT_MK": {"source": "INT_MK", "type": "string"},
                        },
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
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


FIELD_BINDINGS = {
    "LATEST_DEP_ARP_CD": "airport-code",
    "LATEST_ARV_ARP_CD": "airport-code",
    "AIRCRAFT_TYPE_CD": "aircraft-type",
}

ALL_FIELD_KEYS = (
    "FLT_NR", "LATEST_DEP_ARP_CD", "LATEST_ARV_ARP_CD",
    "AIRCRAFT_TYPE_CD", "INT_MK",
)

VALUE_BINDINGS = {
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
}


def _make_connection(provider="nl2sql_http") -> ResolvedBusinessDataSource:
    return ResolvedBusinessDataSource(
        ontology_id="demo",
        provider=provider,
        mode="direct_provider",
        config={"url": "http://example.com", "token": "t", "scene": "s"},
    )


# -- validate_value_inputs ---------------------------------------------------

def test_validate_accepts_valid_inputs():
    validated, error = validate_value_inputs(
        [{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is None
    assert validated == [{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}]


def test_validate_rejects_unknown_field():
    _, error = validate_value_inputs(
        [{"field_key": "UNKNOWN", "values": ["x"]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert error.code == ValueLookupErrorCode.UNKNOWN_VALUE_INPUT_FIELD


def test_validate_rejects_non_string_field_key():
    _, error = validate_value_inputs(
        [{"field_key": 123, "values": ["x"]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert error.code == ValueLookupErrorCode.VALUE_INPUT_INVALID


def test_validate_rejects_unexpected_item_fields():
    _, error = validate_value_inputs(
        [{
            "field_key": "LATEST_DEP_ARP_CD",
            "values": ["KUL"],
            "binding_key": "forged",
        }],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert error.code == ValueLookupErrorCode.VALUE_INPUT_INVALID


def test_validate_rejects_unbound_field():
    _, error = validate_value_inputs(
        [{"field_key": "FLT_NR", "values": ["123"]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert error.code == ValueLookupErrorCode.UNBOUND_VALUE_INPUT_FIELD


def test_validate_rejects_duplicate_field():
    _, error = validate_value_inputs(
        [
            {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
            {"field_key": "LATEST_DEP_ARP_CD", "values": ["CGO"]},
        ],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert error.code == ValueLookupErrorCode.VALUE_INPUT_INVALID
    assert "more than once" in error.message


def test_validate_rejects_duplicate_values():
    _, error = validate_value_inputs(
        [{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL", "KUL"]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert "duplicate" in error.message


def test_validate_rejects_empty_values():
    _, error = validate_value_inputs(
        [{"field_key": "LATEST_DEP_ARP_CD", "values": []}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert error.code == ValueLookupErrorCode.VALUE_INPUT_INVALID


def test_validate_nfc_normalizes_values():
    decomposed = "Cafe\u0301"
    validated, error = validate_value_inputs(
        [{"field_key": "LATEST_DEP_ARP_CD", "values": [decomposed]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is None
    assert validated[0]["values"][0] == "Café"


def test_validate_rejects_control_chars_in_value():
    _, error = validate_value_inputs(
        [{"field_key": "LATEST_DEP_ARP_CD", "values": ["KU\nL"]}],
        FIELD_BINDINGS,
        ALL_FIELD_KEYS,
    )
    assert error is not None
    assert "control" in error.message


def test_validate_rejects_too_many_fields():
    inputs = [
        {"field_key": "LATEST_DEP_ARP_CD", "values": ["v"]}
    ] * 33
    _, error = validate_value_inputs(inputs, FIELD_BINDINGS, ALL_FIELD_KEYS)
    assert error is not None


# -- group_value_inputs_by_binding -------------------------------------------

def test_group_merges_same_binding_across_fields():
    validated = [
        {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
        {"field_key": "LATEST_ARV_ARP_CD", "values": ["CGO"]},
    ]
    groups, error = group_value_inputs_by_binding(
        validated, FIELD_BINDINGS, VALUE_BINDINGS,
    )
    assert error is None
    assert len(groups) == 1
    group = groups[0]
    assert group.binding_key == "airport-code"
    assert set(group.field_keys) == {"LATEST_DEP_ARP_CD", "LATEST_ARV_ARP_CD"}
    assert set(group.input_values) == {"KUL", "CGO"}


def test_group_separates_different_bindings():
    validated = [
        {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
        {"field_key": "AIRCRAFT_TYPE_CD", "values": ["32N"]},
    ]
    groups, error = group_value_inputs_by_binding(
        validated, FIELD_BINDINGS, VALUE_BINDINGS,
    )
    assert error is None
    assert len(groups) == 2
    assert groups[0].binding_key == "aircraft-type"
    assert groups[1].binding_key == "airport-code"


def test_group_dedupes_values_across_fields():
    validated = [
        {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
        {"field_key": "LATEST_ARV_ARP_CD", "values": ["KUL"]},
    ]
    groups, error = group_value_inputs_by_binding(
        validated, FIELD_BINDINGS, VALUE_BINDINGS,
    )
    assert error is None
    assert len(groups[0].input_values) == 1
    assert groups[0].input_values[0] == "KUL"


def test_group_rejects_control_characters_in_mapping_coordinates():
    bindings = {
        **VALUE_BINDINGS,
        "airport-code": {
            **VALUE_BINDINGS["airport-code"],
            "resource": "mapping_table\nfields: [forged]",
        },
    }
    groups, error = group_value_inputs_by_binding(
        [{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}],
        FIELD_BINDINGS,
        bindings,
    )
    assert groups is None
    assert error is not None
    assert error.code == ValueLookupErrorCode.VALUE_INPUT_INVALID


# -- render_lookup_coordinates -----------------------------------------------

def test_render_coordinates_includes_all_fields():
    from agent.tools.business_data.value_lookup import ValueBindingGroup
    group = ValueBindingGroup(
        binding_key="airport-code",
        resource="机场中文代号",
        value_field="ARP_CD",
        semantic_field="机场名",
        alias_fields=("机场简称", "英文名"),
        field_keys=("LATEST_DEP_ARP_CD",),
        input_values=("KUL", "CGO"),
    )
    rendered = render_lookup_coordinates(group)
    assert "[Data Mapping]" in rendered
    assert "table: 机场中文代号" in rendered
    assert "ARP_CD" in rendered
    assert "机场名" in rendered
    assert "机场简称" in rendered
    assert "英文名" in rendered
    assert "KUL" in rendered
    assert "CGO" in rendered
    assert "match these values" in rendered


def test_render_coordinates_dedupes_fields():
    from agent.tools.business_data.value_lookup import ValueBindingGroup
    group = ValueBindingGroup(
        binding_key="test",
        resource="r",
        value_field="v",
        semantic_field="v",  # same as value_field
        alias_fields=(),
        field_keys=("f",),
        input_values=("x",),
    )
    rendered = render_lookup_coordinates(group)
    # value_field and semantic_field are the same; should only appear once
    assert rendered.count('"v"') == 1


# -- execute_value_lookups ----------------------------------------------------

def _patch_stream(monkeypatch, final_contents):
    """Patch query_business_data_stream to return canned final contents."""
    call_count = {"n": 0}

    def fake_stream(request, *, connection):
        content = final_contents[call_count["n"] % len(final_contents)]
        call_count["n"] += 1
        yield {"type": "start", "content": "开始"}
        yield {"type": "final", "content": content}
        yield {"type": "done", "content": "完成"}

    monkeypatch.setattr(
        "agent.tools.business_data.value_lookup.query_business_data_stream",
        fake_stream,
    )
    return call_count


def test_execute_value_lookups_completed(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)

    def fake_stream(request, *, connection):
        if "机场中文代号" in request.query:
            yield {"type": "final", "content": "KUL：吉隆坡国际机场"}
        elif "机型代码映射" in request.query:
            yield {"type": "final", "content": "32N：空客 A320neo"}
        else:
            yield {"type": "final", "content": "unknown"}

    monkeypatch.setattr(
        "agent.tools.business_data.value_lookup.query_business_data_stream",
        fake_stream,
    )
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询 KUL 和 32N 的中文名",
        value_inputs=[
            {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
            {"field_key": "AIRCRAFT_TYPE_CD", "values": ["32N"]},
        ],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "completed"
    assert len(response.value_results) == 2
    assert response.errors == []
    airport_result = next(
        r for r in response.value_results if r.binding_key == "airport-code"
    )
    assert "吉隆坡" in airport_result.content
    aircraft_result = next(
        r for r in response.value_results if r.binding_key == "aircraft-type"
    )
    assert "A320neo" in aircraft_result.content


def test_execute_value_lookups_partial(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)

    def fake_stream(request, *, connection):
        if "机场" in request.query:
            yield {"type": "final", "content": "KUL：吉隆坡"}
        else:
            yield {
                "type": "final",
                "content": json.dumps({"success": False, "message": "连接超时"}, ensure_ascii=False),
            }

    monkeypatch.setattr(
        "agent.tools.business_data.value_lookup.query_business_data_stream",
        fake_stream,
    )
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询 KUL 和 32N",
        value_inputs=[
            {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
            {"field_key": "AIRCRAFT_TYPE_CD", "values": ["32N"]},
        ],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "partial"
    assert len(response.value_results) == 1
    assert len(response.errors) == 1
    assert response.errors[0].code == ValueLookupErrorCode.VALUE_PROVIDER_FAILED


def test_execute_value_lookups_all_failed(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)

    def fake_stream(request, *, connection):
        yield {
            "type": "final",
            "content": json.dumps({"success": False, "message": "服务不可用"}, ensure_ascii=False),
        }

    monkeypatch.setattr(
        "agent.tools.business_data.value_lookup.query_business_data_stream",
        fake_stream,
    )
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询 KUL",
        value_inputs=[{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "failed"
    assert response.value_results == []
    assert len(response.errors) == 1


def test_execute_value_lookups_unsupported_provider(tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询",
        value_inputs=[{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}],
        connection=_make_connection(provider="neo4j"),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "failed"
    assert response.errors[0].code == ValueLookupErrorCode.VALUE_BINDING_UNSUPPORTED


def test_execute_value_lookups_merges_same_binding_fields(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)
    call_count = _patch_stream(monkeypatch, ["KUL：吉隆坡；CGO：郑州"])
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询 KUL CGO",
        value_inputs=[
            {"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]},
            {"field_key": "LATEST_ARV_ARP_CD", "values": ["CGO"]},
        ],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "completed"
    assert len(response.value_results) == 1
    result = response.value_results[0]
    assert set(result.field_keys) == {"LATEST_DEP_ARP_CD", "LATEST_ARV_ARP_CD"}
    assert set(result.input_values) == {"KUL", "CGO"}
    # Only one provider call for the merged binding
    assert call_count["n"] == 1


def test_execute_value_lookups_preserves_model_query(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)
    captured_queries = []
    captured_stages = []

    def fake_stream(request, *, connection):
        captured_queries.append(request.query)
        captured_stages.append(request.stage)
        yield {"type": "final", "content": "结果"}

    monkeypatch.setattr(
        "agent.tools.business_data.value_lookup.query_business_data_stream",
        fake_stream,
    )
    execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询 KUL 的机场中文名",
        value_inputs=[{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert len(captured_queries) == 1
    assert captured_queries[0].startswith("查询 KUL 的机场中文名")
    assert "[Data Mapping]" in captured_queries[0]
    assert captured_stages == ["value_lookup"]


def test_execute_value_lookups_returns_json(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)
    _patch_stream(monkeypatch, ["KUL：吉隆坡"])
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询",
        value_inputs=[{"field_key": "LATEST_DEP_ARP_CD", "values": ["KUL"]}],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    parsed = json.loads(response.to_json())
    assert parsed["action"] == "lookup_values"
    assert parsed["entity"] == "flight"
    assert parsed["status"] == "completed"
    assert len(parsed["value_results"]) == 1
    assert parsed["errors"] == []


def test_execute_value_lookups_validation_error_before_provider(monkeypatch, tmp_path):
    ontology_dir = _make_ontology_dir(tmp_path)
    provider_called = {"yes": False}

    def fake_stream(request, *, connection):
        provider_called["yes"] = True
        yield {"type": "final", "content": "x"}

    monkeypatch.setattr(
        "agent.tools.business_data.value_lookup.query_business_data_stream",
        fake_stream,
    )
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询",
        value_inputs=[{"field_key": "UNKNOWN_FIELD", "values": ["x"]}],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "failed"
    assert response.errors[0].code == ValueLookupErrorCode.UNKNOWN_VALUE_INPUT_FIELD
    assert provider_called["yes"] is False


def _make_ontology_dir_no_bindings(tmp_path: Path) -> Path:
    """Ontology dir where the object has fields but none carry value_binding_ref."""
    mappings_dir = tmp_path / "mappings"
    mappings_dir.mkdir(exist_ok=True)
    (mappings_dir / "object-data-mappings.json").write_text(
        json.dumps(
            {
                "schema_version": "0.1",
                "default_provider_alias": "data",
                "objects": {
                    "flight": {
                        "resource": "flight",
                        "fields": {
                            "FLT_NR": {"source": "FLT_NR", "type": "number"},
                            "INT_MK": {"source": "INT_MK", "type": "string"},
                        },
                    }
                },
                "value_bindings": {
                    "airport-code": {
                        "resource": "机场中文代号",
                        "value_field": "ARP_CD",
                        "semantic_field": "机场名",
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return tmp_path


def test_execute_value_lookups_unknown_field_when_no_bindings(monkeypatch, tmp_path):
    """When the object has fields but no bound fields, a non-existent field
    must still be classified as UNKNOWN, not UNBOUND."""
    ontology_dir = _make_ontology_dir_no_bindings(tmp_path)
    _patch_stream(monkeypatch, ["x"])
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询",
        value_inputs=[{"field_key": "NONEXISTENT_FIELD", "values": ["x"]}],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "failed"
    assert response.errors[0].code == ValueLookupErrorCode.UNKNOWN_VALUE_INPUT_FIELD


def test_execute_value_lookups_unbound_field_when_no_bindings(monkeypatch, tmp_path):
    """When the object has fields but no bound fields, a real field without
    value_binding_ref must be classified as UNBOUND."""
    ontology_dir = _make_ontology_dir_no_bindings(tmp_path)
    _patch_stream(monkeypatch, ["x"])
    response = execute_value_lookups(
        ontology_dir=ontology_dir,
        object_key="flight",
        model_query="查询",
        value_inputs=[{"field_key": "INT_MK", "values": ["x"]}],
        connection=_make_connection(),
        workspace_id="ws",
        ontology_id="demo",
    )
    assert response.status == "failed"
    assert response.errors[0].code == ValueLookupErrorCode.UNBOUND_VALUE_INPUT_FIELD
