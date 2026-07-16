"""Semantic observer facade for one agent loop execution."""

from __future__ import annotations

import time
import uuid
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional

from agent.exceptions import WorkflowCancelledError
from utils.logging import get_logger
from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from integration.observability.observer import NOOP_OBSERVER

logger = get_logger("observability.traces.agent")


class _LLMCallTrace:
    def __init__(
        self,
        runtime_trace: "AgentRuntimeTrace",
        iteration: int,
        call_index: Optional[int] = None,
    ) -> None:
        self._runtime_trace = runtime_trace
        self._iteration = iteration
        self._call_index = call_index

    def finish(
        self,
        *,
        usage: Dict[str, Any],
        prompt_text: Any,
        completion_text: Any,
        reasoning_text: Any = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        finish_reason: Optional[str] = None,
        response_model: Optional[str] = None,
        response_headers_ms: Optional[int] = None,
        duration_ms: Optional[int] = None,
        stream_duration_ms: Optional[int] = None,
        wall_duration_ms: Optional[int] = None,
        first_reasoning_ms: Optional[int] = None,
        first_content_ms: Optional[int] = None,
        first_tool_call_ms: Optional[int] = None,
        request_label: Optional[str] = None,
        model: Optional[str] = None,
        model_provider: Optional[str] = None,
        tool_names: Optional[List[str]] = None,
    ) -> None:
        self._runtime_trace.llm_finished(
            iteration=self._iteration,
            usage=usage,
            prompt_text=prompt_text,
            completion_text=completion_text,
            reasoning_text=reasoning_text,
            tool_calls=tool_calls,
            finish_reason=finish_reason,
            response_model=response_model,
            response_headers_ms=response_headers_ms,
            duration_ms=duration_ms,
            stream_duration_ms=stream_duration_ms,
            wall_duration_ms=wall_duration_ms,
            first_reasoning_ms=first_reasoning_ms,
            first_content_ms=first_content_ms,
            first_tool_call_ms=first_tool_call_ms,
            request_label=request_label,
            model=model,
            model_provider=model_provider,
            tool_names=tool_names,
            call_index=self._call_index,
        )


class _ToolBatchTrace:
    def __init__(self, runtime_trace: "AgentRuntimeTrace", iteration: int) -> None:
        self._runtime_trace = runtime_trace
        self._iteration = iteration

    def finish(
        self,
        *,
        tool_calls_count: int,
        tool_names: List[str],
        duration_ms: int,
    ) -> None:
        self._runtime_trace.tool_batch_finished(
            iteration=self._iteration,
            tool_calls_count=tool_calls_count,
            tool_names=tool_names,
            duration_ms=duration_ms,
        )


class AgentRuntimeTrace:
    """Business-facing facade for one agent loop execution.

    Runtime code calls this layer to describe agent/LLM/tool lifecycle events.
    Provider-specific mapping and I/O stay outside this facade.
    """

    def __init__(
        self,
        *,
        observer: Any = None,
        run_id: str,
        session_id: Optional[str],
        turn_id: Optional[int],
        caller: str,
        lineage: Optional[Dict[str, Any]] = None,
    ) -> None:
        self.observer = observer or NOOP_OBSERVER
        self.run_id = str(run_id or "")
        self.session_id = session_id
        self.turn_id = turn_id
        self.caller = str(caller or "main")
        self.lineage = dict(lineage or {})
        self.loop_span_key = "agent_loop:{}".format(self._caller_suffix())
        self._loop_finished = False
        self._phase_stack: List[str] = []
        self._prepared_iterations: set[str] = set()

    @property
    def enabled(self) -> bool:
        return _observer_enabled(self.observer)

    @contextmanager
    def agent_loop_scope(
        self,
        *,
        agent_mode: str,
    ) -> Iterator["AgentRuntimeTrace"]:
        self.agent_loop_started(
            agent_mode=agent_mode,
        )
        try:
            yield self
        except WorkflowCancelledError:
            self.agent_loop_finished(RuntimeStatus.CANCELLED)
            raise
        except Exception:
            self.agent_loop_finished(RuntimeStatus.ERROR)
            raise
        else:
            self.agent_loop_finished(RuntimeStatus.SUCCESS)

    @contextmanager
    def llm_call_scope(
        self,
        *,
        iteration: int,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Any]],
        model: str,
        api_params: Optional[Dict[str, Any]] = None,
        call_index: Optional[int] = None,
        request_label: Optional[str] = None,
    ) -> Iterator[_LLMCallTrace]:
        self.llm_started(
            iteration=iteration,
            messages=messages,
            tools=tools,
            model=model,
            api_params=api_params,
            call_index=call_index,
            request_label=request_label,
        )
        trace = _LLMCallTrace(self, iteration, call_index=call_index)
        try:
            yield trace
        except WorkflowCancelledError:
            self.llm_failed(
                iteration=iteration,
                status=RuntimeStatus.CANCELLED,
                error_type="WorkflowCancelledError",
                error_message="Workflow cancelled by user",
                call_index=call_index,
            )
            raise
        except Exception as exc:
            self.llm_failed(
                iteration=iteration,
                error_type=type(exc).__name__,
                error_message=str(exc),
                call_index=call_index,
            )
            raise

    @contextmanager
    def tool_batch_scope(
        self,
        *,
        iteration: int,
    ) -> Iterator[_ToolBatchTrace]:
        with self._tool_batch_completion_scope(iteration=iteration, starts_batch=True) as trace:
            yield trace

    @contextmanager
    def continued_tool_batch_scope(
        self,
        *,
        iteration: int,
    ) -> Iterator[_ToolBatchTrace]:
        with self._tool_batch_completion_scope(iteration=iteration, starts_batch=False) as trace:
            yield trace

    @contextmanager
    def tool_batch_wait_scope(self, *, iteration: int) -> Iterator[None]:
        try:
            yield
        except WorkflowCancelledError:
            self.tool_batch_failed(iteration=iteration, status=RuntimeStatus.CANCELLED)
            raise
        except Exception:
            self.tool_batch_failed(iteration=iteration)
            raise

    @contextmanager
    def phase_scope(
        self,
        *,
        phase_name: str,
        component: str,
        parent_key: Optional[str] = None,
        metrics: Optional[Dict[str, Any]] = None,
    ) -> Iterator[str]:
        started_at = time.perf_counter()
        phase_metrics = dict(metrics or {})
        span_key = self._phase_span_key(phase_name, component)
        resolved_parent_key = parent_key or self._current_phase_parent_key() or self.loop_span_key
        self._emit_phase(
            RuntimeEventType.RUNTIME_PHASE_STARTED,
            span_key=span_key,
            parent_key=resolved_parent_key,
            phase_name=phase_name,
            component=component,
            metrics=phase_metrics,
        )
        self._phase_stack.append(span_key)
        try:
            yield span_key
        except WorkflowCancelledError:
            self._pop_phase_span(span_key)
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FAILED,
                span_key=span_key,
                parent_key=resolved_parent_key,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.CANCELLED,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics=phase_metrics,
            )
            raise
        except Exception as exc:
            self._pop_phase_span(span_key)
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FAILED,
                span_key=span_key,
                parent_key=resolved_parent_key,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.ERROR,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics={
                    **phase_metrics,
                    "error_type": type(exc).__name__,
                    "error_message": str(exc)[:300],
                },
            )
            raise
        else:
            self._pop_phase_span(span_key)
            self._emit_phase(
                RuntimeEventType.RUNTIME_PHASE_FINISHED,
                span_key=span_key,
                parent_key=resolved_parent_key,
                phase_name=phase_name,
                component=component,
                status=RuntimeStatus.SUCCESS,
                duration_ms=int((time.perf_counter() - started_at) * 1000),
                metrics=phase_metrics,
            )

    @contextmanager
    def _tool_batch_completion_scope(
        self,
        *,
        iteration: int,
        starts_batch: bool,
    ) -> Iterator[_ToolBatchTrace]:
        if starts_batch:
            self.tool_batch_started(iteration=iteration)
        trace = _ToolBatchTrace(self, iteration)
        try:
            yield trace
        except WorkflowCancelledError:
            self.tool_batch_failed(iteration=iteration, status=RuntimeStatus.CANCELLED)
            raise
        except Exception:
            self.tool_batch_failed(iteration=iteration)
            raise

    def agent_loop_started(self, *, agent_mode: str) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.AGENT_LOOP_STARTED,
            span_key=self.loop_span_key,
            parent_key="run:{}".format(self.run_id),
            payload={
                "caller": self.caller,
                "run_id": self.run_id,
                "session_id": self.session_id,
                "agent_mode": agent_mode,
                **self.lineage,
            },
        )

    def agent_loop_finished(self, status: str = RuntimeStatus.SUCCESS) -> None:
        if self._loop_finished:
            return
        self._loop_finished = True
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.AGENT_LOOP_FINISHED,
            span_key=self.loop_span_key,
            payload={"status": status, "caller": self.caller},
        )

    def iteration_prepared(
        self,
        *,
        iteration: int,
        purpose: str,
        agent_mode: str,
        prompt_build_ms: int,
        system_prompt_chars: int,
        sliding_window_applied: bool,
        llm_message_count: int,
        llm_input_chars: Any,
        tools_count: int,
    ) -> None:
        self._prepared_iterations.add(str(iteration))
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.AGENT_ITERATION_PREPARED,
            span_key=self.iteration_span_key(iteration),
            parent_key=self._current_phase_parent_key() or self.loop_span_key,
            payload={
                "caller": self.caller,
                "iteration": iteration,
                "purpose": purpose,
                "agent_mode": agent_mode,
                "prompt_build_ms": prompt_build_ms,
                "system_prompt_chars": system_prompt_chars,
                "sliding_window_applied": sliding_window_applied,
                "llm_message_count": llm_message_count,
                "llm_input_chars": _resolve_lazy_value(llm_input_chars),
                "tools_count": tools_count,
                **self.lineage,
            },
        )

    def iteration_finished(
        self,
        *,
        iteration: int,
        status: str,
        prompt_build_ms: int,
        llm_stream_ms: int,
        llm_wall_ms: int,
        tools_window_ms: int,
        tools_overlap_llm_ms: int,
        tools_after_llm_ms: int,
        tool_calls_count: int,
        tool_names: List[str],
        total_ms: int,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.AGENT_ITERATION_FINISHED,
            span_key=self.iteration_span_key(iteration),
            payload={
                "caller": self.caller,
                "iteration": iteration,
                "status": status,
                "prompt_build_ms": prompt_build_ms,
                "llm_stream_ms": llm_stream_ms,
                "llm_wall_ms": llm_wall_ms,
                "tools_window_ms": tools_window_ms,
                "tools_overlap_llm_ms": tools_overlap_llm_ms,
                "tools_after_llm_ms": tools_after_llm_ms,
                "tool_calls_count": tool_calls_count,
                "tool_names": list(tool_names or []),
                "total_ms": total_ms,
            },
        )

    def llm_started(
        self,
        *,
        iteration: int,
        messages: List[Dict[str, Any]],
        tools: Optional[List[Any]],
        model: str,
        api_params: Optional[Dict[str, Any]] = None,
        call_index: Optional[int] = None,
        request_label: Optional[str] = None,
    ) -> str:
        span_key = self.llm_span_key(iteration, call_index=call_index)
        if not self.enabled:
            return span_key
        latest_input = _latest_user_input(messages)
        self._emit(
            RuntimeEventType.LLM_STARTED,
            span_key=span_key,
            parent_key=self._iteration_parent_key(iteration),
            payload={
                "caller": self.caller,
                "iteration": iteration,
                **({"call_index": call_index} if call_index is not None else {}),
                **({"request_label": request_label} if request_label else {}),
                "total_messages": len(messages),
                "messages_preview": _messages_preview(messages),
                "latest_input": latest_input,
                "tools_count": len(tools or []),
                "model": model,
                "api_params": dict(api_params or {}),
                "input": {
                    "latest_input": latest_input,
                    "messages": messages,
                    "tools": tools or [],
                    "api_params": dict(api_params or {}),
                },
                **self.lineage,
            },
        )
        return span_key

    def llm_finished(
        self,
        *,
        iteration: int,
        usage: Dict[str, Any],
        prompt_text: Any,
        completion_text: Any,
        reasoning_text: Any = "",
        tool_calls: Optional[List[Dict[str, Any]]] = None,
        finish_reason: Optional[str] = None,
        response_model: Optional[str] = None,
        response_headers_ms: Optional[int] = None,
        duration_ms: Optional[int] = None,
        stream_duration_ms: Optional[int] = None,
        wall_duration_ms: Optional[int] = None,
        first_reasoning_ms: Optional[int] = None,
        first_content_ms: Optional[int] = None,
        first_tool_call_ms: Optional[int] = None,
        request_label: Optional[str] = None,
        model: Optional[str] = None,
        model_provider: Optional[str] = None,
        tool_names: Optional[List[str]] = None,
        call_index: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        resolved_completion = _resolve_lazy_value(completion_text)
        resolved_reasoning = _resolve_lazy_value(reasoning_text)
        self._emit(
            RuntimeEventType.LLM_FINISHED,
            span_key=self.llm_span_key(iteration, call_index=call_index),
            payload={
                "status": RuntimeStatus.SUCCESS,
                "caller": self.caller,
                "iteration": iteration,
                **({"call_index": call_index} if call_index is not None else {}),
                "request_label": request_label,
                "model": model,
                "model_provider": model_provider,
                "usage": usage or {},
                "prompt_text": _resolve_lazy_value(prompt_text),
                "completion_text": resolved_completion,
                "reasoning_text": resolved_reasoning,
                "tool_calls": tool_calls or [],
                "finish_reason": finish_reason,
                "response_model": response_model,
                "response_headers_ms": response_headers_ms,
                "duration_ms": duration_ms,
                "stream_duration_ms": stream_duration_ms,
                "wall_duration_ms": wall_duration_ms,
                "first_reasoning_ms": first_reasoning_ms,
                "first_content_ms": first_content_ms,
                "first_tool_call_ms": first_tool_call_ms,
                "tool_calls_count": len(tool_calls or []),
                "tool_names": list(tool_names or []),
                "output": {
                    "content": resolved_completion,
                    "reasoning_content": resolved_reasoning,
                    "tool_calls": tool_calls or [],
                    "finish_reason": finish_reason,
                    "model": response_model,
                },
            },
        )

    def llm_failed(
        self,
        *,
        iteration: int,
        status: str = RuntimeStatus.ERROR,
        error_type: Optional[str] = None,
        error_message: Optional[str] = None,
        call_index: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.LLM_FAILED,
            span_key=self.llm_span_key(iteration, call_index=call_index),
            payload={
                "status": status,
                "iteration": iteration,
                **({"call_index": call_index} if call_index is not None else {}),
                "error_type": error_type,
                "error_message": str(error_message or "")[:1000],
            },
        )

    def tool_batch_started(
        self,
        *,
        iteration: int,
        batch_index: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.TOOL_BATCH_STARTED,
            span_key=self.tool_batch_span_key(iteration, batch_index=batch_index),
            parent_key=self._iteration_parent_key(iteration),
            payload={
                "caller": self.caller,
                "caller_suffix": self._caller_suffix(),
                "iteration": iteration,
                **({"batch_index": batch_index} if batch_index is not None else {}),
                **self.lineage,
            },
        )

    def tool_batch_finished(
        self,
        *,
        iteration: int,
        tool_calls_count: int,
        tool_names: List[str],
        duration_ms: int,
        batch_index: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.TOOL_BATCH_FINISHED,
            span_key=self.tool_batch_span_key(iteration, batch_index=batch_index),
            payload={
                "status": RuntimeStatus.SUCCESS,
                "caller": self.caller,
                "iteration": iteration,
                **({"batch_index": batch_index} if batch_index is not None else {}),
                "tool_calls_count": tool_calls_count,
                "tool_names": tool_names,
                "duration_ms": duration_ms,
            },
        )

    def tool_batch_failed(
        self,
        *,
        iteration: int,
        status: str = RuntimeStatus.ERROR,
        batch_index: Optional[int] = None,
    ) -> None:
        if not self.enabled:
            return
        self._emit(
            RuntimeEventType.TOOL_BATCH_FAILED,
            span_key=self.tool_batch_span_key(iteration, batch_index=batch_index),
            payload={
                "status": status,
                "caller": self.caller,
                "iteration": iteration,
                **({"batch_index": batch_index} if batch_index is not None else {}),
            },
        )

    def _emit_phase(
        self,
        event_type: str,
        *,
        span_key: str,
        parent_key: str,
        phase_name: str,
        component: str,
        metrics: Dict[str, Any],
        status: Optional[str] = None,
        duration_ms: Optional[int] = None,
    ) -> None:
        payload = {
            "phase_name": phase_name,
            "component": component,
            "caller": self.caller,
            **metrics,
            **self.lineage,
        }
        if status is not None:
            payload["status"] = status
        if duration_ms is not None:
            payload["duration_ms"] = duration_ms
        self._emit(
            event_type,
            span_key=span_key,
            parent_key=parent_key,
            payload=payload,
        )

    def llm_span_key(self, iteration: int, call_index: Optional[int] = None) -> str:
        base_key = "llm:{}:{}".format(self._caller_suffix(), iteration)
        if call_index is None:
            return base_key
        return "{}:{}".format(base_key, int(call_index))

    def iteration_span_key(self, iteration: int) -> str:
        return "iteration:{}:{}".format(self._caller_suffix(), iteration)

    def tool_batch_span_key(self, iteration: int, batch_index: Optional[int] = None) -> str:
        base_key = "tools:{}:{}".format(self._caller_suffix(), iteration)
        if batch_index is None:
            return base_key
        return "{}:{}".format(base_key, int(batch_index))

    def _phase_span_key(self, phase_name: str, component: str) -> str:
        return "phase:{}:{}:{}:{}".format(
            self._caller_suffix(),
            _safe_span_token(component),
            _safe_span_token(phase_name),
            uuid.uuid4().hex[:12],
        )

    def _current_phase_parent_key(self) -> Optional[str]:
        return self._phase_stack[-1] if self._phase_stack else None

    def _iteration_parent_key(self, iteration: int) -> str:
        if str(iteration) in self._prepared_iterations:
            return self.iteration_span_key(iteration)
        return self._current_phase_parent_key() or self.loop_span_key

    def _pop_phase_span(self, span_key: str) -> None:
        if not self._phase_stack:
            return
        if self._phase_stack[-1] == span_key:
            self._phase_stack.pop()
            return
        try:
            self._phase_stack.remove(span_key)
        except ValueError:
            return

    def _emit(
        self,
        event_type: str,
        *,
        span_key: Optional[str] = None,
        parent_key: Optional[str] = None,
        payload: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not self.run_id or not self.enabled:
            return
        self.observer.emit(
            RuntimeEvent(
                type=event_type,
                run_id=self.run_id,
                session_id=self.session_id,
                turn_id=self.turn_id,
                span_key=span_key,
                parent_key=parent_key,
                payload=payload or {},
            )
        )

    def _caller_suffix(self) -> str:
        if self.caller != "subagent":
            return self.caller
        subagent_id = str(self.lineage.get("subagent_id") or "").strip()
        return "subagent:{}".format(subagent_id) if subagent_id else "subagent"


def _messages_preview(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    preview: List[Dict[str, Any]] = []
    for message in messages[-5:]:
        preview.append(
            {
                "role": message.get("role", "unknown"),
                "content": str(message.get("content", ""))[:300],
            }
        )
    return preview


def _latest_user_input(messages: List[Dict[str, Any]]) -> str:
    for message in reversed(messages or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def _observer_enabled(observer: Any) -> bool:
    return bool(getattr(observer, "enabled", True))


def _resolve_lazy_value(value: Any) -> Any:
    if not callable(value):
        return value
    try:
        return value()
    except Exception:
        logger.warning("Failed to build observability payload", exc_info=True)
        return ""


def _safe_span_token(value: Any) -> str:
    token = str(value or "unknown").strip().replace(" ", "_")
    return token or "unknown"
