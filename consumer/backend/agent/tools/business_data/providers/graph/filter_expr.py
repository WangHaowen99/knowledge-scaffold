"""Shared Neo4J filter expression helpers migrated from Jiutian search_server."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any


_OR_SPLIT_RE = re.compile(r"\s+OR\s+", re.IGNORECASE)
_COMPARE_RE = re.compile(r"^(>=|<=|>|<|=)\s*(.+)$")
_CONTAINS_RE = re.compile(r"^CONTAINS\s+['\"]([^'\"]*)['\"]$", re.IGNORECASE)
_START_END_RE = re.compile(r"^(STARTS\s+WITH|ENDS\s+WITH)\s+['\"]([^'\"]*)['\"]$", re.IGNORECASE)
_CYPHER_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")
_STRUCT_OPS = {
    "eq",
    "ne",
    "gt",
    "gte",
    "lt",
    "lte",
    "contains",
    "starts_with",
    "ends_with",
    "in",
    "not_in",
    "is_null",
}
_STRUCT_OP_ALIASES = {
    "startswith": "starts_with",
    "starts-with": "starts_with",
    "endswith": "ends_with",
    "ends-with": "ends_with",
    "notin": "not_in",
    "not-in": "not_in",
    "nin": "not_in",
}


def normalize_structured_operator(raw_op: Any) -> str:
    op = str(raw_op or "").strip().lower()
    if op.startswith("$"):
        op = op[1:]
    return _STRUCT_OP_ALIASES.get(op, op)


def safe_prop(name: Any) -> str:
    prop = str(name or "").strip()
    if not prop or "`" in prop or _CONTROL_CHAR_RE.search(prop):
        raise ValueError("Invalid property name: {!r}".format(name))
    return prop


def cypher_identifier(name: Any) -> str:
    identifier = safe_prop(name)
    if _CYPHER_IDENTIFIER_RE.match(identifier):
        return identifier
    return "`{}`".format(identifier)


def cypher_prop_ref(alias: str, name: Any) -> str:
    if not _CYPHER_IDENTIFIER_RE.match(str(alias or "")):
        raise ValueError("Invalid Cypher alias: {!r}".format(alias))
    return "{}.{}".format(alias, cypher_identifier(name))


def build_filter_where(
    filter_dict: Mapping[str, Any] | None,
    alias: str,
    params: dict[str, Any],
    param_prefix: str,
) -> list[str]:
    if not filter_dict:
        return []
    if not isinstance(filter_dict, Mapping):
        raise ValueError("filter_dict 必须是对象类型，实际为: {}".format(type(filter_dict).__name__))

    conditions: list[str] = []
    for prop_index, (raw_prop, expr) in enumerate(filter_dict.items()):
        prop = safe_prop(raw_prop)
        field_name = "filter_dict.{}".format(prop)
        ref = cypher_prop_ref(alias, prop)
        key = _param_base(param_prefix, prop, prop_index)

        if expr is None:
            conditions.append("{} IS NULL".format(ref))
            continue
        if isinstance(expr, bool):
            params[key] = expr
            conditions.append("{} = ${}".format(ref, key))
            continue
        if isinstance(expr, (int, float)):
            params[key] = expr
            conditions.append("{} = ${}".format(ref, key))
            continue
        if isinstance(expr, Mapping):
            conditions.append(_build_structured_condition(ref, expr, params, key, field_name))
            continue
        if isinstance(expr, Sequence) and not isinstance(expr, (str, bytes)):
            params[key] = _validate_sequence(expr, field_name)
            conditions.append("{} IN ${}".format(ref, key))
            continue
        if not isinstance(expr, str):
            raise ValueError("{} 的值类型不支持: {}".format(field_name, type(expr).__name__))

        text = expr.strip()
        if not text:
            raise ValueError("{} 不能为空字符串".format(field_name))
        parts = [part.strip() for part in _OR_SPLIT_RE.split(text)]
        part_conditions: list[str] = []
        for part_index, part in enumerate(parts):
            if not part:
                raise ValueError("{} 包含空条件".format(field_name))
            part_key = _part_param_key(param_prefix, prop, prop_index, part_index)
            part_conditions.append(_build_part_condition(ref, part, params, part_key))
        conditions.append(part_conditions[0] if len(part_conditions) == 1 else "({})".format(" OR ".join(part_conditions)))
    return conditions


def _param_base(param_prefix: str, prop: str, prop_index: int) -> str:
    if _CYPHER_IDENTIFIER_RE.match(prop):
        return "{}_{}_0".format(param_prefix, prop)
    return "{}_{}".format(param_prefix, prop_index)


def _part_param_key(param_prefix: str, prop: str, prop_index: int, part_index: int) -> str:
    if _CYPHER_IDENTIFIER_RE.match(prop):
        return "{}_{}_{}".format(param_prefix, prop, part_index)
    return "{}_{}_{}".format(param_prefix, prop_index, part_index)


def _parse_scalar(token: str) -> Any:
    raw = token.strip()
    if not raw:
        raise ValueError("过滤表达式不能为空")
    if len(raw) >= 2 and raw[0] == raw[-1] and raw[0] in ("'", '"'):
        quote = raw[0]
        inner = raw[1:-1]
        if quote == "'":
            inner = inner.replace("\\'", "'")
        else:
            inner = inner.replace('\\"', '"')
        return inner.replace("\\\\", "\\")
    low = raw.lower()
    if low == "true":
        return True
    if low == "false":
        return False
    if low == "null":
        return None
    try:
        return int(raw)
    except ValueError:
        pass
    try:
        return float(raw)
    except ValueError:
        return raw


def _ensure_scalar(value: Any, field_name: str) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    raise ValueError("{} 仅支持标量值（str/int/float/bool/null）".format(field_name))


def _validate_sequence(value: Any, field_name: str) -> list[Any]:
    if isinstance(value, (str, bytes)) or not isinstance(value, Sequence):
        raise ValueError("{} 必须是数组".format(field_name))
    seq = list(value)
    if not seq:
        raise ValueError("{} 不能为空数组".format(field_name))
    return [_ensure_scalar(item, field_name) for item in seq]


def _build_scalar_condition(
    prop_ref: str,
    op: str,
    value: Any,
    params: dict[str, Any],
    param_key: str,
    field_name: str,
) -> str:
    scalar = _ensure_scalar(value, field_name)
    if scalar is None:
        if op == "=":
            return "{} IS NULL".format(prop_ref)
        if op == "<>":
            return "{} IS NOT NULL".format(prop_ref)
        raise ValueError("{} 中 null 仅支持 eq/ne".format(field_name))
    params[param_key] = scalar
    return "{} {} ${}".format(prop_ref, op, param_key)


def _build_part_condition(prop_ref: str, part: str, params: dict[str, Any], param_key: str) -> str:
    text = part.strip()
    match = _COMPARE_RE.match(text)
    if match:
        op, value_token = match.group(1), match.group(2).strip()
        value = _parse_scalar(value_token)
        if value is None:
            if op != "=":
                raise ValueError("NULL 仅支持 '=' 比较")
            return "{} IS NULL".format(prop_ref)
        params[param_key] = value
        return "{} {} ${}".format(prop_ref, op, param_key)

    match = _CONTAINS_RE.match(text)
    if match:
        params[param_key] = match.group(1)
        return "{} CONTAINS ${}".format(prop_ref, param_key)

    match = _START_END_RE.match(text)
    if match:
        params[param_key] = match.group(2)
        op = "STARTS WITH" if "STARTS" in match.group(1).upper() else "ENDS WITH"
        return "{} {} ${}".format(prop_ref, op, param_key)

    value = _parse_scalar(text)
    if value is None:
        return "{} IS NULL".format(prop_ref)
    params[param_key] = value
    return "{} = ${}".format(prop_ref, param_key)


def _build_structured_condition(
    prop_ref: str,
    expr: Mapping[str, Any],
    params: dict[str, Any],
    param_key: str,
    field_name: str,
) -> str:
    if not expr:
        raise ValueError("{} 不能为空对象".format(field_name))
    normalized: dict[str, Any] = {}
    for raw_op, value in expr.items():
        op = normalize_structured_operator(raw_op)
        if op not in _STRUCT_OPS:
            raise ValueError("{} 包含不支持的操作符: {!r}，支持: {}".format(field_name, raw_op, sorted(_STRUCT_OPS)))
        normalized[op] = value

    conditions: list[str] = []
    for index, (op, value) in enumerate(normalized.items()):
        key = "{}_{}_{}".format(param_key, index, op)
        if op == "eq":
            conditions.append(_build_scalar_condition(prop_ref, "=", value, params, key, "{}.eq".format(field_name)))
        elif op == "ne":
            conditions.append(_build_scalar_condition(prop_ref, "<>", value, params, key, "{}.ne".format(field_name)))
        elif op == "gt":
            conditions.append(_build_scalar_condition(prop_ref, ">", value, params, key, "{}.gt".format(field_name)))
        elif op == "gte":
            conditions.append(_build_scalar_condition(prop_ref, ">=", value, params, key, "{}.gte".format(field_name)))
        elif op == "lt":
            conditions.append(_build_scalar_condition(prop_ref, "<", value, params, key, "{}.lt".format(field_name)))
        elif op == "lte":
            conditions.append(_build_scalar_condition(prop_ref, "<=", value, params, key, "{}.lte".format(field_name)))
        elif op == "contains":
            scalar = _ensure_scalar(value, "{}.contains".format(field_name))
            if scalar is None:
                raise ValueError("{}.contains 不能为 null".format(field_name))
            params[key] = str(scalar)
            conditions.append("{} CONTAINS ${}".format(prop_ref, key))
        elif op == "starts_with":
            scalar = _ensure_scalar(value, "{}.starts_with".format(field_name))
            if scalar is None:
                raise ValueError("{}.starts_with 不能为 null".format(field_name))
            params[key] = str(scalar)
            conditions.append("{} STARTS WITH ${}".format(prop_ref, key))
        elif op == "ends_with":
            scalar = _ensure_scalar(value, "{}.ends_with".format(field_name))
            if scalar is None:
                raise ValueError("{}.ends_with 不能为 null".format(field_name))
            params[key] = str(scalar)
            conditions.append("{} ENDS WITH ${}".format(prop_ref, key))
        elif op == "in":
            params[key] = _validate_sequence(value, "{}.in".format(field_name))
            conditions.append("{} IN ${}".format(prop_ref, key))
        elif op == "not_in":
            params[key] = _validate_sequence(value, "{}.not_in".format(field_name))
            conditions.append("NOT ({} IN ${})".format(prop_ref, key))
        elif op == "is_null":
            if not isinstance(value, bool):
                raise ValueError("{}.is_null 必须是布尔值".format(field_name))
            conditions.append("{} IS NULL".format(prop_ref) if value else "{} IS NOT NULL".format(prop_ref))
    if not conditions:
        raise ValueError("{} 未生成有效过滤条件".format(field_name))
    return conditions[0] if len(conditions) == 1 else "({})".format(" AND ".join(conditions))
