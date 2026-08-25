"""Shared graph-query agent helpers for business_data_search."""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any, Mapping

from ontology.ontology_catalog import resolve_object_reference
from ontology.ontologies import get_ontology_version_dir
from .models import GraphAgentResult, GraphBackendResult, GraphEntityMapping, GraphSearchRuntimeContext
from .timeouts import resolve_graph_agent_timeout_seconds
from .tools.executor import GraphToolExecution


GRAPH_AGENT_SYSTEM_PROMPT = """你是 business_data_search Provider 内部的图查询 Agent。
你的任务不是直接面向用户聊天，而是通过内部图工具检索实例数据，并返回可靠的结构化工具结果。

必须遵守：
1. 基于 ontology_id、entity、query 理解查询目标。
2. 优先使用输入中的 object_definition、business_context、mapping、schema_context；schema_context 已由 Provider 按 entity 预取。
3. 字段、关系和业务口径必须以本体包对象定义、Operation/Rule/Script 上下文、mapping 和 schema_context 为准。
4. 当 schema_context 已包含所需节点/关系 schema 时，不要再调用 get_nodes_schema 或 get_edges_schema。
5. object-data-mappings.json 只提供 object 到 resource/fields 的标准映射；不要假设 mapping 中存在 path_pattern。
6. 关系/路径型问题（如“关联、影响、对应、父子项、BOM、变更前/后版本、从 A 找 B”）优先基于 schema_context.edges_schema.relation_triples 推导 pattern_search。
7. pattern_search 的节点 label、关系 type、过滤字段、return_properties 必须来自 schema_context.nodes_schema / edges_schema / relation_triples；不确定时先查记录资源，再用 schema_context 继续尝试路径检索。
8. 属性过滤前先确认节点或关系字段；优先从 mapping.property_map、mapping.return_properties 和 schema_context 确认。
9. 不要编造未检索到的数据。
10. 不要要求用户提供 Neo4J/HugeGraph 连接信息。
11. 不要暴露 workspace_id、密码、token。
12. 不要输出 Cypher、Gremlin、SQL 或数据库连接信息。
13. return_properties 必须列出明确字段名，不能使用 "*"、"all" 或其它通配符；不确定字段时优先参考 schema_context/mapping。
14. 结构化 filter_dict 操作符使用 eq/ne/gt/gte/lt/lte/contains/starts_with/ends_with/in/not_in/is_null，不要使用 Mongo 风格 "$contains"。
15. filter_dict 顶层 key 必须是 schema 中存在的字段名，不能使用 "$or"、"$and" 等 Mongo 风格逻辑操作符；“编码或名称”等 OR 查询必须拆成 queries 数组里的多个子查询。
16. 示例：查询编码或名称包含 A001 时，应生成两个 queries 子项，分别使用 {"ObjectCode":{"contains":"A001"}} 和 {"ObjectName":{"contains":"A001"}}。
17. 同一字段同一值不要同时使用 eq 和 contains/starts_with/ends_with 重复查询；精确 eq 已命中时，不要再追加模糊兜底查询。
18. 同一个 element_class/element_type/filter_dict/return_properties 查询不要重复调用。
19. 需要追加更窄过滤条件时可以继续查询，但最终事实以 Provider 去重后的工具结果为准。
20. 当 schema_context.mapping_intent.prefer_tool 为 property_filter，且 property_filter/sorted_search 返回 success=true、sufficient_for_query=true 时，立即结束本次内部图检索，不要再追加 pattern_search。
21. “关系记录/工艺关系记录/受影响对象记录/表记录”的查询不等同于图路径查询；如果用户只要求记录字段、编号、列表或表信息，property_filter 命中后即视为充分。

路径检索策略：
- schema_context.query_analysis.path_intent 为 true 且 schema_context.mapping_intent.pattern_search_required 为 true 时，应至少尝试一次 pattern_search，除非 schema_context 中完全没有可用 relation_triples。
- 若 object mapping 的 resource 是关系记录节点（例如名称含“关系”“映射”“受影响”“结构”“清单”等），用户只查询“记录/字段/编号/表信息”时应使用 property_filter；只有问题明确需要关联对象详情、上下游对象、从 A 找 B 的对象属性时，才继续 pattern_search。
- 构造 path_pattern 时使用 relation_triples 的 source_label、relation_type、target_label；物理关系 type 通常来自 edges_schema.relationship_types 或 relation_triples.physical_relation_type/type。
- 对 D2O_RELATION 这类通用物理边，业务关系名通常在边属性 relation_type 中；可在边段 props/filters 中约束 relation_type。

可用内部工具以本次输入 allowed_tools 和实际 tools 列表为准；schema 工具只在 Provider 未预取到 schema 时提供。可能的内部工具包括：
- get_object_types
- get_relation_types
- get_nodes_schema
- get_edges_schema
- property_filter
- pattern_search
- count_search
- aggregate_search
- sorted_search
- get_element_info
- hop_search
- shortest_path
- list_logic_titles
- get_logic_content

当已经获得足够的工具结果时，输出简短 JSON object 作为结束说明。最终 provider 返回的数据以工具结果为准。
"""


class GraphAgentError(RuntimeError):
    """Raised when GraphQueryAgent cannot complete the internal tool loop."""

    error_code = "GRAPH_AGENT_ERROR"
    retryable = True


class GraphAgentUnavailableError(GraphAgentError):
    """Raised when the configured LLM router/service is unavailable."""

    error_code = "GRAPH_AGENT_UNAVAILABLE"
    retryable = False


class GraphAgentTimeoutError(GraphAgentError):
    """Raised when GraphQueryAgent exceeds its provider-level timeout."""

    error_code = "GRAPH_AGENT_TIMEOUT"
    retryable = False


class GraphAgentToolInputError(GraphAgentError):
    """Raised when GraphQueryAgent cannot recover from invalid internal tool args."""

    error_code = "GRAPH_TOOL_INPUT_ERROR"
    retryable = True


def _build_agent_input(
    *,
    context: GraphSearchRuntimeContext,
    mapping: GraphEntityMapping,
    tool_schemas: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "provider": context.provider,
        "ontology_id": context.ontology_id,
        "entity": context.entity,
        "query": context.query,
        "object_definition": _read_object_definition(context),
        "business_context": context.business_context,
        "schema_context": _agent_input_schema_context(context.schema_context),
        "mapping": _mapping_payload(mapping),
        "limits": {
            "max_result_rows": context.max_result_rows,
        },
        "allowed_tools": [schema["function"]["name"] for schema in tool_schemas],
    }


def _build_graph_agent_system_prompt(schema_context: Mapping[str, Any] | None) -> str:
    if not _schema_context_preloaded(schema_context):
        return GRAPH_AGENT_SYSTEM_PROMPT
    try:
        schema_payload = json.dumps(schema_context, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return GRAPH_AGENT_SYSTEM_PROMPT
    if len(schema_payload) > 12000:
        schema_payload = "{}...".format(schema_payload[:12000])
    return "{}\n\n预取图 schema 上下文（Provider 已按本次 entity/mapping 预取，优先使用，不要重复调用 schema 工具）：\n```json\n{}\n```".format(
        GRAPH_AGENT_SYSTEM_PROMPT,
        schema_payload,
    )


def _agent_input_schema_context(schema_context: Mapping[str, Any] | None) -> Any:
    if not _schema_context_preloaded(schema_context):
        return dict(schema_context or {})
    return {
        "in_system_prompt": True,
        "source": str(schema_context.get("source") or "provider_preload"),
        "node_labels": list(schema_context.get("node_labels") or []),
        "relationship_types": list(schema_context.get("relationship_types") or []),
        "focused_node_labels": list(schema_context.get("focused_node_labels") or []),
        "focused_relationship_types": list(schema_context.get("focused_relationship_types") or []),
        "mapping_resource": str(schema_context.get("mapping_resource") or ""),
        "query_analysis": dict(schema_context.get("query_analysis") or {}),
        "mapping_intent": dict(schema_context.get("mapping_intent") or {}),
    }


def _mapping_payload(mapping: GraphEntityMapping) -> dict[str, Any]:
    return {
        "entity": mapping.entity,
        "kind": mapping.kind,
        "source": mapping.source,
        "label": mapping.label,
        "edge_type": mapping.edge_type,
        "title": mapping.title,
        "id_property": mapping.id_property,
        "property_map": dict(mapping.property_map or {}),
        "return_properties": list(mapping.return_properties or []),
        "default_filters": dict(mapping.default_filters or {}),
        "path_pattern": list(mapping.path_pattern or []),
        "return_vars": list(mapping.return_vars or []),
    }


def _read_object_definition(context: GraphSearchRuntimeContext) -> str:
    root = str(context.workspace_root or "").strip()
    ontology_id = str(context.ontology_id or "").strip()
    entity = str(context.entity or "").strip()
    if not root or not ontology_id or not entity:
        return ""
    ontology_root = (Path(root) / ontology_id).resolve()
    if not ontology_root.is_dir():
        return ""
    try:
        version_dir = get_ontology_version_dir(
            ontology_root,
            str(context.ontology_version or "").strip() or None,
            create=False,
        ) or ontology_root
    except Exception:
        version_dir = ontology_root
    resolution, error = resolve_object_reference(version_dir, entity)
    if error or resolution is None:
        return ""
    try:
        return resolution.path.read_text(encoding="utf-8")[:6000]
    except OSError:
        return ""


def _safe_exception_detail(exc: Exception, *, limit: int = 400) -> str:
    detail = "{}: {}".format(exc.__class__.__name__, str(exc or "").strip()).strip()
    detail = _redact_sensitive_text(detail.replace("\n", " ").replace("\r", " "))
    if len(detail) > limit:
        return "{}...".format(detail[:limit])
    return detail


def _redact_sensitive_text(text: str) -> str:
    redacted = re.sub(
        r"(?i)\b(password|passwd|pwd|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*[^,\s;]+",
        lambda match: "{}=[REDACTED]".format(match.group(1)),
        text,
    )
    return re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer [REDACTED]", redacted)


def _agent_result_from_execution(
    *,
    execution: GraphToolExecution,
    executed_steps: list[dict[str, Any]],
    final_message: str,
    warnings: list[str] | None = None,
) -> GraphAgentResult:
    backend_result = _deduped_backend_result_for_execution(execution)
    return GraphAgentResult(
        tool_name=execution.tool_name,
        tool_args=dict(execution.tool_args or {}),
        backend_result=backend_result,
        executed_steps=list(executed_steps or []),
        warnings=list(warnings or []),
        final_message=final_message,
    )


def _agent_result_from_executions(
    *,
    executions: list[GraphToolExecution],
    executed_steps: list[dict[str, Any]],
    final_message: str,
    warnings: list[str] | None = None,
) -> GraphAgentResult:
    if len(executions) == 1:
        return _agent_result_from_execution(
            execution=executions[0],
            executed_steps=executed_steps,
            final_message=final_message,
            warnings=warnings,
        )

    combined_records: list[dict[str, Any]] = []
    combined_warnings: list[str] = list(warnings or [])
    seen_record_keys: set[str] = set()
    total = 0
    raw_total = 0
    truncated = False
    for execution in executions:
        backend_result = execution.backend_result or GraphBackendResult()
        raw_records = list(backend_result.records or [])
        raw_record_count = backend_result.total if backend_result.total is not None else len(raw_records)
        raw_total += int(raw_record_count or 0)
        records = _dedupe_execution_records(
            tool_name=execution.tool_name,
            records=raw_records,
            seen_record_keys=seen_record_keys,
        )
        record_count = len(records) if _dedupe_records_for_tool(execution.tool_name) else raw_record_count
        total += int(record_count or 0)
        truncated = truncated or bool(backend_result.truncated)
        combined_warnings.extend(backend_result.warnings or [])
        combined_records.append({
            "tool": execution.tool_name,
            "tool_args": dict(execution.tool_args or {}),
            "record_count": record_count,
            "raw_record_count": raw_record_count,
            "result": _compact_execution_result(execution),
            "records": records,
            "truncated": bool(backend_result.truncated),
        })

    return GraphAgentResult(
        tool_name="multi_tool_search",
        tool_args={
            "tools": [
                {
                    "tool": execution.tool_name,
                    "tool_args": dict(execution.tool_args or {}),
                }
                for execution in executions
            ]
        },
        backend_result=GraphBackendResult(
            records=combined_records,
            total=total,
            truncated=truncated,
            warnings=combined_warnings,
            payload={
                "success": True,
                "result": {
                    "results": combined_records,
                    "count": len(combined_records),
                    "total_record_count": total,
                    "raw_total_record_count": raw_total,
                    "deduplicated": raw_total != total,
                },
            },
        ),
        executed_steps=list(executed_steps or []),
        warnings=list(warnings or []),
        final_message=final_message,
    )


def _summarize_tool_execution(execution: GraphToolExecution) -> str:
    result = execution.backend_result
    if result is None:
        return "执行内部图工具 {}".format(execution.tool_name)
    total = result.total if result.total is not None else len(result.records or [])
    return "执行内部图工具 {}，返回 {} 条".format(execution.tool_name, total)


def _compact_execution_result(execution: GraphToolExecution) -> Any:
    if not isinstance(execution.payload, Mapping):
        return None
    result = execution.payload.get("result")
    if not isinstance(result, Mapping):
        return result
    return _strip_duplicate_result_payload(dict(result))


def _strip_duplicate_result_payload(value: dict[str, Any]) -> dict[str, Any]:
    compact: dict[str, Any] = {}
    for key, item in value.items():
        if key in {"items", "paths", "records"}:
            continue
        if key == "results" and isinstance(item, list):
            compact[key] = [
                _strip_duplicate_result_payload(dict(child))
                if isinstance(child, Mapping)
                else child
                for child in item
            ]
            continue
        compact[key] = item
    return compact


def _dedupe_execution_records(
    *,
    tool_name: str,
    records: list[dict[str, Any]],
    seen_record_keys: set[str],
) -> list[dict[str, Any]]:
    if not _dedupe_records_for_tool(tool_name):
        return list(records or [])
    deduped: list[dict[str, Any]] = []
    for record in records or []:
        key = _record_dedupe_key(record)
        if key in seen_record_keys:
            continue
        seen_record_keys.add(key)
        deduped.append(record)
    return deduped


def _deduped_backend_result_for_execution(execution: GraphToolExecution) -> GraphBackendResult:
    backend_result = execution.backend_result or GraphBackendResult()
    if not _dedupe_records_for_tool(execution.tool_name):
        return backend_result
    raw_records = list(backend_result.records or [])
    raw_record_count = backend_result.total if backend_result.total is not None else len(raw_records)
    records = _dedupe_execution_records(
        tool_name=execution.tool_name,
        records=raw_records,
        seen_record_keys=set(),
    )
    if len(records) == len(raw_records) and raw_record_count == len(records):
        return backend_result
    payload = dict(backend_result.payload or {})
    result = payload.get("result")
    if isinstance(result, Mapping):
        result_payload = dict(result)
        result_payload["raw_record_count"] = raw_record_count
        result_payload["deduplicated"] = True
        payload["result"] = result_payload
    elif payload:
        payload["raw_record_count"] = raw_record_count
        payload["deduplicated"] = True
    return GraphBackendResult(
        records=records,
        total=len(records),
        truncated=bool(backend_result.truncated),
        warnings=list(backend_result.warnings or []),
        payload=payload,
    )


def _dedupe_records_for_tool(tool_name: str) -> bool:
    return str(tool_name or "").strip() in {
        "property_filter",
        "sorted_search",
        "pattern_search",
        "get_element_info",
    }


def _record_dedupe_key(record: Any) -> str:
    if isinstance(record, Mapping):
        for field in ("entity_instance_id", "element_id", "id", "ID", "_id"):
            value = record.get(field)
            if value not in (None, ""):
                return "{}={}".format(field, str(value))
        path_key = _path_record_key(record)
        if path_key:
            return path_key
        properties = record.get("properties")
        if isinstance(properties, Mapping):
            for field in _candidate_property_identity_fields(properties):
                value = properties.get(field)
                if value not in (None, ""):
                    return "properties.{}={}".format(field, str(value))
    try:
        return "json:{}".format(json.dumps(record, sort_keys=True, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return "repr:{}".format(repr(record))


def _path_record_key(record: Mapping[str, Any]) -> str:
    parts: list[str] = []
    for collection_name in ("nodes", "relationships"):
        collection = record.get(collection_name)
        if not isinstance(collection, list):
            continue
        for item in collection:
            if not isinstance(item, Mapping):
                continue
            identity = item.get("entity_instance_id") or item.get("element_id") or item.get("id") or item.get("ID")
            if identity not in (None, ""):
                parts.append("{}:{}".format(collection_name, str(identity)))
    if not parts:
        return ""
    return "path:{}".format("|".join(parts))


def _candidate_property_identity_fields(properties: Mapping[str, Any]) -> list[str]:
    preferred = ["ID"]
    keys = [str(key) for key in properties if str(key)]
    selected = [key for key in preferred if key in properties]
    selected.extend(
        key
        for key in sorted(keys)
        if key not in selected and (key.endswith("ID") or key.endswith("_ID"))
    )
    return selected


def _graph_agent_timeout_seconds(
    *,
    provider_config: Mapping[str, Any],
    outer_timeout_seconds: int,
) -> float:
    del outer_timeout_seconds
    return resolve_graph_agent_timeout_seconds(provider_config)


def _graph_agent_max_iterations(provider_config: Mapping[str, Any]) -> int:
    try:
        value = int(provider_config.get("max_iterations") or 4)
    except (TypeError, ValueError):
        value = 4
    return max(1, min(value, 8))


def _path_intent_missing_pattern(
    *,
    context: GraphSearchRuntimeContext,
    executions: list[GraphToolExecution],
) -> bool:
    query_analysis = context.schema_context.get("query_analysis") if isinstance(context.schema_context, Mapping) else None
    if not isinstance(query_analysis, Mapping) or not bool(query_analysis.get("path_intent")):
        return False
    if any(execution.tool_name == "pattern_search" for execution in executions):
        return False
    edges_schema = context.schema_context.get("edges_schema") if isinstance(context.schema_context, Mapping) else None
    if not isinstance(edges_schema, Mapping):
        return False
    if not edges_schema.get("relation_triples"):
        return False
    if _explicit_field_request_satisfied(context=context, executions=executions):
        return False
    return True


def _path_missing_pattern_warnings(
    context: GraphSearchRuntimeContext,
    executions: list[GraphToolExecution],
) -> list[str]:
    if not _path_intent_missing_pattern(context=context, executions=executions):
        return []
    return ["关系/路径型查询尚未成功执行 pattern_search，当前结果可能只包含记录型资源而非完整关联路径"]


def _path_pattern_retry_message(context: GraphSearchRuntimeContext) -> str:
    return (
        "当前问题被识别为关系/路径型查询，但你尚未调用 pattern_search。"
        "请基于 schema_context.edges_schema.relation_triples 中的 source_label、relation_type、target_label，"
        "以及 nodes_schema/edges_schema 中存在的字段，追加一次 pattern_search。"
        "如果之前已经通过 property_filter 查到记录资源，可将记录中的关键字段作为路径过滤条件；"
        "不能编造 schema 中不存在的 label、关系 type 或字段。"
        "原始查询：{}".format(str(context.query or "").strip())
    )


def _explicit_field_request_satisfied(
    *,
    context: GraphSearchRuntimeContext,
    executions: list[GraphToolExecution],
) -> bool:
    requested_properties = _requested_properties_from_query(context)
    if not requested_properties:
        return False
    returned_properties: set[str] = set()
    has_records = False
    for execution in executions:
        if execution.tool_name not in {"property_filter", "sorted_search"}:
            continue
        result = execution.backend_result
        if result is None:
            continue
        records = list(result.records or [])
        if records:
            has_records = True
        returned_properties.update(_returned_properties_from_records(records))
    return has_records and requested_properties.issubset(returned_properties)


def _requested_properties_from_query(context: GraphSearchRuntimeContext) -> set[str]:
    query = str(context.query or "")
    mapping = context.mapping
    if mapping is None:
        return set()
    requested: set[str] = set()
    for business_name, physical_name in (mapping.property_map or {}).items():
        business_key = str(business_name or "").strip()
        physical_key = str(physical_name or "").strip()
        if not physical_key:
            continue
        if (business_key and business_key in query) or physical_key in query:
            requested.add(physical_key)
    return requested


def _returned_properties_from_records(records: list[dict[str, Any]]) -> set[str]:
    returned: set[str] = set()
    for record in records:
        if not isinstance(record, Mapping):
            continue
        properties = record.get("properties")
        if isinstance(properties, Mapping):
            returned.update(str(prop).strip() for prop in properties if str(prop).strip())
        returned.update(_returned_properties_from_nested_record(record))
    return returned


def _returned_properties_from_nested_record(value: Any) -> set[str]:
    returned: set[str] = set()
    if isinstance(value, Mapping):
        properties = value.get("properties")
        if isinstance(properties, Mapping):
            returned.update(str(prop).strip() for prop in properties if str(prop).strip())
        for child in value.values():
            returned.update(_returned_properties_from_nested_record(child))
    elif isinstance(value, list):
        for item in value:
            returned.update(_returned_properties_from_nested_record(item))
    return returned


def _graph_tool_schemas_for_context(context: GraphSearchRuntimeContext) -> list[dict[str, Any]]:
    if _schema_context_preloaded(context.schema_context):
        return [
            schema
            for schema in _GRAPH_TOOL_SCHEMAS
            if schema["function"]["name"] not in _GRAPH_SCHEMA_TOOL_NAMES
        ]
    return list(_GRAPH_TOOL_SCHEMAS)


def _schema_context_preloaded(schema_context: Mapping[str, Any] | None) -> bool:
    if not isinstance(schema_context, Mapping):
        return False
    if schema_context.get("disable_schema_tools") is True:
        return True
    nodes_schema = schema_context.get("nodes_schema")
    edges_schema = schema_context.get("edges_schema")
    return bool(nodes_schema or edges_schema)


def _format_timeout(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else "{:.3f}".format(value).rstrip("0").rstrip(".")


_GRAPH_SCHEMA_TOOL_NAMES = {
    "get_object_types",
    "get_relation_types",
    "get_nodes_schema",
    "get_edges_schema",
}


_GRAPH_TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_nodes_schema",
            "description": "获取图谱节点类型及属性字段 schema，可指定 element_types。",
            "parameters": {
                "type": "object",
                "properties": {
                    "element_types": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_edges_schema",
            "description": "获取关系 schema 和实体类型间关系三元组，可指定 relation_types。",
            "parameters": {
                "type": "object",
                "properties": {
                    "relation_types": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "property_filter",
            "description": "按属性过滤节点或关系，支持 queries 批量子查询；OR 条件必须拆成多个 queries 子项，不要在 filter_dict 顶层使用 $or/$and。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "query_id": {"type": "string"},
                                "element_class": {"type": "string"},
                                "element_type": {"type": "string", "enum": ["NODE", "EDGE"]},
                                "filter_dict": {
                                    "type": "object",
                                    "description": "顶层 key 必须是字段名；结构化操作符使用 contains/in/not_in 等无 $ 名称；不要使用 $or/$and，OR 请拆成多个 queries 子项。",
                                },
                                "return_properties": {
                                    "type": "array",
                                    "description": "明确字段名数组，不能使用 * 或 all。",
                                    "items": {"type": "string", "description": "明确字段名，不能使用 *。"},
                                },
                                "limit": {"type": "integer"},
                                "offset": {"type": "integer"},
                            },
                            "required": ["element_class", "element_type"],
                        },
                    },
                    "max_total_rows": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "count_search",
            "description": "统计满足条件的节点或关系数量，支持 queries 批量子查询；OR 条件必须拆成多个 queries 子项，不要在 filter_dict 顶层使用 $or/$and。",
            "parameters": {
                "type": "object",
                "properties": {
                    "queries": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "query_id": {"type": "string"},
                                "element_class": {"type": "string"},
                                "element_type": {"type": "string", "enum": ["NODE", "EDGE"]},
                                "filter_dict": {
                                    "type": "object",
                                    "description": "顶层 key 必须是字段名；结构化操作符使用 contains/in/not_in 等无 $ 名称；不要使用 $or/$and，OR 请拆成多个 queries 子项。",
                                },
                            },
                            "required": ["element_class", "element_type"],
                        },
                    },
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "aggregate_search",
            "description": "对节点或关系的某个属性做 SUM/AVG/MIN/MAX/COUNT 聚合。",
            "parameters": {
                "type": "object",
                "properties": {
                    "element_class": {"type": "string"},
                    "element_type": {"type": "string", "enum": ["NODE", "EDGE"]},
                    "target_property": {"type": "string"},
                    "agg": {"type": "string", "enum": ["SUM", "AVG", "MIN", "MAX", "COUNT"]},
                    "filter_dict": {
                        "type": "object",
                        "description": "顶层 key 必须是字段名；结构化操作符使用 contains/in/not_in 等无 $ 名称；不要使用 $or/$and。",
                    },
                },
                "required": ["element_class", "element_type", "agg"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "sorted_search",
            "description": "查询节点或关系并按指定属性排序返回。",
            "parameters": {
                "type": "object",
                "properties": {
                    "element_class": {"type": "string"},
                    "element_type": {"type": "string", "enum": ["NODE", "EDGE"]},
                    "filter_dict": {
                        "type": "object",
                        "description": "顶层 key 必须是字段名；结构化操作符使用 contains/in/not_in 等无 $ 名称；不要使用 $or/$and。",
                    },
                    "return_properties": {
                        "type": "array",
                        "description": "明确字段名数组，不能使用 * 或 all。",
                        "items": {"type": "string", "description": "明确字段名，不能使用 *。"},
                    },
                    "sort_by": {"type": "array", "items": {"type": "string"}},
                    "ascending": {"type": "boolean"},
                    "limit": {"type": "integer"},
                    "offset": {"type": "integer"},
                },
                "required": ["element_class", "element_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "pattern_search",
            "description": (
                "按图结构模式匹配路径。path_pattern 使用 Jiutian 数组协议：节点、关系、节点交替排列。"
                "节点或关系属性过滤写在 props 中，例如 {\"label\":\"ObjectVersion\",\"props\":{\"ObjectCode\":\"OBJ-001\"}}。"
                "兼容 filters 作为 props 别名。"
                "用 return_vars 指定需要返回的变量；用 return_properties 指定返回字段，"
                "可传字符串数组应用到全部变量，或传 {变量名: [字段名]} 精确控制；"
                "return_properties 只能写明确字段名，不能使用 * 或 all。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "path_pattern": {"type": "array", "items": {"type": "array"}},
                    "return_vars": {"type": "array", "items": {"type": "string"}},
                    "return_properties": {
                        "description": "明确字段名数组应用到全部返回变量，或变量名到明确字段名数组的对象；不能使用 * 或 all。",
                        "oneOf": [
                            {"type": "array", "items": {"type": "string", "description": "明确字段名，不能使用 *。"}},
                            {
                                "type": "object",
                                "additionalProperties": {
                                    "type": "array",
                                    "items": {"type": "string", "description": "明确字段名，不能使用 *。"},
                                },
                            },
                        ],
                    },
                    "limit": {"type": "integer"},
                },
                "required": ["path_pattern"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_element_info",
            "description": "根据实例 ID 获取单个节点或关系详情。",
            "parameters": {
                "type": "object",
                "properties": {
                    "element_class": {"type": "string"},
                    "element_type": {"type": "string", "enum": ["NODE", "EDGE"]},
                    "element_instance_id": {"type": "string"},
                },
                "required": ["element_instance_id"],
            },
        },
    },
]

_GRAPH_TOOL_SCHEMAS.extend([
    {
        "type": "function",
        "function": {
            "name": "get_object_types",
            "description": "获取当前 scope 内全部实体/节点类型。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_relation_types",
            "description": "获取当前 scope 内全部语义关系类型。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "hop_search",
            "description": "从指定节点实例出发查询 N 度邻域。",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_instance_id": {"type": "string"},
                    "hop_num": {"type": "integer", "minimum": 1, "maximum": 5},
                    "accurate_flag": {"type": "boolean"},
                },
                "required": ["entity_instance_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "shortest_path",
            "description": "查询两个节点之间的最短关联路径。",
            "parameters": {
                "type": "object",
                "properties": {
                    "start_label": {"type": "string"},
                    "start_props": {"type": "object"},
                    "end_label": {"type": "string"},
                    "end_props": {"type": "object"},
                    "max_depth": {"type": "integer", "minimum": 1, "maximum": 15},
                    "directed": {"type": "boolean"},
                },
                "required": ["start_label", "start_props", "end_label"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_logic_titles",
            "description": "列出当前 scope 内的业务逻辑标题目录。",
            "parameters": {
                "type": "object",
                "properties": {"entity_type": {"type": "string"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_logic_content",
            "description": "按实体实例 ID 和可选逻辑标题读取业务逻辑内容。",
            "parameters": {
                "type": "object",
                "properties": {
                    "entity_instance_ids": {
                        "type": "array",
                        "items": {"type": "string"},
                        "minItems": 1,
                    },
                    "logic_titles": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["entity_instance_ids"],
            },
        },
    },
])

_GRAPH_TOOL_ORDER = {
    name: index
    for index, name in enumerate([
        "get_object_types",
        "get_relation_types",
        "get_nodes_schema",
        "get_edges_schema",
        "property_filter",
        "pattern_search",
        "count_search",
        "aggregate_search",
        "sorted_search",
        "get_element_info",
        "hop_search",
        "shortest_path",
        "list_logic_titles",
        "get_logic_content",
    ])
}
_GRAPH_TOOL_SCHEMAS.sort(
    key=lambda schema: _GRAPH_TOOL_ORDER[schema["function"]["name"]]
)
