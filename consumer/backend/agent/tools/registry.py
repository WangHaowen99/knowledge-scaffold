"""Tool registration, schema queries, and argument validation."""

from __future__ import annotations

import json
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from utils.json_utils import _loads_json_object_loose
from utils.logging import get_logger
from agent.tools.base import BaseTool, ToolBatchResult, ToolCaller, ToolContext, ToolOutcome
from agent.tools.business_data_search import BusinessDataSearchTool
from agent.tools.execute_script import ExecuteScriptTool, build_execute_script_tool_schema
from agent.tools.memory import MemoryTool
from agent.tools.ontology_context import OntologyContextTool, get_ontology_context_schema
from agent.tools.ontology_evidence_search import (
    OntologyEvidenceSearchTool,
    is_ontology_evidence_search_visible,
)
from agent.tools.request_user_interaction import RequestUserInteractionTool
from agent.tools.report_business_checkpoint import ReportBusinessCheckpointTool
from agent.tools.skill import SkillTool
from agent.tools.spawn_sub_agent import SpawnSubAgentTool
from agent.tools.web_search_tool import WebSearchTool
from utils.skills import get_skill_registry

# Re-export the native-async entry point from the registry boundary.
from agent.tools.executor import aexecute_tool_batch  # noqa: F401

logger = get_logger("tools.registry")

_ARGUMENT_PARSE_ERROR_KEY = "__tool_argument_parse_error__"


def _build_tool_registry(tools: Iterable[BaseTool]) -> Dict[str, BaseTool]:
    registry: Dict[str, BaseTool] = {}
    for tool in tools:
        parameters = tool.schema.get("function", {}).get("parameters")
        if isinstance(parameters, dict) and parameters.get("type") == "object":
            parameters.setdefault("additionalProperties", False)
        if tool.name in registry:
            raise ValueError("Duplicate tool registration for '{}'".format(tool.name))
        registry[tool.name] = tool
    return registry


TOOL_SPECS: Dict[str, BaseTool] = _build_tool_registry([
    OntologyContextTool(),
    OntologyEvidenceSearchTool(),
    SkillTool(),
    BusinessDataSearchTool(),
    WebSearchTool(),
    ExecuteScriptTool(),
    RequestUserInteractionTool(),
    ReportBusinessCheckpointTool(),
    MemoryTool(),
    SpawnSubAgentTool(),
])


# ============================================================================
# Schema queries
# ============================================================================


def get_all_tools_schema(
    *,
    workspace_id: Optional[str] = None,
    selected_ontology_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    evidence_visible = _evidence_visible(workspace_id, selected_ontology_id)
    return [
        get_tool_schema_for_context(
            spec,
            selected_ontology_id=selected_ontology_id,
            workspace_id=workspace_id,
        )
        for spec in TOOL_SPECS.values()
        if spec.name != "ontology_evidence_search" or evidence_visible
    ]


def get_tool_specs_for_caller(
    caller: ToolCaller,
    *,
    include_memory: bool = True,
    include_orchestration: bool = True,
    include_web_search: bool = False,
    include_interaction: bool = True,
    include_business_checkpoint: bool = True,
    include_solver: bool = True,
    workspace_id: Optional[str] = None,
    selected_ontology_id: Optional[str] = None,
    selected_ontology_version: Optional[str] = None,
) -> List[BaseTool]:
    specs: List[BaseTool] = []
    has_skills = _has_registered_skills(workspace_id=workspace_id)
    evidence_visible = _evidence_visible(workspace_id, selected_ontology_id)
    for spec in TOOL_SPECS.values():
        if caller not in spec.allowed_callers:
            continue
        if spec.name == "skill" and not has_skills:
            continue
        if not include_memory and spec.category == "memory":
            continue
        if not include_orchestration and spec.category == "orchestration":
            continue
        if not include_web_search and spec.category == "web_search":
            continue
        if not include_interaction and spec.category == "interaction":
            continue
        if spec.name == "ontology_evidence_search" and not evidence_visible:
            continue
        if spec.category == "business_checkpoint":
            from ontology.capabilities import is_business_checkpoint_available

            if (
                not include_business_checkpoint
                or not selected_ontology_id
                or not is_business_checkpoint_available(
                    selected_ontology_id,
                    ontology_version=selected_ontology_version,
                    workspace_id=workspace_id,
                )
            ):
                continue
        specs.append(spec)
    if include_solver:
        solver_specs = get_solver_tool_specs_for_context(
            selected_ontology_id=selected_ontology_id,
            selected_ontology_version=selected_ontology_version,
            workspace_id=workspace_id,
        )
        for spec in solver_specs:
            if caller in spec.allowed_callers:
                specs.append(spec)
    return specs


def get_tools_schema(
    caller: ToolCaller,
    *,
    include_memory: bool = True,
    include_orchestration: bool = True,
    include_web_search: bool = False,
    include_interaction: bool = True,
    include_business_checkpoint: bool = True,
    include_solver: bool = True,
    selected_ontology_id: Optional[str] = None,
    selected_ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return [
        get_tool_schema_for_context(
            spec,
            selected_ontology_id=selected_ontology_id,
            selected_ontology_version=selected_ontology_version,
            workspace_id=workspace_id,
        )
        for spec in get_tool_specs_for_caller(
            caller,
            include_memory=include_memory,
            include_orchestration=include_orchestration,
            include_web_search=include_web_search,
            include_interaction=include_interaction,
            include_business_checkpoint=include_business_checkpoint,
            include_solver=include_solver,
            workspace_id=workspace_id,
            selected_ontology_id=selected_ontology_id,
            selected_ontology_version=selected_ontology_version,
        )
    ]


def get_tool_schema_for_context(
    spec: BaseTool,
    *,
    selected_ontology_id: Optional[str] = None,
    selected_ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    if spec.name == "execute_script":
        return build_execute_script_tool_schema(
            selected_ontology_id,
            selected_ontology_version=selected_ontology_version,
            workspace_id=workspace_id,
        )
    if spec.name == "ontology_context":
        return get_ontology_context_schema(
            structured_enabled=_evidence_visible(workspace_id, selected_ontology_id)
        )
    return spec.schema


def get_regular_tools_schema(
    *,
    include_memory: bool = False,
    include_web_search: bool = False,
    workspace_id: Optional[str] = None,
    selected_ontology_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    schemas = []
    has_skills = _has_registered_skills(workspace_id=workspace_id)
    evidence_visible = _evidence_visible(workspace_id, selected_ontology_id)
    for spec in TOOL_SPECS.values():
        if spec.category not in {"regular", "business_data_search", "web_search"}:
            continue
        if spec.name == "skill" and not has_skills:
            continue
        if spec.category == "web_search" and not include_web_search:
            continue
        if spec.name == "ontology_evidence_search" and not evidence_visible:
            continue
        schemas.append(
            get_tool_schema_for_context(
                spec,
                selected_ontology_id=selected_ontology_id,
                workspace_id=workspace_id,
            )
        )
    if include_memory:
        schemas.extend(spec.schema for spec in TOOL_SPECS.values() if spec.category == "memory")
    return schemas


def get_universal_tools_schema(
    *,
    include_memory: bool = True,
    include_web_search: bool = False,
    include_interaction: bool = True,
    workspace_id: Optional[str] = None,
    selected_ontology_id: Optional[str] = None,
) -> List[Dict[str, Any]]:
    return get_tools_schema(
        "main",
        include_memory=include_memory,
        include_web_search=include_web_search,
        include_interaction=include_interaction,
        workspace_id=workspace_id,
        selected_ontology_id=selected_ontology_id,
    )


def _evidence_visible(workspace_id: Optional[str], selected_ontology_id: Optional[str]) -> bool:
    return is_ontology_evidence_search_visible(
        workspace_id=workspace_id,
        selected_ontology_id=selected_ontology_id,
    )


def _has_registered_skills(*, workspace_id: Optional[str] = None) -> bool:
    try:
        registry = (
            get_skill_registry(workspace_id=workspace_id)
            if workspace_id
            else get_skill_registry()
        )
        return bool(registry.list_skills())
    except Exception:
        logger.warning("Failed to inspect skill registry while building tool schemas", exc_info=True)
        # Schema generation can run before a default workspace is established.
        # Preserve the base registry in that indeterminate state; an explicitly
        # loaded empty registry still hides the skill tool.
        return True


# ============================================================================
# Tool lookup
# ============================================================================


def get_solver_tool_specs_for_context(
    *,
    selected_ontology_id: Optional[str],
    selected_ontology_version: Optional[str],
    workspace_id: Optional[str],
) -> Tuple[BaseTool, ...]:
    from agent.tools.solver import get_solver_tool_specs_for_context as load_specs

    return tuple(
        load_specs(
            selected_ontology_id=selected_ontology_id,
            selected_ontology_version=selected_ontology_version,
            workspace_id=workspace_id,
        )
    )


def get_tool_spec(
    tool_name: str,
    *,
    selected_ontology_id: Optional[str] = None,
    selected_ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Optional[BaseTool]:
    normalized_name = str(tool_name or "")
    static = TOOL_SPECS.get(normalized_name)
    if static is not None:
        return static
    matches: List[BaseTool] = []
    solver_specs = get_solver_tool_specs_for_context(
        selected_ontology_id=selected_ontology_id,
        selected_ontology_version=selected_ontology_version,
        workspace_id=workspace_id,
    )
    for spec in solver_specs:
        if spec.name == normalized_name:
            matches.append(spec)
    return matches[0] if len(matches) == 1 else None


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
# Tool call parsing and validation
# ============================================================================


def parse_tool_call(tool_call: Dict[str, Any]) -> Tuple[str, Dict[str, Any], Optional[str]]:
    func_info = tool_call.get("function", {}) or {}
    name = str(func_info.get("name") or "")
    args_str = func_info.get("arguments", "{}")
    tool_call_id = tool_call.get("id")
    parsed_args = _loads_json_object_loose(args_str)
    if parsed_args is None and str(args_str or "").strip():
        args = {_ARGUMENT_PARSE_ERROR_KEY: str(args_str)}
    else:
        args = parsed_args or {}
    return name, args, tool_call_id


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

    # ontology_context 'asset' is validated by the tool handler (checkpoint key
    # constraint); the request-visible text schema deliberately strips it from
    # the resource enum in evidence-off mode, so exempt it from enum
    # enforcement here to keep the legacy asset resource callable.
    if (
        tool_name == "ontology_context"
        and str(payload.get("resource") or "").strip() == "asset"
    ):
        resource_schema = properties.get("resource")
        if isinstance(resource_schema, dict):
            resource_schema.pop("enum", None)

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
    if missing_fields:
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

    validation_errors = _validate_schema_value(payload, parameters, path="arguments")
    if validation_errors:
        return "Error: Tool '{}' invalid argument(s): {}.".format(
            tool_name,
            "; ".join(validation_errors[:5]),
        )
    return None


# ============================================================================
# Internal helpers
# ============================================================================


def _is_missing_required_tool_argument(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
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


def _validate_schema_value(value: Any, schema: Dict[str, Any], *, path: str) -> List[str]:
    errors: List[str] = []
    one_of = schema.get("oneOf")
    if isinstance(one_of, list):
        branch_errors = [
            _validate_schema_value(value, branch, path=path)
            for branch in one_of
            if isinstance(branch, dict)
        ]
        matching = sum(not branch for branch in branch_errors)
        if matching != 1:
            errors.append("{} must match exactly one oneOf branch".format(path))
            return errors

    expected_type = schema.get("type")
    if expected_type and not _matches_json_type(value, str(expected_type)):
        return ["{} must be {}".format(path, expected_type)]

    enum_values = schema.get("enum")
    if isinstance(enum_values, list) and value not in enum_values:
        errors.append(
            "{} must be one of {}".format(path, json.dumps(enum_values, ensure_ascii=False))
        )
        return errors

    if "const" in schema and value != schema.get("const"):
        return ["{} must equal the oneOf discriminator".format(path)]

    if isinstance(value, (int, float)) and not isinstance(value, bool):
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            errors.append("{} must be at least {}".format(path, minimum))
        if isinstance(maximum, (int, float)) and value > maximum:
            errors.append("{} must be at most {}".format(path, maximum))

    if isinstance(value, dict):
        properties = schema.get("properties") or {}
        required = schema.get("required") or []
        for field in required:
            if field not in value:
                errors.append("{}.{} is required".format(path, field))
        if schema.get("additionalProperties") is False:
            unexpected = sorted(str(key) for key in value if key not in properties)
            if unexpected:
                errors.append("{} has unexpected field(s): {}".format(path, ", ".join(unexpected)))
        for key, item in value.items():
            child_schema = properties.get(key)
            if isinstance(child_schema, dict):
                errors.extend(
                    _validate_schema_value(item, child_schema, path="{}.{}".format(path, key))
                )

    if isinstance(value, list):
        min_items = schema.get("minItems")
        if isinstance(min_items, int) and len(value) < min_items:
            errors.append("{} must contain at least {} item(s)".format(path, min_items))
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, item in enumerate(value):
                errors.extend(
                    _validate_schema_value(item, item_schema, path="{}[{}]".format(path, index))
                )
    return errors


def _matches_json_type(value: Any, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True
