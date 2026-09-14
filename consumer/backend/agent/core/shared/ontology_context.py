"""Shared selected-ontology context helpers."""

from __future__ import annotations

from collections.abc import Mapping
import hashlib
from pathlib import Path
from typing import Any, Dict, Optional

from ontology.ontologies import (
    build_selected_ontology_preload_context,
    build_selected_ontology_preload_records,
    get_active_ontology_version,
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


def active_ontology_version_from_state(state: Mapping[str, Any]) -> Optional[str]:
    """Resolve the package revision this run operates on.

    Mirrors :func:`active_ontology_id_from_state`. An explicit session version
    always wins and is never upgraded, so a version-locked session stays
    reproducible. When the session carries no version -- which is exactly what
    auto-selecting the sole enabled package produces, since such a session is
    created without a selection and therefore persists no revision -- the
    active version of the resolved package is used. Revision-bound
    capabilities such as solver tool cards resolve to nothing without it, and
    an absent version already means "the active version" everywhere else.
    Like the id resolution, this never writes back into ``user_context`` or
    session metadata.
    """
    selected_version = selected_ontology_version_from_state(state)
    if selected_version:
        return selected_version
    ontology_id = active_ontology_id_from_state(state)
    if not ontology_id:
        return None
    workspace_id = str(state.get("workspace_id") or "").strip() or None
    return get_active_ontology_version(ontology_id, workspace_id)


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
    selected_ontology_id = active_ontology_id_from_state(state)
    turn_id = int(state.get("current_turn_id") or 0)
    preflight_records = state.get("_preflight_selected_ontology_preload_records")
    if isinstance(preflight_records, (list, tuple)):
        records = [
            {**dict(record), "turn_id": turn_id}
            for record in preflight_records
            if isinstance(record, Mapping)
        ]
    else:
        records = build_selected_ontology_preload_records(
            selected_ontology_id=selected_ontology_id,
            selected_ontology_version=selected_ontology_version_from_state(state),
            turn_id=turn_id,
            workspace_id=str(state.get("workspace_id") or "").strip() or None,
        )
    if not records:
        return
    _stamp_preload_evidence_context(
        records,
        workspace_id=str(state.get("workspace_id") or "").strip() or None,
        ontology_id=selected_ontology_id,
    )
    state["read_documents"] = merge_read_documents_registry(
        state.get("read_documents"),
        records,
    )


def _stamp_preload_evidence_context(
    records: list[Dict[str, object]],
    *,
    workspace_id: Optional[str],
    ontology_id: Optional[str],
) -> None:
    """Attach the ready pointer snapshot without opening catalog or index."""
    from config.evidence_config import EvidenceConfig
    from ontology.evidence.manager import (
        EvidenceIndexManagerError,
        active_pointer_is_serviceable,
        read_active_pointer_snapshot,
    )
    from workspace.workspaces import (
        get_current_workspace_id,
        get_workspace_paths,
        get_workspace_project_root,
    )

    selected = str(ontology_id or "").strip()
    workspace = str(workspace_id or "").strip() or get_current_workspace_id()
    if not selected or EvidenceConfig.load().workspace_mode(workspace) == "off":
        return
    try:
        pointer = read_active_pointer_snapshot(get_workspace_paths(workspace).runtime_dir, selected)
    except (ValueError, EvidenceIndexManagerError):
        return
    if not active_pointer_is_serviceable(pointer) or pointer.get("epoch_status") != "ready":
        return

    project_root = get_workspace_project_root(workspace)
    for record in records:
        record.update(
            {
                "ontology_id": selected,
                "mutation_revision": int(pointer.get("mutation_revision") or 0),
                "ontology_epoch": str(pointer.get("expected_epoch") or ""),
            }
        )
        path = Path(project_root) / str(record.get("normalized_path") or record.get("path") or "")
        try:
            record["source_content_sha256"] = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            continue
