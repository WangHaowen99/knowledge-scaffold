"""User interaction tool for ontology execution checkpoints."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

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
