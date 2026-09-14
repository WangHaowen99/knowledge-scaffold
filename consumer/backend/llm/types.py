# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Public response types for the LLM service."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional


@dataclass
class LLMResponse:
    """Response from an LLM API call."""

    content: str
    model: str
    finish_reason: Optional[str] = None
    reasoning_content: Optional[str] = None
    messages: Optional[List[Dict[str, Any]]] = None
    tool_calls: Optional[List[Dict[str, Any]]] = None
    data: Optional[Any] = None
    usage: Optional[Dict[str, Any]] = None
    request_params: Optional[Dict[str, Any]] = None
    response_headers_ms: Optional[int] = None
    duration_ms: Optional[int] = None
    first_reasoning_ms: Optional[int] = None
    first_content_ms: Optional[int] = None
    first_tool_call_ms: Optional[int] = None
