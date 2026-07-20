"""HugeGraph graph backend for graph business data search."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import json
import re
from typing import Any
from urllib.parse import quote

import requests

from .base import GraphBackendError, GraphBackendTimeoutError, GraphBackendUnsupportedError
from ..filter_expr import normalize_structured_operator, safe_prop
from ..models import GraphBackendResult, GraphScope
from ..scope import sanitize_labels, sanitize_properties, sanitize_return_properties

_ENTITY_TYPE_ID_TOKEN_RE = re.compile(r"^[eE]\d+$")
_RELATION_TYPE_ID_TOKEN_RE = re.compile(r"^[rR]\d+$")
_DIRECTION_VALUES = {"->", "<-", "--", "→", "←"}
_DEFAULT_VERTEX_LABEL = "D2O_ENTITY"
_DEFAULT_EDGE_LABEL = "D2O_RELATION"
_DEFAULT_GRAPH = "hugegraph"
_SCHEMA_EXCLUDED_KEYS = {
    "id",
    "user_id",
    "workspace_name",
    "scene_name",
    "logic_blocks",
    "properties_json",
    "_d2o_record_key",
}
_HUGEG_INTERNAL_KEYS = {
    "properties_json",
    "d2o_vertex_id",
    "d2o_entity_id",
    "d2o_entity_name",
    "d2o_record_key",
    "d2o_record_pk",
    "d2o_source_type",
    "d2o_source_table",
    "d2o_relation_id",
    "d2o_relation_type",
    "d2o_relation_name",
    "d2o_relation_instance_id",
    "d2o_source_vertex_id",
    "d2o_target_vertex_id",
}
_PATTERN_SEGMENT_META_KEYS = {"return_properties"}
_PATTERN_COMPACT_EXACT_KEYS = {
    "ID",
    "Status",
    "Effective",
    "relation_id",
    "relation_type",
    "relation_name",
    "source_table",
}
_PATTERN_COMPACT_KEYWORDS = (
    "id",
    "code",
    "name",
    "type",
    "status",
    "version",
    "num",
    "qty",
    "count",
    "cost",
    "price",
    "amount",
    "cycle",
    "time",
    "date",
    "effective",
    "conclusion",
    "factory",
    "process",
    "inventory",
    "material",
)


@dataclass(frozen=True)
class _NodeRecord:
    entity_instance_id: str
    entity_type_id: str
    labels: list[str]
    properties: dict[str, Any]


@dataclass(frozen=True)
class _EdgeRecord:
    relation_instance_id: str
    relation_type_id: str
    relation_type: str
    relation_name: str
    relation_physical_type: str
    source_entity_instance_id: str
    target_entity_instance_id: str
    properties: dict[str, Any]


@dataclass(frozen=True)
class _GraphSnapshot:
    nodes: list[_NodeRecord]
    edges: list[_EdgeRecord]
    nodes_by_id: dict[str, _NodeRecord]
    out_edges: dict[str, list[_EdgeRecord]]
    in_edges: dict[str, list[_EdgeRecord]]


@dataclass(frozen=True)
class _NodeStep:
    alias: str
    selector: Any
    props: dict[str, Any]
    return_properties: list[str] | None = None


@dataclass(frozen=True)
class _EdgeStep:
    alias: str
    selector: Any
    direction: str
    return_properties: list[str] | None = None


class HugeGraphBackend:
    """Read-only HugeGraph backend used inside GraphSearchRuntime.

    HugeGraph's REST API is not Cypher-compatible. This backend loads a bounded,
    workspace-scoped graph snapshot and applies the same schema/filter/pattern
    semantics in memory. The scope is enforced while loading the snapshot, before
    any tool-specific filtering can run.
    """

    def __init__(self, provider_config: Mapping[str, Any]) -> None:
        self.url = str(provider_config.get("url") or "").rstrip("/")
        self.graph = str(provider_config.get("graph") or _DEFAULT_GRAPH).strip() or _DEFAULT_GRAPH
        self.graph_space = str(provider_config.get("graph_space") or "").strip()
        self.username = str(provider_config.get("username") or "").strip()
        self.password = str(provider_config.get("password") or "")
        self.token = str(provider_config.get("token") or "").strip()
        self.timeout = int(provider_config.get("timeout_seconds") or 60)
        self.vertex_query_limit = int(provider_config.get("vertex_query_limit") or 200000)
        self.edge_query_limit = int(provider_config.get("edge_query_limit") or 200000)
        self.max_retries = max(0, int(provider_config.get("max_retries") or 0))

    def verify_connectivity(self) -> None:
        if not self.url:
            raise GraphBackendError("HugeGraph URL 未配置")
        try:
            self._request("GET", self._graph_path("/schema"))
        except GraphBackendError:
            raise

    def get_nodes_schema(
        self,
        *,
        scope: GraphScope,
        element_types: list[str] | None = None,
    ) -> dict:
        snapshot = self._load_snapshot(scope=scope)
        wanted = {str(item).strip() for item in (element_types or []) if str(item).strip()}
        schema: dict[str, dict[str, str]] = {}
        excluded = _schema_excluded_keys(scope)
        for node in snapshot.nodes:
            for label in node.labels:
                if wanted and label not in wanted:
                    continue
                bucket = schema.setdefault(label, {})
                for prop_name, value in node.properties.items():
                    if prop_name in excluded:
                        continue
                    _merge_schema_property(bucket, prop_name, _infer_property_type(value))
        labels = sorted(schema)
        return {"labels": labels, "schema": {label: schema[label] for label in labels}, "count": len(labels)}

    def get_edges_schema(
        self,
        *,
        scope: GraphScope,
        relation_types: list[str] | None = None,
    ) -> dict:
        snapshot = self._load_snapshot(scope=scope)
        wanted = {str(item).strip() for item in (relation_types or []) if str(item).strip()}
        excluded = _schema_excluded_keys(scope)
        physical_schema: dict[str, dict[str, str]] = {}
        triple_counts: dict[tuple[str, ...], int] = defaultdict(int)

        for edge in snapshot.edges:
            physical_type = _text(edge.relation_physical_type)
            if physical_type:
                bucket = physical_schema.setdefault(physical_type, {})
                for prop_name, value in edge.properties.items():
                    if prop_name in excluded:
                        continue
                    _merge_schema_property(bucket, prop_name, _infer_property_type(value))

            if wanted and edge.relation_type not in wanted:
                continue
            source_node = snapshot.nodes_by_id.get(edge.source_entity_instance_id)
            target_node = snapshot.nodes_by_id.get(edge.target_entity_instance_id)
            if source_node is None or target_node is None:
                continue
            source_name = _text(source_node.properties.get("entity_name"))
            target_name = _text(target_node.properties.get("entity_name"))
            source_label = _pick_business_label(source_node.labels, source_name)
            target_label = _pick_business_label(target_node.labels, target_name)
            triple_counts[
                (
                    source_node.entity_type_id,
                    source_name,
                    source_label,
                    edge.relation_type_id,
                    edge.relation_type,
                    edge.relation_name,
                    target_node.entity_type_id,
                    target_name,
                    target_label,
                )
            ] += 1

        relation_triples = [
            {
                "source_entity_type_id": key[0],
                "source_entity_name": key[1],
                "source_label": key[2],
                "relation_type_id": key[3],
                "relation_type": key[4],
                "relation_name": key[5],
                "target_entity_type_id": key[6],
                "target_entity_name": key[7],
                "target_label": key[8],
                "edge_count": int(count),
            }
            for key, count in triple_counts.items()
        ]
        relation_triples.sort(key=lambda item: (-int(item.get("edge_count") or 0), _text(item.get("relation_type"))))
        return {
            "relationship_types": sorted({edge.relation_type for edge in snapshot.edges if _text(edge.relation_type)}),
            "schema": physical_schema,
            "physical_schema": physical_schema,
            "relation_triples": relation_triples,
            "physical_relation_type_count": len(physical_schema),
            "relation_triple_count": len(relation_triples),
        }

    def property_filter(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        return_properties: list[str] | None,
        limit: int,
        offset: int,
        scope: GraphScope,
    ) -> GraphBackendResult:
        selected = self._select_elements(
            element_class=element_class,
            element_type=element_type,
            filter_dict=filter_dict or {},
            scope=scope,
        )
        normalized_limit = max(1, int(limit or 100))
        normalized_offset = max(0, int(offset or 0))
        page = selected[normalized_offset : normalized_offset + normalized_limit]
        elem_type = _text(element_type).upper()
        records = [
            _node_payload(item, scope=scope, return_properties=return_properties)
            if elem_type == "NODE"
            else _edge_payload(item, scope=scope, return_properties=return_properties)
            for item in page
        ]
        return GraphBackendResult(
            records=records,
            total=len(records),
            truncated=normalized_offset + len(records) < len(selected),
        )

    def count_search(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        selected = self._select_elements(
            element_class=element_class,
            element_type=element_type,
            filter_dict=filter_dict or {},
            scope=scope,
        )
        return GraphBackendResult(records=[], total=len(selected), truncated=False)

    def aggregate_search(
        self,
        *,
        element_class: str,
        element_type: str,
        target_property: str,
        agg: str,
        filter_dict: dict[str, Any] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        agg_name = _text(agg).upper()
        if agg_name not in {"SUM", "AVG", "MIN", "MAX", "COUNT"}:
            raise GraphBackendUnsupportedError("不支持的聚合函数: {}".format(agg or "<empty>"))
        target = safe_prop(target_property) if _text(target_property) else ""
        if agg_name != "COUNT" and not target:
            raise GraphBackendUnsupportedError("target_property 不能为空")

        selected = self._select_elements(
            element_class=element_class,
            element_type=element_type,
            filter_dict=filter_dict or {},
            scope=scope,
        )
        values = [item.properties.get(target) for item in selected] if target else list(selected)
        if agg_name == "COUNT":
            value = len(selected) if not target else sum(1 for item in values if item is not None)
        else:
            numeric_values = [
                float(item)
                for item in values
                if isinstance(item, (int, float)) and not isinstance(item, bool)
            ]
            value = None
            if numeric_values:
                if agg_name == "SUM":
                    value = float(sum(numeric_values))
                elif agg_name == "AVG":
                    value = float(sum(numeric_values) / len(numeric_values))
                elif agg_name == "MIN":
                    value = float(min(numeric_values))
                elif agg_name == "MAX":
                    value = float(max(numeric_values))

        payload = {
            "value": value,
            "agg": agg_name,
            "target_property": target,
            "applied_filter": dict(filter_dict or {}),
            "filter_keys": list((filter_dict or {}).keys()),
        }
        return GraphBackendResult(records=[payload], total=1, truncated=False, payload={"result": payload})

    def sorted_search(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        return_properties: list[str] | None,
        sort_by: list[str] | None,
        ascending: bool,
        limit: int,
        offset: int,
        scope: GraphScope,
    ) -> GraphBackendResult:
        selected = self._select_elements(
            element_class=element_class,
            element_type=element_type,
            filter_dict=filter_dict or {},
            scope=scope,
        )
        safe_sort_props = [safe_prop(item) for item in (sort_by or []) if _text(item)]
        if safe_sort_props:
            selected.sort(
                key=lambda item: tuple(_sortable_value(item.properties.get(prop)) for prop in safe_sort_props),
                reverse=not bool(ascending),
            )

        normalized_limit = max(1, int(limit or 50))
        normalized_offset = max(0, int(offset or 0))
        page = selected[normalized_offset : normalized_offset + normalized_limit]
        elem_type = _text(element_type).upper()
        records = [
            _node_payload(item, scope=scope, return_properties=return_properties)
            if elem_type == "NODE"
            else _edge_payload(item, scope=scope, return_properties=return_properties)
            for item in page
        ]
        return GraphBackendResult(
            records=records,
            total=len(selected),
            truncated=normalized_offset + len(records) < len(selected),
        )

    def pattern_search(
        self,
        *,
        path_pattern: list[Any],
        return_vars: list[str] | None,
        limit: int,
        scope: GraphScope,
        return_properties: Any | None = None,
    ) -> GraphBackendResult:
        if not path_pattern or len(path_pattern) < 3 or len(path_pattern) % 2 == 0:
            raise GraphBackendUnsupportedError("path_pattern 需为奇数长度，且至少包含节点-关系-节点")

        snapshot = self._load_snapshot(scope=scope)
        node_steps, edge_steps = _build_pattern_steps(path_pattern)
        available_vars = _build_available_query_vars(node_steps, edge_steps)
        output_selection = _resolve_output_selection(return_vars, available_vars)
        property_projection = _normalize_pattern_return_properties(return_properties)
        normalized_limit = max(1, int(limit or 100))

        records: list[dict[str, Any]] = []
        first_node_step = node_steps[0]
        for node in snapshot.nodes:
            if not _matches_node_selector(node, first_node_step.selector, first_node_step.props):
                continue
            assignment: dict[str, Any] = {first_node_step.alias: node}
            self._dfs_pattern_match(
                snapshot=snapshot,
                node_steps=node_steps,
                edge_steps=edge_steps,
                depth=0,
                current_node=node,
                assignment=assignment,
                output_selection=output_selection,
                return_properties=property_projection,
                results=records,
                limit=normalized_limit,
                scope=scope,
            )
            if len(records) >= normalized_limit:
                break

        return GraphBackendResult(records=records, total=len(records), truncated=len(records) >= normalized_limit)

    def hop_search(
        self,
        *,
        entity_instance_id: str,
        hop_num: int,
        accurate_flag: bool,
        scope: GraphScope,
    ) -> GraphBackendResult:
        normalized_start = _text(entity_instance_id)
        if not normalized_start:
            raise GraphBackendUnsupportedError("entity_instance_id 不能为空")
        depth_limit = max(1, min(int(hop_num or 1), 5))
        snapshot = self._load_snapshot(scope=scope)
        if normalized_start not in snapshot.nodes_by_id:
            payload = _hop_payload([], [], normalized_start, depth_limit, accurate_flag)
            return GraphBackendResult(records=[payload], total=0, truncated=False, payload={"result": payload})

        distance = self._shortest_distance_map(snapshot, normalized_start, depth_limit)
        if accurate_flag:
            selected_nodes, selected_edges = self._select_exact_hop_subgraph(snapshot, distance, depth_limit)
        else:
            selected_nodes = {node_id for node_id, dist in distance.items() if dist <= depth_limit}
            selected_edges = {
                edge.relation_instance_id
                for edge in snapshot.edges
                if edge.source_entity_instance_id in selected_nodes and edge.target_entity_instance_id in selected_nodes
            }

        node_payloads = [
            _node_payload(snapshot.nodes_by_id[node_id], scope=scope)
            for node_id in sorted(selected_nodes)
            if node_id in snapshot.nodes_by_id
        ]
        edge_payloads = [
            _edge_payload_with_compat(edge, scope=scope)
            for edge in sorted(snapshot.edges, key=lambda row: row.relation_instance_id)
            if edge.relation_instance_id in selected_edges
        ]
        payload = _hop_payload(node_payloads, edge_payloads, normalized_start, depth_limit, accurate_flag)
        return GraphBackendResult(records=[payload], total=len(node_payloads), truncated=False, payload={"result": payload})

    def shortest_path_search(
        self,
        *,
        start_label: str,
        start_props: dict[str, Any],
        end_label: str,
        end_props: dict[str, Any],
        max_depth: int,
        directed: bool,
        scope: GraphScope,
    ) -> GraphBackendResult:
        start_token = _text(start_label)
        end_token = _text(end_label)
        if not start_token:
            raise GraphBackendUnsupportedError("start_label 不能为空")
        if not end_token:
            raise GraphBackendUnsupportedError("end_label 不能为空")
        if not isinstance(start_props, Mapping):
            raise GraphBackendUnsupportedError("start_props 必须是对象")
        if not isinstance(end_props, Mapping):
            raise GraphBackendUnsupportedError("end_props 必须是对象")

        depth_limit = max(1, min(int(max_depth or 15), 15))
        snapshot = self._load_snapshot(scope=scope)
        start_nodes = [
            node
            for node in snapshot.nodes
            if start_token in node.labels and _match_filter_dict(node.properties, start_props or {})
        ]
        end_nodes = [
            node
            for node in snapshot.nodes
            if end_token in node.labels and _match_filter_dict(node.properties, end_props or {})
        ]
        if not start_nodes or not end_nodes:
            return GraphBackendResult(records=[], total=0, truncated=False)

        records: list[dict[str, Any]] = []
        seen_path_signatures: set[tuple[str, ...]] = set()
        for start_node in sorted(start_nodes, key=lambda row: row.entity_instance_id):
            for end_node in sorted(end_nodes, key=lambda row: row.entity_instance_id):
                if start_node.entity_instance_id == end_node.entity_instance_id:
                    continue
                found_path = self._find_shortest_path(
                    snapshot=snapshot,
                    start_id=start_node.entity_instance_id,
                    end_id=end_node.entity_instance_id,
                    directed=directed,
                    max_depth=depth_limit,
                )
                if found_path is None:
                    continue
                node_ids, edge_ids = found_path
                signature = tuple(node_ids + edge_ids)
                if signature in seen_path_signatures:
                    continue
                seen_path_signatures.add(signature)
                relationships: list[dict[str, Any]] = []
                for edge_id in edge_ids:
                    edge_record = self._find_edge(snapshot, edge_id)
                    if edge_record is not None:
                        relationships.append(_edge_payload_with_compat(edge_record, scope=scope))
                records.append({
                    "nodes": [_node_payload(snapshot.nodes_by_id[node_id], scope=scope) for node_id in node_ids],
                    "edges": relationships,
                    "relationships": relationships,
                })
                if len(records) >= 5:
                    return GraphBackendResult(records=records, total=len(records), truncated=False)
            if len(records) >= 5:
                break
        return GraphBackendResult(records=records, total=len(records), truncated=False)

    def list_logic_titles(
        self,
        *,
        entity_type: str = "",
        scope: GraphScope,
    ) -> GraphBackendResult:
        normalized_entity_type = _text(entity_type)
        snapshot = self._load_snapshot(scope=scope)
        items: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for node in snapshot.nodes:
            if _is_logic_element(node.properties):
                continue
            entity_type_value = _pick_business_label(node.labels, node.properties.get("entity_type"))
            if normalized_entity_type and entity_type_value != normalized_entity_type:
                continue
            for block in _normalize_logic_blocks(node.properties.get("logic_blocks")):
                title = _text(block.get("logic_title"))
                content = _text(block.get("logic_content"))
                if not title:
                    continue
                dedup_key = (entity_type_value.casefold(), title.casefold(), content.casefold())
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                items.append({
                    "entity_instance_id": node.entity_instance_id,
                    "entity_type": entity_type_value,
                    "logic_title": title,
                })
        return GraphBackendResult(records=items, total=len(items), truncated=False)

    def get_logic_content(
        self,
        *,
        entity_instance_ids: list[str],
        logic_titles: list[str] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        normalized_entity_ids = _normalize_text_list(entity_instance_ids)
        if not normalized_entity_ids:
            raise GraphBackendUnsupportedError("entity_instance_ids 不能为空")
        normalized_titles = {item.casefold() for item in _normalize_text_list(logic_titles)}
        snapshot = self._load_snapshot(scope=scope)
        items: list[dict[str, Any]] = []
        seen: set[tuple[str, str, str]] = set()
        for entity_id in normalized_entity_ids:
            node = snapshot.nodes_by_id.get(entity_id)
            if node is None or _is_logic_element(node.properties):
                continue
            entity_type_value = _pick_business_label(node.labels, node.properties.get("entity_type"))
            for block in _normalize_logic_blocks(node.properties.get("logic_blocks")):
                title = _text(block.get("logic_title"))
                content = _text(block.get("logic_content"))
                if normalized_titles and title.casefold() not in normalized_titles:
                    continue
                if not title or not content:
                    continue
                dedup_key = (entity_type_value.casefold(), title.casefold(), content.casefold())
                if dedup_key in seen:
                    continue
                seen.add(dedup_key)
                items.append({
                    "entity_instance_id": node.entity_instance_id,
                    "entity_type": entity_type_value,
                    "logic_title": title,
                    "logic_content": content,
                })
        return GraphBackendResult(records=items, total=len(items), truncated=False)

    def get_element_info(
        self,
        *,
        element_instance_id: str,
        element_class: str = "",
        element_type: str = "",
        scope: GraphScope,
    ) -> dict:
        normalized_id = _text(element_instance_id)
        if not normalized_id:
            raise GraphBackendUnsupportedError("element_instance_id 不能为空")
        snapshot = self._load_snapshot(scope=scope)
        normalized_class = _text(element_class)
        elem_type = _text(element_type).upper()

        if elem_type in {"", "NODE"}:
            node = snapshot.nodes_by_id.get(normalized_id)
            if node is not None and (not normalized_class or normalized_class in node.labels):
                payload = _node_payload(node, scope=scope)
                payload.update({"found": True, "kind": "node", "element_type": "NODE"})
                return payload
        if elem_type in {"", "EDGE"}:
            for edge in snapshot.edges:
                if edge.relation_instance_id != normalized_id:
                    continue
                if normalized_class and not _matches_edge_class(edge, normalized_class):
                    continue
                payload = _edge_payload(edge, scope=scope)
                payload.update({"found": True, "kind": "relationship", "element_type": "EDGE"})
                return payload
        return {"found": False, "element_id": normalized_id, "element_instance_id": normalized_id}

    def close(self) -> None:
        return None

    def _shortest_distance_map(
        self,
        snapshot: _GraphSnapshot,
        start_node_id: str,
        max_depth: int,
    ) -> dict[str, int]:
        distance: dict[str, int] = {start_node_id: 0}
        queue: deque[str] = deque([start_node_id])
        while queue:
            current = queue.popleft()
            current_depth = distance[current]
            if current_depth >= max_depth:
                continue
            for _edge, neighbor_id in _iter_neighbors(snapshot, current, directed=False):
                if neighbor_id in distance:
                    continue
                distance[neighbor_id] = current_depth + 1
                queue.append(neighbor_id)
        return distance

    def _select_exact_hop_subgraph(
        self,
        snapshot: _GraphSnapshot,
        distance: dict[str, int],
        hop_num: int,
    ) -> tuple[set[str], set[str]]:
        target_nodes = {node_id for node_id, dist in distance.items() if dist == hop_num}
        if not target_nodes:
            return set(), set()

        reverse_distance: dict[str, int] = {node_id: 0 for node_id in target_nodes}
        queue: deque[str] = deque(target_nodes)
        while queue:
            current = queue.popleft()
            current_depth = reverse_distance[current]
            if current_depth >= hop_num:
                continue
            for _edge, neighbor_id in _iter_neighbors(snapshot, current, directed=False):
                if neighbor_id in reverse_distance:
                    continue
                reverse_distance[neighbor_id] = current_depth + 1
                queue.append(neighbor_id)

        selected_nodes = {
            node_id
            for node_id, dist_start in distance.items()
            if node_id in reverse_distance and dist_start + reverse_distance[node_id] == hop_num
        }
        selected_edges: set[str] = set()
        for edge in snapshot.edges:
            source = edge.source_entity_instance_id
            target = edge.target_entity_instance_id
            if source not in selected_nodes or target not in selected_nodes:
                continue
            source_depth = distance.get(source)
            target_depth = distance.get(target)
            if source_depth is None or target_depth is None:
                continue
            if abs(source_depth - target_depth) != 1:
                continue
            selected_edges.add(edge.relation_instance_id)
        return selected_nodes, selected_edges

    def _find_shortest_path(
        self,
        *,
        snapshot: _GraphSnapshot,
        start_id: str,
        end_id: str,
        directed: bool,
        max_depth: int,
    ) -> tuple[list[str], list[str]] | None:
        queue: deque[str] = deque([start_id])
        visited: set[str] = {start_id}
        depth: dict[str, int] = {start_id: 0}
        previous_node: dict[str, str] = {}
        previous_edge: dict[str, str] = {}

        while queue:
            current = queue.popleft()
            current_depth = depth[current]
            if current == end_id:
                break
            if current_depth >= max_depth:
                continue
            for edge, neighbor_id in _iter_neighbors(snapshot, current, directed=directed):
                if neighbor_id in visited:
                    continue
                visited.add(neighbor_id)
                depth[neighbor_id] = current_depth + 1
                previous_node[neighbor_id] = current
                previous_edge[neighbor_id] = edge.relation_instance_id
                queue.append(neighbor_id)

        if end_id not in visited:
            return None

        node_ids: list[str] = [end_id]
        edge_ids: list[str] = []
        cursor = end_id
        while cursor != start_id:
            parent = previous_node.get(cursor)
            edge_id = previous_edge.get(cursor)
            if parent is None or edge_id is None:
                return None
            edge_ids.append(edge_id)
            node_ids.append(parent)
            cursor = parent
        node_ids.reverse()
        edge_ids.reverse()
        return node_ids, edge_ids

    def _find_edge(self, snapshot: _GraphSnapshot, edge_id: str) -> _EdgeRecord | None:
        for edge in snapshot.edges:
            if edge.relation_instance_id == edge_id:
                return edge
        return None

    def _select_elements(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any],
        scope: GraphScope,
    ) -> list[_NodeRecord | _EdgeRecord]:
        normalized_class = _text(element_class)
        elem_type = _text(element_type).upper()
        if not normalized_class:
            raise GraphBackendUnsupportedError("element_class 不能为空")
        snapshot = self._load_snapshot(scope=scope)

        if elem_type == "NODE":
            return [
                node
                for node in snapshot.nodes
                if normalized_class in node.labels and _match_filter_dict(node.properties, filter_dict)
            ]
        if elem_type == "EDGE":
            return [
                edge
                for edge in snapshot.edges
                if _matches_edge_class(edge, normalized_class) and _match_filter_dict(edge.properties, filter_dict)
            ]
        raise GraphBackendUnsupportedError("HugeGraph 仅支持 NODE/EDGE 查询")

    def _load_snapshot(self, *, scope: GraphScope) -> _GraphSnapshot:
        nodes = self._load_nodes(scope=scope)
        nodes_by_id = {node.entity_instance_id: node for node in nodes}
        edges = self._load_edges(scope=scope, nodes_by_id=nodes_by_id)
        out_edges: dict[str, list[_EdgeRecord]] = defaultdict(list)
        in_edges: dict[str, list[_EdgeRecord]] = defaultdict(list)
        for edge in edges:
            out_edges[edge.source_entity_instance_id].append(edge)
            in_edges[edge.target_entity_instance_id].append(edge)
        return _GraphSnapshot(
            nodes=nodes,
            edges=edges,
            nodes_by_id=nodes_by_id,
            out_edges=dict(out_edges),
            in_edges=dict(in_edges),
        )

    def _load_nodes(self, *, scope: GraphScope) -> list[_NodeRecord]:
        rows = self._list_vertices(limit=max(1, self.vertex_query_limit), label=_DEFAULT_VERTEX_LABEL)
        if not rows:
            rows = self._list_vertices(limit=max(1, self.vertex_query_limit), label=None)

        nodes: list[_NodeRecord] = []
        for row in rows:
            node = _build_node_record(row)
            if node is None:
                continue
            if not _matches_scope(node.properties, scope):
                continue
            if _is_logic_element(node.properties):
                continue
            nodes.append(node)
        return nodes

    def _load_edges(self, *, scope: GraphScope, nodes_by_id: dict[str, _NodeRecord]) -> list[_EdgeRecord]:
        rows = self._list_edges(limit=max(1, self.edge_query_limit), label=_DEFAULT_EDGE_LABEL)
        if not rows:
            rows = self._list_edges(limit=max(1, self.edge_query_limit), label=None)

        edges: list[_EdgeRecord] = []
        for row in rows:
            edge = _build_edge_record(row)
            if edge is None:
                continue
            if not _matches_scope(edge.properties, scope):
                continue
            source_node = nodes_by_id.get(edge.source_entity_instance_id)
            target_node = nodes_by_id.get(edge.target_entity_instance_id)
            if source_node is None or target_node is None:
                continue
            edges.append(edge)
        return edges

    def _dfs_pattern_match(
        self,
        *,
        snapshot: _GraphSnapshot,
        node_steps: list[_NodeStep],
        edge_steps: list[_EdgeStep],
        depth: int,
        current_node: _NodeRecord,
        assignment: dict[str, Any],
        output_selection: list[tuple[str, str]],
        return_properties: Any | None,
        results: list[dict[str, Any]],
        limit: int,
        scope: GraphScope,
    ) -> None:
        if len(results) >= limit:
            return
        if depth >= len(edge_steps):
            results.append(_pattern_record(
                node_steps,
                edge_steps,
                assignment,
                output_selection,
                return_properties=return_properties,
                scope=scope,
            ))
            return

        edge_step = edge_steps[depth]
        next_node_step = node_steps[depth + 1]
        for edge, neighbor_id in _iter_neighbors(snapshot, current_node.entity_instance_id, directed=edge_step.direction == "->"):
            if edge_step.direction == "<-" and edge.target_entity_instance_id != current_node.entity_instance_id:
                continue
            if edge_step.direction == "->" and edge.source_entity_instance_id != current_node.entity_instance_id:
                continue
            if edge_step.direction == "--":
                neighbor_id = (
                    edge.target_entity_instance_id
                    if current_node.entity_instance_id == edge.source_entity_instance_id
                    else edge.source_entity_instance_id
                )
            if not _matches_edge_selector(edge, edge_step.selector):
                continue
            next_node = snapshot.nodes_by_id.get(neighbor_id)
            if next_node is None:
                continue
            if not _matches_node_selector(next_node, next_node_step.selector, next_node_step.props):
                continue
            next_assignment = dict(assignment)
            next_assignment[edge_step.alias] = edge
            next_assignment[next_node_step.alias] = next_node
            self._dfs_pattern_match(
                snapshot=snapshot,
                node_steps=node_steps,
                edge_steps=edge_steps,
                depth=depth + 1,
                current_node=next_node,
                assignment=next_assignment,
                output_selection=output_selection,
                return_properties=return_properties,
                results=results,
                limit=limit,
                scope=scope,
            )

    def _list_vertices(
        self,
        *,
        limit: int,
        label: str | None,
        properties: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            self._graph_path("/graph/vertices"),
            params=_query_params(limit=limit, label=label, properties=properties),
        )
        return _extract_items(payload, "vertices")

    def _list_edges(
        self,
        *,
        limit: int,
        label: str | None,
        properties: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        payload = self._request(
            "GET",
            self._graph_path("/graph/edges"),
            params=_query_params(limit=limit, label=label, properties=properties),
        )
        return _extract_items(payload, "edges")

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any | None = None,
    ) -> Any:
        if not self.url:
            raise GraphBackendError("HugeGraph URL 未配置")
        url = "{}{}".format(self.url, path)
        timeout = (min(max(1, int(self.timeout or 60)), 15), max(1, int(self.timeout or 60)))
        headers = {}
        if self.token:
            headers["Authorization"] = "Bearer {}".format(self.token)
        auth = (self.username, self.password) if self.username and self.password else None

        attempts = self.max_retries + 1
        last_exc: Exception | None = None
        for _attempt in range(attempts):
            try:
                response = requests.request(
                    method=method,
                    url=url,
                    params=params,
                    json=json_body,
                    timeout=timeout,
                    headers=headers,
                    auth=auth,
                )
                response.raise_for_status()
                try:
                    return response.json()
                except Exception:
                    return response.text
            except requests.exceptions.Timeout as exc:
                last_exc = exc
            except requests.exceptions.RequestException as exc:
                last_exc = exc
                if _attempt >= attempts - 1:
                    break
                continue
        if isinstance(last_exc, requests.exceptions.Timeout):
            raise GraphBackendTimeoutError("HugeGraph request timeout") from last_exc
        raise GraphBackendError("HugeGraph request failed") from last_exc

    def _graph_path(self, suffix: str) -> str:
        graph_name = quote(self.graph or _DEFAULT_GRAPH, safe="")
        normalized_suffix = suffix if str(suffix or "").startswith("/") else "/{}".format(suffix)
        if self.graph_space:
            graph_space = quote(self.graph_space, safe="")
            return "/graphspaces/{}/graphs/{}{}".format(graph_space, graph_name, normalized_suffix)
        return "/graphs/{}{}".format(graph_name, normalized_suffix)


def _query_params(
    *,
    limit: int,
    label: str | None,
    properties: dict[str, Any] | None,
) -> dict[str, Any]:
    params: dict[str, Any] = {"limit": max(1, int(limit or 1))}
    if label:
        params["label"] = str(label).strip()
    if properties is not None:
        params["properties"] = json.dumps(properties, ensure_ascii=False)
    return params


def _extract_items(payload: Any, key: str) -> list[dict[str, Any]]:
    if isinstance(payload, Mapping):
        items = payload.get(key)
        if isinstance(items, list):
            return [dict(item) for item in items if isinstance(item, Mapping)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, Mapping)]
    return []


def _build_node_record(raw: Mapping[str, Any]) -> _NodeRecord | None:
    raw_properties = dict(raw.get("properties") or {}) if isinstance(raw.get("properties"), Mapping) else {}
    properties = dict(raw_properties)
    properties.update(_json_object(raw_properties.get("properties_json")))
    properties.setdefault("entity_id", _text(raw_properties.get("d2o_entity_id")))
    properties.setdefault("entity_name", _text(raw_properties.get("d2o_entity_name")))
    properties.setdefault("record_key", _text(raw_properties.get("d2o_record_key")))
    properties.setdefault("record_pk", _text(raw_properties.get("d2o_record_pk")))
    properties.setdefault("source_type", _text(raw_properties.get("d2o_source_type")))
    properties.setdefault("source_table", _text(raw_properties.get("d2o_source_table")))

    entity_instance_id = _text(raw.get("id")) or _text(raw_properties.get("d2o_vertex_id"))
    if not entity_instance_id:
        return None
    labels = _derive_node_labels(raw, properties)
    return _NodeRecord(
        entity_instance_id=entity_instance_id,
        entity_type_id=_text(properties.get("entity_id")),
        labels=labels,
        properties=properties,
    )


def _derive_node_labels(raw_node: Mapping[str, Any], properties: Mapping[str, Any]) -> list[str]:
    labels: list[str] = []
    raw_labels = properties.get("labels")
    if isinstance(raw_labels, list):
        labels.extend(_text(item) for item in raw_labels)
    elif isinstance(raw_labels, str):
        labels.append(_text(raw_labels))
    entity_name = _text(properties.get("entity_name"))
    if entity_name:
        labels.append(entity_name)
    raw_label = _text(raw_node.get("label"))
    if raw_label and raw_label != _DEFAULT_VERTEX_LABEL:
        labels.append(raw_label)
    return sanitize_labels(labels)


def _build_edge_record(raw: Mapping[str, Any]) -> _EdgeRecord | None:
    raw_properties = dict(raw.get("properties") or {}) if isinstance(raw.get("properties"), Mapping) else {}
    properties = dict(raw_properties)
    properties.update(_json_object(raw_properties.get("properties_json")))
    relation_type_id = _text(properties.get("relation_id")) or _text(raw_properties.get("d2o_relation_id"))
    relation_type = (
        _text(properties.get("relation_type"))
        or _text(raw_properties.get("d2o_relation_type"))
        or _text(raw_properties.get("d2o_relation_name"))
        or _text(raw.get("label"))
    )
    relation_name = (
        _text(properties.get("relation_name"))
        or _text(raw_properties.get("d2o_relation_name"))
        or relation_type
    )
    properties.setdefault("relation_id", relation_type_id)
    properties.setdefault("relation_type", relation_type)
    properties.setdefault("relation_name", relation_name)

    edge_id = _text(raw.get("id")) or _text(raw_properties.get("d2o_relation_instance_id"))
    source_id = _text(raw.get("outV")) or _text(raw_properties.get("d2o_source_vertex_id"))
    target_id = _text(raw.get("inV")) or _text(raw_properties.get("d2o_target_vertex_id"))
    physical_type = _text(raw.get("label")) or _text(raw.get("type"))
    if not edge_id or not source_id or not target_id:
        return None
    return _EdgeRecord(
        relation_instance_id=edge_id,
        relation_type_id=relation_type_id,
        relation_type=relation_type,
        relation_name=relation_name,
        relation_physical_type=physical_type,
        source_entity_instance_id=source_id,
        target_entity_instance_id=target_id,
        properties=properties,
    )


def _node_payload(
    node: _NodeRecord,
    *,
    scope: GraphScope,
    return_properties: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "element_id": node.entity_instance_id,
        "entity_instance_id": node.entity_instance_id,
        "entity_type_id": node.entity_type_id,
        "labels": sanitize_labels(node.labels),
        "properties": sanitize_properties(
            node.properties,
            return_properties=return_properties,
            hidden_properties=_hidden_properties(scope),
        ),
    }


def _edge_payload(
    edge: _EdgeRecord,
    *,
    scope: GraphScope,
    return_properties: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "element_id": edge.relation_instance_id,
        "relation_instance_id": edge.relation_instance_id,
        "type": edge.relation_physical_type,
        "relation_type": edge.relation_type,
        "relation_type_id": edge.relation_type_id,
        "source_id": edge.source_entity_instance_id,
        "target_id": edge.target_entity_instance_id,
        "source_entity_instance_id": edge.source_entity_instance_id,
        "target_entity_instance_id": edge.target_entity_instance_id,
        "properties": sanitize_properties(
            edge.properties,
            return_properties=return_properties,
            hidden_properties=_hidden_properties(scope),
        ),
    }


def _edge_payload_with_compat(
    edge: _EdgeRecord,
    *,
    scope: GraphScope,
    return_properties: list[str] | None = None,
) -> dict[str, Any]:
    payload = _edge_payload(edge, scope=scope, return_properties=return_properties)
    payload.setdefault("relation_physical_type", payload.get("type") or "")
    return payload


def _hop_payload(
    nodes: list[dict[str, Any]],
    relationships: list[dict[str, Any]],
    center_id: str,
    hop_num: int,
    accurate_flag: bool,
) -> dict[str, Any]:
    return {
        "nodes": nodes,
        "edges": relationships,
        "relationships": relationships,
        "node_count": len(nodes),
        "edge_count": len(relationships),
        "relationship_count": len(relationships),
        "center_id": center_id,
        "hop_num": hop_num,
        "accurate_flag": bool(accurate_flag),
    }


def _pattern_record(
    node_steps: list[_NodeStep],
    edge_steps: list[_EdgeStep],
    assignment: Mapping[str, Any],
    output_selection: list[tuple[str, str]],
    *,
    return_properties: Any | None,
    scope: GraphScope,
) -> dict[str, Any]:
    selected_aliases = {query_var for _output_var, query_var in output_selection}
    nodes = [
        _node_payload(
            value,
            scope=scope,
            return_properties=_pattern_return_properties(
                alias=step.alias,
                step_return_properties=step.return_properties,
                top_level_return_properties=return_properties,
                properties=value.properties,
            ),
        )
        for step in node_steps
        for value in [assignment.get(step.alias)]
        if step.alias in selected_aliases and isinstance(value, _NodeRecord)
    ]
    relationships = [
        _edge_payload(
            value,
            scope=scope,
            return_properties=_pattern_return_properties(
                alias=step.alias,
                step_return_properties=step.return_properties,
                top_level_return_properties=return_properties,
                properties=value.properties,
            ),
        )
        for step in edge_steps
        for value in [assignment.get(step.alias)]
        if step.alias in selected_aliases and isinstance(value, _EdgeRecord)
    ]
    node_steps_by_alias = {step.alias: step for step in node_steps}
    edge_steps_by_alias = {step.alias: step for step in edge_steps}
    selected: dict[str, Any] = {}
    for output_var, query_var in output_selection:
        value = assignment.get(query_var)
        if isinstance(value, _NodeRecord):
            step = node_steps_by_alias.get(query_var)
            selected[output_var] = _node_payload(
                value,
                scope=scope,
                return_properties=_pattern_return_properties(
                    alias=query_var,
                    step_return_properties=step.return_properties if step else None,
                    top_level_return_properties=return_properties,
                    properties=value.properties,
                ),
            )
        elif isinstance(value, _EdgeRecord):
            step = edge_steps_by_alias.get(query_var)
            selected[output_var] = _edge_payload(
                value,
                scope=scope,
                return_properties=_pattern_return_properties(
                    alias=query_var,
                    step_return_properties=step.return_properties if step else None,
                    top_level_return_properties=return_properties,
                    properties=value.properties,
                ),
            )
    return {
        "nodes": nodes,
        "relationships": relationships,
        "return_vars": [name for name, _query_var in output_selection],
        "selected": selected,
    }


def _build_pattern_steps(path_pattern: list[Any]) -> tuple[list[_NodeStep], list[_EdgeStep]]:
    if all(isinstance(item, Mapping) for item in path_pattern):
        return _build_mapping_pattern_steps([dict(item) for item in path_pattern])
    node_steps: list[_NodeStep] = []
    edge_steps: list[_EdgeStep] = []
    for index, raw_item in enumerate(path_pattern):
        if not isinstance(raw_item, (list, tuple)) or not raw_item:
            raise GraphBackendUnsupportedError("path_pattern[{}] 结构非法".format(index))
        item = list(raw_item)
        if index % 2 == 0:
            alias = _node_alias(index // 2)
            selector = item[0]
            props = item[1] if len(item) > 1 and isinstance(item[1], Mapping) else {}
            return_properties = _segment_return_properties(selector) if isinstance(selector, Mapping) else None
            props_return_properties = _segment_return_properties(props) if isinstance(props, Mapping) else None
            if props_return_properties is not None:
                return_properties = props_return_properties
            if isinstance(props, Mapping):
                props = _expand_segment_props(props)
            if isinstance(selector, Mapping):
                selector = dict(selector)
                selector_props = _segment_props(selector)
                if selector_props:
                    merged_props = dict(selector_props)
                    merged_props.update(dict(props))
                    selector["props"] = merged_props
                    selector.pop("filters", None)
                    props = {}
            node_steps.append(_NodeStep(
                alias=alias,
                selector=selector,
                props=dict(props),
                return_properties=return_properties,
            ))
        else:
            alias = "r{}".format((index + 1) // 2)
            selector = item[0]
            return_properties = _segment_return_properties(selector) if isinstance(selector, Mapping) else None
            if isinstance(selector, Mapping):
                selector = dict(selector)
                selector_props = _segment_props(selector)
                if selector_props:
                    selector["props"] = selector_props
                    selector.pop("filters", None)
            edge_steps.append(
                _EdgeStep(
                    alias=alias,
                    selector=selector,
                    direction=_normalize_direction(item[1] if len(item) > 1 else "->"),
                    return_properties=return_properties,
                )
            )
    return node_steps, edge_steps


def _build_mapping_pattern_steps(segments: list[dict[str, Any]]) -> tuple[list[_NodeStep], list[_EdgeStep]]:
    node_steps: list[_NodeStep] = []
    edge_steps: list[_EdgeStep] = []
    for index, segment in enumerate(segments):
        if index % 2 == 0:
            selector = {
                "label": _text(segment.get("label") or segment.get("type")),
                "entity_type_id": _text(segment.get("entity_type_id")),
                "entity_instance_id": _text(segment.get("entity_instance_id")),
                "entity_name": _text(segment.get("entity_name")),
                "props": _segment_props(segment),
            }
            node_steps.append(
                _NodeStep(
                    alias=_text(segment.get("var")) or _node_alias(index // 2),
                    selector=selector,
                    props={},
                    return_properties=_segment_return_properties(segment),
                )
            )
        else:
            selector = {
                "type": _text(segment.get("type")),
                "relation_type": _text(segment.get("relation_type")),
                "relation_type_id": _text(segment.get("relation_type_id")),
                "relation_name": _text(segment.get("relation_name")),
                "props": _segment_props(segment),
            }
            edge_steps.append(
                _EdgeStep(
                    alias=_text(segment.get("var")) or "r{}".format((index + 1) // 2),
                    selector=selector,
                    direction=_normalize_direction(segment.get("direction") or "->"),
                    return_properties=_segment_return_properties(segment),
                )
            )
    return node_steps, edge_steps


def _build_available_query_vars(node_steps: list[_NodeStep], edge_steps: list[_EdgeStep]) -> list[str]:
    variables: list[str] = []
    for index, node_step in enumerate(node_steps):
        if index == 0:
            variables.append(node_step.alias)
        if index < len(edge_steps):
            variables.append(edge_steps[index].alias)
            variables.append(node_steps[index + 1].alias)
    return variables


def _resolve_output_selection(
    return_vars: list[str] | None,
    available_query_vars: list[str],
) -> list[tuple[str, str]]:
    default_selection = [(name, name) for name in available_query_vars]
    if not return_vars:
        return default_selection
    selected: list[tuple[str, str]] = []
    seen: set[str] = set()
    for raw_var in return_vars:
        name = _text(raw_var)
        if not name or name in seen or name not in available_query_vars:
            continue
        seen.add(name)
        selected.append((name, name))
    return selected or default_selection


def _matches_node_selector(node: _NodeRecord, selector: Any, props: Mapping[str, Any]) -> bool:
    if isinstance(selector, str):
        token = _text(selector)
        if not token:
            return False
        if _ENTITY_TYPE_ID_TOKEN_RE.match(token):
            if node.entity_type_id != token:
                return False
        elif token not in node.labels:
            return False
        return _match_filter_dict(node.properties, props or {})
    if isinstance(selector, Mapping):
        label = _text(selector.get("label") or selector.get("type"))
        if label and label not in node.labels:
            return False
        entity_type_id = _text(selector.get("entity_type_id"))
        if entity_type_id and node.entity_type_id != entity_type_id:
            return False
        entity_instance_id = _text(selector.get("entity_instance_id"))
        if entity_instance_id and node.entity_instance_id != entity_instance_id:
            return False
        entity_name = _text(selector.get("entity_name"))
        if entity_name and _text(node.properties.get("entity_name")) != entity_name:
            return False
        selector_props = _segment_props(selector)
        merged_props = dict(selector_props or {})
        merged_props.update(dict(props or {}))
        return _match_filter_dict(node.properties, merged_props)
    return False


def _matches_edge_selector(edge: _EdgeRecord, selector: Any) -> bool:
    if isinstance(selector, str):
        token = _text(selector)
        if not token:
            return False
        if _RELATION_TYPE_ID_TOKEN_RE.match(token):
            return edge.relation_type_id == token
        return token in {edge.relation_physical_type, edge.relation_type, edge.relation_name}
    if isinstance(selector, Mapping):
        physical_type = _text(selector.get("type"))
        if physical_type and edge.relation_physical_type != physical_type:
            return False
        relation_type = _text(selector.get("relation_type"))
        if relation_type and edge.relation_type != relation_type:
            return False
        relation_type_id = _text(selector.get("relation_type_id"))
        if relation_type_id and edge.relation_type_id != relation_type_id:
            return False
        relation_name = _text(selector.get("relation_name"))
        if relation_name and edge.relation_name != relation_name:
            return False
        props = _segment_props(selector)
        return _match_filter_dict(edge.properties, props or {})
    return False


def _segment_props(segment: Mapping[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key in ("props", "filters"):
        raw = segment.get(key)
        if isinstance(raw, Mapping):
            props.update({
                str(prop): value
                for prop, value in raw.items()
                if str(prop or "").strip() not in _PATTERN_SEGMENT_META_KEYS
            })
    return props


def _expand_segment_props(segment: Mapping[str, Any]) -> dict[str, Any]:
    nested = _segment_props(segment)
    if not nested:
        return {
            key: value
            for key, value in segment.items()
            if str(key or "").strip() not in _PATTERN_SEGMENT_META_KEYS
        }
    direct = {
        key: value
        for key, value in segment.items()
        if key not in {"props", "filters"} and str(key or "").strip() not in _PATTERN_SEGMENT_META_KEYS
    }
    direct.update(nested)
    return direct


def _segment_return_properties(segment: Any) -> list[str] | None:
    if not isinstance(segment, Mapping) or "return_properties" not in segment:
        return None
    raw = segment.get("return_properties")
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if not isinstance(raw, (list, tuple)):
        return []
    return _clean_return_properties(raw)


def _normalize_pattern_return_properties(raw: Any) -> Any | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, Mapping):
        normalized: dict[str, list[str]] = {}
        for raw_alias, raw_properties in raw.items():
            alias = _text(raw_alias)
            if not alias:
                continue
            normalized[alias] = _clean_return_properties(raw_properties)
        return normalized
    return _clean_return_properties(raw)


def _clean_return_properties(raw: Any) -> list[str]:
    if raw in (None, ""):
        return []
    if isinstance(raw, str):
        value = raw.strip()
        return [value] if value else []
    if not isinstance(raw, (list, tuple)):
        return []
    return sanitize_return_properties([str(item).strip() for item in raw if str(item or "").strip()])


def _pattern_return_properties(
    *,
    alias: str,
    step_return_properties: list[str] | None,
    top_level_return_properties: Any | None,
    properties: Mapping[str, Any],
) -> list[str]:
    if isinstance(top_level_return_properties, Mapping):
        if alias in top_level_return_properties:
            return list(top_level_return_properties.get(alias) or [])
    elif isinstance(top_level_return_properties, list):
        return list(top_level_return_properties)
    if step_return_properties is not None:
        return list(step_return_properties)
    return _default_pattern_return_properties(properties)


def _default_pattern_return_properties(properties: Mapping[str, Any]) -> list[str]:
    selected: list[str] = []
    seen: set[str] = set()
    for raw_key in properties.keys():
        key = str(raw_key or "").strip()
        if not key or key in seen:
            continue
        lower = key.lower()
        if key in _PATTERN_COMPACT_EXACT_KEYS or any(keyword in lower for keyword in _PATTERN_COMPACT_KEYWORDS):
            selected.append(key)
            seen.add(key)
    return sanitize_return_properties(selected)


def _iter_neighbors(snapshot: _GraphSnapshot, node_id: str, directed: bool) -> list[tuple[_EdgeRecord, str]]:
    result: list[tuple[_EdgeRecord, str]] = []
    if directed:
        for edge in snapshot.out_edges.get(node_id, []):
            result.append((edge, edge.target_entity_instance_id))
        return result
    seen_edges: set[str] = set()
    for edge in snapshot.out_edges.get(node_id, []):
        if edge.relation_instance_id in seen_edges:
            continue
        seen_edges.add(edge.relation_instance_id)
        result.append((edge, edge.target_entity_instance_id))
    for edge in snapshot.in_edges.get(node_id, []):
        if edge.relation_instance_id in seen_edges:
            continue
        seen_edges.add(edge.relation_instance_id)
        result.append((edge, edge.source_entity_instance_id))
    return result


def _match_filter_dict(properties: Mapping[str, Any], filter_dict: Mapping[str, Any] | None) -> bool:
    if not filter_dict:
        return True
    if not isinstance(filter_dict, Mapping):
        raise GraphBackendUnsupportedError("filter_dict 必须是对象")
    for raw_prop, expression in filter_dict.items():
        prop = safe_prop(raw_prop)
        if not _match_expression(properties.get(prop), expression, "filter_dict.{}".format(prop)):
            return False
    return True


def _match_expression(value: Any, expression: Any, field_name: str) -> bool:
    if expression is None:
        return value is None
    if isinstance(expression, bool):
        return value == expression
    if isinstance(expression, (int, float)) and not isinstance(expression, bool):
        return value == expression
    if isinstance(expression, Mapping):
        return _match_structured_expression(value, expression, field_name)
    if isinstance(expression, Sequence) and not isinstance(expression, (str, bytes)):
        return value in [_ensure_scalar(item, field_name) for item in expression]
    if isinstance(expression, str):
        return _match_text_expression(value, expression, field_name)
    raise GraphBackendUnsupportedError("{} 的值类型不支持: {}".format(field_name, type(expression).__name__))


def _match_structured_expression(value: Any, expression: Mapping[str, Any], field_name: str) -> bool:
    if not expression:
        raise GraphBackendUnsupportedError("{} 不能为空对象".format(field_name))
    for raw_op, operand in expression.items():
        op = normalize_structured_operator(raw_op)
        if op == "eq" and value != _ensure_scalar(operand, "{}.eq".format(field_name)):
            return False
        if op == "ne" and value == _ensure_scalar(operand, "{}.ne".format(field_name)):
            return False
        if op == "gt" and not _compare(value, _ensure_scalar(operand, "{}.gt".format(field_name)), ">"):
            return False
        if op == "gte" and not _compare(value, _ensure_scalar(operand, "{}.gte".format(field_name)), ">="):
            return False
        if op == "lt" and not _compare(value, _ensure_scalar(operand, "{}.lt".format(field_name)), "<"):
            return False
        if op == "lte" and not _compare(value, _ensure_scalar(operand, "{}.lte".format(field_name)), "<="):
            return False
        if op == "contains" and str(_ensure_scalar(operand, "{}.contains".format(field_name))) not in str(value or ""):
            return False
        if op == "starts_with" and not str(value or "").startswith(str(_ensure_scalar(operand, "{}.starts_with".format(field_name)))):
            return False
        if op == "ends_with" and not str(value or "").endswith(str(_ensure_scalar(operand, "{}.ends_with".format(field_name)))):
            return False
        if op == "in" and value not in _validate_sequence(operand, "{}.in".format(field_name)):
            return False
        if op == "not_in" and value in _validate_sequence(operand, "{}.not_in".format(field_name)):
            return False
        if op == "is_null":
            if not isinstance(operand, bool):
                raise GraphBackendUnsupportedError("{}.is_null 必须是布尔值".format(field_name))
            if operand and value is not None:
                return False
            if not operand and value is None:
                return False
        if op not in {"eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with", "ends_with", "in", "not_in", "is_null"}:
            raise GraphBackendUnsupportedError("{} 包含不支持的操作符: {}".format(field_name, raw_op))
    return True


def _match_text_expression(value: Any, expression: str, field_name: str) -> bool:
    text = expression.strip()
    if not text:
        raise GraphBackendUnsupportedError("{} 不能为空字符串".format(field_name))
    parts = [part.strip() for part in re.split(r"\s+OR\s+", text, flags=re.IGNORECASE)]
    if any(not part for part in parts):
        raise GraphBackendUnsupportedError("{} 包含空条件".format(field_name))
    return any(_match_text_part(value, part) for part in parts)


def _match_text_part(value: Any, part: str) -> bool:
    compare_match = re.match(r"^(>=|<=|>|<|=)\s*(.+)$", part)
    if compare_match:
        operator, token = compare_match.group(1), compare_match.group(2).strip()
        target = _parse_scalar(token)
        if target is None:
            return operator == "=" and value is None
        return _compare(value, target, operator)
    contains_match = re.match(r"^CONTAINS\s+['\"]([^'\"]*)['\"]$", part, flags=re.IGNORECASE)
    if contains_match:
        return contains_match.group(1) in str(value or "")
    starts_ends_match = re.match(r"^(STARTS\s+WITH|ENDS\s+WITH)\s+['\"]([^'\"]*)['\"]$", part, flags=re.IGNORECASE)
    if starts_ends_match:
        target = starts_ends_match.group(2)
        if "STARTS" in starts_ends_match.group(1).upper():
            return str(value or "").startswith(target)
        return str(value or "").endswith(target)
    fallback = _parse_scalar(part)
    if fallback is None:
        return value is None
    return value == fallback


def _compare(left: Any, right: Any, operator: str) -> bool:
    if left is None or right is None:
        return False
    try:
        if operator == ">":
            return left > right
        if operator == ">=":
            return left >= right
        if operator == "<":
            return left < right
        if operator == "<=":
            return left <= right
        if operator == "=":
            return left == right
    except Exception:
        return False
    return False


def _ensure_scalar(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise GraphBackendUnsupportedError("{} 仅支持标量值".format(field_name))


def _validate_sequence(value: Any, field_name: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise GraphBackendUnsupportedError("{} 必须是数组".format(field_name))
    result = list(value)
    if not result:
        raise GraphBackendUnsupportedError("{} 不能为空数组".format(field_name))
    return [_ensure_scalar(item, field_name) for item in result]


def _parse_scalar(token: str) -> Any:
    text = str(token or "").strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1]
    lowered = text.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    try:
        return int(text)
    except ValueError:
        pass
    try:
        return float(text)
    except ValueError:
        return text


def _matches_scope(properties: Mapping[str, Any], scope: GraphScope) -> bool:
    if not scope.is_enabled():
        return False
    if _text(properties.get(scope.workspace_property)) != scope.workspace_value:
        return False
    if scope.ontology_property and _text(properties.get(scope.ontology_property)) != scope.ontology_value:
        return False
    return True


def _is_logic_element(properties: Mapping[str, Any]) -> bool:
    class_kind = _text(properties.get("class_kind")).lower()
    source_table = _text(properties.get("source_table")).lower()
    return class_kind in {"logic", "rule"} or source_table == "unstructured_data_logic_core"


def _matches_edge_class(edge: _EdgeRecord, element_class: str) -> bool:
    return element_class in {edge.relation_physical_type, edge.relation_type, edge.relation_type_id}


def _normalize_direction(value: Any) -> str:
    direction = _text(value) or "->"
    if direction not in _DIRECTION_VALUES:
        return "--"
    if direction in {"→", "->"}:
        return "->"
    if direction in {"←", "<-"}:
        return "<-"
    return "--"


def _node_alias(index: int) -> str:
    return chr(ord("a") + index) if 0 <= index < 26 else "n{}".format(index)


def _json_object(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    if not isinstance(value, str):
        return {}
    try:
        payload = json.loads(value.strip())
    except Exception:
        return {}
    return dict(payload) if isinstance(payload, Mapping) else {}


def _normalize_text_list(value: list[str] | None = None) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for item in value or []:
        text = _text(item)
        if not text or text in seen:
            continue
        seen.add(text)
        result.append(text)
    return result


def _normalize_logic_blocks(value: Any) -> list[dict[str, str]]:
    raw_items: Any = value
    if isinstance(value, str):
        raw_text = value.strip()
        if not raw_text:
            return []
        try:
            raw_items = json.loads(raw_text)
        except Exception:
            return []
    if isinstance(raw_items, Mapping):
        raw_items = [raw_items]
    if not isinstance(raw_items, list):
        return []

    result: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in raw_items:
        if not isinstance(item, Mapping):
            continue
        title = _text(item.get("title") or item.get("logic_title"))
        content = _text(item.get("content") or item.get("logic_content"))
        if not title or not content:
            continue
        dedup_key = (title.casefold(), content.casefold())
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        result.append({"logic_title": title, "logic_content": content})
    return result


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _infer_property_type(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, bool):
        return "BOOLEAN"
    if isinstance(value, int) and not isinstance(value, bool):
        return "INTEGER"
    if isinstance(value, float):
        return "FLOAT"
    if isinstance(value, str):
        return "STRING"
    if isinstance(value, list):
        return "LIST"
    if isinstance(value, Mapping):
        return "MAP"
    return type(value).__name__.upper()


def _merge_schema_property(bucket: dict[str, str], prop_name: str, type_name: str) -> None:
    if not prop_name or prop_name in bucket and bucket[prop_name] == type_name:
        return
    current = bucket.get(prop_name)
    if not current:
        bucket[prop_name] = type_name
        return
    bucket[prop_name] = "|".join(sorted(set(current.split("|")) | {type_name}))


def _sortable_value(value: Any) -> tuple[int, str, Any]:
    if value is None:
        return (1, "none", "")
    if isinstance(value, bool):
        return (0, "bool", int(value))
    if isinstance(value, (int, float)):
        return (0, "num", float(value))
    return (0, "text", str(value))


def _pick_business_label(labels: list[str], fallback: Any) -> str:
    cleaned = sanitize_labels(labels)
    return cleaned[0] if cleaned else _text(fallback)


def _schema_excluded_keys(scope: GraphScope) -> set[str]:
    return set(_SCHEMA_EXCLUDED_KEYS) | set(_hidden_properties(scope))


def _hidden_properties(scope: GraphScope) -> list[str]:
    dynamic = [
        scope.workspace_property,
        scope.ontology_property,
    ]
    return [
        item
        for item in [*dynamic, *_HUGEG_INTERNAL_KEYS]
        if _text(item)
    ]
