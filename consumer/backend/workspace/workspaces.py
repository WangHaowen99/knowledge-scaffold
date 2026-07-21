"""Workspace metadata and path resolution helpers."""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import shutil
import threading
import unicodedata
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional
from urllib.parse import unquote

from flask import Request, abort, g, has_request_context, jsonify, make_response

from config import AppConfig, LimitsConfig
from config.model_profile_refs import model_profile_ref_to_dict, parse_model_profile_ref


WORKSPACE_HEADER = "X-Workspace-Id"
LEGACY_DEFAULT_WORKSPACE_ID = "default"
_WORKSPACE_ID_MAX_LEN = 64
_WORKSPACE_MEMBERS_MAX_COUNT = 64
_UNCHANGED = object()
_INVALID_WORKSPACE_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
_workspace_var: ContextVar[Optional[str]] = ContextVar("workspace_id", default=None)
_workspace_metadata_lock = threading.RLock()


class WorkspaceNotFoundError(KeyError):
    """Raised when a request explicitly references a missing workspace."""

    code = "WORKSPACE_NOT_FOUND"

    def __init__(self, workspace_id: str) -> None:
        self.workspace_id = validate_workspace_id(workspace_id)
        self.detail = f"工作空间 '{self.workspace_id}' 不存在"
        super().__init__(self.detail)

    def __str__(self) -> str:
        return self.detail

    def to_dict(self) -> Dict[str, str]:
        return {
            "code": self.code,
            "detail": self.detail,
            "workspace_id": self.workspace_id,
        }


class WorkspaceOwnershipConflictError(ValueError):
    """Raised when a user still owns workspaces and cannot be deleted."""

    def __init__(self, username: str, workspace_ids: List[str]) -> None:
        self.username = username
        self.workspace_ids = list(workspace_ids)
        super().__init__(
            "用户 '{}' 仍是工作空间 owner: {}".format(
                username,
                ", ".join(self.workspace_ids),
            )
        )


class WorkspaceAuthorizationError(PermissionError):
    """Raised when a workspace metadata mutation is not authorized."""


@dataclass(frozen=True)
class WorkspacePaths:
    workspace_id: str
    workspace_dir: Path
    metadata_path: Path
    ontologies_dir: Path
    skills_dir: Path
    files_dir: Path
    runtime_dir: Path
    users_runtime_dir: Path


@dataclass(frozen=True)
class WorkspaceSubjectAccessDecision:
    """Current workspace ACL decision for a persisted identity subject."""

    allowed: bool
    reason: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def get_runtime_root() -> Path:
    return Path(AppConfig.from_env().project_root).resolve()


def get_system_root() -> Path:
    return get_runtime_root() / "system"


def get_workspaces_root() -> Path:
    return get_runtime_root() / "workspaces"


def get_workspace_settings_path() -> Path:
    return get_runtime_root() / "workspace-settings.json"


def _ensure_runtime_layout() -> None:
    get_runtime_root().mkdir(parents=True, exist_ok=True)
    get_workspaces_root().mkdir(parents=True, exist_ok=True)
    (get_system_root() / "skills").mkdir(parents=True, exist_ok=True)


def _normalize_workspace_name(name: str) -> str:
    value = str(name or "").strip()
    if not value:
        raise ValueError("工作空间名称不能为空")
    return value


_WORKSPACE_ID_PATTERN = re.compile(r'^[a-z][a-z0-9_-]*$')


def validate_workspace_id(workspace_id: str) -> str:
    value = str(workspace_id or "").strip().lower()
    if not value:
        raise ValueError("工作空间 ID 不能为空")

    if not _WORKSPACE_ID_PATTERN.match(value):
        raise ValueError("工作空间 ID 格式非法：必须以英文小写字母开头，只能包含英文、数字、下划线(_)或中划线(-)")

    if len(value) > _WORKSPACE_ID_MAX_LEN:
        raise ValueError(f"工作空间 ID 过长，最多 {_WORKSPACE_ID_MAX_LEN} 个字符")

    return value


def _slugify_workspace_name(name: str) -> str:
    normalized = unicodedata.normalize("NFKC", _normalize_workspace_name(name)).strip()
    ascii_name = unicodedata.normalize("NFKD", normalized).encode("ascii", "ignore").decode("ascii")
    if ascii_name.strip():
        candidate = re.sub(r"[^a-zA-Z0-9]+", "-", ascii_name).strip("-").lower()
    else:
        candidate = re.sub(r"\s+", "-", normalized).strip("-")
    candidate = re.sub(r"-{2,}", "-", candidate).strip("-")
    if not candidate:
        candidate = f"workspace-{uuid.uuid4().hex[:8]}"
    candidate = candidate[:_WORKSPACE_ID_MAX_LEN]
    return validate_workspace_id(candidate)


def _with_suffix(base_id: str, suffix: str) -> str:
    suffix_part = f"-{suffix}"
    trimmed_base = base_id[: max(1, _WORKSPACE_ID_MAX_LEN - len(suffix_part))]
    return validate_workspace_id(f"{trimmed_base}{suffix_part}")


def _read_workspace_metadata_raw(metadata_path: Path) -> Dict[str, Any]:
    with metadata_path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _write_json_atomic(path: Path, payload: Any) -> None:
    """Write JSON with an atomic replace so interrupted writes cannot truncate state."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        tmp_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(tmp_path, path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _write_workspace_metadata(paths: WorkspacePaths, payload: Dict[str, Any]) -> None:
    paths.workspace_dir.mkdir(parents=True, exist_ok=True)
    _write_json_atomic(paths.metadata_path, payload)


def _read_workspace_settings() -> Dict[str, Any]:
    settings_path = get_workspace_settings_path()
    if not settings_path.exists():
        return {}
    try:
        with settings_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_workspace_settings(payload: Dict[str, Any]) -> None:
    _write_json_atomic(get_workspace_settings_path(), payload)


def _list_workspace_dirs() -> List[Path]:
    _ensure_runtime_layout()
    workspaces_root = get_workspaces_root()
    if not workspaces_root.exists():
        return []
    return sorted(
        [entry for entry in workspaces_root.iterdir() if entry.is_dir() and (entry / "workspace.json").exists()],
        key=lambda item: item.name,
    )


def _normalize_workspace_owner(owner: Optional[str]) -> Optional[str]:
    if owner is None:
        return None
    if not isinstance(owner, str) or not owner.strip():
        raise ValueError("owner 必须是非空字符串")
    return owner.strip()


def _normalize_workspace_members(members: Any) -> List[str]:
    if not isinstance(members, list):
        raise ValueError("members 必须是字符串数组")
    normalized: List[str] = []
    seen = set()
    for item in members:
        if not isinstance(item, str) or not item.strip():
            raise ValueError("members 只能包含非空字符串")
        value = item.strip()
        if value in seen:
            continue
        seen.add(value)
        normalized.append(value)
    if len(normalized) > _WORKSPACE_MEMBERS_MAX_COUNT:
        raise ValueError(f"members 数量超过上限 {_WORKSPACE_MEMBERS_MAX_COUNT}")
    return normalized


def _public_members(data: Dict[str, Any]) -> List[str]:
    members = data.get("members")
    if not isinstance(members, list):
        return []
    return [item for item in members if isinstance(item, str) and item]


def _validate_workspace_metadata_identity(
    expected_workspace_id: str,
    payload: Dict[str, Any],
) -> str:
    expected = validate_workspace_id(expected_workspace_id)
    actual = validate_workspace_id(payload.get("id"))
    if actual != expected:
        raise ValueError(
            "workspace metadata id mismatch: directory={!r}, payload={!r}".format(
                expected,
                actual,
            )
        )
    return actual


def _workspace_to_public(
    data: Dict[str, Any], *, default_workspace_id: Optional[str] = None
) -> Dict[str, Any]:
    workspace_id = validate_workspace_id(data["id"])
    return {
        "id": workspace_id,
        "name": data["name"],
        "description": data.get("description", ""),
        "created_at": data["created_at"],
        "updated_at": data["updated_at"],
        "is_default": workspace_id == default_workspace_id,
        "owner": data.get("owner") or None,
        "members": _public_members(data),
    }


def user_can_access_workspace(user: Any, workspace: Dict[str, Any], is_default: bool) -> bool:
    """Return whether a user may access a workspace under the shared ACL policy."""
    if user is None:
        return True
    if getattr(user, "role", None) == "admin":
        return True
    owner = workspace.get("owner") or None
    if not owner or is_default:
        return True
    username = getattr(user, "username", None)
    if username and username == owner:
        return True
    return bool(username and username in _public_members(workspace))


def user_can_administer_workspace(user: Any, workspace: Dict[str, Any]) -> bool:
    """Return whether a user is the global admin or the workspace owner."""
    if user is None:
        return False
    if getattr(user, "role", None) == "admin":
        return True
    username = getattr(user, "username", None)
    owner = workspace.get("owner") or None
    return bool(username and owner and username == owner)


def _init_workspace_layout(
    paths: WorkspacePaths,
    *,
    name: str,
    description: str = "",
    owner: Optional[str] = None,
) -> Dict[str, Any]:
    now = _utc_now()
    payload = {
        "id": paths.workspace_id,
        "name": _normalize_workspace_name(name),
        "description": str(description or "").strip(),
        "created_at": now,
        "updated_at": now,
        "owner": _normalize_workspace_owner(owner),
        "members": [],
    }
    paths.ontologies_dir.mkdir(parents=True, exist_ok=True)
    paths.skills_dir.mkdir(parents=True, exist_ok=True)
    paths.files_dir.mkdir(parents=True, exist_ok=True)
    paths.users_runtime_dir.mkdir(parents=True, exist_ok=True)
    _write_workspace_metadata(paths, payload)
    return payload


def get_workspace_paths(workspace_id: Optional[str] = None) -> WorkspacePaths:
    resolved_id = validate_workspace_id(workspace_id or get_current_workspace_id())
    workspace_dir = get_workspaces_root() / resolved_id
    runtime_dir = workspace_dir / "runtime"
    return WorkspacePaths(
        workspace_id=resolved_id,
        workspace_dir=workspace_dir,
        metadata_path=workspace_dir / "workspace.json",
        ontologies_dir=workspace_dir / "ontologies",
        skills_dir=workspace_dir / "skills",
        files_dir=workspace_dir / "files",
        runtime_dir=runtime_dir,
        users_runtime_dir=runtime_dir / "users",
    )


def get_current_workspace_paths() -> WorkspacePaths:
    return get_workspace_paths(get_current_workspace_id())


def get_workspace_project_root(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir.resolve()


def get_workspace_ontologies_root(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).ontologies_dir.resolve()


def get_workspace_skills_root(workspace_id: Optional[str] = None) -> Path:
    return get_workspace_paths(workspace_id).skills_dir.resolve()


def get_system_skills_root() -> Path:
    return (get_system_root() / "skills").resolve()


def get_skill_roots(workspace_id: Optional[str] = None) -> List[Path]:
    return [
        get_workspace_skills_root(workspace_id),
        get_system_skills_root(),
    ]


def workspace_exists(workspace_id: str) -> bool:
    return get_workspace_paths(workspace_id).metadata_path.exists()


def _migrate_legacy_default_workspace() -> Optional[str]:
    _ensure_runtime_layout()
    legacy_dir = get_workspaces_root() / LEGACY_DEFAULT_WORKSPACE_ID
    legacy_metadata = legacy_dir / "workspace.json"
    if not legacy_metadata.exists():
        return None

    with _workspace_metadata_lock:
        if not legacy_metadata.exists():
            return None
        raw = _read_workspace_metadata_raw(legacy_metadata)
        return _do_migrate_legacy(legacy_dir, raw)


def _do_migrate_legacy(legacy_dir: Path, raw: Dict[str, Any]) -> str:
    target_id = _slugify_workspace_name(raw.get("name") or raw.get("id") or "workspace")
    if target_id == LEGACY_DEFAULT_WORKSPACE_ID:
        target_id = validate_workspace_id(f"workspace-{uuid.uuid4().hex[:8]}")
    while (get_workspaces_root() / target_id).exists():
        target_id = _with_suffix(target_id, uuid.uuid4().hex[:4])

    target_dir = get_workspaces_root() / target_id
    shutil.move(str(legacy_dir), str(target_dir))
    target_paths = get_workspace_paths(target_id)
    raw["id"] = target_id
    raw["updated_at"] = raw.get("updated_at") or _utc_now()
    _write_workspace_metadata(target_paths, raw)

    settings = _read_workspace_settings()
    if settings.get("default_workspace_id") in {None, "", LEGACY_DEFAULT_WORKSPACE_ID}:
        settings["default_workspace_id"] = target_id
        _write_workspace_settings(settings)

    return target_id


def _ensure_workspace_state() -> None:
    with _workspace_metadata_lock:
        _ensure_runtime_layout()
        migrated_id = _migrate_legacy_default_workspace()
        workspace_dirs = _list_workspace_dirs()
        settings = _read_workspace_settings()
        default_workspace_id = settings.get("default_workspace_id")
        default_explicitly_cleared = (
            "default_workspace_id" in settings and default_workspace_id is None
        )

        if default_workspace_id and workspace_exists(default_workspace_id):
            return

        if migrated_id and workspace_exists(migrated_id):
            settings["default_workspace_id"] = migrated_id
            _write_workspace_settings(settings)
            return

        if len(workspace_dirs) == 1 and not default_explicitly_cleared:
            only_id = validate_workspace_id(workspace_dirs[0].name)
            settings["default_workspace_id"] = only_id
            _write_workspace_settings(settings)
            return

        if not workspace_dirs:
            if "default_workspace_id" not in settings or settings.get("default_workspace_id") is not None:
                settings["default_workspace_id"] = None
                _write_workspace_settings(settings)


def get_default_workspace_id() -> Optional[str]:
    with _workspace_metadata_lock:
        _ensure_workspace_state()
        settings = _read_workspace_settings()
        default_workspace_id = settings.get("default_workspace_id")
        if default_workspace_id and workspace_exists(default_workspace_id):
            return validate_workspace_id(default_workspace_id)
        if "default_workspace_id" in settings and default_workspace_id is None:
            return None

        workspace_dirs = _list_workspace_dirs()
        if len(workspace_dirs) == 1:
            only_id = validate_workspace_id(workspace_dirs[0].name)
            set_default_workspace(only_id)
            return only_id
        return None


def set_default_workspace(workspace_id: str) -> Dict[str, str]:
    resolved_id = validate_workspace_id(workspace_id)
    with _workspace_metadata_lock:
        paths = get_workspace_paths(resolved_id)
        if not paths.metadata_path.exists():
            raise WorkspaceNotFoundError(resolved_id)
        settings = _read_workspace_settings()
        settings["default_workspace_id"] = resolved_id
        _write_workspace_settings(settings)
    return get_workspace(resolved_id)


def get_current_workspace_id() -> str:
    current = _workspace_var.get()
    if current:
        return validate_workspace_id(current)
    default_workspace_id = get_default_workspace_id()
    if default_workspace_id:
        return default_workspace_id
    raise ValueError("当前未配置默认工作空间")


def list_workspaces() -> List[Dict[str, str]]:
    _ensure_workspace_state()
    default_workspace_id = get_default_workspace_id()
    items: List[Dict[str, str]] = []
    for entry in _list_workspace_dirs():
        metadata_path = entry / "workspace.json"
        try:
            payload = _read_workspace_metadata_raw(metadata_path)
            _validate_workspace_metadata_identity(entry.name, payload)
            items.append(_workspace_to_public(payload, default_workspace_id=default_workspace_id))
        except Exception:
            continue

    items.sort(key=lambda item: (0 if item.get("is_default") else 1, item["name"].lower(), item["id"]))
    return items


def get_workspace(workspace_id: str) -> Dict[str, str]:
    _ensure_workspace_state()
    paths = get_workspace_paths(workspace_id)
    with _workspace_metadata_lock:
        if not paths.metadata_path.exists():
            raise WorkspaceNotFoundError(workspace_id)
        payload = _read_workspace_metadata_raw(paths.metadata_path)
        _validate_workspace_metadata_identity(paths.workspace_id, payload)
        return _workspace_to_public(
            payload,
            default_workspace_id=get_default_workspace_id(),
        )


def create_workspace(
    name: str,
    description: str = "",
    *,
    workspace_id: Optional[str] = None,
    set_as_default: bool = False,
    auto_set_default: bool = True,
    owner: Optional[str] = None,
) -> Dict[str, str]:
    _ensure_workspace_state()
    normalized_name = _normalize_workspace_name(name)
    normalized_owner = _normalize_workspace_owner(owner)

    with _workspace_metadata_lock:
        if workspace_id:
            candidate = validate_workspace_id(workspace_id)
            if workspace_exists(candidate):
                raise ValueError(f"工作空间 ID '{candidate}' 已存在")
        else:
            candidate = _slugify_workspace_name(normalized_name)
            while workspace_exists(candidate):
                candidate = _with_suffix(candidate, uuid.uuid4().hex[:4])

        max_workspaces = LimitsConfig.from_env().workspace_max_count
        if len(list_workspaces()) >= max_workspaces:
            raise ValueError("工作空间数量已达上限: {}".format(max_workspaces))

        paths = get_workspace_paths(candidate)
        _init_workspace_layout(
            paths,
            name=normalized_name,
            description=description,
            owner=normalized_owner,
        )
    if set_as_default or (auto_set_default and get_default_workspace_id() is None):
        set_default_workspace(candidate)
    return get_workspace(candidate)


def update_workspace(
    workspace_id: str,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    owner: Any = _UNCHANGED,
    members: Optional[List[str]] = None,
) -> Dict[str, str]:
    normalized_owner = _UNCHANGED if owner is _UNCHANGED else _normalize_workspace_owner(owner)
    normalized_members = _normalize_workspace_members(members) if members is not None else None

    with _workspace_metadata_lock:
        paths = _update_workspace_locked(
            workspace_id,
            name=name,
            description=description,
            owner=normalized_owner,
            members=normalized_members,
        )
    return get_workspace(paths.workspace_id)


def _update_workspace_locked(
    workspace_id: str,
    *,
    name: Optional[str],
    description: Optional[str],
    owner: Any,
    members: Optional[List[str]],
) -> WorkspacePaths:
    paths = get_workspace_paths(workspace_id)
    if not paths.metadata_path.exists():
        raise WorkspaceNotFoundError(workspace_id)
    payload = _read_workspace_metadata_raw(paths.metadata_path)
    _validate_workspace_metadata_identity(paths.workspace_id, payload)
    if name is not None:
        payload["name"] = _normalize_workspace_name(name)
    if description is not None:
        payload["description"] = str(description or "").strip()
    if owner is not _UNCHANGED:
        payload["owner"] = owner
    if members is not None:
        payload["members"] = members
    payload["updated_at"] = _utc_now()
    _write_workspace_metadata(paths, payload)
    return paths


def update_workspace_authorized(
    workspace_id: str,
    user: Any,
    *,
    name: Optional[str] = None,
    description: Optional[str] = None,
    owner: Any = _UNCHANGED,
    members: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Authorize against current metadata and update it in one lock transaction."""
    normalized_owner = _UNCHANGED if owner is _UNCHANGED else _normalize_workspace_owner(owner)
    normalized_members = _normalize_workspace_members(members) if members is not None else None

    with _workspace_metadata_lock:
        paths = get_workspace_paths(workspace_id)
        if not paths.metadata_path.exists():
            raise WorkspaceNotFoundError(workspace_id)
        payload = _read_workspace_metadata_raw(paths.metadata_path)
        _validate_workspace_metadata_identity(paths.workspace_id, payload)
        username = getattr(user, "username", None)
        is_admin = getattr(user, "role", None) == "admin"
        is_owner = bool(payload.get("owner")) and payload.get("owner") == username
        if not (is_admin or is_owner):
            raise WorkspaceAuthorizationError("workspace mutation is forbidden")
        if normalized_owner is not _UNCHANGED and not is_admin:
            raise WorkspaceAuthorizationError("only a global admin may change workspace owner")
        _update_workspace_locked(
            paths.workspace_id,
            name=name,
            description=description,
            owner=normalized_owner,
            members=normalized_members,
        )
        return get_workspace(paths.workspace_id)


def delete_workspace(workspace_id: str, *, allow_default: bool = False) -> None:
    resolved_id = validate_workspace_id(workspace_id)
    with _workspace_metadata_lock:
        is_default = get_default_workspace_id() == resolved_id
        if is_default and not allow_default:
            raise ValueError("默认工作空间不可删除，请先切换默认工作空间")
        paths = get_workspace_paths(resolved_id)
        if not paths.metadata_path.exists():
            raise WorkspaceNotFoundError(resolved_id)
        shutil.rmtree(paths.workspace_dir, ignore_errors=True)
        if is_default:
            settings = _read_workspace_settings()
            settings["default_workspace_id"] = None
            _write_workspace_settings(settings)


def list_owned_workspace_ids(username: str) -> List[str]:
    """List workspaces that must be transferred before deleting a user."""
    normalized_username = str(username or "").strip()
    if not normalized_username:
        raise ValueError("username 不能为空")

    with _workspace_metadata_lock:
        owned: List[str] = []
        for entry in _list_workspace_dirs():
            payload = _read_workspace_metadata_raw(entry / "workspace.json")
            workspace_id = _validate_workspace_metadata_identity(entry.name, payload)
            if payload.get("owner") == normalized_username:
                owned.append(workspace_id)
        return sorted(owned)


def remove_user_from_workspace_members(username: str) -> List[str]:
    """Remove a deleted user from every membership list under the metadata lock."""
    normalized_username = str(username or "").strip()
    if not normalized_username:
        raise ValueError("username 不能为空")

    updated_workspace_ids: List[str] = []
    with _workspace_metadata_lock:
        pending: List[tuple[WorkspacePaths, Dict[str, Any]]] = []
        for entry in _list_workspace_dirs():
            metadata_path = entry / "workspace.json"
            payload = _read_workspace_metadata_raw(metadata_path)
            workspace_id = _validate_workspace_metadata_identity(entry.name, payload)
            members = _public_members(payload)
            if normalized_username not in members:
                continue
            payload["members"] = [item for item in members if item != normalized_username]
            payload["updated_at"] = _utc_now()
            paths = get_workspace_paths(workspace_id)
            pending.append((paths, payload))
        for paths, payload in pending:
            _write_workspace_metadata(paths, payload)
            updated_workspace_ids.append(paths.workspace_id)
    return sorted(updated_workspace_ids)


@contextmanager
def workspace_user_deletion_guard(username: str) -> Iterator[None]:
    """Serialize user deletion with workspace ownership and membership updates."""
    normalized_username = str(username or "").strip()
    if not normalized_username:
        raise ValueError("username 不能为空")
    with _workspace_metadata_lock:
        owned_workspace_ids = list_owned_workspace_ids(normalized_username)
        if owned_workspace_ids:
            raise WorkspaceOwnershipConflictError(normalized_username, owned_workspace_ids)
        yield


def evaluate_workspace_subject_access(
    username: str,
    workspace_id: str,
) -> WorkspaceSubjectAccessDecision:
    """Re-read ACL metadata for standalone protocol callers and fail closed."""
    subject = str(username or "").strip()
    if not subject:
        return WorkspaceSubjectAccessDecision(False, "subject_invalid")

    claimed_workspace_id = str(workspace_id or "").strip()
    try:
        resolved_workspace_id = validate_workspace_id(claimed_workspace_id)
    except (TypeError, ValueError):
        return WorkspaceSubjectAccessDecision(False, "workspace_metadata_invalid")
    if resolved_workspace_id != claimed_workspace_id:
        return WorkspaceSubjectAccessDecision(False, "workspace_metadata_invalid")

    paths = get_workspace_paths(resolved_workspace_id)
    with _workspace_metadata_lock:
        if not paths.metadata_path.is_file():
            return WorkspaceSubjectAccessDecision(False, "workspace_not_found")
        try:
            metadata = _read_workspace_metadata_raw(paths.metadata_path)
            owner, members = _validate_workspace_acl_metadata(
                resolved_workspace_id,
                metadata,
            )
        except (OSError, TypeError, ValueError, KeyError):
            return WorkspaceSubjectAccessDecision(False, "workspace_metadata_invalid")
        default_workspace_id = _read_default_workspace_id_for_acl()

    if owner is None:
        return WorkspaceSubjectAccessDecision(True, "ownerless")
    if resolved_workspace_id == default_workspace_id:
        return WorkspaceSubjectAccessDecision(True, "default")
    if subject == owner:
        return WorkspaceSubjectAccessDecision(True, "owner")
    if subject in members:
        return WorkspaceSubjectAccessDecision(True, "member")
    return WorkspaceSubjectAccessDecision(False, "access_revoked")


def _validate_workspace_acl_metadata(
    expected_workspace_id: str,
    metadata: Any,
) -> tuple[Optional[str], List[str]]:
    if not isinstance(metadata, dict):
        raise ValueError("workspace metadata must be an object")
    actual_workspace_id = metadata.get("id")
    if not isinstance(actual_workspace_id, str) or actual_workspace_id != expected_workspace_id:
        raise ValueError("workspace metadata id mismatch")

    owner = metadata.get("owner")
    if owner is not None:
        if not isinstance(owner, str) or not owner.strip() or owner != owner.strip():
            raise ValueError("workspace owner is invalid")

    raw_members = metadata.get("members", [])
    if not isinstance(raw_members, list):
        raise ValueError("workspace members must be an array")
    members: List[str] = []
    seen: set[str] = set()
    for member in raw_members:
        if not isinstance(member, str) or not member.strip() or member != member.strip():
            raise ValueError("workspace member is invalid")
        if member in seen:
            raise ValueError("workspace members contain duplicates")
        seen.add(member)
        members.append(member)
    if len(members) > _WORKSPACE_MEMBERS_MAX_COUNT:
        raise ValueError("workspace members exceed the configured limit")
    return owner, members


def _read_default_workspace_id_for_acl() -> Optional[str]:
    settings_path = get_workspace_settings_path()
    if not settings_path.is_file():
        return None
    try:
        settings = json.loads(settings_path.read_text(encoding="utf-8"))
        if not isinstance(settings, dict):
            return None
        raw_default = settings.get("default_workspace_id")
        if not isinstance(raw_default, str) or not raw_default.strip():
            return None
        resolved_default = validate_workspace_id(raw_default)
        return resolved_default if resolved_default == raw_default else None
    except (OSError, TypeError, ValueError):
        return None


def _current_request_user() -> Any:
    if not has_request_context():
        return None
    return g.get("current_user")


def _abort_workspace_access_denied() -> None:
    abort(make_response(
        jsonify({"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}),
        403,
    ))


def _abort_workspace_metadata_invalid(workspace_id: str) -> None:
    abort(make_response(
        jsonify({
            "error": "Workspace metadata is invalid",
            "code": "WORKSPACE_METADATA_INVALID",
            "workspace_id": workspace_id,
        }),
        500,
    ))


def _enforce_workspace_access(
    workspace_id: str, *, default_workspace_id: Optional[str] = None
) -> str:
    user = _current_request_user()
    if user is None:
        return workspace_id
    paths = get_workspace_paths(workspace_id)
    try:
        with _workspace_metadata_lock:
            workspace = _read_workspace_metadata_raw(paths.metadata_path)
            _validate_workspace_metadata_identity(workspace_id, workspace)
    except Exception:
        _abort_workspace_metadata_invalid(workspace_id)
    if default_workspace_id is None:
        default_workspace_id = get_default_workspace_id()
    if not user_can_access_workspace(
        user,
        workspace,
        workspace_id == default_workspace_id,
    ):
        _abort_workspace_access_denied()
    return workspace_id


def enforce_workspace_access(workspace_id: str) -> str:
    """Enforce ACL for a workspace id obtained outside request header resolution."""
    resolved_id = validate_workspace_id(workspace_id)
    if not workspace_exists(resolved_id):
        raise WorkspaceNotFoundError(resolved_id)
    return _enforce_workspace_access(resolved_id)


def enforce_workspace_owner_or_admin(workspace_id: str) -> str:
    """Require global admin or current workspace ownership for a request mutation."""
    resolved_id = enforce_workspace_access(workspace_id)
    paths = get_workspace_paths(resolved_id)
    try:
        with _workspace_metadata_lock:
            workspace = _read_workspace_metadata_raw(paths.metadata_path)
            _validate_workspace_metadata_identity(resolved_id, workspace)
    except Exception:
        _abort_workspace_metadata_invalid(resolved_id)
    if not user_can_administer_workspace(_current_request_user(), workspace):
        _abort_workspace_access_denied()
    return resolved_id


def resolve_workspace_id(request: Request) -> str:
    _ensure_workspace_state()
    default_workspace_id = get_default_workspace_id()
    header_value = request.headers.get(WORKSPACE_HEADER)
    if header_value:
        workspace_id = validate_workspace_id(unquote(header_value))
        if not workspace_exists(workspace_id):
            raise WorkspaceNotFoundError(workspace_id)
        return _enforce_workspace_access(
            workspace_id,
            default_workspace_id=default_workspace_id,
        )

    if default_workspace_id and workspace_exists(default_workspace_id):
        return _enforce_workspace_access(
            default_workspace_id,
            default_workspace_id=default_workspace_id,
        )

    workspaces = list_workspaces()
    if len(workspaces) == 1:
        return _enforce_workspace_access(
            workspaces[0]["id"],
            default_workspace_id=default_workspace_id,
        )
    if not workspaces:
        raise ValueError("当前不存在任何工作空间")
    raise ValueError("X-Workspace-Id 不能为空；当前存在多个工作空间")


def resolve_workspace_owner_or_admin_id(request: Request) -> str:
    """Resolve request workspace and require global admin or workspace ownership."""
    return enforce_workspace_owner_or_admin(resolve_workspace_id(request))


@contextmanager
def workspace_scope(workspace_id: Optional[str] = None) -> Iterator[str]:
    resolved_id = validate_workspace_id(workspace_id or get_current_workspace_id())
    token = _workspace_var.set(resolved_id)
    try:
        yield resolved_id
    finally:
        _workspace_var.reset(token)


DEFAULT_RUNTIME_CONFIG: Dict[str, Any] = {
    "lang": {
        "enabled": False,
        "default": "zh",
    },
    "acp_suppress_script_patterns": "",
    "llm": {
        "default_main_profile_ref": None,
        "default_task_profile_ref": None,
    },
}

_RUNTIME_CONFIG_KEYS = set(DEFAULT_RUNTIME_CONFIG.keys())
_LANG_CONFIG_KEYS = set(DEFAULT_RUNTIME_CONFIG["lang"].keys())
_LLM_CONFIG_KEYS = set(DEFAULT_RUNTIME_CONFIG["llm"].keys())


def _get_runtime_config_path(workspace_id: str) -> Path:
    return get_workspace_paths(workspace_id).workspace_dir / "runtime_config.json"


def _migrate_legacy_runtime_llm_ref(workspace_id, config_path, saved):
    """DEPRECATED: rename legacy llm.default_profile_ref to default_main_profile_ref. Remove next release."""
    llm = saved.get("llm")
    if not isinstance(llm, dict) or "default_profile_ref" not in llm:
        return saved
    if "default_main_profile_ref" not in llm:
        llm["default_main_profile_ref"] = llm.pop("default_profile_ref")
    else:
        llm.pop("default_profile_ref", None)
    try:
        _write_json_atomic(config_path, saved)
        logging.getLogger(__name__).warning(
            "Migrated workspace %s legacy llm.default_profile_ref to default_main_profile_ref",
            workspace_id,
        )
    except Exception:
        logging.getLogger(__name__).warning(
            "Failed to persist runtime config migration for workspace %s", workspace_id, exc_info=True
        )
    return saved


def get_workspace_runtime_config(workspace_id: str) -> dict:
    with _workspace_metadata_lock:
        if not workspace_exists(workspace_id):
            raise WorkspaceNotFoundError(workspace_id)
        config_path = _get_runtime_config_path(workspace_id)
        if not config_path.exists():
            return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
        try:
            with config_path.open("r", encoding="utf-8") as f:
                saved = json.load(f)
        except Exception:
            return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
        if not isinstance(saved, dict):
            return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
        saved = _migrate_legacy_runtime_llm_ref(workspace_id, config_path, saved)
        merged = copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
        try:
            _deep_update_runtime_config(merged, _normalize_runtime_config_patch(saved, strict=False))
        except ValueError:
            return copy.deepcopy(DEFAULT_RUNTIME_CONFIG)
        return merged


def update_workspace_runtime_config(workspace_id: str, data: dict) -> dict:
    normalized = _normalize_runtime_config_patch(data, strict=True)
    with _workspace_metadata_lock:
        if not workspace_exists(workspace_id):
            raise WorkspaceNotFoundError(workspace_id)
        current = get_workspace_runtime_config(workspace_id)
        _deep_update_runtime_config(current, normalized)
        _write_json_atomic(_get_runtime_config_path(workspace_id), current)
    return current


def get_current_workspace_runtime_config() -> dict:
    return get_workspace_runtime_config(get_current_workspace_id())


def _deep_update_runtime_config(target: dict, source: dict) -> None:
    for key, value in source.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _deep_update_runtime_config(target[key], value)
        else:
            target[key] = value


def _normalize_runtime_config_patch(data: dict, *, strict: bool) -> dict:
    if not isinstance(data, dict):
        raise ValueError("Runtime config patch must be an object")

    normalized: dict = {}
    for key, value in data.items():
        if key not in _RUNTIME_CONFIG_KEYS:
            if strict:
                raise ValueError(f"Unknown runtime config key: {key}")
            continue

        if key == "lang":
            if not isinstance(value, dict):
                raise ValueError("lang must be an object")
            normalized_lang = {}
            for lang_key, lang_value in value.items():
                if lang_key not in _LANG_CONFIG_KEYS:
                    if strict:
                        raise ValueError(f"Unknown runtime config key: lang.{lang_key}")
                    continue
                if lang_key == "enabled" and not isinstance(lang_value, bool):
                    raise ValueError("lang.enabled must be a boolean")
                if lang_key == "default" and not isinstance(lang_value, str):
                    raise ValueError("lang.default must be a string")
                normalized_lang[lang_key] = lang_value
            normalized[key] = normalized_lang

        if key == "acp_suppress_script_patterns":
            if isinstance(value, str):
                normalized[key] = value
            elif isinstance(value, list) and all(isinstance(item, str) for item in value):
                normalized[key] = value
            else:
                raise ValueError("acp_suppress_script_patterns must be a string or list of strings")

        if key == "llm":
            if not isinstance(value, dict):
                raise ValueError("llm must be an object")
            normalized_llm = {}
            for llm_key, llm_value in value.items():
                if llm_key not in _LLM_CONFIG_KEYS:
                    if strict:
                        raise ValueError(f"Unknown runtime config key: llm.{llm_key}")
                    continue
                if llm_key in ("default_main_profile_ref", "default_task_profile_ref"):
                    if llm_value in (None, ""):
                        normalized_llm[llm_key] = None
                    else:
                        try:
                            normalized_llm[llm_key] = model_profile_ref_to_dict(
                                parse_model_profile_ref(llm_value)
                            )
                        except ValueError as exc:
                            raise ValueError(str(exc)) from exc
            normalized[key] = normalized_llm
    return normalized
