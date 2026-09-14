# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Crash-recoverable portable directory replacement."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path


_BACKUP_SEPARATOR = ".replace-backup-"
_TRANSACTION_MARKER = ".replace-transaction-v1"


class DirectoryReplacementRecoveryError(RuntimeError):
    """Raised when an interrupted replacement cannot be recovered safely."""


def _sync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags, 0o600)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _write_transaction_marker(backup_root: Path) -> None:
    marker = backup_root / _TRANSACTION_MARKER
    descriptor = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(b"1\n")
            stream.flush()
            os.fsync(stream.fileno())
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    _sync_directory(backup_root)


def portable_replace_directory(*, staged_dir: Path, target_dir: Path) -> None:
    """Publish a staged directory with a durable rollback point.

    Platforms without an atomic directory exchange necessarily use two rename
    operations. A fixed marker and same-filesystem backup make every crash state
    recoverable before application services start.
    """
    staged_dir = Path(staged_dir)
    target_dir = Path(target_dir)
    backup_root = Path(
        tempfile.mkdtemp(
            prefix=".{}{}".format(target_dir.name, _BACKUP_SEPARATOR),
            dir=str(target_dir.parent),
        )
    )
    backup_dir = backup_root / "previous"
    _write_transaction_marker(backup_root)
    _sync_directory(target_dir.parent)
    target_dir.replace(backup_dir)
    _sync_directory(target_dir.parent)
    _sync_directory(backup_root)
    try:
        staged_dir.replace(target_dir)
        _sync_directory(target_dir.parent)
    except BaseException:
        backup_dir.replace(target_dir)
        _sync_directory(target_dir.parent)
        shutil.rmtree(backup_root)
        _sync_directory(target_dir.parent)
        raise
    shutil.rmtree(backup_root)
    _sync_directory(target_dir.parent)


def _target_name_from_backup(backup_root: Path) -> str:
    name = backup_root.name
    if not name.startswith(".") or _BACKUP_SEPARATOR not in name:
        return ""
    target_name, separator, suffix = name[1:].rpartition(_BACKUP_SEPARATOR)
    if not separator or not suffix or Path(target_name).name != target_name:
        return ""
    return target_name


def _recover_backup_root(backup_root: Path) -> bool:
    if backup_root.is_symlink() or not backup_root.is_dir():
        return False
    marker = backup_root / _TRANSACTION_MARKER
    if not marker.is_file() or marker.is_symlink():
        return False
    target_name = _target_name_from_backup(backup_root)
    if not target_name:
        raise DirectoryReplacementRecoveryError(
            "directory replacement marker has an invalid target"
        )
    target_dir = backup_root.parent / target_name
    backup_dir = backup_root / "previous"
    if target_dir.is_symlink() or (target_dir.exists() and not target_dir.is_dir()):
        raise DirectoryReplacementRecoveryError(
            "directory replacement target is unsafe"
        )
    if target_dir.is_dir():
        shutil.rmtree(backup_root)
        _sync_directory(target_dir.parent)
        return True
    if backup_dir.is_symlink() or not backup_dir.is_dir():
        raise DirectoryReplacementRecoveryError(
            "directory replacement rollback tree is unavailable"
        )
    backup_dir.replace(target_dir)
    _sync_directory(target_dir.parent)
    shutil.rmtree(backup_root)
    _sync_directory(target_dir.parent)
    return True


def recover_directory_replacements(root: Path) -> int:
    """Recover marked replacements directly below one trusted target parent."""
    root = Path(root)
    if root.is_symlink() or not root.is_dir():
        return 0
    recovered = 0
    for candidate in root.iterdir():
        if _BACKUP_SEPARATOR not in candidate.name or candidate.is_symlink():
            continue
        if _recover_backup_root(candidate):
            recovered += 1
    return recovered


def recover_workspace_ontology_replacements(workspaces_root: Path) -> int:
    """Recover ontology replacements for all persisted workspaces at startup."""
    root = Path(workspaces_root)
    if root.is_symlink() or not root.is_dir():
        return 0
    recovered = 0
    for workspace_dir in root.iterdir():
        if workspace_dir.is_symlink() or not workspace_dir.is_dir():
            continue
        recovered += recover_directory_replacements(workspace_dir / "ontologies")
    return recovered
