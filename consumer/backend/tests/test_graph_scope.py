"""Tests for graph scope and sanitization."""

from __future__ import annotations

import pytest
from agent.tools.business_data.providers.graph.models import GraphScope
from agent.tools.business_data.providers.graph.scope import (
    GraphScopeError,
    build_graph_scope,
    sanitize_labels,
    sanitize_properties,
    validate_graph_identifier,
)


def test_build_graph_scope_uses_runtime_workspace_id():
    scope = build_graph_scope(
        {
            "workspace_id": "workspace-a",
            "ontology_id": "demo-ontology",
            "workspace_property": "d2o_workspace_id",
        }
    )

    assert scope.workspace_property == "d2o_workspace_id"
    assert scope.workspace_value == "workspace-a"
    assert scope.ontology_property == ""
    assert scope.ontology_value == ""


def test_build_graph_scope_prefers_explicit_graph_values_over_runtime_ids():
    scope = build_graph_scope(
        {
            "workspace_id": "workspace-solver-demo",
            "workspace_value": "workspace_demo",
            "ontology_id": "runtime-package-id",
            "ontology_value": "demo-refinery-ontology",
            "workspace_property": "d2o_workspace_id",
            "ontology_property": "d2o_ontology_id",
        }
    )

    assert scope.workspace_value == "workspace_demo"
    assert scope.ontology_value == "demo-refinery-ontology"


def test_build_graph_scope_uses_workspace_name_for_workspace_name_property():
    scope = build_graph_scope(
        {
            "workspace_id": "workspace-a",
            "ontology_id": "demo-ontology",
            "workspace_name": "示例业务空间",
            "workspace_property": "workspace_name",
        }
    )

    assert scope.workspace_property == "workspace_name"
    assert scope.workspace_value == "示例业务空间"
    assert scope.ontology_property == ""
    assert scope.ontology_value == ""


def test_build_graph_scope_requires_runtime_workspace_name_for_name_property():
    with pytest.raises(GraphScopeError, match="workspace_name"):
        build_graph_scope(
            {
                "workspace_id": "workspace-a",
                "ontology_id": "demo-ontology",
                "workspace_property": "workspace_name",
            }
        )


def test_build_graph_scope_requires_ontology_id_when_enabled():
    with pytest.raises(GraphScopeError, match="ontology_id"):
        build_graph_scope(
            {
                "workspace_id": "workspace-a",
                "ontology_property": "d2o_ontology_id",
            }
        )


def test_build_graph_scope_disables_ontology_scope_for_explicit_blank_property():
    scope = build_graph_scope(
        {
            "workspace_id": "workspace-a",
            "ontology_id": "demo-ontology",
            "ontology_property": "",
        }
    )

    assert scope.workspace_property == "d2o_workspace_id"
    assert scope.workspace_value == "workspace-a"
    assert scope.ontology_property == ""
    assert scope.ontology_value == ""
    assert scope.is_enabled() is True

    direct_scope = GraphScope(
        workspace_property="d2o_workspace_id",
        workspace_value="workspace-a",
        ontology_property="",
        ontology_value="",
    )
    assert direct_scope.is_enabled() is True


def test_graph_scope_rejects_partial_ontology_scope():
    with pytest.raises(ValueError, match="configured together"):
        GraphScope(
            workspace_property="d2o_workspace_id",
            workspace_value="workspace-a",
            ontology_property="d2o_ontology_id",
            ontology_value="",
        )


def test_build_graph_scope_requires_workspace_id():
    with pytest.raises(GraphScopeError, match="workspace_id"):
        build_graph_scope({"ontology_id": "demo-ontology"})


def test_validate_graph_identifier_allows_chinese_but_rejects_unsafe_names():
    assert validate_graph_identifier("制造物料") == "制造物料"
    assert validate_graph_identifier("TargetItem_制造物料1") == "TargetItem_制造物料1"

    for value in ("1制造物料", "制造 物料", "制造-物料", "制造`物料"):
        with pytest.raises(ValueError):
            validate_graph_identifier(value)


def test_sanitize_graph_output_hides_internal_fields():
    assert sanitize_labels(["D2O_NODE", "TargetItem"]) == ["TargetItem"]
    assert sanitize_properties(
        {
            "d2o_workspace_id": "workspace-a",
            "d2o_ontology_id": "demo-ontology",
            "_d2o_record_key": "internal",
            "logic_blocks": ["x"],
            "target_item_code": "M-001",
        }
    ) == {"target_item_code": "M-001"}


def test_sanitize_graph_output_hides_dynamic_scope_fields():
    assert sanitize_properties(
        {
            "workspace_scope": "workspace-a",
            "ontology_scope": "demo-ontology",
            "target_item_code": "M-001",
        },
        hidden_properties=["workspace_scope", "ontology_scope"],
    ) == {"target_item_code": "M-001"}
