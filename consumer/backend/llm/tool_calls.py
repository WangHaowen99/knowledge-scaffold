# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tool-call normalization helpers for chat completions."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, List, Optional

from utils.json_utils import _loads_json_object_loose


def normalize_tool_calls(tool_calls: Optional[List[Dict[str, Any]]]) -> Optional[List[Dict[str, Any]]]:
    """Normalize tool calls and repair JSON arguments when possible."""
    if not tool_calls:
        return None

    normalized_tool_calls: List[Dict[str, Any]] = []
    for raw_tool_call in tool_calls:
        if not isinstance(raw_tool_call, dict):
            continue

        tool_call = dict(raw_tool_call)
        function_payload = dict(tool_call.get("function") or {})
        function_payload.setdefault("name", "")
        function_payload.setdefault("arguments", "")

        parsed_args = _loads_json_object_loose(function_payload.get("arguments"))
        if parsed_args is not None:
            function_payload["arguments"] = json.dumps(parsed_args, ensure_ascii=False)

        tool_call["function"] = function_payload
        normalized_tool_calls.append(tool_call)

    return normalized_tool_calls or None


def emit_closed_stream_tool_calls(
    *,
    tool_calls_map: Dict[int, Dict[str, Any]],
    closed_indices: List[int],
    emitted_indices: set[int],
    tool_calls_closed_callback: Optional[Callable[[List[Dict[str, Any]]], None]],
    normalized_cache: Optional[Dict[int, Dict[str, Any]]] = None,
) -> None:
    """Emit streamed tool calls once their JSON arguments are complete.

    Populates ``normalized_cache`` with finalized tool-call dicts so the
    outer collector can reuse them without re-normalizing.
    """
    normalized_indices = sorted(
        {
            int(idx)
            for idx in (closed_indices or [])
            if idx in tool_calls_map and idx not in emitted_indices
        }
    )
    if not normalized_indices:
        return

    ready_tool_calls: List[Dict[str, Any]] = []
    ready_indices: List[int] = []
    for idx in normalized_indices:
        normalized_tool_call_list = normalize_tool_calls([tool_calls_map[idx]])
        if not normalized_tool_call_list:
            continue
        normalized_tool_call = normalized_tool_call_list[0]
        function_payload = dict(normalized_tool_call.get("function") or {})
        tool_call_id = str(normalized_tool_call.get("id") or "").strip()
        function_name = str(function_payload.get("name") or "").strip()
        parsed_arguments = _loads_json_object_loose(function_payload.get("arguments"))
        if not tool_call_id or not function_name or parsed_arguments is None:
            continue
        function_payload["arguments"] = json.dumps(parsed_arguments, ensure_ascii=False)
        normalized_tool_call["function"] = function_payload
        ready_tool_calls.append(normalized_tool_call)
        ready_indices.append(idx)
        if normalized_cache is not None:
            normalized_cache[idx] = normalized_tool_call

    if not ready_tool_calls:
        return

    emitted_indices.update(ready_indices)
    if tool_calls_closed_callback:
        tool_calls_closed_callback(ready_tool_calls)
