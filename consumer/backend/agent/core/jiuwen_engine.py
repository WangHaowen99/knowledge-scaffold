"""Boundary helpers for the openJiuwen ReAct runtime.

The current runtime keeps OpenAI-compatible nested tool schemas and tool calls.
openJiuwen uses flat ToolInfo/ToolCall objects, so this module owns only that
translation layer and lazy dependency checks.
"""

from __future__ import annotations

import importlib.util
import asyncio
import json
import threading
import time
import uuid
from collections.abc import AsyncIterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import copy, deepcopy
from dataclasses import is_dataclass, replace
from inspect import isawaitable
from typing import Any, Callable, Dict, Optional

from ..exceptions import (
    ModelContentFilteredError,
    ModelInvocationError,
    ModelOutputTruncatedError,
    WorkflowCancelledError,
)
from integration.observability import AgentRuntimeTrace, NOOP_OBSERVER, RuntimeStatus
from integration.observability import bind_observability_context
from integration.observability.stack_profiler import profile_current_thread
from llm import LLMPurpose, get_llm_router
from llm.provider_adapters import get_provider_adapter
from ..tools.executor import aexecute_tool_batch
from ..tools.base import ToolContext
from llm.llm_request_mode import is_llm_streaming_enabled
from utils.logging import bind_log_context, get_logger
from workspace.workspaces import (
    get_current_workspace_id,
    get_workspace_project_root,
)
from .jiuwen.common import (
    JiuwenBackendUnavailable,
    format_jiuwen_import_error as _format_jiuwen_import_error,
    read_field as _read_field,
    to_mapping as _to_mapping,
)
from .jiuwen.concurrency import (
    current_jiuwen_tool_call_identity as _current_jiuwen_tool_call_identity,
    install_jiuwen_concurrency_ability_manager as _install_jiuwen_concurrency_ability_manager,
)
from .jiuwen.patches import patch_jiuwen_openai_reasoning_alias as _patch_jiuwen_openai_reasoning_alias
from .jiuwen.tools import (
    clear_jiuwen_pending_assistant_context as _clear_jiuwen_pending_assistant_context,
    first_tool_message_content as _first_tool_message_content,
    build_jiuwen_agent_resource_id as _build_jiuwen_agent_resource_id,
    get_jiuwen_tool_infos,
    record_jiuwen_pending_assistant_text as _record_jiuwen_pending_assistant_text,
    register_current_runtime_tools as _register_current_runtime_tools,
    unregister_current_runtime_tools as _unregister_current_runtime_tools,
)
from .jiuwen.trace import (
    JiuwenModelTraceBridge as _BaseJiuwenModelTraceBridge,
    jiuwen_agent_callback_event_class as _default_jiuwen_agent_callback_event_class,
)
from .model_connection_policy import resolve_model_connection_policy
from .shared.events import (
    emit_iteration_completed_event,
    emit_reasoning_summary_event,
    emit_token_delta_event,
)
from .shared.interaction_policy import (
    user_interaction_supported_from_state as _user_interaction_supported_from_state,
)
from .shared.messages import append_current_date_reminder_to_text
from .shared.modes import resolve_agent_mode as _resolve_agent_mode
from .shared.ontology_context import (
    get_selected_ontology_context,
    hydrate_selected_ontology_read_documents,
    active_ontology_id_from_state as _active_ontology_id_from_state,
    active_ontology_version_from_state as _active_ontology_version_from_state,
    selected_ontology_id_from_state as _selected_ontology_id_from_state,
    selected_ontology_version_from_state as _selected_ontology_version_from_state,
)
from .shared.prompting import build_main_system_prompt
from .shared.timing import (
    build_public_iteration_timing as _build_public_iteration_timing,
    build_tool_timing_metrics as _build_jiuwen_tool_timing_metrics,
    tool_window_elapsed_ms as _tool_window_elapsed_ms,
)
from .shared.tool_context import build_tool_context
logger = get_logger("jiuwen_engine")


async def _persist_state_snapshot(
    snapshot_callback: Any,
    state: Mapping[str, Any],
) -> None:
    if snapshot_callback is None:
        return
    result = snapshot_callback(deepcopy(state))
    if isawaitable(result):
        await result


def _jiuwen_agent_callback_event_class() -> Any:
    return _default_jiuwen_agent_callback_event_class()


class _JiuwenModelTraceBridge(_BaseJiuwenModelTraceBridge):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("event_class_provider", _jiuwen_agent_callback_event_class)
        super().__init__(**kwargs)


class _JiuwenTerminalErrorSignal:
    """Carry a terminal tool error across openJiuwen's exception boundary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._error: Optional[Exception] = None

    def record(self, error: Exception) -> None:
        with self._lock:
            if self._error is None:
                self._error = error

    def get(self) -> Optional[Exception]:
        with self._lock:
            return self._error

    def raise_if_set(self) -> None:
        error = self.get()
        if error is not None:
            raise error


class _JiuwenModelErrorSignal:
    """Carry the exception observed by Jiuwen's model callback boundary."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._error: BaseException | None = None

    def record(self, error: BaseException | None) -> None:
        with self._lock:
            self._error = error

    def get(self) -> BaseException | None:
        with self._lock:
            return self._error

    def raise_if_set(self) -> None:
        error = self.get()
        if error is not None:
            raise ModelInvocationError(error) from error


class _JiuwenModelCompletionSignal:
    """Keep the latest provider completion state outside observability."""

    _TRUNCATED_REASONS = frozenset({"length", "max_tokens", "max_output_tokens"})
    _FILTERED_REASONS = frozenset({"content_filter"})
    _COMPLETE_OR_UNSPECIFIED_REASONS = frozenset(
        {None, "stop", "eos", "stop_sequence"}
    )

    def __init__(self, *, max_output_tokens: Any = None) -> None:
        self._lock = threading.Lock()
        self._latest: dict[str, Any] | None = None
        try:
            self._max_output_tokens = max(0, int(max_output_tokens or 0))
        except (TypeError, ValueError, OverflowError):
            self._max_output_tokens = 0

    @staticmethod
    def normalize_finish_reason(value: Any) -> str | None:
        if value is None:
            return None
        raw = getattr(value, "value", value)
        normalized = str(raw or "").strip().lower()
        if not normalized or normalized in {"null", "none"}:
            return None
        if "." in normalized:
            normalized = normalized.rsplit(".", 1)[-1]
        return normalized.replace("-", "_").replace(" ", "_")

    def record(self, completion: Mapping[str, Any]) -> None:
        normalized = dict(completion)
        normalized["finish_reason"] = self.normalize_finish_reason(
            completion.get("finish_reason")
        )
        with self._lock:
            self._latest = normalized

    def raise_if_incomplete(self, partial_output: str) -> None:
        with self._lock:
            completion = dict(self._latest) if self._latest is not None else None
        if completion is None:
            return
        reason = completion.get("finish_reason")
        if reason in self._TRUNCATED_REASONS:
            raise ModelOutputTruncatedError(
                partial_output,
                finish_reason=str(reason),
            )
        if reason in self._FILTERED_REASONS:
            raise ModelContentFilteredError(partial_output)
        if completion.get("has_tool_calls"):
            return
        if (
            reason in self._COMPLETE_OR_UNSPECIFIED_REASONS
            and self._reached_configured_output_limit(completion)
        ):
            raise ModelOutputTruncatedError(
                partial_output,
                finish_reason="token_limit_reached",
            )
        if reason not in self._COMPLETE_OR_UNSPECIFIED_REASONS:
            logger.warning("Unknown model finish reason: %s", reason)

    def _reached_configured_output_limit(
        self,
        completion: Mapping[str, Any],
    ) -> bool:
        if self._max_output_tokens <= 0:
            return False
        usage = completion.get("usage")
        if not isinstance(usage, Mapping):
            return False
        raw_tokens = usage.get("completion_tokens", usage.get("output_tokens"))
        try:
            output_tokens = int(raw_tokens)
        except (TypeError, ValueError, OverflowError):
            return False
        return output_tokens >= self._max_output_tokens


class JiuwenEngineEventBufferFull(RuntimeError):
    """Raised when Engine event producers outrun the stream consumer."""


class _JiuwenEngineEventBuffer:
    """Thread-safe bounded handoff from synchronous callbacks to a stream."""

    def __init__(self, *, max_buffer_size: int) -> None:
        if max_buffer_size < 1:
            raise ValueError("max_buffer_size must be positive")
        self.max_buffer_size = int(max_buffer_size)
        self._loop = asyncio.get_running_loop()
        self._loop_thread_id = threading.get_ident()
        self._queue: asyncio.Queue[Any] = asyncio.Queue(
            maxsize=self.max_buffer_size
        )
        self._closed_marker = object()
        self._queued_event_tag = object()
        self._lock = threading.Lock()
        self._buffered_count = 0
        self._accepting = True
        self._close_pending = False
        self._closed_enqueued = False
        self._overflow_error: JiuwenEngineEventBufferFull | None = None
        self._overflow_callback: Callable[[], None] | None = None

    @property
    def overflow_error(self) -> JiuwenEngineEventBufferFull | None:
        with self._lock:
            return self._overflow_error

    def set_overflow_callback(self, callback: Callable[[], None]) -> None:
        self._overflow_callback = callback

    def publish(self, event: Dict[str, Any]) -> None:
        overflow_error: JiuwenEngineEventBufferFull | None = None
        with self._lock:
            if not self._accepting:
                return
            if self._buffered_count >= self.max_buffer_size:
                overflow_error = self._overflow_error
                if overflow_error is None:
                    overflow_error = JiuwenEngineEventBufferFull(
                        "Jiuwen Engine event buffer is full "
                        "(max_buffer_size={})".format(self.max_buffer_size)
                    )
                    self._overflow_error = overflow_error
                self._accepting = False
                self._close_pending = True
            else:
                self._buffered_count += 1

        if overflow_error is not None:
            self._schedule(self._handle_overflow)
            raise overflow_error
        try:
            self._schedule(self._enqueue_event, event)
        except BaseException:
            # The slot was reserved under the producer lock. If loop shutdown
            # prevents scheduling, release that reservation here.
            self._rollback_reserved_slot()
            raise

    async def get(self) -> Dict[str, Any] | None:
        item = await self._queue.get()
        if item is self._closed_marker:
            return None
        if (
            isinstance(item, tuple)
            and len(item) == 2
            and item[0] is self._queued_event_tag
        ):
            item = item[1]
        should_close = False
        with self._lock:
            self._buffered_count -= 1
            should_close = self._close_pending and self._buffered_count == 0
        if should_close:
            self._enqueue_closed_marker()
        return item

    def close(self) -> None:
        with self._lock:
            self._accepting = False
            self._close_pending = True
            should_close = self._buffered_count == 0
        if should_close:
            self._schedule(self._enqueue_closed_marker)

    def _handle_overflow(self) -> None:
        callback = self._overflow_callback
        if callback is not None:
            callback()
        with self._lock:
            should_close = self._close_pending and self._buffered_count == 0
        if should_close:
            self._enqueue_closed_marker()

    def _enqueue_event(self, event: Dict[str, Any]) -> None:
        try:
            self._queue.put_nowait((self._queued_event_tag, event))
        except asyncio.QueueFull:
            self._rollback_reserved_slot()
            with self._lock:
                if self._overflow_error is None:
                    self._overflow_error = JiuwenEngineEventBufferFull(
                        "Jiuwen Engine event buffer is full "
                        "(max_buffer_size={})".format(self.max_buffer_size)
                    )
                self._accepting = False
                self._close_pending = True
            self._handle_overflow()

    def _rollback_reserved_slot(self) -> None:
        with self._lock:
            self._buffered_count = max(0, self._buffered_count - 1)

    def _enqueue_closed_marker(self) -> None:
        with self._lock:
            if self._closed_enqueued:
                return
            self._closed_enqueued = True
        self._queue.put_nowait(self._closed_marker)

    def _schedule(self, callback: Callable[..., None], *args: Any) -> None:
        if threading.get_ident() == self._loop_thread_id:
            callback(*args)
            return
        self._loop.call_soon_threadsafe(callback, *args)


def register_current_runtime_tools(*args: Any, **kwargs: Any) -> list[Any]:
    # The native engine has one tool scheduling path. Legacy callers enter
    # through the sync Engine wrapper, not through a second tool executor.
    kwargs.setdefault("tool_batch_executor", aexecute_tool_batch)
    kwargs.setdefault("import_module", importlib.import_module)
    return _register_current_runtime_tools(*args, **kwargs)


def unregister_current_runtime_tools(*args: Any, **kwargs: Any) -> None:
    kwargs.setdefault("import_module", importlib.import_module)
    _unregister_current_runtime_tools(*args, **kwargs)


class _JiuwenTerminalForceFinishBridge:
    """Stop ReAct after a terminal tool result or repeated tool failure."""

    def __init__(
        self,
        terminal_result: Dict[str, Any],
        *,
        terminal_error_signal: Optional[_JiuwenTerminalErrorSignal] = None,
        event_class_provider: Any,
    ) -> None:
        self.terminal_result = terminal_result
        self.terminal_error_signal = terminal_error_signal
        self.event_class_provider = event_class_provider
        self._manager: Any = None
        self._event: Any = None

    async def register(self, agent: Any) -> bool:
        manager = getattr(agent, "agent_callback_manager", None)
        register_callback = getattr(manager, "register_callback", None)
        if not callable(register_callback):
            return False
        event_cls = self.event_class_provider()
        event = getattr(event_cls, "AFTER_TOOL_CALL", None) if event_cls is not None else None
        if event is None:
            return False
        result = register_callback(event, self._on_after_tool_call, priority=-1000)
        if isawaitable(result):
            await result
        self._manager = manager
        self._event = event
        return True

    async def unregister(self) -> None:
        unregister = getattr(self._manager, "unregister", None)
        if not callable(unregister) or self._event is None:
            return
        try:
            result = unregister(self._event, self._on_after_tool_call)
            if isawaitable(result):
                await result
        finally:
            self._manager = None
            self._event = None

    async def _on_after_tool_call(self, ctx: Any) -> None:
        terminal_error = (
            self.terminal_error_signal.get()
            if self.terminal_error_signal is not None
            else None
        )
        if terminal_error is not None:
            await self._request_force_finish(
                ctx,
                {
                    "output": str(
                        getattr(terminal_error, "public_message", None)
                        or getattr(terminal_error, "message", None)
                        or terminal_error
                    ),
                    "result_type": "error",
                },
            )
            return

        batch_result = self.terminal_result.get("batch_result")
        if batch_result is None or _read_field(ctx, "exception") is not None:
            return
        expected_token = self.terminal_result.get("correlation_token")
        if expected_token is not None:
            current_identity = _current_jiuwen_tool_call_identity()
            if (
                current_identity is None
                or current_identity.correlation_token is not expected_token
            ):
                return
        elif "model_tool_call_id" in self.terminal_result:
            inputs = _read_field(ctx, "inputs")
            tool_call = _read_field(inputs, "tool_call")
            actual_tool_call_id = str(_read_field(tool_call, "id") or "")
            expected_tool_call_id = str(
                self.terminal_result.get("model_tool_call_id") or ""
            )
            if actual_tool_call_id != expected_tool_call_id:
                return
        await self._request_force_finish(
            ctx,
            {
                "output": _terminal_force_finish_output(batch_result),
                "result_type": "answer",
            },
        )

    async def _request_force_finish(self, ctx: Any, result_payload: Mapping[str, Any]) -> None:
        request_force_finish = getattr(ctx, "request_force_finish", None)
        if not callable(request_force_finish):
            return
        result = request_force_finish(dict(result_payload))
        if isawaitable(result):
            await result


class JiuwenReactLoopEngine:
    """openJiuwen ReAct loop adapter.

    This class owns the current main-agent ReAct runtime contract: it prepares
    prompts, configures the openJiuwen agent, bridges tool execution back to
    onto-agent, and returns the normalized state expected by the orchestrator.
    """

    def __init__(
        self,
        *,
        memory_manager: Any = None,
        agent_config: Any = None,
        agent_factory: Optional[Any] = None,
    ) -> None:
        require_openjiuwen()
        self.memory_manager = memory_manager
        self.agent_config = agent_config
        self._agent_factory = agent_factory

    async def arun(
        self,
        state: Dict[str, Any],
        *,
        caller: str,
        event_writer: Optional[Any] = None,
        snapshot_callback: Optional[Any] = None,
        iteration_limit: Optional[int] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Execute the single native-async ReAct implementation.

        The application owns Runner startup and shutdown through its ASGI
        lifespan, and every invocation executes on that same event loop.
        """
        with bind_log_context(
            _build_jiuwen_log_context(
                state,
                caller=caller,
                subagent_lineage=kwargs.get("subagent_lineage"),
            )
        ):
            return await self._arun_core(
                state,
                caller=caller,
                event_writer=event_writer,
                snapshot_callback=snapshot_callback,
                iteration_limit=iteration_limit,
                system_prompt=system_prompt,
                **kwargs,
            )

    async def astream(
        self,
        state: Dict[str, Any],
        *,
        caller: str,
        event_writer: Optional[Any] = None,
        snapshot_callback: Optional[Any] = None,
        iteration_limit: Optional[int] = None,
        system_prompt: Optional[str] = None,
        max_event_buffer_size: int = 1024,
        **kwargs: Any,
    ) -> AsyncIterator[Dict[str, Any]]:
        """Yield existing engine events while the same ``arun`` core executes."""
        buffer = _JiuwenEngineEventBuffer(
            max_buffer_size=max(1, int(max_event_buffer_size))
        )
        producer_errors: list[BaseException] = []

        def capture_event(event: Dict[str, Any]) -> None:
            if event_writer is not None:
                event_writer(event)
            buffer.publish(event)

        async def produce() -> Dict[str, Any]:
            try:
                return await self.arun(
                    state,
                    caller=caller,
                    event_writer=capture_event,
                    snapshot_callback=snapshot_callback,
                    iteration_limit=iteration_limit,
                    system_prompt=system_prompt,
                    **kwargs,
                )
            except BaseException as exc:
                producer_errors.append(exc)
                return state
            finally:
                buffer.close()

        producer = asyncio.create_task(
            produce(),
            name="jiuwen-engine-astream-producer",
        )

        def cancel_overflowed_producer() -> None:
            if producer.done() or producer is asyncio.current_task():
                return
            producer.cancel()

        buffer.set_overflow_callback(cancel_overflowed_producer)
        try:
            while True:
                event = await buffer.get()
                if event is None:
                    break
                yield event
            overflow_error = buffer.overflow_error
            if overflow_error is not None:
                raise overflow_error
            if producer_errors:
                raise producer_errors[0]
        finally:
            if not producer.done():
                producer.cancel()
            await asyncio.gather(producer, return_exceptions=True)

    async def _arun_core(
        self,
        state: Dict[str, Any],
        *,
        caller: str,
        event_writer: Optional[Any] = None,
        snapshot_callback: Optional[Any] = None,
        iteration_limit: Optional[int] = None,
        system_prompt: Optional[str] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        resolved_workspace_id = str(state.get("workspace_id") or "").strip() or _resolve_current_workspace_id()
        if resolved_workspace_id:
            state["workspace_id"] = resolved_workspace_id
        started_at = time.perf_counter()
        event_source_node = _main_event_source_node(caller, kwargs.get("event_source_node"))
        agent_mode = _resolve_agent_mode(state.get("user_context"))
        reasoning_enabled = agent_mode != "quick"
        include_orchestration = agent_mode == "multi_agent"
        runtime_observer = kwargs.get("observer") or NOOP_OBSERVER
        runtime_trace = AgentRuntimeTrace(
            observer=runtime_observer,
            run_id=str(state.get("run_id") or ""),
            session_id=str(state.get("session_id") or "") or None,
            turn_id=int(state.get("current_turn_id") or 0),
            caller=caller,
            lineage=kwargs.get("subagent_lineage"),
        )
        runtime_trace.agent_loop_started(agent_mode=agent_mode)
        prompt_started_at = time.perf_counter()
        system_prompt_for_agent = system_prompt
        if caller in ("main", "acp"):
            system_prompt_for_agent = system_prompt or _run_jiuwen_setup_step(
                runtime_trace,
                self._build_main_system_prompt,
                state,
                include_orchestration=include_orchestration,
            )
        prompt_elapsed_ms = int((time.perf_counter() - prompt_started_at) * 1000)
        progress = {
            "iteration": 0,
            "last_completed_at": started_at,
            "prompt_elapsed_ms": prompt_elapsed_ms,
        }
        terminal_error_signal = _JiuwenTerminalErrorSignal()
        model_error_signal = _JiuwenModelErrorSignal()
        model_timing_lock = threading.Lock()
        model_elapsed_ms_by_iteration: Dict[int, int] = {}

        def record_model_timing(iteration: int, elapsed_ms: int) -> None:
            normalized_iteration = max(1, int(iteration or 1))
            with model_timing_lock:
                model_elapsed_ms_by_iteration[normalized_iteration] = (
                    model_elapsed_ms_by_iteration.get(normalized_iteration, 0)
                    + max(0, int(elapsed_ms or 0))
                )

        def model_elapsed_for_iteration(iteration: int) -> Optional[int]:
            with model_timing_lock:
                return model_elapsed_ms_by_iteration.get(max(1, int(iteration or 1)))
        cancel_event = _get_jiuwen_cancel_event(state)
        _run_jiuwen_setup_step(runtime_trace, _raise_if_jiuwen_cancelled, cancel_event, state)
        max_iterations = _run_jiuwen_setup_step(
            runtime_trace,
            _resolve_max_iterations,
            iteration_limit,
            self.agent_config,
            caller=caller,
        )
        state.setdefault("messages", [])
        _run_jiuwen_setup_step(
            runtime_trace,
            self._seed_initial_messages,
            state,
            system_prompt=system_prompt_for_agent,
        )
        interaction_response = dict(state.get("user_context") or {}).get(
            "interaction_response"
        )
        history_source_messages = _run_jiuwen_setup_step(
            runtime_trace,
            _build_jiuwen_history_source_messages,
            state,
        )
        effective_context_max_rounds: Optional[int] = None
        if self._agent_factory is not None:
            _run_jiuwen_setup_step(
                runtime_trace,
                _reject_factory_agent_with_history,
                state,
                history_source_messages=history_source_messages,
                interaction_response=interaction_response,
            )
        else:
            configured_rounds = _normalize_context_max_rounds(
                getattr(self.agent_config, "context_max_rounds", 10)
            )
            effective_context_max_rounds = _resolve_effective_context_max_rounds(
                configured_rounds,
                state,
            )
        if caller == "main":
            _run_jiuwen_setup_step(
                runtime_trace,
                self._hydrate_selected_ontology_read_documents,
                state,
            )
        tools_schema = _run_jiuwen_setup_step(
            runtime_trace,
            self._tools_schema_for_state,
            state,
            caller=caller,
        )
        tool_infos = _run_jiuwen_setup_step(
            runtime_trace,
            self._jiuwen_tool_infos_for_state,
            state,
            caller=caller,
        )
        inputs = _run_jiuwen_setup_step(runtime_trace, self._build_inputs, state)
        run_input_snapshot = _run_jiuwen_setup_step(
            runtime_trace,
            _build_jiuwen_context_messages,
            state,
            inputs=inputs,
            system_prompt=system_prompt_for_agent,
            agent_config=self.agent_config,
        )
        _run_jiuwen_setup_step(runtime_trace, _raise_if_jiuwen_cancelled, cancel_event, state)
        llm_config_for_agent = (
            None
            if self._agent_factory is not None
            else _run_jiuwen_setup_step(
                runtime_trace,
                _resolve_jiuwen_llm_config,
                state,
                caller=caller,
                agent_config=self.agent_config,
            )
        )
        stream_enabled = is_llm_streaming_enabled(
            (state.get("user_context") or {}).get("llm_request_mode")
        )
        trace_iteration = 1
        prepared_iterations: set[int] = set()
        model_name = str(getattr(llm_config_for_agent, "model", "") or "jiuwen_agent_factory")
        owns_jiuwen_runtime = self._agent_factory is None
        context_engine_managed_window = owns_jiuwen_runtime

        def prepare_iteration(iteration: int) -> None:
            normalized_iteration = max(1, int(iteration or 1))
            if normalized_iteration in prepared_iterations:
                return
            prepared_iterations.add(normalized_iteration)
            runtime_trace.iteration_prepared(
                iteration=normalized_iteration,
                purpose=str((LLMPurpose.MAIN if caller in {"main", "acp"} else LLMPurpose.SUBAGENT).value),
                agent_mode=agent_mode,
                prompt_build_ms=prompt_elapsed_ms,
                system_prompt_chars=len(str(system_prompt_for_agent or "")),
                sliding_window_applied=context_engine_managed_window,
                llm_message_count=len(run_input_snapshot),
                llm_input_chars=lambda: _estimate_jiuwen_messages_chars(run_input_snapshot),
                tools_count=len(tool_infos or []),
            )

        prepare_iteration(trace_iteration)
        agent = _run_jiuwen_setup_step(
            runtime_trace,
            self._create_agent,
            max_iterations=max_iterations,
            system_prompt=system_prompt_for_agent,
            state=state,
            caller=caller,
            enable_reasoning=reasoning_enabled,
            llm_config=llm_config_for_agent,
            stream_enabled=stream_enabled,
            context_max_rounds=effective_context_max_rounds,
        )
        owned_jiuwen_session_id: Optional[str] = None
        if owns_jiuwen_runtime:
            owned_jiuwen_session_id = _build_owned_jiuwen_session_id(agent)
            inputs = {
                **inputs,
                "conversation_id": owned_jiuwen_session_id,
            }

        def next_iteration() -> int:
            return int(progress.get("iteration") or 0) + 1

        def current_iteration() -> int:
            active_iteration = progress.get("active_iteration")
            if active_iteration:
                return int(active_iteration)
            iteration = next_iteration()
            progress["active_iteration"] = iteration
            progress["active_started_at"] = float(progress.get("last_completed_at") or started_at)
            progress["active_tool_names"] = []
            progress["active_tool_intervals_ms"] = []
            progress["active_has_tools"] = False
            progress["active_tool_batch_started"] = False
            progress["active_tool_batch_iteration"] = None
            progress["active_status"] = None
            prepare_iteration(iteration)
            return iteration

        def start_active_tool_batch(
            _context: Any = None,
            *,
            call_index: Optional[int] = None,
            batch_index: Optional[int] = None,
        ) -> None:
            del call_index
            iteration = current_iteration()
            if progress.get("active_tool_batch_started"):
                return
            runtime_trace.tool_batch_started(iteration=iteration, batch_index=batch_index)
            progress["active_tool_batch_started"] = True
            progress["active_tool_batch_iteration"] = iteration
            progress["active_tool_batch_index"] = batch_index

        def fail_active_tool_batch(
            _context: Any = None,
            status: str = RuntimeStatus.ERROR,
            *,
            call_index: Optional[int] = None,
            batch_index: Optional[int] = None,
        ) -> None:
            del call_index
            iteration = int(progress.get("active_tool_batch_iteration") or current_iteration())
            if not progress.get("active_tool_batch_started"):
                runtime_trace.tool_batch_started(iteration=iteration, batch_index=batch_index)
            runtime_trace.tool_batch_failed(
                iteration=iteration,
                status=status,
                batch_index=progress.get("active_tool_batch_index", batch_index),
            )
            progress["active_tool_batch_started"] = False
            progress["active_tool_batch_iteration"] = None
            progress["active_tool_batch_index"] = None

        def fail_unclosed_tool_batch(status: str) -> None:
            if progress.get("active_tool_batch_started"):
                fail_active_tool_batch(status=status)

        def finish_active_tool_iteration() -> None:
            if not progress.get("active_has_tools"):
                return
            iteration = int(progress.get("active_iteration") or next_iteration())
            tool_intervals = list(progress.get("active_tool_intervals_ms") or [])
            tool_names = list(progress.get("active_tool_names") or [])
            finished_at = float(progress.get("last_completed_at") or time.perf_counter())
            total_elapsed_ms = int(
                (finished_at - float(progress.get("active_started_at") or started_at)) * 1000
            )
            timing_metrics = _build_jiuwen_tool_timing_metrics(
                tool_intervals,
                tools_window_elapsed_ms=_tool_window_elapsed_ms(tool_intervals),
            )
            tools_window_elapsed_ms = int(timing_metrics.get("tools_window_elapsed_ms") or 0)
            llm_elapsed_ms = max(0, total_elapsed_ms - tools_window_elapsed_ms)
            status = str(progress.get("active_status") or "continue")
            if progress.get("active_tool_batch_started"):
                runtime_trace.tool_batch_finished(
                    iteration=int(progress.get("active_tool_batch_iteration") or iteration),
                    batch_index=progress.get("active_tool_batch_index"),
                    tool_calls_count=len(tool_names),
                    tool_names=tool_names,
                    duration_ms=tools_window_elapsed_ms,
                )
                progress["active_tool_batch_started"] = False
                progress["active_tool_batch_iteration"] = None
                progress["active_tool_batch_index"] = None
            runtime_trace.iteration_finished(
                iteration=iteration,
                status=status,
                prompt_build_ms=int(progress.get("prompt_elapsed_ms") or 0),
                llm_stream_ms=llm_elapsed_ms,
                llm_wall_ms=llm_elapsed_ms,
                tools_window_ms=tools_window_elapsed_ms,
                tools_overlap_llm_ms=int(timing_metrics.get("tools_overlap_llm_ms") or 0),
                tools_after_llm_ms=int(timing_metrics.get("tools_after_llm_ms") or 0),
                tool_calls_count=len(tool_names),
                tool_names=tool_names,
                total_ms=max(0, total_elapsed_ms),
            )

        def start_llm_iteration() -> int:
            if progress.get("active_has_tools"):
                finish_active_tool_iteration()
                progress["active_iteration"] = None
                progress["active_tool_names"] = []
                progress["active_tool_intervals_ms"] = []
                progress["active_has_tools"] = False
                progress["active_tool_batch_started"] = False
                progress["active_tool_batch_iteration"] = None
                progress["active_status"] = None
            return current_iteration()

        def final_iteration() -> int:
            if progress.get("active_has_tools"):
                finish_active_tool_iteration()
                progress["active_iteration"] = None
                progress["active_tool_names"] = []
                progress["active_tool_intervals_ms"] = []
                progress["active_has_tools"] = False
                progress["active_tool_batch_started"] = False
                progress["active_tool_batch_iteration"] = None
                progress["active_status"] = None
            return current_iteration()

        def close_terminal_tool_iteration() -> None:
            if progress.get("active_has_tools"):
                progress["active_status"] = "terminal_tool_batch"
                finish_active_tool_iteration()
            progress["active_iteration"] = None
            progress["active_tool_names"] = []
            progress["active_tool_intervals_ms"] = []
            progress["active_has_tools"] = False
            progress["active_tool_batch_started"] = False
            progress["active_tool_batch_iteration"] = None
            progress["active_status"] = None

        def emit_tool_iteration_completed(
            tool_name: str,
            tools_elapsed_ms: int,
            tool_interval_ms: Optional[Mapping[str, int]] = None,
            *,
            terminal: bool = False,
        ) -> None:
            iteration = current_iteration()
            finished_at = time.perf_counter()
            if terminal:
                progress["active_status"] = "terminal_tool_batch"
            normalized_tools_elapsed_ms = max(0, int(tools_elapsed_ms or 0))
            tool_intervals = list(progress.get("active_tool_intervals_ms") or [])
            if isinstance(tool_interval_ms, Mapping):
                tool_intervals.append(dict(tool_interval_ms))
            else:
                finished_at_ms = int(finished_at * 1000)
                tool_intervals.append(
                    {
                        "started_at_ms": max(0, finished_at_ms - normalized_tools_elapsed_ms),
                        "finished_at_ms": finished_at_ms,
                        "elapsed_ms": normalized_tools_elapsed_ms,
                    }
                )
            progress["active_tool_intervals_ms"] = tool_intervals
            tool_names = list(progress.get("active_tool_names") or [])
            if str(tool_name or "").strip():
                tool_names.append(str(tool_name or "").strip())
            progress["active_tool_names"] = tool_names
            progress["active_has_tools"] = True
            progress["iteration"] = max(int(progress.get("iteration") or 0), iteration)
            total_elapsed_ms = int(
                (finished_at - float(progress.get("active_started_at") or started_at)) * 1000
            )
            timing_metrics = _build_jiuwen_tool_timing_metrics(
                tool_intervals,
                tools_window_elapsed_ms=_tool_window_elapsed_ms(tool_intervals),
            )
            tools_window_elapsed_ms = int(timing_metrics.get("tools_window_elapsed_ms") or 0)
            llm_elapsed_ms = max(0, total_elapsed_ms - tools_window_elapsed_ms)
            public_timing = _build_public_iteration_timing(
                total_elapsed_ms=total_elapsed_ms,
                model_elapsed_ms=model_elapsed_for_iteration(iteration),
                tools_elapsed_ms=tools_window_elapsed_ms,
            )
            progress["last_completed_at"] = finished_at
            self._emit_iteration_completed(
                state,
                caller=caller,
                event_writer=event_writer,
                iteration=iteration,
                status="terminal_tool_batch" if terminal else "continue",
                prompt_elapsed_ms=int(progress.get("prompt_elapsed_ms") or 0),
                llm_elapsed_ms=llm_elapsed_ms,
                llm_stream_elapsed_ms=llm_elapsed_ms,
                llm_wall_elapsed_ms=llm_elapsed_ms,
                tools_elapsed_ms=tools_window_elapsed_ms,
                total_elapsed_ms=max(0, total_elapsed_ms),
                tool_names=tool_names,
                model_elapsed_ms=public_timing.get("model_elapsed_ms"),
                setup_elapsed_ms=public_timing.get("setup_elapsed_ms"),
                tool_timing_metrics=timing_metrics,
                event_source_node=event_source_node,
            )

        terminal_result: Dict[str, Any] = {}
        terminal_stop_event = threading.Event()

        def remember_terminal_tool_batch(batch_result: Any) -> None:
            _remember_jiuwen_terminal_tool_batch(terminal_result, batch_result)
            terminal_stop_event.set()

        registered_tool_cards: list[Any] = []
        model_completion_signal = _JiuwenModelCompletionSignal(
            max_output_tokens=getattr(llm_config_for_agent, "max_tokens", None),
        )
        model_trace_bridge = _JiuwenModelTraceBridge(
            runtime_trace=runtime_trace,
            state=state,
            caller=caller,
            iteration_provider=start_llm_iteration,
            model_provider=getattr(llm_config_for_agent, "provider", None),
            llm_config=llm_config_for_agent,
            stream_enabled=stream_enabled,
            enable_reasoning=reasoning_enabled,
            model_timing_observer=record_model_timing,
            model_error_callback=model_error_signal.record,
            model_completion_callback=model_completion_signal.record,
            prompt_build_ms=prompt_elapsed_ms,
            system_prompt_chars=len(str(system_prompt_for_agent or "")),
            sliding_window_applied=context_engine_managed_window,
        )
        terminal_force_finish_bridge = _JiuwenTerminalForceFinishBridge(
            terminal_result,
            terminal_error_signal=terminal_error_signal,
            event_class_provider=_jiuwen_agent_callback_event_class,
        )
        try:
            if owns_jiuwen_runtime:
                registered_tool_cards = _run_jiuwen_setup_step(
                    runtime_trace,
                    register_current_runtime_tools,
                    agent,
                    state=state,
                    context_factory=lambda: self._build_tool_context(
                        state,
                        caller=caller,
                        tool_event_emitter=kwargs.get("tool_event_emitter"),
                        protocol_event_observer=kwargs.get("protocol_event_observer"),
                        subagent_manager=kwargs.get("subagent_manager"),
                        subagent_lineage=kwargs.get("subagent_lineage"),
                        observer=runtime_observer,
                        agent_iteration=current_iteration(),
                    ),
                    tool_infos=tool_infos,
                    iteration_event_callback=emit_tool_iteration_completed,
                    tool_batch_started_callback=start_active_tool_batch,
                    tool_batch_failed_callback=fail_active_tool_batch,
                    terminal_tool_batch_callback=remember_terminal_tool_batch,
                    state_snapshot_callback=snapshot_callback,
                    terminal_error_callback=terminal_error_signal.record,
                )
            await model_trace_bridge.register(agent)
            await terminal_force_finish_bridge.register(agent)
            try:
                try:
                    raw_result = await self._invoke_agent(
                        agent,
                        inputs,
                        state=state,
                        history_source_messages=history_source_messages,
                        caller=caller,
                        event_writer=event_writer,
                        iteration_provider=start_llm_iteration,
                        cancel_event=cancel_event,
                        token_callback=kwargs.get("token_callback"),
                        reasoning_summary_callback=kwargs.get("reasoning_summary_callback"),
                        stream_enabled=stream_enabled,
                        event_source_node=event_source_node,
                        terminal_error_signal=terminal_error_signal,
                        terminal_stop_event=terminal_stop_event,
                        observability_context={
                            "observer": runtime_observer,
                            "run_id": runtime_trace.run_id,
                            "session_id": runtime_trace.session_id,
                            "turn_id": runtime_trace.turn_id,
                            "parent_key": runtime_trace.loop_span_key,
                        },
                    )
                    model_error_signal.raise_if_set()
                    terminal_error_signal.raise_if_set()
                except (WorkflowCancelledError, asyncio.CancelledError):
                    fail_unclosed_tool_batch(RuntimeStatus.CANCELLED)
                    runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
                    raise
                except Exception as exc:
                    model_error = model_error_signal.get()
                    fail_unclosed_tool_batch(RuntimeStatus.ERROR)
                    runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
                    if model_error is not None and not isinstance(exc, ModelInvocationError):
                        raise ModelInvocationError(model_error) from model_error
                    raise
            finally:
                try:
                    await _run_best_effort_jiuwen_cleanup_async(
                        "terminal force-finish callback unregister",
                        terminal_force_finish_bridge.unregister,
                    )
                finally:
                    await _run_best_effort_jiuwen_cleanup_async(
                        "model trace callback unregister",
                        model_trace_bridge.unregister,
                    )
            try:
                _raise_if_jiuwen_cancelled(cancel_event, state)
            except WorkflowCancelledError:
                fail_unclosed_tool_batch(RuntimeStatus.CANCELLED)
                runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
                raise
            if terminal_result.get("batch_result") is not None:
                close_terminal_tool_iteration()
                await _persist_state_snapshot(snapshot_callback, state)
                runtime_trace.agent_loop_finished(RuntimeStatus.SUCCESS)
                return state
            if _is_error_result(raw_result):
                output = _extract_output(raw_result)
                fail_unclosed_tool_batch(RuntimeStatus.ERROR)
                runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
                raise RuntimeError(output or "Jiuwen ReAct runtime returned an error.")

            output = _extract_output(raw_result)
            model_completion_signal.raise_if_incomplete(output)

            assistant_message = {"role": "assistant", "content": output}
            reasoning_content = _extract_reasoning_content(raw_result)
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            _clear_jiuwen_pending_assistant_context(state)
            state.setdefault("messages", []).append(assistant_message)
            state["final_output"] = output
            state["results"] = {**dict(state.get("results") or {}), "output": output}

            await _persist_state_snapshot(snapshot_callback, state)

            completed_iteration = final_iteration()
            final_elapsed_ms = int(
                (
                    time.perf_counter()
                    - float(progress.get("active_started_at") or progress.get("last_completed_at") or started_at)
                ) * 1000
            )
            public_timing = _build_public_iteration_timing(
                total_elapsed_ms=final_elapsed_ms,
                model_elapsed_ms=model_elapsed_for_iteration(completed_iteration),
                tools_elapsed_ms=0,
            )
            runtime_trace.iteration_finished(
                iteration=completed_iteration,
                status="final_output",
                prompt_build_ms=int(progress.get("prompt_elapsed_ms") or 0),
                llm_stream_ms=final_elapsed_ms,
                llm_wall_ms=final_elapsed_ms,
                tools_window_ms=0,
                tools_overlap_llm_ms=0,
                tools_after_llm_ms=0,
                tool_calls_count=0,
                tool_names=[],
                total_ms=final_elapsed_ms,
            )
            self._emit_iteration_completed(
                state,
                caller=caller,
                event_writer=event_writer,
                iteration=completed_iteration,
                status="final_output",
                prompt_elapsed_ms=int(progress.get("prompt_elapsed_ms") or 0),
                llm_elapsed_ms=final_elapsed_ms,
                llm_stream_elapsed_ms=final_elapsed_ms,
                llm_wall_elapsed_ms=final_elapsed_ms,
                tools_elapsed_ms=0,
                total_elapsed_ms=final_elapsed_ms,
                tool_names=[],
                model_elapsed_ms=public_timing.get("model_elapsed_ms"),
                setup_elapsed_ms=public_timing.get("setup_elapsed_ms"),
                tool_timing_metrics={},
                event_source_node=event_source_node,
            )
            runtime_trace.agent_loop_finished(RuntimeStatus.SUCCESS)
            return state
        except (WorkflowCancelledError, asyncio.CancelledError):
            runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
            raise
        except Exception:
            runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
            raise
        finally:
            try:
                await _run_best_effort_jiuwen_cleanup_async(
                    "terminal force-finish callback unregister",
                    terminal_force_finish_bridge.unregister,
                )
            finally:
                try:
                    await _run_best_effort_jiuwen_cleanup_async(
                        "model trace callback unregister",
                        model_trace_bridge.unregister,
                    )
                finally:
                    try:
                        await _run_best_effort_jiuwen_cleanup_async(
                            "runtime tool unregister",
                            lambda: unregister_current_runtime_tools(
                                agent,
                                registered_tool_cards,
                            ),
                        )
                    finally:
                        if owns_jiuwen_runtime and owned_jiuwen_session_id:
                            await _run_best_effort_jiuwen_cleanup_async(
                                "owned agent callback and session release",
                                lambda: _dispose_owned_jiuwen_runtime(
                                    agent,
                                    session_id=owned_jiuwen_session_id,
                                ),
                            )

    def _create_agent(
        self,
        *,
        max_iterations: int,
        system_prompt: Optional[str] = None,
        state: Optional[Mapping[str, Any]] = None,
        caller: str = "main",
        enable_reasoning: bool = False,
        llm_config: Any = None,
        stream_enabled: bool = True,
        context_max_rounds: Optional[int] = None,
    ) -> Any:
        if self._agent_factory is not None:
            # _arun_core validates the factory capability contract once, before any
            # factory-backed agent can reach this creation point.
            agent = self._agent_factory()
            _install_jiuwen_concurrency_ability_manager(
                agent,
                max_parallel_workers=getattr(
                    self.agent_config,
                    "parallel_default_max_workers",
                    10,
                ),
            )
            return agent

        effective_rounds = context_max_rounds
        if effective_rounds is None:
            configured_rounds = _normalize_context_max_rounds(
                getattr(self.agent_config, "context_max_rounds", 10)
            )
            effective_rounds = _resolve_effective_context_max_rounds(configured_rounds, state)

        from .shared.solver_route_guard import solver_route_requirement_from_state

        tool_choice = (
            "required"
            if solver_route_requirement_from_state(state) is not None
            else None
        )

        return create_default_jiuwen_agent(
            llm_config=llm_config
            or _resolve_jiuwen_llm_config(
                state,
                caller=caller,
                agent_config=self.agent_config,
            ),
            max_iterations=max_iterations,
            system_prompt=system_prompt,
            agent_id=_build_jiuwen_agent_resource_id(state, caller=caller),
            context_max_rounds=effective_rounds,
            enable_reasoning=enable_reasoning,
            stream_enabled=stream_enabled,
            parallel_default_max_workers=getattr(
                self.agent_config,
                "parallel_default_max_workers",
                10,
            ),
            tool_choice=tool_choice,
        )

    def _seed_initial_messages(self, state: Dict[str, Any], *, system_prompt: Optional[str]) -> None:
        if state.get("messages"):
            return
        user_content = str(state.get("user_request") or "").strip()
        if not user_content or not str(system_prompt or "").strip():
            return
        state["messages"] = [
            {"role": "system", "content": str(system_prompt)},
            {"role": "user", "content": user_content},
        ]

    def _build_main_system_prompt(self, state: Mapping[str, Any], *, include_orchestration: bool = True) -> str:
        return build_main_system_prompt(state, include_orchestration=include_orchestration)

    async def _invoke_agent(
        self,
        agent: Any,
        inputs: Dict[str, Any],
        *,
        state: Optional[Mapping[str, Any]] = None,
        history_source_messages: Optional[list[Dict[str, Any]]] = None,
        caller: str = "main",
        event_writer: Optional[Any] = None,
        iteration_provider: Optional[Any] = None,
        cancel_event: Optional[threading.Event] = None,
        token_callback: Optional[Any] = None,
        reasoning_summary_callback: Optional[Any] = None,
        stream_enabled: bool = True,
        event_source_node: Optional[str] = None,
        terminal_error_signal: Optional[_JiuwenTerminalErrorSignal] = None,
        terminal_stop_event: Optional[threading.Event] = None,
        observability_context: Optional[Dict[str, Any]] = None,
    ) -> Any:
        _raise_if_jiuwen_cancelled(cancel_event, state)
        history_messages = _build_jiuwen_history_messages(
            state or {},
            self.agent_config,
            history_source_messages=history_source_messages,
        )
        session = None
        if history_messages:
            session = await _seed_jiuwen_context_from_history(
                agent,
                inputs,
                history_messages=history_messages,
            )
            _raise_if_jiuwen_cancelled(cancel_event, state)
        obs_ctx = observability_context or {}
        if stream_enabled and _supports_managed_jiuwen_stream(agent):
            return await _with_observability_context(
                self._consume_managed_agent_stream(
                    agent,
                    inputs,
                    session=session,
                    state=state,
                    caller=caller,
                    event_writer=event_writer,
                    iteration_provider=iteration_provider,
                    cancel_event=cancel_event,
                    token_callback=token_callback,
                    reasoning_summary_callback=reasoning_summary_callback,
                    event_source_node=event_source_node,
                    terminal_error_signal=terminal_error_signal,
                    terminal_stop_event=terminal_stop_event,
                ),
                observer=obs_ctx.get("observer") or NOOP_OBSERVER,
                run_id=str(obs_ctx.get("run_id") or ""),
                session_id=obs_ctx.get("session_id"),
                turn_id=obs_ctx.get("turn_id"),
                parent_key=obs_ctx.get("parent_key"),
            )
        stream = getattr(agent, "stream", None) if stream_enabled else None
        if callable(stream):
            return await _with_observability_context(
                self._consume_agent_stream(
                    agent,
                    stream,
                    inputs,
                    session=session,
                    state=state,
                    caller=caller,
                    event_writer=event_writer,
                    iteration_provider=iteration_provider,
                    cancel_event=cancel_event,
                    token_callback=token_callback,
                    reasoning_summary_callback=reasoning_summary_callback,
                    event_source_node=event_source_node,
                    terminal_error_signal=terminal_error_signal,
                    terminal_stop_event=terminal_stop_event,
                ),
                observer=obs_ctx.get("observer") or NOOP_OBSERVER,
                run_id=str(obs_ctx.get("run_id") or ""),
                session_id=obs_ctx.get("session_id"),
                turn_id=obs_ctx.get("turn_id"),
                parent_key=obs_ctx.get("parent_key"),
            )
        invoke = getattr(agent, "invoke", None)
        if invoke is None:
            raise JiuwenBackendUnavailable("Configured Jiuwen agent does not expose invoke().")
        result = await _with_observability_context(
            _invoke_jiuwen_agent(agent, invoke, inputs, session=session),
            observer=obs_ctx.get("observer") or NOOP_OBSERVER,
            run_id=str(obs_ctx.get("run_id") or ""),
            session_id=obs_ctx.get("session_id"),
            turn_id=obs_ctx.get("turn_id"),
            parent_key=obs_ctx.get("parent_key"),
        )
        if terminal_error_signal is not None:
            terminal_error_signal.raise_if_set()
        return result

    async def _consume_agent_stream(
        self,
        agent: Any,
        stream: Any,
        inputs: Dict[str, Any],
        *,
        session: Any = None,
        state: Optional[Mapping[str, Any]],
        caller: str,
        event_writer: Optional[Any],
        iteration_provider: Optional[Any],
        cancel_event: Optional[threading.Event],
        token_callback: Optional[Any],
        reasoning_summary_callback: Optional[Any],
        event_source_node: Optional[str],
        terminal_error_signal: Optional[_JiuwenTerminalErrorSignal] = None,
        terminal_stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        _raise_if_jiuwen_cancelled(cancel_event, state)
        stream_result = stream(inputs, session=session) if session is not None else stream(inputs)
        if isawaitable(stream_result):
            stream_result = await stream_result

        try:
            return await self._consume_stream_chunks(
                stream_result,
                state=state,
                caller=caller,
                event_writer=event_writer,
                iteration_provider=iteration_provider,
                cancel_event=cancel_event,
                token_callback=token_callback,
                reasoning_summary_callback=reasoning_summary_callback,
                event_source_node=event_source_node,
                terminal_error_signal=terminal_error_signal,
                terminal_stop_event=terminal_stop_event,
            )
        finally:
            if terminal_stop_event is not None and terminal_stop_event.is_set():
                close_stream = getattr(stream_result, "aclose", None)
                if callable(close_stream):
                    try:
                        result = close_stream()
                        if isawaitable(result):
                            await result
                    except Exception:
                        logger.debug("Failed to close Jiuwen stream after terminal tool", exc_info=True)
            if session is not None:
                await _save_jiuwen_session_contexts(agent, session)

    async def _consume_managed_agent_stream(
        self,
        agent: Any,
        inputs: Dict[str, Any],
        *,
        session: Any = None,
        state: Optional[Mapping[str, Any]],
        caller: str,
        event_writer: Optional[Any],
        iteration_provider: Optional[Any],
        cancel_event: Optional[threading.Event],
        token_callback: Optional[Any],
        reasoning_summary_callback: Optional[Any],
        event_source_node: Optional[str],
        terminal_error_signal: Optional[_JiuwenTerminalErrorSignal] = None,
        terminal_stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        managed_session = session or _create_jiuwen_agent_session(agent, inputs)
        try:
            pre_run = getattr(managed_session, "pre_run", None)
            if callable(pre_run):
                result = pre_run(inputs=dict(inputs))
                if isawaitable(result):
                    await result
            async with asyncio.TaskGroup() as task_group:
                producer_task = task_group.create_task(
                    _produce_managed_jiuwen_stream(agent, inputs, managed_session),
                    name="jiuwen-managed-stream-producer",
                )
                try:
                    stream_result = await self._consume_stream_chunks(
                        managed_session.stream_iterator(),
                        state=state,
                        caller=caller,
                        event_writer=event_writer,
                        iteration_provider=iteration_provider,
                        cancel_event=cancel_event,
                        token_callback=token_callback,
                        reasoning_summary_callback=reasoning_summary_callback,
                        event_source_node=event_source_node,
                        terminal_error_signal=terminal_error_signal,
                        terminal_stop_event=terminal_stop_event,
                    )
                finally:
                    if not producer_task.done():
                        producer_task.cancel()
            return stream_result
        finally:
            await _save_jiuwen_session_contexts(agent, managed_session)
            commit = getattr(managed_session, "commit", None)
            if callable(commit):
                result = commit()
                if isawaitable(result):
                    await result

    async def _consume_stream_chunks(
        self,
        stream_result: Any,
        *,
        state: Optional[Mapping[str, Any]],
        caller: str,
        event_writer: Optional[Any],
        iteration_provider: Optional[Any],
        cancel_event: Optional[threading.Event],
        token_callback: Optional[Any],
        reasoning_summary_callback: Optional[Any],
        event_source_node: Optional[str],
        terminal_error_signal: Optional[_JiuwenTerminalErrorSignal] = None,
        terminal_stop_event: Optional[threading.Event] = None,
    ) -> Dict[str, Any]:
        final_result: Dict[str, Any] = {}
        stream_state: Dict[str, str] = {"content": "", "reasoning": "", "reasoning_block_id": ""}
        async for chunk in stream_result:
            _raise_if_jiuwen_cancelled(cancel_event, state)
            if terminal_error_signal is not None:
                terminal_error_signal.raise_if_set()
            if terminal_stop_event is not None and terminal_stop_event.is_set():
                break
            chunk_result = self._handle_stream_chunk(
                chunk,
                state=state,
                caller=caller,
                event_writer=event_writer,
                iteration_provider=iteration_provider,
                stream_state=stream_state,
                token_callback=token_callback,
                event_source_node=event_source_node,
            )
            if chunk_result is not None:
                final_result = chunk_result
        if terminal_error_signal is not None:
            terminal_error_signal.raise_if_set()
        if stream_state.get("reasoning") and not final_result.get("reasoning_content"):
            final_result["reasoning_content"] = stream_state["reasoning"]
        if stream_state.get("reasoning_block_id") and not final_result.get("reasoning_block_id"):
            final_result["reasoning_block_id"] = stream_state["reasoning_block_id"]
        if stream_state.get("reasoning") and stream_state.get("reasoning_block_id"):
            self._dispatch_reasoning_summary(
                state or {},
                caller=caller,
                event_writer=event_writer,
                iteration=(int(iteration_provider()) if callable(iteration_provider) else 1),
                reasoning_text=stream_state["reasoning"],
                reasoning_block_id=stream_state["reasoning_block_id"],
                reasoning_summary_callback=reasoning_summary_callback,
                event_source_node=event_source_node,
            )
        return final_result

    def _handle_stream_chunk(
        self,
        chunk: Any,
        *,
        state: Optional[Mapping[str, Any]],
        caller: str,
        event_writer: Optional[Any],
        iteration_provider: Optional[Any],
        stream_state: Optional[Dict[str, str]] = None,
        token_callback: Optional[Any] = None,
        event_source_node: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        chunk_type = str(_read_field(chunk, "type") or "").strip()
        payload = _to_mapping(_read_field(chunk, "payload") or {})
        if chunk_type in {"llm_output", "llm_reasoning"}:
            content = str(payload.get("content") or "")
            if content:
                channel = "reasoning" if chunk_type == "llm_reasoning" else "content"
                meta = None
                if channel == "reasoning":
                    meta = {
                        "reasoning_block_id": _ensure_reasoning_block_id(stream_state),
                    }
                self._emit_token_delta(
                    state or {},
                    caller=caller,
                    event_writer=event_writer,
                    channel=channel,
                    text=content,
                    iteration=(
                        int(iteration_provider())
                        if callable(iteration_provider)
                        else 1
                    ),
                    token_callback=token_callback,
                    meta=meta,
                    event_source_node=event_source_node,
                )
                _record_streamed_text(stream_state, channel, content)
                _record_jiuwen_pending_assistant_text(state, channel, content)
            return None
        if chunk_type == "answer":
            output = str(payload.get("output") or payload.get("content") or "")
            result_type = str(payload.get("result_type") or "answer")
            reasoning_content = str(payload.get("reasoning_content") or payload.get("reasoning") or "")
            if result_type.strip().lower() != "error" and _should_emit_answer_delta(stream_state, output):
                self._emit_token_delta(
                    state or {},
                    caller=caller,
                    event_writer=event_writer,
                    channel="content",
                    text=output,
                    iteration=(
                        int(iteration_provider())
                        if callable(iteration_provider)
                        else 1
                    ),
                    token_callback=token_callback,
                    event_source_node=event_source_node,
                )
                _record_streamed_text(stream_state, "content", output)
            return {
                "output": output,
                "result_type": result_type,
                **({"reasoning_content": reasoning_content} if reasoning_content else {}),
            }
        return None

    def _emit_token_delta(
        self,
        state: Mapping[str, Any],
        *,
        caller: str,
        event_writer: Optional[Any],
        channel: str,
        text: str,
        iteration: int,
        token_callback: Optional[Any] = None,
        meta: Optional[Mapping[str, Any]] = None,
        event_source_node: Optional[str] = None,
    ) -> None:
        emit_token_delta_event(
            state,
            caller=caller,
            event_writer=event_writer,
            channel=channel,
            text=text,
            iteration=iteration,
            token_callback=token_callback,
            meta=meta,
            source_node=event_source_node,
        )

    def _dispatch_reasoning_summary(
        self,
        state: Mapping[str, Any],
        *,
        caller: str,
        event_writer: Optional[Any],
        iteration: int,
        reasoning_text: str,
        reasoning_block_id: str,
        reasoning_summary_callback: Optional[Any] = None,
        event_source_node: Optional[str] = None,
    ) -> None:
        if not reasoning_text or not reasoning_block_id:
            return

        def emit_summary(block_id: str, summary: str) -> None:
            emit_reasoning_summary_event(
                state,
                caller=caller,
                event_writer=event_writer,
                iteration=iteration,
                reasoning_block_id=str(block_id or reasoning_block_id),
                summary=summary,
                reasoning_summary_callback=reasoning_summary_callback,
                source_node=event_source_node,
            )

        try:
            router = get_llm_router()
        except RuntimeError:
            return
        get_summary_service = getattr(router, "get_summary_service", None)
        if not callable(get_summary_service):
            return
        dispatch = getattr(get_summary_service(), "_dispatch_reasoning_summary", None)
        if not callable(dispatch):
            return
        dispatch(
            reasoning_text=reasoning_text,
            reasoning_block_id=reasoning_block_id,
            run_id=str(state.get("run_id") or "") or None,
            reasoning_summary_callback=emit_summary,
        )

    def _build_inputs(self, state: Mapping[str, Any]) -> Dict[str, Any]:
        query = str(state.get("user_request") or "").strip()
        if not query:
            query = _last_user_message_content(state)
        if not query:
            raise ValueError("Jiuwen ReAct runtime requires a user_request or user message.")
        query_with_context = self._append_selected_ontology_context_to_query(state, query)
        query_with_context = self._append_current_turn_tool_provenance_to_query(
            state,
            query_with_context,
        )
        return {
            "query": append_current_date_reminder_to_text(query_with_context),
            "conversation_id": str(state.get("session_id") or "default_session"),
        }

    @staticmethod
    def _append_current_turn_tool_provenance_to_query(
        state: Mapping[str, Any],
        query: str,
    ) -> str:
        from .shared.solver_route_guard import solver_tools_suppressed_from_state

        if not solver_tools_suppressed_from_state(state):
            return query
        return "\n".join(
            (
                query,
                "",
                "<Current-Turn-Tool-Provenance>",
                "Only the latest user message defines the active request.",
                (
                    "Earlier completed tool-using turns may have been omitted from "
                    "the active context."
                ),
                (
                    "Do not infer, continue, or report an omitted task unless the "
                    "latest user message explicitly requests it."
                ),
                "Do not mention unavailable or omitted tools in the answer.",
                "Use only the tools exposed for this turn.",
                "</Current-Turn-Tool-Provenance>",
            )
        )

    def _append_selected_ontology_context_to_query(self, state: Mapping[str, Any], query: str) -> str:
        selected_ontology_id = _active_ontology_id_from_state(state)
        selected_ontology_version = _selected_ontology_version_from_state(state)
        if not selected_ontology_id:
            return query
        selected_ontology_context = self._get_selected_ontology_context(state)
        lines = [
            query,
            "",
            "<Ontology-Selection-Constraint>",
            'Ontology "{}" is the active ontology for this session.'.format(selected_ontology_id),
            (
                'The selected ontology version is "{}".'.format(selected_ontology_version)
                if selected_ontology_version
                else ""
            ),
            'Always use ontology_id "{}" when calling ontology_context, business_data_search, or execute_script.'.format(
                selected_ontology_id
            ),
            (
                'Always use ontology version "{}" for this session; do not follow active_version changes.'.format(
                    selected_ontology_version
                )
                if selected_ontology_version
                else ""
            ),
            "Do not infer ontology_id from business identifiers in the user request.",
            "Do not switch to another ontology. If the selected ontology is insufficient, say so.",
            "</Ontology-Selection-Constraint>",
        ]
        if selected_ontology_context:
            lines.extend(["", selected_ontology_context])
        return "\n".join(line for line in lines if line != "").strip()

    def _get_selected_ontology_context(self, state: Mapping[str, Any]) -> Optional[str]:
        return get_selected_ontology_context(state)

    def _hydrate_selected_ontology_read_documents(self, state: Dict[str, Any]) -> None:
        hydrate_selected_ontology_read_documents(state)

    def _build_tool_context(
        self,
        state: Mapping[str, Any],
        *,
        caller: str,
        tool_event_emitter: Optional[Any],
        protocol_event_observer: Optional[Any],
        subagent_manager: Any,
        subagent_lineage: Optional[Dict[str, Any]],
        observer: Any,
        agent_iteration: int,
        traceparent: Optional[str] = None,
    ) -> ToolContext:
        workspace_id = str(state.get("workspace_id") or "").strip() or _resolve_current_workspace_id()
        return build_tool_context(
            state,
            memory_manager=self.memory_manager,
            agent_config=self.agent_config,
            caller=caller,
            agent_iteration=agent_iteration,
            workspace_id=workspace_id,
            project_root=str(get_workspace_project_root(workspace_id)),
            tool_event_emitter=tool_event_emitter,
            subagent_manager=subagent_manager,
            subagent_lineage=subagent_lineage,
            protocol_event_observer=protocol_event_observer,
            observer=observer or NOOP_OBSERVER,
            traceparent=traceparent,
        )

    def _tools_schema_for_state(self, state: Mapping[str, Any], *, caller: str) -> list[Mapping[str, Any]]:
        from ..tools.registry import get_tools_schema
        from .shared.solver_route_guard import (
            restrict_tools_for_solver_route,
            solver_route_requirement_from_state,
            solver_tools_suppressed_from_state,
        )

        schema_caller, include_orchestration, include_web_search, include_interaction = self._tool_visibility_for_state(state, caller=caller)
        workspace_id = str(state.get("workspace_id") or "").strip() or _resolve_current_workspace_id()
        include_memory = _memory_tool_requested_in_state(state)
        schemas = get_tools_schema(
            schema_caller,
            include_memory=include_memory,
            include_orchestration=include_orchestration,
            include_web_search=include_web_search,
            include_interaction=include_interaction,
            include_business_checkpoint=bool(_selected_ontology_id_from_state(state)),
            include_solver=not solver_tools_suppressed_from_state(state),
            workspace_id=workspace_id,
            selected_ontology_id=_active_ontology_id_from_state(state),
            selected_ontology_version=_active_ontology_version_from_state(state),
        )
        return restrict_tools_for_solver_route(
            schemas,
            requirement=solver_route_requirement_from_state(state),
        )

    def _jiuwen_tool_infos_for_state(self, state: Mapping[str, Any], *, caller: str) -> list[Mapping[str, Any]]:
        from .shared.solver_route_guard import (
            restrict_tools_for_solver_route,
            solver_route_requirement_from_state,
            solver_tools_suppressed_from_state,
        )

        schema_caller, include_orchestration, include_web_search, include_interaction = self._tool_visibility_for_state(state, caller=caller)
        workspace_id = str(state.get("workspace_id") or "").strip() or _resolve_current_workspace_id()
        include_memory = _memory_tool_requested_in_state(state)
        infos = get_jiuwen_tool_infos(
            schema_caller,
            include_memory=include_memory,
            include_orchestration=include_orchestration,
            include_web_search=include_web_search,
            include_interaction=include_interaction,
            include_business_checkpoint=bool(_selected_ontology_id_from_state(state)),
            include_solver=not solver_tools_suppressed_from_state(state),
            workspace_id=workspace_id,
            selected_ontology_id=_active_ontology_id_from_state(state),
            selected_ontology_version=_active_ontology_version_from_state(state),
        )
        return restrict_tools_for_solver_route(
            infos,
            requirement=solver_route_requirement_from_state(state),
        )

    def _tool_visibility_for_state(self, state: Mapping[str, Any], *, caller: str) -> tuple[str, bool, bool, bool]:
        agent_mode = _resolve_agent_mode(state.get("user_context"))
        include_web_search = bool(getattr(getattr(self.agent_config, "web_search", None), "enabled", False))
        schema_caller = "main" if caller == "acp" else caller
        include_interaction = _user_interaction_supported_from_state(state)
        return schema_caller, agent_mode == "multi_agent", include_web_search, include_interaction

    def _emit_iteration_completed(
        self,
        state: Mapping[str, Any],
        *,
        caller: str,
        event_writer: Optional[Any],
        iteration: int,
        status: str,
        prompt_elapsed_ms: int,
        llm_elapsed_ms: int,
        llm_stream_elapsed_ms: int,
        llm_wall_elapsed_ms: int,
        tools_elapsed_ms: int,
        total_elapsed_ms: int,
        tool_names: list[str],
        model_elapsed_ms: Optional[int] = None,
        setup_elapsed_ms: Optional[int] = None,
        tool_timing_metrics: Optional[Mapping[str, int]] = None,
        event_source_node: Optional[str] = None,
    ) -> None:
        emit_iteration_completed_event(
            state,
            caller=caller,
            event_writer=event_writer,
            iteration=iteration,
            status=status,
            prompt_elapsed_ms=prompt_elapsed_ms,
            llm_elapsed_ms=llm_elapsed_ms,
            llm_stream_elapsed_ms=llm_stream_elapsed_ms,
            llm_wall_elapsed_ms=llm_wall_elapsed_ms,
            tools_elapsed_ms=tools_elapsed_ms,
            total_elapsed_ms=total_elapsed_ms,
            tool_names=tool_names,
            model_elapsed_ms=model_elapsed_ms,
            setup_elapsed_ms=setup_elapsed_ms,
            tool_timing_metrics=tool_timing_metrics,
            source_node=event_source_node,
        )


def is_openjiuwen_available() -> bool:
    return importlib.util.find_spec("openjiuwen") is not None


def _main_event_source_node(caller: str, source_node: Any) -> Optional[str]:
    if str(caller or "").strip() != "main":
        return None
    normalized = str(source_node or "").strip()
    return normalized or None


def require_openjiuwen() -> None:
    if is_openjiuwen_available():
        return
    raise JiuwenBackendUnavailable(
        "Jiuwen ReAct runtime requires the 'openjiuwen' package. "
        "Install backend dependencies from backend/requirements.txt before running the main agent loop."
    )


_JIUWEN_LOG_CONFIGURED = False


def _configure_jiuwen_log_path() -> None:
    """Direct openjiuwen's own log files into our log directory to avoid logs/logs/ nesting."""
    global _JIUWEN_LOG_CONFIGURED
    if _JIUWEN_LOG_CONFIGURED:
        return
    try:
        from openjiuwen.core.common.logging.log_config import configure_log_config
        from utils.logging import LOG_DIR
        import os
        jiuwen_log_path = os.path.join(LOG_DIR, "jiuwen") + os.sep
        configure_log_config({"log_path": jiuwen_log_path})
        _JIUWEN_LOG_CONFIGURED = True
    except Exception:
        pass


def create_default_jiuwen_agent(
    *,
    llm_config: Any,
    max_iterations: Optional[int] = None,
    system_prompt: Optional[str] = None,
    agent_id: Optional[str] = None,
    context_max_rounds: Optional[int] = None,
    enable_reasoning: bool = False,
    stream_enabled: bool = True,
    parallel_default_max_workers: int = 10,
    tool_choice: Optional[str] = None,
) -> Any:
    _configure_jiuwen_log_path()
    try:
        agent_card_module = importlib.import_module("openjiuwen.core.single_agent.schema.agent_card")
        react_agent_module = importlib.import_module("openjiuwen.core.single_agent.agents.react_agent")
        model_config_module = importlib.import_module("openjiuwen.core.foundation.llm.schema.config")
        context_engine_module = importlib.import_module("openjiuwen.core.context_engine")
    except ImportError as exc:
        raise JiuwenBackendUnavailable(
            _format_jiuwen_import_error(exc)
        ) from exc
    try:
        openai_client_module = importlib.import_module(
            "openjiuwen.core.foundation.llm.model_clients.openai_model_client"
        )
    except ImportError:
        openai_client_module = None
    _patch_jiuwen_openai_reasoning_alias(openai_client_module)

    agent_card_cls = agent_card_module.AgentCard
    react_agent_cls = react_agent_module.ReActAgent
    react_agent_config_cls = react_agent_module.ReActAgentConfig
    model_client_config_cls = model_config_module.ModelClientConfig
    model_request_config_cls = model_config_module.ModelRequestConfig

    connection_policy = resolve_model_connection_policy(
        configured_timeout=getattr(llm_config, "timeout", None)
    )
    model_client_config = model_client_config_cls(
        client_provider=_map_jiuwen_provider(getattr(llm_config, "provider", None)),
        api_key=str(getattr(llm_config, "api_key", "") or ""),
        api_base=str(getattr(llm_config, "base_url", "") or ""),
        timeout=connection_policy.timeout,
        max_retries=connection_policy.max_retries,
        verify_ssl=False,
    )
    reasoning_extra_body = get_provider_adapter(
        getattr(llm_config, "provider", None)
    ).extra_body(enable_reasoning=bool(enable_reasoning))
    model_request_kwargs = {
        "model": str(getattr(llm_config, "model", "") or ""),
        "temperature": float(getattr(llm_config, "temperature", 0.95)),
        "max_tokens": getattr(llm_config, "max_tokens", None),
    }
    if reasoning_extra_body:
        model_request_kwargs["extra_body"] = reasoning_extra_body
    if tool_choice is not None:
        if tool_choice != "required":
            raise ValueError("tool_choice must be required or None")
        model_request_kwargs["tool_choice"] = tool_choice
    model_request_kwargs.update(
        get_provider_adapter(getattr(llm_config, "provider", None)).top_level_params(
            stream=bool(stream_enabled)
        )
    )
    model_request_config = model_request_config_cls(**model_request_kwargs)
    context_engine_config_cls = context_engine_module.ContextEngineConfig
    context_engine_config = context_engine_config_cls(
        max_context_message_num=None,
        default_window_round_num=_normalize_context_max_rounds(context_max_rounds),
        enable_reload=False,
    )
    react_config = react_agent_config_cls(
        model_client_config=model_client_config,
        model_config_obj=model_request_config,
        model_name=str(getattr(llm_config, "model", "") or ""),
        model_provider=_map_jiuwen_provider(getattr(llm_config, "provider", None)),
        max_iterations=_normalize_max_iterations(max_iterations),
        prompt_template=(
            [{"role": "system", "content": system_prompt}]
            if str(system_prompt or "").strip()
            else []
        ),
        context_engine_config=context_engine_config,
        kv_cache_affinity_config={
            "enable_kv_cache_release": False,
            "enable_kv_cache_affinity": False,
        },
        context_processors=[],
        parallel_tool_calls=True,
    )
    agent = react_agent_cls(
        card=agent_card_cls(
            id=str(agent_id or "onto_agent_jiuwen_react"),
            name="onto_agent_jiuwen_react",
            description="onto-agent Jiuwen ReAct runtime",
        )
    )
    configure = getattr(agent, "configure", None)
    if callable(configure):
        configured_agent = configure(react_config)
        if configured_agent is not None:
            agent = configured_agent
    _install_jiuwen_concurrency_ability_manager(
        agent,
        max_parallel_workers=parallel_default_max_workers,
    )
    return agent


def _terminal_tool_call_id(batch_result: Any) -> str:
    explicit_tool_call_id = str(
        getattr(batch_result, "terminal_tool_call_id", None) or ""
    ).strip()
    if explicit_tool_call_id:
        return explicit_tool_call_id
    for message in reversed(list(getattr(batch_result, "messages", None) or [])):
        if not isinstance(message, Mapping):
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_call_id:
            return tool_call_id
    return ""


def _remember_jiuwen_terminal_tool_batch(
    terminal_result: Dict[str, Any],
    batch_result: Any,
) -> None:
    """Correlate a Consumer terminal result with the active model call."""

    terminal_result["batch_result"] = batch_result
    terminal_result["tool_call_id"] = _terminal_tool_call_id(batch_result)
    identity = _current_jiuwen_tool_call_identity()
    if identity is None:
        terminal_result.pop("model_tool_call_id", None)
        terminal_result.pop("correlation_token", None)
        return
    terminal_result["model_tool_call_id"] = identity.model_tool_call_id
    terminal_result["correlation_token"] = identity.correlation_token


def warm_up_jiuwen_runtime(
    *,
    agent_config: Any = None,
    cancel_event: Any = None,
) -> dict[str, Any]:
    """Preload Jiuwen runtime modules and global resources without invoking the LLM.

    This creates a throwaway warm-up agent to trigger module imports, class
    loading and global registry initialization, then discards the agent. Runner
    ownership remains with the already-started ASGI lifespan.
    """
    started = time.perf_counter()

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError
    require_openjiuwen()
    _configure_jiuwen_log_path()

    llm_config = _resolve_jiuwen_llm_config(
        {},
        caller="main",
        agent_config=agent_config,
    )

    if cancel_event is not None and cancel_event.is_set():
        raise asyncio.CancelledError

    agent = create_default_jiuwen_agent(
        llm_config=llm_config,
        max_iterations=1,
        system_prompt="warmup",
        agent_id="onto_agent_jiuwen_react.warmup",
        enable_reasoning=False,
        stream_enabled=True,
        parallel_default_max_workers=getattr(
            agent_config,
            "parallel_default_max_workers",
            10,
        ),
    )

    get_jiuwen_tool_infos("main")
    importlib.import_module("openjiuwen.core.foundation.tool")
    importlib.import_module("openjiuwen.core.runner")

    del agent

    return {
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "mode": "jiuwen-runtime",
    }


def _map_jiuwen_provider(provider: Any) -> str:
    normalized = str(provider or "").strip().lower()
    if normalized in {"", "openai", "openai_compatible", "glm"}:
        return "OpenAI"
    if normalized in {"dashscope", "qwen"}:
        return "DashScope"
    if normalized == "deepseek":
        return "DeepSeek"
    if normalized == "openrouter":
        return "OpenRouter"
    if normalized == "siliconflow":
        return "SiliconFlow"
    return str(provider or "OpenAI")


def _build_jiuwen_log_context(
    state: Mapping[str, Any],
    *,
    caller: str,
    subagent_lineage: Optional[Mapping[str, Any]],
) -> Dict[str, Any]:
    lineage = dict(subagent_lineage or {})
    context: Dict[str, Any] = {
        "agent_type": caller,
        "agent_id": "main" if caller == "main" else lineage.get("subagent_id"),
        "run_id": state.get("run_id"),
        "session_id": state.get("session_id"),
        "turn_id": state.get("current_turn_id"),
    }
    if caller == "subagent":
        context.update(
            {
                "task_id": lineage.get("task_id"),
                "group_id": lineage.get("group_id"),
                "depth": lineage.get("depth"),
                "parent_subagent_id": lineage.get("parent_subagent_id"),
                "root_tool_call_id": lineage.get("root_tool_call_id"),
            }
        )
    return context


def _resolve_max_iterations(
    iteration_limit: Optional[int],
    agent_config: Any,
    *,
    caller: str = "main",
) -> int:
    if iteration_limit is not None:
        return _normalize_max_iterations(iteration_limit)
    if str(caller or "").strip() == "subagent":
        return _normalize_max_iterations(
            getattr(agent_config, "sub_agent_max_iterations", None)
        )
    return _normalize_max_iterations(
        getattr(agent_config, "main_agent_max_iterations", None)
    )


def _normalize_max_iterations(value: Optional[Any]) -> int:
    try:
        normalized = int(value)
    except (TypeError, ValueError):
        return 50
    return max(1, normalized)


def _normalize_context_max_rounds(value: Optional[Any]) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return 10


def _resolve_effective_context_max_rounds(
    configured_rounds: int,
    state: Optional[Mapping[str, Any]],
) -> int:
    """Return the effective ``default_window_round_num`` for the agent's
    ContextEngineConfig.

    For ask-user resume requests (``interaction_response`` present in
    ``user_context``) the effective rounds are clamped to at least 2 so the
    model always sees the preceding user turn and the interaction context
    needed to continue the conversation.

    For normal requests the configured value is used unchanged; the
    ContextEngine is solely responsible for applying the window.
    """
    user_context = dict((state or {}).get("user_context") or {})
    interaction_response = user_context.get("interaction_response")
    if interaction_response:
        return max(configured_rounds, 2)
    return configured_rounds


def _reject_factory_agent_with_history(
    state: Optional[Mapping[str, Any]],
    *,
    history_source_messages: Optional[list[Dict[str, Any]]] = None,
    interaction_response: Any = None,
) -> None:
    """Reject factory-agent invocations that carry conversation history or an
    ask-user interaction response.

    The ``_agent_factory`` path bypasses the default Jiuwen ReAct agent and
    its ContextEngineConfig; it is only safe for single-turn, stateless
    requests.  When prior messages or an ``interaction_response`` exist, the
    factory cannot guarantee correct window management and must be rejected.
    """
    if not state:
        return
    resolved_interaction_response = interaction_response
    if resolved_interaction_response is None:
        resolved_interaction_response = dict(state.get("user_context") or {}).get(
            "interaction_response"
        )
    if resolved_interaction_response:
        raise ValueError(
            "_agent_factory cannot be used for ask-user resume requests "
            "with interaction_response; use the default agent path instead."
        )
    history_source = (
        history_source_messages
        if history_source_messages is not None
        else _build_jiuwen_history_source_messages(state)
    )
    if history_source:
        raise ValueError(
            "_agent_factory cannot be used with conversation history; "
            "use the default agent path instead."
        )


_PREFLIGHT_MAIN_LLM_CONFIG: ContextVar[Any] = ContextVar(
    "jiuwen_preflight_main_llm_config",
    default=None,
)


@contextmanager
def jiuwen_preflight_llm_config_context(
    model_profile: Optional[Mapping[str, Any]],
):
    """Bind an admitted MAIN profile without exposing its secrets in state."""

    llm_config = None
    if model_profile is not None:
        from config import LLMConfig

        llm_config = LLMConfig.from_mapping(dict(model_profile))
    token = _PREFLIGHT_MAIN_LLM_CONFIG.set(llm_config)
    try:
        yield
    finally:
        _PREFLIGHT_MAIN_LLM_CONFIG.reset(token)


def _resolve_jiuwen_llm_config(
    state: Optional[Mapping[str, Any]],
    *,
    caller: str,
    agent_config: Any = None,
) -> Any:
    user_context = dict((state or {}).get("user_context") or {})
    profile_id = str(user_context.get("model_profile_id") or "").strip()
    workspace_id = (
        str((state or {}).get("workspace_id") or "").strip()
        or _resolve_current_workspace_id()
    )
    purpose = LLMPurpose.MAIN if str(caller or "").strip() in {"main", "acp"} else LLMPurpose.SUBAGENT
    admitted_config = _PREFLIGHT_MAIN_LLM_CONFIG.get()
    if purpose == LLMPurpose.MAIN and admitted_config is not None:
        return _clamp_jiuwen_llm_config_output_tokens(
            admitted_config,
            agent_config,
        )
    try:
        router = get_llm_router()
    except RuntimeError:
        if profile_id:
            raise
        from config import AppConfig

        app_config = AppConfig.from_env()
        fallback = app_config.llm if purpose == LLMPurpose.MAIN else app_config.task_llm
        return _clamp_jiuwen_llm_config_output_tokens(fallback, agent_config)

    resolved_agent_config = agent_config or getattr(router, "agent_config", None)
    if profile_id:
        return _clamp_jiuwen_llm_config_output_tokens(
            router.get_config(
                purpose,
                profile_id=profile_id,
                workspace_id=workspace_id,
            ),
            resolved_agent_config,
        )
    try:
        return _clamp_jiuwen_llm_config_output_tokens(
            router.get_config(purpose, workspace_id=workspace_id),
            resolved_agent_config,
        )
    except RuntimeError:
        from config import AppConfig

        app_config = AppConfig.from_env()
        fallback = app_config.llm if purpose == LLMPurpose.MAIN else app_config.task_llm
        return _clamp_jiuwen_llm_config_output_tokens(fallback, resolved_agent_config)


def _clamp_jiuwen_llm_config_output_tokens(llm_config: Any, agent_config: Any) -> Any:
    raw_limit = getattr(
        getattr(agent_config, "limits", None),
        "model_output_token_limit",
        None,
    )
    try:
        output_limit = max(1, int(raw_limit))
    except (TypeError, ValueError):
        return llm_config
    try:
        requested_max_tokens = max(1, int(getattr(llm_config, "max_tokens", None)))
    except (TypeError, ValueError):
        return llm_config
    resolved_max_tokens = min(requested_max_tokens, output_limit)
    if resolved_max_tokens == requested_max_tokens:
        return llm_config
    if is_dataclass(llm_config):
        return replace(llm_config, max_tokens=resolved_max_tokens)
    cloned = copy(llm_config)
    setattr(cloned, "max_tokens", resolved_max_tokens)
    return cloned


def _is_tool_concurrency_safe(tool_name: str) -> bool:
    try:
        from ..tools.registry import get_tool_spec
    except Exception:
        return False
    spec = get_tool_spec(str(tool_name or ""))
    if spec is None:
        return False
    return bool(getattr(spec, "concurrency_safe", False))


def _resolve_current_workspace_id() -> Optional[str]:
    try:
        return get_current_workspace_id()
    except Exception:
        return None


def _build_jiuwen_history_messages(
    state: Mapping[str, Any],
    agent_config: Any,
    *,
    history_source_messages: Optional[list[Dict[str, Any]]] = None,
) -> list[Any]:
    del agent_config
    history_messages = (
        history_source_messages
        if history_source_messages is not None
        else _build_jiuwen_history_source_messages(state)
    )
    converted: list[Any] = []
    for message in history_messages:
        converted_message = _to_jiuwen_history_message(message)
        if converted_message is not None:
            converted.append(converted_message)
    return converted


def _build_jiuwen_history_source_messages(state: Mapping[str, Any]) -> list[Dict[str, Any]]:
    """Return the exact persisted history source used for ContextEngine seeding."""
    messages = [
        dict(message)
        for message in list(state.get("messages") or [])
        if isinstance(message, Mapping)
    ]
    history = _drop_current_user_message(messages, state)
    from .shared.solver_route_guard import (
        filter_solver_history_messages,
        solver_tools_suppressed_from_state,
    )

    if solver_tools_suppressed_from_state(state):
        return filter_solver_history_messages(history)
    return history


def _build_jiuwen_context_messages(
    state: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any],
    system_prompt: Optional[str],
    agent_config: Any,
) -> list[Dict[str, Any]]:
    """Build the immutable run-start snapshot used by legacy trace metrics.

    This production helper intentionally includes the complete persisted
    history plus the current query.  It is not the ContextEngine output, is
    never sent to the model, and is reused for every ``iteration_prepared``
    event in the run.  Consequently its message/character counts can be
    larger than the final model window.
    """
    del agent_config
    messages = [
        dict(message)
        for message in list(state.get("messages") or [])
        if isinstance(message, Mapping)
    ]
    estimated_messages = [
        dict(message)
        for message in messages
        if isinstance(message, Mapping) and message.get("role") != "system"
    ]
    query = str(inputs.get("query") or "").strip()
    if estimated_messages and estimated_messages[-1].get("role") == "user":
        estimated_messages[-1] = {
            **estimated_messages[-1],
            "content": query,
        }
    elif query:
        estimated_messages.append({"role": "user", "content": query})
    if str(system_prompt or "").strip():
        return [{"role": "system", "content": str(system_prompt)}, *estimated_messages]
    return estimated_messages


def _estimate_jiuwen_messages_chars(messages: list[Mapping[str, Any]]) -> int:
    total = 0
    for message in messages or []:
        if not isinstance(message, Mapping):
            continue
        try:
            total += len(json.dumps(message, ensure_ascii=False, default=str))
        except TypeError:
            total += len(str(message))
    return total


def _drop_current_user_message(messages: list[Dict[str, Any]], state: Mapping[str, Any]) -> list[Dict[str, Any]]:
    if not messages:
        return []
    trimmed = [message for message in messages if message.get("role") != "system"]
    if not trimmed:
        return []
    current_query = str(state.get("user_request") or "").strip() or _last_user_message_content({"messages": trimmed})
    if trimmed[-1].get("role") == "user":
        latest_user_content = _string_content(trimmed[-1].get("content"))
        if not current_query or latest_user_content == current_query:
            return trimmed[:-1]
    return trimmed


def _to_jiuwen_history_message(message: Mapping[str, Any]) -> Optional[Any]:
    role = str(message.get("role") or "").strip()
    if role == "system":
        return None
    classes = _jiuwen_message_classes()
    content = _jiuwen_message_content(message.get("content"))
    if role == "user":
        return classes["user"](content=content)
    if role == "assistant":
        payload: Dict[str, Any] = {"content": content}
        if message.get("tool_calls"):
            payload["tool_calls"] = deepcopy(message.get("tool_calls"))
        reasoning_content = message.get("reasoning_content")
        if reasoning_content:
            payload["reasoning_content"] = str(reasoning_content)
        return classes["assistant"](**payload)
    if role == "tool":
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if not tool_call_id:
            return None
        payload = {
            "content": content,
            "tool_call_id": tool_call_id,
        }
        name = str(message.get("name") or "").strip()
        if name:
            payload["name"] = name
        return classes["tool"](**payload)
    return None


def _jiuwen_message_classes() -> Dict[str, Any]:
    try:
        message_module = importlib.import_module("openjiuwen.core.foundation.llm.schema.message")
    except ImportError as exc:
        raise JiuwenBackendUnavailable(
            _format_jiuwen_import_error(exc)
        ) from exc
    return {
        "user": message_module.UserMessage,
        "assistant": message_module.AssistantMessage,
        "tool": message_module.ToolMessage,
    }


def _jiuwen_message_content(content: Any) -> Any:
    if isinstance(content, list):
        return deepcopy(content)
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


def _string_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if content is None:
        return ""
    return str(content)


async def _seed_jiuwen_context_from_history(
    agent: Any,
    inputs: Mapping[str, Any],
    *,
    history_messages: list[Any],
) -> Optional[Any]:
    context_engine = getattr(agent, "context_engine", None)
    create_context = getattr(context_engine, "create_context", None)
    if not callable(create_context):
        return None
    session = _create_jiuwen_agent_session(agent, inputs)
    pre_run = getattr(session, "pre_run", None)
    if callable(pre_run):
        result = pre_run(inputs=dict(inputs))
        if isawaitable(result):
            await result
    result = create_context(session=session, history_messages=history_messages)
    if isawaitable(result):
        await result
    return session


def _create_jiuwen_agent_session(agent: Any, inputs: Mapping[str, Any]) -> Any:
    try:
        session_module = importlib.import_module("openjiuwen.core.session.agent")
    except ImportError as exc:
        raise JiuwenBackendUnavailable(
            _format_jiuwen_import_error(exc)
        ) from exc
    conversation_id = str(inputs.get("conversation_id") or "default_session")
    return session_module.create_agent_session(
        session_id=conversation_id,
        card=getattr(agent, "card", None),
    )


def _supports_managed_jiuwen_stream(agent: Any) -> bool:
    return callable(getattr(agent, "invoke", None)) and callable(
        getattr(agent, "write_invoke_result_to_stream", None)
    )


async def _produce_managed_jiuwen_stream(
    agent: Any,
    inputs: Dict[str, Any],
    session: Any,
) -> None:
    write_result = getattr(agent, "write_invoke_result_to_stream")
    try:
        try:
            result = agent.invoke(inputs, session=session, _streaming=True)
            if isawaitable(result):
                result = await result
            if isinstance(result, list):
                for chunk in result:
                    await session.write_stream(chunk)
            else:
                written = write_result(result, session)
                if isawaitable(written):
                    await written
        except Exception as exc:
            written = write_result(
                {
                    "output": str(getattr(exc, "public_message", None) or exc),
                    "result_type": "error",
                },
                session,
            )
            if isawaitable(written):
                await written
    finally:
        close_stream = getattr(session, "close_stream", None)
        if callable(close_stream):
            closed = close_stream()
            if isawaitable(closed):
                await closed


async def _invoke_jiuwen_agent(agent: Any, invoke: Any, inputs: Dict[str, Any], *, session: Any = None) -> Any:
    try:
        result = invoke(inputs, session=session) if session is not None else invoke(inputs)
        if isawaitable(result):
            return await result
        return result
    finally:
        if session is not None:
            await _finalize_jiuwen_session(agent, session)


async def _finalize_jiuwen_session(agent: Any, session: Any) -> None:
    await _save_jiuwen_session_contexts(agent, session)
    close_stream = getattr(session, "close_stream", None)
    if callable(close_stream):
        result = close_stream()
        if isawaitable(result):
            await result
    commit = getattr(session, "commit", None)
    if callable(commit):
        result = commit()
        if isawaitable(result):
            await result


async def _save_jiuwen_session_contexts(agent: Any, session: Any) -> None:
    context_engine = getattr(agent, "context_engine", None)
    save_contexts = getattr(context_engine, "save_contexts", None)
    if callable(save_contexts):
        result = save_contexts(session)
        if isawaitable(result):
            await result


def _build_owned_jiuwen_session_id(agent: Any) -> str:
    """Return a per-agent internal session id owned by this runtime invocation."""
    agent_id = str(getattr(getattr(agent, "card", None), "id", "") or "").strip()
    if not agent_id:
        agent_id = "onto_agent_jiuwen_react"
    return "{}.session.{}".format(agent_id, uuid.uuid4().hex)


async def _run_best_effort_jiuwen_cleanup_async(step: str, cleanup: Any) -> None:
    """Await one cleanup step without changing the invocation outcome."""
    try:
        result = cleanup()
        if isawaitable(result):
            await result
    except Exception:
        logger.warning("OpenJiuwen cleanup step failed: %s", step, exc_info=True)


def _owned_jiuwen_agent_event_names(agent: Any) -> set[str]:
    manager = getattr(agent, "agent_callback_manager", None)
    if manager is None:
        return set()
    event_cls = _jiuwen_agent_callback_event_class()
    if event_cls is None:
        return set()
    try:
        events = list(event_cls)
    except TypeError:
        events = [
            getattr(event_cls, name)
            for name in dir(event_cls)
            if name.isupper() and not name.startswith("_")
        ]
    get_agent_event = getattr(manager, "_get_agent_event", None)
    if callable(get_agent_event):
        return {str(get_agent_event(event)) for event in events}
    agent_id = str(
        getattr(manager, "agent_id", None)
        or getattr(getattr(agent, "card", None), "id", "")
        or ""
    ).strip()
    if not agent_id:
        return set()
    return {"{}_{}".format(agent_id, event) for event in events}


def _remove_owned_jiuwen_callback_metrics(
    runner: Any,
    *,
    agent: Any,
    owned_event_names: set[str],
) -> None:
    framework = getattr(runner, "callback_framework", None)
    metrics = getattr(framework, "_metrics", None)
    if metrics is None or not callable(getattr(metrics, "pop", None)):
        return
    agent_id = str(getattr(getattr(agent, "card", None), "id", "") or "").strip()
    owned_prefix = "{}_".format(agent_id) if agent_id else ""
    for key in tuple(metrics.keys()):
        event_name = str(key).split(":", 1)[0]
        if event_name in owned_event_names or (
            not owned_event_names
            and owned_prefix
            and event_name.startswith(owned_prefix)
        ):
            metrics.pop(key, None)


async def _dispose_owned_jiuwen_runtime(
    agent: Any,
    *,
    session_id: str,
) -> None:
    """Release callback and checkpoint state owned by one ephemeral agent.

    Every operation runs on the singleton Jiuwen loop after the agent invocation
    has finished. Nested ``finally`` blocks ensure a failed callback cleanup does
    not skip metric pruning or the public session release.
    """
    runner: Any = None
    owned_event_names: set[str] = set()
    try:
        runner_module = importlib.import_module("openjiuwen.core.runner")
        runner = getattr(runner_module, "Runner", None)
        owned_event_names = _owned_jiuwen_agent_event_names(agent)
        manager = getattr(agent, "agent_callback_manager", None)
        clear = getattr(manager, "clear", None)
        if callable(clear):
            result = clear()
            if isawaitable(result):
                await result
    finally:
        try:
            if runner is not None:
                _remove_owned_jiuwen_callback_metrics(
                    runner,
                    agent=agent,
                    owned_event_names=owned_event_names,
                )
        finally:
            release = getattr(runner, "release", None) if runner is not None else None
            if callable(release):
                result = release(session_id)
                if isawaitable(result):
                    await result


async def _with_observability_context(
    awaitable: Any,
    *,
    observer: Any,
    run_id: str,
    session_id: Optional[str],
    turn_id: Optional[int],
    parent_key: Optional[str],
) -> Any:
    """Bind observability context for the native async Jiuwen invocation."""
    with profile_current_thread(run_id):
        with bind_observability_context(
            observer=observer,
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            parent_key=parent_key,
        ):
            return await awaitable
def _raise_if_jiuwen_cancelled(
    cancel_event: Optional[threading.Event],
    state: Optional[Mapping[str, Any]],
) -> None:
    if cancel_event is None or not cancel_event.is_set():
        return
    run_id = str((state or {}).get("run_id") or "").strip() or None
    raise WorkflowCancelledError(
        "Workflow cancelled by user",
        details={"run_id": run_id},
    )


def _get_jiuwen_cancel_event(state: Mapping[str, Any]) -> Optional[threading.Event]:
    run_id = str((state or {}).get("run_id") or "").strip()
    if not run_id:
        return None
    try:
        router = get_llm_router()
    except RuntimeError:
        return None
    get_cancel_event = getattr(router, "get_cancel_event", None)
    if not callable(get_cancel_event):
        return None
    return get_cancel_event(run_id)


def _run_jiuwen_setup_step(runtime_trace: AgentRuntimeTrace, step: Any, *args: Any, **kwargs: Any) -> Any:
    try:
        return step(*args, **kwargs)
    except WorkflowCancelledError:
        runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
        raise
    except Exception:
        runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
        raise


def _extract_output(result: Any) -> str:
    if isinstance(result, Mapping):
        return str(result.get("output") or "")
    return str(result or "")


def _terminal_tool_call_id(batch_result: Any) -> str:
    explicit_tool_call_id = str(
        getattr(batch_result, "terminal_tool_call_id", None) or ""
    ).strip()
    if explicit_tool_call_id:
        return explicit_tool_call_id
    for message in reversed(list(getattr(batch_result, "messages", None) or [])):
        if not isinstance(message, Mapping):
            continue
        tool_call_id = str(message.get("tool_call_id") or "").strip()
        if tool_call_id:
            return tool_call_id
    return ""


def _remember_jiuwen_terminal_tool_batch(
    terminal_result: Dict[str, Any],
    batch_result: Any,
) -> None:
    """Correlate a Consumer terminal result with the active model call."""

    terminal_result["batch_result"] = batch_result
    terminal_result["tool_call_id"] = _terminal_tool_call_id(batch_result)
    identity = _current_jiuwen_tool_call_identity()
    if identity is None:
        terminal_result.pop("model_tool_call_id", None)
        terminal_result.pop("correlation_token", None)
        return
    terminal_result["model_tool_call_id"] = identity.model_tool_call_id
    terminal_result["correlation_token"] = identity.correlation_token


def _terminal_force_finish_output(batch_result: Any) -> str:
    terminal_content = getattr(batch_result, "terminal_content", None)
    if terminal_content is not None:
        return str(terminal_content)
    return _first_tool_message_content(batch_result)


def _extract_reasoning_content(result: Any) -> str:
    if not isinstance(result, Mapping):
        return ""
    return str(result.get("reasoning_content") or result.get("reasoning") or "")


def _is_error_result(result: Any) -> bool:
    if not isinstance(result, Mapping):
        return False
    return str(result.get("result_type") or "").strip().lower() == "error"


def _record_streamed_text(stream_state: Optional[Dict[str, str]], channel: str, text: str) -> None:
    if not isinstance(stream_state, dict) or not text:
        return
    key = "reasoning" if channel == "reasoning" else "content"
    stream_state[key] = "{}{}".format(stream_state.get(key) or "", text)


def _ensure_reasoning_block_id(stream_state: Optional[Dict[str, str]]) -> str:
    if not isinstance(stream_state, dict):
        return "reasoning_{}_1".format(uuid.uuid4().hex[:8])
    block_id = str(stream_state.get("reasoning_block_id") or "").strip()
    if block_id:
        return block_id
    block_id = "reasoning_{}_1".format(uuid.uuid4().hex[:8])
    stream_state["reasoning_block_id"] = block_id
    return block_id


def _should_emit_answer_delta(stream_state: Optional[Mapping[str, str]], output: str) -> bool:
    normalized_output = str(output or "").strip()
    if not normalized_output:
        return False
    streamed_content = ""
    if isinstance(stream_state, Mapping):
        streamed_content = str(stream_state.get("content") or "")
    return not streamed_content.strip().endswith(normalized_output)


def _memory_tool_requested_in_state(state: Mapping[str, Any]) -> bool:
    from ..tools.memory import has_explicit_memory_request

    messages = [
        dict(message)
        for message in list(state.get("messages") or [])
        if isinstance(message, Mapping)
    ]
    current_request = str(state.get("user_request") or "").strip()
    should_append_request = bool(current_request)
    if should_append_request and messages:
        last_message = messages[-1]
        if last_message.get("role") == "user":
            last_content = str(last_message.get("content") or "").strip()
            should_append_request = last_content != current_request
    if should_append_request:
        messages.append({"role": "user", "content": current_request})
    return has_explicit_memory_request(messages)


def _last_user_message_content(state: Mapping[str, Any]) -> str:
    for message in reversed(list(state.get("messages") or [])):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "user":
            continue
        return str(message.get("content") or "").strip()
    return ""
