"""Shared loop event emitters."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Callable, Dict, Optional

from integration.observability import AgentRuntimeTrace
from utils.streaming import emit_agent_event


def event_source_node(caller: str, source_node: Optional[str] = None) -> str:
    explicit_source_node = str(source_node or "").strip()
    if explicit_source_node:
        return explicit_source_node
    normalized = str(caller or "").strip()
    if normalized == "main":
        return "agent_loop"
    return normalized or "agent_loop"


def _stream_llm_span_key(
    caller: str,
    iteration: int,
    meta: Optional[Mapping[str, Any]],
) -> str:
    suffix = str((meta or {}).get("caller_suffix") or caller or "main").strip() or "main"
    call_index = max(1, int((meta or {}).get("call_index") or 1))
    return "llm:{}:{}:{}".format(
        suffix,
        max(1, int(iteration or 1)),
        call_index,
    )


def _stream_iteration_span_key(
    caller: str,
    iteration: int,
    meta: Optional[Mapping[str, Any]],
) -> str:
    suffix = str((meta or {}).get("caller_suffix") or caller or "main").strip() or "main"
    return "iteration:{}:{}".format(suffix, max(1, int(iteration or 1)))


def emit_token_delta_event(
    state: Mapping[str, Any],
    *,
    caller: str,
    event_writer: Optional[Callable[[Dict[str, Any]], None]],
    channel: str,
    text: str,
    iteration: int,
    token_callback: Optional[Callable[[str, str, Optional[Dict[str, Any]]], None]] = None,
    meta: Optional[Mapping[str, Any]] = None,
    source_node: Optional[str] = None,
) -> None:
    normalized_channel = "reasoning" if channel == "reasoning" else "content"
    callback_meta = dict(meta or {})
    callback_meta.setdefault("iteration", max(1, int(iteration or 1)))
    if callable(token_callback):
        token_callback(normalized_channel, text, callback_meta)
        return
    if event_writer is None or not state.get("run_id") or not text:
        return
    payload: Dict[str, Any] = {
        "channel": normalized_channel,
        "text": text,
        "iteration": max(1, int(iteration or 1)),
    }
    reasoning_block_id = str((meta or {}).get("reasoning_block_id") or "").strip()
    if normalized_channel == "reasoning" and reasoning_block_id:
        payload["reasoning_block_id"] = reasoning_block_id
    emit_agent_event(
        run_id=str(state.get("run_id") or ""),
        session_id=str(state.get("session_id") or ""),
        turn_id=int(state.get("current_turn_id") or 0),
        event_type="token.delta",
        source={"node": event_source_node(caller, source_node), "component": "llm"},
        payload=payload,
        writer=event_writer,
        span_key=_stream_llm_span_key(caller, iteration, meta),
        parent_key=_stream_iteration_span_key(caller, iteration, meta),
    )


def emit_reasoning_summary_event(
    state: Mapping[str, Any],
    *,
    caller: str,
    event_writer: Optional[Callable[[Dict[str, Any]], None]],
    iteration: int,
    reasoning_block_id: str,
    summary: str,
    reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
    source_node: Optional[str] = None,
    call_index: Optional[int] = None,
) -> None:
    if callable(reasoning_summary_callback):
        reasoning_summary_callback(reasoning_block_id, summary)
        return
    missing_event_context = (
        event_writer is None
        or not state.get("run_id")
        or not reasoning_block_id
        or not summary
    )
    if missing_event_context:
        return
    emit_agent_event(
        run_id=str(state.get("run_id") or ""),
        session_id=str(state.get("session_id") or ""),
        turn_id=int(state.get("current_turn_id") or 0),
        event_type="reasoning.summary",
        source={"node": event_source_node(caller, source_node), "component": "llm"},
        payload={
            "channel": "reasoning",
            "summary": str(summary or ""),
            "reasoning_block_id": str(reasoning_block_id or ""),
            "iteration": max(1, int(iteration or 1)),
        },
        writer=event_writer,
        span_key=_stream_llm_span_key(
            caller,
            iteration,
            {"call_index": max(1, int(call_index or 1))},
        ),
        parent_key=_stream_iteration_span_key(caller, iteration, None),
    )


def emit_iteration_completed_event(
    state: Mapping[str, Any],
    *,
    caller: str,
    event_writer: Optional[Callable[[Dict[str, Any]], None]],
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
    tool_calls_count: Optional[int] = None,
    runtime_trace: Optional[AgentRuntimeTrace] = None,
    source_node: Optional[str] = None,
) -> None:
    normalized_tool_names = [
        str(name or "").strip()
        for name in tool_names
        if str(name or "").strip()
    ]
    timing_metrics = dict(tool_timing_metrics or {})
    normalized_tool_calls_count = (
        max(0, int(tool_calls_count))
        if tool_calls_count is not None
        else len(normalized_tool_names)
    )
    if runtime_trace is not None:
        runtime_trace.iteration_finished(
            iteration=iteration,
            status=status,
            prompt_build_ms=max(0, int(prompt_elapsed_ms or 0)),
            llm_stream_ms=max(0, int(llm_stream_elapsed_ms or 0)),
            llm_wall_ms=max(0, int(llm_wall_elapsed_ms or 0)),
            tools_window_ms=max(0, int(tools_elapsed_ms or 0)),
            tools_overlap_llm_ms=int(timing_metrics.get("tools_overlap_llm_ms") or 0),
            tools_after_llm_ms=int(timing_metrics.get("tools_after_llm_ms") or 0),
            tool_calls_count=normalized_tool_calls_count,
            tool_names=normalized_tool_names,
            total_ms=max(0, int(total_elapsed_ms or 0)),
        )
    if caller != "main" or event_writer is None or not state.get("run_id"):
        return
    iteration_span_key = "iteration:{}:{}".format(
        event_source_node(caller, source_node) if caller != "main" else "main",
        iteration,
    )
    public_timing: Dict[str, int] = {}
    if model_elapsed_ms is not None:
        public_timing["model_elapsed_ms"] = max(0, int(model_elapsed_ms or 0))
    if setup_elapsed_ms is not None:
        public_timing["setup_elapsed_ms"] = max(0, int(setup_elapsed_ms or 0))
    emit_agent_event(
        run_id=str(state.get("run_id") or ""),
        session_id=str(state.get("session_id") or ""),
        turn_id=int(state.get("current_turn_id") or 0),
        event_type="iteration.completed",
        source={"node": event_source_node(caller, source_node), "component": "iteration"},
        payload={
            "iteration": iteration,
            "status": status,
            "llm_elapsed_ms": max(0, int(llm_elapsed_ms or 0)),
            "llm_stream_elapsed_ms": max(0, int(llm_stream_elapsed_ms or 0)),
            "llm_wall_elapsed_ms": max(0, int(llm_wall_elapsed_ms or 0)),
            "tools_elapsed_ms": max(0, int(tools_elapsed_ms or 0)),
            "prompt_build_ms": max(0, int(prompt_elapsed_ms or 0)),
            "total_elapsed_ms": max(0, int(total_elapsed_ms or 0)),
            "tool_calls_count": normalized_tool_calls_count,
            "tool_names": normalized_tool_names,
            **public_timing,
            **timing_metrics,
        },
        writer=event_writer,
        span_key=iteration_span_key,
        parent_key="agent_loop:main",
    )
