# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Build an application runtime fingerprint from shipped files and dependencies."""

from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import json
import os
import platform
from pathlib import Path
from typing import Iterable, Mapping, Sequence


APPLICATION_BUILD_REVISION_FILENAME = ".app-runtime-revision"
_IGNORED_DIRECTORY_NAMES = frozenset(
    {
        ".git",
        ".onto-runtime",
        ".pytest_cache",
        "__pycache__",
        "data",
        "dist",
        "env",
        "htmlcov",
        "logs",
        "tests",
        "venv",
    }
)
_IGNORED_FILE_SUFFIXES = frozenset({".pyc", ".pyo"})


def _included_application_file(path: Path, root: Path) -> bool:
    relative = path.relative_to(root)
    if any(part in _IGNORED_DIRECTORY_NAMES for part in relative.parts[:-1]):
        return False
    if relative.name == APPLICATION_BUILD_REVISION_FILENAME:
        return False
    if relative.name == ".env" or relative.name.startswith(".env."):
        return False
    return path.is_symlink() or (
        path.is_file() and path.suffix.casefold() not in _IGNORED_FILE_SUFFIXES
    )


def _application_file_entries(root: Path) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for current_root, directory_names, file_names in os.walk(
        root,
        topdown=True,
        followlinks=False,
    ):
        current = Path(current_root)
        symlink_directories = [
            name for name in directory_names if (current / name).is_symlink()
        ]
        included_directories = []
        for name in directory_names:
            if name in _IGNORED_DIRECTORY_NAMES:
                continue
            if (current / name).is_symlink():
                continue
            included_directories.append(name)
        directory_names[:] = sorted(included_directories)
        for name in sorted([*file_names, *symlink_directories]):
            path = current / name
            if not _included_application_file(path, root):
                continue
            relative = path.relative_to(root).as_posix()
            if path.is_symlink():
                digest = hashlib.sha256(os.readlink(path).encode("utf-8")).hexdigest()
                kind = "symlink"
            else:
                digest = hashlib.sha256(path.read_bytes()).hexdigest()
                kind = "file"
            entries.append({"path": relative, "kind": kind, "sha256": digest})
    entries.sort(key=lambda item: item["path"])
    return entries


def _installed_distribution_entries() -> list[dict[str, str]]:
    entries = {
        (
            str(distribution.metadata.get("Name") or "").strip().casefold(),
            str(distribution.version or "").strip(),
        )
        for distribution in importlib.metadata.distributions()
    }
    return [
        {"name": name, "version": version}
        for name, version in sorted(entries)
        if name and version
    ]


def calculate_application_build_revision(
    application_root: Path,
    *,
    distributions: Iterable[tuple[str, str]] | None = None,
    runtime: Mapping[str, str] | None = None,
) -> str:
    """Hash the complete shipped application, installed packages and Python ABI."""
    root = Path(application_root).resolve()
    if not root.is_dir():
        raise FileNotFoundError("application root does not exist: {}".format(root))
    if distributions is None:
        distribution_entries = _installed_distribution_entries()
    else:
        normalized_distributions = set()
        for name, version in distributions:
            normalized_name = str(name).strip().casefold()
            normalized_version = str(version).strip()
            if normalized_name and normalized_version:
                normalized_distributions.add((normalized_name, normalized_version))
        distribution_entries = [
            {"name": name, "version": version}
            for name, version in sorted(normalized_distributions)
        ]
    runtime_entry = dict(
        runtime
        or {
            "implementation": platform.python_implementation(),
            "python_version": platform.python_version(),
            "architecture": platform.machine(),
        }
    )
    material = {
        "version": 1,
        "runtime": runtime_entry,
        "distributions": distribution_entries,
        "application_files": _application_file_entries(root),
    }
    payload = json.dumps(
        material,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def write_application_build_revision(application_root: Path, output_path: Path) -> str:
    revision = calculate_application_build_revision(application_root)
    output = Path(output_path)
    output.write_text(revision + "\n", encoding="utf-8")
    return revision


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    args = parser.parse_args(argv)
    write_application_build_revision(Path(args.root), Path(args.output))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
