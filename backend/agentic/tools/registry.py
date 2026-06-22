"""Tool registration, schema queries, and argument validation."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional

from ..utils.logging import get_logger
from .base import BaseTool, ToolBatchResult, ToolCaller, ToolContext, ToolOutcome
from .business_data_search import BusinessDataSearchTool
from .execute_script import ExecuteScriptTool
from .memory import MemoryTool
from .ontology_context import OntologyContextTool
from .request_user_interaction import RequestUserInteractionTool
from .skill import SkillTool
from .spawn_sub_agent import SpawnSubAgentTool
from .web_search import WebSearchTool

# Re-export execute_tool_batch for backward compatibility
from .executor import execute_tool_batch  # noqa: F401

logger = get_logger("tools.registry")

_ARGUMENT_PARSE_ERROR_KEY = "__tool_argument_parse_error__"


def _build_tool_registry(tools: Iterable[BaseTool]) -> Dict[str, BaseTool]:
    registry: Dict[str, BaseTool] = {}
    for tool in tools:
        if tool.name in registry:
            raise ValueError("Duplicate tool registration for '{}'".format(tool.name))
        registry[tool.name] = tool
    return registry


TOOL_SPECS: Dict[str, BaseTool] = _build_tool_registry([
    OntologyContextTool(),
    SkillTool(),
    BusinessDataSearchTool(),
    WebSearchTool(),
    ExecuteScriptTool(),
    RequestUserInteractionTool(),
    MemoryTool(),
    SpawnSubAgentTool(),
])


# ============================================================================
# Schema queries
# ============================================================================


def get_all_tools_schema() -> List[Dict[str, Any]]:
    return [spec.schema for spec in TOOL_SPECS.values()]


def get_tool_specs_for_caller(
    caller: ToolCaller,
    *,
    include_memory: bool = True,
    include_orchestration: bool = True,
    include_web_search: bool = False,
) -> List[BaseTool]:
    specs: List[BaseTool] = []
    for spec in TOOL_SPECS.values():
        if caller not in spec.allowed_callers:
            continue
        if not include_memory and spec.category == "memory":
            continue
        if not include_orchestration and spec.category == "orchestration":
            continue
        if not include_web_search and spec.category == "web_search":
            continue
        specs.append(spec)
    return specs


def get_tools_schema(
    caller: ToolCaller,
    *,
    include_memory: bool = True,
    include_orchestration: bool = True,
    include_web_search: bool = False,
) -> List[Dict[str, Any]]:
    return [
        spec.schema
        for spec in get_tool_specs_for_caller(
            caller,
            include_memory=include_memory,
            include_orchestration=include_orchestration,
            include_web_search=include_web_search,
        )
    ]


def get_regular_tools_schema(
    *,
    include_memory: bool = False,
    include_web_search: bool = False,
) -> List[Dict[str, Any]]:
    schemas = []
    for spec in TOOL_SPECS.values():
        if spec.category not in {"regular", "business_data_search", "web_search"}:
            continue
        if spec.category == "web_search" and not include_web_search:
            continue
        schemas.append(spec.schema)
    if include_memory:
        schemas.extend(spec.schema for spec in TOOL_SPECS.values() if spec.category == "memory")
    return schemas


def get_universal_tools_schema(
    *,
    include_memory: bool = True,
    include_web_search: bool = False,
) -> List[Dict[str, Any]]:
    return get_tools_schema(
        "main",
        include_memory=include_memory,
        include_web_search=include_web_search,
    )


# ============================================================================
# Tool lookup
# ============================================================================


def get_tool_spec(tool_name: str) -> Optional[BaseTool]:
    return TOOL_SPECS.get(str(tool_name or ""))


def get_tool_schema(tool_name: str) -> Optional[Dict[str, Any]]:
    spec = get_tool_spec(tool_name)
    return spec.schema if spec else None


def get_tool_function(tool_name: str) -> Optional[Callable[..., Any]]:
    spec = get_tool_spec(tool_name)
    return spec.handler if spec else None


def get_tool_category(tool_name: str) -> str:
    spec = get_tool_spec(tool_name)
    return spec.category if spec else "unknown"


def is_tool_available(tool_name: str) -> bool:
    return str(tool_name or "") in TOOL_SPECS


def is_tool_allowed(tool_name: str, caller: ToolCaller) -> bool:
    spec = get_tool_spec(tool_name)
    return bool(spec and caller in spec.allowed_callers)


# ============================================================================
# Tool argument validation
# ============================================================================


def validate_tool_arguments(
    tool_name: str,
    args: Optional[Dict[str, Any]],
    *,
    tool_schema: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    schema = tool_schema or get_tool_schema(tool_name)
    if not schema:
        return None

    parameters = schema.get("function", {}).get("parameters") or {}
    properties = parameters.get("properties") or {}
    required_fields = list(parameters.get("required") or [])
    payload = dict(args or {})

    parse_error = payload.get(_ARGUMENT_PARSE_ERROR_KEY)
    if parse_error is not None:
        preview = str(parse_error)
        if len(preview) > 240:
            preview = "{}...".format(preview[:240])
        return (
            "Error: Tool '{}' arguments are not valid JSON object. "
            "Call the tool again with arguments matching the schema. Received: {}"
        ).format(tool_name, preview)

    missing_fields = [
        field for field in required_fields if _is_missing_required_tool_argument(payload.get(field))
    ]
    if not missing_fields:
        return None

    example_payload = {
        field: (
            payload[field]
            if field in payload and not _is_missing_required_tool_argument(payload.get(field))
            else _example_value_for_property(field, properties.get(field) or {})
        )
        for field in required_fields
    }

    return (
        "Error: Tool '{}' missing required argument(s): {}. "
        "Call the tool again with a non-empty JSON object, for example {}."
    ).format(
        tool_name,
        ", ".join(missing_fields),
        json.dumps(example_payload, ensure_ascii=False),
    )


# ============================================================================
# Internal helpers
# ============================================================================


def _is_missing_required_tool_argument(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict, tuple, set)):
        return len(value) == 0
    return False


def _example_value_for_property(field_name: str, prop_schema: Dict[str, Any]) -> Any:
    field_type = str(prop_schema.get("type") or "").strip().lower()
    if field_type == "string":
        return "<{}>".format(field_name)
    if field_type == "integer":
        return 1
    if field_type == "number":
        return 1
    if field_type == "boolean":
        return True
    if field_type == "array":
        return []
    if field_type == "object":
        return {}
    return "<value>"
