#!/usr/bin/env python3
"""Forward-only migration from global to workspace observability configuration.

The migration is startup-safe and deliberately non-blocking in the shared
registry: observability can degrade to the disabled workspace default while the
unchanged global source remains available for a later retry. Legacy performance
log cleanup is reported separately and is always best-effort.

Manual recovery copies are private operator material. They are never consumed
by application reads or an automatic rollback path.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import stat
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from config import system_config as system_config_module
from integration.observability.workspace_config import (
    DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG,
    WorkspaceObservabilityMigrationRequiredError,
    decode_workspace_observability_document,
    workspace_langfuse_secret_context,
)
from utils.logging import LOG_DIR
from utils.security.secrets import (
    ENCRYPTED_SECRET_PREFIX,
    decrypt_secret,
    encrypt_secret,
)
from workspace.workspaces import validate_workspace_id


MIGRATION_ID = "observability-workspace-config-v1"
MIGRATION_VERSION = 1
GLOBAL_LANGFUSE_SECRET_CONTEXT = "observability.langfuse.secret_key"
MARKER_RELATIVE_PATH = Path("config") / "migrations" / f"{MIGRATION_ID}.json"
SYSTEM_CONFIG_RELATIVE_PATH = Path("config") / "system_config.json"
SYSTEM_CONFIG_BACKUP_SUFFIX = f".pre-{MIGRATION_ID}.bak"
LOG_BACKUP_RELATIVE_DIR = (
    Path("config") / "migration-backups" / MIGRATION_ID / "legacy-perf-logs"
)
LOG_BACKUP_SUFFIX = f".pre-{MIGRATION_ID}.bak"
LEGACY_DEFAULT_WORKSPACE_ID = "default"
MARKER_PREPARED = "prepared"
MARKER_COMPLETED = "completed"


@dataclass(frozen=True)
class MigrationProblem:
    code: str
    object_id: str
    detail: str

    def as_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "object_id": self.object_id,
            "detail": self.detail,
        }

    def render(self) -> str:
        return f"{self.code}: {self.detail} [{self.object_id}]"


@dataclass
class ConfigPlan:
    runtime_root: Path
    source_path: Path
    marker_path: Path
    source_bytes: bytes = b""
    source_document: dict[str, Any] = field(default_factory=dict)
    next_document: dict[str, Any] = field(default_factory=dict)
    workspace_config: Optional[dict[str, Any]] = None
    workspace_ids: list[str] = field(default_factory=list)
    workspace_writes: list[str] = field(default_factory=list)
    workspace_preserved: list[str] = field(default_factory=list)
    owned_workspace_ids: list[str] = field(default_factory=list)
    marker: Optional[dict[str, Any]] = None
    legacy_source_present: bool = False
    problems: list[MigrationProblem] = field(default_factory=list)


@dataclass(frozen=True)
class LegacyLog:
    path: Path
    size: int


@dataclass
class StepResult:
    ok: bool
    status: str
    counts: dict[str, int]
    problems: list[MigrationProblem] = field(default_factory=list)
    backup_paths: list[Path] = field(default_factory=list)
    planned_backup_paths: list[Path] = field(default_factory=list)
    source_retained: Optional[bool] = None
    workspace_ids: list[str] = field(default_factory=list)


class _ProblemError(ValueError):
    def __init__(self, problem: MigrationProblem) -> None:
        self.problem = problem
        super().__init__(problem.code)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _object_id(path: Path, runtime_root: Path) -> str:
    try:
        return str(path.absolute().relative_to(runtime_root.absolute()))
    except ValueError:
        return str(path.absolute())


def _problem(code: str, object_id: str, detail: str) -> MigrationProblem:
    return MigrationProblem(code=code, object_id=object_id, detail=detail)


def _operation_problem(
    code: str,
    object_id: str,
    action: str,
    exc: BaseException,
) -> MigrationProblem:
    return _problem(
        code,
        object_id,
        f"{action}失败（{type(exc).__name__}）",
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _ensure_private_file(path: Path) -> None:
    path.chmod(0o600)
    if stat.S_IMODE(path.stat(follow_symlinks=False).st_mode) != 0o600:
        raise OSError("private backup permissions could not be verified")
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_bytes_atomic(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("target must not be a symbolic link")
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            mode,
        )
        stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
        path.chmod(mode)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _read_json_object(
    path: Path,
    *,
    runtime_root: Path,
    invalid_code: str,
    label: str,
) -> tuple[bytes, dict[str, Any]]:
    object_id = _object_id(path, runtime_root)
    if path.is_symlink():
        raise _ProblemError(_problem(invalid_code, object_id, f"{label}不能是符号链接"))
    try:
        metadata = path.stat(follow_symlinks=False)
        if not stat.S_ISREG(metadata.st_mode):
            raise _ProblemError(_problem(invalid_code, object_id, f"{label}不是普通文件"))
        raw_bytes = path.read_bytes()
        payload = json.loads(raw_bytes.decode("utf-8"))
    except _ProblemError:
        raise
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise _ProblemError(
            _operation_problem(invalid_code, object_id, f"读取或解析{label}", exc)
        ) from exc
    if not isinstance(payload, dict):
        raise _ProblemError(_problem(invalid_code, object_id, f"{label}必须是 JSON object"))
    return raw_bytes, payload


def _secret_key_available(runtime_root: Path) -> bool:
    if str(os.getenv("SECRET_ENCRYPTION_KEY") or "").strip():
        return True
    key_path = runtime_root / "config" / "secret.key"
    return key_path.is_file() and not key_path.is_symlink()


def _coerce_bool(value: Any, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise ValueError(f"{field_name} 必须是布尔值")


def _coerce_retention_days(value: Any) -> int:
    if isinstance(value, bool):
        raise ValueError("local.retention_days 必须是整数")
    try:
        result = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("local.retention_days 必须是整数") from exc
    if result < 1:
        raise ValueError("local.retention_days 必须大于等于 1")
    return result


def _normalize_global_observability(
    raw: dict[str, Any],
    *,
    runtime_root: Path,
) -> dict[str, Any]:
    allowed_top = {"enabled", "mode", "local", "langfuse"}
    unknown_top = sorted(set(raw) - allowed_top)
    if unknown_top:
        raise ValueError("全局观测配置包含未知字段")

    normalized = copy.deepcopy(DEFAULT_WORKSPACE_OBSERVABILITY_CONFIG)
    if "enabled" in raw:
        normalized["enabled"] = _coerce_bool(raw.get("enabled"), "enabled")
    if "mode" in raw:
        mode = str(raw.get("mode") or "").strip().lower()
        if mode not in {"local", "langfuse"}:
            raise ValueError("mode 必须是 local 或 langfuse")
        normalized["mode"] = mode

    if "local" in raw:
        local = raw.get("local")
        if not isinstance(local, dict):
            raise ValueError("local 必须是 object")
        if set(local) - {"retention_days"}:
            raise ValueError("local 包含未知字段")
        if "retention_days" in local:
            normalized["local"]["retention_days"] = _coerce_retention_days(
                local.get("retention_days")
            )

    if "langfuse" in raw:
        langfuse = raw.get("langfuse")
        if not isinstance(langfuse, dict):
            raise ValueError("langfuse 必须是 object")
        allowed_langfuse = {"public_key", "secret_key", "base_url", "debug"}
        if set(langfuse) - allowed_langfuse:
            raise ValueError("langfuse 包含未知字段")
        for field_name in ("public_key", "base_url"):
            if field_name in langfuse:
                normalized["langfuse"][field_name] = str(
                    langfuse.get(field_name) or ""
                ).strip()
        if "debug" in langfuse:
            normalized["langfuse"]["debug"] = _coerce_bool(
                langfuse.get("debug"),
                "langfuse.debug",
            )
        raw_secret = str(langfuse.get("secret_key") or "")
        if raw_secret:
            if not _secret_key_available(runtime_root):
                raise _ProblemError(
                    _problem(
                        "SECRET_KEY_REQUIRED",
                        "config/secret.key",
                        "观测 secret 迁移需要可用的加密密钥",
                    )
                )
            try:
                normalized["langfuse"]["secret_key"] = decrypt_secret(
                    raw_secret,
                    context=GLOBAL_LANGFUSE_SECRET_CONTEXT,
                )
                # This is an in-memory key validation only. The key precondition
                # above prevents the encryption helper from creating a key file.
                encrypt_secret(
                    normalized["langfuse"]["secret_key"],
                    context=workspace_langfuse_secret_context("migration-check"),
                )
            except Exception as exc:
                raise _ProblemError(
                    _operation_problem(
                        "SECRET_KEY_INVALID",
                        "config/secret.key",
                        "校验观测 secret 加密密钥",
                        exc,
                    )
                ) from exc
    return normalized


def _validate_workspace_metadata(
    workspace_dir: Path,
    *,
    runtime_root: Path,
) -> tuple[Optional[str], list[MigrationProblem]]:
    problems: list[MigrationProblem] = []
    metadata_path = workspace_dir / "workspace.json"
    object_id = _object_id(metadata_path, runtime_root)
    if workspace_dir.is_symlink():
        return None, [_problem("UNSAFE_WORKSPACE_PATH", object_id, "workspace 目录不能是符号链接")]
    try:
        workspace_id = validate_workspace_id(workspace_dir.name)
    except ValueError:
        return None, [_problem("INVALID_WORKSPACE_ID", object_id, "workspace 目录名无效")]
    if workspace_id == LEGACY_DEFAULT_WORKSPACE_ID:
        return None, [
            _problem(
                "LEGACY_DEFAULT_WORKSPACE_REQUIRES_MIGRATION",
                object_id,
                "旧 default workspace 必须先完成独立迁移",
            )
        ]
    try:
        _, metadata = _read_json_object(
            metadata_path,
            runtime_root=runtime_root,
            invalid_code="INVALID_WORKSPACE_METADATA",
            label="workspace metadata",
        )
    except _ProblemError as exc:
        return None, [exc.problem]
    if metadata.get("id") != workspace_id:
        problems.append(
            _problem(
                "WORKSPACE_ID_CONFLICT",
                object_id,
                "workspace metadata id 与目录名不一致",
            )
        )
    return (workspace_id if not problems else None), problems


def _list_canonical_workspaces(
    runtime_root: Path,
) -> tuple[list[str], list[MigrationProblem]]:
    workspaces_root = runtime_root / "workspaces"
    if not workspaces_root.exists():
        return [], []
    if workspaces_root.is_symlink() or not workspaces_root.is_dir():
        return [], [
            _problem(
                "UNSAFE_WORKSPACES_ROOT",
                _object_id(workspaces_root, runtime_root),
                "workspaces 根目录无效",
            )
        ]

    workspace_ids: list[str] = []
    problems: list[MigrationProblem] = []
    for entry in sorted(workspaces_root.iterdir(), key=lambda item: item.name):
        metadata_path = entry / "workspace.json"
        if not metadata_path.exists() and not metadata_path.is_symlink():
            continue
        workspace_id, entry_problems = _validate_workspace_metadata(
            entry,
            runtime_root=runtime_root,
        )
        problems.extend(entry_problems)
        if workspace_id:
            workspace_ids.append(workspace_id)
    return workspace_ids, problems


def _workspace_config_path(runtime_root: Path, workspace_id: str) -> Path:
    return runtime_root / "workspaces" / workspace_id / "observability_config.json"


def _write_workspace_target(
    runtime_root: Path,
    workspace_id: str,
    config: dict[str, Any],
) -> None:
    """Write a planned target without invoking any configuration read path."""
    raw = copy.deepcopy(config)
    raw["langfuse"]["secret_key"] = encrypt_secret(
        str(raw.get("langfuse", {}).get("secret_key") or ""),
        context=workspace_langfuse_secret_context(workspace_id),
    )
    _write_json_atomic(_workspace_config_path(runtime_root, workspace_id), raw)


def _validate_workspace_config(
    runtime_root: Path,
    workspace_id: str,
) -> dict[str, Any]:
    path = _workspace_config_path(runtime_root, workspace_id)
    object_id = _object_id(path, runtime_root)
    try:
        _, raw = _read_json_object(
            path,
            runtime_root=runtime_root,
            invalid_code="INVALID_WORKSPACE_OBSERVABILITY_TARGET",
            label="workspace observability config",
        )
    except _ProblemError:
        raise

    langfuse = raw.get("langfuse")
    raw_secret = str(langfuse.get("secret_key") or "") if isinstance(langfuse, dict) else ""
    if raw_secret and not raw_secret.startswith(ENCRYPTED_SECRET_PREFIX):
        raise _ProblemError(
            _problem(
                "PLAINTEXT_WORKSPACE_OBSERVABILITY_SECRET",
                object_id,
                "workspace 观测目标仍包含明文 secret",
            )
        )
    if raw_secret and not _secret_key_available(runtime_root):
        raise _ProblemError(
            _problem(
                "SECRET_KEY_REQUIRED",
                "config/secret.key",
                "校验 workspace 观测目标需要加密密钥",
            )
        )
    try:
        return decode_workspace_observability_document(
            workspace_id,
            raw,
            runtime_root=runtime_root,
        )
    except WorkspaceObservabilityMigrationRequiredError as exc:
        raise _ProblemError(
            _problem(
                "WORKSPACE_OBSERVABILITY_TARGET_CONFLICT",
                object_id,
                "workspace 观测目标不是规范格式",
            )
        ) from exc
    except Exception as exc:
        raise _ProblemError(
            _operation_problem(
                "WORKSPACE_OBSERVABILITY_SECRET_INVALID",
                object_id,
                "校验 workspace 观测 secret",
                exc,
            )
        ) from exc


def _marker_path(runtime_root: Path) -> Path:
    return runtime_root / MARKER_RELATIVE_PATH


def _load_marker(runtime_root: Path) -> Optional[dict[str, Any]]:
    path = _marker_path(runtime_root)
    if not path.exists() and not path.is_symlink():
        return None
    _, marker = _read_json_object(
        path,
        runtime_root=runtime_root,
        invalid_code="INVALID_MIGRATION_MARKER",
        label="observability migration marker",
    )
    workspace_ids = marker.get("workspace_ids")
    owned_workspace_ids = marker.get("owned_workspace_ids")
    backup_paths = marker.get("manual_recovery_backups")
    valid_identity = (
        marker.get("migration_id") == MIGRATION_ID
        and marker.get("version") == MIGRATION_VERSION
        and marker.get("status") in {MARKER_PREPARED, MARKER_COMPLETED}
    )
    valid_collections = (
        isinstance(workspace_ids, list)
        and isinstance(owned_workspace_ids, list)
        and isinstance(backup_paths, list)
    )
    if not (valid_identity and valid_collections and marker.get("errors") in (None, [])):
        raise _ProblemError(
            _problem(
                "INVALID_MIGRATION_MARKER",
                _object_id(path, runtime_root),
                "observability migration marker 结构无效",
            )
        )
    normalized_ids: list[str] = []
    for value in workspace_ids:
        try:
            workspace_id = validate_workspace_id(value)
        except ValueError as exc:
            raise _ProblemError(
                _problem(
                    "INVALID_MIGRATION_MARKER",
                    _object_id(path, runtime_root),
                    "marker 包含无效 workspace id",
                )
            ) from exc
        if workspace_id == LEGACY_DEFAULT_WORKSPACE_ID or workspace_id in normalized_ids:
            raise _ProblemError(
                _problem(
                    "INVALID_MIGRATION_MARKER",
                    _object_id(path, runtime_root),
                    "marker workspace id 冲突",
                )
            )
        normalized_ids.append(workspace_id)
    marker = dict(marker)
    marker["workspace_ids"] = normalized_ids
    normalized_owned_ids: list[str] = []
    for value in owned_workspace_ids:
        try:
            workspace_id = validate_workspace_id(value)
        except ValueError as exc:
            raise _ProblemError(
                _problem(
                    "INVALID_MIGRATION_MARKER",
                    _object_id(path, runtime_root),
                    "marker 包含无效的迁移目标 workspace id",
                )
            ) from exc
        if (
            workspace_id == LEGACY_DEFAULT_WORKSPACE_ID
            or workspace_id in normalized_owned_ids
            or workspace_id not in normalized_ids
        ):
            raise _ProblemError(
                _problem(
                    "INVALID_MIGRATION_MARKER",
                    _object_id(path, runtime_root),
                    "marker 迁移目标 workspace id 冲突",
                )
            )
        normalized_owned_ids.append(workspace_id)
    marker["owned_workspace_ids"] = normalized_owned_ids
    normalized_backups: list[str] = []
    for value in backup_paths:
        relative = Path(str(value or ""))
        if not relative.parts or relative.is_absolute() or ".." in relative.parts:
            raise _ProblemError(
                _problem(
                    "INVALID_MIGRATION_MARKER",
                    _object_id(path, runtime_root),
                    "marker 备份路径无效",
                )
            )
        normalized_backups.append(str(relative))
    marker["manual_recovery_backups"] = normalized_backups
    return marker


def _marker_payload(
    runtime_root: Path,
    *,
    status: str,
    workspace_ids: list[str],
    owned_workspace_ids: list[str],
    backup_paths: list[Path],
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    now = _utc_now()
    payload = {
        "migration_id": MIGRATION_ID,
        "version": MIGRATION_VERSION,
        "status": status,
        "prepared_at": (existing or {}).get("prepared_at") or now,
        "workspace_ids": sorted(workspace_ids),
        # These targets were absent when this migration first claimed them.
        # Recording ownership lets a retry validate partial migration output
        # without mistaking pre-existing workspace overrides for conflicts.
        # Existing targets are never overwritten during a retry.
        "owned_workspace_ids": sorted(owned_workspace_ids),
        "manual_recovery_backups": [
            _object_id(path, runtime_root) for path in backup_paths
        ],
        "errors": [],
    }
    if status == MARKER_COMPLETED:
        payload["completed_at"] = (existing or {}).get("completed_at") or now
    return payload


def _plan_config(runtime_root: Path) -> ConfigPlan:
    source_path = runtime_root / SYSTEM_CONFIG_RELATIVE_PATH
    plan = ConfigPlan(
        runtime_root=runtime_root,
        source_path=source_path,
        marker_path=_marker_path(runtime_root),
    )

    try:
        plan.marker = _load_marker(runtime_root)
    except _ProblemError as exc:
        plan.problems.append(exc.problem)
    if plan.marker:
        plan.owned_workspace_ids = list(plan.marker["owned_workspace_ids"])

    if source_path.exists() or source_path.is_symlink():
        try:
            plan.source_bytes, plan.source_document = _read_json_object(
                source_path,
                runtime_root=runtime_root,
                invalid_code="INVALID_SYSTEM_CONFIG",
                label="system config",
            )
        except _ProblemError as exc:
            plan.problems.append(exc.problem)
            return plan

    plan.legacy_source_present = "observability" in plan.source_document
    if not plan.legacy_source_present:
        if plan.marker:
            for workspace_id in plan.marker["workspace_ids"]:
                workspace_dir = runtime_root / "workspaces" / workspace_id
                if not workspace_dir.exists() and not workspace_dir.is_symlink():
                    continue
                validated_id, workspace_problems = _validate_workspace_metadata(
                    workspace_dir,
                    runtime_root=runtime_root,
                )
                plan.problems.extend(workspace_problems)
                if validated_id is None:
                    continue
                try:
                    _validate_workspace_config(runtime_root, workspace_id)
                except _ProblemError as exc:
                    plan.problems.append(exc.problem)
        return plan

    if plan.marker and plan.marker.get("status") == MARKER_COMPLETED:
        plan.problems.append(
            _problem(
                "COMPLETED_MARKER_SOURCE_CONFLICT",
                _object_id(source_path, runtime_root),
                "完成标记存在时不应残留全局观测源配置",
            )
        )

    raw_observability = plan.source_document.get("observability")
    if not isinstance(raw_observability, dict):
        plan.problems.append(
            _problem(
                "INVALID_GLOBAL_OBSERVABILITY",
                _object_id(source_path, runtime_root),
                "全局 observability 必须是 JSON object",
            )
        )
        return plan

    plan.workspace_ids, workspace_problems = _list_canonical_workspaces(runtime_root)
    plan.problems.extend(workspace_problems)
    plan.owned_workspace_ids = [
        workspace_id
        for workspace_id in plan.owned_workspace_ids
        if workspace_id in plan.workspace_ids
    ]
    if not plan.workspace_ids and not workspace_problems:
        plan.problems.append(
            _problem(
                "NO_CANONICAL_WORKSPACES",
                "workspaces",
                "没有可承接全局观测配置的规范 workspace",
            )
        )
    try:
        plan.workspace_config = _normalize_global_observability(
            raw_observability,
            runtime_root=runtime_root,
        )
    except _ProblemError as exc:
        plan.problems.append(exc.problem)
    except ValueError:
        plan.problems.append(
            _problem(
                "INVALID_GLOBAL_OBSERVABILITY",
                _object_id(source_path, runtime_root),
                "全局 observability 字段无效",
            )
        )

    for workspace_id in plan.workspace_ids:
        target = _workspace_config_path(runtime_root, workspace_id)
        if target.exists() or target.is_symlink():
            try:
                persisted = _validate_workspace_config(runtime_root, workspace_id)
            except _ProblemError as exc:
                plan.problems.append(exc.problem)
            else:
                if (
                    workspace_id in plan.owned_workspace_ids
                    and plan.workspace_config is not None
                    and persisted != plan.workspace_config
                ):
                    plan.problems.append(
                        _problem(
                            "WORKSPACE_OBSERVABILITY_TARGET_CONFLICT",
                            _object_id(target, runtime_root),
                            "迁移重试目标与当前全局观测配置不一致，拒绝覆盖",
                        )
                    )
                else:
                    plan.workspace_preserved.append(workspace_id)
        else:
            plan.workspace_writes.append(workspace_id)
            if workspace_id not in plan.owned_workspace_ids:
                plan.owned_workspace_ids.append(workspace_id)

    plan.next_document = dict(plan.source_document)
    plan.next_document.pop("observability", None)
    return plan


def _system_backup_base(path: Path) -> Path:
    return path.with_name(f"{path.name}{SYSTEM_CONFIG_BACKUP_SUFFIX}")


def _candidate_backup_path(base_path: Path, index: int) -> Path:
    return base_path if index == 0 else base_path.with_name(f"{base_path.name}.{index}")


def _create_verified_bytes_backup(
    source_bytes: bytes,
    base_path: Path,
) -> Path:
    for index in range(1000):
        candidate = _candidate_backup_path(base_path, index)
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError("backup target is not a regular file")
            if candidate.read_bytes() == source_bytes:
                _ensure_private_file(candidate)
                return candidate
            continue
        _write_bytes_atomic(candidate, source_bytes)
        if candidate.read_bytes() != source_bytes:
            raise OSError("backup verification failed")
        _ensure_private_file(candidate)
        return candidate
    raise OSError("too many conflicting backup files")


def _write_marker(
    plan: ConfigPlan,
    *,
    status: str,
    backup_paths: list[Path],
    existing: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    marker = _marker_payload(
        plan.runtime_root,
        status=status,
        workspace_ids=plan.workspace_ids or list((existing or {}).get("workspace_ids") or []),
        owned_workspace_ids=plan.owned_workspace_ids,
        backup_paths=backup_paths,
        existing=existing,
    )
    _write_json_atomic(plan.marker_path, marker)
    _, persisted = _read_json_object(
        plan.marker_path,
        runtime_root=plan.runtime_root,
        invalid_code="INVALID_MIGRATION_MARKER",
        label="observability migration marker",
    )
    if persisted != marker:
        raise OSError("marker verification failed")
    return marker


def _config_counts(plan: ConfigPlan) -> dict[str, int]:
    return {
        "workspaces_scanned": len(plan.workspace_ids),
        "workspace_writes_planned": len(plan.workspace_writes),
        "workspace_configs_written": 0,
        "workspace_configs_preserved": len(plan.workspace_preserved),
        "global_sections_removed": 0,
        "markers_written": 0,
    }


def _dry_run_config(plan: ConfigPlan) -> StepResult:
    counts = _config_counts(plan)
    planned_backups = (
        [_system_backup_base(plan.source_path)] if plan.legacy_source_present else []
    )
    if plan.problems:
        status = "dry-run-blocked"
    elif plan.legacy_source_present:
        status = "dry-run-ready"
    elif plan.marker and plan.marker.get("status") == MARKER_PREPARED:
        status = "dry-run-finalize-marker"
    elif plan.marker and plan.marker.get("status") == MARKER_COMPLETED:
        status = "complete"
    else:
        status = "noop"
    return StepResult(
        ok=not plan.problems,
        status=status,
        counts=counts,
        problems=list(plan.problems),
        planned_backup_paths=planned_backups,
        source_retained=plan.legacy_source_present,
        workspace_ids=list(plan.workspace_ids or ((plan.marker or {}).get("workspace_ids") or [])),
    )


def _marker_backup_paths(runtime_root: Path, marker: dict[str, Any]) -> list[Path]:
    results: list[Path] = []
    for value in marker.get("manual_recovery_backups") or []:
        relative = Path(str(value or ""))
        if relative.is_absolute() or ".." in relative.parts:
            raise OSError("marker backup path is invalid")
        results.append(runtime_root / relative)
    return results


def _apply_config(plan: ConfigPlan) -> StepResult:
    counts = _config_counts(plan)
    if plan.problems:
        return StepResult(
            ok=False,
            status="apply-precheck-blocked",
            counts=counts,
            problems=list(plan.problems),
            source_retained=plan.legacy_source_present,
            workspace_ids=list(plan.workspace_ids),
        )

    if not plan.legacy_source_present:
        if not plan.marker:
            return StepResult(
                ok=True,
                status="noop",
                counts=counts,
                source_retained=False,
            )
        try:
            backups = _marker_backup_paths(plan.runtime_root, plan.marker)
        except OSError as exc:
            problem = _operation_problem(
                "INVALID_MIGRATION_MARKER",
                _object_id(plan.marker_path, plan.runtime_root),
                "读取 marker 备份路径",
                exc,
            )
            return StepResult(
                ok=False,
                status="apply-precheck-blocked",
                counts=counts,
                problems=[problem],
                source_retained=False,
                workspace_ids=list(plan.marker["workspace_ids"]),
            )
        if plan.marker.get("status") == MARKER_COMPLETED:
            return StepResult(
                ok=True,
                status="complete",
                counts=counts,
                backup_paths=backups,
                source_retained=False,
                workspace_ids=list(plan.marker["workspace_ids"]),
            )
        try:
            _write_marker(
                plan,
                status=MARKER_COMPLETED,
                backup_paths=backups,
                existing=plan.marker,
            )
            counts["markers_written"] = 1
        except Exception as exc:
            problem = _operation_problem(
                "MARKER_FINALIZE_FAILED",
                _object_id(plan.marker_path, plan.runtime_root),
                "完成 migration marker",
                exc,
            )
            return StepResult(
                ok=False,
                status="marker-finalize-failed",
                counts=counts,
                problems=[problem],
                backup_paths=backups,
                source_retained=False,
                workspace_ids=list(plan.marker["workspace_ids"]),
            )
        return StepResult(
            ok=True,
            status="completed-from-prepared-marker",
            counts=counts,
            backup_paths=backups,
            source_retained=False,
            workspace_ids=list(plan.marker["workspace_ids"]),
        )

    problems: list[MigrationProblem] = []
    backup_paths = (
        _marker_backup_paths(plan.runtime_root, plan.marker) if plan.marker else []
    )
    try:
        progress_marker = _write_marker(
            plan,
            status=MARKER_PREPARED,
            backup_paths=backup_paths,
            existing=plan.marker,
        )
        counts["markers_written"] += 1
    except Exception as exc:
        return StepResult(
            ok=False,
            status="marker-prepare-failed",
            counts=counts,
            problems=[
                _operation_problem(
                    "MIGRATION_MARKER_PREPARE_FAILED",
                    _object_id(plan.marker_path, plan.runtime_root),
                    "登记 workspace 迁移目标",
                    exc,
                )
            ],
            backup_paths=backup_paths,
            source_retained=True,
            workspace_ids=list(plan.workspace_ids),
        )

    written: list[str] = []
    for workspace_id in plan.workspace_writes:
        try:
            _write_workspace_target(
                plan.runtime_root,
                workspace_id,
                plan.workspace_config or {},
            )
            persisted = _validate_workspace_config(plan.runtime_root, workspace_id)
            if persisted != plan.workspace_config:
                raise OSError("persisted target differs from migration plan")
        except _ProblemError as exc:
            problems.append(exc.problem)
        except Exception as exc:
            problems.append(
                _operation_problem(
                    "WORKSPACE_OBSERVABILITY_WRITE_FAILED",
                    _object_id(
                        _workspace_config_path(plan.runtime_root, workspace_id),
                        plan.runtime_root,
                    ),
                    "写入并校验 workspace 观测目标",
                    exc,
                )
            )
        else:
            written.append(workspace_id)
    counts["workspace_configs_written"] = len(written)

    for workspace_id in plan.workspace_ids:
        try:
            _validate_workspace_config(plan.runtime_root, workspace_id)
        except _ProblemError as exc:
            if exc.problem.object_id not in {problem.object_id for problem in problems}:
                problems.append(exc.problem)
    if problems:
        return StepResult(
            ok=False,
            status="workspace-target-write-failed",
            counts=counts,
            problems=problems,
            backup_paths=backup_paths,
            source_retained=True,
            workspace_ids=list(plan.workspace_ids),
        )

    try:
        with system_config_module.system_config_lock():
            latest_bytes = plan.source_path.read_bytes()
            if latest_bytes != plan.source_bytes:
                raise _ProblemError(
                    _problem(
                        "SYSTEM_CONFIG_CHANGED_DURING_MIGRATION",
                        _object_id(plan.source_path, plan.runtime_root),
                        "system config 在迁移提交前发生变化",
                    )
                )
            backup_path = _create_verified_bytes_backup(
                latest_bytes,
                _system_backup_base(plan.source_path),
            )
            if backup_path not in backup_paths:
                backup_paths.append(backup_path)
            prepared = _write_marker(
                plan,
                status=MARKER_PREPARED,
                backup_paths=backup_paths,
                existing=progress_marker,
            )
            counts["markers_written"] += 1
            _write_json_atomic(plan.source_path, plan.next_document)
            _, persisted = _read_json_object(
                plan.source_path,
                runtime_root=plan.runtime_root,
                invalid_code="SYSTEM_CONFIG_REWRITE_FAILED",
                label="migrated system config",
            )
            if persisted != plan.next_document or "observability" in persisted:
                raise OSError("system config verification failed")
        counts["global_sections_removed"] = 1
        _write_marker(
            plan,
            status=MARKER_COMPLETED,
            backup_paths=backup_paths,
            existing=prepared,
        )
        counts["markers_written"] += 1
    except _ProblemError as exc:
        problems.append(exc.problem)
    except Exception as exc:
        source_retained = True
        try:
            _, latest = _read_json_object(
                plan.source_path,
                runtime_root=plan.runtime_root,
                invalid_code="SYSTEM_CONFIG_REWRITE_FAILED",
                label="system config",
            )
            source_retained = "observability" in latest
        except _ProblemError:
            source_retained = False
        problems.append(
            _operation_problem(
                "SYSTEM_CONFIG_COMMIT_FAILED",
                _object_id(plan.source_path, plan.runtime_root),
                "提交全局观测配置迁移",
                exc,
            )
        )
        return StepResult(
            ok=False,
            status="source-commit-failed",
            counts=counts,
            problems=problems,
            backup_paths=backup_paths,
            source_retained=source_retained,
            workspace_ids=list(plan.workspace_ids),
        )

    if problems:
        return StepResult(
            ok=False,
            status="source-commit-failed",
            counts=counts,
            problems=problems,
            backup_paths=backup_paths,
            source_retained=True,
            workspace_ids=list(plan.workspace_ids),
        )
    return StepResult(
        ok=True,
        status="applied",
        counts=counts,
        backup_paths=backup_paths,
        source_retained=False,
        workspace_ids=list(plan.workspace_ids),
    )


def _resolve_old_log_dir(log_dir: str | os.PathLike[str] | None) -> Path:
    if log_dir is not None:
        return Path(log_dir).expanduser()
    configured = os.getenv("LOG_DIR")
    return Path(configured or LOG_DIR).expanduser()


def _inspect_legacy_logs(
    runtime_root: Path,
    log_dir: Path,
) -> tuple[list[LegacyLog], list[MigrationProblem]]:
    if not log_dir.exists() and not log_dir.is_symlink():
        return [], []
    object_id = _object_id(log_dir, runtime_root)
    if log_dir.is_symlink() or not log_dir.is_dir():
        return [], [_problem("UNSAFE_LEGACY_LOG_DIR", object_id, "旧性能日志目录无效")]
    candidates: list[LegacyLog] = []
    problems: list[MigrationProblem] = []
    for path in sorted(log_dir.glob("agent_perf-*"), key=lambda item: item.name):
        source_id = _object_id(path, runtime_root)
        try:
            metadata = path.stat(follow_symlinks=False)
        except OSError as exc:
            problems.append(
                _operation_problem("LEGACY_LOG_INSPECTION_FAILED", source_id, "检查旧性能日志", exc)
            )
            continue
        if path.is_symlink() or not stat.S_ISREG(metadata.st_mode):
            problems.append(
                _problem("UNSAFE_LEGACY_LOG", source_id, "旧性能日志不是普通文件")
            )
            continue
        candidates.append(LegacyLog(path=path, size=metadata.st_size))
    return candidates, problems


def _file_fingerprint(path: Path) -> tuple[int, bytes]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            digest.update(chunk)
    return size, digest.digest()


def _files_equal(left: Path, right: Path) -> bool:
    return _file_fingerprint(left) == _file_fingerprint(right)


def _copy_file_atomic(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise OSError("backup target must not be a symbolic link")
    temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.tmp")
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        target_stream = os.fdopen(descriptor, "wb")
        descriptor = None
        with source.open("rb") as source_stream, target_stream:
            while True:
                chunk = source_stream.read(1024 * 1024)
                if not chunk:
                    break
                target_stream.write(chunk)
            target_stream.flush()
            os.fsync(target_stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, target)
        target.chmod(0o600)
        _fsync_directory(target.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _create_verified_file_backup(source: Path, base_path: Path) -> Path:
    for index in range(1000):
        candidate = _candidate_backup_path(base_path, index)
        if candidate.exists() or candidate.is_symlink():
            if candidate.is_symlink() or not candidate.is_file():
                raise OSError("backup target is not a regular file")
            if _files_equal(source, candidate):
                _ensure_private_file(candidate)
                return candidate
            continue
        _copy_file_atomic(source, candidate)
        if not _files_equal(source, candidate):
            raise OSError("backup verification failed")
        _ensure_private_file(candidate)
        return candidate
    raise OSError("too many conflicting backup files")


def _log_backup_base(runtime_root: Path, source: Path) -> Path:
    backup_dir = runtime_root / LOG_BACKUP_RELATIVE_DIR
    return backup_dir / f"{source.name}{LOG_BACKUP_SUFFIX}"


def _dry_run_logs(
    runtime_root: Path,
    candidates: list[LegacyLog],
    inspection_problems: list[MigrationProblem],
) -> StepResult:
    counts = {
        "legacy_logs_found": len(candidates),
        "legacy_logs_deleted": 0,
        "legacy_log_backups_created": 0,
        "candidate_backup_bytes": sum(item.size for item in candidates),
    }
    return StepResult(
        ok=not inspection_problems,
        status="dry-run-ready" if candidates else "noop",
        counts=counts,
        problems=list(inspection_problems),
        planned_backup_paths=[
            _log_backup_base(runtime_root, item.path) for item in candidates
        ],
    )


def _apply_logs(
    runtime_root: Path,
    candidates: list[LegacyLog],
    inspection_problems: list[MigrationProblem],
) -> StepResult:
    counts = {
        "legacy_logs_found": len(candidates),
        "legacy_logs_deleted": 0,
        "legacy_log_backups_created": 0,
        "candidate_backup_bytes": sum(item.size for item in candidates),
    }
    problems = list(inspection_problems)
    backup_paths: list[Path] = []
    for candidate in candidates:
        source_id = _object_id(candidate.path, runtime_root)
        try:
            backup_path = _create_verified_file_backup(
                candidate.path,
                _log_backup_base(runtime_root, candidate.path),
            )
            backup_paths.append(backup_path)
            counts["legacy_log_backups_created"] += 1
            if not _files_equal(candidate.path, backup_path):
                raise OSError("source changed after backup")
            candidate.path.unlink()
            _fsync_directory(candidate.path.parent)
            counts["legacy_logs_deleted"] += 1
        except Exception as exc:
            problems.append(
                _operation_problem(
                    "LEGACY_LOG_CLEANUP_FAILED",
                    source_id,
                    "备份并清理旧性能日志",
                    exc,
                )
            )
    return StepResult(
        ok=not problems,
        status=("applied" if candidates and not problems else "best-effort-incomplete" if problems else "noop"),
        counts=counts,
        problems=problems,
        backup_paths=backup_paths,
    )


def _render_step(result: StepResult) -> dict[str, Any]:
    return {
        "ok": result.ok,
        "status": result.status,
        "counts": result.counts,
        "workspace_ids": result.workspace_ids,
        "source_retained": result.source_retained,
        "backup_paths": [str(path) for path in result.backup_paths],
        "planned_backup_paths": [str(path) for path in result.planned_backup_paths],
        "error_codes": sorted({problem.code for problem in result.problems}),
        "failed_object_ids": sorted({problem.object_id for problem in result.problems}),
        "errors": [problem.as_dict() for problem in result.problems],
    }


def _writes_stopped_error(
    runtime_root: Path,
    *,
    from_version: str,
    to_version: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": False,
        "dry_run": False,
        "phase": "apply-precheck",
        "scope": {"type": "runtime", "workspace_ids": []},
        "from_version": from_version,
        "to_version": to_version,
        "counts": {},
        "backup_paths": [],
        "error_codes": ["WRITES_NOT_STOPPED"],
        "failed_object_ids": [_object_id(runtime_root, runtime_root)],
        "errors": ["正式迁移前必须确认旧版本已停止写入"],
        "warnings": [],
        "config_migration": {},
        "legacy_log_cleanup": {},
    }


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
    log_dir: str | os.PathLike[str] | None = None,
) -> tuple[int, dict[str, Any]]:
    """Execute a full precheck and then apply the two independent sub-steps."""
    resolved_root = Path(runtime_root).expanduser().resolve()
    if not dry_run and not writes_stopped:
        return 2, _writes_stopped_error(
            resolved_root,
            from_version=from_version,
            to_version=to_version,
        )

    previous_root = os.environ.get("PROJECT_ROOT")
    os.environ["PROJECT_ROOT"] = str(resolved_root)
    try:
        config_plan = _plan_config(resolved_root)
        resolved_log_dir = _resolve_old_log_dir(log_dir)
        log_candidates, log_inspection_problems = _inspect_legacy_logs(
            resolved_root,
            resolved_log_dir,
        )
        if dry_run:
            phase = "dry-run"
            config_result = _dry_run_config(config_plan)
            log_result = _dry_run_logs(
                resolved_root,
                log_candidates,
                log_inspection_problems,
            )
        else:
            phase = "apply"
            config_result = _apply_config(config_plan)
            # Log cleanup has its own complete precheck and remains independent
            # from a retryable config migration failure.
            log_result = _apply_logs(
                resolved_root,
                log_candidates,
                log_inspection_problems,
            )
    finally:
        if previous_root is None:
            os.environ.pop("PROJECT_ROOT", None)
        else:
            os.environ["PROJECT_ROOT"] = previous_root

    config_report = _render_step(config_result)
    log_report = _render_step(log_result)
    config_errors = [problem.render() for problem in config_result.problems]
    log_warnings = [problem.render() for problem in log_result.problems]
    backup_paths = [
        str(path)
        for path in [*config_result.backup_paths, *log_result.backup_paths]
    ]
    report = {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": config_result.ok,
        "dry_run": dry_run,
        "phase": phase,
        "scope": {
            "type": "runtime",
            "workspace_ids": config_result.workspace_ids,
        },
        "from_version": from_version,
        "to_version": to_version,
        "counts": {
            "workspaces_scanned": config_result.counts.get("workspaces_scanned", 0),
            "workspace_configs_written": config_result.counts.get(
                "workspace_configs_written", 0
            ),
            "workspace_configs_preserved": config_result.counts.get(
                "workspace_configs_preserved", 0
            ),
            "legacy_logs_found": log_result.counts.get("legacy_logs_found", 0),
            "legacy_logs_deleted": log_result.counts.get("legacy_logs_deleted", 0),
        },
        "backup_paths": backup_paths,
        "planned_backup_paths": [
            str(path)
            for path in [
                *config_result.planned_backup_paths,
                *log_result.planned_backup_paths,
            ]
        ],
        "error_codes": sorted({problem.code for problem in config_result.problems}),
        "failed_object_ids": sorted(
            {
                problem.object_id
                for problem in [*config_result.problems, *log_result.problems]
            }
        ),
        "errors": config_errors,
        "warnings": log_warnings,
        "config_migration": config_report,
        "legacy_log_cleanup": log_report,
    }
    return (0 if report["ok"] else 2), report
