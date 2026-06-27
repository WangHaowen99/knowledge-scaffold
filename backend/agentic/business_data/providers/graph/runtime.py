"""Runtime orchestration for graph business data search."""

from __future__ import annotations

import json
from typing import Any, Iterator, Mapping

from ...provider_base import BusinessDataSearchEvent
from .agent import GraphAgentError, GraphAgentTimeoutError, GraphAgentUnavailableError, GraphQueryAgent
from .backends.base import GraphBackend, GraphBackendError, GraphBackendUnsupportedError
from .formatting import build_error_result, build_success_result
from .business_context import load_graph_business_context
from .mapping import GraphMappingResolver
from .models import GraphEntityMapping, GraphSearchRuntimeContext
from .scope import GraphScopeError, build_graph_scope
from .trace import GraphProviderTrace
from .tools import schema as schema_tools
from .tools.executor import GraphToolExecutor

_MAX_SCHEMA_LABELS = 80
_MAX_SCHEMA_FIELDS_PER_LABEL = 30
_MAX_RELATION_TRIPLES = 120


class GraphSearchRuntime:
    """Runtime orchestration for LLM-planned graph search."""

    def __init__(
        self,
        *,
        provider: str,
        provider_config: Mapping[str, Any],
        backend: GraphBackend,
        mapping_resolver: GraphMappingResolver | None = None,
        graph_agent: GraphQueryAgent | None = None,
    ) -> None:
        self.provider = str(provider or "").strip().lower()
        self.provider_config = dict(provider_config or {})
        self.backend = backend
        self.mapping_resolver = mapping_resolver
        self.graph_agent = graph_agent

    def search_stream(
        self,
        *,
        query: str,
        entity: str,
    ) -> Iterator[BusinessDataSearchEvent]:
        normalized_query = str(query or "").strip()
        normalized_entity = str(entity or "").strip()

        try:
            context = self._build_context(query=normalized_query, entity=normalized_entity)
        except GraphScopeError as exc:
            yield _final(build_error_result(
                provider=self.provider,
                entity=normalized_entity,
                query=normalized_query,
                error_code="GRAPH_SCOPE_REQUIRED",
                error=str(exc),
                retryable=False,
            ))
            return

        if context.mapping is None:
            yield {"type": "intermediate", "content": "未找到图谱映射，跳过图数据库访问"}
            yield _final(build_error_result(
                provider=self.provider,
                entity=normalized_entity,
                query=normalized_query,
                error_code="GRAPH_MAPPING_NOT_FOUND",
                error="Entity '{}' has no graph mapping.".format(normalized_entity),
                retryable=False,
                warnings=["请在 mappings/object-data-mappings.json 中补充 object data mapping"],
            ))
            return

        yield {
            "type": "intermediate",
            "content": "已解析图谱映射: {} ({})".format(context.mapping.entity, context.mapping.source),
        }

        yield {"type": "intermediate", "content": "调用 GraphQueryAgent 执行内部图检索工具"}
        agent = self.graph_agent or GraphQueryAgent()
        trace = GraphProviderTrace(context)
        tool_executor = GraphToolExecutor(
            backend=self.backend,
            context=context,
            mapping=context.mapping,
            trace=trace,
        )
        try:
            agent_result = agent.run(
                context=context,
                mapping=context.mapping,
                tool_executor=tool_executor,
            )
        except GraphAgentUnavailableError as exc:
            yield _final(build_error_result(
                provider=self.provider,
                entity=context.entity,
                query=context.query,
                error_code=exc.error_code,
                error=str(exc),
                retryable=exc.retryable,
            ))
            return
        except GraphAgentTimeoutError as exc:
            yield _final(build_error_result(
                provider=self.provider,
                entity=context.entity,
                query=context.query,
                error_code=exc.error_code,
                error=str(exc),
                retryable=exc.retryable,
            ))
            return
        except GraphAgentError as exc:
            yield _final(build_error_result(
                provider=self.provider,
                entity=context.entity,
                query=context.query,
                error_code=exc.error_code,
                error=str(exc),
                retryable=exc.retryable,
            ))
            return
        except GraphBackendUnsupportedError as exc:
            yield _final(build_error_result(
                provider=self.provider,
                entity=context.entity,
                query=context.query,
                error_code=exc.error_code,
                error=str(exc),
                retryable=exc.retryable,
            ))
            return
        except GraphBackendError as exc:
            yield _final(build_error_result(
                provider=self.provider,
                entity=context.entity,
                query=context.query,
                error_code=exc.error_code,
                error="图数据库查询失败",
                retryable=exc.retryable,
            ))
            return

        backend_result = agent_result.backend_result
        yield {
            "type": "intermediate",
            "content": "已执行 {}，命中 {} 条".format(
                agent_result.tool_name,
                backend_result.total if backend_result.total is not None else len(backend_result.records or []),
            ),
        }
        yield _final(build_success_result(
            provider=self.provider,
            entity=context.entity,
            query=context.query,
            mapping=context.mapping,
            agent_result=agent_result,
            scope=context.scope,
        ))

    def _build_context(self, *, query: str, entity: str) -> GraphSearchRuntimeContext:
        workspace_id = str(self.provider_config.get("workspace_id") or "").strip()
        ontology_id = str(self.provider_config.get("ontology_id") or "").strip()
        workspace_root = str(self.provider_config.get("_workspace_root") or "").strip()
        scope = build_graph_scope(self.provider_config)
        resolver = self.mapping_resolver or GraphMappingResolver(
            workspace_root=workspace_root,
            ontology_id=ontology_id,
            provider=self.provider,
        )
        mapping = resolver.resolve(entity)
        if mapping is None and self.mapping_resolver is None:
            schema_labels = self._load_node_schema_labels(scope=scope)
            if schema_labels:
                resolver = GraphMappingResolver(
                    workspace_root=workspace_root,
                    ontology_id=ontology_id,
                    provider=self.provider,
                    schema_labels=schema_labels,
                )
                mapping = resolver.resolve(entity)
        context = GraphSearchRuntimeContext(
            provider=self.provider,
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            workspace_root=workspace_root,
            entity=entity,
            query=query,
            timeout_seconds=int(self.provider_config.get("timeout_seconds") or 60),
            max_result_rows=int(self.provider_config.get("max_result_rows") or 100),
            scope=scope,
            mapping=mapping,
            provider_config=dict(self.provider_config),
        )
        if mapping is not None:
            business_context = load_graph_business_context(context=context, mapping=mapping)
            schema_context = self._load_schema_context(mapping=mapping, scope=scope, query=query, entity=entity)
            context = GraphSearchRuntimeContext(
                provider=context.provider,
                workspace_id=context.workspace_id,
                ontology_id=context.ontology_id,
                workspace_root=context.workspace_root,
                entity=context.entity,
                query=context.query,
                timeout_seconds=context.timeout_seconds,
                max_result_rows=context.max_result_rows,
                scope=context.scope,
                mapping=context.mapping,
                provider_config=dict(context.provider_config),
                business_context=business_context,
                schema_context=schema_context,
            )
        return context

    def _load_node_schema_labels(self, *, scope) -> list[str]:
        try:
            schema = schema_tools.get_nodes_schema(self.backend, scope=scope)
        except Exception:
            return []
        labels = schema.get("labels") if isinstance(schema, dict) else None
        if not isinstance(labels, list):
            return []
        return [str(label).strip() for label in labels if str(label).strip()]

    def _load_schema_context(self, *, mapping: GraphEntityMapping, scope, query: str, entity: str) -> dict[str, Any]:
        focused_node_labels = sorted(_mapping_node_labels(mapping))
        focused_relationship_types = sorted(_mapping_relationship_types(mapping))
        path_analysis = _analyze_path_intent(query=query, entity=entity, mapping=mapping)
        expand_for_path_planning = _graph_schema_expand_for_path_planning(self.provider_config)
        node_labels = [] if expand_for_path_planning else focused_node_labels
        relationship_types = [] if expand_for_path_planning else focused_relationship_types
        schema_context: dict[str, Any] = {
            "source": "provider_preload",
            "node_labels": node_labels,
            "relationship_types": relationship_types,
            "focused_node_labels": focused_node_labels,
            "focused_relationship_types": focused_relationship_types,
            "mapping_resource": mapping.label or mapping.edge_type,
            "mapping_fields": dict(mapping.property_map or {}),
            "query_analysis": path_analysis,
            "schema_strategy": "expanded_for_path_planning" if expand_for_path_planning else "focused",
            "nodes_schema": {},
            "edges_schema": {},
            "warnings": [],
        }

        if expand_for_path_planning or node_labels:
            try:
                schema_context["nodes_schema"] = _compact_nodes_schema(
                    schema_tools.get_nodes_schema(
                        self.backend,
                        scope=scope,
                        element_types=node_labels or None,
                    ),
                    focused_node_labels if expand_for_path_planning else node_labels,
                )
            except Exception as exc:
                schema_context["warnings"].append("节点 schema 预取失败: {}".format(type(exc).__name__))

        if expand_for_path_planning or relationship_types:
            try:
                schema_context["edges_schema"] = _compact_edges_schema(
                    schema_tools.get_edges_schema(
                        self.backend,
                        scope=scope,
                        relation_types=relationship_types or None,
                    ),
                    focused_relationship_types if not expand_for_path_planning else [],
                )
            except Exception as exc:
                schema_context["warnings"].append("关系 schema 预取失败: {}".format(type(exc).__name__))

        schema_context["node_labels"] = list((schema_context.get("nodes_schema") or {}).get("labels") or node_labels)
        schema_context["relationship_types"] = list(
            (schema_context.get("edges_schema") or {}).get("relationship_types") or relationship_types
        )
        if schema_context["nodes_schema"] or schema_context["edges_schema"]:
            schema_context["disable_schema_tools"] = True
        return schema_context


def _final(payload: Mapping[str, Any]) -> BusinessDataSearchEvent:
    return {"type": "final", "content": json.dumps(dict(payload), ensure_ascii=False)}


def _mapping_node_labels(mapping: GraphEntityMapping) -> set[str]:
    labels: set[str] = set()
    if mapping.kind == "node" and mapping.label:
        labels.add(mapping.label)
    for segment in list(mapping.path_pattern or []):
        if not isinstance(segment, Mapping):
            continue
        label = str(segment.get("label") or "").strip()
        if label:
            labels.add(label)
    return labels


def _mapping_relationship_types(mapping: GraphEntityMapping) -> set[str]:
    relationship_types: set[str] = set()
    if mapping.kind == "edge" and mapping.edge_type:
        relationship_types.add(mapping.edge_type)
    for segment in list(mapping.path_pattern or []):
        if not isinstance(segment, Mapping):
            continue
        edge_type = str(segment.get("type") or segment.get("edge_type") or "").strip()
        if edge_type:
            relationship_types.add(edge_type)
    return relationship_types


def _compact_nodes_schema(payload: Any, focused_node_labels: list[str]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    focused = [str(label).strip() for label in focused_node_labels if str(label).strip()]
    focused_set = set(focused)
    raw_schema = payload.get("schema")
    schema = raw_schema if isinstance(raw_schema, Mapping) else {}
    selected_schema = {
        str(label): _limit_schema_fields(fields)
        for label, fields in schema.items()
        if isinstance(fields, Mapping)
    }
    raw_labels = sorted(selected_schema) or [
        str(label).strip()
        for label in (payload.get("labels") if isinstance(payload.get("labels"), list) else [])
        if str(label).strip()
    ]
    labels = _prioritize_values(raw_labels, focused_set)[:_MAX_SCHEMA_LABELS]
    return {
        "labels": labels,
        "schema": {label: selected_schema.get(label, {}) for label in labels},
        "count": len(labels),
        "truncated": len(raw_labels) > len(labels),
    }


def _compact_edges_schema(payload: Any, relationship_types: list[str]) -> dict[str, Any]:
    if not isinstance(payload, Mapping):
        return {}
    requested = {str(item).strip() for item in relationship_types if str(item).strip()}
    raw_types = payload.get("relationship_types")
    relationship_type_values = [
        str(item).strip()
        for item in (raw_types if isinstance(raw_types, list) else [])
        if str(item).strip() and (not requested or str(item).strip() in requested)
    ]
    raw_schema = payload.get("schema")
    schema = raw_schema if isinstance(raw_schema, Mapping) else {}
    selected_schema = {
        str(edge_type): _limit_schema_fields(fields)
        for edge_type, fields in schema.items()
        if isinstance(fields, Mapping) and (not requested or str(edge_type) in requested)
    }
    if not relationship_type_values:
        relationship_type_values = sorted(selected_schema)
    relationship_type_values = relationship_type_values[:_MAX_SCHEMA_LABELS]
    triples = _compact_relation_triples(payload.get("relation_triples"), requested)
    return {
        "relationship_types": relationship_type_values,
        "schema": {edge_type: selected_schema.get(edge_type, {}) for edge_type in relationship_type_values},
        "relation_triples": triples,
        "relation_triples_truncated": len(triples) >= _MAX_RELATION_TRIPLES,
    }


def _compact_relation_triples(raw: Any, requested: set[str]) -> list[dict[str, Any]]:
    if not isinstance(raw, list):
        return []
    triples: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        relation_type = str(item.get("relation_type") or item.get("type") or item.get("physical_relation_type") or "").strip()
        if requested and relation_type not in requested:
            continue
        triples.append({
            key: item.get(key)
            for key in ("source_label", "relation_type", "target_label", "type", "physical_relation_type")
            if item.get(key) not in (None, "")
        })
        if len(triples) >= _MAX_RELATION_TRIPLES:
            break
    return triples


def _limit_schema_fields(fields: Any) -> dict[str, Any]:
    if not isinstance(fields, Mapping):
        return {}
    selected: dict[str, Any] = {}
    pairs = [
        (str(key).strip(), value)
        for key, value in fields.items()
        if str(key).strip()
    ]
    for key, value in sorted(pairs, key=lambda item: item[0]):
        selected[key] = value
        if len(selected) >= _MAX_SCHEMA_FIELDS_PER_LABEL:
            break
    return selected


def _prioritize_values(values: list[str], preferred: set[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in sorted(preferred):
        if value in values and value not in seen:
            seen.add(value)
            ordered.append(value)
    for value in values:
        if value not in seen:
            seen.add(value)
            ordered.append(value)
    return ordered


def _graph_schema_expand_for_path_planning(provider_config: Mapping[str, Any]) -> bool:
    raw = provider_config.get("graph_schema_expand_for_path_planning")
    if raw in (None, ""):
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _analyze_path_intent(*, query: str, entity: str, mapping: GraphEntityMapping) -> dict[str, Any]:
    text = "{} {} {} {}".format(query or "", entity or "", mapping.entity or "", mapping.title or "")
    cues = [
        cue
        for cue in (
            "关系",
            "关联",
            "影响",
            "受影响",
            "对应",
            "父项",
            "子项",
            "上游",
            "下游",
            "变更前",
            "变更后",
            "BOM",
            "SOURCE_STRUCTURE",
            "STRUCTURE_LIST",
            "工艺关系",
            "供应商",
        )
        if cue in text
    ]
    return {
        "path_intent": bool(cues),
        "path_cues": cues,
        "preferred_tool": "pattern_search" if cues else "",
        "instruction": (
            "该问题包含关系/路径意图；若 schema_context.edges_schema.relation_triples 中存在可连接的节点和关系，"
            "应优先尝试 pattern_search。"
            if cues
            else ""
        ),
    }
