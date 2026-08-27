"""Workspace-scoped runtime capabilities for ontology packages."""

from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from ontology.ontologies import get_runtime_ontology_manifest
from ontology.ontology_catalog import find_asset_file, read_required_asset_text
from workspace.workspaces import get_workspace_paths


DOCUMENT_VERSION = 1
BUSINESS_CHECKPOINT_CAPABILITY = "business_checkpoint"
BUSINESS_CHECKPOINT_TEMPLATE_KEY = "business-checkpoint-template.md"
_document_lock = threading.RLock()
logger = logging.getLogger(__name__)


class OntologyCapabilityStoreError(ValueError):
    """Raised when the capability document cannot satisfy a request."""


class OntologyCapabilityRevisionConflictError(OntologyCapabilityStoreError):
    """Raised when a write targets an obsolete capability revision."""


def _capabilities_path(workspace_id: Optional[str]) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "ontology-capabilities.json"


def _empty_document() -> Dict[str, Any]:
    return {"version": DOCUMENT_VERSION, "ontologies": {}}


def _new_revision() -> str:
    return uuid.uuid4().hex


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _normalize_capability(value: Any) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    return {
        "enabled": payload.get("enabled") is True,
        "revision": str(payload.get("revision") or ""),
        "updated_at": str(payload.get("updated_at") or ""),
    }


def _normalize_document(value: Any) -> Dict[str, Any]:
    if not isinstance(value, dict) or value.get("version") != DOCUMENT_VERSION:
        raise OntologyCapabilityStoreError("Invalid knowledge package capability configuration")
    raw_ontologies = value.get("ontologies")
    if not isinstance(raw_ontologies, dict):
        raise OntologyCapabilityStoreError("Invalid knowledge package capability configuration")
    ontologies: Dict[str, Any] = {}
    for ontology_id, raw_capabilities in raw_ontologies.items():
        if not isinstance(raw_capabilities, dict):
            continue
        capability = raw_capabilities.get(BUSINESS_CHECKPOINT_CAPABILITY)
        if isinstance(capability, dict):
            ontologies[str(ontology_id)] = {
                BUSINESS_CHECKPOINT_CAPABILITY: _normalize_capability(capability),
            }
    return {"version": DOCUMENT_VERSION, "ontologies": ontologies}


def _read_document(workspace_id: Optional[str]) -> Dict[str, Any]:
    path = _capabilities_path(workspace_id)
    if not path.exists():
        return _empty_document()
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OntologyCapabilityStoreError("Failed to read knowledge package capability configuration") from exc
    return _normalize_document(payload)


def _write_document(document: Dict[str, Any], workspace_id: Optional[str]) -> None:
    path = _capabilities_path(workspace_id)
    tmp_path = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(
            json.dumps(_normalize_document(document), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    except OSError as exc:
        raise OntologyCapabilityStoreError("Failed to write knowledge package capability configuration") from exc
    finally:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _validate_ontology_id(ontology_id: Any) -> str:
    resolved = str(ontology_id or "").strip()
    if not resolved or Path(resolved).name != resolved:
        raise OntologyCapabilityStoreError("Invalid ontology_id")
    return resolved


def get_business_checkpoint_capabilities(
    ontology_ids: Iterable[str],
    *,
    workspace_id: Optional[str] = None,
) -> Dict[str, Dict[str, Any]]:
    resolved_ids = [_validate_ontology_id(item) for item in ontology_ids]
    with _document_lock:
        document = _read_document(workspace_id)
    return {
        ontology_id: _normalize_capability(
            document["ontologies"].get(ontology_id, {}).get(BUSINESS_CHECKPOINT_CAPABILITY)
        )
        for ontology_id in resolved_ids
    }


def get_business_checkpoint_capability(
    ontology_id: str,
    *,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved = _validate_ontology_id(ontology_id)
    return get_business_checkpoint_capabilities(
        [resolved],
        workspace_id=workspace_id,
    )[resolved]


def set_business_checkpoint_capability(
    ontology_id: str,
    *,
    enabled: bool,
    expected_revision: str,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    if not isinstance(enabled, bool):
        raise OntologyCapabilityStoreError("enabled must be a boolean")
    resolved = _validate_ontology_id(ontology_id)
    expected = str(expected_revision or "").strip()
    if get_runtime_ontology_manifest(resolved, workspace_id=workspace_id) is None:
        raise OntologyCapabilityStoreError("Knowledge package not found")

    with _document_lock:
        document = _read_document(workspace_id)
        current = _normalize_capability(
            document["ontologies"].get(resolved, {}).get(BUSINESS_CHECKPOINT_CAPABILITY)
        )
        if current["revision"] != expected:
            raise OntologyCapabilityRevisionConflictError(
                "Knowledge package CheckPoint configuration has changed; refresh and retry"
            )
        capability = {
            "enabled": enabled,
            "revision": _new_revision(),
            "updated_at": _utc_now(),
        }
        document["ontologies"][resolved] = {
            BUSINESS_CHECKPOINT_CAPABILITY: capability,
        }
        _write_document(document, workspace_id)
        return dict(capability)


def reset_business_checkpoint_capability(
    ontology_id: str,
    *,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved = _validate_ontology_id(ontology_id)
    capability = {
        "enabled": False,
        "revision": _new_revision(),
        "updated_at": "",
    }
    with _document_lock:
        document = _read_document(workspace_id)
        document["ontologies"][resolved] = {
            BUSINESS_CHECKPOINT_CAPABILITY: capability,
        }
        _write_document(document, workspace_id)
    return dict(capability)


def delete_ontology_capabilities(
    ontology_id: str,
    *,
    workspace_id: Optional[str] = None,
) -> bool:
    resolved = _validate_ontology_id(ontology_id)
    with _document_lock:
        document = _read_document(workspace_id)
        if resolved not in document["ontologies"]:
            return False
        document["ontologies"].pop(resolved, None)
        _write_document(document, workspace_id)
        return True


def inspect_business_checkpoint_template(
    ontology_id: str,
    *,
    ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    resolved = _validate_ontology_id(ontology_id)
    ontology = get_runtime_ontology_manifest(
        resolved,
        version=str(ontology_version or "").strip() or None,
        workspace_id=workspace_id,
    )
    if ontology is None or not ontology.enabled:
        return {"available": False, "error": "Knowledge package is unavailable"}
    path, error = find_asset_file(
        Path(ontology.ontology_dir).resolve(),
        BUSINESS_CHECKPOINT_TEMPLATE_KEY,
    )
    if error or path is None:
        return {"available": False, "error": error or "CheckPoint template does not exist"}
    content, read_error = read_required_asset_text(path, "business CheckPoint template")
    if read_error or not str(content or "").strip():
        if read_error:
            logger.warning(
                "Failed to read business CheckPoint template: ontology_id=%s workspace_id=%s error=%s",
                resolved,
                workspace_id,
                read_error,
            )
            return {"available": False, "error": "CheckPoint template cannot be read"}
        return {"available": False, "error": "CheckPoint template is empty"}
    return {"available": True, "error": ""}


def is_business_checkpoint_available(
    ontology_id: str,
    *,
    ontology_version: Optional[str] = None,
    workspace_id: Optional[str] = None,
) -> bool:
    try:
        capability = get_business_checkpoint_capability(
            ontology_id,
            workspace_id=workspace_id,
        )
        if not capability["enabled"]:
            return False
        return bool(
            inspect_business_checkpoint_template(
                ontology_id,
                ontology_version=ontology_version,
                workspace_id=workspace_id,
            )["available"]
        )
    except (OntologyCapabilityStoreError, ValueError):
        return False
