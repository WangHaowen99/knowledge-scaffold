"""Single-loop agent runtime inspired by Claude Code."""

from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
from contextvars import copy_context
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import threading
import time
from typing import Any, Callable, Dict, List, Optional

from config import AgentConfig
from agent.exceptions import WorkflowCancelledError
from agent.state import AgenticState
from integration.observability import (
    AgentRuntimeTrace,
    NOOP_OBSERVER,
    RuntimeStatus,
    bind_observability_context,
)
from agent.prompts.universal import build_lang_instruction, build_universal_system_prompt
from llm import LLMPurpose, get_llm_router
from agent.tools.registry import ToolBatchResult, ToolCaller, ToolContext, execute_tool_batch, get_tool_spec, get_tools_schema, parse_tool_call
from utils.lang import detect_language
from agent.context import apply_sliding_window
from llm.llm_request_mode import is_llm_streaming_enabled
from utils.logging import bind_log_context, get_logger
from ontology.ontologies import (
    build_ontologies_section,
    build_selected_ontology_preload_context,
    build_selected_ontology_preload_records,
)
from utils.read_docs import merge_read_documents_registry
from utils.skills import build_skills_section
from utils.streaming import emit_agent_event, stream_writer_context, try_get_stream_writer
from workspace.workspaces import get_current_workspace_id, get_current_workspace_runtime_config, get_workspace_project_root

logger = get_logger("loop.engine")

_UTC8 = timezone(timedelta(hours=8))
_CURRENT_DATE_REMINDER_START = "<system-reminder>\nAs you answer the user's questions, you can use the following context:"
_CURRENT_DATE_REMINDER_END = "</system-reminder>"


def _resolve_current_workspace_id() -> Optional[str]:
    try:
        return get_current_workspace_id()
    except Exception:
        return None


@dataclass(frozen=True)
class _IterationRequest:
    llm_messages: List[Dict[str, Any]]
    tools: List[Dict[str, Any]]
    purpose: LLMPurpose
    profile_id: Optional[str]
    workspace_id: Optional[str]
    stream: Optional[bool] = None
    prompt_elapsed_ms: int = 0
    sliding_window_applied: bool = False
    system_prompt_chars: int = 0


class _StreamedToolCallExecutor:
    """Executes tool calls as they stream in without blocking the token reader.

    Scheduling rules:
      * ``concurrency_safe`` tools are submitted to a shared pool; a per-tool
        ``BoundedSemaphore`` enforces ``spec.max_parallelism``.
      * Non-concurrency-safe tools act as a barrier: once one is seen, the
        barrier and all later calls are deferred to the post-stream batch path.
        ``wait_all()`` still drains already-submitted safe tools before that
        tail batch runs. This avoids leaving the upstream LLM stream unread
        while long-running tools such as ``spawn_sub_agent`` execute nested
        work.

    ``wait_all()`` must be called after the LLM stream closes so that the
    engine observes the fully aggregated ``batch_result`` / ``executed_tool_calls``.
    """

    def __init__(
        self,
        engine: "AgentLoopEngine",
        state: AgenticState,
        *,
        caller: ToolCaller,
        iteration: int,
        tool_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        protocol_event_observer: Optional[Callable[[str, Dict[str, Any]], None]],
        subagent_manager: Any,
        subagent_lineage: Optional[Dict[str, Any]],
        runtime_trace: AgentRuntimeTrace,
    ) -> None:
        self.engine = engine
        self.base_state = state
        self.caller = caller
        self.iteration = iteration
        self.tool_event_emitter = tool_event_emitter
        self.protocol_event_observer = protocol_event_observer
        self.subagent_manager = subagent_manager
        self.subagent_lineage = subagent_lineage
        self.runtime_trace = runtime_trace
        self.batch_result = ToolBatchResult()
        self.executed_tool_calls: List[Dict[str, Any]] = []
        self.elapsed_ms = 0
        self._execution_state: Optional[AgenticState] = None
        self._assistant_message_index: Optional[int] = None
        self._lock = threading.Lock()
        self._pool: Optional[ThreadPoolExecutor] = None
        self._tool_semaphores: Dict[str, threading.BoundedSemaphore] = {}
        self._pending_futures: List[Future] = []
        # Capture streaming context in the constructing (engine) thread so
        # worker threads can re-enter it via ``copy_context().run(...)``.
        self._captured_writer = try_get_stream_writer()
        self._first_activity_at: Optional[float] = None
        self._last_activity_at: float = 0.0
        self._defer_remaining_tool_calls = False
        self._tool_batch_started = False

    @property
    def tool_batch_started(self) -> bool:
        return self._tool_batch_started

    def fail_started_tool_batch(self, *, status: str) -> None:
        if not self._tool_batch_started:
            return
        self.runtime_trace.tool_batch_failed(
            iteration=self.iteration,
            status=status,
        )

    def execute(self, closed_tool_calls: List[Dict[str, Any]]) -> None:
        if not closed_tool_calls or self.batch_result.terminal or self._defer_remaining_tool_calls:
            return

        self._emit_tool_batch_started()

        for tool_call in closed_tool_calls:
            name, _, _ = parse_tool_call(tool_call)
            spec = get_tool_spec(name)
            concurrency_safe = True if spec is None else bool(spec.concurrency_safe)

            if concurrency_safe:
                self._submit_safe(tool_call, name=name, spec=spec)
            else:
                self._defer_remaining_tool_calls = True
                return

    def _emit_tool_batch_started(self) -> None:
        if self._tool_batch_started:
            return
        self._tool_batch_started = True
        self.runtime_trace.tool_batch_started(iteration=self.iteration)

    def wait_all(self) -> None:
        """Drain in-flight tasks and shut the pool down; safe to call twice."""
        try:
            self._drain_pending()
        except WorkflowCancelledError:
            pool = self._pop_pool()
            if pool is not None:
                pool.shutdown(wait=False, cancel_futures=True)
            raise

        pool = self._pop_pool()
        if pool is not None:
            pool.shutdown(wait=True)

    def _submit_safe(
        self,
        tool_call: Dict[str, Any],
        *,
        name: str,
        spec: Any,
    ) -> None:
        max_parallelism = getattr(spec, "max_parallelism", None) if spec else None
        semaphore = self._get_semaphore(name, max_parallelism) if max_parallelism else None
        pool = self._get_pool()
        ctx = copy_context()

        def submission() -> None:
            if semaphore is None:
                self._run_one(tool_call)
                return
            with semaphore:
                self._run_one(tool_call)

        future = pool.submit(ctx.run, submission)
        with self._lock:
            self._pending_futures.append(future)

    def _drain_pending(self) -> None:
        with self._lock:
            futures = self._pending_futures
            self._pending_futures = []
        for future in futures:
            try:
                future.result()
            except WorkflowCancelledError:
                raise
            except Exception:
                logger.exception("Streamed tool call raised in worker thread")

    def _run_one(self, tool_call: Dict[str, Any]) -> None:
        started_at = time.perf_counter()
        with self._lock:
            if self._first_activity_at is None:
                self._first_activity_at = started_at
            if self.batch_result.terminal:
                return
            execution_state = self._prepare_execution_state([tool_call])
            context = self.engine._build_tool_context(
                execution_state,
                caller=self.caller,
                iteration=self.iteration,
                tool_event_emitter=self.tool_event_emitter,
                protocol_event_observer=self.protocol_event_observer,
                subagent_manager=self.subagent_manager,
                subagent_lineage=self.subagent_lineage,
                observer=self.runtime_trace.observer,
            )

        # Run outside the lock so safe tools can truly execute in parallel.
        if self._captured_writer is not None:
            with stream_writer_context(self._captured_writer):
                stream_batch_result = execute_tool_batch([tool_call], context)
        else:
            stream_batch_result = execute_tool_batch([tool_call], context)

        with self._lock:
            self.engine._apply_tool_batch_result(execution_state, stream_batch_result)
            self.batch_result = _merge_tool_batch_results(self.batch_result, stream_batch_result)
            self.executed_tool_calls.append(tool_call)
            self._last_activity_at = time.perf_counter()
            if self._first_activity_at is not None:
                self.elapsed_ms = int((self._last_activity_at - self._first_activity_at) * 1000)

    def _get_pool(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                max_workers = max(
                    1,
                    int(self.engine.agent_config.parallel_default_max_workers or 1),
                )
                self._pool = ThreadPoolExecutor(
                    max_workers=max_workers,
                    thread_name_prefix="stream-tool",
                )
            return self._pool

    def _pop_pool(self) -> Optional[ThreadPoolExecutor]:
        with self._lock:
            pool, self._pool = self._pool, None
            return pool

    def _get_semaphore(
        self,
        tool_name: str,
        max_parallelism: int,
    ) -> threading.BoundedSemaphore:
        with self._lock:
            sem = self._tool_semaphores.get(tool_name)
            if sem is None:
                sem = threading.BoundedSemaphore(max(1, int(max_parallelism)))
                self._tool_semaphores[tool_name] = sem
            return sem

    def _prepare_execution_state(self, closed_tool_calls: List[Dict[str, Any]]) -> AgenticState:
        if self._execution_state is None:
            self._execution_state = dict(self.base_state)
            self._execution_state["messages"] = [
                _copy_message_for_streamed_tool_context(message)
                for message in (self.base_state.get("messages") or [])
            ]
            self._assistant_message_index = None

        messages = self._execution_state.setdefault("messages", [])
        if self._assistant_message_index is None:
            messages.append({"role": "assistant", "content": "", "tool_calls": []})
            self._assistant_message_index = len(messages) - 1

        assistant_message = messages[self._assistant_message_index]
        existing_tool_calls = list(assistant_message.get("tool_calls") or [])
        assistant_message["tool_calls"] = [
            *existing_tool_calls,
            *[_copy_tool_call(tool_call) for tool_call in closed_tool_calls],
        ]
        return self._execution_state


class AgentLoopEngine:
    """Single ReAct-style loop that owns model calls and tool execution."""

    def __init__(
        self,
        *,
        memory_manager: Any = None,
        agent_config: Optional[AgentConfig] = None,
    ) -> None:
        self.memory_manager = memory_manager
        self.agent_config = agent_config or AgentConfig.from_env()
        self.project_root = str(get_workspace_project_root())

    def run(
        self,
        state: AgenticState,
        *,
        caller: ToolCaller,
        event_writer: Optional[Callable[[Dict[str, Any]], None]] = None,
        snapshot_callback: Optional[Callable[[AgenticState], None]] = None,
        iteration_limit: Optional[int] = None,
        system_prompt: Optional[str] = None,
        token_callback: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]] = None,
        reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
        tool_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        protocol_event_observer: Optional[Callable[[str, Dict[str, Any]], None]] = None,
        subagent_manager: Any = None,
        subagent_lineage: Optional[Dict[str, Any]] = None,
        observer: Any = None,
    ) -> AgenticState:
        mutable_state: AgenticState = state
        runtime_observer = observer or NOOP_OBSERVER
        resolved_workspace_id = str(mutable_state.get("workspace_id") or "").strip() or _resolve_current_workspace_id()
        if resolved_workspace_id:
            mutable_state["workspace_id"] = resolved_workspace_id
        self.project_root = str(get_workspace_project_root(resolved_workspace_id))
        resolved_limit = self._resolve_iteration_limit(caller, iteration_limit)

        with bind_log_context(
            self._build_log_context(
                mutable_state,
                caller=caller,
                subagent_lineage=subagent_lineage,
            )
        ):
            run_id = str(mutable_state.get("run_id") or "")
            session_id = str(mutable_state.get("session_id") or "")
            turn_id = int(mutable_state.get("current_turn_id") or 0)
            user_context = dict(mutable_state.get("user_context") or {})
            agent_mode = self._resolve_agent_mode(user_context)
            reasoning_enabled = agent_mode in ("thinking", "multi_agent")
            include_orchestration = agent_mode == "multi_agent"
            runtime_trace = AgentRuntimeTrace(
                observer=runtime_observer,
                run_id=run_id,
                session_id=session_id,
                turn_id=turn_id,
                caller=caller,
                lineage=subagent_lineage,
            )
            with runtime_trace.agent_loop_scope(
                agent_mode=agent_mode,
            ):
                should_seed_selected_ontology = (
                    caller == "main"
                    and not mutable_state.get("messages")
                    and bool(str(user_context.get("selected_ontology_id") or "").strip())
                )
                selected_ontology_context = (
                    self._get_selected_ontology_context(mutable_state)
                    if should_seed_selected_ontology
                    else None
                )
                if should_seed_selected_ontology:
                    self._hydrate_selected_ontology_read_documents(mutable_state)

                for _step in range(resolved_limit):
                    iteration = _step + 1
                    iteration_started_at = time.perf_counter()

                    iteration_request = self._prepare_iteration_request(
                        mutable_state,
                        caller=caller,
                        step=_step,
                        system_prompt=system_prompt,
                        include_orchestration=include_orchestration,
                        selected_ontology_context=selected_ontology_context,
                    )

                    runtime_trace.iteration_prepared(
                        iteration=iteration,
                        purpose=str(iteration_request.purpose.value),
                        agent_mode=agent_mode,
                        prompt_build_ms=iteration_request.prompt_elapsed_ms,
                        system_prompt_chars=iteration_request.system_prompt_chars,
                        sliding_window_applied=iteration_request.sliding_window_applied,
                        llm_message_count=len(iteration_request.llm_messages),
                        llm_input_chars=lambda: _estimate_messages_chars(iteration_request.llm_messages),
                        tools_count=len(iteration_request.tools),
                    )

                    streamed_tool_executor = _StreamedToolCallExecutor(
                        self,
                        mutable_state,
                        caller=caller,
                        iteration=iteration,
                        tool_event_emitter=tool_event_emitter,
                        protocol_event_observer=protocol_event_observer,
                        subagent_manager=subagent_manager,
                        subagent_lineage=subagent_lineage,
                        runtime_trace=runtime_trace,
                    )

                    llm_started_at = time.perf_counter()
                    llm_stream_finished_at = llm_started_at
                    model_config = get_llm_router().get_config(
                        iteration_request.purpose,
                        profile_id=iteration_request.profile_id,
                        workspace_id=iteration_request.workspace_id,
                    )
                    model_name = getattr(model_config, "model", None) or "unknown"
                    llm_error_status: Optional[str] = None
                    request_label = "main_agent_iteration" if caller == "main" else "subagent_iteration"
                    api_params = _build_llm_api_params(
                        iteration_request=iteration_request,
                        enable_reasoning=reasoning_enabled,
                        request_label=request_label,
                    )

                    try:
                        with runtime_trace.llm_call_scope(
                            iteration=iteration,
                            messages=iteration_request.llm_messages,
                            tools=iteration_request.tools,
                            model=model_name,
                            api_params=api_params,
                        ) as llm_trace:
                            with bind_observability_context(
                                observer=runtime_observer,
                                run_id=run_id,
                                session_id=session_id,
                                turn_id=turn_id,
                                parent_key=runtime_trace.loop_span_key,
                            ):
                                response = get_llm_router().call(
                                    purpose=iteration_request.purpose,
                                    profile_id=iteration_request.profile_id,
                                    workspace_id=iteration_request.workspace_id,
                                    messages=iteration_request.llm_messages,
                                    tools=iteration_request.tools or None,
                                    stream=iteration_request.stream,
                                    enable_reasoning=reasoning_enabled,
                                    run_id=mutable_state.get("run_id"),
                                    emit_func=self._build_token_emitter(
                                        mutable_state,
                                        caller=caller,
                                        iteration=iteration,
                                        writer=event_writer,
                                        token_callback=token_callback,
                                    ),
                                    reasoning_summary_callback=self._build_reasoning_summary_emitter(
                                        mutable_state,
                                        caller=caller,
                                        iteration=iteration,
                                        writer=event_writer,
                                        reasoning_summary_callback=reasoning_summary_callback,
                                    ),
                                    request_label=request_label,
                                    tool_calls_closed_callback=streamed_tool_executor.execute,
                                )
                            llm_stream_finished_at = time.perf_counter()
                            llm_response_wall_ms = int((llm_stream_finished_at - llm_started_at) * 1000)
                            llm_trace.finish(
                                usage=getattr(response, "usage", None) or {},
                                prompt_text=lambda: "\n".join(str(m.get("content", "")) for m in iteration_request.llm_messages),
                                completion_text=lambda: response.content or "",
                                reasoning_text=lambda: getattr(response, "reasoning_content", None) or "",
                                tool_calls=getattr(response, "tool_calls", None) or [],
                                finish_reason=getattr(response, "finish_reason", None),
                                response_model=getattr(response, "model", None),
                                response_headers_ms=getattr(response, "response_headers_ms", None),
                                duration_ms=getattr(response, "duration_ms", None),
                                stream_duration_ms=llm_response_wall_ms,
                                wall_duration_ms=llm_response_wall_ms,
                                first_reasoning_ms=getattr(response, "first_reasoning_ms", None),
                                first_content_ms=getattr(response, "first_content_ms", None),
                                first_tool_call_ms=getattr(response, "first_tool_call_ms", None),
                                request_label=request_label,
                                model=model_name,
                                model_provider=getattr(model_config, "provider", None),
                                tool_names=_extract_tool_call_names(getattr(response, "tool_calls", None)),
                            )
                    except WorkflowCancelledError:
                        llm_error_status = RuntimeStatus.CANCELLED
                        raise
                    except Exception:
                        llm_error_status = RuntimeStatus.ERROR
                        raise
                    finally:
                        if llm_stream_finished_at == llm_started_at:
                            llm_stream_finished_at = time.perf_counter()
                        with runtime_trace.tool_batch_wait_scope(
                            iteration=iteration,
                        ):
                            streamed_tool_executor.wait_all()
                        if llm_error_status and streamed_tool_executor.tool_batch_started:
                            streamed_tool_executor.fail_started_tool_batch(status=llm_error_status)
                    llm_finished_at = time.perf_counter()
                    llm_wall_elapsed_ms = int((llm_finished_at - llm_started_at) * 1000)
                    llm_stream_elapsed_ms = int((llm_stream_finished_at - llm_started_at) * 1000)
                    tool_names = _extract_tool_call_names(response.tool_calls)
                    tool_calls_count = len(response.tool_calls or [])

                    assistant_msg = _build_assistant_message_from_response(response)

                    mutable_state.setdefault("messages", [])
                    mutable_state["messages"].append(assistant_msg)
                    if snapshot_callback:
                        snapshot_callback(deepcopy(mutable_state))

                    if not response.tool_calls:
                        iteration_total_elapsed_ms = int((time.perf_counter() - iteration_started_at) * 1000)
                        tool_timing_metrics = _build_tool_timing_metrics(
                            [],
                            llm_started_at_ms=int(llm_started_at * 1000),
                            llm_finished_at_ms=int(llm_stream_finished_at * 1000),
                            tools_window_elapsed_ms=0,
                        )
                        mutable_state["final_output"] = response.content
                        mutable_state["results"] = {
                            **dict(mutable_state.get("results") or {}),
                            "output": response.content,
                        }
                        if snapshot_callback:
                            snapshot_callback(deepcopy(mutable_state))
                        self._emit_iteration_completed_event(
                            caller=caller,
                            run_id=run_id,
                            session_id=session_id,
                            turn_id=turn_id,
                            iteration=iteration,
                            prompt_elapsed_ms=iteration_request.prompt_elapsed_ms,
                            status="final_output",
                            llm_elapsed_ms=llm_stream_elapsed_ms,
                            llm_stream_elapsed_ms=llm_stream_elapsed_ms,
                            llm_wall_elapsed_ms=llm_wall_elapsed_ms,
                            tools_elapsed_ms=0,
                            total_elapsed_ms=iteration_total_elapsed_ms,
                            tool_calls_count=tool_calls_count,
                            tool_names=tool_names,
                            event_writer=event_writer,
                            tool_timing_metrics=tool_timing_metrics,
                            runtime_trace=runtime_trace,
                        )
                        return mutable_state

                    if streamed_tool_executor.batch_result.messages or streamed_tool_executor.batch_result.state_updates:
                        self._apply_tool_batch_result(mutable_state, streamed_tool_executor.batch_result)

                    remaining_tool_calls = _filter_pending_tool_calls(
                        response.tool_calls,
                        streamed_tool_executor.executed_tool_calls,
                    )
                    batch_result = streamed_tool_executor.batch_result

                    tool_batch_started_at = time.perf_counter()
                    tool_batch_scope = (
                        runtime_trace.continued_tool_batch_scope
                        if streamed_tool_executor.tool_batch_started
                        else runtime_trace.tool_batch_scope
                    )
                    with tool_batch_scope(iteration=iteration) as tool_trace:
                        if remaining_tool_calls and not batch_result.terminal:
                            tail_batch_result = execute_tool_batch(
                                remaining_tool_calls,
                                self._build_tool_context(
                                    mutable_state,
                                    caller=caller,
                                    iteration=iteration,
                                    tool_event_emitter=tool_event_emitter,
                                    protocol_event_observer=protocol_event_observer,
                                    subagent_manager=subagent_manager,
                                    subagent_lineage=subagent_lineage,
                                    observer=runtime_observer,
                                ),
                            )
                            self._apply_tool_batch_result(mutable_state, tail_batch_result)
                            batch_result = _merge_tool_batch_results(batch_result, tail_batch_result)
                        batch_elapsed_ms = streamed_tool_executor.elapsed_ms + int(
                            (time.perf_counter() - tool_batch_started_at) * 1000
                        )
                        tool_trace.finish(
                            tool_calls_count=tool_calls_count,
                            tool_names=tool_names,
                            duration_ms=batch_elapsed_ms,
                        )

                    tool_timing_metrics = _build_tool_timing_metrics(
                        batch_result.tool_intervals_ms,
                        llm_started_at_ms=int(llm_started_at * 1000),
                        llm_finished_at_ms=int(llm_stream_finished_at * 1000),
                        tools_window_elapsed_ms=batch_elapsed_ms,
                    )

                    if snapshot_callback:
                        snapshot_callback(deepcopy(mutable_state))

                    if batch_result.terminal:
                        iteration_total_elapsed_ms = int((time.perf_counter() - iteration_started_at) * 1000)
                        self._emit_iteration_completed_event(
                            caller=caller,
                            run_id=run_id,
                            session_id=session_id,
                            turn_id=turn_id,
                            iteration=iteration,
                            prompt_elapsed_ms=iteration_request.prompt_elapsed_ms,
                            status="terminal_tool_batch",
                            llm_elapsed_ms=llm_stream_elapsed_ms,
                            llm_stream_elapsed_ms=llm_stream_elapsed_ms,
                            llm_wall_elapsed_ms=llm_wall_elapsed_ms,
                            tools_elapsed_ms=batch_elapsed_ms,
                            total_elapsed_ms=iteration_total_elapsed_ms,
                            tool_calls_count=tool_calls_count,
                            tool_names=tool_names,
                            tool_timing_metrics=tool_timing_metrics,
                            event_writer=event_writer,
                            runtime_trace=runtime_trace,
                        )
                        return mutable_state

                    iteration_total_elapsed_ms = int((time.perf_counter() - iteration_started_at) * 1000)
                    self._emit_iteration_completed_event(
                        caller=caller,
                        run_id=run_id,
                        session_id=session_id,
                        turn_id=turn_id,
                        iteration=iteration,
                        prompt_elapsed_ms=iteration_request.prompt_elapsed_ms,
                        status="continue",
                        llm_elapsed_ms=llm_stream_elapsed_ms,
                        llm_stream_elapsed_ms=llm_stream_elapsed_ms,
                        llm_wall_elapsed_ms=llm_wall_elapsed_ms,
                        tools_elapsed_ms=batch_elapsed_ms,
                        total_elapsed_ms=iteration_total_elapsed_ms,
                        tool_calls_count=tool_calls_count,
                        tool_names=tool_names,
                        tool_timing_metrics=tool_timing_metrics,
                        event_writer=event_writer,
                        runtime_trace=runtime_trace,
                    )
                raise RuntimeError("Agent loop iteration limit exceeded ({})".format(resolved_limit))

    def _prepare_iteration_request(
        self,
        state: AgenticState,
        *,
        caller: ToolCaller,
        step: int,
        system_prompt: Optional[str],
        include_orchestration: bool,
        selected_ontology_context: Optional[str],
    ) -> _IterationRequest:
        messages = list(state.get("messages") or [])
        profile_id = (
            str((state.get("user_context") or {}).get("model_profile_id") or "").strip()
            or None
        )
        workspace_id = str(state.get("workspace_id") or "").strip() or _resolve_current_workspace_id()
        if caller in ("main", "acp"):
            prompt_started_at = time.perf_counter()
            state_system_prompt = ""
            if messages:
                for message in messages:
                    if message.get("role") != "system":
                        continue
                    content = message.get("content")
                    state_system_prompt = content if isinstance(content, str) else str(content or "")
                    break
            else:
                state_system_prompt = system_prompt or self._build_main_system_prompt(
                    state, include_orchestration=include_orchestration,
                )
                user_message_content = state.get("user_request", "")
                if selected_ontology_context:
                    user_message_content = _append_selected_ontology_context_to_user_content(
                        user_message_content,
                        selected_ontology_context,
                    )
                seeded_messages = [
                    {"role": "system", "content": state_system_prompt},
                    {"role": "user", "content": user_message_content},
                ]
                state["messages"] = list(seeded_messages)
                messages = list(state["messages"])
            prompt_elapsed_ms = int((time.perf_counter() - prompt_started_at) * 1000)

            refreshed = list(messages)
            sliding_window_applied = step == 0
            if sliding_window_applied:
                llm_messages = apply_sliding_window(
                    refreshed,
                    self.agent_config.context_max_rounds,
                    self.agent_config.context_prune_tools or None,
                )
            else:
                llm_messages = refreshed
            llm_messages = _append_current_date_reminder_to_latest_user_message(llm_messages)
            stream = is_llm_streaming_enabled(
                (state.get("user_context") or {}).get("llm_request_mode")
            )
            tools = get_tools_schema(
                "main",
                include_orchestration=include_orchestration,
                include_web_search=bool(getattr(self.agent_config.web_search, "enabled", False)),
            )
            return _IterationRequest(
                llm_messages=llm_messages,
                tools=tools,
                purpose=LLMPurpose.MAIN,
                profile_id=profile_id,
                workspace_id=workspace_id,
                stream=stream,
                prompt_elapsed_ms=prompt_elapsed_ms,
                sliding_window_applied=sliding_window_applied,
                system_prompt_chars=len(state_system_prompt),
            )

        if not messages and system_prompt is not None:
            seeded_messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": state.get("user_request", "")},
            ]
            state["messages"] = list(seeded_messages)
            messages = list(state["messages"])

        return _IterationRequest(
            llm_messages=_append_current_date_reminder_to_latest_user_message(messages),
            tools=get_tools_schema(
                caller,
                include_orchestration=include_orchestration,
                include_web_search=bool(getattr(self.agent_config.web_search, "enabled", False)),
            ),
            purpose=LLMPurpose.SUBAGENT,
            profile_id=profile_id,
            workspace_id=workspace_id,
            stream=is_llm_streaming_enabled((state.get("user_context") or {}).get("llm_request_mode")),
        )

    def _emit_iteration_completed_event(
        self,
        *,
        caller: ToolCaller,
        run_id: str,
        session_id: str,
        turn_id: int,
        iteration: int,
        prompt_elapsed_ms: int,
        status: str,
        llm_elapsed_ms: int,
        llm_stream_elapsed_ms: int,
        llm_wall_elapsed_ms: int,
        tools_elapsed_ms: int,
        total_elapsed_ms: int,
        tool_calls_count: int,
        tool_names: List[str],
        event_writer: Optional[Callable[[Dict[str, Any]], None]],
        tool_timing_metrics: Optional[Dict[str, int]] = None,
        runtime_trace: Optional[AgentRuntimeTrace] = None,
    ) -> None:
        timing_metrics = dict(tool_timing_metrics or {})
        if runtime_trace is not None:
            runtime_trace.iteration_finished(
                iteration=iteration,
                status=status,
                prompt_build_ms=prompt_elapsed_ms,
                llm_stream_ms=llm_stream_elapsed_ms,
                llm_wall_ms=llm_wall_elapsed_ms,
                tools_window_ms=tools_elapsed_ms,
                tools_overlap_llm_ms=int(timing_metrics.get("tools_overlap_llm_ms") or 0),
                tools_after_llm_ms=int(timing_metrics.get("tools_after_llm_ms") or 0),
                tool_calls_count=tool_calls_count,
                tool_names=tool_names,
                total_ms=total_elapsed_ms,
            )
        if caller != "main" or not run_id:
            return
        emit_agent_event(
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            event_type="iteration.completed",
            source={"node": "agent_loop", "component": "iteration"},
            payload={
                "iteration": iteration,
                "status": status,
                "llm_elapsed_ms": llm_elapsed_ms,
                "llm_stream_elapsed_ms": llm_stream_elapsed_ms,
                "llm_wall_elapsed_ms": llm_wall_elapsed_ms,
                "tools_elapsed_ms": tools_elapsed_ms,
                "prompt_build_ms": prompt_elapsed_ms,
                "total_elapsed_ms": total_elapsed_ms,
                "tool_calls_count": tool_calls_count,
                "tool_names": list(tool_names or []),
                **timing_metrics,
            },
            writer=event_writer,
        )

    def _build_log_context(
        self,
        state: AgenticState,
        *,
        caller: ToolCaller,
        subagent_lineage: Optional[Dict[str, Any]],
    ) -> Dict[str, Any]:
        lineage = dict(subagent_lineage or {})
        context: Dict[str, Any] = {
            "agent_type": caller,
            "agent_id": ("main" if caller == "main" else lineage.get("subagent_id")),
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

    def _build_main_system_prompt(self, state: AgenticState, *, include_orchestration: bool = True) -> str:
        user_context = dict(state.get("user_context") or {})
        selected_ontology_id = str(user_context.get("selected_ontology_id") or "").strip() or None

        lang_instruction = None
        try:
            runtime_config = get_current_workspace_runtime_config()
            lang_config = runtime_config.get("lang", {})
            if lang_config.get("enabled"):
                if "_lang_detected" not in state:
                    user_query = state.get("user_request", "")
                    explicit_lang = state.get("user_lang")
                    state["_lang_detected"] = detect_language(
                        user_query,
                        lang=explicit_lang,
                        default_lang=lang_config.get("default"),
                    )
                lang_instruction = build_lang_instruction(state["_lang_detected"])
        except Exception:
            pass

        return build_universal_system_prompt(
            ontologies_section=build_ontologies_section(
                selected_ontology_id=selected_ontology_id,
            ),
            skills_section=build_skills_section(),
            memory_context=state.get("memory_context"),
            include_orchestration=include_orchestration,
            lang_instruction=lang_instruction,
        )

    def _get_selected_ontology_context(self, state: AgenticState) -> Optional[str]:
        user_context = dict(state.get("user_context") or {})
        selected_ontology_id = str(user_context.get("selected_ontology_id") or "").strip() or None
        cache_key = selected_ontology_id
        if state.get("_selected_ontology_context_cache_key") == cache_key:
            return state.get("_selected_ontology_context_cache")

        selected_ontology_context = build_selected_ontology_preload_context(
            selected_ontology_id=selected_ontology_id,
        )
        state["_selected_ontology_context_cache_key"] = cache_key
        state["_selected_ontology_context_cache"] = selected_ontology_context
        return selected_ontology_context

    def _hydrate_selected_ontology_read_documents(self, state: AgenticState) -> None:
        user_context = dict(state.get("user_context") or {})
        records = build_selected_ontology_preload_records(
            selected_ontology_id=str(user_context.get("selected_ontology_id") or "").strip() or None,
            turn_id=int(state.get("current_turn_id") or 0),
        )
        if not records:
            return
        state["read_documents"] = merge_read_documents_registry(
            state.get("read_documents"),
            records,
        )

    def _apply_tool_batch_result(
        self,
        state: AgenticState,
        batch_result: ToolBatchResult,
    ) -> None:
        if batch_result.messages:
            state.setdefault("messages", [])
            state["messages"].extend(batch_result.messages)
        if batch_result.state_updates:
            self._apply_state_updates(state, batch_result.state_updates)

    def _build_tool_context(
        self,
        state: AgenticState,
        *,
        caller: ToolCaller,
        iteration: int,
        tool_event_emitter: Optional[Callable[[str, Dict[str, Any]], None]],
        protocol_event_observer: Optional[Callable[[str, Dict[str, Any]], None]],
        subagent_manager: Any,
        subagent_lineage: Optional[Dict[str, Any]],
        observer: Any = None,
    ) -> ToolContext:
        return ToolContext(
            memory_manager=self.memory_manager,
            messages=list(state.get("messages") or []),
            run_id=state.get("run_id"),
            session_id=state.get("session_id"),
            turn_id=int(state.get("current_turn_id") or 0),
            agent_config=self.agent_config,
            caller=caller,
            agent_mode=self._resolve_agent_mode(state.get("user_context")),
            agent_iteration=iteration,
            workspace_id=state.get("workspace_id") or _resolve_current_workspace_id(),
            project_root=self.project_root,
            event_emitter=tool_event_emitter,
            parent_tool_call_id=None,
            subagent_manager=subagent_manager,
            subagent_lineage=subagent_lineage,
            protocol_event_observer=protocol_event_observer,
            read_documents=state.get("read_documents"),
            selected_ontology_id=(
                str((state.get("user_context") or {}).get("selected_ontology_id") or "").strip()
                or None
            ),
            model_profile_id=(
                str((state.get("user_context") or {}).get("model_profile_id") or "").strip()
                or None
            ),
            observer=observer or NOOP_OBSERVER,
        )

    def _resolve_iteration_limit(
        self,
        caller: ToolCaller,
        iteration_limit: Optional[int],
    ) -> int:
        if iteration_limit is not None:
            try:
                return max(1, int(iteration_limit))
            except (TypeError, ValueError):
                pass
        if caller == "main":
            return max(1, int(self.agent_config.main_agent_max_iterations))
        return max(1, int(self.agent_config.sub_agent_max_iterations))

    def _apply_state_updates(
        self,
        state: AgenticState,
        updates: Dict[str, Any],
    ) -> None:
        for key, value in (updates or {}).items():
            if key == "results" and isinstance(value, dict):
                state["results"] = {**dict(state.get("results") or {}), **value}
            elif key in {"completed_ids", "failed_ids", "errors"} and isinstance(value, list):
                existing = list(state.get(key) or [])
                existing.extend(value)
                state[key] = existing
            elif key == "messages" and isinstance(value, list):
                existing_messages = list(state.get("messages") or [])
                existing_messages.extend(value)
                state["messages"] = existing_messages
            elif key == "read_document_records" and isinstance(value, list):
                state["read_documents"] = merge_read_documents_registry(
                    state.get("read_documents"),
                    value,
                )
            elif key == "evolution_recall_records" and isinstance(value, list):
                existing = list(state.get("evolution_recall_records") or [])
                existing.extend(value)
                state["evolution_recall_records"] = existing
            else:
                state[key] = value

    def _build_token_emitter(
        self,
        state: AgenticState,
        *,
        caller: ToolCaller,
        iteration: int,
        writer: Optional[Callable[[Dict[str, Any]], None]],
        token_callback: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]],
    ) -> Callable[[str, str, Optional[Dict[str, Any]]], None]:
        run_id = state.get("run_id")
        session_id = state.get("session_id")
        turn_id = int(state.get("current_turn_id") or 0)

        def emit(token_type: str, token: str, meta: Optional[Dict[str, Any]] = None) -> None:
            if token_callback:
                token_callback(token_type, token, meta)
                return
            if not run_id or not token:
                return
            payload = {
                "channel": "reasoning" if token_type == "reasoning" else "content",
                "text": token,
                "iteration": iteration,
            }
            reasoning_block_id = str((meta or {}).get("reasoning_block_id") or "").strip()
            if token_type == "reasoning" and reasoning_block_id:
                payload["reasoning_block_id"] = reasoning_block_id
            emit_agent_event(
                run_id=run_id,
                session_id=session_id,
                turn_id=turn_id,
                event_type="token.delta",
                source={"node": ("agent_loop" if caller == "main" else caller), "component": "llm"},
                payload=payload,
                writer=writer,
            )

        return emit

    def _build_reasoning_summary_emitter(
        self,
        state: AgenticState,
        *,
        caller: ToolCaller,
        iteration: int,
        writer: Optional[Callable[[Dict[str, Any]], None]],
        reasoning_summary_callback: Optional[Callable[[str, str], None]],
    ) -> Callable[[str, str], None]:
        run_id = state.get("run_id")
        session_id = state.get("session_id")
        turn_id = int(state.get("current_turn_id") or 0)

        def emit(reasoning_block_id: str, summary: str) -> None:
            if reasoning_summary_callback:
                reasoning_summary_callback(reasoning_block_id, summary)
                return
            if not run_id or not reasoning_block_id or not summary:
                return
            emit_agent_event(
                run_id=run_id,
                session_id=session_id,
                turn_id=turn_id,
                event_type="reasoning.summary",
                source={"node": ("agent_loop" if caller == "main" else caller), "component": "llm"},
                payload={
                    "channel": "reasoning",
                    "summary": summary,
                    "reasoning_block_id": reasoning_block_id,
                    "iteration": iteration,
                },
                writer=writer,
            )

        return emit

    _VALID_AGENT_MODES = {"quick", "thinking", "multi_agent"}

    @staticmethod
    def _resolve_agent_mode(user_context: Optional[Dict[str, Any]]) -> str:
        ctx = user_context or {}
        raw_mode = str(ctx.get("agent_mode") or "").strip().lower()
        if raw_mode in AgentLoopEngine._VALID_AGENT_MODES:
            return raw_mode
        return "quick"


def _estimate_messages_chars(messages: List[Dict[str, Any]]) -> int:
    total = 0
    for message in messages:
        content = message.get("content")
        if isinstance(content, str):
            total += len(content)
        elif isinstance(content, list):
            total += sum(len(str(item)) for item in content)
        elif content is not None:
            total += len(str(content))
    return total


def _build_assistant_message_from_response(response: Any) -> Dict[str, Any]:
    assistant_msg: Dict[str, Any] = {
        "role": "assistant",
        "content": response.content,
    }
    reasoning_content = getattr(response, "reasoning_content", None)
    if reasoning_content:
        assistant_msg["reasoning_content"] = reasoning_content
    if response.tool_calls:
        assistant_msg["tool_calls"] = response.tool_calls
    return assistant_msg


def _copy_message_for_streamed_tool_context(message: Dict[str, Any]) -> Dict[str, Any]:
    copied = dict(message)
    if isinstance(copied.get("tool_calls"), list):
        copied["tool_calls"] = [_copy_tool_call(tool_call) for tool_call in copied["tool_calls"]]
    return copied


def _copy_tool_call(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    copied = dict(tool_call)
    if isinstance(copied.get("function"), dict):
        copied["function"] = dict(copied["function"])
    return copied


def _extract_tool_call_names(tool_calls: Optional[List[Dict[str, Any]]]) -> List[str]:
    if not tool_calls:
        return []
    names: List[str] = []
    for tool_call in tool_calls:
        function_payload = tool_call.get("function") or {}
        name = str(function_payload.get("name") or "").strip()
        if name:
            names.append(name)
    return names


def _build_llm_api_params(
    *,
    iteration_request: _IterationRequest,
    enable_reasoning: bool,
    request_label: str,
) -> Dict[str, Any]:
    llm_config = get_llm_router().get_config(
        iteration_request.purpose,
        profile_id=iteration_request.profile_id,
        workspace_id=iteration_request.workspace_id,
    )
    return {
        "model": getattr(llm_config, "model", None) or "unknown",
        "base_url": str(getattr(llm_config, "base_url", "") or "").rstrip("/"),
        "stream": True if iteration_request.stream is None else bool(iteration_request.stream),
        "temperature": getattr(llm_config, "temperature", None),
        "max_tokens": getattr(llm_config, "max_tokens", None),
        "timeout": getattr(llm_config, "timeout", None),
        "enable_reasoning": bool(enable_reasoning),
        "request_label": request_label,
        "tools_count": len(iteration_request.tools or []),
        "sliding_window_applied": bool(iteration_request.sliding_window_applied),
        "system_prompt_chars": int(iteration_request.system_prompt_chars or 0),
        "prompt_build_ms": int(iteration_request.prompt_elapsed_ms or 0),
    }


def _merge_tool_state_updates(
    existing: Optional[Dict[str, Any]],
    incoming: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    merged: Dict[str, Any] = dict(existing or {})
    for key, value in (incoming or {}).items():
        if key == "results" and isinstance(value, dict):
            merged[key] = {**dict(merged.get(key) or {}), **value}
        elif key in {"completed_ids", "failed_ids", "errors"} and isinstance(value, list):
            items = list(merged.get(key) or [])
            items.extend(value)
            merged[key] = items
        elif key == "messages" and isinstance(value, list):
            messages = list(merged.get(key) or [])
            messages.extend(value)
            merged[key] = messages
        elif key == "read_document_records" and isinstance(value, list):
            records = list(merged.get(key) or [])
            records.extend(value)
            merged[key] = records
        elif key == "evolution_recall_records" and isinstance(value, list):
            records = list(merged.get(key) or [])
            records.extend(value)
            merged[key] = records
        else:
            merged[key] = value
    return merged


def _merge_tool_batch_results(
    existing: ToolBatchResult,
    incoming: ToolBatchResult,
) -> ToolBatchResult:
    return ToolBatchResult(
        messages=[*(existing.messages or []), *(incoming.messages or [])],
        state_updates=_merge_tool_state_updates(existing.state_updates, incoming.state_updates),
        terminal=bool(existing.terminal or incoming.terminal),
        tool_intervals_ms=[
            *(existing.tool_intervals_ms or []),
            *(incoming.tool_intervals_ms or []),
        ],
    )


def _build_tool_timing_metrics(
    tool_intervals_ms: List[Dict[str, int]],
    *,
    llm_started_at_ms: int,
    llm_finished_at_ms: int,
    tools_window_elapsed_ms: int,
) -> Dict[str, int]:
    intervals = _normalize_intervals(tool_intervals_ms)
    merged_tool_intervals = _merge_intervals(intervals)
    tools_wall_elapsed_ms = _sum_interval_lengths(merged_tool_intervals)
    tools_execution_elapsed_ms = sum(
        max(0, int(interval.get("elapsed_ms", interval["finished_at_ms"] - interval["started_at_ms"])))
        for interval in intervals
    )
    tools_overlap_llm_ms = _intervals_overlap_ms(
        merged_tool_intervals,
        max(0, int(llm_started_at_ms)),
        max(0, int(llm_finished_at_ms)),
    )
    tools_net_wait_ms = max(0, tools_wall_elapsed_ms - tools_overlap_llm_ms)

    return {
        "tools_window_elapsed_ms": max(0, int(tools_window_elapsed_ms or 0)),
        "tools_wall_elapsed_ms": tools_wall_elapsed_ms,
        "tools_execution_elapsed_ms": tools_execution_elapsed_ms,
        "tools_overlap_llm_ms": tools_overlap_llm_ms,
        "tools_net_wait_ms": tools_net_wait_ms,
    }


def _normalize_intervals(intervals: List[Dict[str, int]]) -> List[Dict[str, int]]:
    normalized: List[Dict[str, int]] = []
    for interval in intervals or []:
        try:
            started_at_ms = int(interval.get("started_at_ms"))
            finished_at_ms = int(interval.get("finished_at_ms"))
        except (TypeError, ValueError):
            continue
        if finished_at_ms < started_at_ms:
            started_at_ms, finished_at_ms = finished_at_ms, started_at_ms
        try:
            elapsed_ms = int(interval.get("elapsed_ms"))
        except (TypeError, ValueError):
            elapsed_ms = finished_at_ms - started_at_ms
        normalized.append(
            {
                "started_at_ms": max(0, started_at_ms),
                "finished_at_ms": max(0, finished_at_ms),
                "elapsed_ms": max(0, elapsed_ms),
            }
        )
    return normalized


def _merge_intervals(intervals: List[Dict[str, int]]) -> List[Dict[str, int]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: (item["started_at_ms"], item["finished_at_ms"]))
    merged: List[Dict[str, int]] = []
    for interval in sorted_intervals:
        if not merged or interval["started_at_ms"] > merged[-1]["finished_at_ms"]:
            merged.append(
                {
                    "started_at_ms": interval["started_at_ms"],
                    "finished_at_ms": interval["finished_at_ms"],
                }
            )
            continue
        merged[-1]["finished_at_ms"] = max(merged[-1]["finished_at_ms"], interval["finished_at_ms"])
    return merged


def _sum_interval_lengths(intervals: List[Dict[str, int]]) -> int:
    return sum(max(0, int(interval["finished_at_ms"]) - int(interval["started_at_ms"])) for interval in intervals)


def _intervals_overlap_ms(
    intervals: List[Dict[str, int]],
    started_at_ms: int,
    finished_at_ms: int,
) -> int:
    if finished_at_ms < started_at_ms:
        started_at_ms, finished_at_ms = finished_at_ms, started_at_ms
    total = 0
    for interval in intervals:
        overlap_start = max(started_at_ms, int(interval["started_at_ms"]))
        overlap_finish = min(finished_at_ms, int(interval["finished_at_ms"]))
        total += max(0, overlap_finish - overlap_start)
    return total


def _extract_tool_call_index(tool_call: Dict[str, Any]) -> Optional[int]:
    raw_index = tool_call.get("index")
    if raw_index in {None, ""}:
        return None
    try:
        return int(raw_index)
    except (TypeError, ValueError):
        return None


def _serialize_tool_call(tool_call: Dict[str, Any]) -> str:
    function_payload = dict(tool_call.get("function") or {})
    normalized = {
        "index": _extract_tool_call_index(tool_call),
        "id": tool_call.get("id"),
        "type": tool_call.get("type"),
        "function": {
            "name": function_payload.get("name"),
            "arguments": function_payload.get("arguments"),
        },
    }
    return json.dumps(normalized, ensure_ascii=False, sort_keys=True)


def _filter_pending_tool_calls(
    tool_calls: Optional[List[Dict[str, Any]]],
    executed_tool_calls: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    remaining: List[Dict[str, Any]] = []
    executed_counts: Dict[str, int] = {}
    executed_index_counts: Dict[int, int] = {}
    for tool_call in executed_tool_calls or []:
        key = _serialize_tool_call(tool_call)
        executed_counts[key] = int(executed_counts.get(key) or 0) + 1
        tool_call_index = _extract_tool_call_index(tool_call)
        if tool_call_index is not None:
            executed_index_counts[tool_call_index] = int(executed_index_counts.get(tool_call_index) or 0) + 1

    for tool_call in tool_calls or []:
        tool_call_index = _extract_tool_call_index(tool_call)
        if tool_call_index is not None and int(executed_index_counts.get(tool_call_index) or 0) > 0:
            executed_index_counts[tool_call_index] -= 1
            continue
        key = _serialize_tool_call(tool_call)
        if int(executed_counts.get(key) or 0) > 0:
            executed_counts[key] -= 1
            continue
        remaining.append(tool_call)
    return remaining


def _current_utc8_date_text() -> str:
    return datetime.now(_UTC8).strftime("%Y/%m/%d")


def _build_current_date_reminder() -> str:
    return (
        "{}\n# currentDate\nToday's date is {}.\n\n"
        "      IMPORTANT: this context may or may not be relevant to your tasks. "
        "You should not respond to this context unless it is highly relevant to your task.\n"
        "{}"
    ).format(
        _CURRENT_DATE_REMINDER_START,
        _current_utc8_date_text(),
        _CURRENT_DATE_REMINDER_END,
    )


def _append_current_date_reminder_to_latest_user_message(
    messages: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    appended = [dict(message) for message in messages]
    for index in range(len(appended) - 1, -1, -1):
        message = appended[index]
        if message.get("role") != "user":
            continue
        content = message.get("content")
        if not isinstance(content, str):
            return appended
        stripped_content = _strip_current_date_reminder(content).rstrip()
        appended[index] = {
            **message,
            "content": "{}\n\n{}".format(stripped_content, _build_current_date_reminder()),
        }
        return appended
    return appended


def _strip_current_date_reminder(content: str) -> str:
    start = content.find(_CURRENT_DATE_REMINDER_START)
    if start < 0:
        return content
    end = content.find(_CURRENT_DATE_REMINDER_END, start)
    if end < 0:
        return content
    return "{}{}".format(
        content[:start],
        content[end + len(_CURRENT_DATE_REMINDER_END):],
    )


def _append_selected_ontology_context_to_user_content(
    content: Any,
    selected_ontology_context: str,
) -> Any:
    if isinstance(content, list):
        return [
            *content,
            {
                "type": "text",
                "text": "\n\n{}".format(selected_ontology_context),
            },
        ]
    return "{}\n\n{}".format(str(content or ""), selected_ontology_context).strip()
