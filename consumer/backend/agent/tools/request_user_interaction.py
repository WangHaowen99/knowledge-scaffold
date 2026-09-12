"""User interaction tool for ontology execution checkpoints."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Mapping, Optional

from agent.tools.base import ToolContext, ToolOutcome

INTERACTION_TYPES = {"input", "single_choice", "approval"}


REQUEST_USER_INTERACTION_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "request_user_interaction",
        "description": (
            "Use this tool whenever:"
            "- The operation markdown says '暂停执行'、'待用户确认'、'供用户选择'、'询问用户'、'由调用方决定'"
            "- You reach a branch where the next step depends on a human decision"
            "- You need to present interim results before continuing"
            "- The user must pick from a list of options defined in the operation"
            "Never use plain text to ask for these decisions — always call this tool."
            "Pause ontology execution and request a user decision before continuing. "
            "Use this only when the loaded operation logic explicitly requires "
            "missing user input, a single choice, or an approval checkpoint."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "type": {
                    "type": "string",
                    "enum": ["input", "single_choice", "approval"],
                    "description": "Interaction shape: free text input, single choice, or continue/stop approval.",
                },
                "prompt": {
                    "type": "string",
                    "description": "Question or decision prompt shown to the user.",
                },
                "options": {
                    "type": "array",
                    "description": "Choices for single_choice or approval interactions.",
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "properties": {
                            "id": {"type": "string"},
                            "label": {"type": "string"},
                            "value": {},
                            "description": {"type": "string"},
                        },
                    },
                },
                "executed_summary": {
                    "type": "string",
                    "description": "For approval checkpoints, summarize what has already been executed.",
                },
                "next_plan": {
                    "type": "string",
                    "description": "For approval checkpoints, summarize the next step if the user continues.",
                },
                "requested_fields": {
                    "type": "array",
                    "description": (
                        "For a missing-input interaction, list the exact tool-schema field "
                        "paths that block execution. Never list optional properties."
                    ),
                    "items": {"type": "string"},
                    "uniqueItems": True,
                },
            },
            "required": ["type", "prompt"],
        },
    },
}


@dataclass
class RequestUserInteractionTool:
    name: str = "request_user_interaction"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: set[str] = None  # type: ignore[assignment]
    concurrency_safe: bool = False
    max_parallelism: Optional[int] = 1
    category: str = "interaction"

    def __post_init__(self) -> None:
        self.schema = REQUEST_USER_INTERACTION_TOOL_SCHEMA
        self.allowed_callers = {"main"}

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Any] = None,
    ) -> ToolOutcome:
        del event_emitter
        if not getattr(context, "supports_user_interaction", True):
            return ToolOutcome(
                content=(
                    "Error: request_user_interaction is unavailable in this run. "
                    "Return a direct text answer for consultation or recommendation requests, "
                    "or explain the missing required input for execution or approval requests."
                ),
                terminal=False,
            )
        optional_rejection = _reject_optional_solver_interaction(args, context=context)
        if optional_rejection is not None:
            return optional_rejection
        interaction = build_user_interaction(args, tool_call_id=tool_call_id)
        final_output = {
            "type": "user_interaction",
            "text": interaction["prompt"],
            "options": interaction["options"],
            "interaction": interaction,
        }
        content = json.dumps(
            {
                "status": "awaiting_input",
                "interaction_id": interaction["id"],
                "type": interaction["type"],
                "prompt": interaction["prompt"],
            },
            ensure_ascii=False,
        )
        return ToolOutcome(
            content=content,
            state_updates={
                "pending_user_interaction": interaction,
                "final_output": final_output,
            },
            terminal=True,
        )

    async def aexecute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Any] = None,
    ) -> ToolOutcome:
        return self.execute(
            args=args,
            context=context,
            tool_call_id=tool_call_id,
            event_emitter=event_emitter,
        )


def _reject_optional_solver_interaction(args: Mapping[str, Any], *, context: ToolContext) -> Optional[ToolOutcome]:
    interaction_type = str(args.get("type") or "").strip().lower()
    if interaction_type not in {"input", "single_choice"}:
        return None

    try:
        from agent.tools.registry import get_solver_tool_specs_for_context

        specs = tuple(
            get_solver_tool_specs_for_context(
                selected_ontology_id=getattr(context, "selected_ontology_id", None),
                selected_ontology_version=getattr(context, "selected_ontology_version", None),
                workspace_id=getattr(context, "workspace_id", None),
            )
        )
    except Exception:
        return None
    if not specs:
        return None

    requested_fields = _requested_field_names(args.get("requested_fields"))
    interaction_text = _interaction_search_text(args)
    optional_matches: set[str] = set()
    required_matches: set[str] = set()
    matching_tools: set[str] = set()

    for spec in specs:
        function = getattr(spec, "schema", {}).get("function", {})
        parameters = function.get("parameters", {}) if isinstance(function, Mapping) else {}
        if not isinstance(parameters, Mapping):
            continue
        tool_optional: set[str] = set()
        tool_required: set[str] = set()
        for path, required, field_schema in _schema_fields(parameters):
            if requested_fields:
                matched = _requested_field_matches(path, requested_fields)
            else:
                matched = _field_signals_match(path, field_schema, interaction_text)
            if not matched:
                continue
            if required:
                tool_required.add(path)
            else:
                tool_optional.add(path)
        if tool_required:
            required_matches.update(tool_required)
        if tool_optional:
            optional_matches.update(tool_optional)
            matching_tools.add(str(getattr(spec, "name", "") or "").strip())

    if not optional_matches or required_matches:
        return None

    tool_names = sorted(name for name in matching_tools if name)
    payload = {
        "status": "interaction_rejected",
        "code": "optional_solver_input",
        "optional_fields": sorted(optional_matches),
        "instruction": (
            "These fields are optional. Do not pause or invent values; omit them and "
            "invoke the matching Solver ToolCard now with all available required arguments."
        ),
    }
    if tool_names:
        payload["solver_tools"] = tool_names
    return ToolOutcome(
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        terminal=False,
    )


def _requested_field_names(value: Any) -> set[str]:
    if not isinstance(value, list):
        return set()
    return {str(item or "").strip() for item in value if isinstance(item, str) and str(item or "").strip()}


def _requested_field_matches(path: str, requested_fields: set[str]) -> bool:
    leaf = path.rsplit(".", 1)[-1].replace("[]", "")
    normalized_path = path.replace("[]", "")
    return any(
        requested == path
        or requested.replace("[]", "") == normalized_path
        or requested.rsplit(".", 1)[-1].replace("[]", "") == leaf
        for requested in requested_fields
    )


def _schema_fields(schema: Mapping[str, Any], *, prefix: str = "") -> Iterable[tuple[str, bool, Mapping[str, Any]]]:
    properties = schema.get("properties")
    if not isinstance(properties, Mapping):
        return
    required_names = {str(name) for name in schema.get("required", []) if isinstance(name, str)}
    for raw_name, raw_field_schema in properties.items():
        if not isinstance(raw_name, str) or not isinstance(raw_field_schema, Mapping):
            continue
        path = "{}.{}".format(prefix, raw_name) if prefix else raw_name
        yield path, raw_name in required_names, raw_field_schema
        field_type = raw_field_schema.get("type")
        if field_type == "object":
            yield from _schema_fields(raw_field_schema, prefix=path)
        elif field_type == "array":
            item_schema = raw_field_schema.get("items")
            if isinstance(item_schema, Mapping):
                yield from _schema_fields(item_schema, prefix=path + "[]")


def _interaction_search_text(args: Mapping[str, Any]) -> str:
    values: list[str] = [str(args.get("prompt") or "")]
    options = args.get("options")
    if isinstance(options, list):
        for option in options:
            if isinstance(option, Mapping):
                values.extend(str(option.get(key) or "") for key in ("id", "label", "value", "description"))
            else:
                values.append(str(option or ""))
    return "\n".join(values).casefold()


def _field_signals_match(path: str, field_schema: Mapping[str, Any], interaction_text: str) -> bool:
    signals = {
        path,
        path.rsplit(".", 1)[-1].replace("[]", ""),
    }
    for key in ("title", "x-display-name"):
        value = field_schema.get(key)
        if isinstance(value, str) and value.strip():
            signals.add(value.strip())
    enum_values = field_schema.get("enum")
    if isinstance(enum_values, list):
        signals.update(
            str(value).strip() for value in enum_values if isinstance(value, str) and len(value.strip()) >= 2
        )
    return any(signal.casefold() in interaction_text for signal in signals if signal)


def build_user_interaction(args: Dict[str, Any], *, tool_call_id: Optional[str] = None) -> Dict[str, Any]:
    interaction_type = str(args.get("type") or "").strip().lower()
    if interaction_type not in INTERACTION_TYPES:
        interaction_type = "input"

    prompt = str(args.get("prompt") or "").strip()
    if not prompt:
        prompt = "请补充信息后继续。"

    options = _normalize_options(args.get("options"), interaction_type=interaction_type)
    if interaction_type == "single_choice" and not options:
        raise ValueError("single_choice interaction requires at least one option")
    option_ids = [str(option.get("id") or "").strip() for option in options]
    if len(option_ids) != len(set(option_ids)):
        raise ValueError("interaction option ids must be unique")
    interaction_id = _build_interaction_id(tool_call_id)
    for option in options:
        option["interaction_id"] = interaction_id
        option["type"] = interaction_type

    interaction: Dict[str, Any] = {
        "id": interaction_id,
        "type": interaction_type,
        "prompt": prompt,
        "options": options,
        "tool_call_id": tool_call_id,
        "created_at_ms": int(time.time() * 1000),
    }

    executed_summary = str(args.get("executed_summary") or "").strip()
    next_plan = str(args.get("next_plan") or "").strip()
    if executed_summary:
        interaction["executed_summary"] = executed_summary
    if next_plan:
        interaction["next_plan"] = next_plan
    return interaction


def _normalize_options(raw_options: Any, *, interaction_type: str) -> List[Dict[str, Any]]:
    if interaction_type == "approval" and not raw_options:
        raw_options = [
            {"id": "continue", "label": "继续", "value": "continue"},
            {"id": "stop", "label": "停止", "value": "stop"},
        ]

    if interaction_type == "input":
        return []

    normalized: List[Dict[str, Any]] = []
    if not isinstance(raw_options, list):
        raw_options = []

    for index, raw in enumerate(raw_options):
        if isinstance(raw, dict):
            label = str(raw.get("label") or raw.get("value") or raw.get("id") or "").strip()
            value = raw.get("value", label)
            description = str(raw.get("description") or "").strip()
            option_id = str(raw.get("id") or _slug(label) or "option_{}".format(index + 1)).strip()
        else:
            label = str(raw or "").strip()
            value = label
            description = ""
            option_id = _slug(label) or "option_{}".format(index + 1)

        if not label:
            label = "选项 {}".format(index + 1)
        option = {
            "id": option_id,
            "label": label,
            "value": value,
        }
        if description:
            option["description"] = description
        normalized.append(option)

    return normalized


def _build_interaction_id(tool_call_id: Optional[str]) -> str:
    suffix = str(tool_call_id or "").strip()
    if not suffix:
        suffix = str(int(time.time() * 1000))
    return "interaction_{}".format(_slug(suffix) or suffix)


def _slug(value: str) -> str:
    text = str(value or "").strip().lower()
    text = re.sub(r"[^a-z0-9_-]+", "_", text)
    text = re.sub(r"_+", "_", text).strip("_")
    return text
