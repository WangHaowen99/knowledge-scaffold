"""Tool binding and state helpers for the Jiuwen runtime."""

from __future__ import annotations

import asyncio
import importlib
import json
import threading
import time
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import Any, Dict, Optional

from integration.observability import AgentRuntimeTrace, RuntimeStatus
from utils.json_utils import _loads_json_object_loose

from ...exceptions import (
    AgentLoopStalledError,
    TerminalToolExecutionError,
    WorkflowCancelledError,
)
from ...runtime.async_utils import call_async_or_thread
from ...script_argument_errors import safe_script_argument_error
from ...tools.base import ToolBatchResult, ToolInvocation
from ...tools.executor import aexecute_tool_batch
from ..shared.state_updates import apply_tool_batch_result_to_state
from .common import (
    JiuwenBackendUnavailable,
    format_jiuwen_import_error,
    normalize_arguments,
    read_field,
)

JIUWEN_STATE_MESSAGES_LOCK = threading.Lock()
JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY = "_jiuwen_pending_assistant_context"


class ConsecutiveToolFailureGuard:
    """Stop a run after the same tool failure is returned twice in a row."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._last_failure: Optional[tuple[str, str, str]] = None
        self._last_failure_iteration: Optional[int] = None
        self._closed = False
        self._next_invocation_index: Optional[int] = None
        self._registered_invocations: set[int] = set()
        self._pending_results: Dict[
            int,
            tuple[ToolInvocation, ToolBatchResult, Optional[int]],
        ] = {}
        self._skipped_invocations: set[int] = set()
        self._reported_invocations: set[int] = set()
        self._settled_invocations: set[int] = set()

    def register_invocation(self, invocation_index: int) -> None:
        """Register a call before it starts so completion order is irrelevant."""
        index = _coerce_invocation_index(invocation_index)
        if index is None:
            return
        with self._lock:
            if self._closed or self._invocation_is_known_locked(index):
                return
            self._registered_invocations.add(index)
            self._ensure_next_invocation_locked(index)
            self._drain_ordered_results_locked()

    def skip_invocation(self, invocation_index: int) -> None:
        """Advance the ordered stream when a call has no batch result."""
        index = _coerce_invocation_index(invocation_index)
        if index is None:
            return
        with self._lock:
            if self._closed or self._invocation_is_known_locked(index):
                return
            self._registered_invocations.add(index)
            self._skipped_invocations.add(index)
            self._ensure_next_invocation_locked(index)
            self._drain_ordered_results_locked()

    def record(
        self,
        tool_call: ToolInvocation,
        batch_result: ToolBatchResult,
        *,
        invocation_index: Optional[int] = None,
        agent_iteration: Optional[int] = None,
    ) -> None:
        index = _coerce_invocation_index(invocation_index)
        iteration = _coerce_agent_iteration(agent_iteration)
        if index is None:
            with self._lock:
                if self._closed:
                    return
                self._record_one_locked(
                    tool_call,
                    batch_result,
                    agent_iteration=iteration,
                )
            return

        with self._lock:
            if self._closed or self._invocation_is_known_locked(index):
                return
            self._registered_invocations.add(index)
            self._reported_invocations.add(index)
            self._pending_results[index] = (tool_call, batch_result, iteration)
            self._ensure_next_invocation_locked(index)
            self._drain_ordered_results_locked()

    def _record_one_locked(
        self,
        tool_call: ToolInvocation,
        batch_result: ToolBatchResult,
        agent_iteration: Optional[int],
    ) -> None:
        error = _canonical_tool_error(batch_result)
        if bool(getattr(batch_result, "terminal", False)):
            self._close_locked()
            return
        if error is None:
            self._last_failure = None
            self._last_failure_iteration = None
            return

        validation_error = _script_argument_failure(batch_result)
        fingerprint = (
            str(tool_call.name or "").strip().lower(),
            _canonical_tool_arguments({
                key: tool_call.arguments.get(key)
                for key in ("ontology_id", "operation_key")
            } if validation_error else tool_call.arguments),
            _canonical_json(validation_error) if validation_error else error,
        )
        repeated = fingerprint == self._last_failure and (
            agent_iteration is None
            or self._last_failure_iteration is None
            or agent_iteration != self._last_failure_iteration
        )
        self._last_failure = fingerprint
        self._last_failure_iteration = agent_iteration
        if repeated:
            # Never include raw tool output: script stderr/stdout may contain
            # credentials or private infrastructure details.
            self._close_locked()
            raise AgentLoopStalledError(
                "Repeated failure for tool '{}'".format(
                    str(tool_call.name or "").strip() or "unknown",
                ),
                details={"last_argument_validation": validation_error} if validation_error else None,
            )

    def _ensure_next_invocation_locked(self, index: int) -> None:
        if self._next_invocation_index is None or index < self._next_invocation_index:
            self._next_invocation_index = index

    def _drain_ordered_results_locked(self) -> None:
        while not self._closed and self._next_invocation_index is not None:
            index = self._next_invocation_index
            if index in self._skipped_invocations:
                self._last_failure = None
                self._last_failure_iteration = None
                self._skipped_invocations.remove(index)
                self._registered_invocations.discard(index)
                self._settled_invocations.add(index)
                self._advance_next_invocation_locked(index)
                continue
            pending = self._pending_results.get(index)
            if pending is None:
                return
            self._pending_results.pop(index, None)
            self._registered_invocations.discard(index)
            self._settled_invocations.add(index)
            self._next_invocation_index = index + 1
            self._record_one_locked(*pending)
            if self._closed:
                return
            self._advance_next_invocation_locked(index)

    def _advance_next_invocation_locked(self, completed_index: int) -> None:
        next_index = completed_index + 1
        candidates = self._registered_invocations | set(self._pending_results) | self._skipped_invocations
        self._next_invocation_index = (
            next_index if next_index in candidates else (min(candidates) if candidates else None)
        )

    def _invocation_is_known_locked(self, index: int) -> bool:
        return (
            index in self._reported_invocations
            or index in self._skipped_invocations
            or index in self._settled_invocations
        )

    def reset(self) -> None:
        with self._lock:
            self._reset_locked()

    def _close_locked(self) -> None:
        self._closed = True
        self._clear_locked()

    def _reset_locked(self) -> None:
        self._closed = False
        self._clear_locked()

    def _clear_locked(self) -> None:
        self._last_failure = None
        self._last_failure_iteration = None
        self._next_invocation_index = None
        self._registered_invocations.clear()
        self._pending_results.clear()
        self._skipped_invocations.clear()
        self._reported_invocations.clear()
        self._settled_invocations.clear()


class CurrentRuntimeToolAdapter:
    """Expose one current-project tool through a Jiuwen-style async invoke."""

    def __init__(
        self,
        *,
        tool_name: str,
        state: Dict[str, Any],
        context_factory: Any,
        tool_batch_executor: Any = None,
        iteration_event_callback: Any = None,
        tool_batch_started_callback: Any = None,
        tool_batch_failed_callback: Any = None,
        terminal_tool_batch_callback: Any = None,
        state_snapshot_callback: Any = None,
        invocation_index_provider: Any = None,
        failure_guard: Optional[ConsecutiveToolFailureGuard] = None,
        terminal_error_callback: Any = None,
    ) -> None:
        self.tool_name = str(tool_name or "").strip()
        if not self.tool_name:
            raise ValueError("tool_name is required")
        self.state = state
        self.context_factory = context_factory
        self.tool_batch_executor = tool_batch_executor or aexecute_tool_batch
        self.iteration_event_callback = iteration_event_callback
        self.tool_batch_started_callback = tool_batch_started_callback
        self.tool_batch_failed_callback = tool_batch_failed_callback
        self.terminal_tool_batch_callback = terminal_tool_batch_callback
        self.state_snapshot_callback = state_snapshot_callback
        self.invocation_index_provider = invocation_index_provider
        self.failure_guard = failure_guard or ConsecutiveToolFailureGuard()
        self.terminal_error_callback = terminal_error_callback
        self._invocation_index = 0
        self._invocation_index_lock = threading.Lock()
        self._serial_execution_lock = asyncio.Lock() if not _is_tool_concurrency_safe(self.tool_name) else None

    async def invoke(self, tool_args: Any, session: Any = None) -> str:
        del session
        if _requires_pre_append_pending_guard(self.tool_name) and self._serial_execution_lock is not None:
            await _acquire_thread_lock_async(self._serial_execution_lock)
            try:
                return await self._invoke_after_pending_guard(tool_args, lock_held=True)
            finally:
                self._serial_execution_lock.release()
        return await self._invoke_after_pending_guard(tool_args)

    async def _invoke_after_pending_guard(self, tool_args: Any, *, lock_held: bool = False) -> str:
        if _has_pending_user_interaction(self.state):
            batch_result = _pending_user_interaction_batch_result(self.state)
            if self.terminal_tool_batch_callback is not None:
                self.terminal_tool_batch_callback(batch_result)
            return _terminal_tool_content(self.state, batch_result)
        call_index = self._next_invocation_index()
        run_scope = (
            str(self.state.get("run_id") or "").strip()
            or str(self.state.get("session_id") or "").strip()
            or str(id(self.state))
        )
        tool_call_id = "jiuwen_{}_{}_{}".format(
            safe_jiuwen_resource_part(run_scope),
            safe_jiuwen_resource_part(self.tool_name),
            call_index,
        )
        invocation = ToolInvocation(
            id=tool_call_id,
            name=self.tool_name,
            arguments=_normalize_jiuwen_tool_args(tool_args),
        )
        try:
            self._register_failure_guard_invocation(call_index)
            assistant_tool_call = {
                "id": tool_call_id,
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "arguments": normalize_arguments(tool_args),
                },
            }
            append_assistant_tool_call_to_state(self.state, assistant_tool_call)
            await self._emit_state_snapshot()
            if self._serial_execution_lock is not None and not lock_held:
                await _acquire_thread_lock_async(self._serial_execution_lock)
                try:
                    return await self._execute_tool_call(
                        invocation,
                        call_index=call_index,
                        batch_index=call_index,
                    )
                finally:
                    self._serial_execution_lock.release()
            return await self._execute_tool_call(
                invocation,
                call_index=call_index,
                batch_index=call_index,
            )
        except BaseException:
            self._skip_failure_guard_invocation(call_index)
            raise

    async def _execute_tool_call(
        self,
        tool_call: ToolInvocation,
        *,
        call_index: int,
        batch_index: int,
    ) -> str:
        started_at = time.perf_counter()
        started_at_ms = int(started_at * 1000)
        context = self.context_factory()
        context.tool_batch_index = batch_index
        runtime_trace = runtime_trace_from_tool_context(context)
        iteration = tool_context_iteration(context)
        guard_iteration = _coerce_agent_iteration(getattr(context, "agent_iteration", None))
        try:
            if self.tool_batch_started_callback is not None:
                self.tool_batch_started_callback(
                    context,
                    call_index=call_index,
                    batch_index=batch_index,
                )
            else:
                runtime_trace.tool_batch_started(iteration=iteration, batch_index=batch_index)
            batch_result = await _invoke_tool_batch_executor(
                self.tool_batch_executor,
                [tool_call],
                context,
            )
            with JIUWEN_STATE_MESSAGES_LOCK:
                apply_tool_batch_result_to_state(self.state, batch_result)
            self._record_failure_guard_invocation(
                tool_call,
                batch_result,
                call_index=call_index,
                agent_iteration=guard_iteration,
            )
        except (WorkflowCancelledError, asyncio.CancelledError):
            self._skip_failure_guard_invocation(call_index)
            await self._rollback_cancelled_tool_call(tool_call.id)
            if self.tool_batch_failed_callback is not None:
                self.tool_batch_failed_callback(
                    context,
                    RuntimeStatus.CANCELLED,
                    call_index=call_index,
                    batch_index=batch_index,
                )
            else:
                runtime_trace.tool_batch_failed(
                    iteration=iteration,
                    status=RuntimeStatus.CANCELLED,
                    batch_index=batch_index,
                )
            raise
        except AgentLoopStalledError as exc:
            await self._emit_state_snapshot()
            if callable(self.terminal_error_callback):
                self.terminal_error_callback(exc)
            if self.tool_batch_failed_callback is not None:
                self.tool_batch_failed_callback(
                    context,
                    RuntimeStatus.ERROR,
                    call_index=call_index,
                    batch_index=batch_index,
                )
            else:
                runtime_trace.tool_batch_failed(
                    iteration=iteration,
                    status=RuntimeStatus.ERROR,
                    batch_index=batch_index,
                )
            raise
        except Exception:
            self._skip_failure_guard_invocation(call_index)
            if self.tool_batch_failed_callback is not None:
                self.tool_batch_failed_callback(
                    context,
                    RuntimeStatus.ERROR,
                    call_index=call_index,
                    batch_index=batch_index,
                )
            else:
                runtime_trace.tool_batch_failed(
                    iteration=iteration,
                    status=RuntimeStatus.ERROR,
                    batch_index=batch_index,
                )
            raise
        await self._emit_state_snapshot()
        finished_at = time.perf_counter()
        elapsed_ms = int((finished_at - started_at) * 1000)
        terminal = bool(getattr(batch_result, "terminal", False))
        if self.tool_batch_started_callback is None:
            runtime_trace.tool_batch_finished(
                iteration=iteration,
                batch_index=batch_index,
                tool_calls_count=1,
                tool_names=[self.tool_name],
                duration_ms=elapsed_ms,
            )
        if self.iteration_event_callback is not None:
            self.iteration_event_callback(
                self.tool_name,
                elapsed_ms,
                {
                    "started_at_ms": started_at_ms,
                    "finished_at_ms": int(finished_at * 1000),
                    "elapsed_ms": elapsed_ms,
                },
                terminal=terminal,
            )
        if terminal:
            terminal_error = _terminal_tool_execution_error(batch_result)
            if terminal_error is not None and callable(self.terminal_error_callback):
                self.terminal_error_callback(terminal_error)
            if self.terminal_tool_batch_callback is not None:
                self.terminal_tool_batch_callback(batch_result)
        return first_tool_message_content(batch_result)

    def _next_invocation_index(self) -> int:
        provider = self.invocation_index_provider
        if callable(provider):
            return int(provider())
        with self._invocation_index_lock:
            self._invocation_index += 1
            return self._invocation_index

    async def _emit_state_snapshot(self) -> None:
        if not callable(self.state_snapshot_callback):
            return
        with JIUWEN_STATE_MESSAGES_LOCK:
            snapshot = deepcopy(self.state)
        await call_async_or_thread(
            self.state_snapshot_callback,
            snapshot,
            run_id=str(self.state.get("run_id") or "").strip() or None,
            task_name="jiuwen-state-snapshot",
            offload_kind="short",
        )

    async def _rollback_cancelled_tool_call(
        self,
        tool_call_id: Optional[str],
    ) -> None:
        if remove_assistant_tool_call_from_state(self.state, tool_call_id):
            await self._emit_state_snapshot()

    def _register_failure_guard_invocation(self, call_index: int) -> None:
        register_invocation = getattr(self.failure_guard, "register_invocation", None)
        if callable(register_invocation):
            register_invocation(call_index)

    def _record_failure_guard_invocation(
        self,
        tool_call: ToolInvocation,
        batch_result: ToolBatchResult,
        *,
        call_index: int,
        agent_iteration: Optional[int],
    ) -> None:
        register_invocation = getattr(self.failure_guard, "register_invocation", None)
        if callable(register_invocation):
            self.failure_guard.record(
                tool_call,
                batch_result,
                invocation_index=call_index,
                agent_iteration=agent_iteration,
            )
            return
        self.failure_guard.record(tool_call, batch_result)

    def _skip_failure_guard_invocation(self, call_index: int) -> None:
        skip_invocation = getattr(self.failure_guard, "skip_invocation", None)
        if callable(skip_invocation):
            skip_invocation(call_index)


def _normalize_jiuwen_tool_args(tool_args: Any) -> Dict[str, Any]:
    if isinstance(tool_args, Mapping):
        return dict(tool_args)
    if isinstance(tool_args, str):
        parsed = _loads_json_object_loose(tool_args)
        if isinstance(parsed, Mapping):
            return dict(parsed)
    return {}


def _canonical_tool_arguments(arguments: Mapping[str, Any]) -> str:
    return _canonical_json(dict(arguments or {}))


def _canonical_tool_error(batch_result: Any) -> Optional[str]:
    text = first_tool_message_content(batch_result).strip()
    if not text:
        return None
    try:
        parsed = json.loads(text)
    except (TypeError, ValueError):
        parsed = None
    if isinstance(parsed, Mapping):
        is_error = (
            parsed.get("ok") is False
            or parsed.get("success") is False
            or parsed.get("_success") is False
            or str(parsed.get("status") or "").strip().lower() == "error"
            or bool(parsed.get("error"))
            or bool(parsed.get("_error"))
        )
        return _canonical_json(parsed) if is_error else None
    normalized = " ".join(text.split())
    lowered = normalized.lower()
    starts_with_error = lowered.startswith("error:")
    starts_with_tool_failure = lowered.startswith("tool execution failed")
    starts_with_script_failure = lowered.startswith("error executing script")
    starts_with_localized_failure = normalized.startswith("工具执行失败")
    if any(
        (
            starts_with_error,
            starts_with_tool_failure,
            starts_with_script_failure,
            starts_with_localized_failure,
        )
    ):
        return normalized
    return None


def _script_argument_failure(batch_result: Any) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(first_tool_message_content(batch_result))
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping) or payload.get("ok") is not False:
        return None
    return safe_script_argument_error(payload.get("error"))


def _canonical_json(value: Any) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    except (TypeError, ValueError):
        return repr(value)


def _coerce_invocation_index(value: Any) -> Optional[int]:
    try:
        index = int(value)
    except (TypeError, ValueError):
        return None
    return index if index > 0 else None


def _coerce_agent_iteration(value: Any) -> Optional[int]:
    try:
        iteration = int(value)
    except (TypeError, ValueError):
        return None
    return iteration if iteration > 0 else None


def _has_pending_user_interaction(state: Mapping[str, Any]) -> bool:
    pending = state.get("pending_user_interaction") if isinstance(state, Mapping) else None
    return isinstance(pending, Mapping) and bool(pending)


def _pending_user_interaction_batch_result(state: Mapping[str, Any]) -> ToolBatchResult:
    updates: Dict[str, Any] = {}
    pending = state.get("pending_user_interaction") if isinstance(state, Mapping) else None
    final_output = state.get("final_output") if isinstance(state, Mapping) else None
    if isinstance(pending, Mapping):
        updates["pending_user_interaction"] = dict(pending)
    if final_output:
        updates["final_output"] = final_output
    return ToolBatchResult(state_updates=updates, terminal=True)


def _terminal_tool_execution_error(batch_result: Any) -> Optional[TerminalToolExecutionError]:
    updates = getattr(batch_result, "state_updates", None)
    payload = updates.get("terminal_error") if isinstance(updates, Mapping) else None
    if not isinstance(payload, Mapping):
        return None
    return TerminalToolExecutionError(
        error_code=str(payload.get("error_code") or "TERMINAL_TOOL_ERROR"),
        public_message=str(payload.get("message") or "").strip() or None,
        tool_name=str(payload.get("tool_name") or "").strip() or None,
    )


def _terminal_tool_content(state: Mapping[str, Any], batch_result: Any) -> str:
    content = first_tool_message_content(batch_result)
    if content:
        return content
    final_output = state.get("final_output") if isinstance(state, Mapping) else None
    if isinstance(final_output, Mapping):
        return str(final_output.get("text") or final_output.get("output") or final_output)
    return str(final_output or "")


async def _acquire_thread_lock_async(lock: asyncio.Lock) -> None:
    """Compatibility boundary for cancellable serial tool admission."""
    await lock.acquire()


async def _invoke_tool_batch_executor(
    func: Any,
    *args: Any,
) -> Any:
    return await call_async_or_thread(
        func,
        *args,
        run_id=_run_id_from_tool_executor_args(args),
        task_name="jiuwen-tool-batch",
    )


def _run_id_from_tool_executor_args(args: tuple[Any, ...]) -> Optional[str]:
    for arg in args:
        run_id = str(getattr(arg, "run_id", "") or "").strip()
        if run_id:
            return run_id
    return None


def runtime_trace_from_tool_context(context: Any) -> AgentRuntimeTrace:
    return AgentRuntimeTrace(
        observer=getattr(context, "observer", None),
        run_id=str(getattr(context, "run_id", "") or ""),
        session_id=str(getattr(context, "session_id", "") or "") or None,
        turn_id=int(getattr(context, "turn_id", 0) or 0),
        caller=str(getattr(context, "caller", "") or "main"),
        lineage=getattr(context, "subagent_lineage", None),
    )


def tool_context_iteration(context: Any) -> int:
    try:
        return max(1, int(getattr(context, "agent_iteration", None) or 1))
    except (TypeError, ValueError):
        return 1


def register_current_runtime_tools(
    agent: Any,
    *,
    state: Dict[str, Any],
    context_factory: Any,
    tool_infos: Optional[list[Mapping[str, Any]]] = None,
    tool_batch_executor: Any = None,
    import_module: Any = None,
    iteration_event_callback: Any = None,
    tool_batch_started_callback: Any = None,
    tool_batch_failed_callback: Any = None,
    terminal_tool_batch_callback: Any = None,
    state_snapshot_callback: Any = None,
    terminal_error_callback: Any = None,
) -> list[Any]:
    import_module = import_module or importlib.import_module
    try:
        tool_module = import_module("openjiuwen.core.foundation.tool")
        runner_module = import_module("openjiuwen.core.runner")
    except ImportError as exc:
        raise JiuwenBackendUnavailable(format_jiuwen_import_error(exc)) from exc

    infos = list(tool_infos or [])
    if not infos:
        infos = get_jiuwen_tool_infos("main")
    cards = []
    tools = []
    tool_cls = tool_module.Tool
    tool_card_cls = tool_module.ToolCard
    invocation_index = _SharedInvocationIndex()
    failure_guard = ConsecutiveToolFailureGuard()
    for tool_info in infos:
        card = tool_card_cls(
            id=build_jiuwen_tool_resource_id(
                tool_info["name"],
                agent=agent,
                state=state,
            ),
            name=tool_info["name"],
            description=tool_info["description"],
            input_params=tool_info["parameters"],
        )
        delegate = CurrentRuntimeToolAdapter(
            tool_name=tool_info["name"],
            state=state,
            context_factory=context_factory,
            tool_batch_executor=tool_batch_executor or aexecute_tool_batch,
            iteration_event_callback=iteration_event_callback,
            tool_batch_started_callback=tool_batch_started_callback,
            tool_batch_failed_callback=tool_batch_failed_callback,
            terminal_tool_batch_callback=terminal_tool_batch_callback,
            state_snapshot_callback=state_snapshot_callback,
            invocation_index_provider=invocation_index.next,
            failure_guard=failure_guard,
            terminal_error_callback=terminal_error_callback,
        )
        tool = build_jiuwen_tool_wrapper(tool_cls, card, delegate)
        cards.append(card)
        tools.append(tool)

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


class _SharedInvocationIndex:
    """Allocate stable tool-batch indexes across every tool in one Jiuwen run."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value = 0

    def next(self) -> int:
        with self._lock:
            self._value += 1
            return self._value


def unregister_current_runtime_tools(
    agent: Any,
    cards: list[Any],
    *,
    import_module: Any = None,
) -> None:
    if not cards:
        return
    import_module = import_module or importlib.import_module
    try:
        runner_module = import_module("openjiuwen.core.runner")
    except ImportError:
        return
    resource_mgr = runner_module.Runner.resource_mgr
    remove_tool = getattr(resource_mgr, "remove_tool", None)
    if not callable(remove_tool):
        return
    tool_ids = [str(getattr(card, "id", "") or "").strip() for card in cards]
    tool_ids = [tool_id for tool_id in tool_ids if tool_id]
    if not tool_ids:
        return
    remove_tool(
        tool_id=tool_ids,
        tag=getattr(getattr(agent, "card", None), "id", None),
        skip_if_tag_not_exists=True,
    )


def get_jiuwen_tool_infos(
    caller: str,
    *,
    include_memory: bool = True,
    include_orchestration: bool = True,
    include_web_search: bool = False,
    include_interaction: bool = True,
    include_business_checkpoint: bool = True,
    include_solver: bool = True,
    selected_ontology_id: Optional[str] = None,
    selected_ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> list[Dict[str, Any]]:
    from ...tools.registry import get_tool_schema_for_context, get_tool_specs_for_caller

    tool_specs = get_tool_specs_for_caller(
        caller,
        include_memory=include_memory,
        include_orchestration=include_orchestration,
        include_web_search=include_web_search,
        include_interaction=include_interaction,
        include_business_checkpoint=include_business_checkpoint,
        include_solver=include_solver,
        workspace_id=workspace_id,
        selected_ontology_id=selected_ontology_id,
        selected_ontology_version=selected_ontology_version,
    )
    return [
        jiuwen_tool_info_from_schema(
            get_tool_schema_for_context(
                spec,
                selected_ontology_id=selected_ontology_id,
                selected_ontology_version=selected_ontology_version,
                workspace_id=workspace_id,
            )
        )
        for spec in tool_specs
    ]


def jiuwen_tool_info_from_schema(schema: Mapping[str, Any]) -> Dict[str, Any]:
    function = schema.get("function") if isinstance(schema, Mapping) else None
    function = function if isinstance(function, Mapping) else {}
    name = str(function.get("name") or "").strip()
    if not name:
        raise ValueError("Jiuwen tool schema name is required")
    return {
        "type": "function",
        "name": name,
        "description": str(function.get("description") or ""),
        "parameters": function.get("parameters") or {},
    }


def jiuwen_tool_info_from_spec(spec: Any) -> Dict[str, Any]:
    schema = getattr(spec, "schema", {}) or {}
    function = schema.get("function") if isinstance(schema, Mapping) else None
    function = function if isinstance(function, Mapping) else {}
    name = str(getattr(spec, "name", "") or function.get("name") or "").strip()
    if not name:
        raise ValueError("Jiuwen tool spec name is required")
    if name == function.get("name"):
        return jiuwen_tool_info_from_schema(schema)
    return jiuwen_tool_info_from_schema(
        {
            "type": "function",
            "function": {
                **dict(function),
                "name": name,
            },
        }
    )


def build_jiuwen_tool_resource_id(
    tool_name: str,
    *,
    agent: Any,
    state: Mapping[str, Any],
) -> str:
    agent_card = getattr(agent, "card", None)
    agent_id = str(getattr(agent_card, "id", "") or "").strip()
    if not agent_id:
        agent_id = build_jiuwen_agent_resource_id(state, caller=str(state.get("caller") or "main"))
    return "{}.tool.{}".format(
        safe_jiuwen_resource_part(agent_id),
        safe_jiuwen_resource_part(tool_name),
    )


def build_jiuwen_agent_resource_id(
    state: Optional[Mapping[str, Any]],
    *,
    caller: str,
) -> str:
    state_map = state if isinstance(state, Mapping) else {}
    scope = (
        str(state_map.get("run_id") or "").strip()
        or str(state_map.get("session_id") or "").strip()
        or str(id(state_map))
    )
    turn_id = str(state_map.get("current_turn_id") or "").strip()
    parts = ["onto_agent_jiuwen_react", str(caller or "main").strip() or "main", scope]
    if turn_id:
        parts.append("turn{}".format(turn_id))
    parts.append(uuid.uuid4().hex)
    return ".".join(safe_jiuwen_resource_part(part) for part in parts if str(part or "").strip())


def safe_jiuwen_resource_part(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return "default"
    chars = []
    for char in text:
        if char.isalnum() or char in {"_", "-", "."}:
            chars.append(char)
        else:
            chars.append("_")
    return "".join(chars).strip("._-") or "default"


def build_jiuwen_tool_wrapper(tool_cls: Any, card: Any, delegate: CurrentRuntimeToolAdapter) -> Any:
    class _CurrentRuntimeJiuwenTool(tool_cls):
        async def invoke(self, inputs: Any, **kwargs: Any) -> Any:
            return await delegate.invoke(inputs, session=kwargs.get("session"))

        async def stream(self, inputs: Any, **kwargs: Any) -> Any:
            yield await self.invoke(inputs, **kwargs)

    return _CurrentRuntimeJiuwenTool(card=card)


def set_jiuwen_pending_assistant_context(
    state: Mapping[str, Any],
    *,
    content: str = "",
    reasoning_content: str = "",
) -> None:
    if not isinstance(state, dict):
        return
    pending = dict(state.get(JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY) or {})
    if str(content or ""):
        pending["content"] = str(content or "")
    if str(reasoning_content or ""):
        pending["reasoning_content"] = str(reasoning_content or "")
    if pending:
        state[JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY] = pending


def record_jiuwen_pending_assistant_text(
    state: Optional[Mapping[str, Any]],
    channel: str,
    text: str,
) -> None:
    if not isinstance(state, dict) or not text:
        return
    pending = dict(state.get(JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY) or {})
    key = "reasoning_content" if channel == "reasoning" else "content"
    pending[key] = "{}{}".format(pending.get(key) or "", text)
    state[JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY] = pending


def consume_jiuwen_pending_assistant_context(state: Dict[str, Any]) -> Dict[str, str]:
    pending = state.pop(JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY, None)
    if not isinstance(pending, Mapping):
        return {}
    return {key: str(pending.get(key) or "") for key in ("content", "reasoning_content") if str(pending.get(key) or "")}


def clear_jiuwen_pending_assistant_context(state: Dict[str, Any]) -> None:
    state.pop(JIUWEN_PENDING_ASSISTANT_CONTEXT_KEY, None)


def apply_jiuwen_pending_assistant_context(
    message: Dict[str, Any],
    pending_context: Mapping[str, Any],
) -> None:
    content = str(pending_context.get("content") or "")
    reasoning_content = str(pending_context.get("reasoning_content") or "")
    if content and not str(message.get("content") or ""):
        message["content"] = content
    if reasoning_content and not str(message.get("reasoning_content") or ""):
        message["reasoning_content"] = reasoning_content


def append_assistant_tool_call_to_state(state: Dict[str, Any], tool_call: Mapping[str, Any]) -> None:
    assistant_tool_call = deepcopy(dict(tool_call))
    with JIUWEN_STATE_MESSAGES_LOCK:
        pending_context = consume_jiuwen_pending_assistant_context(state)
        messages = state.setdefault("messages", [])
        if messages:
            last_message = messages[-1]
            if last_message.get("role") == "assistant" and isinstance(last_message.get("tool_calls"), list):
                last_tool_calls = list(last_message.get("tool_calls") or [])
                next_message = {
                    **last_message,
                    "tool_calls": [*last_tool_calls, assistant_tool_call],
                }
                apply_jiuwen_pending_assistant_context(next_message, pending_context)
                messages[-1] = next_message
                return
        assistant_message = {
            "role": "assistant",
            "content": "",
            "tool_calls": [assistant_tool_call],
        }
        apply_jiuwen_pending_assistant_context(assistant_message, pending_context)
        messages.append(assistant_message)


def remove_assistant_tool_call_from_state(
    state: Dict[str, Any],
    tool_call_id: Optional[str],
) -> bool:
    """Remove only the cancelled invocation from a shared assistant message."""
    normalized_id = str(tool_call_id or "").strip()
    if not normalized_id:
        return False

    with JIUWEN_STATE_MESSAGES_LOCK:
        messages = state.get("messages")
        if not isinstance(messages, list):
            return False
        for index in range(len(messages) - 1, -1, -1):
            message = messages[index]
            if not isinstance(message, Mapping) or message.get("role") != "assistant":
                continue
            tool_calls = message.get("tool_calls")
            if not isinstance(tool_calls, list):
                continue
            remaining_calls = [
                tool_call
                for tool_call in tool_calls
                if str(tool_call.get("id") if isinstance(tool_call, Mapping) else "").strip() != normalized_id
            ]
            if len(remaining_calls) == len(tool_calls):
                continue

            next_message = dict(message)
            if remaining_calls:
                next_message["tool_calls"] = remaining_calls
                messages[index] = next_message
                return True

            next_message.pop("tool_calls", None)
            has_remaining_payload = any(
                value not in (None, "", [], {}) for key, value in next_message.items() if key != "role"
            )
            if has_remaining_payload:
                messages[index] = next_message
            else:
                messages.pop(index)
            return True
    return False


def first_tool_message_content(batch_result: Any) -> str:
    for message in list(getattr(batch_result, "messages", None) or []):
        if isinstance(message, Mapping) and message.get("content") is not None:
            return str(message.get("content") or "")
    return ""


def _is_tool_concurrency_safe(tool_name: str) -> bool:
    try:
        from ...tools.registry import get_tool_spec
    except Exception:
        return False
    spec = get_tool_spec(str(tool_name or ""))
    if spec is None:
        return False
    return bool(getattr(spec, "concurrency_safe", False))


def _requires_pre_append_pending_guard(tool_name: str) -> bool:
    if str(tool_name or "") == "request_user_interaction":
        return True
    try:
        from ...tools.registry import get_tool_spec
    except Exception:
        return False
    spec = get_tool_spec(str(tool_name or ""))
    return bool(spec and getattr(spec, "category", "") == "interaction")
