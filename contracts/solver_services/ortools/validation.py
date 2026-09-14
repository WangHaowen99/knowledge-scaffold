# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Strict, Consumer-independent helpers for ``ortools.solver.v1``."""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from pathlib import Path
import re
from typing import Any
import unicodedata

from jsonschema import Draft202012Validator, FormatChecker


ROOT = Path(__file__).resolve().parent
SCHEMA_ROOT = ROOT / "schemas"
PLATFORM_FAMILY_SCHEMA = (
    ROOT / "problem-families" / "binary_ordered_route.v1.schema.json"
)
_DOCUMENTS = {
    name: SCHEMA_ROOT / f"{name}.schema.json"
    for name in ("capabilities", "submit-request", "job", "result", "error")
}
_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-"
    r"(?P<trace_flags>[0-9a-f]{2})$"
)


class ContractValidationError(ValueError):
    """Raised when a service document cannot be parsed or validated."""


def validate_traceparent(value: Any) -> str:
    if not isinstance(value, str):
        raise ContractValidationError("traceparent is invalid")
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None:
        raise ContractValidationError("traceparent is invalid")
    if (
        match.group("version") != "00"
        or match.group("trace_id") == "0" * 32
        or match.group("parent_id") == "0" * 16
    ):
        raise ContractValidationError("traceparent is invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_surrogates(value: Any) -> Any:
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ContractValidationError("JSON contains an invalid Unicode surrogate")
    elif isinstance(value, Mapping):
        for key, item in value.items():
            _reject_surrogates(key)
            _reject_surrogates(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _reject_surrogates(item)
    return value


def parse_json_bytes(payload: bytes | str) -> Any:
    if isinstance(payload, bytes):
        try:
            text = payload.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ContractValidationError("JSON must be UTF-8") from exc
    elif isinstance(payload, str):
        text = payload
    else:
        raise ContractValidationError("JSON payload must be bytes or text")
    if text.startswith("\ufeff"):
        raise ContractValidationError("JSON must not contain a BOM")
    try:
        parsed = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=lambda value: (_ for _ in ()).throw(
                ContractValidationError(f"non-finite JSON number: {value}")
            ),
        )
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("invalid JSON") from exc
    return _reject_surrogates(parsed)


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ContractValidationError("non-finite JSON number")
        raise ContractValidationError("floating-point values are not canonical")
    if isinstance(value, str):
        _reject_surrogates(value)
        return unicodedata.normalize("NFC", value)
    if isinstance(value, Mapping):
        normalized: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ContractValidationError("JSON object keys must be strings")
            normalized_key = unicodedata.normalize("NFC", key)
            if normalized_key in normalized:
                raise ContractValidationError("duplicate key after NFC normalization")
            normalized[normalized_key] = _canonicalize(item)
        return normalized
    if isinstance(value, (list, tuple)):
        return [_canonicalize(item) for item in value]
    raise ContractValidationError("unsupported JSON value")


def canonical_json_bytes(value: Any) -> bytes:
    return json.dumps(
        _canonicalize(value),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def load_schema(name: str) -> dict[str, Any]:
    path = _DOCUMENTS.get(name)
    if path is None:
        raise ContractValidationError(f"unknown service document: {name}")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractValidationError("service schema cannot be loaded") from exc


def validate_document(name: str, document: Any) -> Any:
    if name not in _DOCUMENTS:
        raise ContractValidationError(f"unknown service document: {name}")
    try:
        Draft202012Validator(
            load_schema(name), format_checker=FormatChecker()
        ).validate(document)
        if name == "submit-request":
            problem = document["problem"]
            platform_problem = {
                "schema_version": "solver.problem.v1",
                "problem_family": problem["type"],
                "family_version": problem["version"],
                "payload": problem["data"],
            }
            family_schema = json.loads(
                PLATFORM_FAMILY_SCHEMA.read_text(encoding="utf-8")
            )
            Draft202012Validator(family_schema).validate(platform_problem)
    except Exception as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError(
            f"document does not satisfy {name} schema"
        ) from exc
    return document


__all__ = [
    "ContractValidationError",
    "canonical_json_bytes",
    "load_schema",
    "parse_json_bytes",
    "validate_document",
    "validate_traceparent",
]
