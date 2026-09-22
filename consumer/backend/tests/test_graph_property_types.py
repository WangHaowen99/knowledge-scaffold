# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Synthetic tests for graph physical-type normalization."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

import pytest

from agent.tools.business_data.providers.graph.filter_expr import build_filter_where
from agent.tools.business_data.providers.graph.models import GraphScope
from agent.tools.business_data.providers.graph.property_types import (
    BOOLEAN,
    INTEGER,
    LIST,
    UNION,
    GraphFilterTypeError,
    GraphFilterTypeResolver,
    extract_pattern_filter_targets,
    normalize_filter_dict,
    normalize_pattern_filters,
    parse_graph_property_type,
)


def _scope() -> GraphScope:
    return GraphScope(
        workspace_property="workspace_key",
        workspace_value="workspace_demo",
        ontology_property="ontology_key",
        ontology_value="ontology_demo",
    )


class _TypedBackend:
    provider = "neo4j"

    def __init__(
        self,
        types=None,
        *,
        complete=True,
        fail=False,
        authoritative_fields=None,
        physical_relation_types=None,
    ):
        self.types = dict(types or {})
        self.complete = complete
        self.fail = fail
        self.authoritative_fields = authoritative_fields
        self.physical_relation_types = physical_relation_types
        self.calls = []

    def get_property_types(
        self,
        *,
        element_class,
        element_type,
        property_names,
        scope,
    ):
        self.calls.append(
            (element_type, element_class, tuple(property_names), scope.workspace_value)
        )
        if self.fail:
            raise RuntimeError("synthetic lookup failure")
        result = {
            "property_types": {
                field: self.types[field]
                for field in property_names
                if field in self.types
            },
            "complete": self.complete,
            "source": "synthetic_database",
        }
        if self.authoritative_fields is not None:
            result["authoritative_fields"] = list(self.authoritative_fields)
        if self.physical_relation_types is not None:
            result["physical_relation_types"] = list(self.physical_relation_types)
        return result


def _schema(field_types, *, truncated=False):
    return {
        "source": "provider_preload",
        "nodes_schema": {
            "labels": ["SyntheticEvent"],
            "schema": {"SyntheticEvent": dict(field_types)},
            "fields_truncated": ["SyntheticEvent"] if truncated else [],
        },
        "edges_schema": {},
    }


def test_parses_provider_type_families_and_merges_equivalent_aliases():
    assert parse_graph_property_type("BOOLEAN").kind == BOOLEAN
    assert parse_graph_property_type("LIST<INTEGER NOT NULL>").kind == LIST
    assert parse_graph_property_type("LIST<INTEGER NOT NULL>").item_type.kind == INTEGER

    equivalent = parse_graph_property_type("INT|LONG")
    assert equivalent.kind == INTEGER
    assert equivalent.integer_bounds == (-(2**63), 2**63 - 1)

    ambiguous = parse_graph_property_type("INTEGER|STRING")
    assert ambiguous.kind == UNION
    with pytest.raises(GraphFilterTypeError, match="多个物理类型"):
        normalize_filter_dict({"value": "42"}, {"value": ambiguous})


@pytest.mark.parametrize(
    ("raw_type", "raw_value"),
    [
        ("BOOLEAN", "yes"),
        ("BYTE", "128"),
        ("INTEGER", "12.5"),
        ("FLOAT", "NaN"),
        ("DATE", "2026-02-30"),
        ("LOCAL DATETIME", "2026-01-02T03:04:05+08:00"),
        ("ZONED DATETIME", "2026-01-02T03:04:05"),
        ("LIST<INTEGER>", "1,2"),
    ],
)
def test_rejects_invalid_or_lossy_typed_operands(raw_type, raw_value):
    with pytest.raises(GraphFilterTypeError):
        normalize_filter_dict({"value": raw_value}, {"value": raw_type})


def test_integer_accepts_exact_numeric_transport_but_rejects_unsafe_float():
    assert normalize_filter_dict(
        {"value": 42.0},
        {"value": "INTEGER"},
    ).filter_dict == {"value": 42}
    assert normalize_filter_dict(
        {"value": Decimal("42")},
        {"value": "INTEGER"},
    ).filter_dict == {"value": 42}

    with pytest.raises(GraphFilterTypeError, match="不能无损转换"):
        normalize_filter_dict(
            {"value": float(2**53)},
            {"value": "INTEGER"},
        )


def test_normalizes_scalar_temporal_and_collection_operands_without_float_roundtrip():
    large_integer_text = "9007199254740993"
    result = normalize_filter_dict(
        {
            "event_key": large_integer_text,
            "enabled": "false",
            "ratio": "12.5",
            "day": "2026-01-02",
            "local_at": "2026-01-02T03:04:05",
            "zoned_at": "2026-01-02T03:04:05+08:00",
            "tags": {"contains": "7"},
            "vector": ["1.5", 2],
            "metadata": {"tier": "gold"},
            "location": {"x": 1, "y": 2},
        },
        {
            "event_key": "INTEGER",
            "enabled": "BOOLEAN",
            "ratio": "FLOAT",
            "day": "DATE",
            "local_at": "LOCAL DATETIME",
            "zoned_at": "ZONED DATETIME",
            "tags": "LIST<INTEGER>",
            "vector": "VECTOR<FLOAT>",
            "metadata": "MAP",
            "location": "POINT",
        },
        provider="neo4j",
    ).filter_dict

    assert result["event_key"] == 9007199254740993
    assert type(result["event_key"]) is int
    assert result["enabled"] is False
    assert result["ratio"] == 12.5
    assert result["day"] == date(2026, 1, 2)
    assert result["local_at"] == datetime(2026, 1, 2, 3, 4, 5)
    assert result["zoned_at"].utcoffset() is not None
    assert result["tags"] == {"contains_item": 7}
    assert result["vector"]["eq"] == [1.5, 2.0]
    assert result["metadata"] == {"eq": {"tier": "gold"}}
    assert "eq" in result["location"]


def test_neo4j_native_duration_remains_one_parameter_value():
    from neo4j.time import Duration

    duration = Duration.from_iso_format("P1D")
    normalized = normalize_filter_dict(
        {"elapsed": duration},
        {"elapsed": "DURATION"},
        provider="neo4j",
    ).filter_dict
    params = {}
    conditions = build_filter_where(normalized, "e", params, "fp")

    assert conditions == ["e.elapsed = $fp_elapsed_0"]
    assert params["fp_elapsed_0"] is duration


def test_text_types_stay_text_for_numeric_boolean_and_or_expressions():
    result = normalize_filter_dict(
        {
            "numeric_text": "00042",
            "boolean_text": False,
            "choice_text": "= 00042 OR = 00043",
            "phrase": "'NORTH OR SOUTH'",
        },
        {
            "numeric_text": "STRING",
            "boolean_text": "STRING",
            "choice_text": "STRING",
            "phrase": "STRING",
        },
    ).filter_dict

    assert result["numeric_text"] == {"eq": "00042"}
    assert result["boolean_text"] == {"eq": "false"}
    assert result["choice_text"] == {
        "or": [{"eq": "00042"}, {"eq": "00043"}]
    }
    assert result["phrase"] == {"eq": "NORTH OR SOUTH"}


def test_structured_or_normalizes_plain_branches_into_condition_objects():
    result = normalize_filter_dict(
        {"event_key": {"or": ["41", "42"]}},
        {"event_key": "INTEGER"},
    ).filter_dict

    assert result == {
        "event_key": {"or": [{"eq": 41}, {"eq": 42}]},
    }
    params = {}
    conditions = build_filter_where(result, "e", params, "fp")
    assert " OR " in conditions[0]
    assert sorted(params.values()) == [41, 42]


def test_compact_schema_is_fast_path_and_truncated_schema_uses_database_override():
    fast_backend = _TypedBackend({"event_key": "INTEGER"})
    fast_resolver = GraphFilterTypeResolver(
        backend=fast_backend,
        scope=_scope(),
        schema_context=_schema({"event_key": "STRING"}),
        provider="neo4j",
    )
    fast = fast_resolver.normalize_filter(
        {"event_key": "00042"},
        element_type="NODE",
        element_class="SyntheticEvent",
    ).filter_dict
    assert fast == {"event_key": {"eq": "00042"}}
    assert fast_backend.calls == []

    fallback_backend = _TypedBackend({"event_key": "INTEGER"})
    fallback_resolver = GraphFilterTypeResolver(
        backend=fallback_backend,
        scope=_scope(),
        schema_context=_schema({"event_key": "STRING"}, truncated=True),
        provider="neo4j",
    )
    fallback = fallback_resolver.normalize_filter(
        {"event_key": "42"},
        element_type="NODE",
        element_class="SyntheticEvent",
    ).filter_dict
    assert fallback == {"event_key": 42}
    assert fallback_backend.calls == [
        ("NODE", "SyntheticEvent", ("event_key",), "workspace_demo")
    ]


def test_sampled_compact_types_are_not_authoritative_without_provider_metadata():
    backend = _TypedBackend({"event_key": "INTEGER"})
    sampled_schema = _schema({"event_key": "STRING"})
    sampled_schema["nodes_schema"]["types_authoritative"] = False
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=_scope(),
        schema_context=sampled_schema,
        provider="neo4j",
    )

    assert resolver.normalize_filter(
        {"event_key": "42"},
        element_type="NODE",
        element_class="SyntheticEvent",
    ).filter_dict == {"event_key": 42}
    assert backend.calls == [
        ("NODE", "SyntheticEvent", ("event_key",), "workspace_demo")
    ]


def test_business_edge_alias_does_not_discard_physical_schema_type_before_lookup():
    backend = _TypedBackend(
        {"edge_key": "INTEGER"},
        physical_relation_types=[],
    )
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=_scope(),
        schema_context={
            "source": "provider_preload",
            "nodes_schema": {},
            "edges_schema": {
                "relationship_types": ["BusinessLink", "PhysicalLink"],
                "schema": {"PhysicalLink": {"edge_key": "STRING"}},
                "field_complete": {"PhysicalLink": True},
                "relation_triples": [{
                    "relation_type": "BusinessLink",
                    "physical_relation_type": "PhysicalLink",
                }],
            },
        },
        provider="neo4j",
    )

    assert resolver.normalize_filter(
        {"edge_key": "42"},
        element_type="EDGE",
        element_class="BusinessLink",
    ).filter_dict == {"edge_key": 42}
    assert backend.calls == [
        ("EDGE", "BusinessLink", ("edge_key",), "workspace_demo")
    ]


def test_request_resolver_fetches_new_fields_once_each():
    backend = _TypedBackend({"first_key": "INTEGER", "second_key": "BOOLEAN"})
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=_scope(),
        schema_context=_schema({}, truncated=True),
        provider="neo4j",
    )

    assert resolver.normalize_filter(
        {"first_key": "7"},
        element_type="NODE",
        element_class="SyntheticEvent",
    ).filter_dict == {"first_key": 7}
    assert resolver.normalize_filter(
        {"second_key": "true"},
        element_type="NODE",
        element_class="SyntheticEvent",
    ).filter_dict == {"second_key": True}
    resolver.normalize_filter(
        {"first_key": "8"},
        element_type="NODE",
        element_class="SyntheticEvent",
    )

    assert [call[2] for call in backend.calls] == [("first_key",), ("second_key",)]


def test_incomplete_or_unavailable_database_result_preserves_input():
    for backend in (
        _TypedBackend({"event_key": "INTEGER"}, complete=False),
        _TypedBackend({"event_key": "INTEGER"}, fail=True),
    ):
        resolver = GraphFilterTypeResolver(
            backend=backend,
            scope=_scope(),
            schema_context=_schema({"event_key": "STRING"}, truncated=True),
            provider="neo4j",
        )
        assert resolver.normalize_filter(
            {"event_key": "00042"},
            element_type="NODE",
            element_class="SyntheticEvent",
        ).filter_dict == {"event_key": "00042"}


def test_incomplete_database_result_can_authorize_schema_backed_fields_only():
    backend = _TypedBackend(
        {"event_key": "INTEGER", "sampled_flag": "BOOLEAN"},
        complete=False,
        authoritative_fields=["event_key"],
    )
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=_scope(),
        schema_context=_schema(
            {"event_key": "STRING", "sampled_flag": "STRING"},
            truncated=True,
        ),
        provider="neo4j",
    )

    assert resolver.normalize_filter(
        {"event_key": "42", "sampled_flag": "false"},
        element_type="NODE",
        element_class="SyntheticEvent",
    ).filter_dict == {
        "event_key": 42,
        "sampled_flag": "false",
    }


def test_pattern_filters_use_default_aliases_but_return_fields_are_not_converted():
    backend = _TypedBackend({"event_key": "INTEGER", "display_code": "INTEGER"})
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=_scope(),
        schema_context=_schema({}, truncated=True),
        provider="neo4j",
    )
    pattern = [
        ["SyntheticEvent", {"props": {"event_key": "42"}}],
        ["SYNTHETIC_LINK", "->"],
        ["SyntheticEvent"],
    ]

    assert extract_pattern_filter_targets(pattern)[0] == (
        "NODE",
        "SyntheticEvent",
        ["event_key"],
    )
    normalized = normalize_pattern_filters(
        pattern,
        resolver,
        return_properties={"b": ["display_code"]},
    )
    assert normalized[0][1]["props"]["event_key"] == 42
    assert [call[2] for call in backend.calls] == [("event_key",)]


def test_targeted_type_lookup_does_not_claim_complete_label_inventory():
    backend = _TypedBackend({"event_key": "INTEGER"})
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=_scope(),
        provider="neo4j",
    )

    resolver.normalize_filter(
        {"event_key": "42"},
        element_type="NODE",
        element_class="SyntheticEvent",
    )
    validation_schema = resolver.validation_schema_context()

    assert validation_schema["nodes_schema"]["labels"] == ["SyntheticEvent"]
    assert validation_schema["nodes_schema"]["truncated"] is True
    assert validation_schema["nodes_schema"]["field_complete"]["SyntheticEvent"] is False


def test_normalized_container_equality_reaches_cypher_parameter_builder():
    normalized = normalize_filter_dict(
        {
            "tags": ["1", "2"],
            "metadata": {"tier": "gold"},
        },
        {"tags": "LIST<INTEGER>", "metadata": "MAP"},
    ).filter_dict
    params = {}
    conditions = build_filter_where(normalized, "e", params, "fp")

    assert any("e.tags = $" in condition for condition in conditions)
    assert any("e.metadata = $" in condition for condition in conditions)
    assert [1, 2] in params.values()
    assert {"tier": "gold"} in params.values()


@pytest.mark.parametrize(
    ("raw_type", "raw_value", "expected"),
    [
        ("DECIMAL", "12.50", Decimal("12.50")),
        ("DATE", " 2026-01-02 ", " 2026-01-02 "),
        ("LOCAL TIME", "03:04:05", "03:04:05"),
        ("BLOB", "opaque-blob-text", {"eq": "opaque-blob-text"}),
        (
            "UUID",
            "12345678-1234-5678-1234-567812345678",
            {"eq": "12345678-1234-5678-1234-567812345678"},
        ),
    ],
)
def test_additional_scalar_families(raw_type, raw_value, expected):
    actual = normalize_filter_dict(
        {"value": raw_value},
        {"value": raw_type},
        provider="hugegraph",
    ).filter_dict["value"]
    assert actual == expected
