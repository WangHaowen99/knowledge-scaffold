# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared redaction helpers for externally observable runtime data."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any


REDACTED = "[REDACTED]"
_JSON_TEXT_LIMIT = 128 * 1024
_SENSITIVE_KEY_EXACT = {
    "authorization",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "cookie",
    "credential",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
    "privatekey",
    "secretkey",
    "accesskey",
}
_SENSITIVE_KEY_SUFFIXES = (
    "authorization",
    "password",
    "passwd",
    "pwd",
    "secret",
    "token",
    "cookie",
    "credential",
    "apikey",
    "privatekey",
    "secretkey",
    "accesskey",
)
_ASSIGNMENT_RE = re.compile(
    r"(?P<key_quote>[\"']?)(?P<key>[A-Za-z][A-Za-z0-9_.-]{0,127})(?P=key_quote)"
    r"(?P<spacing>\s*)(?P<separator>[:=])(?P<value_spacing>\s*)"
    r"(?P<value>"
    r"(?:(?:Bearer|Basic)\s+[A-Za-z0-9._~+/=-]+)"
    r"|\[(?:REDACTED|REDACTED_JWT)\]"
    r"|\"(?:\\.|[^\"\\])*\""
    r"|'(?:\\.|[^'\\])*'"
    r"|[^\s,;&}\]]+"
    r")",
    re.IGNORECASE,
)
_BEARER_RE = re.compile(r"(?i)\b(?:bearer|basic)\s+[A-Za-z0-9._~+/=-]+")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}(?:\.[A-Za-z0-9_-]+)?\b")
_URI_CREDENTIAL_RE = re.compile(r"(://)[^/@\s:]+:[^/@\s]+@")
_PEM_PRIVATE_KEY_RE = re.compile(
    r"-----BEGIN (?P<label>(?:[A-Z0-9]+ )*PRIVATE KEY(?: BLOCK)?)-----"
    r".*?"
    r"-----END (?P=label)-----",
    re.DOTALL,
)


def is_sensitive_key(value: Any) -> bool:
    """Return whether a structured field name conventionally contains a secret."""
    normalized = re.sub(r"[^a-z0-9]", "", str(value or "").lower())
    if not normalized:
        return False
    if normalized in _SENSITIVE_KEY_EXACT or normalized.startswith("authorization"):
        return True
    return any(normalized.endswith(suffix) for suffix in _SENSITIVE_KEY_SUFFIXES)


def redact_sensitive_value(
    value: Any,
    *,
    max_collection_items: int | None = None,
) -> Any:
    """Recursively redact secret-bearing fields and strings in JSON-like data."""
    if isinstance(value, Mapping):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            key_text = str(key)
            if is_sensitive_key(key_text):
                redacted[key_text] = REDACTED
            else:
                redacted[key_text] = redact_sensitive_value(
                    item,
                    max_collection_items=max_collection_items,
                )
        return redacted
    if isinstance(value, (list, tuple)):
        items = value
        if max_collection_items is not None:
            items = value[:max(0, int(max_collection_items))]
        return [
            redact_sensitive_value(item, max_collection_items=max_collection_items)
            for item in items
        ]
    if isinstance(value, str):
        return redact_sensitive_text(
            value,
            max_collection_items=max_collection_items,
        )
    return value


def redact_sensitive_text(
    value: Any,
    *,
    max_json_chars: int = _JSON_TEXT_LIMIT,
    max_collection_items: int | None = None,
) -> str:
    """Redact structured JSON text and common inline credential formats."""
    if value is None:
        return ""
    if isinstance(value, (Mapping, list, tuple)):
        return json.dumps(
            redact_sensitive_value(
                value,
                max_collection_items=max_collection_items,
            ),
            ensure_ascii=False,
            separators=(",", ":"),
        )

    text = str(value)
    stripped = text.strip()
    if (
        stripped
        and len(stripped) <= max(0, int(max_json_chars))
        and stripped[0] in "[{"
    ):
        try:
            parsed = json.loads(stripped)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (Mapping, list)):
            text = json.dumps(
                redact_sensitive_value(
                    parsed,
                    max_collection_items=max_collection_items,
                ),
                ensure_ascii=False,
                separators=(",", ":"),
            )

    text = _PEM_PRIVATE_KEY_RE.sub(REDACTED, text)
    text = _ASSIGNMENT_RE.sub(_redact_assignment, text)
    text = _BEARER_RE.sub(REDACTED, text)
    text = _JWT_RE.sub("[REDACTED_JWT]", text)
    return _URI_CREDENTIAL_RE.sub(r"\1[REDACTED]@", text)


def _redact_assignment(match: re.Match[str]) -> str:
    if not is_sensitive_key(match.group("key")):
        return match.group(0)
    value = match.group("value")
    if value in {REDACTED, "[REDACTED_JWT]"}:
        return match.group(0)
    replacement = REDACTED
    if len(value) >= 2 and value[0] in {"\"", "'"} and value[-1] == value[0]:
        replacement = "{}{}{}".format(value[0], REDACTED, value[0])
    return "{}{}{}{}{}{}".format(
        match.group("key_quote"),
        match.group("key"),
        match.group("key_quote"),
        match.group("spacing"),
        match.group("separator"),
        match.group("value_spacing") + replacement,
    )
