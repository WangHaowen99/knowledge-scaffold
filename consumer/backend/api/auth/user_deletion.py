"""Crash-safe consumer cleanup for deleting an identity.

The auth server owns identities.  The consumer owns workspace memberships,
per-user runtime data, memories, and MCP credentials.  A durable deletion
intent is written before the remote identity is deleted so a process crash
cannot make the username reusable while consumer artifacts still exist.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterator

from integration.mcp.token_service import revoke_all_mcp_tokens_for_user
from config.system_config import load_system_config
from utils.identifiers import safe_user_storage_key
from workspace.workspaces import (
    get_workspace_catalog_generation,
    get_system_root,
    get_workspace_paths,
    get_workspaces_root,
    invalidate_workspace_catalog,
    remove_user_from_workspace_members,
    workspace_user_deletion_guard,
)


_RECONCILIATION_FILENAME = "user-deletion-reconciliation.json"
_RECONCILIATION_LOCK = threading.RLock()
_IDENTITY_LIFECYCLE_REGISTRY_LOCK = threading.Lock()
_IDENTITY_LIFECYCLE_ENTRIES: dict[str, "_IdentityLifecycleEntry"] = {}
_LEGACY_USER_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_OPERATION_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")


class UserDeletionReconciliationError(RuntimeError):
    """Raised when durable deletion state cannot be read or persisted."""

    def __init__(self, message: str, reconciliation_id: str | None = None) -> None:
        self.reconciliation_id = reconciliation_id
        super().__init__(message)


class UserDeletionPendingError(RuntimeError):
    """Raised when an unfinished deletion reserves a username."""

    def __init__(self, username: str, reconciliation_id: str) -> None:
        self.username = username
        self.reconciliation_id = reconciliation_id
        super().__init__("用户名正在删除或等待清理，暂时不能重新创建")


@dataclass(frozen=True)
class UserDeletionResult:
    pending_reconciliation: bool
    reconciliation_id: str | None
    completed_steps: tuple[str, ...]
    errors: Dict[str, str]


@dataclass
class _IdentityLifecycleEntry:
    lock: threading.RLock
    references: int = 0


def normalize_username(username: Any) -> str:
    """Return the canonical username form shared by route and cleanup code."""

    return str(username or "").strip()


@contextmanager
def user_identity_lifecycle_guard(username: Any) -> Iterator[str]:
    """Serialize create, delete, and retry for one normalized username.

    The current deployment is one process with multiple gthreads.  Reference
    counting removes idle entries so public registration cannot grow an
    unbounded in-memory lock registry.
    """

    normalized_username = normalize_username(username)
    if not normalized_username:
        raise ValueError("username 不能为空")

    with _IDENTITY_LIFECYCLE_REGISTRY_LOCK:
        entry = _IDENTITY_LIFECYCLE_ENTRIES.get(normalized_username)
        if entry is None:
            entry = _IdentityLifecycleEntry(lock=threading.RLock())
            _IDENTITY_LIFECYCLE_ENTRIES[normalized_username] = entry
        entry.references += 1
    try:
        with entry.lock:
            yield normalized_username
    finally:
        with _IDENTITY_LIFECYCLE_REGISTRY_LOCK:
            entry.references -= 1
            if (
                entry.references == 0
                and _IDENTITY_LIFECYCLE_ENTRIES.get(normalized_username) is entry
            ):
                _IDENTITY_LIFECYCLE_ENTRIES.pop(normalized_username, None)


@contextmanager
def identity_reference_guard(usernames: Any) -> Iterator[tuple[str, ...]]:
    """Keep identity references stable while another subsystem persists them.

    Locks are acquired in canonical username order so concurrent workspace ACL
    updates cannot deadlock each other.  A pending deletion reserves the name
    and must fail closed instead of being written back into workspace metadata.
    """

    if isinstance(usernames, str) or not isinstance(usernames, (list, tuple, set)):
        raise ValueError("usernames 必须是字符串集合")
    normalized = sorted({normalize_username(item) for item in usernames})
    if any(not username for username in normalized):
        raise ValueError("usernames 只能包含非空字符串")

    with ExitStack() as stack:
        for username in normalized:
            stack.enter_context(user_identity_lifecycle_guard(username))
        for username in normalized:
            pending = _find_reconciliation_for_username(username)
            if pending is not None:
                raise UserDeletionPendingError(username, str(pending["id"]))
        yield tuple(normalized)


def create_identity_if_reusable(
    username: Any,
    create_identity: Callable[[], Any],
) -> Any:
    """Run identity creation while the same username cannot enter deletion."""

    with user_identity_lifecycle_guard(username) as normalized_username:
        pending = _find_reconciliation_for_username(normalized_username)
        if pending is not None:
            raise UserDeletionPendingError(
                normalized_username,
                str(pending["id"]),
            )
        return create_identity()


def delete_user_with_cleanup(auth_backend: Any, username: str) -> UserDeletionResult:
    """Delete one identity and clean every username-keyed consumer artifact."""

    with user_identity_lifecycle_guard(username) as normalized_username:
        pending = _find_reconciliation_for_username(normalized_username)
        if pending is not None:
            raise UserDeletionPendingError(
                normalized_username,
                str(pending["id"]),
            )
        if auth_backend.get_user(normalized_username) is None:
            raise ValueError("用户 {} 不存在".format(normalized_username))

        operation_id = uuid.uuid4().hex
        with workspace_user_deletion_guard(normalized_username):
            try:
                _begin_deletion_intent(
                    operation_id=operation_id,
                    username=normalized_username,
                )
            except UserDeletionPendingError:
                raise
            except Exception as exc:
                raise UserDeletionReconciliationError(
                    "无法在删除身份前写入 reconciliation intent"
                ) from exc

            try:
                auth_backend.delete_user(normalized_username)
            except Exception as exc:
                errors = {
                    "identity_delete": "{}: {}".format(
                        exc.__class__.__name__,
                        exc,
                    )
                }
                try:
                    _update_reconciliation(
                        operation_id,
                        status="identity_delete_unconfirmed",
                        identity_deleted=False,
                        completed_steps=[],
                        errors=errors,
                    )
                except Exception as record_exc:
                    raise UserDeletionReconciliationError(
                        "身份删除结果未确认，且 reconciliation intent 更新失败",
                        operation_id,
                    ) from record_exc
                raise UserDeletionReconciliationError(
                    "身份删除结果未确认，已保留 reconciliation intent",
                    operation_id,
                ) from exc

            try:
                _update_reconciliation(
                    operation_id,
                    status="cleaning_consumer_artifacts",
                    identity_deleted=True,
                    completed_steps=[],
                    errors={},
                )
            except Exception as exc:
                raise UserDeletionReconciliationError(
                    "用户已删除，但 reconciliation intent 更新失败",
                    operation_id,
                ) from exc

            completed_steps, errors = cleanup_deleted_user_artifacts(
                normalized_username,
                operation_id=operation_id,
            )
            if errors:
                try:
                    _update_reconciliation(
                        operation_id,
                        status="cleanup_pending",
                        identity_deleted=True,
                        completed_steps=completed_steps,
                        errors=errors,
                    )
                except Exception as exc:
                    raise UserDeletionReconciliationError(
                        "用户已删除，但 reconciliation intent 更新失败",
                        operation_id,
                    ) from exc
                return UserDeletionResult(
                    pending_reconciliation=True,
                    reconciliation_id=operation_id,
                    completed_steps=tuple(completed_steps),
                    errors=dict(errors),
                )

            try:
                _remove_reconciliation(operation_id)
            except Exception as exc:
                raise UserDeletionReconciliationError(
                    "用户及关联数据已清理，但 reconciliation intent 未能移除",
                    operation_id,
                ) from exc
            return UserDeletionResult(
                pending_reconciliation=False,
                reconciliation_id=None,
                completed_steps=tuple(completed_steps),
                errors={},
            )


def list_user_deletion_reconciliations() -> list[dict[str, Any]]:
    """Return pending cleanup records for audit and operator-driven retry."""

    with _RECONCILIATION_LOCK:
        return [
            dict(item)
            for item in _read_reconciliation_document()["records"]
        ]


def retry_user_deletion_reconciliation(
    reconciliation_id: str,
    *,
    identity_exists: Callable[[str], bool],
) -> UserDeletionResult:
    """Retry cleanup while proving and preserving username absence."""

    record_id = str(reconciliation_id or "").strip()
    if not _OPERATION_ID_PATTERN.fullmatch(record_id):
        raise ValueError("reconciliation_id 格式无效")
    record = _find_reconciliation(record_id)
    if record is None:
        raise KeyError("reconciliation 记录不存在: {}".format(record_id))
    username = normalize_username(record.get("username"))
    if not username:
        raise UserDeletionReconciliationError(
            "reconciliation 记录缺少 username",
            record_id,
        )

    with user_identity_lifecycle_guard(username):
        record = _find_reconciliation(record_id)
        if record is None:
            raise KeyError("reconciliation 记录不存在: {}".format(record_id))
        if identity_exists(username):
            raise ValueError("用户名已被重新创建或仍存在，拒绝自动清理身份数据")

        with workspace_user_deletion_guard(username):
            completed_steps, errors = cleanup_deleted_user_artifacts(
                username,
                operation_id=record_id,
            )

        if errors:
            _update_reconciliation(
                record_id,
                status="cleanup_pending",
                identity_deleted=True,
                completed_steps=completed_steps,
                errors=errors,
                attempts=int(record.get("attempts") or 1) + 1,
            )
        else:
            _remove_reconciliation(record_id)
        return UserDeletionResult(
            pending_reconciliation=bool(errors),
            reconciliation_id=record_id if errors else None,
            completed_steps=tuple(completed_steps),
            errors=dict(errors),
        )


def cleanup_deleted_user_artifacts(
    username: str,
    *,
    operation_id: str,
) -> tuple[list[str], Dict[str, str]]:
    completed_steps: list[str] = []
    errors: Dict[str, str] = {}
    steps: tuple[tuple[str, Callable[[], Any]], ...] = (
        (
            "workspace_members",
            lambda: remove_user_from_workspace_members_and_invalidate(username),
        ),
        (
            "workspace_runtime",
            lambda: _archive_workspace_runtime(username, operation_id),
        ),
        (
            "memory",
            lambda: _archive_memory(username, operation_id),
        ),
        (
            "mcp_tokens",
            lambda: revoke_all_mcp_tokens_for_user(username=username),
        ),
    )
    for step_name, action in steps:
        try:
            action()
            completed_steps.append(step_name)
        except Exception as exc:  # independent steps remain retryable
            errors[step_name] = "{}: {}".format(exc.__class__.__name__, exc)
    try:
        _write_deletion_manifest(
            username=username,
            operation_id=operation_id,
            completed_steps=completed_steps,
            errors=errors,
        )
        completed_steps.append("archive_manifest")
    except Exception as exc:
        errors["archive_manifest"] = "{}: {}".format(exc.__class__.__name__, exc)
    return completed_steps, errors


def remove_user_from_workspace_members_and_invalidate(username: str) -> list[str]:
    """Keep the user-deletion boundary safe if the cleanup helper evolves."""
    generation_before = get_workspace_catalog_generation()
    updated_workspace_ids = remove_user_from_workspace_members(username)
    if (
        updated_workspace_ids
        and get_workspace_catalog_generation() == generation_before
    ):
        invalidate_workspace_catalog()
    return updated_workspace_ids


def _user_storage_keys(username: str) -> tuple[str, ...]:
    keys = [safe_user_storage_key(username)]
    # The pre-switch runtime used the literal auth-server username for its
    # allowed alphabet.  Archive that legacy location when keys differ.
    if (
        _LEGACY_USER_KEY_PATTERN.fullmatch(username)
        and username not in {".", ".."}
        and username not in keys
    ):
        keys.append(username)
    return tuple(keys)


def _archive_workspace_runtime(username: str, operation_id: str) -> list[str]:
    archived: list[str] = []
    root = get_workspaces_root()
    if not root.exists():
        return archived
    for entry in sorted(root.iterdir(), key=lambda item: item.name):
        if not entry.is_dir() or not (entry / "workspace.json").is_file():
            continue
        paths = get_workspace_paths(entry.name)
        for user_key in _user_storage_keys(username):
            source = paths.users_runtime_dir / user_key
            destination = (
                get_system_root()
                / "deleted-user-data"
                / operation_id
                / "workspaces"
                / paths.workspace_id
                / user_key
            )
            if _move_directory_if_present(source, destination):
                archived.append(str(destination))
    return archived


def _archive_memory(username: str, operation_id: str) -> list[str]:
    config = load_system_config()
    memory_config = dict(config.get("memory") or {})
    configured_root = str(memory_config.get("workspace_root") or "").strip()
    if configured_root:
        memory_root = Path(configured_root).resolve()
    else:
        project_root = Path(str(config["storage"]["project_root"])).resolve()
        memory_root = project_root / "memory"

    archived: list[str] = []
    for user_key in _user_storage_keys(username):
        source = memory_root / user_key
        destination = (
            get_system_root()
            / "deleted-user-data"
            / operation_id
            / "memory"
            / user_key
        )
        if _move_directory_if_present(source, destination):
            archived.append(str(destination))
    return archived


def _move_directory_if_present(source: Path, destination: Path) -> bool:
    if not source.exists():
        return False
    if not source.is_dir():
        raise ValueError("用户数据路径不是目录: {}".format(source))
    if destination.exists():
        raise FileExistsError("归档目标已存在: {}".format(destination))
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)
    return True


def _reconciliation_path() -> Path:
    return get_system_root() / _RECONCILIATION_FILENAME


def _write_deletion_manifest(
    *,
    username: str,
    operation_id: str,
    completed_steps: list[str],
    errors: Dict[str, str],
) -> None:
    manifest_path = (
        get_system_root()
        / "deleted-user-data"
        / operation_id
        / "manifest.json"
    )
    _write_json_atomic(
        manifest_path,
        {
            "id": operation_id,
            "username": username,
            "identity_deleted": True,
            "created_at": _utc_now(),
            "completed_steps": list(completed_steps),
            "errors": dict(errors),
        },
    )


def _find_reconciliation(record_id: str) -> dict[str, Any] | None:
    with _RECONCILIATION_LOCK:
        document = _read_reconciliation_document()
        return next(
            (
                dict(item)
                for item in document["records"]
                if item["id"] == record_id
            ),
            None,
        )


def _find_reconciliation_for_username(username: str) -> dict[str, Any] | None:
    with _RECONCILIATION_LOCK:
        document = _read_reconciliation_document()
        return next(
            (
                dict(item)
                for item in document["records"]
                if item["username"] == username
            ),
            None,
        )


def _begin_deletion_intent(*, operation_id: str, username: str) -> None:
    with _RECONCILIATION_LOCK:
        document = _read_reconciliation_document()
        pending = next(
            (
                item
                for item in document["records"]
                if item["username"] == username
            ),
            None,
        )
        if pending is not None:
            raise UserDeletionPendingError(username, str(pending["id"]))
        now = _utc_now()
        document["records"].append(
            {
                "id": operation_id,
                "username": username,
                "status": "deleting_identity",
                "identity_deleted": False,
                "created_at": now,
                "updated_at": now,
                "attempts": 1,
                "completed_steps": [],
                "errors": {},
            }
        )
        _write_json_atomic(_reconciliation_path(), document)


def _update_reconciliation(
    operation_id: str,
    *,
    status: str,
    identity_deleted: bool,
    completed_steps: list[str],
    errors: Dict[str, str],
    attempts: int | None = None,
) -> None:
    with _RECONCILIATION_LOCK:
        document = _read_reconciliation_document()
        record = next(
            (
                item
                for item in document["records"]
                if item["id"] == operation_id
            ),
            None,
        )
        if record is None:
            raise UserDeletionReconciliationError(
                "reconciliation 记录不存在: {}".format(operation_id),
                operation_id,
            )
        record.update(
            {
                "status": status,
                "identity_deleted": bool(identity_deleted),
                "updated_at": _utc_now(),
                "completed_steps": list(completed_steps),
                "errors": dict(errors),
            }
        )
        if attempts is not None:
            record["attempts"] = int(attempts)
        _write_json_atomic(_reconciliation_path(), document)


def _remove_reconciliation(operation_id: str) -> None:
    with _RECONCILIATION_LOCK:
        document = _read_reconciliation_document()
        records = [
            item
            for item in document["records"]
            if item["id"] != operation_id
        ]
        if len(records) == len(document["records"]):
            raise UserDeletionReconciliationError(
                "reconciliation 记录不存在: {}".format(operation_id),
                operation_id,
            )
        document["records"] = records
        _write_json_atomic(_reconciliation_path(), document)


def _read_reconciliation_document() -> dict[str, Any]:
    path = _reconciliation_path()
    if not path.exists():
        return {"version": 1, "records": []}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise UserDeletionReconciliationError(
            "user deletion reconciliation 文件损坏"
        ) from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or not isinstance(payload.get("records"), list)
    ):
        raise UserDeletionReconciliationError(
            "user deletion reconciliation 文件结构无效"
        )

    records = payload["records"]
    seen_ids: set[str] = set()
    seen_usernames: set[str] = set()
    for item in records:
        if not isinstance(item, dict):
            raise UserDeletionReconciliationError(
                "user deletion reconciliation 记录结构无效"
            )
        record_id = item.get("id")
        username = item.get("username")
        valid_record_id = (
            isinstance(record_id, str)
            and bool(_OPERATION_ID_PATTERN.fullmatch(record_id))
            and record_id not in seen_ids
        )
        valid_username = (
            isinstance(username, str)
            and bool(username)
            and username == normalize_username(username)
            and username not in seen_usernames
        )
        valid_status = isinstance(item.get("status"), str) and bool(item.get("status"))
        valid_attempts = (
            isinstance(item.get("attempts"), int)
            and int(item.get("attempts")) >= 1
        )
        valid_identity_deleted = isinstance(item.get("identity_deleted"), bool)
        valid_collections = (
            isinstance(item.get("completed_steps"), list)
            and isinstance(item.get("errors"), dict)
        )
        valid_record = all(
            (
                valid_record_id,
                valid_username,
                valid_status,
                valid_attempts,
                valid_identity_deleted,
                valid_collections,
            )
        )
        if not valid_record:
            raise UserDeletionReconciliationError(
                "user deletion reconciliation 记录字段无效"
            )
        seen_ids.add(record_id)
        seen_usernames.add(username)
    return {"version": 1, "records": records}


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _write_json_atomic(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.chmod(temporary_path, 0o600)
        os.replace(temporary_path, path)
    finally:
        temporary_path.unlink(missing_ok=True)
