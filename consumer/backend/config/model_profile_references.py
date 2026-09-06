"""Aggregate workspace model-profile references without exposing runtime config."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from threading import RLock
from typing import Any, Iterator

from config.model_profile_refs import ModelProfileRef, parse_model_profile_ref
from workspace.workspaces import (
    get_workspace_catalog_snapshot,
    get_workspace_config_generation,
    get_workspace_paths,
    validate_workspace_runtime_config_document,
)


_REFERENCE_FIELDS = (
    "default_main_profile_ref",
    "default_task_profile_ref",
)
_IS_WINDOWS = os.name == "nt"
_MODEL_PROFILE_REFERENCE_LOCK = RLock()


class InvalidModelProfileFilterError(ValueError):
    """The optional profile filter does not match the profile-id contract."""


def _is_valid_windows_handle(raw_handle: object, invalid_handle: object) -> bool:
    if isinstance(raw_handle, bool):
        return False
    if not isinstance(raw_handle, int):
        return False
    if raw_handle <= 0:
        return False
    return raw_handle != invalid_handle


@contextmanager
def model_profile_reference_guard() -> Iterator[None]:
    """Serialize supported global-profile and workspace-reference mutations."""

    with _MODEL_PROFILE_REFERENCE_LOCK:
        yield


def _open_windows_runtime_config_descriptor(path: os.PathLike[str]) -> int:
    """Open a Windows file handle without traversing a final reparse point."""
    if not _IS_WINDOWS:
        raise OSError(errno.ENOTSUP, "secure Windows file open is unavailable")

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class ByHandleFileInformation(ctypes.Structure):
        _fields_ = [
            ("file_attributes", wintypes.DWORD),
            ("creation_time", wintypes.FILETIME),
            ("last_access_time", wintypes.FILETIME),
            ("last_write_time", wintypes.FILETIME),
            ("volume_serial_number", wintypes.DWORD),
            ("file_size_high", wintypes.DWORD),
            ("file_size_low", wintypes.DWORD),
            ("number_of_links", wintypes.DWORD),
            ("file_index_high", wintypes.DWORD),
            ("file_index_low", wintypes.DWORD),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        ctypes.c_void_p,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.GetFileInformationByHandle.argtypes = [
        wintypes.HANDLE,
        ctypes.POINTER(ByHandleFileInformation),
    ]
    kernel32.GetFileInformationByHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    kernel32.CloseHandle.restype = wintypes.BOOL

    handle = kernel32.CreateFileW(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001 | 0x00000002 | 0x00000004,  # SHARE_READ | WRITE | DELETE
        None,
        3,  # OPEN_EXISTING
        0x00000080 | 0x00200000,  # NORMAL | OPEN_REPARSE_POINT
        None,
    )
    raw_handle = getattr(handle, "value", handle)
    invalid_handle = ctypes.c_void_p(-1).value
    if not _is_valid_windows_handle(raw_handle, invalid_handle):
        raise OSError(ctypes.get_last_error(), "secure runtime config open failed")

    owns_handle = True
    try:
        information = ByHandleFileInformation()
        if not kernel32.GetFileInformationByHandle(
            handle,
            ctypes.byref(information),
        ):
            raise OSError(
                ctypes.get_last_error(),
                "secure runtime config metadata read failed",
            )
        if information.file_attributes & 0x00000400:  # FILE_ATTRIBUTE_REPARSE_POINT
            raise ValueError("workspace runtime config must not be a symlink")
        if information.file_attributes & 0x00000010:  # FILE_ATTRIBUTE_DIRECTORY
            raise ValueError("workspace runtime config must be a regular file")
        descriptor = msvcrt.open_osfhandle(
            raw_handle,
            os.O_RDONLY | getattr(os, "O_BINARY", 0),
        )
        owns_handle = False
        return descriptor
    finally:
        if owns_handle:
            kernel32.CloseHandle(handle)


def _open_runtime_config_descriptor(path: os.PathLike[str]) -> int:
    """Open without following links or waiting on replaced special files."""
    nonblocking = getattr(os, "O_NONBLOCK", None)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if isinstance(nonblocking, int) and isinstance(nofollow, int):
        return os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_BINARY", 0)
            | nonblocking
            | nofollow,
            0o600,
        )
    if _IS_WINDOWS:
        return _open_windows_runtime_config_descriptor(path)
    raise OSError(errno.ENOTSUP, "secure runtime config open is unavailable")


def read_workspace_runtime_config(workspace_id: str) -> Mapping[str, Any]:
    """Read one workspace runtime-config document without default expansion."""
    workspace_dir = get_workspace_paths(workspace_id).workspace_dir
    if not workspace_dir.is_dir():
        raise FileNotFoundError("workspace directory is unavailable")
    config_path = workspace_dir / "runtime_config.json"
    try:
        path_metadata = config_path.lstat()
    except FileNotFoundError:
        return {}
    if stat.S_ISLNK(path_metadata.st_mode):
        raise ValueError("workspace runtime config must not be a symlink")
    if not stat.S_ISREG(path_metadata.st_mode):
        raise ValueError("workspace runtime config must be a regular file")

    descriptor: int | None = None
    try:
        descriptor = _open_runtime_config_descriptor(config_path)
        opened_metadata = os.fstat(descriptor)
        if not stat.S_ISREG(opened_metadata.st_mode):
            raise ValueError("workspace runtime config must be a regular file")
        if not os.path.samestat(path_metadata, opened_metadata):
            raise ValueError("workspace runtime config changed while being read")
        with os.fdopen(descriptor, "r", encoding="utf-8") as config_file:
            descriptor = None
            payload = json.load(config_file)
    except OSError as exc:
        if exc.errno == errno.ELOOP:
            raise ValueError("workspace runtime config must not be a symlink") from exc
        raise
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if not isinstance(payload, Mapping):
        raise ValueError("workspace runtime config must be an object")
    validate_workspace_runtime_config_document(
        payload,
        allow_legacy_default_profile_ref=True,
    )
    return payload


def get_model_profile_references(profile_id: str | None = None) -> dict[str, Any]:
    """Return a deterministic, secret-free inventory of workspace references."""

    with model_profile_reference_guard():
        return _get_model_profile_references_locked(profile_id)


def _get_model_profile_references_locked(
    profile_id: str | None = None,
) -> dict[str, Any]:
    profile_filter = _parse_profile_filter(profile_id)
    snapshot = get_workspace_catalog_snapshot()
    entries: list[dict[str, Any]] = []
    complete = True
    for workspace_id in sorted(snapshot.ordered_ids):
        workspace = snapshot.copy_item(workspace_id)
        entry: dict[str, Any] = {
            "workspace_id": workspace_id,
            "workspace_name": str(workspace.get("name") or ""),
            "config_generation": None,
            "references": [],
        }
        try:
            entry["config_generation"] = get_workspace_config_generation(workspace_id)
            runtime_config = read_workspace_runtime_config(workspace_id)
            parsed_references, references_complete = _parse_runtime_references(
                runtime_config
            )
            entry["references"] = parsed_references
            if not references_complete:
                complete = False
                entry["diagnostic"] = _safe_diagnostic(
                    ValueError("workspace runtime reference is invalid")
                )
        except Exception as exc:
            complete = False
            entry["diagnostic"] = _safe_diagnostic(exc)
        entries.append(entry)

    generation_payload = {
        "catalog_generation": snapshot.generation,
        "entries": entries,
    }
    encoded = json.dumps(
        generation_payload,
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    visible_entries = entries
    if profile_filter is not None:
        visible_entries = []
        for entry in entries:
            matching = []
            for reference in entry["references"]:
                if reference["scope"] != profile_filter.scope:
                    continue
                if reference["profile_id"] == profile_filter.id:
                    matching.append(reference)
            if not matching and "diagnostic" not in entry:
                continue
            visible_entry = dict(entry)
            visible_entry["references"] = matching
            visible_entries.append(visible_entry)
    return {
        "complete": complete,
        "entries": visible_entries,
        "generation": "sha256:" + hashlib.sha256(encoded).hexdigest(),
    }


def _parse_runtime_references(
    runtime_config: Mapping[str, Any],
) -> tuple[list[dict[str, str]], bool]:
    llm = runtime_config.get("llm", {})
    if llm is None:
        llm = {}
    if not isinstance(llm, Mapping):
        raise ValueError("workspace runtime llm config must be an object")

    references: list[dict[str, str]] = []
    complete = True
    for field in _REFERENCE_FIELDS:
        raw_reference = llm.get(field)
        if field == "default_main_profile_ref" and raw_reference in (None, ""):
            raw_reference = llm.get("default_profile_ref")
        try:
            parsed = parse_model_profile_ref(raw_reference)
        except (TypeError, ValueError):
            complete = False
            continue
        if parsed is None:
            continue
        references.append(
            {
                "field": field,
                "scope": parsed.scope,
                "profile_id": parsed.id,
            }
        )
    return references, complete


def _safe_diagnostic(exc: Exception) -> dict[str, str]:
    if isinstance(exc, ValueError):
        code = "runtime_config_invalid"
    elif isinstance(exc, FileNotFoundError):
        code = "workspace_unavailable"
    else:
        code = "runtime_config_unavailable"
    return {
        "code": code,
        "message": "Runtime model references could not be verified.",
    }


def _parse_profile_filter(profile_id: str | None) -> ModelProfileRef | None:
    if profile_id is None:
        return None
    raw_profile_id = str(profile_id).strip()
    if not raw_profile_id:
        raise InvalidModelProfileFilterError("profile_id is invalid")
    if len(raw_profile_id) > 90:
        raise InvalidModelProfileFilterError("profile_id is invalid")
    if any(character.isspace() for character in raw_profile_id):
        raise InvalidModelProfileFilterError("profile_id is invalid")
    try:
        parsed = parse_model_profile_ref(raw_profile_id)
    except (TypeError, ValueError) as exc:
        raise InvalidModelProfileFilterError("profile_id is invalid") from exc
    if parsed is None:
        raise InvalidModelProfileFilterError("profile_id is invalid")
    if len(parsed.id) > 80:
        raise InvalidModelProfileFilterError("profile_id is invalid")
    if any(character.isspace() for character in parsed.id):
        raise InvalidModelProfileFilterError("profile_id is invalid")
    if parsed.scope == "workspace" and ":" in parsed.id:
        raise InvalidModelProfileFilterError("profile_id is invalid")
    return parsed
