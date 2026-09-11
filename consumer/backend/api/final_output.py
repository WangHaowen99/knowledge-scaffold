"""User-facing final_output coercion helpers for protocol adapters."""

from __future__ import annotations

import json
from typing import Any


_TEXT_KEYS = ("text", "formatted_result", "output", "message", "content")


def extract_final_output_text(final_output: Any) -> str:
    if final_output is None:
        return ""
    if isinstance(final_output, str):
        return final_output
    if isinstance(final_output, dict):
        for key in _TEXT_KEYS:
            value = final_output.get(key)
            if isinstance(value, str) and value.strip():
                return value
        interaction = final_output.get("interaction")
        if isinstance(interaction, dict):
            prompt = interaction.get("prompt")
            if isinstance(prompt, str) and prompt.strip():
                return prompt
    return ""


def coerce_final_output_to_text(final_output: Any) -> str:
    text = extract_final_output_text(final_output)
    if text:
        return text
    if final_output in (None, ""):
        return ""
    if isinstance(final_output, (dict, list)):
        try:
            return json.dumps(final_output, ensure_ascii=False)
        except (TypeError, ValueError):
            return str(final_output)
    return str(final_output)
