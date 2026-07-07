"""openJiuwen-backed internal graph query agent."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeoutError
from copy import copy
from dataclasses import is_dataclass, replace
import hashlib
import importlib.util
import json
import threading
import uuid
from collections.abc import Mapping
from inspect import isawaitable
from typing import Any

from ....loop.jiuwen.common import JiuwenBackendUnavailable, format_jiuwen_import_error
from ....loop.jiuwen.tools import safe_jiuwen_resource_part
from ....services.llm import LLMPurpose, get_llm_router
from .agent import (
    GraphAgentError,
    GraphAgentTimeoutError,
    GraphAgentToolInputError,
    GraphAgentUnavailableError,
    _agent_result_from_executions,
    _build_agent_input,
    _build_graph_agent_system_prompt,
    _format_timeout,
    _graph_agent_max_iterations,
    _graph_agent_timeout_seconds,
    _graph_tool_schemas_for_context,
    _path_intent_missing_pattern,
    _path_missing_pattern_warnings,
    _path_pattern_retry_message,
    _safe_exception_detail,
    _summarize_tool_execution,
)
from .models import GraphAgentResult, GraphEntityMapping, GraphSearchRuntimeContext
from .tools.executor import DATA_TOOLS, GraphToolExecution, GraphToolExecutor, GraphToolInputError


_JIUWEN_GRAPH_TOOL_REGISTRATION_LOCK = threading.RLock()


class JiuwenGraphQueryAgent:
    """Provider-internal graph query agent implemented with openJiuwen ReAct."""

    def __init__(
        self,
        *,
        agent_factory: Any | None = None,
        tool_registrar: Any | None = None,
        invoker: Any | None = None,
    ) -> None:
        self._agent_factory = agent_factory
        self._tool_registrar = tool_registrar or register_graph_runtime_tools
        self._invoker = invoker

    def run(
        self,
        *,
        context: GraphSearchRuntimeContext,
        mapping: GraphEntityMapping,
        tool_executor: GraphToolExecutor,
    ) -> GraphAgentResult:
        timeout_seconds = _graph_agent_timeout_seconds(
            provider_config=context.provider_config,
            outer_timeout_seconds=context.timeout_seconds,
        )
        executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="jiuwen-graph-query-agent")
        future = executor.submit(
            self._run_without_timeout,
            context=context,
            mapping=mapping,
            tool_executor=tool_executor,
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

    def _run_without_timeout(
        self,
        *,
        context: GraphSearchRuntimeContext,
        mapping: GraphEntityMapping,
        tool_executor: GraphToolExecutor,
    ) -> GraphAgentResult:
        if self._agent_factory is None and self._invoker is None:
            try:
                _require_openjiuwen()
            except JiuwenBackendUnavailable as exc:
                raise GraphAgentUnavailableError(str(exc)) from exc

        tool_schemas = _graph_tool_schemas_for_context(context)
        executions: list[GraphToolExecution] = []
        executed_steps: list[dict[str, Any]] = []
        adapter = GraphJiuwenToolAdapter(
            tool_executor=tool_executor,
            executions=executions,
            executed_steps=executed_steps,
        )
        final_message = ""
        input_payload = _build_agent_input(context=context, mapping=mapping, tool_schemas=tool_schemas)
        attempt = 1
        final_message = self._invoke_once(
            context=context,
            tool_schemas=tool_schemas,
            adapter=adapter,
            query=json.dumps(input_payload, ensure_ascii=False, default=str),
            attempt=attempt,
        )

        if not executions and adapter.last_tool_input_error is None and final_message.strip():
            attempt += 1
            executed_steps.append({
                "tool": "GraphQueryAgent",
                "summary": "上一轮未调用内部图工具，使用隔离会话重试",
            })
            final_message = self._invoke_once(
                context=context,
                tool_schemas=tool_schemas,
                adapter=adapter,
                query=_no_tool_retry_query(context=context, final_message=final_message),
                attempt=attempt,
            )

        if executions and _path_intent_missing_pattern(context=context, executions=executions):
            attempt += 1
            executed_steps.append({
                "tool": "GraphQueryAgent",
                "summary": "关系/路径意图尚未尝试 pattern_search，追加一次路径检索修正",
            })
            final_message = self._invoke_once(
                context=context,
                tool_schemas=tool_schemas,
                adapter=adapter,
                query=_retry_query_with_context(context=context, executions=executions),
                attempt=attempt,
            )

        if executions:
            return _agent_result_from_executions(
                executions=executions,
                executed_steps=executed_steps,
                final_message=final_message,
                warnings=_path_missing_pattern_warnings(context, executions),
            )
        last_error = adapter.last_error
        if adapter.last_tool_input_error is not None:
            raise GraphAgentToolInputError(str(adapter.last_tool_input_error))
        if final_message.strip():
            last_error = "GraphQueryAgent 未调用内部图检索工具: {}".format(final_message.strip()[:200])
        raise GraphAgentError(last_error or "GraphQueryAgent 未调用可用图检索工具")

    def _invoke_once(
        self,
        *,
        context: GraphSearchRuntimeContext,
        tool_schemas: list[dict[str, Any]],
        adapter: "GraphJiuwenToolAdapter",
        query: str,
        attempt: int,
    ) -> str:
        invocation_id = _graph_invocation_id(context=context, attempt=attempt)
        if self._invoker is not None:
            raw_result = self._invoker({
                "context": context,
                "tool_schemas": tool_schemas,
                "adapter": adapter,
                "query": query,
                "attempt": attempt,
                "invocation_id": invocation_id,
            })
            return _extract_output(raw_result)

        try:
            create_agent = None
            run_awaitable = None
            if self._agent_factory is None:
                create_agent, run_awaitable = _load_jiuwen_runtime_helpers()
            agent = (
                self._agent_factory()
                if self._agent_factory is not None
                else create_agent(
                    llm_config=_resolve_graph_jiuwen_llm_config(context.provider_config),
                    max_iterations=_graph_agent_max_iterations(context.provider_config),
                    system_prompt=_build_graph_agent_system_prompt(context.schema_context),
                    agent_id=_graph_agent_resource_id(
                        context,
                        attempt=attempt,
                        invocation_id=invocation_id,
                    ),
                    enable_reasoning=False,
                    stream_enabled=False,
                )
            )
            self._tool_registrar(
                agent,
                tool_schemas=tool_schemas,
                adapter=adapter,
                context=context,
                attempt=attempt,
                invocation_id=invocation_id,
            )
            invoke = getattr(agent, "invoke", None)
            if not callable(invoke):
                raise GraphAgentUnavailableError("Configured Jiuwen graph agent does not expose invoke().")
            raw_result = invoke({
                "query": query,
                "conversation_id": _graph_conversation_id(
                    context,
                    attempt=attempt,
                    invocation_id=invocation_id,
                ),
            })
            if isawaitable(raw_result):
                if run_awaitable is None:
                    _, run_awaitable = _load_jiuwen_runtime_helpers()
                raw_result = run_awaitable(raw_result)
            if _is_error_result(raw_result):
                raise GraphAgentError(_extract_output(raw_result) or "Jiuwen graph agent returned an error.")
            return _extract_output(raw_result)
        except GraphAgentError:
            raise
        except JiuwenBackendUnavailable as exc:
            raise GraphAgentUnavailableError(str(exc)) from exc
        except RuntimeError as exc:
            raise GraphAgentUnavailableError(
                "GraphQueryAgent LLM 不可用: {}".format(_safe_exception_detail(exc))
            ) from exc
        except Exception as exc:
            raise GraphAgentError(
                "GraphQueryAgent 调用失败: {}".format(_safe_exception_detail(exc))
            ) from exc


class GraphJiuwenToolAdapter:
    """Expose graph-internal tools to openJiuwen and collect data executions."""

    def __init__(
        self,
        *,
        tool_executor: GraphToolExecutor,
        executions: list[GraphToolExecution],
        executed_steps: list[dict[str, Any]],
    ) -> None:
        self.tool_executor = tool_executor
        self.executions = executions
        self.executed_steps = executed_steps
        self.last_error = "GraphQueryAgent 未调用可用图检索工具"
        self.last_tool_input_error: GraphToolInputError | None = None
        self._property_query_history: list[dict[str, Any]] = []
        self._lock = threading.Lock()

    async def invoke(self, tool_name: str, tool_args: Any) -> str:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, self._invoke_sync, tool_name, tool_args)

    def _invoke_sync(self, tool_name: str, tool_args: Any) -> str:
        name = str(tool_name or "").strip()
        args = _normalize_jiuwen_graph_tool_args(tool_args)
        with self._lock:
            if name == "pattern_search" and self._should_skip_pattern_search_after_record_hit():
                self.last_tool_input_error = None
                return json.dumps(
                    _skipped_pattern_search_payload(),
                    ensure_ascii=False,
                    default=str,
                )
            if name == "property_filter":
                args, skipped = self._filter_redundant_property_queries(args)
                if skipped:
                    self.executed_steps.append({
                        "tool": name,
                        "summary": "跳过重复 property_filter 子查询 {} 个".format(len(skipped)),
                    })
                if skipped and not _property_queries_from_args(args):
                    self.last_tool_input_error = None
                    return json.dumps(
                        _skipped_property_filter_payload(skipped),
                        ensure_ascii=False,
                        default=str,
                    )
            try:
                execution = self.tool_executor.execute(name, args)
            except GraphToolInputError as exc:
                self.last_tool_input_error = exc
                self.last_error = str(exc)
                self.executed_steps.append({
                    "tool": name or "<empty>",
                    "summary": "内部图工具参数错误: {}".format(str(exc)),
                })
                return json.dumps(
                    {
                        "success": False,
                        "error_code": exc.error_code,
                        "error": str(exc),
                        "retryable": exc.retryable,
                    },
                    ensure_ascii=False,
                    default=str,
                )
            self.last_tool_input_error = None
            if execution.backend_result is not None and execution.tool_name in DATA_TOOLS:
                self.executions.append(execution)
            if execution.tool_name == "property_filter":
                self._remember_property_queries(args, execution)
            self.executed_steps.append({
                "tool": execution.tool_name,
                "summary": _summarize_tool_execution(execution),
            })
            return json.dumps(execution.payload, ensure_ascii=False, default=str)

    def _should_skip_pattern_search_after_record_hit(self) -> bool:
        context = self.tool_executor.context
        mapping_intent = context.schema_context.get("mapping_intent") if isinstance(context.schema_context, Mapping) else None
        if not isinstance(mapping_intent, Mapping):
            return False
        if not bool(mapping_intent.get("property_filter_sufficient_after_hit")):
            return False
        if bool(mapping_intent.get("pattern_search_required")):
            return False
        return any(_execution_payload_marks_sufficient(execution) for execution in self.executions)

    def _filter_redundant_property_queries(self, args: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        queries = _property_queries_from_args(args)
        if not queries:
            return args, []
        kept_queries: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        current_signatures: set[str] = set()
        for query in queries:
            signature = _property_query_signature(query)
            if signature in current_signatures:
                reason = "exact_duplicate_in_same_call"
            else:
                reason = self._skip_reason_for_property_query(query)
            if reason:
                skipped.append({
                    "query_id": query.get("query_id") or "",
                    "element_class": query.get("element_class") or "",
                    "element_type": query.get("element_type") or "",
                    "filter_dict": query.get("filter_dict") or {},
                    "reason": reason,
                })
            else:
                kept_queries.append(query)
                current_signatures.add(signature)
        if len(kept_queries) == len(queries):
            return args, []
        next_args = dict(args)
        if "queries" in args:
            next_args["queries"] = [
                _property_query_to_tool_args(query)
                for query in kept_queries
            ]
        elif kept_queries:
            next_args.update(_property_query_to_tool_args(kept_queries[0]))
        else:
            next_args.pop("queries", None)
            for key in ("element_class", "element_type", "filter_dict", "return_properties", "limit", "offset"):
                next_args.pop(key, None)
        return next_args, skipped

    def _skip_reason_for_property_query(self, query: Mapping[str, Any]) -> str:
        signature = _property_query_signature(query)
        for item in self._property_query_history:
            if signature == item.get("signature") and item.get("had_data"):
                return "exact_duplicate_after_hit"
            if _is_broader_fuzzy_fallback(query, item):
                return "broader_fuzzy_fallback_after_exact_hit"
        return ""

    def _remember_property_queries(self, args: Mapping[str, Any], execution: GraphToolExecution) -> None:
        queries = _property_queries_from_args(args)
        if not queries:
            return
        rows = _property_result_rows(execution.payload)
        for index, query in enumerate(queries):
            row = rows[index] if index < len(rows) else {}
            count = _safe_int(row.get("count"), default=0)
            truncated = bool(row.get("truncated"))
            self._property_query_history.append({
                "signature": _property_query_signature(query),
                "element_class": str(query.get("element_class") or ""),
                "element_type": str(query.get("element_type") or ""),
                "filter_dict": dict(query.get("filter_dict") or {}),
                "return_properties": _normalize_return_properties(query.get("return_properties")),
                "had_data": count > 0,
                "truncated": truncated,
            })


def register_graph_runtime_tools(
    agent: Any,
    *,
    tool_schemas: list[dict[str, Any]],
    adapter: GraphJiuwenToolAdapter,
    context: GraphSearchRuntimeContext,
    attempt: int = 1,
    invocation_id: str = "",
) -> list[Any]:
    try:
        tool_module = __import__("openjiuwen.core.foundation.tool", fromlist=["Tool", "ToolCard"])
        runner_module = __import__("openjiuwen.core.runner", fromlist=["Runner"])
    except ImportError as exc:
        raise JiuwenBackendUnavailable(format_jiuwen_import_error(exc)) from exc

    tool_cls = tool_module.Tool
    tool_card_cls = tool_module.ToolCard
    cards = []
    tools = []
    for schema in tool_schemas:
        function = schema.get("function") if isinstance(schema, Mapping) else None
        if not isinstance(function, Mapping):
            continue
        name = str(function.get("name") or "").strip()
        if not name:
            continue
        card = tool_card_cls(
            id=_graph_tool_resource_id(
                agent=agent,
                context=context,
                tool_name=name,
                attempt=attempt,
                invocation_id=invocation_id,
            ),
            name=name,
            description=str(function.get("description") or ""),
            input_params=function.get("parameters") or {},
        )
        tools.append(_build_graph_tool_wrapper(tool_cls, card, adapter, name))
        cards.append(card)

    with _JIUWEN_GRAPH_TOOL_REGISTRATION_LOCK:
        ability_manager = getattr(agent, "ability_manager", None)
        add_ability = getattr(ability_manager, "add", None)
        if callable(add_ability) and cards:
            add_ability(cards)

        resource_mgr = runner_module.Runner.resource_mgr
        add_tool = getattr(resource_mgr, "add_tool", None)
        if callable(add_tool) and tools:
            add_tool(
                tools if len(tools) > 1 else tools[0],
                tag=getattr(getattr(agent, "card", None), "id", None),
                refresh=True,
            )
    return cards


def _require_openjiuwen() -> None:
    if importlib.util.find_spec("openjiuwen") is not None:
        return
    raise JiuwenBackendUnavailable(
        "Jiuwen ReAct runtime requires the 'openjiuwen' package. "
        "Install backend dependencies before using graph business_data_search."
    )


def _load_jiuwen_runtime_helpers() -> tuple[Any, Any]:
    try:
        from ....loop.jiuwen_adapter import _run_awaitable, create_default_jiuwen_agent
    except ImportError as exc:
        raise JiuwenBackendUnavailable(format_jiuwen_import_error(exc)) from exc
    return create_default_jiuwen_agent, _run_awaitable


def _build_graph_tool_wrapper(
    tool_cls: Any,
    card: Any,
    adapter: GraphJiuwenToolAdapter,
    tool_name: str,
) -> Any:
    class _GraphRuntimeJiuwenTool(tool_cls):
        async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
            del kwargs
            return await adapter.invoke(tool_name, inputs)

        async def stream(self, inputs: Any, **kwargs: Any) -> Any:
            yield await self.invoke(inputs, **kwargs)

    return _GraphRuntimeJiuwenTool(card=card)


def _resolve_graph_jiuwen_llm_config(provider_config: Mapping[str, Any]) -> Any:
    try:
        router = get_llm_router()
        profile_id = str(provider_config.get("model_profile_id") or "").strip() or None
        llm_config = router.get_config(LLMPurpose.TASK, profile_id=profile_id)
    except Exception as exc:
        raise GraphAgentUnavailableError(
            "GraphQueryAgent LLM router 未配置: {}".format(_safe_exception_detail(exc))
        ) from exc
    max_tokens = _positive_int(provider_config.get("graph_agent_max_tokens"), default=1600)
    temperature = _optional_float(provider_config.get("graph_agent_temperature"), default=0.0)
    return _clone_llm_config(llm_config, max_tokens=max_tokens, temperature=temperature)


def _clone_llm_config(llm_config: Any, *, max_tokens: int, temperature: float) -> Any:
    if is_dataclass(llm_config):
        return replace(llm_config, max_tokens=max_tokens, temperature=temperature)
    cloned = copy(llm_config)
    setattr(cloned, "max_tokens", max_tokens)
    setattr(cloned, "temperature", temperature)
    return cloned


def _normalize_jiuwen_graph_tool_args(tool_args: Any) -> dict[str, Any]:
    if isinstance(tool_args, Mapping):
        return dict(tool_args)
    if isinstance(tool_args, str):
        text = tool_args.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return {}
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _property_queries_from_args(args: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_queries = args.get("queries")
    source_queries: list[Any]
    if isinstance(raw_queries, list):
        source_queries = raw_queries
    elif any(key in args for key in ("element_class", "element_type", "filter_dict")):
        source_queries = [args]
    else:
        return []
    queries: list[dict[str, Any]] = []
    for index, raw_query in enumerate(source_queries):
        if not isinstance(raw_query, Mapping):
            continue
        element_class = str(raw_query.get("element_class") or "").strip()
        element_type = str(raw_query.get("element_type") or "").strip().upper()
        filter_dict = raw_query.get("filter_dict") or {}
        if not element_class or not element_type or not isinstance(filter_dict, Mapping):
            continue
        query: dict[str, Any] = {
            "query_id": str(raw_query.get("query_id") or "q{}".format(index + 1)),
            "element_class": element_class,
            "element_type": element_type,
            "filter_dict": dict(filter_dict),
            "return_properties": _normalize_return_properties(raw_query.get("return_properties")),
        }
        for key in ("limit", "offset"):
            if raw_query.get(key) not in (None, ""):
                query[key] = raw_query.get(key)
        queries.append(query)
    return queries


def _property_query_to_tool_args(query: Mapping[str, Any]) -> dict[str, Any]:
    payload = {
        "query_id": query.get("query_id") or "",
        "element_class": query.get("element_class") or "",
        "element_type": query.get("element_type") or "",
        "filter_dict": dict(query.get("filter_dict") or {}),
    }
    if query.get("return_properties"):
        payload["return_properties"] = list(query.get("return_properties") or [])
    for key in ("limit", "offset"):
        if query.get(key) not in (None, ""):
            payload[key] = query.get(key)
    return payload


def _property_query_signature(query: Mapping[str, Any]) -> str:
    payload = {
        "element_class": str(query.get("element_class") or ""),
        "element_type": str(query.get("element_type") or ""),
        "filter_dict": _canonical_json(query.get("filter_dict") or {}),
        "return_properties": sorted(_normalize_return_properties(query.get("return_properties"))),
        "limit": query.get("limit"),
        "offset": query.get("offset"),
    }
    return _canonical_json(payload)


def _property_result_rows(payload: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    result = payload.get("result") if isinstance(payload, Mapping) else None
    if not isinstance(result, Mapping):
        return []
    rows = result.get("results")
    if isinstance(rows, list):
        return [row for row in rows if isinstance(row, Mapping)]
    return [result]


def _is_broader_fuzzy_fallback(query: Mapping[str, Any], history: Mapping[str, Any]) -> bool:
    if not history.get("had_data") or history.get("truncated"):
        return False
    if str(query.get("element_class") or "") != str(history.get("element_class") or ""):
        return False
    if str(query.get("element_type") or "") != str(history.get("element_type") or ""):
        return False
    if not _return_properties_cover(history.get("return_properties"), query.get("return_properties")):
        return False
    previous = _single_field_filter(history.get("filter_dict"), exact=True)
    current = _single_field_filter(query.get("filter_dict"), exact=False)
    if previous is None or current is None:
        return False
    previous_field, previous_value = previous
    current_field, current_value = current
    return previous_field == current_field and str(previous_value) == str(current_value)


def _single_field_filter(filter_dict: Any, *, exact: bool) -> tuple[str, Any] | None:
    if not isinstance(filter_dict, Mapping) or len(filter_dict) != 1:
        return None
    field, raw_value = next(iter(filter_dict.items()))
    field = str(field or "").strip()
    if not field:
        return None
    if exact:
        if isinstance(raw_value, Mapping):
            if set(raw_value) != {"eq"}:
                return None
            return field, raw_value.get("eq")
        return field, raw_value
    if not isinstance(raw_value, Mapping) or len(raw_value) != 1:
        return None
    operator, value = next(iter(raw_value.items()))
    if str(operator or "").strip() not in {"contains", "starts_with", "ends_with"}:
        return None
    return field, value


def _return_properties_cover(previous: Any, current: Any) -> bool:
    previous_set = set(_normalize_return_properties(previous))
    current_set = set(_normalize_return_properties(current))
    return not current_set or not previous_set or current_set.issubset(previous_set)


def _normalize_return_properties(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _skipped_property_filter_payload(skipped: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "success": True,
        "skipped": True,
        "skip_reason": "duplicate_or_broader_fallback_after_exact_hit",
        "result": {
            "items": [],
            "count": 0,
            "skipped_queries": skipped,
        },
        "message": {"description": "跳过重复 property_filter 查询"},
    }


def _skipped_pattern_search_payload() -> dict[str, Any]:
    return {
        "success": True,
        "skipped": True,
        "skip_reason": "record_query_already_satisfied_by_property_filter",
        "sufficient_for_query": True,
        "recommended_next_action": "final",
        "result": {
            "paths": [],
            "count": 0,
            "truncated": False,
        },
        "message": {"description": "property_filter 已命中记录型查询所需字段，跳过不必要的 pattern_search"},
    }


def _execution_payload_marks_sufficient(execution: GraphToolExecution) -> bool:
    if execution.tool_name not in {"property_filter", "sorted_search"}:
        return False
    payload = execution.payload if isinstance(execution.payload, Mapping) else {}
    if not bool(payload.get("sufficient_for_query")):
        return False
    result = execution.backend_result
    if result is None:
        return False
    total = result.total if result.total is not None else len(result.records or [])
    return _safe_int(total, default=0) > 0


def _safe_int(value: Any, *, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _retry_query_with_context(
    *,
    context: GraphSearchRuntimeContext,
    executions: list[GraphToolExecution],
) -> str:
    previews = []
    for execution in executions[-3:]:
        previews.append({
            "tool": execution.tool_name,
            "tool_args": execution.tool_args,
            "record_count": (
                execution.backend_result.total
                if execution.backend_result and execution.backend_result.total is not None
                else len(execution.backend_result.records or []) if execution.backend_result else 0
            ),
        })
    return "{}\n\n已有内部图工具结果摘要：\n{}".format(
        _path_pattern_retry_message(context),
        json.dumps(previews, ensure_ascii=False, default=str),
    )


def _no_tool_retry_query(
    *,
    context: GraphSearchRuntimeContext,
    final_message: str,
) -> str:
    return (
        "上一轮没有调用任何内部图检索工具，不能直接回答。\n"
        "本 Provider 必须调用 allowed_tools 中的内部图工具获取真实数据，"
        "禁止复用其它并发查询、历史上下文或自行编造表格。\n"
        "请基于本次 ontology_id/entity/query 重新选择 property_filter、pattern_search、"
        "count_search、aggregate_search 或 sorted_search 等工具，并只以工具结果作为事实来源。\n\n"
        "本次查询上下文：\n"
        "{}\n\n"
        "上一轮未采信输出摘要：\n{}"
    ).format(
        json.dumps(
            {
                "provider": context.provider,
                "ontology_id": context.ontology_id,
                "entity": context.entity,
                "query": context.query,
            },
            ensure_ascii=False,
            default=str,
        ),
        str(final_message or "").strip()[:800],
    )


def _extract_output(raw_result: Any) -> str:
    if isinstance(raw_result, Mapping):
        for key in ("output", "content", "final_output", "message"):
            value = raw_result.get(key)
            if value not in (None, ""):
                return str(value)
        return json.dumps(raw_result, ensure_ascii=False, default=str)
    for attr in ("output", "content", "final_output", "message"):
        value = getattr(raw_result, attr, None)
        if value not in (None, ""):
            return str(value)
    return "" if raw_result is None else str(raw_result)


def _is_error_result(raw_result: Any) -> bool:
    if isinstance(raw_result, Mapping):
        return str(raw_result.get("result_type") or "").strip().lower() == "error"
    return str(getattr(raw_result, "result_type", "") or "").strip().lower() == "error"


def _graph_invocation_id(context: GraphSearchRuntimeContext, *, attempt: int) -> str:
    digest_payload = {
        "provider": context.provider,
        "workspace_id": context.workspace_id,
        "ontology_id": context.ontology_id,
        "entity": context.entity,
        "query": context.query,
        "run_id": context.provider_config.get("run_id"),
        "session_id": context.provider_config.get("session_id"),
        "attempt": attempt,
    }
    digest = hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:12]
    return "q{}.{}".format(digest, uuid.uuid4().hex[:12])


def _graph_agent_resource_id(
    context: GraphSearchRuntimeContext,
    *,
    attempt: int,
    invocation_id: str,
) -> str:
    parts = [
        "onto_agent_jiuwen_graph",
        context.provider,
        context.workspace_id,
        context.provider_config.get("run_id"),
        context.provider_config.get("session_id"),
        context.entity,
        invocation_id,
        "attempt{}".format(attempt),
    ]
    return ".".join(safe_jiuwen_resource_part(part) for part in parts if str(part or "").strip())


def _graph_tool_resource_id(
    *,
    agent: Any,
    context: GraphSearchRuntimeContext,
    tool_name: str,
    attempt: int,
    invocation_id: str = "",
) -> str:
    agent_card = getattr(agent, "card", None)
    agent_id = str(getattr(agent_card, "id", "") or "").strip() or _graph_agent_resource_id(
        context,
        attempt=attempt,
        invocation_id=invocation_id or _graph_invocation_id(context=context, attempt=attempt),
    )
    return "{}.tool.{}".format(
        safe_jiuwen_resource_part(agent_id),
        safe_jiuwen_resource_part(tool_name),
    )


def _graph_conversation_id(
    context: GraphSearchRuntimeContext,
    *,
    attempt: int,
    invocation_id: str,
) -> str:
    raw = (
        str(context.provider_config.get("session_id") or "").strip()
        or str(context.provider_config.get("run_id") or "").strip()
        or "{}-{}".format(context.workspace_id, context.entity)
    )
    return "{}.graph.{}.{}.attempt{}".format(
        safe_jiuwen_resource_part(raw),
        safe_jiuwen_resource_part(context.entity),
        safe_jiuwen_resource_part(invocation_id),
        attempt,
    )


def _positive_int(value: Any, *, default: int) -> int:
    try:
        resolved = int(value)
    except (TypeError, ValueError):
        return default
    return max(1, resolved)


def _optional_float(value: Any, *, default: float) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
