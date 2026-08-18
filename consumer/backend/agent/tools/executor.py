"""Tool batch execution: parallel and sequential dispatch over ToolOutcome."""

from __future__ import annotations

import asyncio
import json
import threading
import time
import weakref
from collections.abc import Sequence
from contextvars import copy_context
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional

from agent.exceptions import WorkflowCancelledError
from agent.runtime.async_utils import (
    call_async_or_thread,
    find_group_exception,
    is_async_callable,
)
from utils.logging import get_logger
from utils.streaming import try_get_stream_writer
from agent.tools.base import BaseTool, ToolBatchResult, ToolContext, ToolInvocation, ToolOutcome
from agent.tools.events import emit_protocol_event, emit_tool_finished, emit_tool_started

logger = get_logger("tools.executor")


_MAX_BACKGROUND_SYNC_TOOL_TASKS = 32


@dataclass
class _BackgroundSyncToolState:
    """Bound and observable ownership for sync workers past a tool deadline."""

    tasks: set[asyncio.Task[Any]] = field(default_factory=set)


_BACKGROUND_SYNC_TOOL_STATES: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, _BackgroundSyncToolState
] = weakref.WeakKeyDictionary()
_BACKGROUND_SYNC_TOOL_STATES_LOCK = threading.Lock()


def _background_sync_tool_state() -> _BackgroundSyncToolState:
    loop = asyncio.get_running_loop()
    with _BACKGROUND_SYNC_TOOL_STATES_LOCK:
        state = _BACKGROUND_SYNC_TOOL_STATES.get(loop)
        if state is None:
            state = _BackgroundSyncToolState()
            _BACKGROUND_SYNC_TOOL_STATES[loop] = state
        return state

_SAFE_SCRIPT_EXECUTION_ERROR = "Error: Script execution failed due to an internal error."

_ONTOLOGY_SCOPED_TOOLS = frozenset({
    "ontology_context",
    "business_data_search",
    "execute_script",
})


def execute_tool_batch(
    tool_calls: Sequence[ToolInvocation],
    context: ToolContext,
) -> ToolBatchResult:
    """Run the native-async tool core for legacy synchronous callers.

    Async runtime code must call :func:`aexecute_tool_batch` directly. Keeping
    this wrapper preserves the existing registry API without maintaining a
    second scheduling implementation.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "execute_tool_batch() cannot run inside an event loop; "
            "await aexecute_tool_batch() instead"
        )
    return asyncio.run(aexecute_tool_batch(tool_calls, context))


async def aexecute_tool_batch(
    tool_calls: Sequence[ToolInvocation],
    context: ToolContext,
) -> ToolBatchResult:
    """Execute normalized invocations without blocking the caller event loop.

    Provider-specific payloads, including OpenAI ``tool_calls`` dictionaries,
    must be converted by the owning adapter before reaching this boundary.
    Native async tools are awaited directly; synchronous tools are isolated by
    :func:`asyncio.to_thread` in :func:`_ainvoke_tool`.
    """
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

    async def flush_safe_batch() -> bool:
        nonlocal pending_safe_batch
        if not pending_safe_batch:
            return False
        batch_outputs = await _aexecute_parallel_batch(
            pending_safe_batch,
            context,
            subagent_event_emitter,
        )
        outputs.extend(batch_outputs)
        pending_safe_batch = []
        return any(output["outcome"].terminal for output in batch_outputs)

    for position, task in enumerate(tasks):
        if task["spec"].concurrency_safe:
            pending_safe_batch.append(task)
            continue
        if await flush_safe_batch():
            outputs.extend(_build_skipped_outputs(tasks[position:]))
            break
        output = await _aexecute_single_task(task, context, subagent_event_emitter)
        outputs.append(output)
        if output["outcome"].terminal:
            outputs.extend(_build_skipped_outputs(tasks[position + 1:]))
            break
    else:
        await flush_safe_batch()

    outputs.sort(key=lambda item: item["index"])
    return _aggregate_outputs(outputs)


def _aggregate_outputs(outputs: List[Dict[str, Any]]) -> ToolBatchResult:
    messages: List[Dict[str, Any]] = []
    state_updates: Dict[str, Any] = {}
    tool_intervals_ms: List[Dict[str, int]] = []
    terminal = False
    terminal_tool_call_id: Optional[str] = None
    terminal_content: Optional[str] = None

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
        if outcome.terminal and not terminal:
            terminal_tool_call_id = output["tool_call_id"]
            terminal_content = outcome.content
        if outcome.terminal:
            terminal = True

    return ToolBatchResult(
        messages=messages,
        state_updates=state_updates,
        terminal=terminal,
        tool_intervals_ms=tool_intervals_ms,
        terminal_tool_call_id=terminal_tool_call_id,
        terminal_content=terminal_content,
    )


def _build_tool_task(
    tool_call: ToolInvocation,
    *,
    index: int,
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Dict[str, Any]:
    from agent.tools.registry import get_tool_spec

    if not isinstance(tool_call, ToolInvocation):
        raise TypeError("execute_tool_batch expects ToolInvocation instances")

    name = tool_call.name
    args = dict(tool_call.arguments)
    tool_call_id = tool_call.id
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

    return {
        "index": index,
        "name": safe_name,
        "args": args,
        "tool_call_id": tool_call_id,
        "spec": spec,
        "subagent_event_emitter": subagent_event_emitter,
    }


async def _aexecute_parallel_batch(
    batch: List[Dict[str, Any]],
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> List[Dict[str, Any]]:
    global_limit = max(1, int(context.agent_config.parallel_default_max_workers or 1))
    global_semaphore = asyncio.Semaphore(global_limit)
    tool_limits = {
        task["name"]: asyncio.Semaphore(
            max(1, int(task["spec"].max_parallelism or global_limit))
        )
        for task in batch
    }
    outputs_by_index: Dict[int, Dict[str, Any]] = {}

    async def run_task(task: Dict[str, Any]) -> None:
        async with global_semaphore:
            async with tool_limits[task["name"]]:
                outputs_by_index[task["index"]] = await _aexecute_single_task(
                    task,
                    context,
                    subagent_event_emitter,
                )

    try:
        async with asyncio.TaskGroup() as task_group:
            for task in batch:
                task_group.create_task(
                    run_task(task),
                    name="tool-{}-{}".format(task["name"], task["index"]),
                )
    except BaseExceptionGroup as exc_group:
        cancellation = find_group_exception(exc_group, WorkflowCancelledError)
        if cancellation is not None:
            raise cancellation from exc_group
        raise

    return [outputs_by_index[task["index"]] for task in batch]


def _execute_single_task(
    task: Dict[str, Any],
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Dict[str, Any]:
    """Compatibility wrapper for tests and legacy synchronous integrations."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise RuntimeError(
            "_execute_single_task() cannot run inside an event loop; "
            "await _aexecute_single_task() instead"
        )
    return asyncio.run(
        _aexecute_single_task(task, context, subagent_event_emitter)
    )


async def _aexecute_single_task(
    task: Dict[str, Any],
    context: ToolContext,
    subagent_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
) -> Dict[str, Any]:
    from agent.tools.registry import get_tool_schema_for_context, validate_tool_arguments

    name = task["name"]
    spec: BaseTool = task["spec"]
    args = task["args"]
    tool_call_id = task.get("tool_call_id")
    started_at_ms = _now_monotonic_ms()
    error_message: Optional[str] = None
    error_code: Optional[str] = None
    timeout_seconds: Optional[float] = None

    emit_tool_started(
        context=context,
        tool_name=name,
        tool_call_id=tool_call_id,
        tool_input=args,
        message=_build_tool_progress_message(name=name, args=args) or "调用工具: {}".format(name),
    )

    request_schema = get_tool_schema_for_context(
        spec,
        selected_ontology_id=context.selected_ontology_id,
        selected_ontology_version=context.selected_ontology_version,
        workspace_id=context.workspace_id,
    )
    validation_error = validate_tool_arguments(spec.name, args, tool_schema=request_schema)
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
                timeout_seconds = _resolve_tool_timeout_seconds(context)
                outcome = await _ainvoke_tool(
                    spec,
                    args=args,
                    context=context,
                    tool_call_id=tool_call_id,
                    event_emitter=event_emitter,
                    timeout_seconds=timeout_seconds,
                )
                terminal_status = _resolve_outcome_status(outcome)
                if terminal_status == "error":
                    error_message = outcome.content if outcome.content is not None else "tool error"
            except WorkflowCancelledError:
                logger.info("Tool execution cancelled for %s", name)
                raise
            except TimeoutError:
                logger.warning(
                    "Tool execution timed out tool=%s timeout_seconds=%s",
                    name,
                    timeout_seconds,
                )
                error_code = "TOOL_TIMEOUT"
                error_message = "工具执行超时 {}（{} 秒）".format(
                    name,
                    _format_timeout_seconds(timeout_seconds),
                )
                outcome = ToolOutcome(content=error_message)
                terminal_status = "error"
            except Exception as exc:
                logger.error("Tool execution failed for %s", name, exc_info=True)
                public_error = (
                    _SAFE_SCRIPT_EXECUTION_ERROR
                    if name == "execute_script"
                    else "工具执行失败 {}: {}".format(name, str(exc))
                )
                outcome = ToolOutcome(content=public_error)
                terminal_status = "error"
                error_message = public_error

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
        error_code=error_code,
        timeout_seconds=timeout_seconds if error_code == "TOOL_TIMEOUT" else None,
    )
    pending_interaction = (
        outcome.state_updates.get("pending_user_interaction")
        if isinstance(outcome.state_updates, dict)
        else None
    )
    if isinstance(pending_interaction, dict) and pending_interaction:
        emit_protocol_event(
            context=context,
            event_type="interaction.requested",
            source={"node": "agent_loop", "component": "interaction"},
            payload={
                "interaction_id": pending_interaction.get("id"),
                "type": pending_interaction.get("type"),
                "prompt": pending_interaction.get("prompt"),
                "options": list(pending_interaction.get("options") or []),
            },
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


async def _ainvoke_tool(
    spec: BaseTool,
    *,
    args: Dict[str, Any],
    context: ToolContext,
    tool_call_id: Optional[str],
    event_emitter: Optional[Callable[..., Any]],
    timeout_seconds: float,
) -> ToolOutcome:
    """Invoke one tool through its native async or sync compatibility API."""
    call_kwargs = {
        "args": args,
        "context": context,
        "tool_call_id": tool_call_id,
        "event_emitter": event_emitter,
    }
    async_execute = getattr(spec, "aexecute", None)
    if callable(async_execute):
        return await _ainvoke_callable_with_timeout(
            async_execute,
            timeout_seconds=timeout_seconds,
            run_id=context.run_id,
            task_name="async-tool-{}".format(spec.name),
            **call_kwargs,
        )

    execute = getattr(spec, "execute", None)
    if not callable(execute):
        raise TypeError("Tool '{}' has no execute/aexecute method".format(spec.name))
    return await _ainvoke_callable_with_timeout(
        execute,
        timeout_seconds=timeout_seconds,
        run_id=context.run_id,
        task_name="sync-tool-{}".format(spec.name),
        **call_kwargs,
    )


async def _ainvoke_callable_with_timeout(
    func: Callable[..., ToolOutcome],
    *,
    timeout_seconds: float,
    run_id: Optional[str],
    task_name: str,
    **kwargs: Any,
) -> ToolOutcome:
    if is_async_callable(func):
        async with asyncio.timeout(timeout_seconds):
            return await call_async_or_thread(
                func,
                run_id=run_id,
                task_name=task_name,
                **kwargs,
            )

    state = _background_sync_tool_state()
    if len(state.tasks) >= _MAX_BACKGROUND_SYNC_TOOL_TASKS:
        raise TimeoutError(
            "sync tool background worker capacity is exhausted"
        )
    invocation = asyncio.create_task(
        call_async_or_thread(
            func,
            run_id=run_id,
            task_name=task_name,
            **kwargs,
        ),
        name="{}-deadline".format(task_name),
    )
    state.tasks.add(invocation)
    invocation.add_done_callback(
        lambda task, owned_state=state: _consume_background_tool_result(
            task,
            owned_state,
        )
    )
    try:
        done, _ = await asyncio.wait({invocation}, timeout=timeout_seconds)
    except asyncio.CancelledError:
        invocation.cancel()
        try:
            await invocation
        except asyncio.CancelledError:
            pass
        raise
    if invocation in done:
        return invocation.result()

    # Python cannot kill a running thread. After the public deadline expires,
    # retain ownership until the worker settles. Admission above caps all live
    # sync workers so permanently blocked tools cannot grow without bound.
    raise TimeoutError


def _consume_background_tool_result(
    task: asyncio.Task[Any],
    state: _BackgroundSyncToolState,
) -> None:
    state.tasks.discard(task)
    if not task.cancelled():
        task.exception()


def _resolve_tool_timeout_seconds(context: ToolContext) -> float:
    raw_timeout = getattr(context.agent_config, "tool_timeout_seconds", 600)
    try:
        timeout_seconds = float(raw_timeout)
    except (TypeError, ValueError):
        timeout_seconds = 600.0
    return max(0.001, timeout_seconds)


def _format_timeout_seconds(timeout_seconds: Optional[float]) -> str:
    value = float(timeout_seconds or 0)
    return str(int(value)) if value.is_integer() else "{:g}".format(value)


def _build_skipped_outputs(tasks: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    outputs: List[Dict[str, Any]] = []
    for task in tasks:
        outputs.append({
            "index": task["index"],
            "tool_call_id": task.get("tool_call_id"),
            "name": task["name"],
            "outcome": ToolOutcome(
                content=json.dumps(
                    {
                        "status": "skipped",
                        "reason": "a previous tool paused the workflow for user interaction",
                    },
                    ensure_ascii=False,
                )
            ),
        })
    return outputs


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
) -> Callable[[Dict[str, Any]], None]:
    def emit_business_data_search_event(event: Dict[str, Any]) -> None:
        if not stream_event_emitter:
            return

        event_type = str(event.get("type") or "").strip().lower()
        content = str(event.get("content") or "")
        ontology_id = str(event.get("ontology_id") or "").strip()
        provider = str(event.get("provider") or "").strip()
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
                "ontology_id": ontology_id,
                "database_provider": provider,
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
    if name not in _ONTOLOGY_SCOPED_TOOLS:
        return None

    selected_ontology_id = str(context.selected_ontology_id or "").strip()
    if not selected_ontology_id:
        return None

    requested_ontology_id = str(args.get("ontology_id") or "").strip()
    if requested_ontology_id == selected_ontology_id:
        return None

    return (
        "Error: Tool '{}' cannot access ontology_id '{}' because the current turn "
        "explicitly selected ontology_id '{}'. Retry with the selected ontology_id."
    ).format(
        name,
        requested_ontology_id or "<empty>",
        selected_ontology_id,
    )


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
        resource_label = {"operation": "逻辑", "object": "实体", "asset": "公共文件"}.get(resource, resource)
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
