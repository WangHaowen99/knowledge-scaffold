# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Helpers for session-level LLM request streaming mode."""

from __future__ import annotations

from typing import Any

LLM_REQUEST_MODE_STREAM = "stream"
LLM_REQUEST_MODE_SYNC = "sync"


def normalize_llm_request_mode(value: Any, default: str = LLM_REQUEST_MODE_STREAM) -> str:
    """Normalize request mode to either 'stream' or 'sync'."""
    default_mode = (
        LLM_REQUEST_MODE_SYNC
        if str(default or "").strip().lower() == LLM_REQUEST_MODE_SYNC
        else LLM_REQUEST_MODE_STREAM
    )

    raw = str(value or "").strip().lower()
    if raw in {"stream", "streaming", "sse", "true", "1", "on", "yes"}:
        return LLM_REQUEST_MODE_STREAM
    if raw in {"sync", "non-stream", "non_stream", "nonstream", "false", "0", "off", "no"}:
        return LLM_REQUEST_MODE_SYNC
    return default_mode


def is_llm_streaming_enabled(value: Any, default: bool = True) -> bool:
    """Return whether the given mode enables streaming to the upstream LLM API."""
    default_mode = LLM_REQUEST_MODE_STREAM if default else LLM_REQUEST_MODE_SYNC
    return normalize_llm_request_mode(value, default=default_mode) == LLM_REQUEST_MODE_STREAM
