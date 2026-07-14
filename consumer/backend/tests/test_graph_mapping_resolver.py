"""Tests for GraphMappingResolver."""

from __future__ import annotations

import json

from agent.tools.business_data.providers.graph.mapping import GraphMappingResolver


def _write_ontology(tmp_path, payload=None):
    workspace_root = tmp_path / "ontologies"
    version_dir = workspace_root / "demo-ontology" / "versions" / "1.0.0"
    (version_dir / "mappings").mkdir(parents=True)
    (workspace_root / "demo-ontology" / "manifest.yaml").write_text(
        "\n".join([
            "kind: ontology-package",
            "schema_version: 0.1",
            "id: demo-ontology",
            "name: Demo Ontology",
            "description: Demo",
            "version: 1.0.0",
            "active_version: 1.0.0",
            "versions:",
            "  - 1.0.0",
            "language: zh-CN",
            "",
        ]),
        encoding="utf-8",
    )
    (version_dir / "mappings" / "object-data-mappings.json").write_text(
        json.dumps(payload or {"schema_version": "0.1", "objects": {}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return workspace_root


def test_resolver_reads_object_data_mappings_final_template(tmp_path):
    workspace_root = _write_ontology(
        tmp_path,
        {
            "schema_version": "0.1",
            "default_provider_alias": "domain_graph",
            "objects": {
                "eco_affected_object": {
                    "provider_alias": "domain_graph",
                    "resource": "ChangeOrderAffectedObject",
                    "primary_key": ["eco_id"],
                    "fields": {
                        "eco_id": {
                            "source": "CHANGE_ORDER_ID",
                            "type": "string",
                            "required": True,
                        },
                        "affected_object_id": {
                            "source": "AFFECTED_OBJECT_CODE",
                            "type": "string",
                        },
                        "workspace": {
                            "source": "d2o_workspace_id",
                            "type": "string",
                        },
                    }
                },
            },
        },
    )

    mapping = GraphMappingResolver(
        workspace_root=str(workspace_root),
        ontology_id="demo-ontology",
        provider="neo4j",
        schema_labels=["eco_affected_object"],
    ).resolve("eco_affected_object")

    assert mapping is not None
    assert mapping.kind == "node"
    assert mapping.label == "ChangeOrderAffectedObject"
    assert mapping.id_property == "CHANGE_ORDER_ID"
    assert mapping.property_map == {
        "eco_id": "CHANGE_ORDER_ID",
        "affected_object_id": "AFFECTED_OBJECT_CODE",
        "workspace": "d2o_workspace_id",
    }
    assert mapping.return_properties == ["CHANGE_ORDER_ID", "AFFECTED_OBJECT_CODE"]
    assert mapping.source == "mappings/object-data-mappings.json"


def test_resolver_returns_none_without_mapping(tmp_path):
    workspace_root = _write_ontology(tmp_path)

    mapping = GraphMappingResolver(
        workspace_root=str(workspace_root),
        ontology_id="demo-ontology",
        provider="neo4j",
    ).resolve("missing_entity")

    assert mapping is None

    schema_mapping = GraphMappingResolver(
        workspace_root=str(workspace_root),
        ontology_id="demo-ontology",
        provider="neo4j",
        schema_labels=["missing_entity"],
    ).resolve("missing_entity")

    assert schema_mapping is not None
    assert schema_mapping.label == "missing_entity"
    assert schema_mapping.source == "graph_schema_exact_match"


def test_resolver_reads_object_data_mappings_provider_section(tmp_path):
    workspace_root = _write_ontology(tmp_path, {"graph": {"entities": {}}})
    mapping_file = (
        workspace_root
        / "demo-ontology"
        / "versions"
        / "1.0.0"
        / "mappings"
        / "object-data-mappings.json"
    )
    mapping_file.write_text(
        json.dumps(
            {
                "objects": {
                    "target_item": {
                        "title": "TargetItem",
                        "providers": {
                            "neo4j": {
                                "resource": "TargetItem",
                                "fields": {"TargetItemCode": {"source": "target_item_code", "type": "string"}},
                            }
                        },
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    mapping = GraphMappingResolver(
        workspace_root=str(workspace_root),
        ontology_id="demo-ontology",
        provider="neo4j",
        schema_labels=["target_item"],
    ).resolve("target_item")

    assert mapping is not None
    assert mapping.kind == "node"
    assert mapping.label == "TargetItem"
    assert mapping.property_map == {"TargetItemCode": "target_item_code"}
    assert mapping.source == "mappings/object-data-mappings.json"


def test_resolver_maps_relationship_like_object_to_record_resource_with_final_template(tmp_path):
    workspace_root = _write_ontology(
        tmp_path,
        {
            "schema_version": "0.1",
            "default_provider_alias": "domain_graph",
            "objects": {
                "SourceItem与TargetItem工艺关系": {
                    "provider_alias": "domain_graph",
                    "resource": "SourceItem_TargetItem",
                    "primary_key": [],
                    "fields": {
                        "SourceItem编码": {"source": "SourceItemCode", "type": "string"},
                        "TargetItem编码": {"source": "TargetItemCode", "type": "string"},
                    },
                }
            },
        },
    )

    mapping = GraphMappingResolver(
        workspace_root=str(workspace_root),
        ontology_id="demo-ontology",
        provider="hugegraph",
    ).resolve("SourceItem与TargetItem工艺关系")

    assert mapping is not None
    assert mapping.kind == "node"
    assert mapping.label == "SourceItem_TargetItem"
    assert mapping.property_map == {"SourceItem编码": "SourceItemCode", "TargetItem编码": "TargetItemCode"}
    assert mapping.path_pattern is None
    assert mapping.return_vars == []


def test_resolver_reads_object_frontmatter_graph_label(tmp_path):
    workspace_root = _write_ontology(tmp_path, {"graph": {"entities": {}}})
    object_dir = workspace_root / "demo-ontology" / "versions" / "1.0.0" / "objects"
    object_dir.mkdir()
    (object_dir / "target_item.md").write_text(
        "\n".join(
            [
                "---",
                "title: TargetItem",
                "graph_label: TargetItem",
                "graph_properties:",
                "  TargetItemCode: target_item_code",
                "---",
                "",
                "# TargetItem",
                "",
            ]
        ),
        encoding="utf-8",
    )

    mapping = GraphMappingResolver(
        workspace_root=str(workspace_root),
        ontology_id="demo-ontology",
        provider="neo4j",
        schema_labels=["target_item"],
    ).resolve("target_item")

    assert mapping is not None
    assert mapping.kind == "node"
    assert mapping.label == "TargetItem"
    assert mapping.property_map == {"TargetItemCode": "target_item_code"}
    assert mapping.source == "objects/target_item.md"
