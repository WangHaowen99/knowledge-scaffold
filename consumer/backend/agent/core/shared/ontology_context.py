"""Shared selected-ontology context helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Dict, Optional

from ontology.ontologies import (
    build_selected_ontology_preload_context,
    build_selected_ontology_preload_records,
)
from utils.read_docs import merge_read_documents_registry


def selected_ontology_id_from_state(state: Mapping[str, Any]) -> Optional[str]:
    user_context = dict(state.get("user_context") or {}) if isinstance(state, Mapping) else {}
    return str(user_context.get("selected_ontology_id") or "").strip() or None


def get_selected_ontology_context(state: Mapping[str, Any]) -> Optional[str]:
    if not isinstance(state, dict):
        return build_selected_ontology_preload_context(
            selected_ontology_id=selected_ontology_id_from_state(state),
        )
    selected_ontology_id = selected_ontology_id_from_state(state)
    cache_key = selected_ontology_id
    if state.get("_selected_ontology_context_cache_key") == cache_key:
        return state.get("_selected_ontology_context_cache")
    selected_ontology_context = build_selected_ontology_preload_context(
        selected_ontology_id=selected_ontology_id,
    )
    state["_selected_ontology_context_cache_key"] = cache_key
    state["_selected_ontology_context_cache"] = selected_ontology_context
    return selected_ontology_context


def hydrate_selected_ontology_read_documents(state: Dict[str, Any]) -> None:
    records = build_selected_ontology_preload_records(
        selected_ontology_id=selected_ontology_id_from_state(state),
        turn_id=int(state.get("current_turn_id") or 0),
    )
    if not records:
        return
    state["read_documents"] = merge_read_documents_registry(
        state.get("read_documents"),
        records,
    )
