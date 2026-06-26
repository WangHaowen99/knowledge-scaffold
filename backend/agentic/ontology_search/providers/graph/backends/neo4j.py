"""Neo4J graph backend for graph business data search."""

from __future__ import annotations

from collections.abc import Mapping
import re
from typing import Any

from .base import GraphBackendError, GraphBackendTimeoutError, GraphBackendUnsupportedError
from ..filter_expr import build_filter_where, cypher_prop_ref, safe_prop
from ..models import GraphBackendResult, GraphScope
from ..scope import (
    quote_graph_identifier,
    sanitize_labels,
    sanitize_properties,
    sanitize_return_properties,
)


_ENTITY_TYPE_ID_TOKEN_RE = re.compile(r"^[eE]\d+$")
_RELATION_TYPE_ID_TOKEN_RE = re.compile(r"^[rR]\d+$")
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


class Neo4jGraphBackend:
    """Read-only Neo4J backend used inside GraphSearchRuntime."""

    def __init__(self, provider_config: Mapping[str, Any]) -> None:
        self.uri = str(provider_config.get("uri") or "").strip()
        self.database = str(provider_config.get("database") or "neo4j").strip() or "neo4j"
        self.username = str(provider_config.get("username") or "").strip()
        self.password = str(provider_config.get("password") or "")
        self.timeout = int(provider_config.get("timeout_seconds") or 60)
        self._driver = None

    def close(self) -> None:
        if self._driver is not None:
            self._driver.close()
            self._driver = None

    def verify_connectivity(self) -> None:
        driver = self._get_driver()
        verify = getattr(driver, "verify_connectivity", None)
        if callable(verify):
            verify()
            return
        with driver.session(database=self.database) as session:
            session.run("RETURN 1 AS ok").single()

    def get_nodes_schema(
        self,
        *,
        scope: GraphScope,
        element_types: list[str] | None = None,
    ) -> dict:
        params: dict[str, Any] = {
            "excluded_keys": sorted(_schema_excluded_keys(scope)),
            "excluded_labels": ["D2O_NODE"],
        }
        where_parts = _build_scope_where("n", scope, params)
        query = (
            "MATCH (n) WHERE {where} "
            "UNWIND labels(n) AS label "
            "WITH n, label "
            "UNWIND keys(n) AS prop "
            "WITH label, prop, replace(valueType(n[prop]), ' NOT NULL', '') AS prop_type "
            "WHERE NOT label IN $excluded_labels AND NOT prop IN $excluded_keys "
            "WITH label, collect(DISTINCT {{name: prop, type: prop_type}}) AS prop_types "
            "RETURN label, prop_types ORDER BY label"
        ).format(where=" AND ".join(where_parts))
        try:
            records = self._run(query, params)
        except Exception as exc:
            _raise_backend_error("Neo4J get_nodes_schema failed", exc)

        wanted = {str(item).strip() for item in (element_types or []) if str(item).strip()}
        schema: dict[str, dict[str, str]] = {}
        for record in records:
            label = str(_record_get(record, "label") or "").strip()
            if not label or label == "D2O_NODE":
                continue
            if wanted and label not in wanted:
                continue
            props: dict[str, str] = {}
            for item in _record_get(record, "prop_types") or []:
                prop_name = str(_mapping_get(item, "name") or "").strip()
                prop_type = str(_mapping_get(item, "type") or "").strip()
                if prop_name and prop_type:
                    props[prop_name] = prop_type
            schema[label] = props
        labels = sorted(schema)
        return {"labels": labels, "schema": schema, "count": len(labels)}

    def get_edges_schema(
        self,
        *,
        scope: GraphScope,
        relation_types: list[str] | None = None,
    ) -> dict:
        params: dict[str, Any] = {"excluded_keys": sorted(_schema_excluded_keys(scope))}
        where_parts = _build_edge_scope_where("s", "r", "t", scope, params)
        schema_query = (
            "MATCH (s)-[r]->(t) WHERE {where} "
            "WITH r, type(r) AS rel_type "
            "UNWIND keys(r) AS prop "
            "WITH rel_type, prop, replace(valueType(r[prop]), ' NOT NULL', '') AS prop_type "
            "WHERE NOT prop IN $excluded_keys "
            "WITH rel_type, collect(DISTINCT {{name: prop, type: prop_type}}) AS prop_types "
            "RETURN rel_type, prop_types ORDER BY rel_type"
        ).format(where=" AND ".join(where_parts))
        try:
            schema_records = self._run(schema_query, params)
        except Exception as exc:
            _raise_backend_error("Neo4J get_edges_schema failed", exc)

        physical_schema: dict[str, dict[str, str]] = {}
        for record in schema_records:
            rel_type = str(_record_get(record, "rel_type") or "").strip()
            if not rel_type:
                continue
            props: dict[str, str] = {}
            for item in _record_get(record, "prop_types") or []:
                prop_name = str(_mapping_get(item, "name") or "").strip()
                prop_type = str(_mapping_get(item, "type") or "").strip()
                if prop_name and prop_type:
                    props[prop_name] = prop_type
            physical_schema[rel_type] = props

        triple_params: dict[str, Any] = {}
        triple_where = _build_edge_scope_where("s", "r", "t", scope, triple_params)
        triple_query = (
            "MATCH (s)-[r]->(t) WHERE {where} "
            "RETURN "
            "coalesce(s.entity_id, '') AS source_entity_type_id, "
            "coalesce(s.entity_name, '') AS source_entity_name, "
            "labels(s) AS source_labels, "
            "coalesce(r.relation_id, '') AS relation_type_id, "
            "coalesce(r.relation_type, type(r)) AS relation_type, "
            "coalesce(r.relation_name, '') AS relation_name, "
            "coalesce(t.entity_id, '') AS target_entity_type_id, "
            "coalesce(t.entity_name, '') AS target_entity_name, "
            "labels(t) AS target_labels, "
            "count(*) AS edge_count "
            "ORDER BY edge_count DESC, relation_type ASC"
        ).format(where=" AND ".join(triple_where))
        try:
            triple_records = self._run(triple_query, triple_params)
        except Exception as exc:
            _raise_backend_error("Neo4J get_edges_schema triples failed", exc)

        wanted = {str(item).strip() for item in (relation_types or []) if str(item).strip()}
        relation_triples: list[dict[str, Any]] = []
        relationship_types: set[str] = set(physical_schema)
        for record in triple_records:
            relation_type = str(_record_get(record, "relation_type") or "").strip()
            if wanted and relation_type not in wanted:
                continue
            if relation_type:
                relationship_types.add(relation_type)
            relation_triples.append({
                "source_entity_type_id": _record_get(record, "source_entity_type_id") or "",
                "source_entity_name": _record_get(record, "source_entity_name") or "",
                "source_label": _pick_business_label(_record_get(record, "source_labels")),
                "relation_type_id": _record_get(record, "relation_type_id") or "",
                "relation_type": relation_type,
                "relation_name": _record_get(record, "relation_name") or "",
                "target_entity_type_id": _record_get(record, "target_entity_type_id") or "",
                "target_entity_name": _record_get(record, "target_entity_name") or "",
                "target_label": _pick_business_label(_record_get(record, "target_labels")),
                "edge_count": int(_record_get(record, "edge_count") or 0),
            })

        return {
            "relationship_types": sorted(relationship_types),
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
        elem_type = str(element_type or "").strip().upper()
        elem_class = str(element_class or "").strip()
        if not elem_class:
            raise GraphBackendUnsupportedError("element_class 不能为空")
        if elem_type not in {"NODE", "EDGE"}:
            raise GraphBackendUnsupportedError("Neo4J 仅支持 NODE/EDGE property_filter")

        params: dict[str, Any] = {
            "elem_class": elem_class,
            "limit": max(1, int(limit or 100)),
            "offset": max(0, int(offset or 0)),
        }
        where_parts = []
        if elem_type == "NODE":
            where_parts.extend(_build_scope_where("e", scope, params))
            where_parts.extend(_build_exclude_logic_where("e"))
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = (
                "MATCH (e) WHERE {class_where} AND {where} "
                "RETURN e SKIP $offset LIMIT $limit"
            ).format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
            )
        else:
            where_parts.extend(_build_edge_scope_where("a", "e", "b", scope, params))
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = (
                "MATCH (a)-[e]->(b) WHERE {class_where} AND {where} "
                "RETURN e, a, b SKIP $offset LIMIT $limit"
            ).format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
            )

        records = []
        try:
            hidden_properties = _scope_hidden_properties(scope)
            for record in self._run(query, params):
                if elem_type == "NODE":
                    records.append(_normalize_node(
                        _record_get(record, "e"),
                        return_properties=return_properties,
                        hidden_properties=hidden_properties,
                    ))
                else:
                    records.append(_normalize_relationship(
                        _record_get(record, "e"),
                        start_node=_record_get(record, "a"),
                        end_node=_record_get(record, "b"),
                        return_properties=return_properties,
                        hidden_properties=hidden_properties,
                    ))
        except Exception as exc:
            _raise_backend_error("Neo4J property_filter failed", exc)
        return GraphBackendResult(records=records, truncated=len(records) >= params["limit"])

    def count_search(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        elem_type = str(element_type or "").strip().upper()
        elem_class = str(element_class or "").strip()
        if not elem_class:
            raise GraphBackendUnsupportedError("element_class 不能为空")
        if elem_type not in {"NODE", "EDGE"}:
            raise GraphBackendUnsupportedError("Neo4J 仅支持 NODE/EDGE count_search")
        params: dict[str, Any] = {"elem_class": elem_class}
        if elem_type == "NODE":
            where_parts = _build_scope_where("e", scope, params)
            where_parts.extend(_build_exclude_logic_where("e"))
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = "MATCH (e) WHERE {class_where} AND {where} RETURN count(e) AS total".format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
            )
        else:
            where_parts = _build_edge_scope_where("a", "e", "b", scope, params)
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = "MATCH (a)-[e]->(b) WHERE {class_where} AND {where} RETURN count(e) AS total".format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
            )
        try:
            records = self._run(query, params)
            record = records[0] if records else None
            total = int(_record_get(record, "total") or _record_get(record, "c") or 0)
        except Exception as exc:
            _raise_backend_error("Neo4J count_search failed", exc)
        return GraphBackendResult(records=[], total=total, truncated=False)

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
        elem_type = str(element_type or "").strip().upper()
        elem_class = str(element_class or "").strip()
        agg_upper = str(agg or "").strip().upper()
        if not elem_class:
            raise GraphBackendUnsupportedError("element_class 不能为空")
        if elem_type not in {"NODE", "EDGE"}:
            raise GraphBackendUnsupportedError("Neo4J 仅支持 NODE/EDGE aggregate_search")
        if agg_upper not in {"SUM", "AVG", "MIN", "MAX", "COUNT"}:
            raise GraphBackendUnsupportedError("不支持的聚合函数: {}".format(agg or "<empty>"))

        target = str(target_property or "").strip()
        if agg_upper != "COUNT" and not target:
            raise GraphBackendUnsupportedError("target_property 不能为空")
        target_ref = cypher_prop_ref("e", safe_prop(target)) if target else "e"
        expr = "count(e)" if agg_upper == "COUNT" and not target else "{}({})".format(agg_upper, target_ref)

        params: dict[str, Any] = {"elem_class": elem_class}
        if elem_type == "NODE":
            where_parts = _build_scope_where("e", scope, params)
            where_parts.extend(_build_exclude_logic_where("e"))
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = "MATCH (e) WHERE {class_where} AND {where} RETURN {expr} AS value".format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
                expr=expr,
            )
        else:
            where_parts = _build_edge_scope_where("a", "e", "b", scope, params)
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = "MATCH (a)-[e]->(b) WHERE {class_where} AND {where} RETURN {expr} AS value".format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
                expr=expr,
            )
        try:
            records = self._run(query, params)
            record = records[0] if records else None
            value = _record_get(record, "value")
        except Exception as exc:
            _raise_backend_error("Neo4J aggregate_search failed", exc)
        payload = {
            "value": value,
            "agg": agg_upper,
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
        elem_type = str(element_type or "").strip().upper()
        elem_class = str(element_class or "").strip()
        if not elem_class:
            raise GraphBackendUnsupportedError("element_class 不能为空")
        if elem_type not in {"NODE", "EDGE"}:
            raise GraphBackendUnsupportedError("Neo4J 仅支持 NODE/EDGE sorted_search")
        safe_sort_props = [safe_prop(prop) for prop in (sort_by or []) if str(prop or "").strip()]
        limit_value = max(1, int(limit or 50))
        offset_value = max(0, int(offset or 0))
        params: dict[str, Any] = {
            "elem_class": elem_class,
            "limit": limit_value,
            "offset": offset_value,
        }
        order_clause = ""
        if safe_sort_props:
            direction = "ASC" if ascending else "DESC"
            order_parts = ["{} {}".format(cypher_prop_ref("e", prop), direction) for prop in safe_sort_props]
            order_clause = " ORDER BY " + ", ".join(order_parts)

        if elem_type == "NODE":
            where_parts = _build_scope_where("e", scope, params)
            where_parts.extend(_build_exclude_logic_where("e"))
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = (
                "MATCH (e) WHERE {class_where} AND {where} "
                "RETURN e{order_clause} SKIP $offset LIMIT $limit"
            ).format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
                order_clause=order_clause,
            )
        else:
            where_parts = _build_edge_scope_where("a", "e", "b", scope, params)
            where_parts.extend(build_filter_where(filter_dict or {}, "e", params, "fp"))
            query = (
                "MATCH (a)-[e]->(b) WHERE {class_where} AND {where} "
                "RETURN e, a, b{order_clause} SKIP $offset LIMIT $limit"
            ).format(
                class_where=_build_element_class_match("e", elem_type),
                where=" AND ".join(where_parts),
                order_clause=order_clause,
            )
        records = []
        try:
            hidden_properties = _scope_hidden_properties(scope)
            for record in self._run(query, params):
                if elem_type == "NODE":
                    records.append(_normalize_node(
                        _record_get(record, "e"),
                        return_properties=return_properties,
                        hidden_properties=hidden_properties,
                    ))
                else:
                    records.append(_normalize_relationship(
                        _record_get(record, "e"),
                        start_node=_record_get(record, "a"),
                        end_node=_record_get(record, "b"),
                        return_properties=return_properties,
                        hidden_properties=hidden_properties,
                    ))
        except Exception as exc:
            _raise_backend_error("Neo4J sorted_search failed", exc)
        return GraphBackendResult(records=records, total=len(records), truncated=len(records) >= limit_value)

    def pattern_search(
        self,
        *,
        path_pattern: list[Any],
        return_vars: list[str] | None,
        limit: int,
        scope: GraphScope,
        return_properties: Any | None = None,
    ) -> GraphBackendResult:
        segments = _normalize_path_segments(path_pattern)
        if not segments:
            raise GraphBackendUnsupportedError("pattern_search 缺少 path_pattern")
        query, params = _build_path_query(segments, scope, max(1, int(limit or 100)))
        records: list[dict[str, Any]] = []
        try:
            hidden_properties = _scope_hidden_properties(scope)
            selected_vars = _resolve_path_return_vars(segments, return_vars)
            selected_var_set = set(selected_vars)
            node_aliases, relationship_aliases = _path_alias_maps(segments)
            segment_return_properties = _path_return_property_map(segments)
            top_level_return_properties = _normalize_pattern_return_properties(return_properties)
            for record in self._run(query, params):
                raw_nodes = list(_record_get(record, "nodes") or [])
                raw_relationships = list(_record_get(record, "relationships") or [])
                node_values = {
                    alias: node
                    for alias, node in zip(node_aliases, raw_nodes)
                    if alias in selected_var_set
                }
                relationship_values = {
                    alias: rel
                    for alias, rel in zip(relationship_aliases, raw_relationships)
                    if alias in selected_var_set
                }
                selected: dict[str, Any] = {}
                for alias in selected_vars:
                    if alias in node_values:
                        selected[alias] = _normalize_pattern_node(
                            node_values[alias],
                            alias=alias,
                            segment_return_properties=segment_return_properties,
                            top_level_return_properties=top_level_return_properties,
                            hidden_properties=hidden_properties,
                        )
                    elif alias in relationship_values:
                        selected[alias] = _normalize_pattern_relationship(
                            relationship_values[alias],
                            alias=alias,
                            segment_return_properties=segment_return_properties,
                            top_level_return_properties=top_level_return_properties,
                            hidden_properties=hidden_properties,
                        )
                records.append({
                    "nodes": [
                        _normalize_pattern_node(
                            node,
                            alias=alias,
                            segment_return_properties=segment_return_properties,
                            top_level_return_properties=top_level_return_properties,
                            hidden_properties=hidden_properties,
                        )
                        for alias, node in zip(node_aliases, raw_nodes)
                        if alias in selected_var_set
                    ],
                    "relationships": [
                        _normalize_pattern_relationship(
                            rel,
                            alias=alias,
                            segment_return_properties=segment_return_properties,
                            top_level_return_properties=top_level_return_properties,
                            hidden_properties=hidden_properties,
                        )
                        for alias, rel in zip(relationship_aliases, raw_relationships)
                        if alias in selected_var_set
                    ],
                    "return_vars": selected_vars,
                    "selected": selected,
                })
        except Exception as exc:
            _raise_backend_error("Neo4J pattern_search failed", exc)
        return GraphBackendResult(records=records, truncated=len(records) >= max(1, int(limit or 100)))

    def get_element_info(
        self,
        *,
        element_instance_id: str,
        element_class: str = "",
        element_type: str = "",
        scope: GraphScope,
    ) -> dict:
        normalized_element_id = str(element_instance_id or "").strip()
        if not normalized_element_id:
            raise GraphBackendUnsupportedError("element_instance_id 不能为空")
        normalized_type = str(element_type or "").strip().upper()
        if normalized_type in {"", "NODE"}:
            node = self._get_node_info(normalized_element_id, scope, element_class=element_class)
            if node.get("found"):
                return node
        if normalized_type in {"", "EDGE"}:
            relationship = self._get_relationship_info(normalized_element_id, scope, element_class=element_class)
            if relationship.get("found"):
                return relationship
        return {"found": False, "element_id": normalized_element_id, "element_instance_id": normalized_element_id}

    def _get_driver(self):
        if self._driver is not None:
            return self._driver
        if not self.uri:
            raise GraphBackendError("Neo4J URI 未配置")
        try:
            from neo4j import GraphDatabase
        except ImportError as exc:
            raise GraphBackendError("Neo4J driver 未安装") from exc
        self._driver = GraphDatabase.driver(
            self.uri,
            auth=(self.username, self.password),
            connection_timeout=self.timeout,
        )
        return self._driver

    def _run(self, query: str, params: Mapping[str, Any]):
        driver = self._get_driver()
        with driver.session(database=self.database) as session:
            query_object = _query_with_timeout(query, self.timeout)
            return list(session.run(query_object, dict(params)))

    def _get_node_info(self, element_id: str, scope: GraphScope, *, element_class: str = "") -> dict:
        params: dict[str, Any] = {"element_id": element_id}
        where_parts = ["(elementId(n) = $element_id OR toString(n.id) = $element_id)"]
        where_parts.extend(_build_scope_where("n", scope, params))
        where_parts.extend(_build_exclude_logic_where("n"))
        if str(element_class or "").strip():
            params["elem_class"] = str(element_class).strip()
            where_parts.append(_build_element_class_match("n", "NODE"))
        query = "MATCH (n) WHERE {where} RETURN n LIMIT 1".format(where=" AND ".join(where_parts))
        try:
            records = self._run(query, params)
        except Exception as exc:
            _raise_backend_error("Neo4J get_element_info node failed", exc)
        record = records[0] if records else None
        node = _record_get(record, "n")
        if node is None:
            return {"found": False, "element_id": element_id, "element_instance_id": element_id}
        normalized = _normalize_node(node, hidden_properties=_scope_hidden_properties(scope))
        normalized.update({"found": True, "kind": "node", "element_type": "NODE"})
        return normalized

    def _get_relationship_info(self, element_id: str, scope: GraphScope, *, element_class: str = "") -> dict:
        params: dict[str, Any] = {"element_id": element_id}
        where_parts = ["(elementId(r) = $element_id OR toString(r.id) = $element_id)"]
        where_parts.extend(_build_edge_scope_where("a", "r", "b", scope, params))
        if str(element_class or "").strip():
            params["elem_class"] = str(element_class).strip()
            where_parts.append(_build_element_class_match("r", "EDGE"))
        query = "MATCH (a)-[r]->(b) WHERE {where} RETURN r, a, b LIMIT 1".format(where=" AND ".join(where_parts))
        try:
            records = self._run(query, params)
        except Exception as exc:
            _raise_backend_error("Neo4J get_element_info relationship failed", exc)
        record = records[0] if records else None
        rel = _record_get(record, "r")
        if rel is None:
            return {"found": False, "element_id": element_id, "element_instance_id": element_id}
        normalized = _normalize_relationship(
            rel,
            start_node=_record_get(record, "a"),
            end_node=_record_get(record, "b"),
            hidden_properties=_scope_hidden_properties(scope),
        )
        normalized.update({"found": True, "kind": "relationship", "element_type": "EDGE"})
        return normalized


def _build_element_class_match(alias: str, element_type: str, param_name: str = "elem_class") -> str:
    elem_type = str(element_type or "").strip().upper()
    if elem_type == "NODE":
        return "${} IN labels({})".format(param_name, alias)
    if elem_type == "EDGE":
        ref = "${}".format(param_name)
        return (
            "(type({alias}) = {ref} OR coalesce({alias}.relation_type, '') = {ref} "
            "OR coalesce({alias}.relation_id, '') = {ref})"
        ).format(alias=alias, ref=ref)
    raise ValueError("不支持的 element_type: {!r}".format(elem_type))


def _build_scope_where(
    alias: str,
    scope: GraphScope,
    params: dict[str, Any],
    *,
    prefix: str = "scope",
) -> list[str]:
    if not scope.is_enabled():
        raise GraphBackendError("workspace scope 未启用")
    parts = []
    workspace_param = "{}_workspace_value".format(prefix)
    params[workspace_param] = scope.workspace_value
    parts.append("{}.{} = ${}".format(
        alias,
        quote_graph_identifier(scope.workspace_property, "workspace_property"),
        workspace_param,
    ))
    if scope.ontology_property:
        ontology_param = "{}_ontology_value".format(prefix)
        params[ontology_param] = scope.ontology_value
        parts.append("{}.{} = ${}".format(
            alias,
            quote_graph_identifier(scope.ontology_property, "ontology_property"),
            ontology_param,
        ))
    return parts


def _build_edge_scope_where(
    source_alias: str,
    relationship_alias: str,
    target_alias: str,
    scope: GraphScope,
    params: dict[str, Any],
) -> list[str]:
    parts = []
    parts.extend(_build_scope_where(source_alias, scope, params, prefix="source_scope"))
    parts.extend(_build_scope_where(relationship_alias, scope, params, prefix="rel_scope"))
    parts.extend(_build_scope_where(target_alias, scope, params, prefix="target_scope"))
    parts.extend(_build_exclude_logic_where(source_alias))
    parts.extend(_build_exclude_logic_where(target_alias))
    return parts


def _build_exclude_logic_where(alias: str) -> list[str]:
    return [
        "("
        "toLower(trim(coalesce({}.class_kind, ''))) <> 'logic' "
        "AND toLower(trim(coalesce({}.source_table, ''))) <> 'unstructured_data_logic_core'"
        ")".format(alias, alias)
    ]


def _normalize_path_segments(path_pattern: list[Any] | None) -> list[dict[str, Any]]:
    if not path_pattern:
        return []
    if all(isinstance(item, Mapping) for item in path_pattern):
        return [_normalize_mapping_segment(dict(item)) for item in path_pattern]
    if len(path_pattern) < 3 or len(path_pattern) % 2 == 0:
        raise GraphBackendUnsupportedError("path_pattern 需要是节点-关系-节点的奇数长度数组")

    segments: list[dict[str, Any]] = []
    for index, raw_segment in enumerate(path_pattern):
        if not isinstance(raw_segment, (list, tuple)) or not raw_segment:
            raise GraphBackendUnsupportedError("path_pattern[{}] 必须是非空数组".format(index))
        if index % 2 == 0:
            alias = _node_alias(index // 2)
            selector = raw_segment[0]
            props = _segment_extra_props(raw_segment)
            return_properties = _segment_return_properties(selector) if isinstance(selector, Mapping) else None
            props_return_properties = _segment_return_properties(raw_segment[1]) if len(raw_segment) > 1 and isinstance(raw_segment[1], Mapping) else None
            if props_return_properties is not None:
                return_properties = props_return_properties
            if isinstance(selector, str):
                token = selector.strip()
                segment = {"var": alias, "props": props}
                if _ENTITY_TYPE_ID_TOKEN_RE.match(token):
                    segment["entity_type_id"] = token
                else:
                    segment["label"] = token
                if return_properties is not None:
                    segment["return_properties"] = return_properties
                segments.append(segment)
                continue
            if isinstance(selector, Mapping):
                segment = {
                    "var": alias,
                    "label": str(selector.get("label") or selector.get("type") or "").strip(),
                    "entity_type_id": str(selector.get("entity_type_id") or "").strip(),
                    "entity_instance_id": str(selector.get("entity_instance_id") or "").strip(),
                    "entity_name": str(selector.get("entity_name") or "").strip(),
                    "props": {**_segment_props(selector), **props},
                }
                if return_properties is not None:
                    segment["return_properties"] = return_properties
                segments.append(segment)
                continue
            raise GraphBackendUnsupportedError("path_pattern[{}] 节点选择器必须是字符串或对象".format(index))

        alias = "r{}".format((index + 1) // 2)
        selector = raw_segment[0]
        direction = str(raw_segment[1] if len(raw_segment) > 1 else "->").strip() or "->"
        if direction not in {"->", "<-", "--", "→", "←"}:
            raise GraphBackendUnsupportedError("path_pattern[{}] 方向不合法".format(index))
        if direction == "→":
            direction = "->"
        elif direction == "←":
            direction = "<-"
        if isinstance(selector, str):
            token = selector.strip()
            segment = {"var": alias, "direction": direction, "props": _segment_extra_props(raw_segment)}
            if _RELATION_TYPE_ID_TOKEN_RE.match(token):
                segment["relation_type_id"] = token
            elif token:
                segment["relation_type"] = token
            segments.append(segment)
            continue
        if isinstance(selector, Mapping):
            return_properties = _segment_return_properties(selector)
            segment = {
                "var": alias,
                "type": str(selector.get("type") or "").strip(),
                "relation_type": str(selector.get("relation_type") or "").strip(),
                "relation_type_id": str(selector.get("relation_type_id") or "").strip(),
                "relation_name": str(selector.get("relation_name") or "").strip(),
                "direction": direction,
                "props": _segment_props(selector),
            }
            if return_properties is not None:
                segment["return_properties"] = return_properties
            segments.append(segment)
            continue
        raise GraphBackendUnsupportedError("path_pattern[{}] 关系选择器必须是字符串或对象".format(index))
    return segments


def _segment_extra_props(raw_segment: Any) -> dict[str, Any]:
    if not isinstance(raw_segment, (list, tuple)) or len(raw_segment) < 2:
        return {}
    value = raw_segment[1]
    if isinstance(value, Mapping):
        nested = _segment_props(value)
        if nested:
            direct = {
                key: item
                for key, item in value.items()
                if key not in {"props", "filters"} and str(key or "").strip() not in _PATTERN_SEGMENT_META_KEYS
            }
            direct.update(nested)
            return direct
        return {
            key: item
            for key, item in value.items()
            if str(key or "").strip() not in _PATTERN_SEGMENT_META_KEYS
        }
    return {}


def _segment_return_properties(segment: Any) -> list[str] | None:
    if not isinstance(segment, Mapping) or "return_properties" not in segment:
        return None
    return _clean_return_properties(segment.get("return_properties"))


def _normalize_mapping_segment(segment: dict[str, Any]) -> dict[str, Any]:
    if "filters" not in segment:
        return segment
    props = _segment_props(segment)
    normalized = dict(segment)
    normalized["props"] = props
    normalized.pop("filters", None)
    return normalized


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


def _node_alias(index: int) -> str:
    return chr(ord("a") + index) if 0 <= index < 26 else "n{}".format(index)


def _build_path_query(segments: list[dict[str, Any]], scope: GraphScope, limit: int) -> tuple[str, dict[str, Any]]:
    params: dict[str, Any] = {"limit": limit}
    pattern_parts: list[str] = []
    aliases: list[tuple[str, str]] = []
    where_parts: list[str] = []

    for index, segment in enumerate(segments):
        if index % 2 == 0:
            var = str(segment.get("var") or _node_alias(index // 2)).strip()
            label = str(segment.get("label") or "").strip()
            pattern_parts.append("({}{})".format(var, ":" + quote_graph_identifier(label, "path label") if label else ""))
            aliases.append((var, "node"))
            props = _segment_props(segment)
            if segment.get("entity_type_id"):
                props.setdefault("entity_id", segment.get("entity_type_id"))
            if segment.get("entity_name"):
                props.setdefault("entity_name", segment.get("entity_name"))
            where_parts.extend(build_filter_where(props, var, params, "pathprop{}_{}".format(index, var)))
            if segment.get("entity_instance_id"):
                key = "node_instance_{}".format(index)
                params[key] = str(segment.get("entity_instance_id")).strip()
                where_parts.append("(elementId({alias}) = ${key} OR toString({alias}.id) = ${key})".format(alias=var, key=key))
            where_parts.extend(_build_exclude_logic_where(var))
            continue

        var = str(segment.get("var") or "r{}".format((index + 1) // 2)).strip()
        rel_type = str(segment.get("type") or "").strip()
        direction = str(segment.get("direction") or "->").strip()
        rel_body = "[{}{}]".format(var, ":" + quote_graph_identifier(rel_type, "path relationship type") if rel_type else "")
        if direction == "<-":
            pattern_parts.append("<-{}-".format(rel_body))
        elif direction == "--":
            pattern_parts.append("-{}-".format(rel_body))
        else:
            pattern_parts.append("-{}->".format(rel_body))
        aliases.append((var, "edge"))
        props = _segment_props(segment)
        if segment.get("relation_type"):
            props.setdefault("relation_type", segment.get("relation_type"))
        if segment.get("relation_type_id"):
            props.setdefault("relation_id", segment.get("relation_type_id"))
        if segment.get("relation_name"):
            props.setdefault("relation_name", segment.get("relation_name"))
        where_parts.extend(build_filter_where(props, var, params, "pathrel{}_{}".format(index, var)))

    if not pattern_parts or not any(kind == "edge" for _var, kind in aliases):
        raise GraphBackendUnsupportedError("pattern_search 需要 node-edge-node 路径模板")
    for index, (alias, _kind) in enumerate(aliases):
        where_parts.extend(_build_scope_where(alias, scope, params, prefix="path{}_{}".format(index, alias)))

    query = (
        "MATCH p = {pattern} WHERE {where} "
        "RETURN nodes(p) AS nodes, relationships(p) AS relationships LIMIT $limit"
    ).format(pattern="".join(pattern_parts), where=" AND ".join(where_parts))
    return query, params


def _path_alias_maps(segments: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    node_aliases: list[str] = []
    relationship_aliases: list[str] = []
    for index, segment in enumerate(segments):
        if index % 2 == 0:
            node_aliases.append(str(segment.get("var") or _node_alias(index // 2)).strip())
        else:
            relationship_aliases.append(str(segment.get("var") or "r{}".format((index + 1) // 2)).strip())
    return node_aliases, relationship_aliases


def _resolve_path_return_vars(segments: list[dict[str, Any]], return_vars: list[str] | None) -> list[str]:
    node_aliases, relationship_aliases = _path_alias_maps(segments)
    available: list[str] = []
    for index, alias in enumerate(node_aliases):
        if index == 0:
            available.append(alias)
        if index < len(relationship_aliases):
            available.append(relationship_aliases[index])
            available.append(node_aliases[index + 1])
    if not return_vars:
        return available
    selected: list[str] = []
    seen: set[str] = set()
    available_set = set(available)
    for raw_var in return_vars:
        var = str(raw_var or "").strip()
        if not var or var in seen or var not in available_set:
            continue
        selected.append(var)
        seen.add(var)
    return selected or available


def _path_return_property_map(segments: list[dict[str, Any]]) -> dict[str, list[str]]:
    mapping: dict[str, list[str]] = {}
    for index, segment in enumerate(segments):
        alias = str(
            segment.get("var")
            or (_node_alias(index // 2) if index % 2 == 0 else "r{}".format((index + 1) // 2))
        ).strip()
        if not alias or "return_properties" not in segment:
            continue
        mapping[alias] = _clean_return_properties(segment.get("return_properties"))
    return mapping


def _normalize_pattern_return_properties(raw: Any) -> Any | None:
    if raw in (None, ""):
        return None
    if isinstance(raw, Mapping):
        normalized: dict[str, list[str]] = {}
        for raw_alias, raw_properties in raw.items():
            alias = str(raw_alias or "").strip()
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


def _normalize_node(
    node: Any,
    *,
    return_properties: list[str] | None = None,
    hidden_properties: list[str] | None = None,
) -> dict[str, Any]:
    if node is None:
        return {}
    properties = dict(node) if isinstance(node, Mapping) else dict(getattr(node, "_properties", {}) or {})
    labels = getattr(node, "labels", [])
    element_id = getattr(node, "element_id", None)
    if element_id is None and isinstance(node, Mapping):
        element_id = node.get("element_id") or node.get("entity_instance_id")
    return {
        "element_id": element_id,
        "entity_instance_id": element_id,
        "entity_type_id": str(properties.get("entity_id") or ""),
        "labels": sanitize_labels(labels),
        "properties": sanitize_properties(
            properties,
            return_properties=return_properties,
            hidden_properties=hidden_properties,
        ),
    }


def _normalize_relationship(
    rel: Any,
    *,
    start_node: Any | None = None,
    end_node: Any | None = None,
    return_properties: list[str] | None = None,
    hidden_properties: list[str] | None = None,
) -> dict[str, Any]:
    if rel is None:
        return {}
    properties = dict(rel) if isinstance(rel, Mapping) else dict(getattr(rel, "_properties", {}) or {})
    rel_type = getattr(rel, "type", "")
    element_id = getattr(rel, "element_id", None)
    if element_id is None and isinstance(rel, Mapping):
        element_id = rel.get("element_id") or rel.get("relation_instance_id")
    start = start_node if start_node is not None else getattr(rel, "start_node", None)
    end = end_node if end_node is not None else getattr(rel, "end_node", None)
    source_id = getattr(start, "element_id", None) if start is not None else None
    target_id = getattr(end, "element_id", None) if end is not None else None
    return {
        "element_id": element_id,
        "relation_instance_id": element_id,
        "type": rel_type,
        "relation_type": str(properties.get("relation_type") or rel_type),
        "relation_type_id": str(properties.get("relation_id") or ""),
        "source_id": source_id,
        "target_id": target_id,
        "source_entity_instance_id": source_id,
        "target_entity_instance_id": target_id,
        "properties": sanitize_properties(
            properties,
            return_properties=return_properties,
            hidden_properties=hidden_properties,
        ),
    }


def _normalize_pattern_node(
    node: Any,
    *,
    alias: str,
    segment_return_properties: Mapping[str, list[str]],
    top_level_return_properties: Any | None,
    hidden_properties: list[str] | None = None,
) -> dict[str, Any]:
    properties = _element_properties(node)
    return _normalize_node(
        node,
        return_properties=_pattern_return_properties(
            alias=alias,
            segment_return_properties=segment_return_properties,
            top_level_return_properties=top_level_return_properties,
            properties=properties,
        ),
        hidden_properties=hidden_properties,
    )


def _normalize_pattern_relationship(
    rel: Any,
    *,
    alias: str,
    segment_return_properties: Mapping[str, list[str]],
    top_level_return_properties: Any | None,
    hidden_properties: list[str] | None = None,
) -> dict[str, Any]:
    properties = _element_properties(rel)
    return _normalize_relationship(
        rel,
        return_properties=_pattern_return_properties(
            alias=alias,
            segment_return_properties=segment_return_properties,
            top_level_return_properties=top_level_return_properties,
            properties=properties,
        ),
        hidden_properties=hidden_properties,
    )


def _element_properties(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    return dict(value) if isinstance(value, Mapping) else dict(getattr(value, "_properties", {}) or {})


def _pattern_return_properties(
    *,
    alias: str,
    segment_return_properties: Mapping[str, list[str]],
    top_level_return_properties: Any | None,
    properties: Mapping[str, Any],
) -> list[str]:
    if isinstance(top_level_return_properties, Mapping):
        if alias in top_level_return_properties:
            return list(top_level_return_properties.get(alias) or [])
    elif isinstance(top_level_return_properties, list):
        return list(top_level_return_properties)
    if alias in segment_return_properties:
        return list(segment_return_properties.get(alias) or [])
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


def _scope_hidden_properties(scope: GraphScope) -> list[str]:
    return [
        value
        for value in (scope.workspace_property, scope.ontology_property)
        if str(value or "").strip()
    ]


def _schema_excluded_keys(scope: GraphScope) -> set[str]:
    return {"id", "user_id", "workspace_name", "scene_name", "logic_blocks", "_d2o_record_key", *set(_scope_hidden_properties(scope))}


def _record_get(record: Any, key: str) -> Any:
    if record is None:
        return None
    if isinstance(record, Mapping):
        return record.get(key)
    try:
        return record[key]
    except Exception:
        return getattr(record, key, None)


def _mapping_get(value: Any, key: str) -> Any:
    if isinstance(value, Mapping):
        return value.get(key)
    return getattr(value, key, None)


def _pick_business_label(labels: Any) -> str:
    cleaned = sanitize_labels(labels)
    return cleaned[0] if cleaned else ""


def _query_with_timeout(query: str, timeout_seconds: int) -> Any:
    try:
        from neo4j import Query
    except Exception:
        return query
    timeout = float(timeout_seconds or 0)
    return Query(query, timeout=timeout) if timeout > 0 else query


def _raise_backend_error(message: str, exc: Exception) -> None:
    if isinstance(exc, GraphBackendError):
        raise exc
    if _is_timeout_exception(exc):
        raise GraphBackendTimeoutError(message) from exc
    raise GraphBackendError(message) from exc


def _is_timeout_exception(exc: Exception) -> bool:
    name = type(exc).__name__.lower()
    text = str(exc).lower()
    code = str(getattr(exc, "code", "") or "").lower()
    return (
        "timeout" in name
        or "timeouterror" in name
        or "timed out" in text
        or "timeout" in text
        or "deadline" in text
        or "timeout" in code
    )
