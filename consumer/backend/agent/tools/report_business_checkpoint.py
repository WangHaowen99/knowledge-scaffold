# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Business-stage CheckPoint reporting tool."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.tools.events import emit_protocol_event
from ontology.capabilities import is_business_checkpoint_available

_STAGE_PATTERN = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_LIFECYCLES = {"started", "completed", "skipped", "failed", "waiting_user"}
_RENDER_INTENTS = {
    "text_summary",
    "table",
    "section_tables",
    "impact_tree",
    "metric_cards",
}
_MAX_DATA_BYTES = 32 * 1024


REPORT_BUSINESS_CHECKPOINT_TOOL_SCHEMA: Dict[str, Any] = {
    "type": "function",
    "function": {
        "name": "report_business_checkpoint",
        "description": (
            "Report the current business stage boundary defined by the loaded operation. "
            "Before using this tool, load asset 'business-checkpoint-template.md' and follow it exactly. "
            "This is an observation event and does not replace business reasoning or request_user_interaction."
        ),
        "parameters": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "stage": {
                    "type": "string",
                    "description": "Stable snake_case business stage key defined by the operation.",
                },
                "stage_title": {
                    "type": "string",
                    "description": "Human-readable stage title defined by the operation.",
                },
                "phase": {
                    "type": "string",
                    "description": (
                        "Lifecycle key formatted as <stage>.<lifecycle>; lifecycle is one of "
                        "started, completed, skipped, failed, waiting_user."
                    ),
                },
                "phase_title": {
                    "type": "string",
                    "description": "Human-readable title for this CheckPoint event.",
                },
                "summary": {
                    "type": "string",
                    "description": "One to three concise sentences about facts from the current stage only.",
                },
                "render_intent": {
                    "type": "string",
                    "enum": sorted(_RENDER_INTENTS),
                    "description": "Optional presentation hint for the upstream business agent.",
                },
                "entity_refs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "maxItems": 20,
                    "description": "Optional references related to the current stage.",
                },
                "data": {
                    "type": "object",
                    "description": "Optional bounded interaction or presentation metadata, not bulk business facts.",
                },
            },
            "required": ["stage", "stage_title", "phase", "phase_title", "summary"],
        },
    },
}


def _required_text(value: Any, field: str, limit: int) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError("{} is required".format(field))
    if len(text) > limit:
        raise ValueError("{} exceeds {} characters".format(field, limit))
    return text


def _build_checkpoint_payload(args: Dict[str, Any], *, tool_call_id: Optional[str]) -> Dict[str, Any]:
    stage = _required_text(args.get("stage"), "stage", 120)
    if not _STAGE_PATTERN.fullmatch(stage):
        raise ValueError("stage must be lower snake_case")
    phase = _required_text(args.get("phase"), "phase", 160)
    prefix = "{}.".format(stage)
    if not phase.startswith(prefix) or phase[len(prefix) :] not in _LIFECYCLES:
        raise ValueError("phase must be <stage>.<supported lifecycle>")

    payload: Dict[str, Any] = {
        "schema_version": 1,
        "tool_name": "report_business_checkpoint",
        "tool_call_id": str(tool_call_id or "").strip(),
        "stage": stage,
        "stage_title": _required_text(args.get("stage_title"), "stage_title", 200),
        "phase": phase,
        "phase_title": _required_text(args.get("phase_title"), "phase_title", 200),
        "summary": _required_text(args.get("summary"), "summary", 1200),
    }
    render_intent = str(args.get("render_intent") or "").strip()
    if render_intent:
        if render_intent not in _RENDER_INTENTS:
            raise ValueError("render_intent is not supported")
        payload["render_intent"] = render_intent

    raw_refs = args.get("entity_refs")
    if raw_refs is not None:
        if not isinstance(raw_refs, list):
            raise ValueError("entity_refs must be an array")
        refs = [str(item or "").strip() for item in raw_refs]
        refs = [item for item in refs if item]
        if len(refs) > 20 or any(len(item) > 200 for item in refs):
            raise ValueError("entity_refs exceeds its limit")
        if refs:
            payload["entity_refs"] = refs

    raw_data = args.get("data")
    if raw_data is not None:
        if not isinstance(raw_data, dict):
            raise ValueError("data must be an object")
        encoded = json.dumps(raw_data, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) > _MAX_DATA_BYTES:
            raise ValueError("data exceeds 32 KB")
        if raw_data:
            payload["data"] = raw_data
    return payload


@dataclass(frozen=True)
class ReportBusinessCheckpointTool:
    name: str = "report_business_checkpoint"
    schema: Dict[str, Any] = None  # type: ignore[assignment]
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = False
    max_parallelism: Optional[int] = 1
    category: str = "business_checkpoint"

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema", REPORT_BUSINESS_CHECKPOINT_TOOL_SCHEMA)
        object.__setattr__(self, "allowed_callers", {"main"})

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del event_emitter
        ontology_id = str(context.selected_ontology_id or "").strip()
        if (
            not context.selected_ontology_is_explicit
            or not ontology_id
            or not is_business_checkpoint_available(
                ontology_id,
                ontology_version=context.selected_ontology_version,
                workspace_id=context.workspace_id,
            )
        ):
            return ToolOutcome(content="Error: report_business_checkpoint is disabled for the selected ontology.")

        payload = _build_checkpoint_payload(args, tool_call_id=tool_call_id)
        emit_protocol_event(
            context=context,
            event_type="business.checkpoint",
            source={"node": "agent_loop", "component": "business_checkpoint"},
            payload=payload,
        )
        lifecycle = payload["phase"].rsplit(".", 1)[-1]
        suffix = (
            " Call request_user_interaction now and stop automatic progress." if lifecycle == "waiting_user" else ""
        )
        return ToolOutcome(content="Business CheckPoint reported: {}.{}".format(payload["phase"], suffix))
