"""Boundary helpers for the openJiuwen ReAct runtime.

The current runtime keeps OpenAI-compatible nested tool schemas and tool calls.
openJiuwen uses flat ToolInfo/ToolCall objects, so this module owns only that
translation layer and lazy dependency checks.
"""

from __future__ import annotations

import importlib.util
import asyncio
import concurrent.futures
import json
import threading
import time
import uuid
from collections.abc import Mapping
from copy import copy, deepcopy
from dataclasses import is_dataclass, replace
from inspect import isawaitable
from typing import Any, Dict, Optional

from ..exceptions import InputTokenLimitError, WorkflowCancelledError
from ..observability import AgentRuntimeTrace, NOOP_OBSERVER, RuntimeStatus
from ..services.llm import LLMPurpose, get_llm_router
from ..services.llm.provider_adapters import get_provider_adapter
from ..tools.executor import execute_tool_batch
from ..tools.base import ToolContext
from ..utils.context_window import apply_sliding_window
from ..utils.llm_request_mode import is_llm_streaming_enabled
from ..utils.logging import bind_log_context
from ..utils.token_budget import estimate_messages_tokens
from ..workspaces import (
    get_current_workspace_id,
    get_workspace_project_root,
)
from .jiuwen.common import (
    JiuwenBackendUnavailable,
    coerce_optional_int as _coerce_optional_int,
    format_jiuwen_import_error as _format_jiuwen_import_error,
    normalize_arguments as _normalize_arguments,
    object_to_dict as _object_to_dict,
    read_field as _read_field,
    to_mapping as _to_mapping,
)
from .jiuwen.patches import patch_jiuwen_openai_reasoning_alias as _patch_jiuwen_openai_reasoning_alias
from .jiuwen.tools import (
    CurrentRuntimeToolAdapter,
    JiuwenTerminalToolBatch as _JiuwenTerminalToolBatch,
    apply_tool_batch_result_to_state,
    clear_jiuwen_pending_assistant_context as _clear_jiuwen_pending_assistant_context,
    first_tool_message_content as _first_tool_message_content,
    build_jiuwen_agent_resource_id as _build_jiuwen_agent_resource_id,
    get_jiuwen_tool_infos,
    record_jiuwen_pending_assistant_text as _record_jiuwen_pending_assistant_text,
    register_current_runtime_tools as _register_current_runtime_tools,
)
from .jiuwen.trace import (
    JiuwenModelTraceBridge as _BaseJiuwenModelTraceBridge,
    jiuwen_agent_callback_event_class as _default_jiuwen_agent_callback_event_class,
)
from .shared.events import (
    emit_iteration_completed_event,
    emit_reasoning_summary_event,
    emit_token_delta_event,
)
from .shared.messages import append_current_date_reminder_to_text
from .shared.modes import resolve_agent_mode as _resolve_agent_mode
from .shared.ontology_context import (
    get_selected_ontology_context,
    hydrate_selected_ontology_read_documents,
    selected_ontology_id_from_state as _selected_ontology_id_from_state,
)
from .shared.prompting import build_main_system_prompt
from .shared.timing import (
    build_tool_timing_metrics as _build_jiuwen_tool_timing_metrics,
    tool_window_elapsed_ms as _tool_window_elapsed_ms,
)
from .shared.tool_context import build_tool_context


def _jiuwen_agent_callback_event_class() -> Any:
    return _default_jiuwen_agent_callback_event_class()


class _JiuwenModelTraceBridge(_BaseJiuwenModelTraceBridge):
    def __init__(self, **kwargs: Any) -> None:
        kwargs.setdefault("event_class_provider", _jiuwen_agent_callback_event_class)
        super().__init__(**kwargs)


def register_current_runtime_tools(*args: Any, **kwargs: Any) -> list[Any]:
    kwargs.setdefault("tool_batch_executor", execute_tool_batch)
    kwargs.setdefault("import_module", importlib.import_module)
    return _register_current_runtime_tools(*args, **kwargs)


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

    def run(
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
        with bind_log_context(
            _build_jiuwen_log_context(
                state,
                caller=caller,
                subagent_lineage=kwargs.get("subagent_lineage"),
            )
        ):
            return self._run(
                state,
                caller=caller,
                event_writer=event_writer,
                snapshot_callback=snapshot_callback,
                iteration_limit=iteration_limit,
                system_prompt=system_prompt,
                **kwargs,
            )

    def _run(
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
        trace_messages = _run_jiuwen_setup_step(
            runtime_trace,
            _build_jiuwen_token_limit_messages,
            state,
            inputs=inputs,
            system_prompt=system_prompt_for_agent,
            agent_config=self.agent_config,
        )
        _run_jiuwen_setup_step(
            runtime_trace,
            _enforce_jiuwen_input_token_limit,
            state,
            inputs=inputs,
            system_prompt=system_prompt_for_agent,
            tools_schema=tools_schema,
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
                sliding_window_applied=True,
                llm_message_count=len(trace_messages),
                llm_input_chars=lambda: _estimate_jiuwen_messages_chars(trace_messages),
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
        )

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

        def start_active_tool_batch(_context: Any = None) -> None:
            iteration = current_iteration()
            if progress.get("active_tool_batch_started"):
                return
            runtime_trace.tool_batch_started(iteration=iteration)
            progress["active_tool_batch_started"] = True
            progress["active_tool_batch_iteration"] = iteration

        def fail_active_tool_batch(_context: Any = None, status: str = RuntimeStatus.ERROR) -> None:
            iteration = int(progress.get("active_tool_batch_iteration") or current_iteration())
            if not progress.get("active_tool_batch_started"):
                runtime_trace.tool_batch_started(iteration=iteration)
            runtime_trace.tool_batch_failed(iteration=iteration, status=status)
            progress["active_tool_batch_started"] = False
            progress["active_tool_batch_iteration"] = None

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
                    tool_calls_count=len(tool_names),
                    tool_names=tool_names,
                    duration_ms=tools_window_elapsed_ms,
                )
                progress["active_tool_batch_started"] = False
                progress["active_tool_batch_iteration"] = None
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
                tool_timing_metrics=timing_metrics,
                event_source_node=event_source_node,
            )

        if self._agent_factory is None:
            _run_jiuwen_setup_step(
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
                terminal_tool_batch_callback=lambda _batch_result: None,
                state_snapshot_callback=snapshot_callback,
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
            prompt_build_ms=prompt_elapsed_ms,
            system_prompt_chars=len(str(system_prompt_for_agent or "")),
            sliding_window_applied=True,
        )
        try:
            _run_awaitable(model_trace_bridge.register(agent))
            try:
                try:
                    raw_result = self._invoke_agent(
                        agent,
                        inputs,
                        state=state,
                        caller=caller,
                        event_writer=event_writer,
                        iteration_provider=start_llm_iteration,
                        cancel_event=cancel_event,
                        token_callback=kwargs.get("token_callback"),
                        reasoning_summary_callback=kwargs.get("reasoning_summary_callback"),
                        stream_enabled=stream_enabled,
                        event_source_node=event_source_node,
                    )
                except _JiuwenTerminalToolBatch:
                    close_terminal_tool_iteration()
                    if snapshot_callback is not None:
                        snapshot_callback(deepcopy(state))
                    runtime_trace.agent_loop_finished(RuntimeStatus.SUCCESS)
                    return state
                except WorkflowCancelledError:
                    fail_unclosed_tool_batch(RuntimeStatus.CANCELLED)
                    runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
                    raise
                except Exception:
                    fail_unclosed_tool_batch(RuntimeStatus.ERROR)
                    runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
                    raise
            finally:
                _run_awaitable(model_trace_bridge.unregister())
            try:
                _raise_if_jiuwen_cancelled(cancel_event, state)
            except WorkflowCancelledError:
                fail_unclosed_tool_batch(RuntimeStatus.CANCELLED)
                runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
                raise
            if _is_error_result(raw_result):
                output = _extract_output(raw_result)
                fail_unclosed_tool_batch(RuntimeStatus.ERROR)
                runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
                raise RuntimeError(output or "Jiuwen ReAct runtime returned an error.")

            output = _extract_output(raw_result)

            assistant_message = {"role": "assistant", "content": output}
            reasoning_content = _extract_reasoning_content(raw_result)
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            _clear_jiuwen_pending_assistant_context(state)
            state.setdefault("messages", []).append(assistant_message)
            state["final_output"] = output
            state["results"] = {**dict(state.get("results") or {}), "output": output}

            if snapshot_callback is not None:
                snapshot_callback(deepcopy(state))

            completed_iteration = final_iteration()
            final_elapsed_ms = int(
                (
                    time.perf_counter()
                    - float(progress.get("active_started_at") or progress.get("last_completed_at") or started_at)
                ) * 1000
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
                tool_timing_metrics={},
                event_source_node=event_source_node,
            )
            runtime_trace.agent_loop_finished(RuntimeStatus.SUCCESS)
            return state
        except WorkflowCancelledError:
            runtime_trace.agent_loop_finished(RuntimeStatus.CANCELLED)
            raise
        except Exception:
            runtime_trace.agent_loop_finished(RuntimeStatus.ERROR)
            raise

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
    ) -> Any:
        if self._agent_factory is not None:
            return self._agent_factory()

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
            enable_reasoning=enable_reasoning,
            stream_enabled=stream_enabled,
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

    def _invoke_agent(
        self,
        agent: Any,
        inputs: Dict[str, Any],
        *,
        state: Optional[Mapping[str, Any]] = None,
        caller: str = "main",
        event_writer: Optional[Any] = None,
        iteration_provider: Optional[Any] = None,
        cancel_event: Optional[threading.Event] = None,
        token_callback: Optional[Any] = None,
        reasoning_summary_callback: Optional[Any] = None,
        stream_enabled: bool = True,
        event_source_node: Optional[str] = None,
    ) -> Any:
        _raise_if_jiuwen_cancelled(cancel_event, state)
        history_messages = _build_jiuwen_history_messages(state or {}, self.agent_config)
        session = None
        if history_messages:
            session = _run_awaitable(
                _seed_jiuwen_context_from_history(
                    agent,
                    inputs,
                    history_messages=history_messages,
                ),
                cancel_event=cancel_event,
                state=state,
            )
            _raise_if_jiuwen_cancelled(cancel_event, state)
        stream = getattr(agent, "stream", None) if stream_enabled else None
        if callable(stream):
            return _run_awaitable(
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
                ),
                cancel_event=cancel_event,
                state=state,
            )
        invoke = getattr(agent, "invoke", None)
        if invoke is None:
            raise JiuwenBackendUnavailable("Configured Jiuwen agent does not expose invoke().")
        return _run_awaitable(
            _invoke_jiuwen_agent(agent, invoke, inputs, session=session),
            cancel_event=cancel_event,
            state=state,
        )

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
    ) -> Dict[str, Any]:
        _raise_if_jiuwen_cancelled(cancel_event, state)
        stream_result = stream(inputs, session=session) if session is not None else stream(inputs)
        if isawaitable(stream_result):
            stream_result = await stream_result

        try:
            final_result: Dict[str, Any] = {}
            stream_state: Dict[str, str] = {"content": "", "reasoning": "", "reasoning_block_id": ""}
            async for chunk in stream_result:
                _raise_if_jiuwen_cancelled(cancel_event, state)
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
            if stream_state.get("reasoning") and not final_result.get("reasoning_content"):
                final_result["reasoning_content"] = stream_state["reasoning"]
            if stream_state.get("reasoning_block_id") and not final_result.get("reasoning_block_id"):
                final_result["reasoning_block_id"] = stream_state["reasoning_block_id"]
            if stream_state.get("reasoning") and stream_state.get("reasoning_block_id"):
                self._dispatch_reasoning_summary(
                    state or {},
                    caller=caller,
                    event_writer=event_writer,
                    iteration=(
                        int(iteration_provider())
                        if callable(iteration_provider)
                        else 1
                    ),
                    reasoning_text=stream_state["reasoning"],
                    reasoning_block_id=stream_state["reasoning_block_id"],
                    reasoning_summary_callback=reasoning_summary_callback,
                    event_source_node=event_source_node,
                )
            return final_result
        finally:
            if session is not None:
                await _finalize_jiuwen_session(agent, session)

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
        return {
            "query": append_current_date_reminder_to_text(query_with_context),
            "conversation_id": str(state.get("session_id") or "default_session"),
        }

    def _append_selected_ontology_context_to_query(self, state: Mapping[str, Any], query: str) -> str:
        selected_ontology_id = _selected_ontology_id_from_state(state)
        if not selected_ontology_id:
            return query
        selected_ontology_context = self._get_selected_ontology_context(state)
        lines = [
            query,
            "",
            "<Ontology-Selection-Constraint>",
            'The user explicitly selected ontology_id "{}".'.format(selected_ontology_id),
            'Always use ontology_id "{}" when calling ontology_context, business_data_search, or execute_script.'.format(
                selected_ontology_id
            ),
            "Do not infer ontology_id from business identifiers in the user request.",
            "Do not switch to another ontology. If the selected ontology is insufficient, say so.",
            "</Ontology-Selection-Constraint>",
        ]
        if selected_ontology_context:
            lines.extend(["", selected_ontology_context])
        return "\n".join(lines).strip()

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
        )

    def _tools_schema_for_state(self, state: Mapping[str, Any], *, caller: str) -> list[Mapping[str, Any]]:
        from ..tools.registry import get_tools_schema

        schema_caller, include_orchestration, include_web_search = self._tool_visibility_for_state(state, caller=caller)
        return get_tools_schema(
            schema_caller,
            include_orchestration=include_orchestration,
            include_web_search=include_web_search,
        )

    def _jiuwen_tool_infos_for_state(self, state: Mapping[str, Any], *, caller: str) -> list[Mapping[str, Any]]:
        schema_caller, include_orchestration, include_web_search = self._tool_visibility_for_state(state, caller=caller)
        return get_jiuwen_tool_infos(
            schema_caller,
            include_orchestration=include_orchestration,
            include_web_search=include_web_search,
        )

    def _tool_visibility_for_state(self, state: Mapping[str, Any], *, caller: str) -> tuple[str, bool, bool]:
        agent_mode = _resolve_agent_mode(state.get("user_context"))
        include_web_search = bool(getattr(getattr(self.agent_config, "web_search", None), "enabled", False))
        schema_caller = "main" if caller == "acp" else caller
        return schema_caller, agent_mode == "multi_agent", include_web_search

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


def create_default_jiuwen_agent(
    *,
    llm_config: Any,
    max_iterations: Optional[int] = None,
    system_prompt: Optional[str] = None,
    agent_id: Optional[str] = None,
    enable_reasoning: bool = False,
    stream_enabled: bool = True,
) -> Any:
    try:
        agent_card_module = importlib.import_module("openjiuwen.core.single_agent.schema.agent_card")
        react_agent_module = importlib.import_module("openjiuwen.core.single_agent.agents.react_agent")
        model_config_module = importlib.import_module("openjiuwen.core.foundation.llm.schema.config")
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

    model_client_config = model_client_config_cls(
        client_provider=_map_jiuwen_provider(getattr(llm_config, "provider", None)),
        api_key=str(getattr(llm_config, "api_key", "") or ""),
        api_base=str(getattr(llm_config, "base_url", "") or ""),
        timeout=float(getattr(llm_config, "timeout", 60) or 60),
        max_retries=3,
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
    model_request_kwargs.update(
        get_provider_adapter(getattr(llm_config, "provider", None)).top_level_params(
            stream=bool(stream_enabled)
        )
    )
    model_request_config = model_request_config_cls(**model_request_kwargs)
    react_config = react_agent_config_cls(
        model_client_config=model_client_config,
        model_config_obj=model_request_config,
        max_iterations=_normalize_max_iterations(max_iterations),
        prompt_template=(
            [{"role": "system", "content": system_prompt}]
            if str(system_prompt or "").strip()
            else []
        ),
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
        return configure(react_config)
    return agent


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


def _resolve_jiuwen_llm_config(
    state: Optional[Mapping[str, Any]],
    *,
    caller: str,
    agent_config: Any = None,
) -> Any:
    user_context = dict((state or {}).get("user_context") or {})
    profile_id = str(user_context.get("model_profile_id") or "").strip()
    purpose = LLMPurpose.MAIN if str(caller or "").strip() in {"main", "acp"} else LLMPurpose.SUBAGENT
    if profile_id:
        return _clamp_jiuwen_llm_config_output_tokens(
            get_llm_router().get_config(purpose, profile_id=profile_id),
            agent_config,
        )
    try:
        return _clamp_jiuwen_llm_config_output_tokens(
            get_llm_router().get_config(purpose),
            agent_config,
        )
    except RuntimeError:
        from ..config.settings import AppConfig

        return _clamp_jiuwen_llm_config_output_tokens(AppConfig.from_env().llm, agent_config)


def _clamp_jiuwen_llm_config_output_tokens(llm_config: Any, agent_config: Any) -> Any:
    limits = getattr(agent_config, "limits", None)
    try:
        output_limit = max(1, int(getattr(limits, "model_output_token_limit")))
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


def _build_jiuwen_history_messages(state: Mapping[str, Any], agent_config: Any) -> list[Any]:
    messages = [
        dict(message)
        for message in list(state.get("messages") or [])
        if isinstance(message, Mapping)
    ]
    if not messages:
        return []

    max_rounds = _normalize_max_iterations(getattr(agent_config, "context_max_rounds", None))
    prune_tools = getattr(agent_config, "context_prune_tools", None) or None
    windowed_messages = apply_sliding_window(messages, max_rounds, prune_tools)
    history_messages = _drop_current_user_message(windowed_messages, state)
    converted: list[Any] = []
    for message in history_messages:
        converted_message = _to_jiuwen_history_message(message)
        if converted_message is not None:
            converted.append(converted_message)
    return converted


def _enforce_jiuwen_input_token_limit(
    state: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any],
    system_prompt: Optional[str],
    tools_schema: list[Mapping[str, Any]],
    agent_config: Any,
) -> None:
    limits = getattr(agent_config, "limits", None)
    try:
        input_limit = max(1, int(getattr(limits, "model_input_token_limit")))
    except (TypeError, ValueError):
        return
    estimated_messages = _build_jiuwen_token_limit_messages(
        state,
        inputs=inputs,
        system_prompt=system_prompt,
        agent_config=agent_config,
    )
    estimated_input_tokens = estimate_messages_tokens(
        estimated_messages,
        tools=list(tools_schema or []),
    )
    if estimated_input_tokens > input_limit:
        raise InputTokenLimitError(estimated_input_tokens, input_limit)


def _build_jiuwen_token_limit_messages(
    state: Mapping[str, Any],
    *,
    inputs: Mapping[str, Any],
    system_prompt: Optional[str],
    agent_config: Any,
) -> list[Dict[str, Any]]:
    messages = [
        dict(message)
        for message in list(state.get("messages") or [])
        if isinstance(message, Mapping)
    ]
    max_rounds = _normalize_max_iterations(getattr(agent_config, "context_max_rounds", None))
    prune_tools = getattr(agent_config, "context_prune_tools", None) or None
    windowed_messages = apply_sliding_window(messages, max_rounds, prune_tools) if messages else []
    estimated_messages = [
        dict(message)
        for message in windowed_messages
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
    context_engine = getattr(agent, "context_engine", None)
    save_contexts = getattr(context_engine, "save_contexts", None)
    if callable(save_contexts):
        result = save_contexts(session)
        if isawaitable(result):
            await result
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


class _JiuwenLoopRunner:
    """Owns a single long-lived asyncio loop on a background thread.

    openJiuwen keeps process-wide singletons (Runner.callback_framework, its
    `asyncio.Lock`s, resource manager) that bind to whichever event loop first
    awaits them. Running every engine invocation on its own `asyncio.run` loop
    causes "bound to a different event loop" on the second call. This runner
    ensures every Jiuwen await uses the same loop for the lifetime of the
    process.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._ready = threading.Event()

    def get_loop(self) -> asyncio.AbstractEventLoop:
        loop = self._loop
        if loop is not None and loop.is_running():
            return loop
        with self._lock:
            if self._loop is not None and self._loop.is_running():
                return self._loop
            self._ready.clear()
            self._loop = asyncio.new_event_loop()
            self._thread = threading.Thread(
                target=self._run_forever,
                name="jiuwen-loop",
                daemon=True,
            )
            self._thread.start()
            if not self._ready.wait(timeout=5.0):
                raise JiuwenBackendUnavailable(
                    "Jiuwen background event loop failed to start."
                )
            return self._loop

    def _run_forever(self) -> None:
        loop = self._loop
        assert loop is not None
        asyncio.set_event_loop(loop)
        loop.call_soon(self._ready.set)
        try:
            loop.run_forever()
        finally:
            try:
                loop.close()
            except Exception:
                pass

    def submit(self, coro: Any) -> "concurrent.futures.Future[Any]":
        loop = self.get_loop()
        return asyncio.run_coroutine_threadsafe(coro, loop)


_JIUWEN_LOOP_RUNNER = _JiuwenLoopRunner()


def _run_awaitable(
    awaitable: Any,
    *,
    cancel_event: Optional[threading.Event] = None,
    state: Optional[Mapping[str, Any]] = None,
) -> Any:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        raise JiuwenBackendUnavailable(
            "Jiuwen ReAct runtime returned an async invoke result, but JiuwenReactLoopEngine.run is synchronous."
        )

    if cancel_event is not None and cancel_event.is_set():
        if asyncio.iscoroutine(awaitable):
            awaitable.close()
        _raise_if_jiuwen_cancelled(cancel_event, state)

    future = _JIUWEN_LOOP_RUNNER.submit(awaitable)
    if cancel_event is None:
        return future.result()

    while True:
        try:
            return future.result(timeout=0.05)
        except concurrent.futures.TimeoutError:
            if cancel_event.is_set():
                future.cancel()
                try:
                    future.result(timeout=1.0)
                except (concurrent.futures.CancelledError, concurrent.futures.TimeoutError):
                    pass
                except Exception:
                    pass
                _raise_if_jiuwen_cancelled(cancel_event, state)


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


def _last_user_message_content(state: Mapping[str, Any]) -> str:
    for message in reversed(list(state.get("messages") or [])):
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "user":
            continue
        return str(message.get("content") or "").strip()
    return ""
