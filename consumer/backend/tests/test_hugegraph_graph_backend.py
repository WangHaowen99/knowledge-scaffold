# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for HugeGraph graph backend behavior."""

from __future__ import annotations

import json

import pytest
import requests
from agent.tools.business_data.providers.graph.backends.base import GraphBackendTimeoutError
from agent.tools.business_data.providers.graph.backends.hugegraph import HugeGraphBackend
from agent.tools.business_data.providers.graph.models import GraphScope
from backend_synthetic_credentials import synthetic_invalid_password, synthetic_secret
from internal_access import internal


def _scope():
    return GraphScope(
        workspace_property="d2o_workspace_id",
        workspace_value="workspace-a",
        ontology_property="d2o_ontology_id",
        ontology_value="onto-a",
    )


def _workspace_only_scope():
    return GraphScope(
        workspace_property="d2o_workspace_id",
        workspace_value="workspace-a",
        ontology_property="",
        ontology_value="",
    )


def _vertex(vertex_id, entity_name, properties, *, workspace="workspace-a", ontology="onto-a"):
    merged = {
        "entity_name": entity_name,
        "entity_id": "e-{}".format(entity_name),
        **properties,
    }
    return {
        "id": vertex_id,
        "label": "D2O_ENTITY",
        "properties": {
            "d2o_workspace_id": workspace,
            "d2o_ontology_id": ontology,
            "properties_json": json.dumps(merged, ensure_ascii=False),
        },
    }


def _edge(edge_id, source, target, relation_type, properties=None, *, workspace="workspace-a", ontology="onto-a"):
    merged = {
        "relation_id": "r-{}".format(relation_type),
        "relation_type": relation_type,
        "relation_name": relation_type,
        **dict(properties or {}),
    }
    return {
        "id": edge_id,
        "label": "D2O_RELATION",
        "outV": source,
        "inV": target,
        "properties": {
            "d2o_workspace_id": workspace,
            "d2o_ontology_id": ontology,
            "properties_json": json.dumps(merged, ensure_ascii=False),
        },
    }


class _FakeResponse:
    status_code = 200
    text = "{}"

    def __init__(self, payload):
        self._payload = payload

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _install_hugegraph_http(
    monkeypatch,
    *,
    include_competing_path=False,
    include_other_ontology=False,
):
    vertices = [
        _vertex(
            "v-m1",
            "TargetItem",
            {
                "TargetItemCode": "M-001",
                "MaterialCost": 450,
                "logic_blocks": json.dumps([{"title": "推荐规则", "content": "命中后推荐"}], ensure_ascii=False),
            },
        ),
        _vertex(
            "v-e1",
            "SourceItemVersion",
            {"SourceItemVersionID": "VERSION0001", "SourceItemCode": "101000-001", "SourceItemVersion": "A"},
        ),
        _vertex("v-other", "TargetItem", {"TargetItemCode": "M-999"}, workspace="workspace-b"),
    ]
    if include_other_ontology:
        vertices.append(
            _vertex(
                "v-other-ontology",
                "TargetItem",
                {"TargetItemCode": "M-002"},
                ontology="onto-b",
            ),
        )
    edges = [
        _edge("e-rel-1", "v-e1", "v-m1", "PROCESS_TO", {"Qty": 2}),
        _edge("e-other", "v-other", "v-m1", "PROCESS_TO", workspace="workspace-b"),
    ]
    if include_competing_path:
        vertices.extend(
            [
                _vertex("v-m2", "TargetItem", {"TargetItemCode": "M-002", "MaterialCost": 300}),
                _vertex(
                    "v-e2",
                    "SourceItemVersion",
                    {"SourceItemVersionID": "VERSION0002", "SourceItemCode": "101001-001", "SourceItemVersion": "A"},
                ),
            ]
        )
        edges.append(_edge("e-rel-2", "v-e2", "v-m2", "PROCESS_TO", {"Qty": 1}))
    calls = []

    def fake_request(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
        method,
        url,
        params=None,
        timeout=None,
        headers=None,
        auth=None,
        **_request_kwargs,
    ):
        calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "timeout": timeout,
                "headers": headers,
                "auth": auth,
            }
        )
        if url.endswith("/schema"):
            return _FakeResponse({"schema": {}})
        if url.endswith("/graph/vertices"):
            return _FakeResponse({"vertices": vertices})
        if url.endswith("/graph/edges"):
            return _FakeResponse({"edges": edges})
        return _FakeResponse({})

    monkeypatch.setattr("requests.request", fake_request)
    return calls


def test_hugegraph_property_filter_can_disable_ontology_scope(monkeypatch):
    _install_hugegraph_http(monkeypatch, include_other_ontology=True)
    backend = HugeGraphBackend(
        {
            "url": "https://service.example.net",
            "graph": "demo",
        }
    )
    scope = GraphScope(
        workspace_property="d2o_workspace_id",
        workspace_value="workspace-a",
        ontology_property="",
        ontology_value="",
    )

    result = backend.property_filter(
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={},
        return_properties=["TargetItemCode"],
        limit=10,
        offset=0,
        scope=scope,
    )

    assert [record["properties"]["TargetItemCode"] for record in result.records] == ["M-001", "M-002"]


def test_hugegraph_backend_executes_schema_property_count_aggregate_sorted_and_detail(monkeypatch):
    calls = _install_hugegraph_http(monkeypatch)
    backend = HugeGraphBackend(
        {
            "url": "https://service.example.net",
            "graph": "demo",
            "graph_space": "DEFAULT",
            "username": "user",
            "password": synthetic_invalid_password("hugegraph_graph_backend-credential-02"),
            "token": synthetic_secret("hugegraph_graph_backend-credential-03"),
        }
    )

    backend.verify_connectivity()
    nodes_schema = backend.get_nodes_schema(scope=_scope())
    edges_schema = backend.get_edges_schema(scope=_scope())
    filtered = backend.property_filter(
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={"TargetItemCode": "M-001"},
        return_properties=["TargetItemCode", "MaterialCost"],
        limit=10,
        offset=0,
        scope=_scope(),
    )
    filtered_alias = backend.property_filter(
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={"TargetItemCode": {"$contains": "M-00"}},
        return_properties=["TargetItemCode"],
        limit=10,
        offset=0,
        scope=_scope(),
    )
    counted = backend.count_search(
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={"TargetItemCode": "M-001"},
        scope=_scope(),
    )
    aggregated = backend.aggregate_search(
        element_class="TargetItem",
        element_type="NODE",
        target_property="MaterialCost",
        agg="SUM",
        filter_dict={},
        scope=_scope(),
    )
    sorted_result = backend.sorted_search(
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={},
        return_properties=["TargetItemCode"],
        sort_by=["TargetItemCode"],
        ascending=True,
        limit=10,
        offset=0,
        scope=_scope(),
    )
    detail = backend.get_element_info(
        element_instance_id="v-m1",
        element_class="TargetItem",
        element_type="NODE",
        scope=_scope(),
    )

    assert "TargetItem" in nodes_schema["labels"]
    assert nodes_schema["schema"]["TargetItem"]["TargetItemCode"] == "STRING"
    assert edges_schema["relation_triples"][0]["relation_type"] == "PROCESS_TO"
    assert filtered.records == [
        {
            "element_id": "v-m1",
            "entity_instance_id": "v-m1",
            "entity_type_id": "e-TargetItem",
            "labels": ["TargetItem"],
            "properties": {"TargetItemCode": "M-001", "MaterialCost": 450},
        }
    ]
    assert filtered_alias.records[0]["properties"] == {"TargetItemCode": "M-001"}
    assert counted.total == 1
    assert aggregated.records[0]["value"] == 450.0
    assert sorted_result.total == 1
    assert sorted_result.records[0]["properties"] == {"TargetItemCode": "M-001"}
    assert detail["found"] is True
    assert detail["properties"]["TargetItemCode"] == "M-001"
    assert calls[0]["url"] == "https://service.example.net/graphspaces/DEFAULT/graphs/demo/schema"
    assert calls[0]["headers"] == {
        "Authorization": f"Bearer {synthetic_secret('hugegraph_graph_backend-credential-03')}"
    }
    assert calls[0]["auth"] == ("user", synthetic_invalid_password("hugegraph_graph_backend-credential-02"))


def test_hugegraph_property_filter_supports_workspace_only_scope(monkeypatch):
    _install_hugegraph_http(monkeypatch)
    backend = HugeGraphBackend({"url": "https://hugegraph.example.net", "graph": "demo"})

    result = backend.property_filter(
        element_class="TargetItem",
        element_type="NODE",
        filter_dict={"TargetItemCode": "M-001"},
        return_properties=["TargetItemCode"],
        limit=10,
        offset=0,
        scope=_workspace_only_scope(),
    )

    assert result.total == 1
    assert result.records[0]["properties"] == {"TargetItemCode": "M-001"}


def test_hugegraph_uses_separate_connection_and_query_timeouts(monkeypatch):
    calls = _install_hugegraph_http(monkeypatch)
    backend = HugeGraphBackend(
        {
            "url": "https://service.example.net",
            "graph": "demo",
            "timeout_seconds": 120,
            "graph_query_timeout_seconds": 30,
            "graph_connect_timeout_seconds": 10,
        }
    )

    backend.verify_connectivity()

    assert calls[0]["timeout"] == (10, 30)


def test_hugegraph_uses_pinned_ip_with_original_tls_identity(monkeypatch):
    captured = {}

    class Response:
        text = "{}"
        status_code = 200

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def raise_for_status(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def json(self):
            return {}

    backend = HugeGraphBackend(
        {
            "url": "https://snapshot.example.com:8443",
            "resolved_url": "https://192.0.2.24:8443",
            "graph": "graph_demo",
        }
    )
    monkeypatch.setattr(
        internal(backend).pinned_session,
        "request",
        lambda **kwargs: captured.update(kwargs) or Response(),
    )

    backend.verify_connectivity()

    assert captured["url"].startswith("https://192.0.2.24:8443/graphs/graph_demo/")
    assert captured["headers"]["Host"] == "snapshot.example.com:8443"
    assert captured["allow_redirects"] is False
    assert internal(backend).pinned_session.trust_env is False
    assert internal(internal(backend).pinned_session.adapters["https://"]).server_hostname == "snapshot.example.com"
    backend.close()


def test_hugegraph_legacy_timeout_does_not_override_default_query_timeout(monkeypatch):
    calls = _install_hugegraph_http(monkeypatch)
    backend = HugeGraphBackend(
        {
            "url": "https://service.example.net",
            "graph": "demo",
            "timeout_seconds": 60,
        }
    )

    backend.verify_connectivity()

    assert calls[0]["timeout"] == (10, 30)


def test_hugegraph_timeout_is_retried_at_most_once(monkeypatch):
    calls = []

    def timeout_request(*args, **kwargs):
        calls.append({"args": args, "timeout": kwargs.get("timeout")})
        raise requests.exceptions.Timeout("read timed out")

    monkeypatch.setattr("requests.request", timeout_request)
    backend = HugeGraphBackend(
        {
            "url": "https://service.example.net",
            "graph": "demo",
            "max_retries": 5,
        }
    )

    with pytest.raises(GraphBackendTimeoutError):
        backend.verify_connectivity()

    assert len(calls) == 2
    assert all(call["timeout"] == (10, 30) for call in calls)


def test_hugegraph_backend_pattern_search_forces_scope(monkeypatch):
    _install_hugegraph_http(monkeypatch)
    backend = HugeGraphBackend({"url": "https://hugegraph.example.net", "graph": "demo"})

    result = backend.pattern_search(
        path_pattern=[
            ["SourceItemVersion", {"SourceItemVersionID": "VERSION0001"}],
            ["PROCESS_TO", "->"],
            ["TargetItem"],
        ],
        return_vars=["a", "r1", "b"],
        limit=10,
        scope=_scope(),
    )

    assert result.total == 1
    assert result.records[0]["nodes"][0]["labels"] == ["SourceItemVersion"]
    assert result.records[0]["nodes"][1]["labels"] == ["TargetItem"]
    assert result.records[0]["relationships"][0]["relation_type"] == "PROCESS_TO"
    assert result.records[0]["selected"]["b"]["properties"]["TargetItemCode"] == "M-001"


def test_hugegraph_backend_pattern_search_treats_filters_as_props(monkeypatch):
    _install_hugegraph_http(monkeypatch, include_competing_path=True)
    backend = HugeGraphBackend({"url": "https://hugegraph.example.net", "graph": "demo"})

    result = backend.pattern_search(
        path_pattern=[
            {
                "var": "source_item_version",
                "label": "SourceItemVersion",
                "filters": {"SourceItemCode": "101000-001", "SourceItemVersion": "A"},
            },
            {"var": "rel", "type": "D2O_RELATION", "relation_type": "PROCESS_TO", "direction": "->"},
            {"var": "target_item", "label": "TargetItem"},
        ],
        return_vars=["source_item_version", "rel", "target_item"],
        limit=10,
        scope=_scope(),
    )

    assert result.total == 1
    assert result.records[0]["selected"]["source_item_version"]["properties"]["SourceItemVersionID"] == "VERSION0001"
    assert result.records[0]["selected"]["target_item"]["properties"]["TargetItemCode"] == "M-001"


def test_hugegraph_backend_pattern_search_projects_return_vars_and_properties(monkeypatch):
    _install_hugegraph_http(monkeypatch, include_competing_path=True)
    backend = HugeGraphBackend({"url": "https://hugegraph.example.net", "graph": "demo"})

    result = backend.pattern_search(
        path_pattern=[
            {
                "var": "source_item_version",
                "label": "SourceItemVersion",
                "filters": {"SourceItemCode": "101000-001", "SourceItemVersion": "A"},
            },
            {"var": "rel", "type": "D2O_RELATION", "relation_type": "PROCESS_TO", "direction": "->"},
            {"var": "target_item", "label": "TargetItem"},
        ],
        return_vars=["target_item"],
        return_properties={"target_item": ["TargetItemCode"]},
        limit=10,
        scope=_scope(),
    )

    record = result.records[0]
    assert record["return_vars"] == ["target_item"]
    assert record["relationships"] == []
    assert len(record["nodes"]) == 1
    assert record["nodes"][0]["labels"] == ["TargetItem"]
    assert record["nodes"][0]["properties"] == {"TargetItemCode": "M-001"}
    assert record["selected"] == {"target_item": record["nodes"][0]}


def test_hugegraph_backend_runs_extra_mcp_graph_tools(monkeypatch):
    _install_hugegraph_http(monkeypatch)
    backend = HugeGraphBackend({"url": "https://hugegraph.example.net", "graph": "demo"})

    hop = backend.hop_search(
        entity_instance_id="v-e1",
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
        entity_instance_ids=["v-m1"],
        logic_titles=["推荐规则"],
        scope=_scope(),
    )

    assert hop.records[0]["node_count"] == 2
    assert hop.records[0]["edge_count"] == 1
    assert shortest.total == 1
    assert shortest.records[0]["relationships"][0]["relation_type"] == "PROCESS_TO"
    assert titles.records == [{"entity_instance_id": "v-m1", "entity_type": "TargetItem", "logic_title": "推荐规则"}]
    assert content.records == [
        {
            "entity_instance_id": "v-m1",
            "entity_type": "TargetItem",
            "logic_title": "推荐规则",
            "logic_content": "命中后推荐",
        }
    ]
