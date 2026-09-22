# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for Neo4J backend query safety."""

from __future__ import annotations

import pytest
from agent.tools.business_data.providers.graph.backends.base import GraphBackendTimeoutError
from agent.tools.business_data.providers.graph.backends.neo4j import Neo4jGraphBackend
from agent.tools.business_data.providers.graph.models import GraphScope
from backend_synthetic_credentials import synthetic_invalid_password
from internal_access import internal


class _FakeNode:
    def __init__(self, *, element_id, labels, properties):
        self.element_id = element_id
        self.labels = labels
        self._properties = properties


class _FakeRelationship:
    def __init__(self, *, element_id, rel_type, properties, start_node=None, end_node=None):
        self.element_id = element_id
        self.type = rel_type
        self._properties = properties
        self.start_node = start_node
        self.end_node = end_node


class _FakePath:
    def __init__(self, nodes, relationships):
        self.nodes = nodes
        self.relationships = relationships


class _FakeSession:
    def __init__(self, calls):
        self.calls = calls

    def __enter__(self):
        return self

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def __exit__(self, exc_type, exc, tb):
        return False

    def run(self, query, params):
        self.calls.append((query, params))
        query_text = str(query)
        if "UNWIND labels(n)" in query_text:
            return [
                {"label": "D2O_NODE", "prop_types": [{"name": "id", "type": "STRING"}]},
                {"label": "ChangeOrderAffectedObject", "prop_types": [{"name": "change_order_id", "type": "STRING"}]},
                {"label": "TargetItem", "prop_types": [{"name": "target_item_code", "type": "STRING"}]},
            ]
        if "WITH r, type(r) AS rel_type" in query_text:
            return [
                {"rel_type": "D2O_RELATION", "prop_types": [{"name": "relation_type", "type": "STRING"}]},
            ]
        if "source_entity_type_id" in query_text:
            return [
                {
                    "source_entity_type_id": "e1",
                    "source_entity_name": "CHANGE_ORDER",
                    "source_labels": ["D2O_NODE", "CHANGE_ORDER"],
                    "relation_type_id": "r1",
                    "relation_type": "CHANGE_ORDER_AFFECTS_BEFORE",
                    "relation_name": "变更前",
                    "target_entity_type_id": "e2",
                    "target_entity_name": "SourceItemVersion",
                    "target_labels": ["D2O_NODE", "SourceItemVersion"],
                    "edge_count": 2,
                }
            ]
        if "MATCH p =" in query_text:
            return [
                {
                    "nodes": [
                        _FakeNode(
                            element_id="node-change_order",
                            labels=["D2O_NODE", "CHANGE_ORDER"],
                            properties={
                                "CHANGE_ORDER_ID": "CHANGE_ORDER-001",
                                "Status": "待决策",
                                "d2o_workspace_id": "workspace-a",
                            },
                        ),
                        _FakeNode(
                            element_id="node-version",
                            labels=["D2O_NODE", "SourceItemVersion"],
                            properties={
                                "SourceItemVersionID": "VERSION0003",
                                "SourceItemCode": "101000-001",
                                "Status": "激活",
                            },
                        ),
                    ],
                    "relationships": [
                        _FakeRelationship(
                            element_id="rel-1",
                            rel_type="D2O_RELATION",
                            properties={
                                "relation_type": "CHANGE_ORDER_AFFECTS_BEFORE",
                                "source_table": "CHANGE_ORDER_affected_obj",
                            },
                        )
                    ],
                }
            ]
        if "MATCH path = (start)-" in query_text and "RETURN nodes(path) AS nodes" in query_text:
            start = _FakeNode(
                element_id="node-start",
                labels=["D2O_NODE", "SourceItemVersion"],
                properties={"SourceItemVersionID": "VERSION0001", "d2o_workspace_id": "workspace-a"},
            )
            end = _FakeNode(
                element_id="node-end",
                labels=["D2O_NODE", "TargetItem"],
                properties={"TargetItemCode": "M-001", "d2o_workspace_id": "workspace-a"},
            )
            rel = _FakeRelationship(
                element_id="rel-hop",
                rel_type="D2O_RELATION",
                properties={"relation_type": "PROCESS_TO", "d2o_workspace_id": "workspace-a"},
                start_node=start,
                end_node=end,
            )
            return [{"nodes": [start, end], "relationships": [rel]}]
        if "shortestPath" in query_text:
            start = _FakeNode(
                element_id="node-start",
                labels=["D2O_NODE", "SourceItemVersion"],
                properties={"SourceItemVersionID": "VERSION0001", "d2o_workspace_id": "workspace-a"},
            )
            end = _FakeNode(
                element_id="node-end",
                labels=["D2O_NODE", "TargetItem"],
                properties={"TargetItemCode": "M-001", "d2o_workspace_id": "workspace-a"},
            )
            rel = _FakeRelationship(
                element_id="rel-shortest",
                rel_type="D2O_RELATION",
                properties={"relation_type": "PROCESS_TO", "d2o_workspace_id": "workspace-a"},
                start_node=start,
                end_node=end,
            )
            return [{"path": _FakePath([start, end], [rel])}]
        if "logic_blocks" in query_text and "entity_instance_id" in query_text:
            return [
                {
                    "entity_instance_id": "node-logic",
                    "labels": ["D2O_NODE", "Product"],
                    "entity_type": "Product",
                    "logic_blocks": '[{"title":"推荐规则","content":"命中后推荐"}]',
                }
            ]
        if "RETURN n LIMIT 1" in query_text:
            return [
                {
                    "n": _FakeNode(
                        element_id="node-1",
                        labels=["D2O_NODE", "TargetItem"],
                        properties={"target_item_code": "M-001", "d2o_workspace_id": "workspace-a"},
                    )
                }
            ]
        if "RETURN r, a, b LIMIT 1" in query_text:
            return []
        if "RETURN count(e) AS total" in query_text:
            return [{"total": 1}]
        return [
            {
                "e": _FakeNode(
                    element_id="node-1",
                    labels=["D2O_NODE", "ChangeOrderAffectedObject"],
                    properties={
                        "change_order_id": "CHANGE_ORDER-001",
                        "d2o_workspace_id": "workspace-a",
                        "d2o_ontology_id": "demo-ontology",
                        "tenant_ontology": "demo-ontology",
                        "_d2o_record_key": synthetic_invalid_password("neo4j_graph_backend-credential-01"),
                    },
                )
            }
        ]


class _FakeDriver:
    def __init__(self, calls):
        self.calls = calls

    def session(self, database=None):
        self.calls.append(("database", database))
        return _FakeSession(self.calls)


class _TimeoutSession(_FakeSession):
    def run(self, query, params):
        self.calls.append((query, params))
        raise TimeoutError("query timed out")


class _TimeoutDriver(_FakeDriver):
    def session(self, database=None):
        self.calls.append(("database", database))
        return _TimeoutSession(self.calls)


def _backend():
    return Neo4jGraphBackend(
        {
            "uri": "bolt://neo4j.example.net:7687",
            "database": "domain",
            "username": "neo4j",
            "password": synthetic_invalid_password("neo4j_graph_backend-credential-01"),
        }
    )


def _scope():
    return GraphScope(
        workspace_property="d2o_workspace_id",
        workspace_value="workspace-a",
        ontology_property="tenant_ontology",
        ontology_value="demo-ontology",
    )


def _workspace_only_scope():
    return GraphScope(
        workspace_property="d2o_workspace_id",
        workspace_value="workspace-a",
        ontology_property="",
        ontology_value="",
    )


def test_neo4j_property_filter_forces_workspace_scope(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    result = backend.property_filter(
        element_class="ChangeOrderAffectedObject",
        element_type="NODE",
        filter_dict={"change_order_id": "CHANGE_ORDER-001"},
        return_properties=["change_order_id"],
        limit=10,
        offset=0,
        scope=_scope(),
    )

    query, params = calls[1]
    query_text = str(query)
    assert "MATCH (e)" in query_text
    assert "$elem_class IN labels(e)" in query_text
    assert "e.`d2o_workspace_id` = $scope_workspace_value" in query_text
    assert "e.`tenant_ontology` = $scope_ontology_value" in query_text
    assert "e.change_order_id = $fp_change_order_id_0" in query_text
    assert getattr(query, "timeout", None) == 30
    assert params["elem_class"] == "ChangeOrderAffectedObject"
    assert params["scope_workspace_value"] == "workspace-a"
    assert params["scope_ontology_value"] == "demo-ontology"
    assert params["fp_change_order_id_0"] == "CHANGE_ORDER-001"
    assert result.records == [
        {
            "element_id": "node-1",
            "entity_instance_id": "node-1",
            "entity_type_id": "",
            "labels": ["ChangeOrderAffectedObject"],
            "properties": {"change_order_id": "CHANGE_ORDER-001"},
        }
    ]


def test_neo4j_property_filter_supports_workspace_only_scope(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    backend.property_filter(
        element_class="ChangeOrderAffectedObject",
        element_type="NODE",
        filter_dict={"change_order_id": "CHANGE_ORDER-001"},
        return_properties=["change_order_id"],
        limit=10,
        offset=0,
        scope=_workspace_only_scope(),
    )

    query, params = calls[1]
    assert "e.`d2o_workspace_id` = $scope_workspace_value" in str(query)
    assert "scope_ontology_value" not in str(query)
    assert "scope_ontology_value" not in params


def test_neo4j_schema_methods_return_sanitized_metadata(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    nodes_schema = backend.get_nodes_schema(scope=_scope())
    edges_schema = backend.get_edges_schema(scope=_scope())

    assert nodes_schema["labels"] == ["ChangeOrderAffectedObject", "TargetItem"]
    assert nodes_schema["schema"]["TargetItem"] == {"target_item_code": "STRING"}
    assert edges_schema["relationship_types"] == ["CHANGE_ORDER_AFFECTS_BEFORE", "D2O_RELATION"]
    assert edges_schema["relation_triples"][0]["source_label"] == "CHANGE_ORDER"
    assert "UNWIND labels(n)" in str(calls[1][0])
    assert "WITH r, type(r) AS rel_type" in str(calls[3][0])


def test_neo4j_get_property_types_uses_scoped_value_type(monkeypatch):
    calls = []
    backend = _backend()

    class _TypeSession(_FakeSession):
        def run(self, query, params):
            self.calls.append((query, params))
            query_text = str(query)
            if "physical_relation_types" in query_text:
                return [{"physical_relation_types": ["D2O_RELATION"]}]
            return [
                {"prop": "event_key", "types": ["INTEGER"]},
                {"prop": "enabled", "types": ["BOOLEAN"]},
            ]

    class _TypeDriver(_FakeDriver):
        def session(self, database=None):
            self.calls.append(("database", database))
            return _TypeSession(self.calls)

    monkeypatch.setattr(backend, "_get_driver", lambda: _TypeDriver(calls))

    result = backend.get_property_types(
        element_class="SyntheticEvent",
        element_type="NODE",
        property_names=["event_key", "enabled"],
        scope=_scope(),
    )

    assert result == {
        "property_types": {"event_key": "INTEGER", "enabled": "BOOLEAN"},
        "complete": True,
        "source": "neo4j_valueType",
    }
    query, params = calls[1]
    assert "valueType(prop_value)" in str(query)
    assert params["property_names"] == ["event_key", "enabled"]


def test_neo4j_get_element_info_forces_scope_and_sanitizes_node(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    result = backend.get_element_info(
        element_instance_id="node-1",
        scope=_scope(),
    )

    query, params = calls[1]
    query_text = str(query)
    assert "elementId(n) = $element_id" in query_text
    assert "n.`d2o_workspace_id` = $scope_workspace_value" in query_text
    assert params["element_id"] == "node-1"
    assert params["scope_workspace_value"] == "workspace-a"
    assert result == {
        "element_id": "node-1",
        "entity_instance_id": "node-1",
        "entity_type_id": "",
        "labels": ["TargetItem"],
        "properties": {"target_item_code": "M-001"},
        "found": True,
        "kind": "node",
        "element_type": "NODE",
    }


def test_neo4j_pattern_search_supports_mapping_dict_segments(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    result = backend.pattern_search(
        path_pattern=[
            {
                "var": "change_order",
                "label": "CHANGE_ORDER",
                "filters": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"},
                "return_properties": ["CHANGE_ORDER_ID"],
            },
            {"var": "rel", "type": "D2O_RELATION", "direction": "->", "return_properties": ["relation_type"]},
            {"var": "version", "label": "SourceItemVersion", "return_properties": ["SourceItemVersionID"]},
        ],
        return_vars=["change_order", "rel", "version"],
        limit=100,
        scope=_scope(),
    )

    query, params = calls[1]
    query_text = str(query)
    assert "MATCH p = (change_order:`CHANGE_ORDER`)-[rel:`D2O_RELATION`]->(version:`SourceItemVersion`)" in query_text
    assert "change_order.CHANGE_ORDER_ID = $pathprop0_change_order_CHANGE_ORDER_ID_0" in query_text
    assert params["pathprop0_change_order_CHANGE_ORDER_ID_0"] == "CHANGE_ORDER-001"
    assert result.records[0]["nodes"][0]["properties"] == {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}
    assert result.records[0]["relationships"][0]["properties"] == {"relation_type": "CHANGE_ORDER_AFFECTS_BEFORE"}


def test_neo4j_pattern_search_supports_jiutian_array_segments(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    result = backend.pattern_search(
        path_pattern=[
            [{"label": "CHANGE_ORDER", "filters": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}}],
            [{"type": "D2O_RELATION", "relation_type": "CHANGE_ORDER_AFFECTS_BEFORE"}, "->"],
            [{"label": "SourceItemVersion"}],
        ],
        return_vars=["a", "r1", "b"],
        limit=100,
        scope=_scope(),
    )

    query, params = calls[1]
    query_text = str(query)
    assert "MATCH p = (a:`CHANGE_ORDER`)-[r1:`D2O_RELATION`]->(b:`SourceItemVersion`)" in query_text
    assert "a.CHANGE_ORDER_ID = $pathprop0_a_CHANGE_ORDER_ID_0" in query_text
    assert params["pathprop0_a_CHANGE_ORDER_ID_0"] == "CHANGE_ORDER-001"
    assert "r1.relation_type = $pathrel1_r1_relation_type_0" in query_text
    assert result.records[0]["return_vars"] == ["a", "r1", "b"]


def test_neo4j_pattern_search_accepts_edge_props_without_explicit_direction(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    backend.pattern_search(
        path_pattern=[
            ["CHANGE_ORDER"],
            ["D2O_RELATION", {"props": {"relation_type": "CHANGE_ORDER_AFFECTS_BEFORE"}}],
            ["SourceItemVersion"],
        ],
        return_vars=["a", "r1", "b"],
        limit=10,
        scope=_scope(),
    )

    query, params = calls[1]
    assert "-[r1]->" in str(query)
    assert params["pathrel1_r1_relation_type_0"] == "CHANGE_ORDER_AFFECTS_BEFORE"


def test_neo4j_pattern_search_projects_return_vars_and_properties(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    result = backend.pattern_search(
        path_pattern=[
            {"var": "change_order", "label": "CHANGE_ORDER", "filters": {"CHANGE_ORDER_ID": "CHANGE_ORDER-001"}},
            {"var": "rel", "type": "D2O_RELATION", "direction": "->"},
            {"var": "version", "label": "SourceItemVersion"},
        ],
        return_vars=["version"],
        return_properties={"version": ["SourceItemVersionID"]},
        limit=100,
        scope=_scope(),
    )

    record = result.records[0]
    assert record["return_vars"] == ["version"]
    assert record["relationships"] == []
    assert len(record["nodes"]) == 1
    assert record["nodes"][0]["labels"] == ["SourceItemVersion"]
    assert record["nodes"][0]["properties"] == {"SourceItemVersionID": "VERSION0003"}
    assert record["selected"] == {"version": record["nodes"][0]}


def test_neo4j_backend_runs_extra_mcp_graph_tools(monkeypatch):
    calls = []
    backend = _backend()
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    hop = backend.hop_search(
        entity_instance_id="node-start",
        hop_num=1,
        accurate_flag=False,
        scope=_scope(),
    )
    shortest = backend.shortest_path_search(
        start_label="SourceItemVersion",
        start_props={"SourceItemVersionID": "VERSION0001"},
        end_label="TargetItem",
        end_props={"TargetItemCode": "M-001"},
        max_depth=3,
        directed=True,
        scope=_scope(),
    )
    titles = backend.list_logic_titles(scope=_scope())
    content = backend.get_logic_content(
        entity_instance_ids=["node-logic"],
        logic_titles=["推荐规则"],
        scope=_scope(),
    )

    query_texts = [str(call[0]) for call in calls if call[0] != "database"]
    assert any("MATCH path = (start)-[*1..1]-(n)" in text for text in query_texts)
    assert any("shortestPath((start)-[*1..3]->(end))" in text for text in query_texts)
    assert any("ALL(node IN nodes(path) WHERE" in text for text in query_texts)
    assert hop.records[0]["node_count"] == 2
    assert hop.records[0]["relationships"][0]["relation_physical_type"] == "D2O_RELATION"
    assert shortest.records[0]["relationships"][0]["relation_type"] == "PROCESS_TO"
    assert titles.records == [{"entity_instance_id": "node-logic", "entity_type": "Product", "logic_title": "推荐规则"}]
    assert content.records == [
        {
            "entity_instance_id": "node-logic",
            "entity_type": "Product",
            "logic_title": "推荐规则",
            "logic_content": "命中后推荐",
        }
    ]


def test_neo4j_timeout_maps_to_backend_timeout_error(monkeypatch):
    calls = []
    backend = Neo4jGraphBackend(
        {
            "uri": "bolt://neo4j.example.net:7687",
            "database": "domain",
            "username": "neo4j",
            "password": synthetic_invalid_password("neo4j_graph_backend-credential-01"),
            "timeout_seconds": 120,
            "graph_query_timeout_seconds": 30,
        }
    )
    monkeypatch.setattr(backend, "_get_driver", lambda: _TimeoutDriver(calls))

    with pytest.raises(GraphBackendTimeoutError) as exc_info:
        backend.property_filter(
            element_class="ChangeOrderAffectedObject",
            element_type="NODE",
            filter_dict={},
            return_properties=[],
            limit=10,
            offset=0,
            scope=_scope(),
        )

    query, _params = calls[1]
    assert getattr(query, "timeout", None) == 30
    assert exc_info.value.error_code == "GRAPH_BACKEND_TIMEOUT"


def test_neo4j_legacy_timeout_does_not_override_default_query_timeout(monkeypatch):
    calls = []
    backend = Neo4jGraphBackend(
        {
            "uri": "bolt://neo4j.example.net:7687",
            "database": "domain",
            "username": "neo4j",
            "password": synthetic_invalid_password("neo4j_graph_backend-credential-01"),
            "timeout_seconds": 60,
        }
    )
    monkeypatch.setattr(backend, "_get_driver", lambda: _FakeDriver(calls))

    backend.property_filter(
        element_class="ChangeOrderAffectedObject",
        element_type="NODE",
        filter_dict={},
        return_properties=[],
        limit=10,
        offset=0,
        scope=_scope(),
    )

    assert getattr(calls[1][0], "timeout", None) == 30


def test_neo4j_uses_separate_connection_and_query_timeouts(monkeypatch):
    calls = []

    def fake_driver(uri, auth=None, connection_timeout=None):
        calls.append(("driver", uri, auth, connection_timeout))
        return _FakeDriver(calls)

    monkeypatch.setattr("neo4j.GraphDatabase.driver", fake_driver)
    backend = Neo4jGraphBackend(
        {
            "uri": "bolt://neo4j.example.net:7687",
            "database": "domain",
            "username": "neo4j",
            "password": synthetic_invalid_password("neo4j_graph_backend-credential-01"),
            "timeout_seconds": 120,
            "graph_query_timeout_seconds": 30,
            "graph_connect_timeout_seconds": 10,
        }
    )

    backend.property_filter(
        element_class="ChangeOrderAffectedObject",
        element_type="NODE",
        filter_dict={},
        return_properties=[],
        limit=10,
        offset=0,
        scope=_scope(),
    )

    assert calls[0] == (
        "driver",
        "bolt://neo4j.example.net:7687",
        ("neo4j", synthetic_invalid_password("neo4j_graph_backend-credential-01")),
        10,
    )
    assert getattr(calls[2][0], "timeout", None) == 30


def test_neo4j_resolver_uses_pinned_ip_and_keeps_original_tls_uri(monkeypatch):
    captured = {}

    def fake_driver(uri, **kwargs):
        captured["uri"] = uri
        captured.update(kwargs)
        return _FakeDriver([])

    monkeypatch.setattr("neo4j.GraphDatabase.driver", fake_driver)
    backend = Neo4jGraphBackend(
        {
            "uri": "neo4j+s://snapshot.example.com:7687",
            "resolved_uri": "neo4j+s://192.0.2.24:7687",
            "username": "operator_a",
            "password": synthetic_invalid_password("neo4j_graph_backend-credential-02"),
        }
    )

    internal(backend).get_driver()

    assert captured["uri"] == "neo4j+s://snapshot.example.com:7687"
    resolved = captured["resolver"](("snapshot.example.com", 7687))
    assert resolved == [("192.0.2.24", 7687)]
    assert internal(resolved[0]).host_name == "snapshot.example.com"
