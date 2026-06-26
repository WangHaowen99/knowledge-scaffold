"""LLM-backed internal graph tool loop for business_data_search."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
import json
from pathlib import Path
import re
from typing import Any, Mapping

from ....services.llm import LLMPurpose, get_llm_router
from ....utils.ontologies import get_ontology_version_dir
from .models import GraphAgentResult, GraphBackendResult, GraphEntityMapping, GraphSearchRuntimeContext
from .tools.executor import DATA_TOOLS, GraphToolExecution, GraphToolExecutor, GraphToolInputError


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
16. 示例：查询编码或名称包含 A001 时，应生成两个 queries 子项，分别使用 {"SourceItemCode":{"contains":"A001"}} 和 {"SourceItemDesc":{"contains":"A001"}}。

路径检索策略：
- schema_context.query_analysis.path_intent 为 true 时，应至少尝试一次 pattern_search，除非 schema_context 中完全没有可用 relation_triples。
- 若 object mapping 的 resource 是关系记录节点（例如 *_obj、*_TargetItem、SOURCE_STRUCTURE、STRUCTURE_LIST 等），可先用 property_filter 查记录字段，但不能把它当作已完成路径检索；问题需要关联对象详情时继续 pattern_search。
- 构造 path_pattern 时使用 relation_triples 的 source_label、relation_type、target_label；物理关系 type 通常来自 edges_schema.relationship_types 或 relation_triples.physical_relation_type/type。
- 对 D2O_RELATION 这类通用物理边，业务关系名通常在边属性 relation_type 中；可在边段 props/filters 中约束 relation_type。

可用内部工具以本次输入 allowed_tools 和实际 tools 列表为准；schema 工具只在 Provider 未预取到 schema 时提供。可能的内部工具包括：
- get_nodes_schema
- get_edges_schema
- property_filter
- pattern_search
- count_search
- aggregate_search
- sorted_search
- get_element_info

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
    retryable = True


class GraphAgentToolInputError(GraphAgentError):
    """Raised when GraphQueryAgent cannot recover from invalid internal tool args."""

    error_code = "GRAPH_TOOL_INPUT_ERROR"
    retryable = True


class GraphQueryAgent:
    """Provider-internal graph query agent backed by the existing task LLM."""

    def __init__(self, *, llm_call: Any | None = None) -> None:
        self._llm_call = llm_call

    def run(
        self,
        *,
        context: GraphSearchRuntimeContext,
        mapping: GraphEntityMapping,
        tool_executor: GraphToolExecutor,
    ) -> GraphAgentResult:
        tool_schemas = _graph_tool_schemas_for_context(context)
        messages = [
            {"role": "system", "content": _build_graph_agent_system_prompt(context.schema_context)},
            {
                "role": "user",
                "content": json.dumps(
                    _build_agent_input(context=context, mapping=mapping, tool_schemas=tool_schemas),
                    ensure_ascii=False,
                ),
            },
        ]
        max_iterations = _graph_agent_max_iterations(context.provider_config)
        executed_steps: list[dict[str, Any]] = []
        data_executions: list[GraphToolExecution] = []
        last_error = "GraphQueryAgent 未调用可用图检索工具"
        last_tool_input_error: GraphToolInputError | None = None
        path_retry_requested = False

        for iteration in range(1, max_iterations + 1):
            response = self._call_with_timeout(
                messages=messages,
                tool_schemas=tool_schemas,
                provider_config=context.provider_config,
                timeout_seconds=_graph_agent_timeout_seconds(
                    provider_config=context.provider_config,
                    outer_timeout_seconds=context.timeout_seconds,
                ),
            )
            tool_calls = _normalize_tool_calls(response.get("tool_calls"))
            content = str(response.get("content") or "")
            if not tool_calls:
                if data_executions:
                    if (
                        not path_retry_requested
                        and iteration < max_iterations
                        and _path_intent_missing_pattern(context=context, executions=data_executions)
                    ):
                        path_retry_requested = True
                        messages.append({
                            "role": "user",
                            "content": _path_pattern_retry_message(context),
                        })
                        executed_steps.append({
                            "tool": "GraphQueryAgent",
                            "summary": "关系/路径意图尚未尝试 pattern_search，追加一次路径检索修正",
                        })
                        continue
                    return _agent_result_from_executions(
                        executions=data_executions,
                        executed_steps=executed_steps,
                        final_message=content,
                        warnings=_path_missing_pattern_warnings(context, data_executions),
                    )
                if content.strip():
                    last_error = "GraphQueryAgent 未调用内部图检索工具: {}".format(content.strip()[:200])
                break

            assistant_message = {"role": "assistant", "content": content, "tool_calls": tool_calls}
            messages.append(assistant_message)
            for call in tool_calls:
                call_id = str(call.get("id") or "graph_tool_{}_{}".format(iteration, len(executed_steps) + 1))
                name = str((call.get("function") or {}).get("name") or "").strip()
                try:
                    arguments = _loads_tool_arguments((call.get("function") or {}).get("arguments"))
                    execution = tool_executor.execute(name, arguments)
                    tool_payload = execution.payload
                    if execution.backend_result is not None and execution.tool_name in DATA_TOOLS:
                        data_executions.append(execution)
                    executed_steps.append({
                        "tool": execution.tool_name,
                        "summary": _summarize_tool_execution(execution),
                    })
                except GraphToolInputError as exc:
                    last_tool_input_error = exc
                    tool_payload = {
                        "success": False,
                        "error_code": exc.error_code,
                        "error": str(exc),
                        "retryable": exc.retryable,
                    }
                    last_error = str(exc)
                    executed_steps.append({
                        "tool": name or "<empty>",
                        "summary": "内部图工具参数错误: {}".format(str(exc)),
                    })
                messages.append({
                    "role": "tool",
                    "tool_call_id": call_id,
                    "name": name,
                    "content": json.dumps(tool_payload, ensure_ascii=False, default=str),
                })
            if (
                _graph_agent_early_stop_enabled(context.provider_config)
                and _has_primary_mapping_result(
                    executions=data_executions,
                    mapping=mapping,
                )
                and not _path_intent_missing_pattern(context=context, executions=data_executions)
            ):
                return _agent_result_from_executions(
                    executions=data_executions,
                    executed_steps=executed_steps,
                    final_message="",
                )

        if data_executions:
            return _agent_result_from_executions(
                executions=data_executions,
                executed_steps=[
                    *executed_steps,
                    {"tool": "GraphQueryAgent", "summary": "达到最大轮次，使用已获得的真实图工具结果"},
                ],
                final_message="",
                warnings=[
                    "GraphQueryAgent 达到最大轮次，使用已获得的真实图工具结果",
                    *_path_missing_pattern_warnings(context, data_executions),
                ],
            )
        if last_tool_input_error is not None:
            raise GraphAgentToolInputError(str(last_tool_input_error))
        raise GraphAgentError(last_error)

    def _call_with_timeout(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        provider_config: Mapping[str, Any],
        timeout_seconds: float,
    ) -> dict[str, Any]:
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="graph-query-agent")
        future = executor.submit(
            self._call_llm,
            messages=list(messages),
            tool_schemas=list(tool_schemas),
            provider_config=provider_config,
        )
        try:
            return future.result(timeout=timeout_seconds)
        except FuturesTimeoutError as exc:
            future.cancel()
            raise GraphAgentTimeoutError(
                "GraphQueryAgent 调用超时（{}s）".format(_format_timeout(timeout_seconds))
            ) from exc
        finally:
            executor.shutdown(wait=False, cancel_futures=True)

    def _call_llm(
        self,
        *,
        messages: list[dict[str, Any]],
        tool_schemas: list[dict[str, Any]],
        provider_config: Mapping[str, Any],
    ) -> dict[str, Any]:
        if self._llm_call is not None:
            try:
                raw = self._llm_call({
                    "messages": messages,
                    "tools": tool_schemas,
                    "provider_config": dict(provider_config or {}),
                })
                return _normalize_llm_response(raw)
            except GraphAgentError:
                raise
            except RuntimeError as exc:
                raise GraphAgentUnavailableError(
                    "GraphQueryAgent LLM 不可用: {}".format(_safe_exception_detail(exc))
                ) from exc
            except Exception as exc:
                raise GraphAgentError(
                    "GraphQueryAgent 调用失败: {}".format(_safe_exception_detail(exc))
                ) from exc

        try:
            router = get_llm_router()
        except Exception as exc:
            raise GraphAgentUnavailableError(
                "GraphQueryAgent LLM router 未配置: {}".format(_safe_exception_detail(exc))
            ) from exc

        try:
            response = router.call(
                purpose=LLMPurpose.TASK,
                profile_id=str(provider_config.get("model_profile_id") or "").strip() or None,
                messages=messages,
                tools=tool_schemas,
                stream=False,
                temperature=0,
                max_tokens=int(provider_config.get("graph_agent_max_tokens") or 1600),
                enable_reasoning=False,
                run_id=str(provider_config.get("run_id") or "").strip() or None,
                request_label="graph_query_agent_tools",
            )
        except RuntimeError as exc:
            raise GraphAgentUnavailableError(
                "GraphQueryAgent LLM 不可用: {}".format(_safe_exception_detail(exc))
            ) from exc
        except Exception as exc:
            raise GraphAgentError(
                "GraphQueryAgent 调用失败: {}".format(_safe_exception_detail(exc))
            ) from exc
        return {
            "content": str(response.content or ""),
            "tool_calls": response.tool_calls or [],
        }


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
        version_dir = get_ontology_version_dir(ontology_root, create=False) or ontology_root
    except Exception:
        version_dir = ontology_root
    path = version_dir / "objects" / "{}.md".format(entity)
    if not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")[:6000]
    except OSError:
        return ""


def _normalize_llm_response(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        return {"content": raw, "tool_calls": []}
    if isinstance(raw, Mapping):
        return {
            "content": str(raw.get("content") or ""),
            "tool_calls": raw.get("tool_calls") or [],
        }
    return {
        "content": str(getattr(raw, "content", "") or ""),
        "tool_calls": getattr(raw, "tool_calls", None) or [],
    }


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


def _normalize_tool_calls(tool_calls: Any) -> list[dict[str, Any]]:
    if not isinstance(tool_calls, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, call in enumerate(tool_calls):
        if not isinstance(call, Mapping):
            continue
        function = call.get("function") or {}
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        normalized.append({
            "id": str(call.get("id") or "graph_tool_{}".format(index + 1)),
            "type": "function",
            "function": {
                "name": name,
                "arguments": function.get("arguments") or "{}",
            },
        })
    return normalized


def _loads_tool_arguments(raw: Any) -> dict[str, Any]:
    if isinstance(raw, Mapping):
        return dict(raw)
    text = str(raw or "").strip()
    if not text:
        return {}
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise GraphToolInputError("tool arguments 必须是 JSON object") from exc
    if not isinstance(payload, dict):
        raise GraphToolInputError("tool arguments 必须是 JSON object")
    return payload


def _agent_result_from_execution(
    *,
    execution: GraphToolExecution,
    executed_steps: list[dict[str, Any]],
    final_message: str,
    warnings: list[str] | None = None,
) -> GraphAgentResult:
    return GraphAgentResult(
        tool_name=execution.tool_name,
        tool_args=dict(execution.tool_args or {}),
        backend_result=execution.backend_result or GraphBackendResult(),
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
    total = 0
    truncated = False
    for execution in executions:
        backend_result = execution.backend_result or GraphBackendResult()
        record_count = backend_result.total if backend_result.total is not None else len(backend_result.records or [])
        total += int(record_count or 0)
        truncated = truncated or bool(backend_result.truncated)
        combined_warnings.extend(backend_result.warnings or [])
        combined_records.append({
            "tool": execution.tool_name,
            "tool_args": dict(execution.tool_args or {}),
            "record_count": record_count,
            "result": _compact_execution_result(execution),
            "records": list(backend_result.records or []),
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


def _graph_agent_timeout_seconds(
    *,
    provider_config: Mapping[str, Any],
    outer_timeout_seconds: int,
) -> float:
    raw = provider_config.get("graph_agent_timeout_seconds")
    if raw in (None, ""):
        raw = min(float(outer_timeout_seconds or 60), 30.0)
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = min(float(outer_timeout_seconds or 60), 30.0)
    return max(0.001, value)


def _graph_agent_max_iterations(provider_config: Mapping[str, Any]) -> int:
    try:
        value = int(provider_config.get("max_iterations") or 4)
    except (TypeError, ValueError):
        value = 4
    return max(1, min(value, 8))


def _graph_agent_early_stop_enabled(provider_config: Mapping[str, Any]) -> bool:
    raw = provider_config.get("graph_agent_early_stop_on_primary_result")
    if raw in (None, ""):
        return True
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() not in {"0", "false", "no", "off"}


def _has_primary_mapping_result(
    *,
    executions: list[GraphToolExecution],
    mapping: GraphEntityMapping,
) -> bool:
    primary_tools = _primary_tools_for_mapping(mapping)
    if not primary_tools:
        return False
    return any(
        execution.tool_name in primary_tools and _execution_has_data(execution)
        for execution in executions
    )


def _primary_tools_for_mapping(mapping: GraphEntityMapping) -> set[str]:
    kind = str(mapping.kind or "").strip().lower()
    if kind == "path":
        return {"pattern_search"}
    if kind in {"node", "edge"}:
        return {"property_filter", "count_search", "aggregate_search", "sorted_search"}
    return set()


def _execution_has_data(execution: GraphToolExecution) -> bool:
    result = execution.backend_result
    if result is None:
        return False
    if result.records:
        return True
    if result.total is None:
        return False
    try:
        return int(result.total) > 0
    except (TypeError, ValueError):
        return False


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
    return _query_requires_associated_object_detail(context)


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
        returned_properties.update(_returned_properties_from_tool_args(execution.tool_args))
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


def _returned_properties_from_tool_args(tool_args: Mapping[str, Any]) -> set[str]:
    returned: set[str] = set()
    raw_queries = tool_args.get("queries")
    if isinstance(raw_queries, list):
        for item in raw_queries:
            if isinstance(item, Mapping):
                returned.update(str(prop).strip() for prop in (item.get("return_properties") or []) if str(prop).strip())
    returned.update(str(prop).strip() for prop in (tool_args.get("return_properties") or []) if str(prop).strip())
    return returned


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


def _query_requires_associated_object_detail(context: GraphSearchRuntimeContext) -> bool:
    query = str(context.query or "")
    entity = str(context.entity or "")
    text = "{} {}".format(query, entity)
    relation_cues = ("对应", "关联", "连接", "映射", "追溯", "展开", "上下游", "上游", "下游")
    object_detail_cues = (
        "TargetItem",
        "SourceItem",
        "版本详情",
        "对象详情",
        "节点详情",
        "关联对象",
        "关联节点",
        "父项",
        "子项",
        "供应商",
    )
    if any(cue in text for cue in relation_cues) and any(cue in text for cue in object_detail_cues):
        return True
    if "编码" in query and any(cue in text for cue in relation_cues):
        return True
    return False


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


_GRAPH_SCHEMA_TOOL_NAMES = {"get_nodes_schema", "get_edges_schema"}


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
                "节点或关系属性过滤写在 props 中，例如 {\"label\":\"SourceItemVersion\",\"props\":{\"SourceItemCode\":\"101000-001\"}}。"
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
