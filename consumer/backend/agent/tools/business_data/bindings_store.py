"""Ontology-to-business-data connection bindings."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ontology.ontology_catalog import get_ontology_manifest
from workspace.workspaces import get_workspace_paths
from agent.tools.business_data.connections_store import (
    BusinessDataConnectionStoreError,
    get_data_connection,
)


class OntologyDataBindingError(ValueError):
    """Raised when an ontology business data binding is invalid."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _bindings_path(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "ontology-bindings.json"


def _read_document(workspace_id: Optional[str] = None) -> Dict[str, Any]:
    path = _bindings_path(workspace_id)
    if not path.exists():
        return {"bindings": {}}
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {"bindings": {}}
    if not isinstance(payload, dict):
        return {"bindings": {}}
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        bindings = {}
    return {"bindings": bindings}


def _write_document(payload: Dict[str, Any], workspace_id: Optional[str] = None) -> None:
    path = _bindings_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_ontology_id(ontology_id: Any) -> str:
    resolved_id = str(ontology_id or "").strip()
    if not resolved_id:
        raise OntologyDataBindingError("ontology_id 不能为空")
    _ontology, error = get_ontology_manifest(resolved_id)
    if error:
        raise OntologyDataBindingError(error.replace("Error: ", ""))
    return resolved_id


def _pop_ontology_data_binding_record(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> bool:
    resolved_ontology_id = str(ontology_id or "").strip()
    if not resolved_ontology_id:
        return False
    document = _read_document(workspace_id)
    if resolved_ontology_id not in document["bindings"]:
        return False
    document["bindings"].pop(resolved_ontology_id, None)
    _write_document(document, workspace_id)
    return True


def get_ontology_data_binding(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    resolved_ontology_id = str(ontology_id or "").strip()
    if not resolved_ontology_id:
        return None
    binding = _read_document(workspace_id)["bindings"].get(resolved_ontology_id)
    return dict(binding) if isinstance(binding, dict) else None


def set_ontology_data_binding(
    *,
    ontology_id: str,
    connection_id: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved_ontology_id = _validate_ontology_id(ontology_id)
    try:
        connection = get_data_connection(connection_id, workspace_id)
    except BusinessDataConnectionStoreError as exc:
        raise OntologyDataBindingError(str(exc)) from exc
    document = _read_document(workspace_id)
    binding = {
        "connection_id": connection.id,
        "updated_at": _utc_now(),
    }
    document["bindings"][resolved_ontology_id] = binding
    _write_document(document, workspace_id)
    return {
        "ontology_id": resolved_ontology_id,
        **binding,
    }


def delete_ontology_data_binding(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> None:
    resolved_ontology_id = _validate_ontology_id(ontology_id)
    _pop_ontology_data_binding_record(resolved_ontology_id, workspace_id=workspace_id)


def clear_ontology_data_binding_on_delete(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> None:
    """Remove a stored binding when the ontology itself is deleted."""
    _pop_ontology_data_binding_record(ontology_id, workspace_id=workspace_id)


def list_bound_ontology_ids(
    connection_id: str,
    workspace_id: Optional[str] = None,
) -> List[str]:
    resolved_connection_id = str(connection_id or "").strip()
    bindings = _read_document(workspace_id)["bindings"]
    return sorted(
        ontology_id
        for ontology_id, binding in bindings.items()
        if isinstance(binding, dict)
        and str(binding.get("connection_id") or "").strip() == resolved_connection_id
    )
