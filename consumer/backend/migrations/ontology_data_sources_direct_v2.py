#!/usr/bin/env python3
"""Forward-only migration from shared connection references to direct ontology data sources.

This module intentionally lives outside the runtime store. Deployment and the
central startup migration runner invoke it explicitly, so the compatibility
code can be removed as one unit after the supported upgrade window closes.
Successful conversion removes the legacy connection and binding documents;
private per-file copies are retained only as manual recovery material and are
not consumed by an automatic rollback path.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional, TextIO

from agent.tools.business_data.provider_config_codec import (
    decrypt_provider_config,
    document_has_plaintext_secrets,
    encrypt_provider_config,
    normalize_source_config,
)
from agent.tools.business_data.provider_registry import (
    BusinessDataProviderError,
    LEGACY_PROVIDER_ATTESTATION_FIELDS,
    normalize_provider_name,
)

DOCUMENT_VERSION = 2
MODE_DIRECT_PROVIDER = "direct_provider"
MODE_CONNECTION_REF = "connection_ref"
MIGRATION_ID = "ontology-data-sources-direct-v2"
MARKER_FILE = "ontology-data-sources.direct-v2.migration-complete.json"
MANUAL_BACKUP_SUFFIX = ".pre-direct-v2.bak"
logger = logging.getLogger(__name__)


def _configure_cli_logging(stream: TextIO) -> None:
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    logger.propagate = False


class OntologyDataSourceMigrationError(ValueError):
    """Raised when persisted legacy data cannot be migrated safely."""


@dataclass(frozen=True)
class WorkspaceMigrationPaths:
    workspace_dir: Path
    sources: Path
    connections: Path
    bindings: Path
    marker: Path


@dataclass(frozen=True)
class MigratedSource:
    ontology_id: str
    provider: str
    config: Dict[str, Any]
    database: Dict[str, Any]
    instantiation: Dict[str, Any]
    created_at: str
    updated_at: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _workspace_paths(runtime_root: Path, workspace_id: str) -> WorkspaceMigrationPaths:
    workspace_dir = runtime_root / "workspaces" / workspace_id
    return WorkspaceMigrationPaths(
        workspace_dir=workspace_dir,
        sources=workspace_dir / "ontology-data-sources.json",
        connections=workspace_dir / "data-connections.json",
        bindings=workspace_dir / "ontology-bindings.json",
        marker=workspace_dir / MARKER_FILE,
    )


def _read_json_document(path: Path, *, label: str) -> Dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise OntologyDataSourceMigrationError("{}无法解析".format(label)) from exc
    if not isinstance(payload, dict):
        raise OntologyDataSourceMigrationError("{}格式无效".format(label))
    return payload


def write_bytes_atomic(path: Path, payload: bytes, *, mode: Optional[int] = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            tmp_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode if mode is not None else 0o666,
        )
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        if mode is not None:
            tmp_path.chmod(mode)
        os.replace(tmp_path, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        tmp_path.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: Dict[str, Any], *, mode: Optional[int] = None) -> None:
    write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        mode=mode,
    )


def _read_migration_marker(paths: WorkspaceMigrationPaths) -> Dict[str, Any]:
    if not paths.marker.exists():
        return {}
    try:
        return _read_json_document(paths.marker, label="本体数据源迁移标记")
    except OntologyDataSourceMigrationError:
        return {}


def _migration_marker_is_complete(paths: WorkspaceMigrationPaths) -> bool:
    payload = _read_migration_marker(paths)
    return (
        payload.get("migration_id") == MIGRATION_ID
        and payload.get("version") == DOCUMENT_VERSION
        and not payload.get("errors")
    )


def _write_migration_marker(
    paths: WorkspaceMigrationPaths,
    *,
    migrated: int,
    preserved: int,
    removed_legacy_files: int,
    backup_paths: list[Path],
) -> None:
    _write_json_atomic(
        paths.marker,
        {
            "migration_id": MIGRATION_ID,
            "completed_at": _utc_now(),
            "version": DOCUMENT_VERSION,
            "migrated": migrated,
            "preserved": preserved,
            "removed_legacy_files": removed_legacy_files,
            "manual_recovery_backups": [path.name for path in backup_paths],
            "errors": [],
        },
        mode=0o600,
    )


def _read_sources(paths: WorkspaceMigrationPaths) -> Dict[str, Any]:
    if not paths.sources.exists():
        return {"version": DOCUMENT_VERSION, "sources": {}}
    payload = _read_json_document(paths.sources, label="本体数据源文件")
    sources = payload.get("sources")
    if not isinstance(sources, dict) or any(not isinstance(item, dict) for item in sources.values()):
        raise OntologyDataSourceMigrationError("本体数据源文件格式无效")
    try:
        version = int(payload.get("version") or 1)
    except (TypeError, ValueError) as exc:
        raise OntologyDataSourceMigrationError("本体数据源文件 version 无效") from exc
    return {"version": version, "sources": {str(key): dict(value) for key, value in sources.items()}}


def _read_bindings(paths: WorkspaceMigrationPaths) -> Dict[str, Any]:
    if not paths.bindings.exists():
        return {}
    payload = _read_json_document(paths.bindings, label="旧本体绑定文件")
    bindings = payload.get("bindings")
    if not isinstance(bindings, dict):
        raise OntologyDataSourceMigrationError("旧本体绑定文件缺少 bindings")
    return bindings


def _read_connections(paths: WorkspaceMigrationPaths) -> Dict[str, Dict[str, Any]]:
    if not paths.connections.exists():
        return {}
    payload = _read_json_document(paths.connections, label="旧数据连接文件")
    connections = payload.get("connections")
    if not isinstance(connections, list):
        raise OntologyDataSourceMigrationError("旧数据连接文件缺少 connections")
    result: Dict[str, Dict[str, Any]] = {}
    for item in connections:
        if not isinstance(item, dict):
            continue
        connection_id = str(item.get("id") or "").strip()
        if connection_id:
            result[connection_id] = item
    return result


def _manual_backup_path(path: Path) -> Path:
    return path.with_name("{}{}".format(path.name, MANUAL_BACKUP_SUFFIX))


def _planned_manual_backups(paths: WorkspaceMigrationPaths) -> list[Path]:
    result: list[Path] = []
    for source_path in (paths.sources, paths.connections, paths.bindings):
        backup_path = _manual_backup_path(source_path)
        if source_path.exists() or backup_path.exists() or backup_path.is_symlink():
            result.append(backup_path)
    return result


def _existing_manual_backups(paths: WorkspaceMigrationPaths) -> list[Path]:
    result: list[Path] = []
    for source_path in (paths.sources, paths.connections, paths.bindings):
        backup_path = _manual_backup_path(source_path)
        if backup_path.is_file() and not backup_path.is_symlink():
            result.append(backup_path)
    return result


def _create_manual_recovery_backups(paths: WorkspaceMigrationPaths) -> list[Path]:
    backup_paths: list[Path] = []
    for source_path in (paths.sources, paths.connections, paths.bindings):
        backup_path = _manual_backup_path(source_path)
        backup_exists = backup_path.exists() or backup_path.is_symlink()
        if not source_path.exists() and not backup_exists:
            continue
        try:
            if backup_path.is_symlink():
                raise OSError("备份路径不能是符号链接")
            if backup_exists:
                if not backup_path.is_file():
                    raise OSError("备份路径不是普通文件")
                backup_path.chmod(0o600)
            else:
                source_bytes = source_path.read_bytes()
                write_bytes_atomic(backup_path, source_bytes, mode=0o600)
                if backup_path.read_bytes() != source_bytes:
                    raise OSError("备份内容校验失败")
        except OSError as exc:
            raise OntologyDataSourceMigrationError(
                "创建人工恢复备份失败: {}: {}".format(source_path.name, str(exc))
            ) from exc
        backup_paths.append(backup_path)
    return backup_paths


def _marker_manual_backups(paths: WorkspaceMigrationPaths) -> list[Path]:
    payload = _read_migration_marker(paths)
    names = payload.get("manual_recovery_backups")
    if not isinstance(names, list):
        return []
    result: list[Path] = []
    for value in names:
        name = str(value or "").strip()
        if name and Path(name).name == name:
            result.append(paths.workspace_dir / name)
    return result


def _contains_encrypted_value(value: Any) -> bool:
    if isinstance(value, str):
        return value.startswith("enc:v1:")
    if isinstance(value, dict):
        return any(_contains_encrypted_value(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_encrypted_value(item) for item in value)
    return False


def _secret_key_available(runtime_root: Path) -> bool:
    return (
        bool(str(os.getenv("SECRET_ENCRYPTION_KEY") or "").strip())
        or (runtime_root / "config" / "secret.key").is_file()
    )


def _without_legacy_provider_attestations(
    provider: str,
    config: Dict[str, Any],
) -> Dict[str, Any]:
    """Drop fields removed from graph-provider configuration during migration.

    Only the two graph providers ever exposed these fields.  Keeping unrelated
    provider payloads untouched makes the migration fail closed for genuinely
    unknown configuration instead of silently hiding a future field.
    """
    if provider not in {"neo4j", "hugegraph"}:
        return dict(config)
    return {key: value for key, value in config.items() if key not in LEGACY_PROVIDER_ATTESTATION_FIELDS}


def _document_has_legacy_provider_attestations(document: Dict[str, Any]) -> bool:
    """Keep migration retryable when an older marker predates field cleanup."""
    sources = document.get("sources")
    if not isinstance(sources, dict):
        return False
    for payload in sources.values():
        if not isinstance(payload, dict):
            continue
        provider = normalize_provider_name(payload.get("provider"))
        if provider not in {"neo4j", "hugegraph"}:
            continue
        config = payload.get("config")
        if isinstance(config, dict) and LEGACY_PROVIDER_ATTESTATION_FIELDS.intersection(config):
            return True
    return False


def _source_from_direct(ontology_id: str, payload: Dict[str, Any]) -> tuple[MigratedSource, bool]:
    provider = normalize_provider_name(payload.get("provider"))
    decrypted = decrypt_provider_config(provider, dict(payload.get("config") or {}))
    compatible = _without_legacy_provider_attestations(provider, decrypted)
    normalized = normalize_source_config(provider, compatible, existing_config=compatible)
    source = MigratedSource(
        ontology_id=ontology_id,
        provider=provider,
        config=normalized,
        database=dict(payload.get("database") or {}),
        instantiation=dict(payload.get("instantiation") or {}),
        created_at=str(payload.get("created_at") or ""),
        updated_at=str(payload.get("updated_at") or ""),
    )
    return source, normalized != decrypted


def _source_from_connection(
    *,
    ontology_id: str,
    connection_id: str,
    source_payload: Dict[str, Any],
    connection_payload: Dict[str, Any],
) -> MigratedSource:
    provider = normalize_provider_name(connection_payload.get("provider"))
    decrypted = decrypt_provider_config(provider, dict(connection_payload.get("config") or {}))
    compatible = _without_legacy_provider_attestations(provider, decrypted)
    try:
        config = normalize_source_config(provider, compatible, existing_config=compatible)
    except BusinessDataProviderError as exc:
        raise OntologyDataSourceMigrationError(
            "{}: 旧数据连接 {} 无效: {}".format(ontology_id, connection_id, str(exc))
        ) from exc
    now = _utc_now()
    created_at = str(
        source_payload.get("created_at")
        or source_payload.get("updated_at")
        or connection_payload.get("created_at")
        or now
    )
    timestamps = [
        str(source_payload.get("updated_at") or ""),
        str(connection_payload.get("updated_at") or ""),
    ]
    return MigratedSource(
        ontology_id=ontology_id,
        provider=provider,
        config=config,
        database={},
        instantiation={},
        created_at=created_at,
        updated_at=max((value for value in timestamps if value), default=created_at),
    )


def _source_payload(source: MigratedSource) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "mode": MODE_DIRECT_PROVIDER,
        "provider": source.provider,
        "config": encrypt_provider_config(source.provider, source.config),
        "created_at": source.created_at,
        "updated_at": source.updated_at,
    }
    if source.database:
        payload["database"] = dict(source.database)
    if source.instantiation:
        payload["instantiation"] = dict(source.instantiation)
    return payload


def _error_result(
    *,
    dry_run: bool,
    errors: list[str],
    candidate_migrated: int = 0,
    preserved: int = 0,
    backup_paths: Optional[list[Path]] = None,
) -> Dict[str, Any]:
    return {
        "migrated": 0,
        "candidate_migrated": candidate_migrated,
        "preserved": preserved,
        "skipped": False,
        "dry_run": dry_run,
        "errors": errors,
        "removed_legacy_files": 0,
        "backup_paths": [str(path) for path in (backup_paths or [])],
    }


def remove_legacy_files(paths: WorkspaceMigrationPaths) -> int:
    removed = 0
    for path in (paths.bindings, paths.connections):
        if not path.exists():
            continue
        path.unlink()
        removed += 1
    return removed


def _verify_canonical_document(paths: WorkspaceMigrationPaths, expected: Dict[str, Any]) -> None:
    persisted = _read_json_document(paths.sources, label="迁移后的本体数据源文件")
    if persisted != expected or document_has_plaintext_secrets(persisted):
        raise OntologyDataSourceMigrationError("迁移后的本体数据源文件校验失败")


def migrate_workspace(
    runtime_root: Path,
    workspace_id: str,
    *,
    dry_run: bool,
) -> Dict[str, Any]:
    resolved_runtime_root = Path(runtime_root).expanduser().resolve()
    resolved_workspace_id = str(workspace_id or "").strip()
    paths = _workspace_paths(resolved_runtime_root, resolved_workspace_id)
    if not resolved_workspace_id or not paths.workspace_dir.is_dir():
        return {
            "migrated": 0,
            "preserved": 0,
            "skipped": False,
            "dry_run": dry_run,
            "errors": ["workspace 不存在: {}".format(resolved_workspace_id or "<empty>")],
            "removed_legacy_files": 0,
            "backup_paths": [],
        }

    try:
        document = _read_sources(paths)
    except OntologyDataSourceMigrationError as exc:
        return _error_result(dry_run=dry_run, errors=[str(exc)])

    raw_sources = document["sources"]
    marker_complete = (
        paths.sources.is_file()
        and document.get("version") == DOCUMENT_VERSION
        and not document_has_plaintext_secrets(document)
        and all(
            str(payload.get("mode") or MODE_DIRECT_PROVIDER).strip() == MODE_DIRECT_PROVIDER
            for payload in raw_sources.values()
        )
        and not _document_has_legacy_provider_attestations(document)
        and _migration_marker_is_complete(paths)
    )
    try:
        bindings = {} if marker_complete else _read_bindings(paths)
    except OntologyDataSourceMigrationError as exc:
        return _error_result(dry_run=dry_run, errors=[str(exc)])

    # A newly created workspace has neither a source document nor either legacy
    # data file. Treat it as a true no-op instead of creating a migration marker
    # on every startup. A canonical source document still receives a marker so
    # subsequent starts can skip all compatibility reads.
    if not paths.sources.exists() and not bindings and not paths.connections.exists():
        return {
            "migrated": 0,
            "preserved": 0,
            "skipped": True,
            "dry_run": dry_run,
            "errors": [],
            "removed_legacy_files": 0,
            "backup_paths": [],
        }

    pending_connection_ids: set[str] = set()
    for payload in raw_sources.values():
        if str(payload.get("mode") or MODE_DIRECT_PROVIDER).strip() == MODE_CONNECTION_REF:
            connection_id = str(payload.get("connection_id") or "").strip()
            if connection_id:
                pending_connection_ids.add(connection_id)
    for ontology_id, binding in bindings.items():
        if str(ontology_id) in raw_sources or not isinstance(binding, dict):
            continue
        connection_id = str(binding.get("connection_id") or "").strip()
        if connection_id:
            pending_connection_ids.add(connection_id)

    try:
        connections = _read_connections(paths) if pending_connection_ids else {}
    except OntologyDataSourceMigrationError as exc:
        return _error_result(dry_run=dry_run, errors=[str(exc)])

    if (_contains_encrypted_value(document) or _contains_encrypted_value(connections)) and not _secret_key_available(
        resolved_runtime_root
    ):
        return _error_result(
            dry_run=dry_run,
            errors=["检测到已加密数据源密钥，但 SECRET_ENCRYPTION_KEY 和 config/secret.key 均不可用"],
        )

    migrated = 0
    preserved = 0
    normalized_changed = False
    errors: list[str] = []
    next_sources: Dict[str, MigratedSource] = {}

    for ontology_id, payload in sorted(raw_sources.items()):
        resolved_ontology_id = str(ontology_id or "").strip()
        if not resolved_ontology_id:
            errors.append("<empty>: ontology_id 不能为空")
            continue
        mode = str(payload.get("mode") or MODE_DIRECT_PROVIDER).strip() or MODE_DIRECT_PROVIDER
        if mode == MODE_DIRECT_PROVIDER:
            try:
                source, changed = _source_from_direct(resolved_ontology_id, payload)
            except (BusinessDataProviderError, TypeError, ValueError) as exc:
                errors.append("{}: {}".format(resolved_ontology_id, str(exc)))
                continue
            next_sources[resolved_ontology_id] = source
            normalized_changed = normalized_changed or changed
            preserved += 1
            continue
        if mode != MODE_CONNECTION_REF:
            errors.append("{}: 不支持的数据源连接模式: {}".format(resolved_ontology_id, mode))
            continue
        connection_id = str(payload.get("connection_id") or "").strip()
        if not connection_id:
            errors.append("{}: connection_id 不能为空".format(resolved_ontology_id))
            continue
        connection = connections.get(connection_id)
        if connection is None:
            errors.append("{}: 业务数据连接不存在: {}".format(resolved_ontology_id, connection_id))
            continue
        try:
            next_sources[resolved_ontology_id] = _source_from_connection(
                ontology_id=resolved_ontology_id,
                connection_id=connection_id,
                source_payload=payload,
                connection_payload=connection,
            )
        except (OntologyDataSourceMigrationError, TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        migrated += 1

    for ontology_id, binding in sorted(bindings.items(), key=lambda item: str(item[0])):
        resolved_ontology_id = str(ontology_id or "").strip()
        if not resolved_ontology_id:
            errors.append("<empty>: ontology_id 不能为空")
            continue
        if resolved_ontology_id in raw_sources:
            continue
        if not isinstance(binding, dict):
            errors.append("{}: binding 格式无效".format(resolved_ontology_id))
            continue
        connection_id = str(binding.get("connection_id") or "").strip()
        if not connection_id:
            errors.append("{}: connection_id 不能为空".format(resolved_ontology_id))
            continue
        if not (paths.workspace_dir / "ontologies" / resolved_ontology_id / "manifest.yaml").is_file():
            errors.append("{}: 本体不存在".format(resolved_ontology_id))
            continue
        connection = connections.get(connection_id)
        if connection is None:
            errors.append("{}: 业务数据连接不存在: {}".format(resolved_ontology_id, connection_id))
            continue
        try:
            next_sources[resolved_ontology_id] = _source_from_connection(
                ontology_id=resolved_ontology_id,
                connection_id=connection_id,
                source_payload=binding,
                connection_payload=connection,
            )
        except (OntologyDataSourceMigrationError, TypeError, ValueError) as exc:
            errors.append(str(exc))
            continue
        migrated += 1

    if errors:
        return _error_result(
            dry_run=dry_run,
            errors=errors,
            candidate_migrated=migrated,
            preserved=preserved,
        )

    raw_modes = {
        str(payload.get("mode") or MODE_DIRECT_PROVIDER).strip() or MODE_DIRECT_PROVIDER
        for payload in raw_sources.values()
    }
    needs_write = (
        migrated > 0
        or document.get("version") != DOCUMENT_VERSION
        or bool(raw_modes - {MODE_DIRECT_PROVIDER})
        or document_has_plaintext_secrets(document)
        or normalized_changed
        or paths.connections.exists()
        or paths.bindings.exists()
    )
    if dry_run:
        backup_paths = _planned_manual_backups(paths) if needs_write else []
        return {
            "migrated": migrated,
            "preserved": preserved,
            "skipped": not bool(needs_write),
            "dry_run": True,
            "errors": [],
            "removed_legacy_files": sum(int(path.exists()) for path in (paths.bindings, paths.connections)),
            "backup_paths": [str(path) for path in backup_paths],
        }

    removed_legacy_files = 0
    backup_paths: list[Path] = []
    if needs_write:
        try:
            backup_paths = _create_manual_recovery_backups(paths)
        except OntologyDataSourceMigrationError as exc:
            return _error_result(
                dry_run=False,
                errors=[str(exc)],
                candidate_migrated=migrated,
                preserved=preserved,
                backup_paths=_existing_manual_backups(paths),
            )
        try:
            serialized_sources = {
                ontology_id: _source_payload(source) for ontology_id, source in sorted(next_sources.items())
            }
        except (BusinessDataProviderError, TypeError, ValueError) as exc:
            return {
                "migrated": 0,
                "candidate_migrated": migrated,
                "preserved": preserved,
                "skipped": False,
                "dry_run": False,
                "errors": ["写入前加密失败: {}".format(str(exc))],
                "removed_legacy_files": 0,
                "backup_paths": [str(path) for path in backup_paths],
            }
        canonical_document = {"version": DOCUMENT_VERSION, "sources": serialized_sources}
        try:
            _write_json_atomic(paths.sources, canonical_document)
            _verify_canonical_document(paths, canonical_document)
            removed_legacy_files = remove_legacy_files(paths)
        except (OSError, OntologyDataSourceMigrationError) as exc:
            return _error_result(
                dry_run=False,
                errors=["迁移执行失败: {}".format(str(exc))],
                candidate_migrated=migrated,
                preserved=preserved,
                backup_paths=backup_paths,
            )

    if marker_complete and not needs_write:
        backup_paths = _marker_manual_backups(paths)
        return {
            "migrated": 0,
            "preserved": preserved,
            "skipped": True,
            "dry_run": False,
            "errors": [],
            "removed_legacy_files": 0,
            "backup_paths": [str(path) for path in backup_paths],
        }

    _write_migration_marker(
        paths,
        migrated=migrated,
        preserved=preserved,
        removed_legacy_files=removed_legacy_files,
        backup_paths=backup_paths,
    )
    return {
        "migrated": migrated,
        "preserved": preserved,
        "skipped": not bool(needs_write),
        "dry_run": False,
        "errors": [],
        "removed_legacy_files": removed_legacy_files,
        "backup_paths": [str(path) for path in backup_paths],
    }


def migrate_all_workspaces(runtime_root: Path, *, dry_run: bool) -> Dict[str, Any]:
    resolved_root = Path(runtime_root).expanduser().resolve()
    workspaces_root = resolved_root / "workspaces"
    workspace_dirs = (
        sorted((path for path in workspaces_root.iterdir() if path.is_dir()), key=lambda path: path.name)
        if workspaces_root.is_dir()
        else []
    )
    results: Dict[str, Any] = {"workspaces": {}, "errors": []}
    for workspace_dir in workspace_dirs:
        try:
            result = migrate_workspace(resolved_root, workspace_dir.name, dry_run=dry_run)
        except Exception as exc:
            result = {
                "migrated": 0,
                "preserved": 0,
                "skipped": False,
                "dry_run": dry_run,
                "errors": ["迁移执行失败: {}".format(str(exc))],
                "removed_legacy_files": 0,
                "backup_paths": [],
            }
        results["workspaces"][workspace_dir.name] = result
        results["errors"].extend("{}: {}".format(workspace_dir.name, message) for message in result.get("errors", []))
    return results


def migrate_legacy_ontology_data_sources(
    workspace_id: Optional[str] = None,
    *,
    dry_run: bool = False,
    runtime_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Compatibility name for focused tests; the runtime store does not import it."""
    root = Path(runtime_root or os.getenv("PROJECT_ROOT") or ".onto-runtime")
    return migrate_workspace(root, workspace_id or "default", dry_run=dry_run)


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, Dict[str, Any]]:
    resolved_root = Path(runtime_root).expanduser().resolve()
    if not dry_run and not writes_stopped:
        return 2, {
            "schema_version": 1,
            "migration_id": MIGRATION_ID,
            "ok": False,
            "dry_run": False,
            "from_version": from_version,
            "to_version": to_version,
            "error": {"code": "WRITES_NOT_STOPPED"},
            "workspaces": {},
            "errors": ["正式迁移前必须确认旧版本已停止写入"],
        }

    previous_root = os.environ.get("PROJECT_ROOT")
    os.environ["PROJECT_ROOT"] = str(resolved_root)
    try:
        if dry_run:
            phase = "dry_run"
            results = migrate_all_workspaces(resolved_root, dry_run=True)
        else:
            preview = migrate_all_workspaces(resolved_root, dry_run=True)
            if preview["errors"]:
                phase = "apply_precheck"
                results = preview
            else:
                phase = "apply"
                results = migrate_all_workspaces(resolved_root, dry_run=False)
    finally:
        if previous_root is None:
            os.environ.pop("PROJECT_ROOT", None)
        else:
            os.environ["PROJECT_ROOT"] = previous_root

    report = {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": not results["errors"],
        "dry_run": dry_run,
        "phase": phase,
        "from_version": from_version,
        "to_version": to_version,
        "workspaces": results["workspaces"],
        "errors": results["errors"],
    }
    return (0 if report["ok"] else 2), report


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default="")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm-writes-stopped", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    _configure_cli_logging(sys.stdout)
    args = _build_parser().parse_args(argv)
    code, report = execute_migration(
        args.runtime_root,
        dry_run=args.dry_run,
        writes_stopped=args.confirm_writes_stopped,
        from_version=args.from_version,
        to_version=args.to_version,
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.report:
        write_bytes_atomic(args.report, rendered.encode("utf-8"), mode=0o600)
    logger.info(rendered.rstrip("\n"))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
