"""Application service for workspace ACL identity references.

Workspace metadata stores usernames, while the auth server owns identities.
This module keeps that cross-system validation out of the filesystem storage
layer and serializes validated references with the existing deletion flow.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Iterable

from api.auth.user_deletion import identity_reference_guard
from workspace.workspaces import (
    _normalize_workspace_members as normalize_workspace_members,
    _normalize_workspace_owner as normalize_workspace_owner,
    create_workspace,
    update_workspace,
    update_workspace_authorized,
)


logger = logging.getLogger(__name__)


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
        if (
            not isinstance(username, str)
            or not username
            or username != username.strip()
            or username in usernames
        ):
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
    **fields: Any,
) -> dict[str, Any]:
    normalized_owner = normalize_workspace_owner(owner)
    if normalized_owner is None:
        return update_workspace_authorized(workspace_id, user, owner=None, **fields)
    return _with_validated_identities(
        auth_backend,
        (normalized_owner,),
        field="owner",
        mutation=lambda: update_workspace_authorized(
            workspace_id,
            user,
            owner=normalized_owner,
            **fields,
        ),
    )


def update_workspace_owner_with_validation(
    auth_backend: Any,
    workspace_id: str,
    owner: Any,
) -> dict[str, Any]:
    normalized_owner = normalize_workspace_owner(owner)
    if normalized_owner is None:
        return update_workspace(workspace_id, owner=None)
    return _with_validated_identities(
        auth_backend,
        (normalized_owner,),
        field="owner",
        mutation=lambda: update_workspace(workspace_id, owner=normalized_owner),
    )
