# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""JSON parsing helpers."""

from __future__ import annotations

import json
import re
from typing import Any, Optional


def _extract_json_from_content(content: str) -> str:
    """Extract JSON from LLM response content, handling code fences."""
    stripped = content.strip()
    if not stripped:
        return stripped

    if "```" in stripped:
        match = re.search(r"```(?:\w+)?\n?(.*?)```", stripped, re.DOTALL)
        if match:
            stripped = match.group(1).strip()

    if stripped and stripped[0] not in "{[":
        start = stripped.find("{")
        end = stripped.rfind("}")
        if start != -1 and end != -1 and end > start:
            stripped = stripped[start : end + 1]
    return stripped


def _loads_json_loose(content: Any) -> Optional[Any]:
    """
    Parse JSON with recovery for common LLM/provider corruption patterns.

    Recovery strategy:
    1. Parse as-is.
    2. Parse JSON extracted from fenced or mixed content.
    3. Scan from the end and decode the last valid JSON object/array substring.
       This repairs duplicated streamed tool arguments such as
       `{"a":1{"a":1}` by recovering the final valid object.
    """
    if isinstance(content, (dict, list)):
        return content
    if content is None:
        return None

    text = str(content).strip()
    if not text:
        return None

    for candidate in (text, _extract_json_from_content(text)):
        try:
            return json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue

    decoder = json.JSONDecoder()
    for index in range(len(text) - 1, -1, -1):
        if text[index] not in "{[":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, (dict, list)):
            return parsed

    return None


def _loads_json_object_loose(content: Any) -> Optional[dict[str, Any]]:
    """Parse JSON content and return a dict when recovery succeeds."""
    parsed = _loads_json_loose(content)
    if isinstance(parsed, dict):
        return parsed
    return None
