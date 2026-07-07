"""Tests for graph scope and sanitization."""

from __future__ import annotations

import pytest

from agentic.business_data.providers.graph.scope import (
    GraphScopeError,
    build_graph_scope,
    sanitize_labels,
    sanitize_properties,
)


def test_build_graph_scope_uses_runtime_workspace_id():
    scope = build_graph_scope({
        "workspace_id": "workspace-a",
        "ontology_id": "demo-ontology",
        "workspace_property": "d2o_workspace_id",
        "ontology_property": "d2o_ontology_id",
    })

    assert scope.workspace_property == "d2o_workspace_id"
    assert scope.workspace_value == "workspace-a"
    assert scope.ontology_property == "d2o_ontology_id"
    assert scope.ontology_value == "demo-ontology"


def test_build_graph_scope_requires_workspace_id():
    with pytest.raises(GraphScopeError):
        build_graph_scope({"workspace_property": "d2o_workspace_id"})


def test_sanitize_graph_output_hides_internal_fields():
    assert sanitize_labels(["D2O_NODE", "TargetItem"]) == ["TargetItem"]
    assert sanitize_properties({
        "d2o_workspace_id": "workspace-a",
        "d2o_ontology_id": "demo-ontology",
        "_d2o_record_key": "internal",
        "logic_blocks": ["x"],
        "target_item_code": "M-001",
    }) == {"target_item_code": "M-001"}


def test_sanitize_graph_output_hides_dynamic_scope_fields():
    assert sanitize_properties(
        {
            "workspace_scope": "workspace-a",
            "ontology_scope": "demo-ontology",
            "target_item_code": "M-001",
        },
        hidden_properties=["workspace_scope", "ontology_scope"],
    ) == {"target_item_code": "M-001"}
