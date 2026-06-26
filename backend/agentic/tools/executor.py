"""Tool batch execution: parallel and sequential dispatch over ToolOutcome."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextvars import copy_context
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..exceptions import WorkflowCancelledError
from ..utils.logging import get_logger
from ..utils.streaming import stream_writer_context, try_get_stream_writer
from .base import BaseTool, ToolBatchResult, ToolContext, ToolInvocation, ToolOutcome
from .events import emit_protocol_event, emit_tool_finished, emit_tool_started

logger = get_logger("tools.executor")


def execute_tool_batch(
    tool_calls: List[ToolInvocation],
    context: ToolContext,
) -> ToolBatchResult:
    """Execute every tool call uniformly, then aggregate outcomes."""
    if not tool_calls:
        return ToolBatchResult()

    subagent_event_emitter = _make_subagent_event_emitter(context)
    tasks: List[Dict[str, Any]] = []
    early_outputs: List[Dict[str, Any]] = []

    for index, tool_call in enumerate(tool_calls):
        task = _build_tool_task(
            tool_call,
            index=index,
            context=context,
            subagent_event_emitter=subagent_event_emitter,
        )
        if "prebuilt_output" in task:
            early_outputs.append(task["prebuilt_output"])
            continue
        tasks.append(task)

    outputs = list(early_outputs)
    pending_safe_batch: List[Dict[str, Any]] = []

    def flush_safe_batch() -> None:
        nonlocal pending_safe_batch
        if not pending_safe_batch:
            return
        outputs.extend(_execute_parallel_batch(pending_safe_batch, context, subagent_event_emitter))
        pending_safe_batch = []

    for task in tasks:
        if _outputs_contain_terminal(outputs):
            break
        if task["spec"].concurrency_safe:
            pending_safe_batch.append(task)
            continue
        flush_safe_batch()
        if _outputs_contain_terminal(outputs):
            break
        outputs.append(_execute_single_task(task, context, subagent_event_emitter))
        if _outputs_contain_terminal(outputs):
            break
    if not _outputs_contain_terminal(outputs):
        flush_safe_batch()

    outputs.sort(key=lambda item: item["index"])
    return _aggregate_outputs(outputs)


def _outputs_contain_terminal(outputs: List[Dict[str, Any]]) -> bool:
    for output in outputs:
        outcome = output.get("outcome") if isinstance(output, Mapping) else None
        if bool(getattr(outcome, "terminal", False)):
            return True
    return False


def _aggregate_outputs(outputs: List[Dict[str, Any]]) -> ToolBatchResult:
    messages: List[Dict[str, Any]] = []
    state_updates: Dict[str, Any] = {}
    tool_intervals_ms: List[Dict[str, int]] = []
    terminal = False

    for output in outputs:
        outcome: ToolOutcome = output["outcome"]
        interval = output.get("tool_interval_ms")
        if isinstance(interval, dict):
            tool_intervals_ms.append(interval)
        if outcome.content is not None:
            messages.append(
                {
                    "role": "tool",
                    "content": outcome.content,
                    "tool_call_id": output["tool_call_id"],
                    "name": output["name"],
                }
            )
        if outcome.state_updates:
            for key, value in outcome.state_updates.items():
                if key in {"read_document_records", "evolution_recall_records"} and isinstance(value, list):
                    existing = list(state_updates.get(key) or [])
                    existing.extend(value)
                    state_updates[key] = existing
                else:
                    state_updates[key] = value
        if outcome.terminal:
            terminal = True

    return ToolBatchResult(
        messages=messages,
        state_updates=state_updates,
        terminal=terminal,
        tool_intervals_ms=tool_intervals_ms,
    )


def _build_tool_task(
    tool_call: ToolInvocation,
    *,
    index: int,
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Dict[str, Any]:
    from .registry import get_tool_spec

    if not isinstance(tool_call, ToolInvocation):
        raise TypeError("execute_tool_batch expects ToolInvocation instances")

    raw_args = tool_call.args if isinstance(tool_call.args, Mapping) else {}
    name = str(tool_call.name or "")
    args = dict(raw_args)
    tool_call_id = tool_call.tool_call_id
    if tool_call.index is not None:
        index = int(tool_call.index)
    spec = get_tool_spec(name)
    safe_name = name or "unknown_tool"

    progress_message = _build_tool_progress_message(name=name, args=args) or "调用工具: {}".format(
        safe_name
    )

    if not spec:
        raw_output = "Error: Tool '{}' not found.".format(safe_name)
        emit_tool_started(
            context=context,
            tool_name=safe_name,
            tool_call_id=tool_call_id,
            tool_input=args,
            message=progress_message,
        )
        emit_tool_finished(
            context=context,
            tool_name=safe_name,
            tool_call_id=tool_call_id,
            status="error",
            raw_output=raw_output,
            error=raw_output,
            elapsed_ms=0,
        )
        now_ms = _now_monotonic_ms()
        return {
            "index": index,
            "tool_call_id": tool_call_id,
            "name": safe_name,
            "prebuilt_output": {
                "index": index,
                "tool_call_id": tool_call_id,
                "name": safe_name,
                "outcome": ToolOutcome(content=raw_output),
                "tool_interval_ms": {
                    "started_at_ms": now_ms,
                    "finished_at_ms": now_ms,
                    "elapsed_ms": 0,
                },
            },
        }

    if context.caller not in spec.allowed_callers:
        raw_output = "Error: Tool '{}' is not available for {} agents.".format(
            safe_name,
            context.caller,
        )
        emit_tool_started(
            context=context,
            tool_name=safe_name,
            tool_call_id=tool_call_id,
            tool_input=args,
            message=progress_message,
        )
        emit_tool_finished(
            context=context,
            tool_name=safe_name,
            tool_call_id=tool_call_id,
            status="error",
            raw_output=raw_output,
            error=raw_output,
            elapsed_ms=0,
        )
        now_ms = _now_monotonic_ms()
        return {
            "index": index,
            "tool_call_id": tool_call_id,
            "name": safe_name,
            "prebuilt_output": {
                "index": index,
                "tool_call_id": tool_call_id,
                "name": safe_name,
                "outcome": ToolOutcome(content=raw_output),
                "tool_interval_ms": {
                    "started_at_ms": now_ms,
                    "finished_at_ms": now_ms,
                    "elapsed_ms": 0,
                },
            },
        }

    emit_tool_started(
        context=context,
        tool_name=safe_name,
        tool_call_id=tool_call_id,
        tool_input=args,
        message=progress_message,
    )

    return {
        "index": index,
        "name": safe_name,
        "args": args,
        "tool_call_id": tool_call_id,
        "spec": spec,
        "subagent_event_emitter": subagent_event_emitter,
    }


def _execute_parallel_batch(
    batch: List[Dict[str, Any]],
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> List[Dict[str, Any]]:
    global_limit = max(1, int(context.agent_config.parallel_default_max_workers or 1))
    captured_writer = try_get_stream_writer()
    tool_limits = {
        task["name"]: threading.BoundedSemaphore(
            max(1, int(task["spec"].max_parallelism or global_limit))
        )
        for task in batch
    }
    outputs_by_index: Dict[int, Dict[str, Any]] = {}

    def run_task(task: Dict[str, Any]) -> Dict[str, Any]:
        semaphore = tool_limits[task["name"]]
        with semaphore:
            if captured_writer:
                with stream_writer_context(captured_writer):
                    return _execute_single_task(task, context, subagent_event_emitter)
            return _execute_single_task(task, context, subagent_event_emitter)

    def run_task_with_context(captured_context, task: Dict[str, Any]) -> Dict[str, Any]:
        return captured_context.run(run_task, task)

    with ThreadPoolExecutor(max_workers=min(global_limit, len(batch))) as executor:
        future_map = {
            executor.submit(run_task_with_context, copy_context(), task): task
            for task in batch
        }
        for future in as_completed(future_map):
            task = future_map[future]
            outputs_by_index[task["index"]] = future.result()

    return [outputs_by_index[task["index"]] for task in batch]


def _execute_single_task(
    task: Dict[str, Any],
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Dict[str, Any]:
    from .registry import validate_tool_arguments

    name = task["name"]
    spec: BaseTool = task["spec"]
    args = task["args"]
    tool_call_id = task.get("tool_call_id")
    started_at_ms = _now_monotonic_ms()
    error_message: Optional[str] = None

    validation_error = validate_tool_arguments(spec.name, args, tool_schema=spec.schema)
    if validation_error:
        logger.warning("Skipping tool %s due to invalid arguments: %s", name, validation_error)
        outcome = ToolOutcome(content=validation_error)
        terminal_status = "error"
        error_message = validation_error
    else:
        prerequisite_error = _validate_tool_prerequisites(name=name, args=args, context=context)
        if prerequisite_error:
            logger.warning("Skipping tool %s due to unmet prerequisites: %s", name, prerequisite_error)
            outcome = ToolOutcome(content=prerequisite_error)
            terminal_status = "error"
            error_message = prerequisite_error
        else:
            event_emitter = subagent_event_emitter
            if name == "business_data_search":
                event_emitter = _build_business_data_search_event_emitter(
                    stream_event_emitter=subagent_event_emitter,
                    tool_call_id=tool_call_id,
                    entity=str(args.get("entity") or "").strip(),
                    query=str(args.get("query") or "").strip(),
                )
            elif name == "web_search":
                event_emitter = _build_web_search_event_emitter(
                    stream_event_emitter=subagent_event_emitter,
                    tool_call_id=tool_call_id,
                    query=str(args.get("query") or "").strip(),
                )

            try:
                outcome = spec.execute(
                    args=args,
                    context=context,
                    tool_call_id=tool_call_id,
                    event_emitter=event_emitter,
                )
                terminal_status = _resolve_outcome_status(outcome)
                if terminal_status == "error":
                    error_message = outcome.content if outcome.content is not None else "tool error"
            except WorkflowCancelledError:
                logger.info("Tool execution cancelled for %s", name)
                raise
            except Exception as exc:
                logger.error("Tool execution failed for %s", name, exc_info=True)
                outcome = ToolOutcome(content="工具执行失败 {}: {}".format(name, str(exc)))
                terminal_status = "error"
                error_message = str(exc)

    finished_at_ms = _now_monotonic_ms()
    elapsed_ms = max(0, finished_at_ms - started_at_ms)
    emit_tool_finished(
        context=context,
        tool_name=name,
        tool_call_id=tool_call_id,
        status=terminal_status,
        raw_output=str(outcome.content if outcome.content is not None else ""),
        error=error_message,
        elapsed_ms=elapsed_ms,
    )

    return {
        "index": task["index"],
        "tool_call_id": tool_call_id,
        "name": name,
        "outcome": outcome,
        "tool_interval_ms": {
            "started_at_ms": started_at_ms,
            "finished_at_ms": finished_at_ms,
            "elapsed_ms": elapsed_ms,
        },
    }


def _resolve_outcome_status(outcome: ToolOutcome) -> str:
    if outcome.content is None:
        return "completed"
    parsed = _try_parse_json(outcome.content)
    if isinstance(parsed, dict):
        if (
            parsed.get("ok") is False
            or parsed.get("_success") is False
            or parsed.get("success") is False
        ):
            return "error"
        if str(parsed.get("status") or "").strip().lower() == "error":
            return "error"
        if parsed.get("error") or parsed.get("_error"):
            return "error"

    text = outcome.content.strip().lower()
    if text.startswith("error:") or text.startswith("工具执行失败"):
        return "error"
    return "completed"


# ============================================================================
# Event emitter helpers
# ============================================================================


def _make_subagent_event_emitter(
    context: ToolContext,
) -> Callable[[str, Dict[str, Any]], None]:
    if context.event_emitter:
        return context.event_emitter

    captured_writer = try_get_stream_writer()
    writer_context = copy_context() if captured_writer else None

    def emit_with_captured_context(event: Dict[str, Any]) -> None:
        if not captured_writer:
            return
        if not writer_context:
            captured_writer(event)
            return
        writer_context.copy().run(captured_writer, event)

    def emit(event_type: str, payload: Dict[str, Any]) -> None:
        emit_protocol_event(
            context=context,
            event_type=event_type,
            source={"node": "agent_loop", "component": "subagent"},
            payload=payload,
            writer=emit_with_captured_context if captured_writer else None,
        )

    return emit


def _build_business_data_search_event_emitter(
    *,
    stream_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
    tool_call_id: Optional[str],
    entity: str = "",
    query: str = "",
) -> Callable[[Dict[str, str]], None]:
    def emit_business_data_search_event(event: Dict[str, str]) -> None:
        if not stream_event_emitter:
            return

        event_type = str(event.get("type") or "").strip().lower()
        content = str(event.get("content") or "")
        message_preview, truncated = _truncate_text(content, limit=100)
        message = "业务数据查询"
        if message_preview:
            message = "{}: {}".format(message, message_preview)
            if truncated:
                message = "{}...".format(message)

        stream_event_emitter(
            "tool.progress",
            {
                "tool_call_id": tool_call_id,
                "tool_name": "business_data_search",
                "input": {
                    key: value
                    for key, value in {"entity": entity, "query": query}.items()
                    if value
                },
                "entity": entity,
                "query": query,
                "status": "running",
                "message": message,
                "progress": {
                    "business_data_search_event": {
                        "type": event_type,
                        "content": content,
                    },
                },
            },
        )

    return emit_business_data_search_event


def _build_web_search_event_emitter(
    *,
    stream_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
    tool_call_id: Optional[str],
    query: str = "",
) -> Callable[[Dict[str, str]], None]:
    def emit_web_search_event(event: Dict[str, str]) -> None:
        if not stream_event_emitter:
            return

        event_type = str(event.get("type") or "").strip().lower()
        content = str(event.get("content") or "")
        message_preview, truncated = _truncate_text(content, limit=100)
        message = "联网搜索"
        if message_preview:
            message = "{}: {}".format(message, message_preview)
            if truncated:
                message = "{}...".format(message)

        stream_event_emitter(
            "tool.progress",
            {
                "tool_call_id": tool_call_id,
                "tool_name": "web_search",
                "input": {
                    key: value
                    for key, value in {"query": query}.items()
                    if value
                },
                "query": query,
                "status": "running",
                "message": message,
                "progress": {
                    "web_search_event": {
                        "type": event_type,
                        "content": content,
                    },
                },
            },
        )

    return emit_web_search_event


# ============================================================================
# Shared helpers
# ============================================================================


def _validate_tool_prerequisites(
    *,
    name: str,
    args: Dict[str, Any],
    context: ToolContext,
) -> Optional[str]:
    del name, args, context
    return None


def _build_tool_progress_message(name: Optional[str], args: Dict[str, Any]) -> Optional[str]:
    if name == "ontology_context":
        resource = str(args.get("resource") or "").strip()
        key = str(args.get("key") or "").strip()
        ontology_label = str(
            args.get("ontology_name")
            or args.get("ontology_label")
            or args.get("name")
            or args.get("ontology_id")
            or ""
        ).strip()
        if resource == "root":
            return "加载本体 {}".format(ontology_label or "本体")
        resource_label = {
            "operation": "逻辑",
            "object": "实体",
            "rule": "规则",
            "metric": "指标",
            "script": "脚本",
        }.get(resource, resource)
        return "加载本体 {}".format("{} {}".format(resource_label, key).strip() or ontology_label or "本体")

    if name == "skill":
        skill_name = str(args.get("skill_name") or "").strip()
        resource = str(args.get("resource") or "").strip()
        key = str(args.get("key") or "").strip()
        detail = " ".join(part for part in [skill_name, resource, key] if part)
        return "加载技能 {}".format(detail or "root")

    if name == "execute_script":
        operation_key = str(args.get("operation_key") or "").strip()
        return "执行脚本流程 {}".format(operation_key or "operation")

    if name == "business_data_search":
        entity = str(args.get("entity") or "").strip()
        query = str(args.get("query") or "").strip()
        if entity and query:
            return "业务数据查询 [{}] {}".format(entity, query)
        if entity:
            return "业务数据查询 [{}]".format(entity)
        return "业务数据查询 {}".format(query or "...")

    if name == "web_search":
        query = str(args.get("query") or "").strip()
        return "联网搜索 {}".format(query or "...")

    return None


def _truncate_text(text: str, *, limit: int) -> tuple[str, bool]:
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def _try_parse_json(text: str) -> Optional[Dict[str, Any]]:
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _now_ms() -> int:
    return int(time.time() * 1000)


def _now_monotonic_ms() -> int:
    return int(time.perf_counter() * 1000)


# Iterable type kept for backwards-compatible imports
_IterableType = Iterable
