"""Lightweight token budget estimation for preflight LLM limits."""

from __future__ import annotations

import json
import math
from typing import Any


def estimate_text_tokens(text: str) -> int:
    """Approximate mixed Chinese/ASCII tokens without a tokenizer dependency."""
    ascii_non_space = 0
    non_ascii = 0
    for ch in str(text or ""):
        if ord(ch) < 128:
            if not ch.isspace():
                ascii_non_space += 1
        elif not ch.isspace():
            non_ascii += 1
    return non_ascii + math.ceil(ascii_non_space / 4)


def estimate_payload_tokens(payload: Any) -> int:
    if payload is None:
        return 0
    if isinstance(payload, str):
        return estimate_text_tokens(payload)
    if isinstance(payload, (int, float, bool)):
        return estimate_text_tokens(str(payload))
    if isinstance(payload, dict):
        total = 0
        for key, value in payload.items():
            total += estimate_text_tokens(str(key))
            total += estimate_payload_tokens(value)
        return total
    if isinstance(payload, (list, tuple)):
        return sum(estimate_payload_tokens(item) for item in payload)
    try:
        return estimate_text_tokens(json.dumps(payload, ensure_ascii=False, default=str))
    except TypeError:
        return estimate_text_tokens(str(payload))


def estimate_messages_tokens(messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None) -> int:
    total = 0
    for message in messages:
        total += 4
        total += estimate_payload_tokens(message)
    if tools:
        total += estimate_payload_tokens(tools)
    return total
