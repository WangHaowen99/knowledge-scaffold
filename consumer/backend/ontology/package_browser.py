"""Safe, read-only browsing helpers for installed ontology packages."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from pathlib import Path
from typing import Any, Iterable, Optional


EXCLUDED_DIRECTORY_NAMES = frozenset(
    {"origin_data", "origin-data", "datasets", "dataset", "__pycache__"}
)
VIEWABLE_TEXT_EXTENSIONS = frozenset(
    {".md", ".yaml", ".yml", ".json", ".py", ".txt", ".csv", ".sql"}
)
MAX_VIEW_BYTES = 2 * 1024 * 1024

_WINDOWS_DRIVE_PATTERN = re.compile(r"^[A-Za-z]:")


class PackageBrowserError(ValueError):
    """A stable browser error that routes can translate into an HTTP response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def build_package_tree(
    root: Path,
    *,
    trusted_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """Return a deterministic tree without following hidden or symbolic links."""
    package_root = _validated_root(root, trusted_root=trusted_root)
    return _build_tree(package_root, package_root)


def list_package_files(
    root: Path,
    *,
    recursive: bool,
    allowed_extensions: Optional[Iterable[str]] = None,
    include_hidden_files: bool = False,
    trusted_root: Optional[Path] = None,
) -> list[dict[str, Any]]:
    """List safe regular files below ``root`` using package-browser filters."""
    package_root = _validated_root(
        root,
        trusted_root=trusted_root,
        allow_missing=True,
    )
    if not package_root.exists():
        return []

    normalized_extensions = _normalize_extensions(allowed_extensions)
    files: list[dict[str, Any]] = []
    for path in _walk_files(
        package_root,
        package_root,
        recursive=recursive,
        include_hidden_files=include_hidden_files,
    ):
        if normalized_extensions is not None and path.suffix.lower() not in normalized_extensions:
            continue
        try:
            stat_result = path.stat()
        except OSError:
            continue
        files.append(
            {
                "name": path.relative_to(package_root).as_posix(),
                "size": stat_result.st_size,
                "updated_at": int(stat_result.st_mtime),
            }
        )
    files.sort(key=lambda item: str(item["name"]).lower())
    return files


def resolve_package_file(
    root: Path,
    raw_path: str,
    *,
    allow_hidden_file: bool = False,
    trusted_root: Optional[Path] = None,
) -> Path:
    """Resolve a user path to a non-symlink regular file inside ``root``."""
    package_root = _validated_root(root, trusted_root=trusted_root)
    parts = _normalize_relative_parts(raw_path, allow_hidden_file=allow_hidden_file)
    candidate = package_root
    for part in parts:
        candidate = candidate / part
        try:
            candidate_stat = candidate.lstat()
        except FileNotFoundError as exc:
            raise PackageBrowserError("not_found", "File not found") from exc
        except OSError as exc:
            raise PackageBrowserError("invalid_path", "Invalid file path") from exc
        if stat.S_ISLNK(candidate_stat.st_mode):
            raise PackageBrowserError("invalid_path", "Symbolic links are not allowed")

    if not stat.S_ISREG(candidate_stat.st_mode):
        raise PackageBrowserError("not_found", "File not found")
    try:
        candidate.resolve(strict=True).relative_to(package_root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise PackageBrowserError("invalid_path", "Invalid file path") from exc
    return candidate


def read_package_text(
    root: Path,
    raw_path: str,
    *,
    allowed_extensions: Optional[Iterable[str]] = VIEWABLE_TEXT_EXTENSIONS,
    max_bytes: int = MAX_VIEW_BYTES,
    allow_hidden_file: bool = False,
    trusted_root: Optional[Path] = None,
    include_sha256: bool = False,
) -> dict[str, Any]:
    """Read one bounded UTF-8 text file from an ontology package."""
    file_path = resolve_package_file(
        root,
        raw_path,
        allow_hidden_file=allow_hidden_file,
        trusted_root=trusted_root,
    )
    normalized_extensions = _normalize_extensions(allowed_extensions)
    if normalized_extensions is not None and file_path.suffix.lower() not in normalized_extensions:
        raise PackageBrowserError("not_viewable", "File type not viewable")

    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise PackageBrowserError("read_failed", "Failed to inspect file") from exc
    if size > max(0, int(max_bytes)):
        raise PackageBrowserError("too_large", "File too large to view online")

    try:
        raw = file_path.read_bytes()
    except OSError as exc:
        raise PackageBrowserError("read_failed", "Failed to read file") from exc
    try:
        content = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageBrowserError("invalid_encoding", "File encoding must be UTF-8") from exc
    if "\x00" in content:
        raise PackageBrowserError("not_viewable", "Binary file is not viewable")

    package_root = _validated_root(root, trusted_root=trusted_root)
    result = {
        "path": file_path.relative_to(package_root).as_posix(),
        "name": file_path.name,
        "size": size,
        "content": content,
    }
    if include_sha256:
        # Keep the digest tied to the exact bytes that were decoded.  Callers
        # must not recreate it from text because BOM/newline handling can make
        # a decoded-text round trip differ from the package artifact.
        result["sha256"] = hashlib.sha256(raw).hexdigest()
    return result


def _validated_root(
    root: Path,
    *,
    trusted_root: Optional[Path] = None,
    allow_missing: bool = False,
) -> Path:
    candidate = _absolute_path(root)
    boundary = (
        _absolute_path(trusted_root)
        if trusted_root is not None
        else candidate.parent
    )
    _reject_symlink_components(
        boundary,
        candidate,
        allow_missing=allow_missing,
    )
    if not candidate.exists():
        if allow_missing:
            return candidate
        raise PackageBrowserError("not_found", "Package directory not found")
    if not candidate.is_dir():
        raise PackageBrowserError("invalid_path", "Package root must be a directory")
    return candidate


def _absolute_path(path: Path) -> Path:
    """Return a lexical absolute path without resolving symbolic links."""
    return Path(os.path.abspath(os.fspath(path)))


def _reject_symlink_components(
    boundary: Path,
    target: Path,
    *,
    allow_missing: bool,
) -> None:
    """Reject links below a caller-selected trust boundary, excluding the boundary."""
    try:
        relative = target.relative_to(boundary)
    except ValueError as exc:
        raise PackageBrowserError(
            "invalid_path",
            "Package root is outside the trusted boundary",
        ) from exc

    current = boundary
    for part in relative.parts:
        current = current / part
        try:
            current_stat = current.lstat()
        except FileNotFoundError:
            if allow_missing:
                return
            raise PackageBrowserError("not_found", "Package directory not found")
        except OSError as exc:
            raise PackageBrowserError("invalid_path", "Invalid package path") from exc
        if stat.S_ISLNK(current_stat.st_mode):
            raise PackageBrowserError("invalid_path", "Symbolic links are not allowed")


def _normalize_relative_parts(
    raw_path: str,
    *,
    allow_hidden_file: bool,
) -> tuple[str, ...]:
    raw = str(raw_path or "").strip()
    normalized = raw.replace("\\", "/")
    has_invalid_prefix = normalized.startswith("/") or bool(_WINDOWS_DRIVE_PATTERN.match(normalized))
    if not normalized or has_invalid_prefix or "\x00" in normalized:
        raise PackageBrowserError("invalid_path", "Invalid file path")

    parts = tuple(normalized.split("/"))
    for index, part in enumerate(parts):
        is_final_part = index == len(parts) - 1
        is_hidden_disallowed = part.startswith(".") and not (allow_hidden_file and is_final_part)
        is_excluded_directory = part.lower() in EXCLUDED_DIRECTORY_NAMES
        if any((not part, part in {".", ".."}, is_hidden_disallowed, is_excluded_directory)):
            raise PackageBrowserError("invalid_path", "Invalid file path")
    return parts


def _normalize_extensions(extensions: Optional[Iterable[str]]) -> Optional[frozenset[str]]:
    if extensions is None:
        return None
    normalized = {
        value if value.startswith(".") else ".{}".format(value)
        for item in extensions
        if (value := str(item or "").strip().lower())
    }
    return frozenset(normalized)


def _safe_children(directory: Path) -> list[Path]:
    try:
        return sorted(directory.iterdir(), key=_child_sort_key)
    except OSError:
        return []


def _child_sort_key(path: Path) -> tuple[bool, str]:
    try:
        is_file = stat.S_ISREG(path.lstat().st_mode)
    except OSError:
        is_file = True
    return is_file, path.name.lower()


def _build_tree(root: Path, directory: Path) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    for child in _safe_children(directory):
        if (
            child.name.startswith(".")
            or child.name.lower() in EXCLUDED_DIRECTORY_NAMES
            or child.is_symlink()
        ):
            continue
        relative_path = child.relative_to(root).as_posix()
        if child.is_dir():
            entries.append(
                {
                    "name": child.name,
                    "path": relative_path,
                    "type": "dir",
                    "children": _build_tree(root, child),
                }
            )
            continue
        if not child.is_file():
            continue
        try:
            size = child.stat().st_size
        except OSError:
            continue
        entries.append(
            {
                "name": child.name,
                "path": relative_path,
                "type": "file",
                "size": size,
                "viewable": child.suffix.lower() in VIEWABLE_TEXT_EXTENSIONS and size <= MAX_VIEW_BYTES,
            }
        )
    return entries


def _walk_files(
    root: Path,
    directory: Path,
    *,
    recursive: bool,
    include_hidden_files: bool,
) -> Iterable[Path]:
    for child in _safe_children(directory):
        if child.is_symlink():
            continue
        if child.is_dir():
            if (
                recursive
                and not child.name.startswith(".")
                and child.name.lower() not in EXCLUDED_DIRECTORY_NAMES
            ):
                yield from _walk_files(
                    root,
                    child,
                    recursive=True,
                    include_hidden_files=include_hidden_files,
                )
            continue
        if not child.is_file():
            continue
        if child.name.startswith(".") and not include_hidden_files:
            continue
        try:
            child.resolve(strict=True).relative_to(root)
        except (OSError, ValueError):
            continue
        yield child
