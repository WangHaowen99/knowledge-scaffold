"""Shared ToolContext construction."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Optional

from integration.observability import NOOP_OBSERVER
from agent.tools.base import ToolContext
from .interaction_policy import user_interaction_supported_from_context
from .modes import resolve_agent_mode
from .ontology_context import (
    active_ontology_id_from_state,
    selected_ontology_id_from_state,
    selected_ontology_version_from_state,
)


def build_tool_context(
    state: Mapping[str, Any],
    *,
    memory_manager: Any,
    agent_config: Any,
    caller: str,
    agent_iteration: int,
    workspace_id: Optional[str],
    project_root: Optional[str],
    tool_event_emitter: Any = None,
    protocol_event_observer: Any = None,
    subagent_manager: Any = None,
    subagent_lineage: Any = None,
    parent_tool_call_id: Any = None,
    observer: Any = None,
    traceparent: Optional[str] = None,
) -> ToolContext:
    user_context = dict(state.get("user_context") or {}) if isinstance(state, Mapping) else {}
    context_kwargs = dict(
        memory_manager=memory_manager,
        messages=list(state.get("messages") or []),
        run_id=state.get("run_id"),
        session_id=state.get("session_id"),
        turn_id=int(state.get("current_turn_id") or 0),
        user_id=str(state.get("user_id") or "").strip() or None,
        agent_config=agent_config,
        caller=caller,
        traceparent=traceparent or (str(state.get("traceparent") or "").strip() or None),
        agent_mode=resolve_agent_mode(user_context),
        agent_iteration=agent_iteration,
        workspace_id=workspace_id,
        project_root=project_root,
        event_emitter=tool_event_emitter,
        parent_tool_call_id=parent_tool_call_id,
        subagent_manager=subagent_manager,
        subagent_lineage=subagent_lineage,
        protocol_event_observer=protocol_event_observer,
        read_documents=state.get("read_documents"),
        selected_ontology_id=active_ontology_id_from_state(state),
        selected_ontology_version=selected_ontology_version_from_state(state),
        selected_ontology_is_explicit=bool(selected_ontology_id_from_state(state)),
        model_profile_id=str(user_context.get("model_profile_id") or "").strip() or None,
        observer=observer or NOOP_OBSERVER,
        supports_user_interaction=user_interaction_supported_from_context(user_context),
    )
    return ToolContext(**context_kwargs)
