"""Scope and sanitization helpers for graph providers."""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from .models import GraphScope

SCOPE_HIDDEN_KEYS: set[str] = {"user_id", "workspace_name", "scene_name"}
LOGIC_HIDDEN_KEYS: set[str] = {"logic_blocks"}
INTERNAL_NODE_LABELS: set[str] = {"D2O_NODE"}
INTERNAL_PROPERTY_KEYS: set[str] = {"d2o_workspace_id", "d2o_ontology_id", "_d2o_record_key"}
HIDDEN_PROPERTY_KEYS: set[str] = SCOPE_HIDDEN_KEYS | LOGIC_HIDDEN_KEYS | INTERNAL_PROPERTY_KEYS

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_\u4e00-\u9fff][A-Za-z0-9_\u4e00-\u9fff]*$")
_PROPERTY_SELECTOR_RE = re.compile(r"[*?\[\]]")


class GraphScopeError(ValueError):
    """Raised when graph workspace scope cannot be built safely."""


def build_graph_scope(provider_config: Mapping[str, Any]) -> GraphScope:
    workspace_property = str(provider_config.get("workspace_property") or "d2o_workspace_id").strip()
    workspace_id = str(provider_config.get("workspace_id") or "").strip()
    ontology_property = str(provider_config.get("ontology_property") or "").strip()
    ontology_id = str(provider_config.get("ontology_id") or "").strip() if ontology_property else ""

    if not workspace_property:
        raise GraphScopeError("workspace_property 不能为空")
    if not workspace_id:
        raise GraphScopeError("workspace_id 不能为空，无法执行带 scope 的图查询")
    validate_graph_identifier(workspace_property, "workspace_property")

    if ontology_property:
        if not ontology_id:
            raise GraphScopeError("ontology_id 不能为空，无法执行带 scope 的图查询")
        validate_graph_identifier(ontology_property, "ontology_property")

    return GraphScope(
        workspace_property=workspace_property,
        workspace_value=workspace_id,
        ontology_property=ontology_property,
        ontology_value=ontology_id,
    )


def validate_graph_identifier(value: Any, field_name: str = "identifier") -> str:
    identifier = str(value or "").strip()
    if not identifier or not _IDENTIFIER_RE.match(identifier):
        raise ValueError("{} 必须是安全的图数据库标识符".format(field_name))
    return identifier


def quote_graph_identifier(value: Any, field_name: str = "identifier") -> str:
    identifier = validate_graph_identifier(value, field_name)
    return "`{}`".format(identifier)


def is_hidden_property(key: Any, *, hidden_properties: Iterable[Any] | None = None) -> bool:
    normalized = str(key or "").strip()
    dynamic_hidden = {str(item or "").strip() for item in (hidden_properties or []) if str(item or "").strip()}
    return normalized in HIDDEN_PROPERTY_KEYS or normalized in dynamic_hidden


def sanitize_labels(labels: Iterable[Any] | str | None) -> list[str]:
    if labels is None:
        return []
    raw_labels: Iterable[Any] = [labels] if isinstance(labels, str) else labels
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw_label in raw_labels:
        label = str(raw_label or "").strip()
        if not label or label in INTERNAL_NODE_LABELS or label in seen:
            continue
        seen.add(label)
        cleaned.append(label)
    return cleaned


def sanitize_return_properties(
    properties: Iterable[Any] | None,
    *,
    hidden_properties: Iterable[Any] | None = None,
) -> list[str]:
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in properties or []:
        key = str(raw or "").strip()
        if not key or key in seen or is_hidden_property(key, hidden_properties=hidden_properties):
            continue
        if _PROPERTY_SELECTOR_RE.search(key):
            raise ValueError("return_properties 只接受属性名")
        validate_graph_identifier(key, "return_properties")
        seen.add(key)
        cleaned.append(key)
    return cleaned


def sanitize_properties(
    properties: Mapping[str, Any] | None,
    *,
    return_properties: Iterable[str] | None = None,
    hidden_properties: Iterable[Any] | None = None,
) -> dict[str, Any]:
    if not isinstance(properties, Mapping):
        return {}
    allowed = (
        set(sanitize_return_properties(return_properties, hidden_properties=hidden_properties))
        if return_properties
        else None
    )
    cleaned: dict[str, Any] = {}
    for raw_key, raw_value in properties.items():
        key = str(raw_key or "").strip()
        if not key or is_hidden_property(key, hidden_properties=hidden_properties):
            continue
        if allowed is not None and key not in allowed:
            continue
        cleaned[key] = _json_safe_value(raw_value)
    return cleaned


def _json_safe_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _json_safe_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe_value(item) for item in value]
    return str(value)
