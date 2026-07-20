"""Map project-relative scripts to sandbox-visible same-path mounts."""

from __future__ import annotations

from pathlib import Path


def posix_path_string(path: Path | str) -> str:
    """Return the path as a POSIX-style string (forward slashes)."""
    return str(path).replace("\\", "/")


def normalize_project_relative(path: str) -> str:
    normalized = posix_path_string(path)
    if normalized.startswith("./"):
        normalized = normalized[2:]
    return normalized


def to_sandbox_script_path(
    project_relative: str,
    *,
    project_root: Path,
) -> str:
    """Convert a validated project-relative script path to a sandbox path."""
    normalized = normalize_project_relative(project_relative)
    return posix_path_string((project_root.resolve() / normalized).resolve())
