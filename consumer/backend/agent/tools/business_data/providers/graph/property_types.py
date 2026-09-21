# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Physical graph-property type resolution and filter operand conversion.

The query agent communicates through JSON, but graph providers compare values
using physical property types.  This module keeps that boundary explicit:
schema metadata is resolved per request and only filter operands are converted.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
import math
import re
from typing import Any
from uuid import UUID

from utils.logging import get_logger


logger = get_logger("ontology_search.graph.property_types")


UNKNOWN = "UNKNOWN"
ANY = "ANY"
NULL = "NULL"
BOOLEAN = "BOOLEAN"
INTEGER = "INTEGER"
FLOAT = "FLOAT"
DECIMAL = "DECIMAL"
STRING = "STRING"
UUID_TYPE = "UUID"
DATE = "DATE"
LOCAL_TIME = "LOCAL_TIME"
ZONED_TIME = "ZONED_TIME"
LOCAL_DATETIME = "LOCAL_DATETIME"
ZONED_DATETIME = "ZONED_DATETIME"
DURATION = "DURATION"
POINT = "POINT"
LIST = "LIST"
MAP = "MAP"
VECTOR = "VECTOR"
BYTES = "BYTES"
UNION = "UNION"

_INT_MIN = -(2**63)
_INT_MAX = 2**63 - 1
_JS_SAFE_INT_MAX = 2**53 - 1
_LEXICAL_TEXT_KINDS = {STRING, UUID_TYPE, BYTES}
_TEXT_OPERATOR_KINDS = {STRING, UUID_TYPE}
_ORDERABLE_KINDS = {
    INTEGER, FLOAT, DECIMAL, STRING, UUID_TYPE, DATE, LOCAL_TIME,
    ZONED_TIME, LOCAL_DATETIME, ZONED_DATETIME, DURATION,
}
_OPERATORS = {
    "eq", "ne", "gt", "gte", "lt", "lte", "contains", "starts_with",
    "ends_with", "contains_item", "in", "not_in", "is_null", "or", "and",
}
_OP_ALIASES = {
    "startswith": "starts_with",
    "starts-with": "starts_with",
    "endswith": "ends_with",
    "ends-with": "ends_with",
    "notin": "not_in",
    "not-in": "not_in",
    "nin": "not_in",
}
_COMPARE_RE = re.compile(r"^(>=|<=|>|<|=)\s*(.+)$")
_CONTAINS_RE = re.compile(r"^CONTAINS\s+(['\"])(.*?)\1$", re.IGNORECASE)
_START_END_RE = re.compile(
    r"^(STARTS\s+WITH|ENDS\s+WITH)\s+(['\"])(.*?)\2$",
    re.IGNORECASE,
)
_INTEGER_RE = re.compile(r"^[+-]?\d+$")
_PATTERN_CONTROL_KEYS = {
    "label", "type", "edge_type", "var", "direction", "return_properties",
    "entity_type_id", "entity_instance_id", "entity_name", "entity_id",
    "relation_type", "relation_type_id", "relation_id", "relation_name",
    "physical_relation_type",
    "props", "filters",
}


class GraphFilterTypeError(ValueError):
    """Raised when an operand cannot safely match its physical type."""


@dataclass(frozen=True)
class GraphPropertyType:
    kind: str
    item_type: "GraphPropertyType | None" = None
    alternatives: tuple["GraphPropertyType", ...] = ()
    raw: str = ""
    integer_bounds: tuple[int, int] | None = None

    @property
    def known(self) -> bool:
        return self.kind not in {UNKNOWN, ANY, NULL} and bool(
            self.kind != UNION or self.alternatives
        )

    @property
    def ambiguous(self) -> bool:
        return self.kind == UNION or len(self.alternatives) > 1


@dataclass(frozen=True)
class TypeNormalization:
    filter_dict: dict[str, Any]
    converted_fields: tuple[str, ...] = ()
    resolved_fields: tuple[str, ...] = ()


def normalize_operator(raw_operator: Any) -> str:
    value = str(raw_operator or "").strip().lower()
    if value.startswith("$"):
        value = value[1:]
    return _OP_ALIASES.get(value, value)


def parse_graph_property_type(raw_type: Any) -> GraphPropertyType:
    """Parse Neo4j valueType, HugeGraph types, and common type aliases."""

    raw = str(raw_type or "").strip()
    if not raw:
        return GraphPropertyType(UNKNOWN, raw=raw)
    text = re.sub(r"\s+NOT\s+NULL\b", "", raw, flags=re.IGNORECASE).strip()
    alternatives = _split_top_level(text, "|")
    if len(alternatives) > 1:
        parsed = [
            item for item in (parse_graph_property_type(part) for part in alternatives)
            if item.kind != NULL
        ]
        if not parsed:
            return GraphPropertyType(UNKNOWN, raw=raw)
        merged = parsed[0]
        for item in parsed[1:]:
            merged = _union_types(merged, item)
        return merged

    list_match = re.match(r"^(?:ARRAY|LIST)\s*<(.+)>$", text, re.IGNORECASE)
    if list_match:
        return GraphPropertyType(
            LIST,
            item_type=parse_graph_property_type(list_match.group(1)),
            raw=raw,
        )
    if text.endswith("[]"):
        return GraphPropertyType(
            LIST,
            item_type=parse_graph_property_type(text[:-2]),
            raw=raw,
        )
    vector_match = re.match(r"^VECTOR(?:\s*<(.+)>)?$", text, re.IGNORECASE)
    if vector_match:
        item = vector_match.group(1)
        return GraphPropertyType(
            VECTOR,
            item_type=parse_graph_property_type(item) if item else GraphPropertyType(FLOAT, raw="FLOAT"),
            raw=raw,
        )

    normalized = re.sub(r"[^A-Z0-9]+", "_", text.upper()).strip("_")
    aliases = {
        "BOOL": BOOLEAN,
        "BOOLEAN": BOOLEAN,
        "INT": INTEGER,
        "INTEGER": INTEGER,
        "LONG": INTEGER,
        "BIGINT": INTEGER,
        "SHORT": INTEGER,
        "SMALLINT": INTEGER,
        "BYTE": INTEGER,
        "TINYINT": INTEGER,
        "INT32": INTEGER,
        "INT64": INTEGER,
        "INTEGER32": INTEGER,
        "INTEGER64": INTEGER,
        "FLOAT": FLOAT,
        "DOUBLE": FLOAT,
        "REAL": FLOAT,
        "FLOAT32": FLOAT,
        "FLOAT64": FLOAT,
        "NUMBER": DECIMAL,
        "NUMERIC": DECIMAL,
        "DECIMAL": DECIMAL,
        "BIGDECIMAL": DECIMAL,
        "STR": STRING,
        "TEXT": STRING,
        "STRING": STRING,
        "VARCHAR": STRING,
        "CHAR": STRING,
        "UUID": UUID_TYPE,
        "DATE": DATE,
        "TIME": ZONED_TIME,
        "LOCAL_TIME": LOCAL_TIME,
        "ZONED_TIME": ZONED_TIME,
        "DATETIME": ZONED_DATETIME,
        "LOCAL_DATETIME": LOCAL_DATETIME,
        "ZONED_DATETIME": ZONED_DATETIME,
        "DURATION": DURATION,
        "POINT": POINT,
        "MAP": MAP,
        "OBJECT": MAP,
        "BYTES": BYTES,
        "BYTE_ARRAY": BYTES,
        "BYTEARRAY": BYTES,
        "BINARY": BYTES,
        "BLOB": BYTES,
        "VECTOR": VECTOR,
        "LIST": LIST,
        "NULL": NULL,
        "ANY": ANY,
        "UNKNOWN": UNKNOWN,
    }
    kind = aliases.get(normalized, UNKNOWN)
    bounds = None
    if kind == INTEGER:
        if normalized in {"BYTE", "TINYINT"}:
            bounds = (-(2**7), 2**7 - 1)
        elif normalized in {"SHORT", "SMALLINT"}:
            bounds = (-(2**15), 2**15 - 1)
        elif normalized in {"INT", "INT32", "INTEGER32"}:
            bounds = (-(2**31), 2**31 - 1)
        elif normalized in {"INT64", "INTEGER64", "LONG", "BIGINT", "INTEGER"}:
            bounds = (_INT_MIN, _INT_MAX)
    return GraphPropertyType(kind, raw=raw, integer_bounds=bounds)


def normalize_filter_dict(
    filter_dict: Mapping[str, Any] | None,
    property_types: Mapping[str, GraphPropertyType | Any] | None,
    *,
    provider: str = "",
) -> TypeNormalization:
    """Convert filter operands using resolved physical types.

    Unknown fields retain their original values.  This preserves the old
    behavior when a provider cannot expose schema metadata.
    """

    if not filter_dict:
        return TypeNormalization(filter_dict={})
    if not isinstance(filter_dict, Mapping):
        raise GraphFilterTypeError("filter_dict 必须是对象类型")
    normalized: dict[str, Any] = {}
    converted: list[str] = []
    resolved: list[str] = []
    type_map = property_types or {}
    for raw_prop, expression in filter_dict.items():
        prop = str(raw_prop or "").strip()
        if not prop:
            raise GraphFilterTypeError("filter_dict 包含空字段名")
        spec = _coerce_type_spec(type_map.get(prop))
        if spec is None or not spec.known:
            normalized[prop] = expression
            continue
        resolved.append(prop)
        value, changed = _normalize_expression(
            expression,
            spec,
            field_name="filter_dict.{}".format(prop),
            provider=provider,
        )
        normalized[prop] = value
        if changed:
            converted.append(prop)
    return TypeNormalization(
        filter_dict=normalized,
        converted_fields=tuple(dict.fromkeys(converted)),
        resolved_fields=tuple(dict.fromkeys(resolved)),
    )


def normalize_property_mapping(
    properties: Mapping[str, Any] | None,
    property_types: Mapping[str, GraphPropertyType | Any] | None,
    *,
    provider: str = "",
) -> dict[str, Any]:
    return normalize_filter_dict(properties, property_types, provider=provider).filter_dict


def _normalize_expression(
    expression: Any,
    spec: GraphPropertyType,
    *,
    field_name: str,
    provider: str,
) -> tuple[Any, bool]:
    if expression is None:
        return None, False
    if not isinstance(expression, str) and is_graph_scalar_parameter_value(expression):
        converted = coerce_graph_value(
            spec,
            expression,
            field_name=field_name,
            provider=provider,
        )
        if spec.kind in _LEXICAL_TEXT_KINDS:
            return {"eq": converted}, True
        return converted, converted != expression or type(converted) is not type(expression)
    if isinstance(expression, Mapping):
        if spec.kind in {MAP, POINT} and not _is_operator_expression(expression):
            converted = coerce_graph_value(
                spec,
                expression,
                field_name=field_name,
                provider=provider,
            )
            return {"eq": converted}, True
        if not expression:
            raise GraphFilterTypeError("{} 不能为空对象".format(field_name))
        result: dict[str, Any] = {}
        changed = False
        for raw_op, operand in expression.items():
            op = normalize_operator(raw_op)
            if op not in _OPERATORS:
                raise GraphFilterTypeError(
                    "{} 包含不支持的操作符: {!r}".format(field_name, raw_op)
                )
            effective_op = (
                "contains_item"
                if op == "contains" and spec.kind in {LIST, VECTOR}
                else op
            )
            normalized_operand, operand_changed = _normalize_operand(
                effective_op,
                operand,
                spec,
                field_name="{}.{}".format(field_name, op),
                provider=provider,
            )
            result[effective_op] = normalized_operand
            if not changed:
                changed = operand_changed
            if not changed:
                changed = effective_op != op
            if not changed:
                changed = str(raw_op).strip().lower().lstrip("$") != op
        return result, changed
    if isinstance(expression, Sequence) and not isinstance(expression, (str, bytes, bytearray)):
        if spec.kind in {LIST, VECTOR, POINT}:
            converted = coerce_graph_value(
                spec,
                expression,
                field_name=field_name,
                provider=provider,
            )
            return {"eq": converted}, True
        values = []
        changed = False
        for item in expression:
            converted = coerce_graph_value(spec, item, field_name=field_name, provider=provider)
            values.append(converted)
            changed = changed or converted != item or type(converted) is not type(item)
        return values, changed
    if isinstance(expression, str):
        terms = _parse_text_terms(
            expression,
            preserve_text=spec.kind in _LEXICAL_TEXT_KINDS,
        )
        if terms is None:
            literal = _decode_quoted_literal(expression)
            converted = coerce_graph_value(spec, literal, field_name=field_name, provider=provider)
            # Text properties must remain text even when their lexical form
            # looks numeric or boolean.  The explicit equality wrapper keeps
            # the legacy Cypher parser from inferring a different scalar type.
            if spec.kind in _LEXICAL_TEXT_KINDS:
                return {"eq": converted}, True
            return converted, converted != expression or type(converted) is not type(expression)
        normalized_terms: list[dict[str, Any]] = []
        for op, raw_value in terms:
            normalized_value, _ = _normalize_operand(
                op,
                raw_value,
                spec,
                field_name="{}.{}".format(field_name, op),
                provider=provider,
            )
            normalized_terms.append({op: normalized_value})
        if len(normalized_terms) == 1:
            return normalized_terms[0], True
        return {"or": normalized_terms}, True
    converted = coerce_graph_value(spec, expression, field_name=field_name, provider=provider)
    if spec.kind in _LEXICAL_TEXT_KINDS:
        return {"eq": converted}, True
    return converted, converted != expression or type(converted) is not type(expression)


def _normalize_operand(
    op: str,
    operand: Any,
    spec: GraphPropertyType,
    *,
    field_name: str,
    provider: str,
) -> tuple[Any, bool]:
    if op in {"or", "and"}:
        if (
            isinstance(operand, (str, bytes, bytearray))
            or not isinstance(operand, Sequence)
            or not operand
        ):
            raise GraphFilterTypeError("{} 必须是非空条件数组".format(field_name))
        values = []
        changed = False
        for index, item in enumerate(operand):
            value, item_changed = _normalize_expression(
                item,
                spec,
                field_name="{}[{}]".format(field_name, index),
                provider=provider,
            )
            if not isinstance(value, Mapping):
                if isinstance(value, Sequence) and not isinstance(
                    value,
                    (str, bytes, bytearray),
                ):
                    value = {"in": value}
                else:
                    value = {"eq": value}
                item_changed = True
            values.append(value)
            changed = changed or item_changed
        return values, changed
    if op == "is_null":
        if not isinstance(operand, bool):
            raise GraphFilterTypeError("{} 必须是布尔值".format(field_name))
        return operand, False
    if op in {"in", "not_in"}:
        if (
            isinstance(operand, (str, bytes, bytearray))
            or not isinstance(operand, Sequence)
            or not operand
        ):
            raise GraphFilterTypeError("{} 必须是非空数组".format(field_name))
        values = []
        changed = False
        for item in operand:
            value = coerce_graph_value(spec, item, field_name=field_name, provider=provider)
            values.append(value)
            changed = changed or value != item or type(value) is not type(item)
        return values, changed
    if op in {"contains", "contains_item", "starts_with", "ends_with"}:
        if op == "contains_item":
            item_spec = spec.item_type or GraphPropertyType(ANY, raw="ANY")
            value = coerce_graph_value(item_spec, operand, field_name=field_name, provider=provider)
            return value, value != operand or type(value) is not type(operand)
        if spec.kind not in _TEXT_OPERATOR_KINDS:
            raise GraphFilterTypeError("{} 不适用于 {} 类型".format(field_name, spec.kind))
        value = operand if isinstance(operand, str) else str(operand)
        return value, value != operand
    if op in {"gt", "gte", "lt", "lte"} and spec.kind not in _ORDERABLE_KINDS:
        raise GraphFilterTypeError("{} 不适用于 {} 类型".format(field_name, spec.kind))
    value = coerce_graph_value(spec, operand, field_name=field_name, provider=provider)
    return value, value != operand or type(value) is not type(operand)


def coerce_graph_value(
    spec: GraphPropertyType,
    value: Any,
    *,
    field_name: str,
    provider: str = "",
) -> Any:
    """Coerce a single operand without going through an imprecise float."""

    if value is None or spec.kind in {UNKNOWN, ANY}:
        return value
    if spec.ambiguous:
        raise GraphFilterTypeError(
            "{} 对应多个物理类型，无法安全转换".format(field_name)
        )
    kind = spec.kind
    if kind == BOOLEAN:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.strip().lower() in {"true", "false"}:
            return value.strip().lower() == "true"
        raise GraphFilterTypeError("{} 必须是 boolean".format(field_name))
    if kind == INTEGER:
        if isinstance(value, bool):
            raise GraphFilterTypeError("{} 不能把 boolean 当作 integer".format(field_name))
        if isinstance(value, int):
            result = value
        elif isinstance(value, float):
            if (
                not math.isfinite(value)
                or not value.is_integer()
                or abs(value) > _JS_SAFE_INT_MAX
            ):
                raise GraphFilterTypeError(
                    "{} 的浮点值不能无损转换为整数；大整数请使用字符串传递".format(
                        field_name
                    )
                )
            result = int(value)
        elif isinstance(value, Decimal):
            if not value.is_finite() or value != value.to_integral_value():
                raise GraphFilterTypeError("{} 必须是整数".format(field_name))
            result = int(value)
        elif isinstance(value, str) and _INTEGER_RE.fullmatch(value.strip()):
            result = int(value.strip())
        else:
            raise GraphFilterTypeError("{} 必须是整数".format(field_name))
        bounds = spec.integer_bounds
        if bounds is None and str(provider).lower() == "neo4j":
            bounds = (_INT_MIN, _INT_MAX)
        if bounds and not bounds[0] <= result <= bounds[1]:
            raise GraphFilterTypeError("{} 超出整数范围".format(field_name))
        return result
    if kind in {FLOAT, DECIMAL}:
        if isinstance(value, bool):
            raise GraphFilterTypeError("{} 不能把 boolean 当作 number".format(field_name))
        if isinstance(value, Decimal):
            decimal_value = value
        elif isinstance(value, (int, float)):
            decimal_value = Decimal(str(value))
        elif isinstance(value, str):
            try:
                decimal_value = Decimal(value.strip())
            except InvalidOperation as exc:
                raise GraphFilterTypeError("{} 必须是合法数字".format(field_name)) from exc
        else:
            raise GraphFilterTypeError("{} 必须是合法数字".format(field_name))
        if not decimal_value.is_finite():
            raise GraphFilterTypeError("{} 不能是 NaN 或 Infinity".format(field_name))
        if kind == DECIMAL and str(provider).lower() != "neo4j":
            return decimal_value
        result = float(decimal_value)
        if not math.isfinite(result):
            raise GraphFilterTypeError("{} 超出浮点范围".format(field_name))
        return result
    if kind in {STRING, UUID_TYPE} and _is_text_operator_expression(value):
        # A MAP value whose keys happen to be named "eq"/"or" is still a
        # literal for a text property.  It cannot be safely coerced to text.
        raise GraphFilterTypeError("{} 必须是文本值，不能是操作符对象".format(field_name))
    if kind == STRING:
        if isinstance(value, str):
            return value
        if isinstance(value, bool):
            return "true" if value else "false"
        if isinstance(value, (int, float, Decimal, UUID)):
            return str(value)
        raise GraphFilterTypeError("{} 必须是 string".format(field_name))
    if kind == UUID_TYPE:
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, str):
            try:
                parsed = UUID(value.strip())
                return str(parsed)
            except (ValueError, AttributeError) as exc:
                raise GraphFilterTypeError("{} 必须是合法 UUID".format(field_name)) from exc
        raise GraphFilterTypeError("{} 必须是 UUID".format(field_name))
    if kind == DATE:
        if isinstance(value, datetime):
            raise GraphFilterTypeError("{} 不能把 datetime 当作 date".format(field_name))
        if isinstance(value, date):
            return value.isoformat() if str(provider).lower() == "hugegraph" else value
        if isinstance(value, str):
            if str(provider).lower() == "hugegraph":
                # HugeGraph REST snapshots may expose DATE using deployment-
                # specific JSON encodings.  A string already came from the
                # caller's comparison boundary, so preserve it byte-for-byte
                # instead of guessing ISO-vs-epoch representation.
                return value
            try:
                parsed = date.fromisoformat(value.strip())
                return parsed
            except ValueError as exc:
                raise GraphFilterTypeError("{} 必须是 ISO date".format(field_name)) from exc
        raise GraphFilterTypeError("{} 必须是 ISO date".format(field_name))
    if kind in {LOCAL_TIME, ZONED_TIME}:
        result = _coerce_time(value, field_name)
        has_zone = result.tzinfo is not None and result.utcoffset() is not None
        if kind == LOCAL_TIME and has_zone:
            raise GraphFilterTypeError("{} 不能带时区".format(field_name))
        if kind == ZONED_TIME and not has_zone:
            raise GraphFilterTypeError("{} 必须带时区".format(field_name))
        return result.isoformat() if str(provider).lower() == "hugegraph" else result
    if kind in {LOCAL_DATETIME, ZONED_DATETIME}:
        result = _coerce_datetime(value, field_name)
        has_zone = result.tzinfo is not None and result.utcoffset() is not None
        if kind == LOCAL_DATETIME and has_zone:
            raise GraphFilterTypeError("{} 不能带时区".format(field_name))
        if kind == ZONED_DATETIME and not has_zone:
            raise GraphFilterTypeError("{} 必须带时区".format(field_name))
        return result.isoformat() if str(provider).lower() == "hugegraph" else result
    if kind == DURATION:
        return _coerce_duration(value, field_name, provider)
    if kind == POINT:
        return _coerce_point(value, field_name, provider)
    if kind == LIST:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise GraphFilterTypeError("{} 必须是数组".format(field_name))
        item_spec = spec.item_type or GraphPropertyType(ANY, raw="ANY")
        return [
            coerce_graph_value(item_spec, item, field_name="{}[]".format(field_name), provider=provider)
            for item in value
        ]
    if kind == VECTOR:
        if isinstance(value, (str, bytes, bytearray)) or not isinstance(value, Sequence):
            raise GraphFilterTypeError("{} 必须是数值数组".format(field_name))
        item_spec = spec.item_type or GraphPropertyType(FLOAT, raw="FLOAT")
        return [
            coerce_graph_value(item_spec, item, field_name="{}[]".format(field_name), provider=provider)
            for item in value
        ]
    if kind == MAP:
        if not isinstance(value, Mapping):
            raise GraphFilterTypeError("{} 必须是对象".format(field_name))
        return dict(value)
    if kind == BYTES:
        if isinstance(value, bytes):
            return value
        if isinstance(value, bytearray):
            return bytes(value)
        if str(provider).lower() == "hugegraph" and isinstance(value, str):
            # HugeGraph's REST representation for BLOB is opaque to this
            # adapter.  Keep caller text byte-for-byte instead of guessing an
            # encoding (hex/base64/UTF-8) that could silently change equality.
            return value
        raise GraphFilterTypeError("{} 必须是 bytes".format(field_name))
    return value


def _coerce_type_spec(value: Any) -> GraphPropertyType | None:
    if value is None:
        return None
    if isinstance(value, GraphPropertyType):
        return value
    return parse_graph_property_type(value)


def _split_top_level(value: str, separator: str) -> list[str]:
    parts: list[str] = []
    start = 0
    depth = 0
    for index, char in enumerate(value):
        if char == "<":
            depth += 1
        elif char == ">":
            depth = max(0, depth - 1)
        elif char == separator and depth == 0:
            parts.append(value[start:index].strip())
            start = index + 1
    parts.append(value[start:].strip())
    return [part for part in parts if part]


def _is_operator_expression(value: Mapping[Any, Any]) -> bool:
    return any(normalize_operator(key) in _OPERATORS for key in value)


def _is_text_operator_expression(value: Any) -> bool:
    return isinstance(value, Mapping) and _is_operator_expression(value)


def _parse_text_terms(
    value: str,
    *,
    preserve_text: bool = False,
) -> list[tuple[str, Any]] | None:
    text = value.strip()
    if not text:
        raise GraphFilterTypeError("过滤表达式不能为空")
    parts = [part.strip() for part in _split_unquoted_or(text)]
    if any(not part for part in parts):
        raise GraphFilterTypeError("过滤表达式包含空条件")
    terms: list[tuple[str, Any]] = []
    for part in parts:
        match = _COMPARE_RE.match(part)
        if match:
            operator = {"=": "eq", ">": "gt", ">=": "gte", "<": "lt", "<=": "lte"}[match.group(1)]
            terms.append((operator, _parse_text_token(match.group(2), preserve_text=preserve_text)))
            continue
        match = _CONTAINS_RE.match(part)
        if match:
            terms.append(("contains", _unescape_text_token(match.group(2), match.group(1))))
            continue
        match = _START_END_RE.match(part)
        if match:
            operator = "starts_with" if match.group(1).upper().startswith("STARTS") else "ends_with"
            terms.append((operator, _unescape_text_token(match.group(3), match.group(2))))
            continue
        # A plain string is a value, even when it looks numeric.
        if len(parts) == 1:
            return None
        terms.append(("eq", _parse_text_token(part, preserve_text=preserve_text)))
    return terms


def _split_unquoted_or(value: str) -> list[str]:
    parts: list[str] = []
    start = 0
    quote = ""
    escaped = False
    index = 0
    text = str(value)
    while index < len(text):
        char = text[index]
        if escaped:
            escaped = False
            index += 1
            continue
        if char == "\\" and quote:
            escaped = True
            index += 1
            continue
        if char in {"'", '"'}:
            if quote == char:
                quote = ""
            elif not quote:
                quote = char
            index += 1
            continue
        if not quote and text[index:index + 2].upper() == "OR":
            before = text[index - 1] if index > 0 else " "
            after = text[index + 2] if index + 2 < len(text) else " "
            if before.isspace() and after.isspace():
                parts.append(text[start:index])
                start = index + 2
                index += 2
                continue
        index += 1
    parts.append(text[start:])
    return parts


def _parse_text_token(value: str, *, preserve_text: bool = False) -> Any:
    raw = value.strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return _unescape_text_token(raw[1:-1], raw[0])
    if preserve_text:
        return raw
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    if lowered == "null":
        return None
    if _INTEGER_RE.fullmatch(raw):
        try:
            return int(raw)
        except ValueError:
            pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _unescape_text_token(value: str, quote: str) -> str:
    return str(value).replace("\\{}".format(quote), quote).replace("\\\\", "\\")


def _decode_quoted_literal(value: str) -> Any:
    raw = str(value).strip()
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in {"'", '"'}:
        return _unescape_text_token(raw[1:-1], raw[0])
    return value


def _coerce_time(value: Any, field_name: str) -> time:
    if isinstance(value, time):
        return value
    if isinstance(value, str):
        try:
            return time.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise GraphFilterTypeError("{} 必须是 ISO time".format(field_name)) from exc
    raise GraphFilterTypeError("{} 必须是 ISO time".format(field_name))


def _coerce_datetime(value: Any, field_name: str) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError as exc:
            raise GraphFilterTypeError("{} 必须是 ISO datetime".format(field_name)) from exc
    raise GraphFilterTypeError("{} 必须是 ISO datetime".format(field_name))


def _coerce_duration(value: Any, field_name: str, provider: str) -> Any:
    if str(value.__class__.__module__).startswith("neo4j."):
        return value
    if isinstance(value, timedelta):
        if str(provider).lower() == "hugegraph":
            return value.total_seconds()
        return value
    if isinstance(value, str) and str(provider).lower() == "neo4j":
        try:
            from neo4j.time import Duration
            return Duration.from_iso_format(value.strip())
        except (ImportError, ValueError) as exc:
            raise GraphFilterTypeError("{} 必须是 ISO duration".format(field_name)) from exc
    if isinstance(value, str):
        return value.strip()
    raise GraphFilterTypeError("{} 必须是 duration".format(field_name))


def _coerce_point(value: Any, field_name: str, provider: str) -> Any:
    if str(value.__class__.__module__).startswith("neo4j."):
        return value
    coordinates: Any = value
    srid = None
    geographic = False
    if isinstance(value, Mapping):
        coordinates = value.get("coordinates")
        if coordinates is None and "longitude" in value and "latitude" in value:
            geographic = True
            coordinates = [value.get("longitude"), value.get("latitude")]
            if "height" in value:
                coordinates.append(value.get("height"))
        if coordinates is None and "x" in value and "y" in value:
            coordinates = [value.get("x"), value.get("y")]
            if "z" in value:
                coordinates.append(value.get("z"))
        srid = value.get("srid") or value.get("crs")
    if isinstance(coordinates, (str, bytes, bytearray)) or not isinstance(coordinates, Sequence):
        raise GraphFilterTypeError("{} 必须是坐标数组或坐标对象".format(field_name))
    try:
        numeric = [float(item) for item in coordinates]
    except (TypeError, ValueError) as exc:
        raise GraphFilterTypeError("{} 坐标必须是数字".format(field_name)) from exc
    if len(numeric) not in {2, 3}:
        raise GraphFilterTypeError("{} 只支持二维或三维坐标".format(field_name))
    if str(provider).lower() != "neo4j":
        return numeric
    try:
        from neo4j.spatial import CartesianPoint, WGS84Point
    except ImportError:
        return numeric
    normalized_srid = str(srid or "").strip().lower()
    if geographic or normalized_srid in {"4326", "wgs-84", "wgs84", "wgs_84", "4979"}:
        return WGS84Point(numeric)
    return CartesianPoint(numeric)


def is_graph_parameter_value(value: Any) -> bool:
    """Whether a value can safely be passed as a parameter value."""

    if value is None or isinstance(
        value,
        (bool, int, float, str, bytes, bytearray, date, time, datetime, timedelta, Decimal, UUID),
    ):
        return True
    if isinstance(value, Mapping):
        return all(isinstance(key, str) and is_graph_parameter_value(item) for key, item in value.items())
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return all(is_graph_parameter_value(item) for item in value)
    return str(value.__class__.__module__).startswith("neo4j.")


def is_graph_scalar_parameter_value(value: Any) -> bool:
    """Return whether a value is one graph-driver scalar parameter.

    Some Neo4j temporal/spatial classes implement the Python sequence protocol
    for their internal representation.  They are still one database value,
    not a Cypher ``IN`` list.
    """

    if str(value.__class__.__module__).startswith("neo4j."):
        return True
    return value is None or isinstance(
        value,
        (bool, int, float, str, bytes, bytearray, date, time, datetime, timedelta, Decimal, UUID),
    )


def _union_types(left: GraphPropertyType, right: GraphPropertyType) -> GraphPropertyType:
    values: list[GraphPropertyType] = []
    for item in (left, right):
        values.extend(item.alternatives if item.kind == UNION else (item,))
    unique: list[GraphPropertyType] = []
    positions: dict[tuple[Any, ...], int] = {}
    for item in values:
        key = _type_identity(item)
        if key not in positions:
            positions[key] = len(unique)
            unique.append(item)
            continue
        position = positions[key]
        unique[position] = _merge_equivalent_types(unique[position], item)
    if len(unique) == 1:
        return unique[0]
    return GraphPropertyType(
        UNION,
        alternatives=tuple(unique),
        raw="|".join(item.raw or item.kind for item in unique),
    )


def _type_identity(spec: GraphPropertyType) -> tuple[Any, ...]:
    return (
        spec.kind,
        _type_identity(spec.item_type) if spec.item_type is not None else None,
        tuple(_type_identity(item) for item in spec.alternatives),
    )


def _merge_equivalent_types(
    left: GraphPropertyType,
    right: GraphPropertyType,
) -> GraphPropertyType:
    item_type = left.item_type
    if left.item_type is not None and right.item_type is not None:
        item_type = _union_types(left.item_type, right.item_type)
    elif item_type is None:
        item_type = right.item_type
    bounds = left.integer_bounds
    if left.integer_bounds is not None and right.integer_bounds is not None:
        bounds = (
            min(left.integer_bounds[0], right.integer_bounds[0]),
            max(left.integer_bounds[1], right.integer_bounds[1]),
        )
    elif bounds is None:
        bounds = right.integer_bounds
    raw_values = list(
        dict.fromkeys(value for value in (left.raw, right.raw) if value)
    )
    return GraphPropertyType(
        left.kind,
        item_type=item_type,
        alternatives=left.alternatives or right.alternatives,
        raw="|".join(raw_values) or left.kind,
        integer_bounds=bounds,
    )


def _schema_field_inventory_complete(
    complete_inventory_source: bool,
    field_complete: Any,
    element_name: str,
) -> bool:
    if complete_inventory_source:
        return True
    if not isinstance(field_complete, Mapping):
        return False
    return field_complete.get(element_name) is True


def schema_type_index(schema_context: Mapping[str, Any] | None) -> tuple[
    dict[str, dict[str, GraphPropertyType]],
    dict[str, dict[str, GraphPropertyType]],
    dict[str, Any],
]:
    """Extract typed indexes from the compact preloaded schema context."""

    nodes: dict[str, dict[str, GraphPropertyType]] = {}
    edges: dict[str, dict[str, GraphPropertyType]] = {}
    metadata: dict[str, Any] = {
        "node_labels": set(),
        "relationship_types": set(),
        "triples": [],
        "node_incomplete": set(),
        "edge_incomplete": set(),
        "node_name_incomplete": set(),
        "edge_name_incomplete": set(),
        "node_names_complete": False,
        "edge_names_complete": False,
        "node_field_inventory": set(),
        "edge_field_inventory": set(),
    }
    if not isinstance(schema_context, Mapping):
        return nodes, edges, metadata
    complete_inventory_source = str(schema_context.get("source") or "") == "provider_preload"
    raw_nodes = schema_context.get("nodes_schema")
    if isinstance(raw_nodes, Mapping):
        metadata["node_names_complete"] = _schema_names_complete(
            raw_nodes,
        )
        metadata["node_labels"].update(
            str(item).strip()
            for item in raw_nodes.get("labels", [])
            if str(item).strip()
        )
        raw_map = raw_nodes.get("schema")
        types_authoritative = raw_nodes.get("types_authoritative") is not False
        incomplete_labels = {
            str(item).strip()
            for item in (raw_nodes.get("fields_truncated") or [])
            if str(item).strip()
        }
        field_complete = raw_nodes.get("field_complete")
        if isinstance(field_complete, Mapping):
            incomplete_labels.update(
                str(label).strip()
                for label, complete in field_complete.items()
                if str(label).strip() and complete is False
            )
        if raw_nodes.get("source_truncated"):
            incomplete_labels.update(metadata["node_labels"])
        if not types_authoritative:
            incomplete_labels.update(metadata["node_labels"])
        metadata["node_incomplete"].update(incomplete_labels)
        if raw_nodes.get("truncated") or raw_nodes.get("source_truncated"):
            metadata["node_name_incomplete"].update(metadata["node_labels"])
        if isinstance(raw_map, Mapping):
            for raw_label, raw_fields in raw_map.items():
                label = str(raw_label).strip()
                if not label or not isinstance(raw_fields, Mapping):
                    continue
                metadata["node_labels"].add(label)
                nodes[label] = {
                    str(field).strip(): parse_graph_property_type(field_type)
                    for field, field_type in raw_fields.items()
                    if str(field).strip()
                }
                field_inventory_complete = _schema_field_inventory_complete(
                    complete_inventory_source,
                    field_complete,
                    label,
                )
                if label not in incomplete_labels and field_inventory_complete:
                    metadata["node_field_inventory"].add(label)
    raw_edges = schema_context.get("edges_schema")
    if isinstance(raw_edges, Mapping):
        metadata["edge_names_complete"] = _schema_names_complete(
            raw_edges,
        )
        metadata["relationship_types"].update(
            str(item).strip()
            for item in raw_edges.get("relationship_types", [])
            if str(item).strip()
        )
        raw_map = raw_edges.get("schema")
        types_authoritative = raw_edges.get("types_authoritative") is not False
        incomplete_relations = {
            str(item).strip()
            for item in (raw_edges.get("fields_truncated") or [])
            if str(item).strip()
        }
        field_complete = raw_edges.get("field_complete")
        if isinstance(field_complete, Mapping):
            incomplete_relations.update(
                str(relation).strip()
                for relation, complete in field_complete.items()
                if str(relation).strip() and complete is False
            )
        if raw_edges.get("source_truncated"):
            incomplete_relations.update(metadata["relationship_types"])
        if not types_authoritative:
            incomplete_relations.update(metadata["relationship_types"])
        metadata["edge_incomplete"].update(incomplete_relations)
        if raw_edges.get("truncated") or raw_edges.get("source_truncated"):
            metadata["edge_name_incomplete"].update(metadata["relationship_types"])
        if isinstance(raw_map, Mapping):
            for raw_type, raw_fields in raw_map.items():
                relation_type = str(raw_type).strip()
                if not relation_type or not isinstance(raw_fields, Mapping):
                    continue
                metadata["relationship_types"].add(relation_type)
                edges[relation_type] = {
                    str(field).strip(): parse_graph_property_type(field_type)
                    for field, field_type in raw_fields.items()
                    if str(field).strip()
                }
                field_inventory_complete = _schema_field_inventory_complete(
                    complete_inventory_source,
                    field_complete,
                    relation_type,
                )
                if relation_type not in incomplete_relations and field_inventory_complete:
                    metadata["edge_field_inventory"].add(relation_type)
        triples = raw_edges.get("relation_triples")
        if isinstance(triples, list):
            metadata["triples"] = [item for item in triples if isinstance(item, Mapping)]
    return nodes, edges, metadata


def _schema_names_complete(
    raw_schema: Mapping[str, Any],
) -> bool:
    if raw_schema.get("truncated") or raw_schema.get("source_truncated"):
        return False
    if raw_schema.get("names_complete") is not None:
        return raw_schema.get("names_complete") is True
    # Runtime preload can be intentionally focused to one mapping.  Unless a
    # producer explicitly marks the inventory complete, do not reject names
    # outside that focused subset.
    return False


class GraphFilterTypeResolver:
    """Request-scoped resolver with a targeted backend database fallback."""

    def __init__(
        self,
        *,
        backend: Any,
        scope: Any,
        schema_context: Mapping[str, Any] | None = None,
        provider: str = "",
    ) -> None:
        self.backend = backend
        self.scope = scope
        self.provider = str(provider or "").strip().lower()
        self._nodes, self._edges, self._metadata = schema_type_index(schema_context)
        self._loaded_fields: dict[tuple[str, str], set[str]] = {}
        self._authoritative_fields: dict[tuple[str, str], set[str]] = {}

    def resolve(
        self,
        *,
        element_type: str,
        element_class: str,
        property_names: Sequence[str] | None = None,
    ) -> dict[str, GraphPropertyType]:
        elem_type = str(element_type or "").strip().upper()
        elem_class = str(element_class or "").strip()
        fields = list(dict.fromkeys(
            str(item).strip() for item in (property_names or []) if str(item).strip()
        ))
        if not fields:
            return {}
        result = self._lookup(elem_type, elem_class)
        target = (elem_type, elem_class)
        missing = [field for field in fields if field not in result]
        incomplete = (
            elem_class in self._metadata.get("node_incomplete", set())
            if elem_type == "NODE"
            else elem_class in self._metadata.get("edge_incomplete", set())
        )
        loaded_fields = self._loaded_fields.get(target, set())
        pending_fields = [field for field in fields if field not in loaded_fields]
        # A compact result is usable only when the provider explicitly says
        # that this element's field inventory is complete.  This matters for
        # business edge aliases: their physical relation schema may be
        # present, while the alias itself still needs a targeted lookup.
        needs_backend = bool(missing)
        if not needs_backend:
            needs_backend = not result
        if not needs_backend:
            needs_backend = incomplete
        if not needs_backend:
            needs_backend = not self._compact_target_complete(
                elem_type,
                elem_class,
            )
        should_load = needs_backend and bool(pending_fields)
        if should_load:
            self._load_from_backend(
                elem_type,
                elem_class,
                pending_fields,
            )
            result = self._lookup(elem_type, elem_class)
        if self._compact_target_complete(elem_type, elem_class):
            return {field: result[field] for field in fields if field in result}
        authoritative = self._authoritative_fields.get(target, set())
        return {
            field: result[field]
            for field in fields
            if field in result and field in authoritative
        }

    def normalize_filter(
        self,
        filter_dict: Mapping[str, Any] | None,
        *,
        element_type: str,
        element_class: str,
    ) -> TypeNormalization:
        fields = list(filter_dict.keys()) if isinstance(filter_dict, Mapping) else []
        types = self.resolve(
            element_type=element_type,
            element_class=element_class,
            property_names=fields,
        )
        return normalize_filter_dict(filter_dict, types, provider=self.provider)

    def normalize_properties(
        self,
        properties: Mapping[str, Any] | None,
        *,
        element_type: str,
        element_class: str,
    ) -> dict[str, Any]:
        fields = list(properties.keys()) if isinstance(properties, Mapping) else []
        types = self.resolve(
            element_type=element_type,
            element_class=element_class,
            property_names=fields,
        )
        return normalize_property_mapping(properties, types, provider=self.provider)

    def prepare_targets(self, targets: Sequence[tuple[str, str, Sequence[str]]]) -> None:
        for element_type, element_class, fields in targets:
            self.resolve(
                element_type=element_type,
                element_class=element_class,
                property_names=fields,
            )

    def prepare_pattern_targets(
        self,
        path_pattern: Sequence[Any],
        *,
        return_properties: Any | None = None,
    ) -> None:
        del return_properties
        self.prepare_targets(
            extract_pattern_filter_targets(path_pattern)
        )

    def validation_schema_context(self) -> dict[str, Any]:
        node_schema = {
            label: {field: spec.raw or spec.kind for field, spec in fields.items()}
            for label, fields in self._nodes.items()
        }
        edge_schema = {
            relation_type: {field: spec.raw or spec.kind for field, spec in fields.items()}
            for relation_type, fields in self._edges.items()
        }
        node_labels = self._metadata["node_labels"] | set(node_schema)
        relation_types = self._metadata["relationship_types"] | set(edge_schema)
        return {
            "node_labels": sorted(node_labels),
            "relationship_types": sorted(relation_types),
            "nodes_schema": {
                "labels": sorted(node_labels),
                "schema": node_schema,
                "field_complete": {
                    label: self._is_target_complete("NODE", label)
                    for label in node_labels
                },
                "fields_truncated": sorted(
                    label for label in node_labels
                    if not self._is_target_complete("NODE", label)
                ),
                "truncated": not bool(self._metadata.get("node_names_complete")),
                "source_truncated": bool(
                    self._metadata.get("node_name_incomplete")
                ),
            },
            "edges_schema": {
                "relationship_types": sorted(relation_types),
                "schema": edge_schema,
                "field_complete": {
                    relation: self._is_target_complete("EDGE", relation)
                    for relation in relation_types
                },
                "fields_truncated": sorted(
                    relation for relation in relation_types
                    if not self._is_target_complete("EDGE", relation)
                ),
                "truncated": not bool(self._metadata.get("edge_names_complete")),
                "source_truncated": bool(
                    self._metadata.get("edge_name_incomplete")
                ),
                "relation_triples": list(self._metadata.get("triples") or []),
            },
        }

    def _lookup(self, element_type: str, element_class: str) -> dict[str, GraphPropertyType]:
        if element_type == "NODE":
            return dict(self._nodes.get(element_class) or {})
        if element_type != "EDGE":
            return {}
        candidates: list[str] = []
        if element_class in self._edges:
            candidates.append(element_class)
        for triple in self._metadata.get("triples") or []:
            aliases = {
                str(triple.get(key) or "").strip()
                for key in ("relation_type", "type", "physical_relation_type", "relation_type_id")
            }
            if element_class in aliases:
                for key in ("physical_relation_type", "type"):
                    physical = str(triple.get(key) or "").strip()
                    if physical and physical in self._edges:
                        candidates.append(physical)
        result: dict[str, GraphPropertyType] = {}
        for candidate in dict.fromkeys(candidates):
            for field, spec in (self._edges.get(candidate) or {}).items():
                result[field] = spec if field not in result else _union_types(result[field], spec)
        # A targeted lookup for the business edge alias is field-authoritative.
        # Keep it above possibly stale physical-schema candidates even when the
        # backend could not return the physical relation names in the same call.
        alias_fields = self._edges.get(element_class) or {}
        for field in self._authoritative_fields.get(("EDGE", element_class), set()):
            if field in alias_fields:
                result[field] = alias_fields[field]
        return result

    def _load_from_backend(
        self,
        element_type: str,
        element_class: str,
        fields: Sequence[str],
    ) -> None:
        target = (element_type, element_class)
        requested_fields = list(dict.fromkeys(str(item).strip() for item in fields if str(item).strip()))
        if not requested_fields:
            return
        self._loaded_fields.setdefault(target, set()).update(requested_fields)
        loader = getattr(self.backend, "get_property_types", None)
        if not callable(loader):
            return
        try:
            payload = loader(
                element_class=element_class,
                element_type=element_type,
                property_names=requested_fields,
                scope=self.scope,
            )
        except Exception:
            logger.warning(
                "Graph property type lookup failed; preserving original filter operands"
            )
            payload = None
        if not isinstance(payload, Mapping):
            return
        raw_types = payload.get("property_types") or payload.get("schema") or {}
        if not isinstance(raw_types, Mapping):
            return
        parsed_types: dict[str, GraphPropertyType] = {}
        for raw_field, raw_type in raw_types.items():
            field = str(raw_field or "").strip()
            parsed = parse_graph_property_type(raw_type)
            if field and parsed.kind != UNKNOWN:
                parsed_types[field] = parsed
        authoritative_fields = _backend_authoritative_fields(
            payload,
            available_fields=parsed_types,
        )
        parsed_types = {
            field: parsed
            for field, parsed in parsed_types.items()
            if field in authoritative_fields
        }
        if not parsed_types:
            return
        if element_type == "NODE":
            self._nodes.setdefault(element_class, {})
            self._metadata["node_labels"].add(element_class)
            candidates: list[str] = []
            aliases = [element_class]
        else:
            physical_types = payload.get("physical_relation_types")
            candidates = [
                str(item).strip()
                for item in physical_types
                if str(item).strip()
            ] if isinstance(physical_types, Sequence) and not isinstance(physical_types, str) else []
            candidates = candidates or [element_class]
            for candidate in candidates:
                self._edges.setdefault(candidate, {})
            self._metadata["relationship_types"].update(candidates)
            self._metadata["relationship_types"].add(element_class)
            aliases = list(dict.fromkeys([element_class, *candidates]))
        for alias in aliases:
            alias_bucket = (
                self._nodes.setdefault(alias, {})
                if element_type == "NODE"
                else self._edges.setdefault(alias, {})
            )
            for field in authoritative_fields:
                alias_bucket.pop(field, None)
            for field, parsed in parsed_types.items():
                alias_bucket[field] = parsed
        self._authoritative_fields.setdefault(target, set()).update(parsed_types)
        if element_type == "EDGE":
            for physical in candidates:
                for triple in self._metadata.setdefault("triples", []):
                    if (
                        str(triple.get("relation_type") or "").strip() == element_class
                        and str(triple.get("physical_relation_type") or triple.get("type") or "").strip() == physical
                    ):
                        break
                else:
                    self._metadata["triples"].append({
                        "relation_type": element_class,
                        "physical_relation_type": physical,
                    })

    def _compact_target_complete(self, element_type: str, element_class: str) -> bool:
        return self._is_target_complete(element_type, element_class)

    def _is_target_complete(self, element_type: str, element_class: str) -> bool:
        target = (str(element_type or "").upper(), str(element_class or "").strip())
        inventory = (
            self._metadata.get("node_field_inventory", set())
            if target[0] == "NODE"
            else self._metadata.get("edge_field_inventory", set())
        )
        return target[1] in inventory


def _backend_authoritative_fields(
    payload: Mapping[str, Any],
    *,
    available_fields: Mapping[str, Any],
) -> set[str]:
    raw_fields = payload.get("authoritative_fields")
    if isinstance(raw_fields, Sequence) and not isinstance(raw_fields, (str, bytes, bytearray)):
        return {
            str(field).strip()
            for field in raw_fields
            if str(field).strip() in available_fields
        }
    if payload.get("complete") is True:
        return set(available_fields)
    return set()


def _pattern_segment_details(raw_segment: Any, index: int) -> tuple[str, str, list[Any]]:
    if isinstance(raw_segment, Mapping):
        label = str(raw_segment.get("label") or "").strip()
        edge_type = str(raw_segment.get("type") or raw_segment.get("edge_type") or "").strip()
        relation = str(raw_segment.get("relation_type") or raw_segment.get("relation_type_id") or "").strip()
        if index % 2 == 0:
            node_type = label or str(
                raw_segment.get("entity_type_id") or raw_segment.get("type") or ""
            ).strip()
            if node_type:
                return "NODE", node_type, [raw_segment]
        elif edge_type or relation:
            return "EDGE", edge_type or relation, [raw_segment]
        return "", "", []
    if not isinstance(raw_segment, (list, tuple)) or not raw_segment:
        return "", "", []
    selector = raw_segment[0]
    if isinstance(selector, Mapping):
        label = str(selector.get("label") or "").strip()
        edge_type = str(selector.get("type") or selector.get("edge_type") or "").strip()
        relation = str(selector.get("relation_type") or selector.get("relation_type_id") or "").strip()
        if index % 2 == 0:
            return "NODE", label or str(
                selector.get("entity_type_id") or selector.get("type") or ""
            ).strip(), list(raw_segment)
        return "EDGE", edge_type or relation, list(raw_segment)
    token = str(selector or "").strip()
    if not token:
        return "", "", []
    return ("EDGE" if index % 2 else "NODE"), token, list(raw_segment[1:])


def extract_pattern_filter_targets(
    path_pattern: Sequence[Any] | None,
) -> list[tuple[str, str, list[str]]]:
    """Return only fields that are used as pattern filter operands."""

    targets: list[tuple[str, str, list[str]]] = []
    for index, raw_segment in enumerate(path_pattern or []):
        role, name, mappings = _pattern_segment_details(raw_segment, index)
        if not role or not name:
            continue
        fields: list[str] = []
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                continue
            for key in ("props", "filters"):
                nested = mapping.get(key)
                if isinstance(nested, Mapping):
                    fields.extend(
                        str(item).strip() for item in nested if str(item).strip()
                    )
            fields.extend(
                str(item).strip()
                for item in mapping
                if str(item).strip() not in _PATTERN_CONTROL_KEYS
            )
        targets.append((role, name, list(dict.fromkeys(fields))))
    return targets


def normalize_pattern_filters(
    path_pattern: Sequence[Any],
    resolver: GraphFilterTypeResolver,
    *,
    return_properties: Any | None = None,
) -> list[Any]:
    del return_properties
    targets = extract_pattern_filter_targets(path_pattern)
    result: list[Any] = []
    target_index = 0
    for index, raw_segment in enumerate(path_pattern):
        role, name, mappings = _pattern_segment_details(raw_segment, index)
        target = targets[target_index] if role and name and target_index < len(targets) else None
        if role and name:
            target_index += 1
        if not target:
            result.append(raw_segment)
            continue
        element_type, element_class, fields = target
        typed_fields = resolver.resolve(
            element_type=element_type,
            element_class=element_class,
            property_names=fields,
        )
        result.append(_normalize_segment(raw_segment, typed_fields, resolver.provider))
    return result


def _normalize_segment(
    raw_segment: Any,
    typed_fields: Mapping[str, GraphPropertyType],
    provider: str,
) -> Any:
    if isinstance(raw_segment, Mapping):
        copied = dict(raw_segment)
        for key in ("props", "filters"):
            nested = copied.get(key)
            if isinstance(nested, Mapping):
                copied[key] = normalize_filter_dict(nested, typed_fields, provider=provider).filter_dict
        direct = {
            key: value
            for key, value in copied.items()
            if str(key).strip() not in _PATTERN_CONTROL_KEYS
        }
        copied.update(normalize_filter_dict(direct, typed_fields, provider=provider).filter_dict)
        return copied
    if isinstance(raw_segment, (list, tuple)):
        copied = list(raw_segment)
        for item_index, item in enumerate(copied):
            if not isinstance(item, Mapping):
                continue
            nested = dict(item)
            for key in ("props", "filters"):
                value = nested.get(key)
                if isinstance(value, Mapping):
                    nested[key] = normalize_filter_dict(value, typed_fields, provider=provider).filter_dict
            direct = {
                key: value
                for key, value in nested.items()
                if str(key).strip() not in _PATTERN_CONTROL_KEYS
            }
            normalized_direct = normalize_filter_dict(direct, typed_fields, provider=provider).filter_dict
            nested.update(normalized_direct)
            copied[item_index] = nested
        return copied
    return raw_segment
