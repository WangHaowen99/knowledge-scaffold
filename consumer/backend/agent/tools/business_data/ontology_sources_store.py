"""Workspace-local ontology business data source storage."""

from __future__ import annotations

import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from agent.tools.business_data.provider_config_codec import (
    decrypt_provider_config,
    document_has_plaintext_secrets,
    encrypt_provider_config,
    normalize_source_config,
    publicize_source_config,
)
from agent.tools.business_data.provider_registry import BusinessDataProviderError, normalize_provider_name
from ontology.ontology_catalog import get_ontology_manifest
from ontology.ontologies import get_ontology_registry
from workspace.workspaces import get_workspace_paths, workspace_scope

DOCUMENT_VERSION = 2
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


def _empty_document() -> Dict[str, Any]:
    return {"version": DOCUMENT_VERSION, "sources": {}}


def _read_json_document(path: Path, *, label: str) -> Dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OntologyDataSourceStoreError("{}无法解析".format(label)) from exc
    if not isinstance(payload, dict):
        raise OntologyDataSourceStoreError("{}格式无效".format(label))
    return payload


def _write_bytes_atomic(path: Path, payload: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        tmp_path.write_bytes(payload)
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"),
    )


def _normalize_document(payload: Dict[str, Any]) -> Dict[str, Any]:
    sources = payload.get("sources")
    if not isinstance(sources, dict):
        sources = {}
    return {
        "version": int(payload.get("version") or DOCUMENT_VERSION),
        "sources": {str(key): value for key, value in sources.items() if isinstance(value, dict)},
    }


def _validate_document(payload: Dict[str, Any]) -> None:
    if payload.get("version") != DOCUMENT_VERSION:
        raise OntologyDataSourceStoreError("本体数据源文件版本不受支持")

    sources = payload.get("sources")
    if not isinstance(sources, dict):
        raise OntologyDataSourceStoreError("本体数据源文件格式无效")
    for ontology_id, source in sources.items():
        if not isinstance(source, dict):
            raise OntologyDataSourceStoreError("本体数据源文件格式无效")
        mode = str(source.get("mode") or "").strip()
        if mode != MODE_DIRECT_PROVIDER:
            raise OntologyDataSourceStoreError(
                "本体数据源文件格式无效: '{}' 的 mode 必须为 {}".format(
                    ontology_id,
                    MODE_DIRECT_PROVIDER,
                )
            )


def _read_document(workspace_id: Optional[str] = None) -> Dict[str, Any]:
    path = _sources_path(workspace_id)
    if not path.exists():
        return _empty_document()
    payload = _read_json_document(path, label="本体数据源文件")
    _validate_document(payload)
    document = _normalize_document(payload)
    if document_has_plaintext_secrets(document):
        document = _encrypt_document(document)
        _write_document(document, workspace_id)
    return document


def _write_document(payload: Dict[str, Any], workspace_id: Optional[str] = None) -> None:
    document = _normalize_document(payload)
    document["version"] = DOCUMENT_VERSION
    _write_json_atomic(_sources_path(workspace_id), document)


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
    return OntologyDataSource(
        ontology_id=ontology_id,
        mode=mode,
        provider=provider,
        config=config,
        database=dict(payload.get("database") or {}),
        instantiation=dict(payload.get("instantiation") or {}),
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
