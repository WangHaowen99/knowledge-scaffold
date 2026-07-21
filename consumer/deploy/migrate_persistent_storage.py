#!/usr/bin/env python3
"""Normalize release storage paths to durable runtime-backed locations."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any


CANONICAL_PROJECT_ROOT = "/app/.onto-runtime"
CANONICAL_CHAT_STORAGE_DIR = "/app/.onto-runtime/data/chats"
CANONICAL_EXECUTION_STORAGE_DIR = "/app/.onto-runtime/data/executions"
_LEGACY_CHAT_VALUES = {"", "./data/chats", "/app/data/chats"}
_LEGACY_EXECUTION_VALUES = {"", "./data/executions", "/app/data/executions"}
_LEGACY_ARCHIVE_NAME = "legacy-backend-data.tar.gz"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    raw = json.loads(path.read_text(encoding="utf-8"))
    return raw if isinstance(raw, dict) else {}


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path.write_text(text, encoding="utf-8")
        tmp_path.replace(path)
    finally:
        tmp_path.unlink(missing_ok=True)


def _replace_env_value(lines: list[str], key: str, expected: str) -> tuple[list[str], bool]:
    changed = False
    replaced = False
    rendered: list[str] = []
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("#") or "=" not in line:
            rendered.append(line)
            continue
        current_key, current_value = line.split("=", 1)
        if current_key.strip() != key:
            rendered.append(line)
            continue
        replaced = True
        current_value = current_value.strip()
        if current_value != expected:
            rendered.append(f"{key}={expected}")
            changed = True
        else:
            rendered.append(line)
        continue
    if not replaced:
        rendered.append(f"{key}={expected}")
        changed = True
    return rendered, changed


def _normalize_env_file(path: Path, *, dry_run: bool) -> dict[str, Any]:
    original_lines = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    lines = list(original_lines)
    changed = False
    lines, step_changed = _replace_env_value(lines, "CHAT_STORAGE_DIR", CANONICAL_CHAT_STORAGE_DIR)
    changed = changed or step_changed
    lines, step_changed = _replace_env_value(lines, "EXECUTION_STORAGE_DIR", CANONICAL_EXECUTION_STORAGE_DIR)
    changed = changed or step_changed
    if changed and not dry_run:
        _atomic_write_text(path, "\n".join(lines) + "\n")
    return {
        "path": str(path),
        "changed": changed,
        "chat_storage_dir": CANONICAL_CHAT_STORAGE_DIR,
        "execution_storage_dir": CANONICAL_EXECUTION_STORAGE_DIR,
    }


def _normalize_system_config(path: Path, *, dry_run: bool) -> dict[str, Any]:
    payload = _read_json(path)
    storage = payload.get("storage")
    if not isinstance(storage, dict):
        storage = {}
        if payload:
            payload["storage"] = storage

    changed = False
    chat_value = str(storage.get("chat_storage_dir") or "").strip()
    if chat_value in _LEGACY_CHAT_VALUES:
        storage["chat_storage_dir"] = CANONICAL_CHAT_STORAGE_DIR
        changed = True

    execution_value = str(storage.get("execution_storage_dir") or "").strip()
    if execution_value in _LEGACY_EXECUTION_VALUES:
        storage["execution_storage_dir"] = CANONICAL_EXECUTION_STORAGE_DIR
        changed = True

    if payload and changed and not dry_run:
        _atomic_write_text(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")
    return {
        "path": str(path),
        "exists": path.exists(),
        "changed": changed,
        "chat_storage_dir": storage.get("chat_storage_dir"),
        "execution_storage_dir": storage.get("execution_storage_dir"),
    }


def _effective_container_storage_targets(
    env_report: dict[str, Any],
    system_config_report: dict[str, Any],
) -> dict[str, str]:
    config_chat = str(system_config_report.get("chat_storage_dir") or "").strip()
    config_execution = str(system_config_report.get("execution_storage_dir") or "").strip()
    return {
        "chat_storage_dir": config_chat or str(env_report["chat_storage_dir"]),
        "execution_storage_dir": config_execution or str(env_report["execution_storage_dir"]),
    }


def _container_path_to_host(root: Path, container_path: str) -> Path:
    normalized = str(container_path or "").strip()
    if not normalized:
        raise ValueError("container storage path is empty")
    if normalized == CANONICAL_PROJECT_ROOT:
        return root / ".onto-runtime"
    prefix = CANONICAL_PROJECT_ROOT + "/"
    if not normalized.startswith(prefix):
        raise ValueError("container storage path is outside canonical runtime root")
    return root / ".onto-runtime" / normalized[len(prefix):]


def _extract_archive(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    destination_root = destination.resolve()
    with tarfile.open(archive_path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.issym() or member.islnk():
                raise ValueError(f"legacy archive contains unsupported link: {member.name}")
            target_path = (destination_root / member.name).resolve()
            target_path.relative_to(destination_root)
        archive.extractall(destination_root)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _merge_legacy_tree(
    source_root: Path,
    destination_root: Path,
    *,
    dry_run: bool,
) -> dict[str, Any]:
    report = {
        "source_exists": source_root.exists(),
        "copied_files": 0,
        "copied_session_roots": 0,
        "skipped_identical_files": 0,
        "conflicts": [],
        "changed": False,
    }
    if not source_root.exists():
        return report

    source_files = sorted(path for path in source_root.rglob("*") if path.is_file())
    session_roots_copied: set[str] = set()
    conflicts: list[str] = []
    copied_files = 0
    skipped_files = 0

    for source_path in source_files:
        relative = source_path.relative_to(source_root)
        target_path = destination_root / relative
        if target_path.exists():
            if target_path.is_file() and _sha256(source_path) == _sha256(target_path):
                skipped_files += 1
                continue
            conflicts.append(str(relative).replace("\\", "/"))
            continue
        copied_files += 1
        if relative.parts:
            session_roots_copied.add(relative.parts[0])
        if not dry_run:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_path, target_path)

    if conflicts:
        report["conflicts"] = conflicts
        raise ValueError(
            "legacy storage import conflicts with existing runtime data: {}".format(
                ", ".join(conflicts[:10])
            )
        )

    report["copied_files"] = copied_files
    report["copied_session_roots"] = len(session_roots_copied)
    report["skipped_identical_files"] = skipped_files
    report["changed"] = copied_files > 0
    return report


def _import_legacy_container_storage(
    root: Path,
    *,
    dry_run: bool,
    legacy_backup_dir: Path | None,
    env_report: dict[str, Any],
    system_config_report: dict[str, Any],
) -> dict[str, Any]:
    report: dict[str, Any] = {
        "archive": None,
        "detected": False,
        "chat": {
            "source_exists": False,
            "copied_files": 0,
            "copied_session_roots": 0,
            "skipped_identical_files": 0,
            "conflicts": [],
            "changed": False,
        },
        "execution": {
            "source_exists": False,
            "copied_files": 0,
            "copied_session_roots": 0,
            "skipped_identical_files": 0,
            "conflicts": [],
            "changed": False,
        },
        "changed": False,
    }
    if legacy_backup_dir is None:
        return report

    archive_path = legacy_backup_dir / _LEGACY_ARCHIVE_NAME
    if not archive_path.exists():
        return report

    report["archive"] = str(archive_path)
    report["detected"] = True

    targets = _effective_container_storage_targets(env_report, system_config_report)
    chat_target = _container_path_to_host(root, targets["chat_storage_dir"])
    execution_target = _container_path_to_host(root, targets["execution_storage_dir"])

    state_dir = root / "deploy" / "state"
    state_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="legacy-storage-import-", dir=str(state_dir)) as temp_dir:
        extracted_root = Path(temp_dir)
        _extract_archive(archive_path, extracted_root)
        source_root = extracted_root / "legacy-backend-data"
        report["chat"] = _merge_legacy_tree(
            source_root / "chats",
            chat_target,
            dry_run=dry_run,
        )
        report["execution"] = _merge_legacy_tree(
            source_root / "executions",
            execution_target,
            dry_run=dry_run,
        )
    report["changed"] = bool(report["chat"]["changed"] or report["execution"]["changed"])
    return report


def migrate(
    root: Path,
    *,
    dry_run: bool,
    legacy_backup_dir: Path | None = None,
) -> dict[str, Any]:
    root = root.resolve()
    env_path = root / "backend" / ".env"
    system_config_path = root / ".onto-runtime" / "config" / "system_config.json"

    report: dict[str, Any] = {
        "schema_version": 1,
        "mode": "dry-run" if dry_run else "apply",
        "status": "ok",
        "changed": False,
        "canonical_project_root": CANONICAL_PROJECT_ROOT,
        "canonical_storage": {
            "chat_storage_dir": CANONICAL_CHAT_STORAGE_DIR,
            "execution_storage_dir": CANONICAL_EXECUTION_STORAGE_DIR,
        },
    }
    report["backend_env"] = _normalize_env_file(env_path, dry_run=dry_run)
    report["system_config"] = _normalize_system_config(system_config_path, dry_run=dry_run)
    targets = _effective_container_storage_targets(report["backend_env"], report["system_config"])
    runtime_chat_dir = _container_path_to_host(root, targets["chat_storage_dir"])
    runtime_execution_dir = _container_path_to_host(root, targets["execution_storage_dir"])
    report["host_paths"] = {
        "chat_storage_dir": str(runtime_chat_dir),
        "execution_storage_dir": str(runtime_execution_dir),
    }
    report["legacy_container_import"] = _import_legacy_container_storage(
        root,
        dry_run=dry_run,
        legacy_backup_dir=legacy_backup_dir,
        env_report=report["backend_env"],
        system_config_report=report["system_config"],
    )
    report["changed"] = bool(
        report["backend_env"]["changed"]
        or report["system_config"]["changed"]
        or report["legacy_container_import"]["changed"]
    )
    if not dry_run:
        runtime_chat_dir.mkdir(parents=True, exist_ok=True)
        runtime_execution_dir.mkdir(parents=True, exist_ok=True)
    report["host_paths"]["chat_storage_exists"] = runtime_chat_dir.exists() or not dry_run
    report["host_paths"]["execution_storage_exists"] = runtime_execution_dir.exists() or not dry_run
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    _atomic_write_text(path, json.dumps(report, ensure_ascii=False, indent=2) + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--legacy-backup-dir", type=Path)
    args = parser.parse_args(argv)
    try:
        report = migrate(
            args.root,
            dry_run=args.dry_run,
            legacy_backup_dir=args.legacy_backup_dir,
        )
        _write_report(args.report, report)
        return 0
    except (OSError, ValueError, json.JSONDecodeError, tarfile.TarError) as exc:
        report = {
            "schema_version": 1,
            "mode": "dry-run" if args.dry_run else "apply",
            "status": "failed",
            "error": str(exc),
        }
        _write_report(args.report, report)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
