# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Strict Operation-schema binding for model-supplied solver input."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
import math
from typing import Any
import unicodedata

from .contracts import (
    MAX_DECIMAL_DIGITS,
    MAX_DECIMAL_EXPONENT,
    MIN_DECIMAL_EXPONENT,
    NormalizedInput,
    SolverContractError,
    _canonical_decimal_string,
    _freeze_array,
    _freeze_object,
    _thaw_json,
)


@dataclass(frozen=True)
class InputBindingResult:
    status: str
    normalized_input: NormalizedInput | None = None
    errors: tuple[Mapping[str, Any], ...] = ()
    awaiting_input: Mapping[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.status not in {"bound", "awaiting_input", "invalid_input"}:
            raise SolverContractError("input binding status is unsupported")
        if self.status == "bound" and not isinstance(
            self.normalized_input, NormalizedInput
        ):
            raise SolverContractError("bound input requires normalized_input")
        if self.status != "bound" and self.normalized_input is not None:
            raise SolverContractError("unbound input cannot contain normalized_input")
        object.__setattr__(self, "errors", _freeze_array(self.errors, "errors"))
        if self.awaiting_input is not None:
            object.__setattr__(
                self,
                "awaiting_input",
                _freeze_object(self.awaiting_input, "awaiting_input"),
            )

    @property
    def code(self) -> str | None:
        if self.status == "awaiting_input":
            return "awaiting_input"
        if self.status == "invalid_input":
            return "invalid_solver_input"
        return None

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"status": self.status}
        if self.code is not None:
            payload["code"] = self.code
        if self.normalized_input is not None:
            payload["normalized_input"] = self.normalized_input.to_dict()
        if self.errors:
            payload["errors"] = _thaw_json(self.errors)
        if self.awaiting_input is not None:
            payload["awaiting_input"] = _thaw_json(self.awaiting_input)
        return payload


BindingResult = InputBindingResult


class SolverInputBinder:
    """Validate the supported JSON-Schema subset and freeze business values."""

    def bind(
        self,
        *,
        operation_key: str,
        schema: Mapping[str, Any] | None = None,
        operation_schema: Mapping[str, Any] | None = None,
        values: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        raw_input: Mapping[str, Any] | Iterable[tuple[str, Any]] | None = None,
        source_evidence: Iterable[Any] = (),
        ambiguous_fields: Mapping[str, Iterable[Any]] | Iterable[str] = (),
        input_adapter: Any = None,
        capability: Any = None,
    ) -> InputBindingResult:
        selected_schema = schema if schema is not None else operation_schema
        if not isinstance(selected_schema, Mapping):
            raise SolverContractError("operation schema must be an object")
        self._validate_schema(selected_schema)
        selected_values = values if values is not None else raw_input
        normalized_values, duplicate_errors = self._coerce_values(selected_values or {})
        properties = selected_schema.get("properties") or {}
        required = tuple(selected_schema.get("required") or ())
        ambiguous = self._coerce_ambiguous(ambiguous_fields)

        missing = sorted(name for name in required if name not in normalized_values)
        if missing or ambiguous:
            errors = list(duplicate_errors)
            errors.extend(
                {
                    "field": name,
                    "code": "required",
                    "message": "required value is missing",
                }
                for name in missing
            )
            errors.extend(
                {
                    "field": name,
                    "code": "ambiguous",
                    "message": "value is ambiguous",
                }
                for name in sorted(ambiguous)
            )
            return InputBindingResult(
                status="awaiting_input",
                errors=tuple(errors),
                awaiting_input={
                    "missing": missing,
                    "ambiguous": {name: ambiguous[name] for name in sorted(ambiguous)},
                },
            )

        errors = list(duplicate_errors)
        for name in sorted(set(normalized_values) - set(properties)):
            errors.append(
                {
                    "field": name,
                    "code": "additionalProperties",
                    "message": "additionalProperties is false",
                }
            )

        bound_values: dict[str, Any] = {}
        for name, raw_value in normalized_values.items():
            property_schema = properties.get(name)
            if not isinstance(property_schema, Mapping):
                continue
            value, field_errors = self._validate_value(name, raw_value, property_schema)
            errors.extend(field_errors)
            if not field_errors:
                bound_values[name] = value

        if errors:
            nested_missing = sorted(
                str(error["field"])
                for error in errors
                if error.get("code") == "required"
            )
            if nested_missing:
                return InputBindingResult(
                    status="awaiting_input",
                    errors=tuple(errors),
                    awaiting_input={"missing": nested_missing, "ambiguous": {}},
                )
            return InputBindingResult(status="invalid_input", errors=tuple(errors))

        units, precision = self._collect_normalization(selected_schema)
        normalization = {
            "unit_conversions": [],
            "units": units,
            "precision": precision or "declared-by-schema",
        }
        if input_adapter is not None:
            if not callable(getattr(input_adapter, "adapt", None)):
                raise SolverContractError("input_adapter must provide adapt")
            try:
                adapted = input_adapter.adapt(
                    capability=capability,
                    operation_key=operation_key,
                    values=bound_values,
                    normalization=normalization,
                )
            except Exception as exc:
                from .profiles.input_adapter import InputAliasConflict

                if isinstance(exc, InputAliasConflict):
                    return InputBindingResult(
                        status="invalid_input",
                        errors=tuple(exc.errors),
                    )
                if isinstance(exc, SolverContractError):
                    return InputBindingResult(
                        status="invalid_input",
                        errors=(
                            {
                                "field": "values",
                                "code": "invalid_solver_input",
                                "message": str(exc),
                            },
                        ),
                    )
                raise
            if not isinstance(adapted, Mapping):
                raise SolverContractError("input_adapter returned invalid data")
            if set(adapted) != {"values", "normalization"}:
                raise SolverContractError("input_adapter returned invalid data")
            if not isinstance(adapted.get("values"), Mapping):
                raise SolverContractError("input_adapter returned invalid data")
            if not isinstance(adapted.get("normalization"), Mapping):
                raise SolverContractError("input_adapter returned invalid data")
            bound_values = dict(adapted["values"])
            normalization = dict(adapted["normalization"])
        try:
            normalized_input = NormalizedInput(
                operation_key=operation_key,
                values=bound_values,
                source_evidence=tuple(source_evidence),
                normalization=normalization,
            )
        except SolverContractError as exc:
            return InputBindingResult(
                status="invalid_input",
                errors=(
                    {
                        "field": "values",
                        "code": "forbidden_field",
                        "message": str(exc),
                    },
                ),
            )
        return InputBindingResult(status="bound", normalized_input=normalized_input)

    @staticmethod
    def _validate_schema(schema: Mapping[str, Any]) -> None:
        if schema.get("type") != "object":
            raise SolverContractError("operation schema type must be object")
        SolverInputBinder._validate_schema_node(schema, "$")

    @staticmethod
    def _validate_schema_node(schema: Mapping[str, Any], path: str) -> None:
        supported_keywords = {
            "$schema",
            "$id",
            "title",
            "description",
            "type",
            "properties",
            "required",
            "additionalProperties",
            "enum",
            "minimum",
            "maximum",
            "exclusiveMinimum",
            "exclusiveMaximum",
            "minLength",
            "maxLength",
            "minItems",
            "maxItems",
            "items",
            "unit",
            "precision",
        }
        unsupported = set(schema) - supported_keywords
        if unsupported:
            raise SolverContractError(
                "unsupported keyword at {}: {}".format(
                    path, ", ".join(sorted(str(item) for item in unsupported))
                )
            )
        declared_type = schema.get("type")
        declared_types = (
            tuple(declared_type)
            if isinstance(declared_type, (list, tuple))
            else (declared_type,)
        )
        supported_types = {
            "string",
            "integer",
            "number",
            "boolean",
            "object",
            "array",
            "null",
        }
        if not declared_type:
            raise SolverContractError("{} has unsupported type".format(path))
        if any(not isinstance(item, str) for item in declared_types):
            raise SolverContractError("{} has unsupported type".format(path))
        if len(set(declared_types)) != len(declared_types):
            raise SolverContractError("{} has unsupported type".format(path))
        if any(item not in supported_types for item in declared_types):
            raise SolverContractError("{} has unsupported type".format(path))

        if "object" in declared_types:
            properties = schema.get("properties")
            if not isinstance(properties, Mapping):
                raise SolverContractError("{} properties must be an object".format(path))
            if schema.get("additionalProperties") is not False:
                raise SolverContractError(
                    "{} additionalProperties must be false".format(path)
                )
            required = schema.get("required", ())
            if not isinstance(required, (list, tuple)) or any(
                not isinstance(name, str) or not name for name in required
            ):
                raise SolverContractError(
                    "{} required must be an array of names".format(path)
                )
            if len(set(required)) != len(tuple(required)):
                raise SolverContractError("{} required contains duplicates".format(path))
            unknown_required = set(required) - set(properties)
            if unknown_required:
                raise SolverContractError(
                    "{} required references unknown properties".format(path)
                )
            for name, property_schema in properties.items():
                if not isinstance(name, str) or not isinstance(property_schema, Mapping):
                    raise SolverContractError("{} property is invalid".format(path))
                SolverInputBinder._validate_schema_node(
                    property_schema, "{}.{}".format(path, name)
                )
        elif any(
            keyword in schema
            for keyword in ("properties", "required", "additionalProperties")
        ):
            raise SolverContractError(
                "{} uses object keywords for a non-object type".format(path)
            )

        if "array" in declared_types:
            items = schema.get("items")
            if not isinstance(items, Mapping):
                raise SolverContractError("{} items must be an object".format(path))
            SolverInputBinder._validate_schema_node(items, "{}[]".format(path))
        elif "items" in schema:
            raise SolverContractError(
                "{} uses items for a non-array type".format(path)
            )

        for keyword in ("minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"):
            if keyword in schema:
                value = schema[keyword]
                if isinstance(value, bool) or not isinstance(
                    value, (int, float, Decimal)
                ):
                    raise SolverContractError(
                        "{} {} must be numeric".format(path, keyword)
                    )
                SolverInputBinder._decimal(value)
        for keyword in ("minLength", "maxLength", "minItems", "maxItems"):
            if keyword not in schema:
                continue
            value = schema[keyword]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise SolverContractError(
                    "{} {} must be a non-negative integer".format(path, keyword)
                )
        if "enum" in schema and (
            not isinstance(schema["enum"], (list, tuple)) or not schema["enum"]
        ):
            raise SolverContractError("{} enum must be a non-empty array".format(path))
        if "unit" in schema and not isinstance(schema["unit"], str):
            raise SolverContractError("{} unit must be a string".format(path))
        if "precision" in schema and not isinstance(schema["precision"], (str, int)):
            raise SolverContractError("{} precision is invalid".format(path))

    @staticmethod
    def _collect_normalization(
        schema: Mapping[str, Any], prefix: str = ""
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        units: dict[str, Any] = {}
        precision: dict[str, Any] = {}

        def visit(node: Mapping[str, Any], path: str) -> None:
            if "unit" in node and path:
                units[path] = node["unit"]
            if "precision" in node and path:
                precision[path] = node["precision"]
            properties = node.get("properties")
            if isinstance(properties, Mapping):
                for name, child in properties.items():
                    if isinstance(child, Mapping):
                        visit(child, "{}.{}".format(path, name) if path else str(name))
            items = node.get("items")
            if isinstance(items, Mapping):
                visit(items, "{}[]".format(path))

        visit(schema, prefix)
        return units, precision

    @staticmethod
    def _coerce_values(
        values: Mapping[str, Any] | Iterable[tuple[str, Any]],
    ) -> tuple[dict[str, Any], list[dict[str, str]]]:
        if isinstance(values, Mapping):
            return dict(values), []
        try:
            pairs = tuple(values)
        except TypeError as exc:
            raise SolverContractError("solver input values must be an object") from exc
        normalized: dict[str, Any] = {}
        errors: list[dict[str, str]] = []
        for pair in pairs:
            if not isinstance(pair, (list, tuple)) or len(pair) != 2:
                raise SolverContractError("solver input pair is invalid")
            name, value = pair
            if not isinstance(name, str):
                raise SolverContractError("solver input field name must be a string")
            if name in normalized:
                errors.append(
                    {
                        "field": name,
                        "code": "duplicate",
                        "message": "input field is duplicated",
                    }
                )
            normalized[name] = value
        return normalized, errors

    @staticmethod
    def _coerce_ambiguous(
        ambiguous_fields: Mapping[str, Iterable[Any]] | Iterable[str],
    ) -> dict[str, list[Any]]:
        if isinstance(ambiguous_fields, Mapping):
            return {
                str(name): list(candidates)
                for name, candidates in ambiguous_fields.items()
            }
        return {str(name): [] for name in ambiguous_fields}

    def _validate_value(
        self, name: str, raw_value: Any, schema: Mapping[str, Any]
    ) -> tuple[Any, list[dict[str, str]]]:
        errors: list[dict[str, str]] = []
        declared = schema.get("type")
        declared_types = tuple(declared) if isinstance(declared, (list, tuple)) else (declared,)
        matched_type = next(
            (item for item in declared_types if self._matches_type(raw_value, item)), None
        )
        if matched_type is None:
            return raw_value, [
                {
                    "field": name,
                    "code": "type",
                    "message": "value does not match declared type",
                }
            ]

        if matched_type == "string":
            raw_value = unicodedata.normalize("NFC", raw_value)

        if "enum" in schema:
            enum_values = schema["enum"]
            normalized_enum_values = tuple(
                unicodedata.normalize("NFC", value)
                if isinstance(value, str)
                else value
                for value in enum_values
            )
            if raw_value not in normalized_enum_values:
                errors.append(
                    {"field": name, "code": "enum", "message": "value is outside enum"}
                )

        if matched_type in {"integer", "number"}:
            try:
                numeric = self._decimal(raw_value)
            except SolverContractError as exc:
                errors.append(
                    {"field": name, "code": "precision", "message": str(exc)}
                )
                return raw_value, errors
            for keyword, comparison in (
                ("minimum", lambda value, limit: value < limit),
                ("maximum", lambda value, limit: value > limit),
                ("exclusiveMinimum", lambda value, limit: value <= limit),
                ("exclusiveMaximum", lambda value, limit: value >= limit),
            ):
                if keyword in schema and comparison(numeric, self._decimal(schema[keyword])):
                    errors.append(
                        {
                            "field": name,
                            "code": keyword,
                            "message": "value violates {}".format(keyword),
                        }
                    )
            if matched_type == "number" and not errors:
                try:
                    raw_value = self._decimal_string(numeric)
                except SolverContractError as exc:
                    errors.append(
                        {"field": name, "code": "precision", "message": str(exc)}
                    )

        if matched_type == "string":
            if "minLength" in schema and len(raw_value) < schema["minLength"]:
                errors.append(
                    {"field": name, "code": "minLength", "message": "string is too short"}
                )
            if "maxLength" in schema and len(raw_value) > schema["maxLength"]:
                errors.append(
                    {"field": name, "code": "maxLength", "message": "string is too long"}
                )

        if matched_type == "array":
            if "minItems" in schema and len(raw_value) < schema["minItems"]:
                errors.append(
                    {"field": name, "code": "minItems", "message": "array is too short"}
                )
            if "maxItems" in schema and len(raw_value) > schema["maxItems"]:
                errors.append(
                    {"field": name, "code": "maxItems", "message": "array is too long"}
                )
            normalized_items = []
            item_schema = schema.get("items")
            for index, item in enumerate(raw_value):
                normalized_item, item_errors = self._validate_value(
                    "{}[{}]".format(name, index), item, item_schema
                )
                errors.extend(item_errors)
                normalized_items.append(normalized_item)
            raw_value = normalized_items

        if matched_type == "object":
            properties = schema.get("properties") or {}
            required = tuple(schema.get("required") or ())
            normalized_object: dict[str, Any] = {}
            for missing_name in sorted(set(required) - set(raw_value)):
                errors.append(
                    {
                        "field": "{}.{}".format(name, missing_name),
                        "code": "required",
                        "message": "required value is missing",
                    }
                )
            for extra_name in sorted(set(raw_value) - set(properties)):
                errors.append(
                    {
                        "field": "{}.{}".format(name, extra_name),
                        "code": "additionalProperties",
                        "message": "additionalProperties is false",
                    }
                )
            for child_name, child_value in raw_value.items():
                child_schema = properties.get(child_name)
                if not isinstance(child_schema, Mapping):
                    continue
                normalized_child, child_errors = self._validate_value(
                    "{}.{}".format(name, child_name), child_value, child_schema
                )
                errors.extend(child_errors)
                normalized_object[child_name] = normalized_child
            raw_value = normalized_object
        return raw_value, errors

    @staticmethod
    def _matches_type(value: Any, declared_type: str) -> bool:
        if declared_type == "null":
            return value is None
        if declared_type == "boolean":
            return isinstance(value, bool)
        if declared_type == "integer":
            return isinstance(value, int) and not isinstance(value, bool)
        if declared_type == "number":
            if isinstance(value, bool):
                return False
            if isinstance(value, (int, Decimal)):
                return True
            if isinstance(value, float):
                return math.isfinite(value)
            return False
        if declared_type == "string":
            return isinstance(value, str)
        if declared_type == "object":
            return isinstance(value, Mapping)
        if declared_type == "array":
            return isinstance(value, (list, tuple))
        return False

    @staticmethod
    def _decimal(value: Any) -> Decimal:
        try:
            rendered = str(value)
            numeric = Decimal(rendered)
        except (InvalidOperation, ValueError, OverflowError) as exc:
            raise SolverContractError("numeric schema value is invalid") from exc
        if not numeric.is_finite():
            raise SolverContractError("numeric schema value must be finite")
        sign, digits, exponent = numeric.as_tuple()
        del sign
        if (
            len(digits) > MAX_DECIMAL_DIGITS
            or exponent < MIN_DECIMAL_EXPONENT
            or exponent > MAX_DECIMAL_EXPONENT
        ):
            raise SolverContractError("numeric value exceeds precision or exponent limits")
        return numeric

    @staticmethod
    def _decimal_string(value: Decimal) -> str:
        return _canonical_decimal_string(value)


def bind_operation_input(**kwargs: Any) -> InputBindingResult:
    return SolverInputBinder().bind(**kwargs)
