"""Versioned canonical JSON and digest helpers for solver artifacts.

The digest boundary deliberately accepts only JSON-shaped values.  Domain
objects therefore serialize decimals and timestamps before reaching this
module; accepting Python floats here would make independently implemented
clients disagree about rounding and non-finite values.
"""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import math
import unicodedata
from typing import Any


DIGEST_PREFIX = "sha256:solver-coordination.v1:"


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise ValueError("float values are not allowed in canonical JSON")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("mapping key must be a string")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("mapping key collision after NFC normalization")
            normalized[normalized_key] = _normalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    raise ValueError(
        "unsupported value type for canonical JSON: {}".format(type(value).__name__)
    )


def canonical_json_bytes(payload: Any) -> bytes:
    normalized = _normalize(payload)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def sha256_digest(payload: Any) -> str:
    return DIGEST_PREFIX + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


def _normalize_json_schema(value: Any) -> Any:
    """Normalize a JSON-Schema document without relaxing artifact numeric rules."""

    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("schema numbers must be finite")
        return value
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError("schema mapping key must be a string")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ValueError("schema mapping key collision after NFC normalization")
            normalized[normalized_key] = _normalize_json_schema(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_normalize_json_schema(item) for item in value]
    raise ValueError(
        "unsupported value type for JSON schema: {}".format(type(value).__name__)
    )


def json_schema_digest(schema: Any) -> str:
    """Digest a JSON-Schema document, including finite JSON fractional bounds."""

    normalized = _normalize_json_schema(schema)
    encoded = json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    return DIGEST_PREFIX + hashlib.sha256(encoded).hexdigest()
