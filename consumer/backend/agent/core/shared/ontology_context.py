"""Shared selected-ontology context helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional

from ontology.ontologies import (
    build_selected_ontology_preload_context,
    build_selected_ontology_preload_records,
    get_sole_enabled_ontology_id,
)
from utils.read_docs import merge_read_documents_registry


def selected_ontology_id_from_state(state: Mapping[str, Any]) -> Optional[str]:
    user_context = dict(state.get("user_context") or {}) if isinstance(state, Mapping) else {}
    return str(user_context.get("selected_ontology_id") or "").strip() or None


def active_ontology_id_from_state(state: Mapping[str, Any]) -> Optional[str]:
    """Resolve the ontology id to attach for this run.

    An explicit session selection wins. When the session has no selection and
    the workspace has exactly one enabled ontology, that ontology is used so
    its preload context (including the value-domain hint) reaches the model.
    This resolution only affects the injected context; it never writes back
    into ``state["user_context"]`` or session metadata.
    """
    selected_id = selected_ontology_id_from_state(state)
    if selected_id:
        return selected_id
    workspace_id = str(state.get("workspace_id") or "").strip() or None
    return get_sole_enabled_ontology_id(workspace_id)


def selected_ontology_version_from_state(state: Mapping[str, Any]) -> Optional[str]:
    user_context = dict(state.get("user_context") or {}) if isinstance(state, Mapping) else {}
    return (
        str(
            user_context.get("selected_ontology_version")
            or user_context.get("selected_version")
            or ""
        ).strip()
        or None
    )


def get_selected_ontology_context(state: Mapping[str, Any]) -> Optional[str]:
    workspace_id = str(state.get("workspace_id") or "").strip() or None
    if not isinstance(state, dict):
        return build_selected_ontology_preload_context(
            selected_ontology_id=active_ontology_id_from_state(state),
            selected_ontology_version=selected_ontology_version_from_state(state),
            workspace_id=workspace_id,
        )
    selected_ontology_id = active_ontology_id_from_state(state)
    selected_ontology_version = selected_ontology_version_from_state(state)
    cache_key = (workspace_id, selected_ontology_id, selected_ontology_version)
    if state.get("_selected_ontology_context_cache_key") == cache_key:
        return state.get("_selected_ontology_context_cache")
    selected_ontology_context = build_selected_ontology_preload_context(
        selected_ontology_id=selected_ontology_id,
        selected_ontology_version=selected_ontology_version,
        workspace_id=workspace_id,
    )
    state["_selected_ontology_context_cache_key"] = cache_key
    state["_selected_ontology_context_cache"] = selected_ontology_context
    return selected_ontology_context


def hydrate_selected_ontology_read_documents(state: Dict[str, Any]) -> None:
    records = build_selected_ontology_preload_records(
        selected_ontology_id=active_ontology_id_from_state(state),
        selected_ontology_version=selected_ontology_version_from_state(state),
        turn_id=int(state.get("current_turn_id") or 0),
        workspace_id=str(state.get("workspace_id") or "").strip() or None,
    )
    if not records:
        return
    state["read_documents"] = merge_read_documents_registry(
        state.get("read_documents"),
        records,
    )
