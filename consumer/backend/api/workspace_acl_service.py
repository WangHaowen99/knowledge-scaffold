"""Application service for workspace ACL identity references.

Workspace metadata stores usernames, while the auth server owns identities.
This module keeps that cross-system validation out of the filesystem storage
layer and serializes validated references with the existing deletion flow.
"""

from __future__ import annotations

import hashlib
import logging
import secrets
from typing import Any, Callable, Iterable

from api.auth.user_deletion import identity_reference_guard
from workspace.workspaces import _normalize_workspace_members as normalize_workspace_members
from workspace.workspaces import _normalize_workspace_owner as normalize_workspace_owner
from workspace.workspaces import (
    create_workspace,
    get_workspace,
    list_workspaces,
    update_workspace,
    update_workspace_authorized,
)

logger = logging.getLogger(__name__)

_PERSONAL_WORKSPACE_ID_PREFIX = "home-"


def _personal_workspace_id(username: str) -> str:
    digest = hashlib.sha256(username.encode("utf-8")).hexdigest()[:20]
    return "{}{}".format(_PERSONAL_WORKSPACE_ID_PREFIX, digest)


def ensure_user_personal_workspace(
    auth_backend: Any,
    user: Any,
) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    if getattr(user, "role", None) == "admin":
        return None, list_workspaces()

    username = normalize_workspace_owner(getattr(user, "username", None))
    if username is None:
        raise ValueError("Authenticated user has no username")

    with identity_reference_guard((username,)):
        available_workspaces = list_workspaces()
        base_workspace_id = _personal_workspace_id(username)
        owned = [workspace for workspace in available_workspaces if workspace.get("owner") == username]
        if owned:
            personal_workspace = min(
                owned,
                key=lambda workspace: (
                    str(workspace.get("created_at") or ""),
                    workspace["id"],
                ),
            )
            workspace_id = personal_workspace["id"]
            suffix = workspace_id[len(base_workspace_id) + 1 :]
            is_managed_personal_id = workspace_id == base_workspace_id or (
                workspace_id.startswith(base_workspace_id + "-")
                and len(suffix) == 8
                and all(character in "0123456789abcdef" for character in suffix)
            )
            if (
                is_managed_personal_id
                and personal_workspace.get("name") == "{} 的工作空间".format(username)
                and personal_workspace.get("description") == "用户个人默认工作空间"
            ):
                personal_workspace = update_workspace(
                    workspace_id,
                    name="默认工作空间",
                )
                available_workspaces = [
                    personal_workspace if workspace["id"] == workspace_id else workspace
                    for workspace in available_workspaces
                ]
            return personal_workspace, available_workspaces

        existing_usernames = _load_auth_usernames(auth_backend)
        if username not in existing_usernames:
            raise WorkspaceIdentityNotFoundError("owner", (username,))

        workspace_id = base_workspace_id
        for attempt in range(9):
            try:
                personal_workspace = create_workspace(
                    name="默认工作空间",
                    description="用户个人默认工作空间",
                    workspace_id=workspace_id,
                    set_as_default=False,
                    auto_set_default=False,
                    owner=username,
                    allow_reserved_id=True,
                )
                break
            except ValueError as exc:
                try:
                    personal_workspace = get_workspace(workspace_id)
                except KeyError:
                    raise exc
                if personal_workspace.get("owner") == username:
                    break
                if attempt == 8:
                    raise ValueError("无法分配个人工作空间 ID") from exc
                workspace_id = "{}-{}".format(
                    base_workspace_id,
                    secrets.token_hex(4),
                )
        available_workspaces.append(personal_workspace)
        return personal_workspace, available_workspaces


class WorkspaceIdentityDirectoryUnavailableError(RuntimeError):
    """Raised when ACL references cannot be checked against the auth store."""

    code = "AUTH_DIRECTORY_UNAVAILABLE"
    detail = "认证服务暂不可用，无法校验工作空间用户"

    def __init__(self) -> None:
        super().__init__(self.detail)


class WorkspaceIdentityNotFoundError(ValueError):
    """Raised when a requested workspace owner or member does not exist."""

    def __init__(self, field: str, usernames: Iterable[str]) -> None:
        normalized = tuple(dict.fromkeys(str(item).strip() for item in usernames if str(item).strip()))
        self.field = field
        self.usernames = normalized
        if field == "owner":
            self.code = "WORKSPACE_OWNER_NOT_FOUND"
            self.detail = "工作空间负责人 {} 不存在".format(normalized[0] if normalized else "")
        elif len(normalized) == 1:
            self.code = "WORKSPACE_MEMBER_NOT_FOUND"
            self.detail = "成员用户 {} 不存在".format(normalized[0])
        else:
            self.code = "WORKSPACE_MEMBERS_NOT_FOUND"
            self.detail = "以下成员用户不存在：{}".format("、".join(normalized))
        super().__init__(self.detail)


def _load_auth_usernames(auth_backend: Any) -> set[str]:
    if auth_backend is None:
        raise WorkspaceIdentityDirectoryUnavailableError()
    try:
        entries = auth_backend.list_users()
    except Exception as exc:
        logger.warning("workspace ACL identity directory lookup failed: %s", exc)
        raise WorkspaceIdentityDirectoryUnavailableError() from exc
    if not isinstance(entries, list):
        raise WorkspaceIdentityDirectoryUnavailableError()

    usernames: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            raise WorkspaceIdentityDirectoryUnavailableError()
        username = entry.get("username")
        if not isinstance(username, str) or not username or username != username.strip() or username in usernames:
            raise WorkspaceIdentityDirectoryUnavailableError()
        usernames.add(username)
    return usernames


def _with_validated_identities(
    auth_backend: Any,
    usernames: Iterable[str],
    *,
    field: str,
    mutation: Callable[[], Any],
) -> Any:
    requested = tuple(dict.fromkeys(str(item).strip() for item in usernames if str(item).strip()))
    if not requested:
        return mutation()
    with identity_reference_guard(requested):
        existing = _load_auth_usernames(auth_backend)
        missing = tuple(username for username in requested if username not in existing)
        if missing:
            raise WorkspaceIdentityNotFoundError(field, missing)
        return mutation()


def validate_workspace_member(auth_backend: Any, username: Any) -> str:
    normalized = normalize_workspace_members([username])[0]
    return _with_validated_identities(
        auth_backend,
        (normalized,),
        field="members",
        mutation=lambda: normalized,
    )


def replace_workspace_members(
    auth_backend: Any,
    workspace_id: str,
    user: Any,
    members: Any,
) -> dict[str, Any]:
    normalized = normalize_workspace_members(members)
    return _with_validated_identities(
        auth_backend,
        normalized,
        field="members",
        mutation=lambda: update_workspace_authorized(
            workspace_id,
            user,
            members=normalized,
        ),
    )


def create_workspace_with_validated_owner(
    auth_backend: Any,
    *,
    owner: Any,
    **fields: Any,
) -> dict[str, Any]:
    normalized_owner = normalize_workspace_owner(owner)
    if normalized_owner is None:
        return create_workspace(owner=None, **fields)
    return _with_validated_identities(
        auth_backend,
        (normalized_owner,),
        field="owner",
        mutation=lambda: create_workspace(owner=normalized_owner, **fields),
    )


def update_workspace_authorized_with_validated_owner(
    auth_backend: Any,
    workspace_id: str,
    user: Any,
    *,
    owner: Any,
    enforce_workspace_limit: bool = True,
    **fields: Any,
) -> dict[str, Any]:
    normalized_owner = normalize_workspace_owner(owner)
    if normalized_owner is None:
        return update_workspace_authorized(
            workspace_id,
            user,
            owner=None,
            enforce_workspace_limit=enforce_workspace_limit,
            **fields,
        )
    return _with_validated_identities(
        auth_backend,
        (normalized_owner,),
        field="owner",
        mutation=lambda: update_workspace_authorized(
            workspace_id,
            user,
            owner=normalized_owner,
            enforce_workspace_limit=enforce_workspace_limit,
            **fields,
        ),
    )


def update_workspace_owner_with_validation(
    auth_backend: Any,
    workspace_id: str,
    owner: Any,
    *,
    enforce_workspace_limit: bool = True,
) -> dict[str, Any]:
    normalized_owner = normalize_workspace_owner(owner)
    if normalized_owner is None:
        return update_workspace(
            workspace_id,
            owner=None,
            enforce_workspace_limit=enforce_workspace_limit,
        )
    return _with_validated_identities(
        auth_backend,
        (normalized_owner,),
        field="owner",
        mutation=lambda: update_workspace(
            workspace_id,
            owner=normalized_owner,
            enforce_workspace_limit=enforce_workspace_limit,
        ),
    )
