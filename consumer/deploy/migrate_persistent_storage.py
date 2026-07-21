#!/usr/bin/env python3
"""Normalize release storage paths to durable runtime-backed locations."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any


CANONICAL_PROJECT_ROOT = "/app/.onto-runtime"
CANONICAL_CHAT_STORAGE_DIR = "/app/.onto-runtime/data/chats"
CANONICAL_EXECUTION_STORAGE_DIR = "/app/.onto-runtime/data/executions"
_LEGACY_CHAT_VALUES = {"", "./data/chats", "/app/data/chats"}
_LEGACY_EXECUTION_VALUES = {"", "./data/executions", "/app/data/executions"}


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


def migrate(root: Path, *, dry_run: bool) -> dict[str, Any]:
    root = root.resolve()
    env_path = root / "backend" / ".env"
    system_config_path = root / ".onto-runtime" / "config" / "system_config.json"
    runtime_chat_dir = root / ".onto-runtime" / "data" / "chats"
    runtime_execution_dir = root / ".onto-runtime" / "data" / "executions"

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
        "backend_env": _normalize_env_file(env_path, dry_run=dry_run),
        "system_config": _normalize_system_config(system_config_path, dry_run=dry_run),
        "host_paths": {
            "chat_storage_dir": str(runtime_chat_dir),
            "execution_storage_dir": str(runtime_execution_dir),
        },
    }
    report["changed"] = bool(
        report["backend_env"]["changed"]
        or report["system_config"]["changed"]
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
    args = parser.parse_args(argv)
    try:
        report = migrate(args.root, dry_run=args.dry_run)
        _write_report(args.report, report)
        return 0
    except (OSError, ValueError, json.JSONDecodeError) as exc:
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
