# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Collectors that adapt OpenAI chat completion responses into internal messages."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from utils.logging import get_logger
from llm.adapters import to_plain_data
from llm.tool_calls import emit_closed_stream_tool_calls, normalize_tool_calls


logger = get_logger(__name__)


def collect_non_stream_response(
    response: Any,
    *,
    emit_func: Optional[Callable[..., None]] = None,
    run_id: Optional[str] = None,
    reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
    request_label: Optional[str] = None,
    dispatch_reasoning_summary: Callable[..., bool],
) -> Dict[str, Any]:
    """Collect a regular JSON chat completion response."""
    def emit_token(token_type: str, token: str, meta: Optional[Dict[str, Any]] = None) -> None:
        if not emit_func or not token:
            return
        if meta:
            try:
                emit_func(token_type, token, meta)
                return
            except TypeError:
                pass
        emit_func(token_type, token)

    data = to_plain_data(response)

    model = data.get("model", "")
    choices = data.get("choices") or []
    if not choices:
        logger.warning("Abnormal non-stream response: %s", data)
        return {
            "message": {"role": "assistant", "content": ""},
            "finish_reason": None,
            "model": model,
        }

    choice = choices[0]
    message_payload = choice.get("message") or {}
    reasoning_text = (
        message_payload.get("reasoning_content")
        or message_payload.get("reasoning")
        or ""
    )
    content = message_payload.get("content") or ""

    message: Dict[str, Any] = {
        "role": "assistant",
        "content": content,
    }
    if reasoning_text:
        message["reasoning_content"] = reasoning_text
        reasoning_block_id = "reasoning_{}_1".format(uuid.uuid4().hex[:8])
        emit_token(
            "reasoning",
            reasoning_text,
            {"reasoning_block_id": reasoning_block_id},
        )
        if reasoning_summary_callback:
            dispatch_reasoning_summary(
                reasoning_text=reasoning_text,
                reasoning_block_id=reasoning_block_id,
                run_id=run_id,
                reasoning_summary_callback=reasoning_summary_callback,
            )

    if content:
        emit_token("streaming", content)

    normalized_tool_calls = normalize_tool_calls(message_payload.get("tool_calls"))
    if normalized_tool_calls:
        message["tool_calls"] = normalized_tool_calls

    usage = data.get("usage") if isinstance(data.get("usage"), dict) else {}

    return {
        "message": message,
        "finish_reason": choice.get("finish_reason"),
        "model": model,
        "usage": dict(usage),
    }


def collect_stream_response(
    response: Any,
    *,
    emit_func: Optional[Callable[..., None]] = None,
    run_id: Optional[str] = None,
    reasoning_summary_callback: Optional[Callable[[str, str], None]] = None,
    request_label: Optional[str] = None,
    tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
    dispatch_reasoning_summary: Callable[..., bool],
) -> Dict[str, Any]:
    """Collect a streaming chat completion response."""
    content_parts: List[str] = []
    reasoning_parts: List[str] = []
    tool_calls_map: Dict[int, Dict[str, Any]] = {}
    emitted_tool_call_indices: set[int] = set()
    normalized_cache: Dict[int, Dict[str, Any]] = {}
    finish_reason: Optional[str] = None
    model: str = ""
    reasoning_stream_id = uuid.uuid4().hex[:8]
    reasoning_block_counter = 0
    active_reasoning_block_id: Optional[str] = None
    active_reasoning_parts: List[str] = []
    collect_started_at = time.perf_counter()
    first_reasoning_ms: Optional[int] = None
    first_content_ms: Optional[int] = None
    first_tool_call_ms: Optional[int] = None
    usage: Dict[str, Any] = {}

    def emit_stream_token(token_type: str, token: str, meta: Optional[Dict[str, Any]] = None) -> None:
        if not emit_func:
            return
        if meta:
            try:
                emit_func(token_type, token, meta)
                return
            except TypeError:
                pass
        emit_func(token_type, token)

    def start_reasoning_block() -> str:
        nonlocal reasoning_block_counter
        reasoning_block_counter += 1
        return "reasoning_{}_{}".format(reasoning_stream_id, reasoning_block_counter)

    def flush_reasoning_block() -> None:
        nonlocal active_reasoning_block_id, active_reasoning_parts
        if not active_reasoning_block_id:
            return
        block_id = active_reasoning_block_id
        reasoning_text = "".join(active_reasoning_parts).strip()
        active_reasoning_block_id = None
        active_reasoning_parts = []

        if not reasoning_text or not reasoning_summary_callback:
            return
        dispatch_reasoning_summary(
            reasoning_text=reasoning_text,
            reasoning_block_id=block_id,
            run_id=run_id,
            reasoning_summary_callback=reasoning_summary_callback,
        )

    def emit_closed_tool_calls(closed_indices: List[int]) -> None:
        emit_closed_stream_tool_calls(
            tool_calls_map=tool_calls_map,
            closed_indices=closed_indices,
            emitted_indices=emitted_tool_call_indices,
            tool_calls_closed_callback=tool_calls_closed_callback,
            normalized_cache=normalized_cache,
        )

    def ready_contiguous_tool_call_indices() -> List[int]:
        ready_indices: List[int] = []
        for idx in sorted(tool_calls_map.keys()):
            if idx in emitted_tool_call_indices:
                continue
            raw_tool_call = tool_calls_map[idx]
            function_payload = dict(raw_tool_call.get("function") or {})
            tool_call_id = str(raw_tool_call.get("id") or "").strip()
            function_name = str(function_payload.get("name") or "").strip()
            try:
                parsed_arguments = json.loads(str(function_payload.get("arguments") or ""))
            except json.JSONDecodeError:
                break
            if not tool_call_id or not function_name or not isinstance(parsed_arguments, dict):
                break
            ready_indices.append(idx)
        return ready_indices

    try:
        for data in iter_chunk_data(response):
            if not model:
                model = data.get("model", "")

            chunk_usage = data.get("usage")
            if isinstance(chunk_usage, dict):
                usage.update(chunk_usage)

            choices = data.get("choices")
            if not choices:
                continue

            choice = choices[0]
            delta = choice.get("delta") or {}
            logger.debug("delta token: %s", delta)
            chunk_finish = choice.get("finish_reason")

            if delta.get("reasoning_content") or delta.get("reasoning"):
                token = delta.get("reasoning_content") if delta.get("reasoning_content") else delta.get("reasoning")
                if first_reasoning_ms is None:
                    first_reasoning_ms = int((time.perf_counter() - collect_started_at) * 1000)
                if not active_reasoning_block_id:
                    active_reasoning_block_id = start_reasoning_block()
                reasoning_parts.append(token)
                active_reasoning_parts.append(token)
                emit_stream_token(
                    "reasoning",
                    token,
                    {"reasoning_block_id": active_reasoning_block_id},
                )

            if delta.get("content"):
                flush_reasoning_block()
                if first_content_ms is None:
                    first_content_ms = int((time.perf_counter() - collect_started_at) * 1000)
                token = delta["content"]
                content_parts.append(token)
                emit_stream_token("streaming", token)

            if delta.get("tool_calls"):
                flush_reasoning_block()
                if first_tool_call_ms is None:
                    first_tool_call_ms = int((time.perf_counter() - collect_started_at) * 1000)
                for tc in delta["tool_calls"]:
                    idx = int(tc.get("index", 0))
                    if idx not in tool_calls_map:
                        tool_calls_map[idx] = {
                            "index": idx,
                            "id": tc.get("id", ""),
                            "type": tc.get("type", "function"),
                            "function": {"name": "", "arguments": ""},
                        }
                    tc_entry = tool_calls_map[idx]
                    if tc.get("id"):
                        tc_entry["id"] = tc["id"]
                    if tc.get("function"):
                        func = tc["function"]
                        if func.get("name"):
                            tc_entry["function"]["name"] = func["name"]
                        if "arguments" in func and func["arguments"] is not None:
                            tc_entry["function"]["arguments"] = "{}{}".format(
                                tc_entry["function"]["arguments"],
                                str(func["arguments"]),
                            )
                emit_closed_tool_calls(ready_contiguous_tool_call_indices())

            if chunk_finish:
                finish_reason = chunk_finish
                emit_closed_tool_calls(sorted(tool_calls_map.keys()))
        flush_reasoning_block()
    finally:
        close_response = getattr(response, "close", None)
        if callable(close_response):
            close_response()

    message: Dict[str, Any] = {"role": "assistant"}
    message["content"] = "".join(content_parts) if content_parts else ""
    if reasoning_parts:
        message["reasoning_content"] = "".join(reasoning_parts)

    final_tool_calls: List[Dict[str, Any]] = []
    for idx in sorted(tool_calls_map.keys()):
        cached = normalized_cache.get(idx)
        if cached is not None:
            final_tool_calls.append(cached)
            continue
        renormalized = normalize_tool_calls([tool_calls_map[idx]])
        if renormalized:
            final_tool_calls.append(renormalized[0])
    normalized_tool_calls = final_tool_calls or None
    if normalized_tool_calls:
        message["tool_calls"] = normalized_tool_calls

    return {
        "message": message,
        "finish_reason": finish_reason,
        "model": model,
        "usage": usage,
        "first_reasoning_ms": first_reasoning_ms,
        "first_content_ms": first_content_ms,
        "first_tool_call_ms": first_tool_call_ms,
    }


def iter_chunk_data(response: Any):
    """Yield plain chunk dictionaries from SDK streams or SSE-style fakes.

    Production path is the OpenAI SDK stream (iterable of pydantic objects).
    The ``iter_lines`` branch exists for unit tests that simulate raw SSE.
    """
    if hasattr(response, "iter_lines"):
        response.encoding = "utf-8"
        for line in response.iter_lines(decode_unicode=True):
            if not line:
                continue
            line = line.strip()
            if not line.startswith("data:"):
                continue
            data_str = line[5:].strip()
            if data_str == "[DONE]":
                break
            try:
                yield json.loads(data_str)
            except json.JSONDecodeError as exc:
                logger.warning("Failed to parse streaming chunk: %s (%s)", data_str, exc)
        return

    for chunk in response:
        data = to_plain_data(chunk)
        if isinstance(data, dict):
            yield data
