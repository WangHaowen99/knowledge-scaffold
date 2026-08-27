"""Standalone validation helpers for the solver.job.v1 contract artifact.

This module intentionally has no dependency on the Consumer application.  It is
usable by a Connector test suite or a platform client that vendors only the
public contract directory.
"""

from __future__ import annotations

from collections.abc import Mapping
import json
import math
from pathlib import Path
import unicodedata
import re
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker, RefResolver


ROOT = Path(__file__).resolve().parent
SCHEMA_ROOT = ROOT / "schemas"
FAMILY_SCHEMA = ROOT / "problem-families" / "binary_ordered_route.v1.schema.json"

_DOCUMENTS = {
    "capabilities": SCHEMA_ROOT / "capabilities.schema.json",
    "submit-request": SCHEMA_ROOT / "submit-request.schema.json",
    "job": SCHEMA_ROOT / "job.schema.json",
    "result": SCHEMA_ROOT / "result.schema.json",
    "error": SCHEMA_ROOT / "error.schema.json",
    "candidate-selection": FAMILY_SCHEMA,
}


class ContractValidationError(ValueError):
    """Raised when a contract document cannot be parsed or validated."""


_TRACEPARENT_RE = re.compile(
    r"^(?P<version>[0-9a-f]{2})-"
    r"(?P<trace_id>[0-9a-f]{32})-"
    r"(?P<parent_id>[0-9a-f]{16})-"
    r"(?P<trace_flags>[0-9a-f]{2})$"
)


def validate_traceparent(value: Any) -> str:
    """Validate the canonical W3C traceparent carried by a Connector request."""

    if not isinstance(value, str):
        raise ContractValidationError("traceparent is invalid")
    match = _TRACEPARENT_RE.fullmatch(value)
    if match is None or match.group("version") != "00":
        raise ContractValidationError("traceparent is invalid")
    if match.group("trace_id") == "0" * 32 or match.group("parent_id") == "0" * 16:
        raise ContractValidationError("traceparent is invalid")
    return value


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ContractValidationError("duplicate JSON object key")
        result[key] = value
    return result


def _reject_nonfinite(value: Any) -> Any:
    if isinstance(value, float) and not math.isfinite(value):
        raise ContractValidationError("non-finite JSON number")
    return value


def _reject_unicode_surrogates(value: Any) -> Any:
    """Reject lone UTF-16 surrogate code points at the JSON boundary.

    Python's JSON decoder can represent ``"\\udead"`` as a ``str`` even
    though it cannot be emitted as valid UTF-8 JSON.  Letting such a value
    reach canonicalization would turn an untrusted request into a 500
    ``UnicodeEncodeError``.  Walk both keys and values so the contract layer
    remains the single fail-closed validation boundary.
    """

    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ContractValidationError("JSON contains an invalid Unicode surrogate")
        return value
    if isinstance(value, Mapping):
        for key, item in value.items():
            _reject_unicode_surrogates(key)
            _reject_unicode_surrogates(item)
        return value
    if isinstance(value, (list, tuple)):
        for item in value:
            _reject_unicode_surrogates(item)
    return value


def parse_json_bytes(payload: bytes | str) -> Any:
    """Parse strict UTF-8 JSON with duplicate-key and NaN rejection."""

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
                ContractValidationError("non-finite JSON number: {}".format(value))
            ),
        )
        return _reject_unicode_surrogates(parsed)
    except (TypeError, ValueError) as exc:
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("invalid JSON") from exc


def _canonicalize(value: Any) -> Any:
    if value is None or isinstance(value, (bool, int)):
        return value
    if isinstance(value, float):
        raise ContractValidationError("floating-point values are not canonical")
    if isinstance(value, str):
        if any(0xD800 <= ord(char) <= 0xDFFF for char in value):
            raise ContractValidationError("JSON contains an invalid Unicode surrogate")
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
    normalized = _canonicalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")


def load_schema(name: str) -> dict[str, Any]:
    path = _DOCUMENTS.get(name)
    if path is None:
        raise ContractValidationError("unknown contract document: {}".format(name))
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ContractValidationError("contract schema cannot be loaded") from exc


def _validator(schema: Mapping[str, Any]) -> Draft202012Validator:
    family = load_schema("candidate-selection")
    store = {
        str(family.get("$id")): family,
        FAMILY_SCHEMA.as_uri(): family,
    }
    resolver = RefResolver.from_schema(schema, store=store)
    return Draft202012Validator(schema, resolver=resolver, format_checker=FormatChecker())


def validate_document(name: str, document: Any) -> Any:
    """Validate and return a document using the named strict schema."""

    if name not in _DOCUMENTS:
        raise ContractValidationError("unknown contract document: {}".format(name))
    try:
        _validator(load_schema(name)).validate(document)
    except Exception as exc:
        # Keep the public utility independent of jsonschema's exception classes;
        # callers only need a stable contract-validation failure.
        if isinstance(exc, ContractValidationError):
            raise
        raise ContractValidationError("document does not satisfy {} schema".format(name)) from exc
    return document


__all__ = [
    "ContractValidationError",
    "canonical_json_bytes",
    "load_schema",
    "parse_json_bytes",
    "validate_traceparent",
    "validate_document",
]
