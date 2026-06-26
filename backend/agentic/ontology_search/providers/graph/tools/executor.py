"""Internal graph tool executor used by GraphQueryAgent."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..backends.base import GraphBackend, GraphBackendUnsupportedError
from ..models import GraphBackendResult, GraphEntityMapping, GraphSearchRuntimeContext
from ..scope import sanitize_return_properties
from ..trace import GraphProviderTrace


DATA_TOOLS = {
    "property_filter",
    "count_search",
    "aggregate_search",
    "sorted_search",
    "pattern_search",
    "get_element_info",
}


@dataclass(frozen=True)
class GraphToolExecution:
    tool_name: str
    tool_args: dict[str, Any]
    payload: dict[str, Any]
    backend_result: GraphBackendResult | None = None


class GraphToolInputError(ValueError):
    """Raised when an internal graph tool receives invalid arguments."""

    error_code = "GRAPH_TOOL_INPUT_ERROR"
    retryable = True


class GraphToolExecutor:
    """Executes Jiutian-style graph tool calls against the provider backend."""

    def __init__(
        self,
        *,
        backend: GraphBackend,
        context: GraphSearchRuntimeContext,
        mapping: GraphEntityMapping,
        trace: GraphProviderTrace | None = None,
    ) -> None:
        self.backend = backend
        self.context = context
        self.mapping = mapping
        self.trace = trace

    def execute(self, tool_name: str, tool_args: Mapping[str, Any] | None) -> GraphToolExecution:
        name = str(tool_name or "").strip()
        args = dict(tool_args or {})
        span_key = ""
        started = 0.0
        if self.trace is not None:
            span_key, started = self.trace.tool_started(tool_name=name or "<empty>", tool_args=args)
        try:
            execution = self._execute(name, args)
        except GraphToolInputError as exc:
            if self.trace is not None:
                self.trace.tool_failed(
                    span_key=span_key,
                    started=started,
                    tool_name=name or "<empty>",
                    error=exc,
                )
            raise
        except (GraphBackendUnsupportedError, ValueError) as exc:
            wrapped = GraphToolInputError(str(exc))
            if self.trace is not None:
                self.trace.tool_failed(
                    span_key=span_key,
                    started=started,
                    tool_name=name or "<empty>",
                    error=wrapped,
                )
            raise wrapped from exc
        except Exception as exc:
            if self.trace is not None:
                self.trace.tool_failed(
                    span_key=span_key,
                    started=started,
                    tool_name=name or "<empty>",
                    error=exc,
                )
            raise
        if self.trace is not None:
            self.trace.tool_finished(
                span_key=span_key,
                started=started,
                tool_name=execution.tool_name,
                backend_result=execution.backend_result,
                payload=execution.payload,
            )
        return execution

    def _execute(self, name: str, args: dict[str, Any]) -> GraphToolExecution:
        _reject_scope_fields(args, _forbidden_scope_fields(self.context))
        _validate_return_properties_fields(args)
        if name == "get_nodes_schema":
            return self._get_nodes_schema(args)
        if name == "get_edges_schema":
            return self._get_edges_schema(args)
        if name == "property_filter":
            return self._property_filter(args)
        if name == "count_search":
            return self._count_search(args)
        if name == "aggregate_search":
            return self._aggregate_search(args)
        if name == "sorted_search":
            return self._sorted_search(args)
        if name == "pattern_search":
            return self._pattern_search(args)
        if name == "get_element_info":
            return self._get_element_info(args)
        raise GraphToolInputError("不支持的内部图工具: {}".format(name or "<empty>"))

    def _get_nodes_schema(self, args: dict[str, Any]) -> GraphToolExecution:
        payload = self.backend.get_nodes_schema(
            scope=self.context.scope,
            element_types=_optional_string_list(args.get("element_types")),
        )
        return GraphToolExecution("get_nodes_schema", args, _success_payload(payload))

    def _get_edges_schema(self, args: dict[str, Any]) -> GraphToolExecution:
        payload = self.backend.get_edges_schema(
            scope=self.context.scope,
            relation_types=_optional_string_list(args.get("relation_types")),
        )
        return GraphToolExecution("get_edges_schema", args, _success_payload(payload))

    def _property_filter(self, args: dict[str, Any]) -> GraphToolExecution:
        queries = _normalize_property_queries(args)
        max_total_rows = _positive_int(args.get("max_total_rows"), default=self.context.max_result_rows, upper=5000)
        all_records: list[dict[str, Any]] = []
        rows: list[dict[str, Any]] = []
        total_returned = 0
        truncated = False
        warnings: list[str] = []
        for index, query in enumerate(queries):
            diagnostic_fallback = _is_empty_filter_diagnostic_fallback(queries, index)
            if diagnostic_fallback:
                effective_limit = min(query["limit"], max_total_rows)
            else:
                remaining = max(0, max_total_rows - total_returned)
                effective_limit = min(query["limit"], remaining) if remaining > 0 else 0
            if effective_limit <= 0:
                if not diagnostic_fallback:
                    truncated = True
                row = {
                    "query_id": query["query_id"],
                    "element_class": query["element_class"],
                    "element_type": query["element_type"],
                    "items": [],
                    "count": 0,
                    "truncated": True,
                }
                if diagnostic_fallback:
                    row.update({
                        "diagnostic": True,
                        "diagnostic_reason": "empty_filter_after_constrained_query",
                    })
                rows.append(row)
                continue
            result = self.backend.property_filter(
                element_class=query["element_class"],
                element_type=query["element_type"],
                filter_dict=query["filter_dict"],
                return_properties=query.get("return_properties"),
                limit=effective_limit,
                offset=query["offset"],
                scope=self.context.scope,
            )
            items = list(result.records or [])
            warnings.extend(result.warnings or [])
            row = {
                "query_id": query["query_id"] or "q{}".format(index + 1),
                "element_class": query["element_class"],
                "element_type": query["element_type"],
                "items": [],
                "count": 0,
                "truncated": bool(result.truncated),
            }
            if diagnostic_fallback:
                ignored_count = _backend_result_count(result, len(items))
                row.update({
                    "diagnostic": True,
                    "diagnostic_reason": "empty_filter_after_constrained_query",
                    "diagnostic_sample_count": len(items),
                    "ignored_count": ignored_count,
                    "diagnostic_sample_truncated": bool(result.truncated),
                })
                _append_warning_once(
                    warnings,
                    (
                        "内部图工具忽略空过滤兜底样本查询 {}: "
                        "已有同类型带条件 property_filter，空过滤结果不计入业务命中"
                    ).format(query["query_id"] or "q{}".format(index + 1)),
                )
            else:
                total_returned += len(items)
                all_records.extend(items)
                truncated = truncated or bool(result.truncated) or total_returned >= max_total_rows
                row["items"] = items
                row["count"] = len(items)
            rows.append(row)
        payload = {
            "result": rows[0] if len(rows) == 1 else {"results": rows, "count": len(rows)},
            "success": True,
            "message": {"description": "查询成功"},
        }
        if warnings:
            payload["warnings"] = list(warnings)
        backend_result = GraphBackendResult(
            records=all_records,
            total=len(all_records),
            truncated=truncated,
            warnings=warnings,
            payload=payload,
        )
        return GraphToolExecution("property_filter", args, payload, backend_result)

    def _count_search(self, args: dict[str, Any]) -> GraphToolExecution:
        queries = _normalize_count_queries(args)
        rows: list[dict[str, Any]] = []
        total = 0
        warnings: list[str] = []
        for index, query in enumerate(queries):
            result = self.backend.count_search(
                element_class=query["element_class"],
                element_type=query["element_type"],
                filter_dict=query["filter_dict"],
                scope=self.context.scope,
            )
            count = int(result.total or 0)
            total += count
            warnings.extend(result.warnings or [])
            rows.append({
                "query_id": query["query_id"] or "q{}".format(index + 1),
                "element_class": query["element_class"],
                "element_type": query["element_type"],
                "count": count,
                "applied_filter": dict(query["filter_dict"] or {}),
            })
        payload = {
            "result": rows[0] if len(rows) == 1 else {"results": rows, "count": len(rows), "total_count": total},
            "success": True,
            "message": {"description": "统计成功"},
        }
        backend_result = GraphBackendResult(records=[], total=total, truncated=False, warnings=warnings, payload=payload)
        return GraphToolExecution("count_search", args, payload, backend_result)

    def _aggregate_search(self, args: dict[str, Any]) -> GraphToolExecution:
        element_class = str(args.get("element_class") or "").strip()
        element_type = str(args.get("element_type") or "").strip().upper()
        target_property = str(args.get("target_property") or "").strip()
        agg = str(args.get("agg") or "").strip().upper()
        filter_dict = args.get("filter_dict") or {}
        if not element_class:
            raise GraphToolInputError("element_class 不能为空")
        if element_type not in {"NODE", "EDGE"}:
            raise GraphToolInputError("element_type 仅支持 NODE 或 EDGE")
        if not isinstance(filter_dict, Mapping):
            raise GraphToolInputError("filter_dict 必须是对象")
        result = self.backend.aggregate_search(
            element_class=element_class,
            element_type=element_type,
            target_property=target_property,
            agg=agg,
            filter_dict=dict(filter_dict),
            scope=self.context.scope,
        )
        payload = {
            "result": (result.records or [{}])[0] if result.records else {},
            "success": True,
            "message": {"description": "聚合成功"},
        }
        backend_result = GraphBackendResult(
            records=list(result.records or []),
            total=result.total,
            truncated=bool(result.truncated),
            warnings=list(result.warnings or []),
            payload=payload,
        )
        return GraphToolExecution("aggregate_search", args, payload, backend_result)

    def _sorted_search(self, args: dict[str, Any]) -> GraphToolExecution:
        element_class = str(args.get("element_class") or "").strip()
        element_type = str(args.get("element_type") or "").strip().upper()
        filter_dict = args.get("filter_dict") or {}
        if not element_class:
            raise GraphToolInputError("element_class 不能为空")
        if element_type not in {"NODE", "EDGE"}:
            raise GraphToolInputError("element_type 仅支持 NODE 或 EDGE")
        if not isinstance(filter_dict, Mapping):
            raise GraphToolInputError("filter_dict 必须是对象")
        limit = _positive_int(args.get("limit"), default=min(self.context.max_result_rows, 50), upper=2000)
        offset = _non_negative_int(args.get("offset"), default=0)
        result = self.backend.sorted_search(
            element_class=element_class,
            element_type=element_type,
            filter_dict=dict(filter_dict),
            return_properties=_optional_string_list(args.get("return_properties")),
            sort_by=_optional_string_list(args.get("sort_by")),
            ascending=bool(args.get("ascending", True)),
            limit=limit,
            offset=offset,
            scope=self.context.scope,
        )
        payload = {
            "result": {
                "items": list(result.records or []),
                "count": len(result.records or []),
                "truncated": bool(result.truncated),
            },
            "success": True,
            "message": {"description": "排序查询成功"},
        }
        backend_result = GraphBackendResult(
            records=list(result.records or []),
            total=result.total if result.total is not None else len(result.records or []),
            truncated=bool(result.truncated),
            warnings=list(result.warnings or []),
            payload=payload,
        )
        return GraphToolExecution("sorted_search", args, payload, backend_result)

    def _pattern_search(self, args: dict[str, Any]) -> GraphToolExecution:
        path_pattern = args.get("path_pattern")
        if not isinstance(path_pattern, list) or not path_pattern:
            raise GraphToolInputError("path_pattern 必须是非空数组")
        return_vars = _optional_string_list(args.get("return_vars"))
        return_properties = _normalize_pattern_return_properties(args.get("return_properties"))
        _validate_pattern_search_against_schema(
            path_pattern=path_pattern,
            return_properties=return_properties,
            schema_context=self.context.schema_context,
        )
        limit = _positive_int(args.get("limit"), default=min(self.context.max_result_rows, 200), upper=2000)
        result = self.backend.pattern_search(
            path_pattern=path_pattern,
            return_vars=return_vars,
            limit=limit,
            scope=self.context.scope,
            return_properties=return_properties,
        )
        result_payload = {
            "paths": list(result.records or []),
            "count": len(result.records or []),
            "return_vars": return_vars or [],
            "truncated": bool(result.truncated),
        }
        if return_properties is not None:
            result_payload["return_properties"] = return_properties
        payload = {
            "result": result_payload,
            "success": True,
            "message": {"description": "图模式匹配成功"},
        }
        backend_result = GraphBackendResult(
            records=list(result.records or []),
            total=len(result.records or []),
            truncated=bool(result.truncated),
            warnings=list(result.warnings or []),
            payload=payload,
        )
        return GraphToolExecution("pattern_search", args, payload, backend_result)

    def _get_element_info(self, args: dict[str, Any]) -> GraphToolExecution:
        element_instance_id = str(args.get("element_instance_id") or args.get("element_id") or "").strip()
        if not element_instance_id:
            raise GraphToolInputError("element_instance_id 不能为空")
        detail = self.backend.get_element_info(
            element_instance_id=element_instance_id,
            element_class=str(args.get("element_class") or "").strip(),
            element_type=str(args.get("element_type") or "").strip().upper(),
            scope=self.context.scope,
        )
        payload = {"result": detail, "success": True, "message": {"description": "查询成功"}}
        backend_result = GraphBackendResult(
            records=[detail] if detail.get("found") else [],
            total=1 if detail.get("found") else 0,
            truncated=False,
            payload=payload,
        )
        return GraphToolExecution("get_element_info", args, payload, backend_result)


def _normalize_property_queries(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_queries = args.get("queries")
    if raw_queries is None:
        raw_queries = [{
            "query_id": "q1",
            "element_class": args.get("element_class"),
            "element_type": args.get("element_type"),
            "filter_dict": args.get("filter_dict") or {},
            "return_properties": args.get("return_properties"),
            "limit": args.get("limit"),
            "offset": args.get("offset"),
        }]
    if not isinstance(raw_queries, list) or not raw_queries:
        raise GraphToolInputError("queries 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_queries):
        if not isinstance(item, Mapping):
            raise GraphToolInputError("queries[{}] 必须是对象".format(index))
        element_class = str(item.get("element_class") or "").strip()
        element_type = str(item.get("element_type") or "").strip().upper()
        if not element_class:
            raise GraphToolInputError("queries[{}].element_class 不能为空".format(index))
        if element_type not in {"NODE", "EDGE"}:
            raise GraphToolInputError("queries[{}].element_type 仅支持 NODE 或 EDGE".format(index))
        filter_dict = item.get("filter_dict") or {}
        if not isinstance(filter_dict, Mapping):
            raise GraphToolInputError("queries[{}].filter_dict 必须是对象".format(index))
        query_id = str(item.get("query_id") or "").strip() or "q{}".format(index + 1)
        expanded_filters = _expand_simple_or_filter_dict(filter_dict, "queries[{}].filter_dict".format(index))
        for expanded_index, expanded_filter in enumerate(expanded_filters):
            normalized.append({
                "query_id": query_id if len(expanded_filters) == 1 else "{}_or{}".format(query_id, expanded_index + 1),
                "element_class": element_class,
                "element_type": element_type,
                "filter_dict": expanded_filter,
                "return_properties": _optional_string_list(item.get("return_properties")),
                "limit": _positive_int(item.get("limit"), default=100, upper=2000),
                "offset": _non_negative_int(item.get("offset"), default=0),
            })
    return normalized


def _is_empty_filter_diagnostic_fallback(queries: list[dict[str, Any]], index: int) -> bool:
    if len(queries) <= 1:
        return False
    query = queries[index]
    if query.get("filter_dict"):
        return False
    current_key = (query.get("element_class"), query.get("element_type"))
    return any(
        bool(previous.get("filter_dict"))
        and (previous.get("element_class"), previous.get("element_type")) == current_key
        for previous in queries[:index]
    )


def _backend_result_count(result: GraphBackendResult, fallback_count: int) -> int:
    try:
        if result.total is not None:
            return int(result.total)
    except (TypeError, ValueError):
        return fallback_count
    return fallback_count


def _append_warning_once(warnings: list[str], warning: str) -> None:
    if warning not in warnings:
        warnings.append(warning)


def _normalize_count_queries(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_queries = args.get("queries")
    if raw_queries is None:
        raw_queries = [{
            "query_id": "q1",
            "element_class": args.get("element_class"),
            "element_type": args.get("element_type"),
            "filter_dict": args.get("filter_dict") or {},
        }]
    if not isinstance(raw_queries, list) or not raw_queries:
        raise GraphToolInputError("queries 必须是非空数组")
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(raw_queries):
        if not isinstance(item, Mapping):
            raise GraphToolInputError("queries[{}] 必须是对象".format(index))
        element_class = str(item.get("element_class") or "").strip()
        element_type = str(item.get("element_type") or "").strip().upper()
        if not element_class:
            raise GraphToolInputError("queries[{}].element_class 不能为空".format(index))
        if element_type not in {"NODE", "EDGE"}:
            raise GraphToolInputError("queries[{}].element_type 仅支持 NODE 或 EDGE".format(index))
        filter_dict = item.get("filter_dict") or {}
        if not isinstance(filter_dict, Mapping):
            raise GraphToolInputError("queries[{}].filter_dict 必须是对象".format(index))
        query_id = str(item.get("query_id") or "").strip() or "q{}".format(index + 1)
        expanded_filters = _expand_simple_or_filter_dict(filter_dict, "queries[{}].filter_dict".format(index))
        for expanded_index, expanded_filter in enumerate(expanded_filters):
            normalized.append({
                "query_id": query_id if len(expanded_filters) == 1 else "{}_or{}".format(query_id, expanded_index + 1),
                "element_class": element_class,
                "element_type": element_type,
                "filter_dict": expanded_filter,
            })
    return normalized


def _expand_simple_or_filter_dict(filter_dict: Mapping[str, Any], field_name: str) -> list[dict[str, Any]]:
    """Normalize simple Mongo-style $or into the supported batched query form."""
    if not filter_dict:
        return [{}]

    logical_keys = [
        str(key or "").strip()
        for key in filter_dict
        if str(key or "").strip().lower() in {"$or", "$and", "$nor"}
    ]
    if not logical_keys:
        return [dict(filter_dict)]

    if len(logical_keys) != 1 or logical_keys[0].lower() != "$or":
        raise GraphToolInputError("{} 不支持顶层逻辑操作符 {}；请用 queries 拆分子查询".format(field_name, ", ".join(logical_keys)))

    raw_or = filter_dict.get(logical_keys[0])
    if not isinstance(raw_or, list) or not raw_or:
        raise GraphToolInputError("{}.{} 必须是非空对象数组".format(field_name, logical_keys[0]))

    common_filter = {
        str(key or "").strip(): value
        for key, value in filter_dict.items()
        if str(key or "").strip().lower() != "$or"
    }
    expanded: list[dict[str, Any]] = []
    for index, branch in enumerate(raw_or):
        branch_name = "{}.{}[{}]".format(field_name, logical_keys[0], index)
        if not isinstance(branch, Mapping):
            raise GraphToolInputError("{} 必须是对象".format(branch_name))
        branch_logical_keys = [
            str(key or "").strip()
            for key in branch
            if str(key or "").strip().lower() in {"$or", "$and", "$nor"}
        ]
        if branch_logical_keys:
            raise GraphToolInputError("{} 不支持嵌套逻辑操作符 {}；请用 queries 拆分子查询".format(branch_name, ", ".join(branch_logical_keys)))
        merged = dict(common_filter)
        for raw_key, value in branch.items():
            key = str(raw_key or "").strip()
            if not key:
                raise GraphToolInputError("{} 包含空字段名".format(branch_name))
            if key in merged and merged[key] != value:
                raise GraphToolInputError("{} 与公共过滤字段 {} 冲突".format(branch_name, key))
            merged[key] = value
        if not merged:
            raise GraphToolInputError("{} 不能为空对象".format(branch_name))
        expanded.append(merged)
    return expanded


def _optional_string_list(raw_value: Any) -> list[str] | None:
    if raw_value in (None, ""):
        return None
    if isinstance(raw_value, str):
        value = raw_value.strip()
        return [value] if value else None
    if not isinstance(raw_value, list):
        raise GraphToolInputError("字段必须是字符串数组")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_value:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned or None


def _normalize_pattern_return_properties(raw_value: Any) -> Any | None:
    if raw_value in (None, ""):
        return None
    if isinstance(raw_value, Mapping):
        normalized: dict[str, list[str]] = {}
        for raw_key, raw_properties in raw_value.items():
            key = str(raw_key or "").strip()
            if not key:
                continue
            normalized[key] = _string_list_preserve_empty(raw_properties)
        return normalized
    return _string_list_preserve_empty(raw_value)


def _string_list_preserve_empty(raw_value: Any) -> list[str]:
    if raw_value in (None, ""):
        return []
    if isinstance(raw_value, str):
        value = raw_value.strip()
        return [value] if value else []
    if not isinstance(raw_value, list):
        raise GraphToolInputError("return_properties 必须是字符串数组或变量到字符串数组的对象")
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_value:
        value = str(raw or "").strip()
        if not value or value in seen:
            continue
        seen.add(value)
        cleaned.append(value)
    return cleaned


def _validate_return_properties_fields(value: Any) -> None:
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip()
            if key == "return_properties":
                _validate_return_properties_value(nested)
            _validate_return_properties_fields(nested)
        return
    if isinstance(value, list):
        for item in value:
            _validate_return_properties_fields(item)


def _validate_return_properties_value(value: Any) -> None:
    if value in (None, ""):
        return
    if isinstance(value, Mapping):
        for nested in value.values():
            _validate_return_properties_value(nested)
        return
    if isinstance(value, str):
        _validate_return_property_names([value])
        return
    if isinstance(value, list):
        _validate_return_property_names([str(item or "").strip() for item in value if str(item or "").strip()])


def _validate_return_property_names(properties: list[str]) -> None:
    if any(prop == "*" or prop.lower() == "all" for prop in properties):
        raise GraphToolInputError("return_properties 必须列出明确字段名，不能使用 * 或 all")
    try:
        sanitize_return_properties(properties)
    except ValueError as exc:
        raise GraphToolInputError(str(exc)) from exc


def _validate_pattern_search_against_schema(
    *,
    path_pattern: list[Any],
    return_properties: Any | None,
    schema_context: Mapping[str, Any] | None,
) -> None:
    schema = _schema_index(schema_context)
    if not schema["node_labels"] and not schema["relationship_types"]:
        return
    segments = _pattern_segments(path_pattern)
    var_targets: dict[str, tuple[str, str]] = {}
    for segment in segments:
        role = segment.get("role")
        name = str(segment.get("name") or "").strip()
        var = str(segment.get("var") or "").strip()
        if var and role in {"node", "edge"} and name:
            var_targets[var] = (role, name)
        if role == "node":
            _validate_schema_name(name, schema["node_labels"], "节点 label")
            _validate_filter_fields(segment.get("props"), schema["node_fields"].get(name), "节点 {}".format(name))
        elif role == "edge":
            _validate_schema_name(name, schema["relationship_types"], "关系 type")
            _validate_filter_fields(segment.get("props"), schema["edge_fields"].get(name), "关系 {}".format(name))

    _validate_pattern_return_properties_against_schema(
        return_properties=return_properties,
        var_targets=var_targets,
        schema=schema,
    )


def _schema_index(schema_context: Mapping[str, Any] | None) -> dict[str, Any]:
    if not isinstance(schema_context, Mapping):
        return {
            "node_labels": set(),
            "relationship_types": set(),
            "node_fields": {},
            "edge_fields": {},
        }
    nodes_schema = schema_context.get("nodes_schema")
    edges_schema = schema_context.get("edges_schema")
    node_schema_map = (nodes_schema or {}).get("schema") if isinstance(nodes_schema, Mapping) else {}
    edge_schema_map = (edges_schema or {}).get("schema") if isinstance(edges_schema, Mapping) else {}
    node_fields = {
        str(label): set(str(field) for field in fields)
        for label, fields in (node_schema_map if isinstance(node_schema_map, Mapping) else {}).items()
        if isinstance(fields, Mapping)
    }
    edge_fields = {
        str(edge_type): set(str(field) for field in fields)
        for edge_type, fields in (edge_schema_map if isinstance(edge_schema_map, Mapping) else {}).items()
        if isinstance(fields, Mapping)
    }
    node_labels = {
        str(label).strip()
        for label in ((nodes_schema or {}).get("labels") if isinstance(nodes_schema, Mapping) else [])
        if str(label).strip()
    } | set(node_fields)
    relationship_types = {
        str(edge_type).strip()
        for edge_type in ((edges_schema or {}).get("relationship_types") if isinstance(edges_schema, Mapping) else [])
        if str(edge_type).strip()
    } | set(edge_fields)
    return {
        "node_labels": node_labels,
        "relationship_types": relationship_types,
        "node_fields": node_fields,
        "edge_fields": edge_fields,
    }


def _pattern_segments(path_pattern: list[Any]) -> list[dict[str, Any]]:
    segments: list[dict[str, Any]] = []
    for index, raw_segment in enumerate(path_pattern):
        segment = _pattern_segment(raw_segment, index)
        if segment:
            segments.append(segment)
    return segments


def _pattern_segment(raw_segment: Any, index: int) -> dict[str, Any]:
    if isinstance(raw_segment, Mapping):
        label = str(raw_segment.get("label") or "").strip()
        edge_type = str(raw_segment.get("type") or raw_segment.get("edge_type") or "").strip()
        role = "node" if label else "edge" if edge_type else ""
        return {
            "role": role,
            "name": label or edge_type,
            "var": str(raw_segment.get("var") or "").strip(),
            "props": _segment_props(raw_segment),
        }
    if isinstance(raw_segment, list) and raw_segment:
        selector = raw_segment[0]
        props = {}
        for item in raw_segment[1:]:
            if isinstance(item, Mapping):
                props.update(_segment_props(item))
        if isinstance(selector, Mapping):
            label = str(selector.get("label") or "").strip()
            edge_type = str(selector.get("type") or selector.get("edge_type") or "").strip()
            role = "node" if label else "edge" if edge_type else ""
            props.update(_segment_props(selector))
            return {
                "role": role,
                "name": label or edge_type,
                "var": str(selector.get("var") or "").strip(),
                "props": props,
            }
        name = str(selector or "").strip()
        if not name:
            return {}
        role = "edge" if index % 2 == 1 or _segment_has_direction(raw_segment[1:]) else "node"
        return {"role": role, "name": name, "var": "", "props": props}
    return {}


def _segment_props(segment: Mapping[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for key in ("props", "filters"):
        value = segment.get(key)
        if isinstance(value, Mapping):
            props.update(dict(value))
    return props


def _segment_has_direction(values: list[Any]) -> bool:
    return any(str(value).strip() in {"->", "<-", "--"} for value in values)


def _validate_schema_name(name: str, allowed: set[str], kind: str) -> None:
    if not name or not allowed:
        return
    if name not in allowed:
        raise GraphToolInputError("{} 不存在于预取图 schema: {}".format(kind, name))


def _validate_filter_fields(raw_props: Any, allowed_fields: set[str] | None, target: str) -> None:
    if not raw_props or not allowed_fields:
        return
    if not isinstance(raw_props, Mapping):
        return
    unknown = [
        str(field).strip()
        for field in raw_props
        if str(field).strip() and str(field).strip() not in allowed_fields
    ]
    if unknown:
        raise GraphToolInputError("{} 过滤字段不存在于预取图 schema: {}".format(target, ", ".join(unknown)))


def _validate_pattern_return_properties_against_schema(
    *,
    return_properties: Any | None,
    var_targets: Mapping[str, tuple[str, str]],
    schema: Mapping[str, Any],
) -> None:
    if return_properties in (None, ""):
        return
    if isinstance(return_properties, Mapping):
        for raw_var, raw_properties in return_properties.items():
            var = str(raw_var or "").strip()
            target = var_targets.get(var)
            if not target:
                continue
            allowed = _fields_for_target(target, schema)
            _validate_return_properties_for_target(raw_properties, allowed, "变量 {}".format(var))
        return
    if isinstance(return_properties, list):
        allowed_all = set()
        for target in var_targets.values():
            allowed_all.update(_fields_for_target(target, schema))
        _validate_return_properties_for_target(return_properties, allowed_all, "pattern_search")


def _fields_for_target(target: tuple[str, str], schema: Mapping[str, Any]) -> set[str]:
    role, name = target
    if role == "node":
        return set((schema.get("node_fields") or {}).get(name) or set())
    if role == "edge":
        return set((schema.get("edge_fields") or {}).get(name) or set())
    return set()


def _validate_return_properties_for_target(raw_properties: Any, allowed_fields: set[str], target: str) -> None:
    if not allowed_fields:
        return
    properties = _string_list_preserve_empty(raw_properties)
    unknown = [prop for prop in properties if prop not in allowed_fields]
    if unknown:
        raise GraphToolInputError("{} return_properties 不存在于预取图 schema: {}".format(target, ", ".join(unknown)))


def _positive_int(raw_value: Any, *, default: int, upper: int) -> int:
    try:
        value = int(raw_value if raw_value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise GraphToolInputError("数值字段必须是整数") from exc
    if value <= 0:
        raise GraphToolInputError("数值字段必须大于 0")
    return min(value, upper)


def _non_negative_int(raw_value: Any, *, default: int) -> int:
    try:
        value = int(raw_value if raw_value not in (None, "") else default)
    except (TypeError, ValueError) as exc:
        raise GraphToolInputError("offset 必须是整数") from exc
    if value < 0:
        raise GraphToolInputError("offset 不能小于 0")
    return value


def _reject_scope_fields(value: Any, forbidden: set[str]) -> None:
    if not forbidden:
        return
    if isinstance(value, Mapping):
        for raw_key, nested in value.items():
            key = str(raw_key or "").strip()
            if key in forbidden:
                raise GraphToolInputError("内部图工具参数不允许包含 scope/internal 字段: {}".format(key))
            _reject_scope_fields(nested, forbidden)
        return
    if isinstance(value, list):
        for item in value:
            _reject_scope_fields(item, forbidden)


def _forbidden_scope_fields(context: GraphSearchRuntimeContext) -> set[str]:
    fields = {
        "d2o_workspace_id",
        "d2o_ontology_id",
        "_d2o_record_key",
        "workspace_name",
        "scene_name",
        "user_id",
    }
    if context.scope.workspace_property:
        fields.add(context.scope.workspace_property)
    if context.scope.ontology_property:
        fields.add(context.scope.ontology_property)
    return fields


def _success_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {"result": payload, "success": True, "message": {"description": "查询成功"}}
