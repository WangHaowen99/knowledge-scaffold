# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Local JSON Schema validation for untrusted solver assignments."""

from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from decimal import Decimal
import math
from types import MappingProxyType
from typing import Any
from urllib.parse import urlsplit

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from ontology.ontologies import validate_no_runtime_binding_data

from .errors import SolverResultRejectedError


_INVALID_SCHEMA_DETAIL = "locked solver result schema is invalid"
_INVALID_ASSIGNMENT_DETAIL = (
    "solver assignment does not match the locked result schema"
)
_REFERENCE_KEYWORDS = frozenset({"$ref", "$dynamicRef"})


def _clone_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {key: _clone_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clone_json(item) for item in value]
    return deepcopy(value)


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _assert_finite_numbers(value: Any) -> None:
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite number")
    if isinstance(value, Decimal) and not value.is_finite():
        raise ValueError("non-finite number")
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_finite_numbers(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_finite_numbers(item)


def _assert_local_schema(value: Any) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in _REFERENCE_KEYWORDS:
                raise ValueError("schema references are not allowed")
            if key == "$id" and isinstance(item, str):
                parsed = urlsplit(item)
                if parsed.scheme or parsed.netloc or item.startswith("//"):
                    raise ValueError("external schema identifiers are not allowed")
            _assert_local_schema(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _assert_local_schema(item)


class SolverAssignmentSchemaValidator:
    """Validate solver assignments without using Operation input semantics."""

    __slots__ = ("_schema", "_validator")

    def __init__(self, *, schema: Mapping[str, Any]) -> None:
        try:
            if not isinstance(schema, Mapping):
                raise TypeError("schema must be an object")
            owned_schema = _clone_json(schema)
            _assert_finite_numbers(owned_schema)
            _assert_local_schema(owned_schema)
            validate_no_runtime_binding_data(owned_schema)
            Draft202012Validator.check_schema(owned_schema)
            validator = Draft202012Validator(owned_schema)
        except Exception as exc:
            raise SolverResultRejectedError(_INVALID_SCHEMA_DETAIL) from exc
        self._schema = _freeze(_clone_json(owned_schema))
        self._validator = validator

    def validate(self, value: object) -> dict[str, Any]:
        try:
            candidate = _clone_json(value)
            _assert_finite_numbers(candidate)
            self._validator.validate(candidate)
            if not isinstance(candidate, dict):
                raise ValidationError("assignment must be an object")
        except Exception as exc:
            raise SolverResultRejectedError(_INVALID_ASSIGNMENT_DETAIL) from exc
        return _clone_json(candidate)
