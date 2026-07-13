"""Workspace-local ontology business data source storage."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from utils.logging import get_logger
from ontology.ontology_catalog import get_ontology_manifest
from ontology.ontologies import get_ontology_registry
from workspace.workspaces import get_workspace_paths, list_workspaces, workspace_scope
from agent.tools.business_data.connections_store import BusinessDataConnectionStoreError, get_data_connection
from agent.tools.business_data.provider_config_codec import (
    decrypt_provider_config,
    document_has_plaintext_secrets,
    encrypt_provider_config,
    normalize_source_config,
    publicize_source_config,
)
from agent.tools.business_data.provider_registry import BusinessDataProviderError, normalize_provider_name


logger = get_logger("business_data.ontology_sources")

DOCUMENT_VERSION = 1
MODE_DIRECT_PROVIDER = "direct_provider"
MODE_INSTANTIATED_CHANNEL = "instantiated_channel"
SUPPORTED_WRITE_MODES = {MODE_DIRECT_PROVIDER}


@dataclass(frozen=True)
class OntologyDataSource:
    ontology_id: str
    mode: str
    provider: str
    config: Dict[str, Any]
    database: Dict[str, Any]
    instantiation: Dict[str, Any]
    created_at: str
    updated_at: str


class OntologyDataSourceStoreError(ValueError):
    """Raised when ontology data source storage cannot satisfy a request."""


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _sources_path(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "ontology-data-sources.json"


def _legacy_bindings_path(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "ontology-bindings.json"


def _empty_document() -> Dict[str, Any]:
    return {"version": DOCUMENT_VERSION, "sources": {}}


def _read_json_document(path: Path) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            payload = json.load(f)
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _normalize_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    return {
        "version": int(payload.get("version") or DOCUMENT_VERSION),
        "sources": {key: value for key, value in sources.items() if isinstance(value, dict)},
    }


def _read_document(
    workspace_id: Optional[str] = None,
    *,
    migrate_if_missing: bool = True,
) -> Dict[str, Any]:
    path = _sources_path(workspace_id)
    if not path.exists() and migrate_if_missing:
        migrate_legacy_ontology_data_sources(workspace_id=workspace_id)
    document = _normalize_document(_read_json_document(path))
    if document_has_plaintext_secrets(document):
        document = _encrypt_document(document)
        _write_document(document, workspace_id)
    return document


def _write_document(payload: Dict[str, Any], workspace_id: Optional[str] = None) -> None:
    document = _normalize_document(payload)
    path = _sources_path(workspace_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")


def _validate_ontology_id(ontology_id: Any, *, workspace_id: Optional[str] = None) -> str:
    resolved_id = str(ontology_id or "").strip()
    if not resolved_id:
        raise OntologyDataSourceStoreError("ontology_id 不能为空")
    with workspace_scope(workspace_id):
        registry = get_ontology_registry()
        registry.refresh()
        _ontology, error = get_ontology_manifest(resolved_id)
    if error:
        raise OntologyDataSourceStoreError(error.replace("Error: ", ""))
    return resolved_id


def _normalize_mode(value: Any) -> str:
    mode = str(value or MODE_DIRECT_PROVIDER).strip() or MODE_DIRECT_PROVIDER
    if mode == MODE_INSTANTIATED_CHANNEL:
        raise OntologyDataSourceStoreError("{} 连接模式尚未启用".format(MODE_INSTANTIATED_CHANNEL))
    if mode not in SUPPORTED_WRITE_MODES:
        raise OntologyDataSourceStoreError("不支持的数据源连接模式: {}".format(mode))
    return mode


def _from_payload(ontology_id: str, payload: Dict[str, Any]) -> OntologyDataSource:
    mode = str(payload.get("mode") or MODE_DIRECT_PROVIDER).strip() or MODE_DIRECT_PROVIDER
    provider = normalize_provider_name(payload.get("provider"))
    config = decrypt_provider_config(provider, dict(payload.get("config") or {})) if provider else {}
    database = dict(payload.get("database") or {})
    instantiation = dict(payload.get("instantiation") or {})
    return OntologyDataSource(
        ontology_id=ontology_id,
        mode=mode,
        provider=provider,
        config=config,
        database=database,
        instantiation=instantiation,
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )


def _to_payload(source: OntologyDataSource) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "mode": source.mode,
        "provider": source.provider,
        "config": encrypt_provider_config(source.provider, dict(source.config)),
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }
    if source.database:
        payload["database"] = dict(source.database)
    if source.instantiation:
        payload["instantiation"] = dict(source.instantiation)
    return payload


def _encrypt_document(document: Dict[str, Any]) -> Dict[str, Any]:
    next_sources: Dict[str, Any] = {}
    for ontology_id, payload in document.get("sources", {}).items():
        if not isinstance(payload, dict):
            continue
        next_sources[str(ontology_id)] = _to_payload(_from_payload(str(ontology_id), payload))
    return {"version": DOCUMENT_VERSION, "sources": next_sources}


def publicize_ontology_data_source(source: OntologyDataSource) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "mode": source.mode,
        "provider": source.provider,
        "config": publicize_source_config(source.provider, source.config),
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }
    if source.mode == MODE_INSTANTIATED_CHANNEL:
        payload["instantiation"] = dict(source.instantiation)
    return payload


def get_ontology_data_source(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> Optional[OntologyDataSource]:
    resolved_id = str(ontology_id or "").strip()
    if not resolved_id:
        return None
    payload = _read_document(workspace_id)["sources"].get(resolved_id)
    if not isinstance(payload, dict):
        return None
    return _from_payload(resolved_id, payload)


def set_ontology_data_source(
    *,
    ontology_id: str,
    data: Dict[str, Any],
    workspace_id: Optional[str] = None,
) -> OntologyDataSource:
    resolved_ontology_id = _validate_ontology_id(ontology_id, workspace_id=workspace_id)
    mode = _normalize_mode((data or {}).get("mode"))
    provider = normalize_provider_name((data or {}).get("provider"))

    document = _read_document(workspace_id)
    existing_payload = document["sources"].get(resolved_ontology_id)
    existing_source = (
        _from_payload(resolved_ontology_id, existing_payload)
        if isinstance(existing_payload, dict)
        else None
    )
    existing_config = (
        existing_source.config
        if existing_source is not None
        and existing_source.mode == mode
        and existing_source.provider == provider
        else None
    )
    try:
        config = normalize_source_config(
            provider,
            (data or {}).get("config") or {},
            existing_config=existing_config,
        )
    except BusinessDataProviderError as exc:
        raise OntologyDataSourceStoreError(str(exc)) from exc

    now = _utc_now()
    source = OntologyDataSource(
        ontology_id=resolved_ontology_id,
        mode=mode,
        provider=provider,
        config=config,
        database={},
        instantiation={},
        created_at=existing_source.created_at if existing_source else now,
        updated_at=now,
    )
    document["sources"][resolved_ontology_id] = _to_payload(source)
    _write_document(document, workspace_id)
    return source


def build_unsaved_ontology_data_source(
    *,
    ontology_id: str,
    data: Dict[str, Any],
    workspace_id: Optional[str] = None,
) -> OntologyDataSource:
    resolved_ontology_id = _validate_ontology_id(ontology_id, workspace_id=workspace_id)
    mode = _normalize_mode((data or {}).get("mode"))
    provider = normalize_provider_name((data or {}).get("provider"))
    existing_source = get_ontology_data_source(resolved_ontology_id, workspace_id=workspace_id)
    existing_config = (
        existing_source.config
        if existing_source is not None
        and existing_source.mode == mode
        and existing_source.provider == provider
        else None
    )
    try:
        config = normalize_source_config(
            provider,
            (data or {}).get("config") or {},
            existing_config=existing_config,
        )
    except BusinessDataProviderError as exc:
        raise OntologyDataSourceStoreError(str(exc)) from exc

    now = _utc_now()
    return OntologyDataSource(
        ontology_id=resolved_ontology_id,
        mode=mode,
        provider=provider,
        config=config,
        database={},
        instantiation={},
        created_at=existing_source.created_at if existing_source else now,
        updated_at=now,
    )


def delete_ontology_data_source(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> bool:
    resolved_id = str(ontology_id or "").strip()
    if not resolved_id:
        return False
    document = _read_document(workspace_id)
    if resolved_id not in document["sources"]:
        return False
    document["sources"].pop(resolved_id, None)
    _write_document(document, workspace_id)
    return True


def clear_ontology_data_source_on_delete(
    ontology_id: str,
    workspace_id: Optional[str] = None,
) -> None:
    delete_ontology_data_source(ontology_id, workspace_id=workspace_id)


def build_ontology_business_data_summary(
    ontology_id: str,
    *,
    workspace_id: Optional[str] = None,
) -> Dict[str, Any]:
    source = get_ontology_data_source(ontology_id, workspace_id=workspace_id)
    if source is None:
        return {
            "configured": False,
            "ready": False,
            "mode": "",
            "provider": "",
            "updated_at": "",
        }
    ready = source.mode == MODE_DIRECT_PROVIDER and bool(source.provider)
    return {
        "configured": True,
        "ready": ready,
        "mode": source.mode,
        "provider": source.provider if ready else "",
        "updated_at": source.updated_at,
    }


def _read_legacy_bindings(workspace_id: Optional[str]) -> Dict[str, Any]:
    payload = _read_json_document(_legacy_bindings_path(workspace_id))
    bindings = payload.get("bindings")
    return bindings if isinstance(bindings, dict) else {}


def migrate_legacy_ontology_data_sources(workspace_id: Optional[str] = None) -> Dict[str, Any]:
    path = _sources_path(workspace_id)
    if path.exists():
        return {"migrated": 0, "skipped": True, "errors": []}

    bindings = _read_legacy_bindings(workspace_id)
    if not bindings:
        return {"migrated": 0, "skipped": True, "errors": []}

    document = _empty_document()
    migrated = 0
    errors: list[str] = []
    with workspace_scope(workspace_id):
        registry = get_ontology_registry()
        registry.refresh()
        for ontology_id, binding in sorted(bindings.items()):
            if not isinstance(binding, dict):
                continue
            resolved_ontology_id = str(ontology_id or "").strip()
            connection_id = str(binding.get("connection_id") or "").strip()
            if not resolved_ontology_id or not connection_id:
                continue
            _ontology, ontology_error = get_ontology_manifest(resolved_ontology_id)
            if ontology_error:
                errors.append("{}: {}".format(resolved_ontology_id, ontology_error))
                continue
            try:
                connection = get_data_connection(connection_id, workspace_id=workspace_id)
            except BusinessDataConnectionStoreError as exc:
                errors.append("{}: {}".format(resolved_ontology_id, str(exc)))
                continue
            now = _utc_now()
            source = OntologyDataSource(
                ontology_id=resolved_ontology_id,
                mode=MODE_DIRECT_PROVIDER,
                provider=connection.provider,
                config=dict(connection.config),
                database={},
                instantiation={},
                created_at=str(binding.get("updated_at") or connection.created_at or now),
                updated_at=str(binding.get("updated_at") or connection.updated_at or now),
            )
            document["sources"][resolved_ontology_id] = _to_payload(source)
            migrated += 1

    if migrated:
        _write_document(document, workspace_id)
    if errors:
        logger.warning(
            "Legacy ontology data source migration completed with warnings: workspace_id=%s errors=%s",
            workspace_id or "default",
            errors,
        )
    return {"migrated": migrated, "skipped": False, "errors": errors}


def migrate_legacy_ontology_data_sources_for_all_workspaces() -> Dict[str, Any]:
    results: Dict[str, Any] = {"workspaces": {}, "errors": []}
    for workspace in list_workspaces():
        workspace_id = str(workspace.get("id") or "").strip()
        if not workspace_id:
            continue
        try:
            results["workspaces"][workspace_id] = migrate_legacy_ontology_data_sources(
                workspace_id=workspace_id,
            )
        except Exception as exc:
            message = "{}: {}".format(workspace_id, str(exc))
            results["errors"].append(message)
            logger.warning(
                "Legacy ontology data source migration failed: workspace_id=%s",
                workspace_id,
                exc_info=True,
            )
    return results
