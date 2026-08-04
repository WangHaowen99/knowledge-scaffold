"""Workspace and session scoped Speedscope file access helpers."""

from __future__ import annotations

from pathlib import Path

from integration.observability.speedscope_io import (
    delete_speedscope_session_files,
    speedscope_session_file_name,
    speedscope_session_path,
)
from integration.observability.workspace_config import get_workspace_observability_log_dir
from utils.identifiers import resolve_session_storage_dir, validate_session_id
from workspace.workspaces import get_workspace_paths


def get_workspace_speedscope_dir(workspace_id: str) -> Path:
    return get_workspace_observability_log_dir(workspace_id) / "speedscope"


def get_user_speedscope_session_path(
    workspace_id: str,
    session_id: str,
    user_id: str,
) -> Path:
    return speedscope_session_path(
        get_workspace_speedscope_dir(workspace_id),
        validate_session_id(session_id),
        user_id=user_id,
    )


def find_speedscope_session_file(
    workspace_id: str,
    session_id: str,
    *,
    user_id: str | None = None,
    any_user: bool = False,
) -> Path | None:
    """Find a trace only when its owning session still exists.

    Non-admin callers pass their authenticated user id. Admin callers can set
    ``any_user`` to search the current workspace's user stores.
    """
    normalized_session_id = validate_session_id(session_id)
    speedscope_dir = get_workspace_speedscope_dir(workspace_id)
    if user_id:
        candidate = get_user_speedscope_session_path(
            workspace_id,
            normalized_session_id,
            user_id,
        )
        return candidate if _is_regular_file(candidate) else None
    if not any_user:
        return None

    paths = get_workspace_paths(workspace_id)
    users_root = paths.users_runtime_dir
    if not users_root.is_dir() or users_root.is_symlink():
        return None
    filename_candidates = []
    for user_dir in users_root.iterdir():
        if not user_dir.is_dir() or user_dir.is_symlink():
            continue
        user_key = user_dir.name
        if not _chat_session_exists(user_dir / "chats", normalized_session_id):
            continue
        candidate = speedscope_dir / user_key / speedscope_session_file_name(normalized_session_id)
        if _is_regular_file(candidate):
            filename_candidates.append(candidate)
    return filename_candidates[0] if filename_candidates else None


def remove_speedscope_session_files(
    workspace_id: str,
    session_id: str,
    *,
    user_id: str | None = None,
) -> int:
    return delete_speedscope_session_files(
        get_workspace_speedscope_dir(workspace_id),
        validate_session_id(session_id),
        user_id=user_id,
    )


def _chat_session_exists(chat_root: Path, session_id: str) -> bool:
    if not chat_root.is_dir() or chat_root.is_symlink():
        return False
    try:
        snapshot = resolve_session_storage_dir(chat_root, session_id) / "snapshot.json"
    except ValueError:
        return False
    if _is_regular_file(snapshot):
        return True
    legacy = chat_root / "{}.json".format(session_id)
    return _is_regular_file(legacy)


def _is_regular_file(path: Path) -> bool:
    return path.is_file() and not path.is_symlink()
