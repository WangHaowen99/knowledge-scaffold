# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Add stable MCP endpoint ids to persisted workspace metadata."""

from __future__ import annotations

import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from workspace.workspaces import ensure_all_workspace_mcp_endpoint_ids


MIGRATION_ID = "workspace-mcp-endpoint-id-v1"
_MARKER_RELATIVE_PATH = Path("config") / "migrations" / f"{MIGRATION_ID}.json"


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    del writes_stopped, from_version, to_version
    resolved_root = Path(runtime_root).expanduser().resolve()
    marker_path = resolved_root / _MARKER_RELATIVE_PATH
    marker_error = _validate_marker(marker_path)
    if marker_error:
        return 2, _report(
            ok=False,
            dry_run=dry_run,
            counts={},
            backup_paths=[],
            failed_object_ids=[],
            error_codes=["WORKSPACE_MCP_ENDPOINT_MARKER_INVALID"],
            errors=[marker_error],
            marker_path=marker_path,
        )

    result = ensure_all_workspace_mcp_endpoint_ids(
        resolved_root,
        dry_run=dry_run,
    )
    if not result.get("ok", False):
        return 2, _report(
            ok=False,
            dry_run=dry_run,
            counts=dict(result.get("counts") or {}),
            backup_paths=list(result.get("backup_paths") or []),
            failed_object_ids=list(result.get("failed_object_ids") or []),
            error_codes=list(result.get("error_codes") or ["WORKSPACE_MCP_ENDPOINT_MIGRATION_FAILED"]),
            errors=list(result.get("errors") or ["工作空间 MCP Endpoint ID 迁移失败"]),
            marker_path=marker_path,
        )

    if not dry_run and not marker_path.exists():
        try:
            _write_json_atomic(
                marker_path,
                {
                    "schema_version": 1,
                    "migration_id": MIGRATION_ID,
                    "completed_at": _utc_now(),
                },
            )
        except OSError as exc:
            return 2, _report(
                ok=False,
                dry_run=False,
                counts=dict(result.get("counts") or {}),
                backup_paths=list(result.get("backup_paths") or []),
                failed_object_ids=[],
                error_codes=["WORKSPACE_MCP_ENDPOINT_MARKER_WRITE_FAILED"],
                errors=[str(exc)],
                marker_path=marker_path,
            )

    return 0, _report(
        ok=True,
        dry_run=dry_run,
        counts=dict(result.get("counts") or {}),
        backup_paths=list(result.get("backup_paths") or []),
        failed_object_ids=[],
        error_codes=[],
        errors=[],
        marker_path=marker_path,
    )


def _validate_marker(path: Path) -> str:
    if not path.exists():
        return ""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        return "MCP Endpoint migration marker 无法读取: {}".format(exc)
    if not isinstance(payload, dict):
        return "MCP Endpoint migration marker 必须是对象"
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError):
        schema_version = 0
    if schema_version != 1:
        return "MCP Endpoint migration marker schema_version 无效"
    if str(payload.get("migration_id") or "") != MIGRATION_ID:
        return "MCP Endpoint migration marker migration_id 无效"
    return ""


def _report(
    *,
    ok: bool,
    dry_run: bool,
    counts: dict[str, Any],
    backup_paths: list[str],
    failed_object_ids: list[str],
    error_codes: list[str],
    errors: list[str],
    marker_path: Path,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": bool(ok),
        "dry_run": bool(dry_run),
        "scope": {"type": "workspaces"},
        "counts": counts,
        "backup_paths": backup_paths,
        "failed_object_ids": failed_object_ids,
        "error_codes": error_codes,
        "errors": errors,
        "warnings": [],
        "marker_path": str(marker_path),
    }


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    descriptor = os.open(
        temporary,
        os.O_WRONLY | os.O_CREAT | os.O_EXCL,
        0o600,
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as marker_file:
            descriptor = -1
            json.dump(payload, marker_file, ensure_ascii=False, indent=2)
            marker_file.flush()
            os.fsync(marker_file.fileno())
        os.replace(temporary, path)
        # Windows does not support opening a directory with os.open(); keep
        # the directory fsync as a POSIX-only durability enhancement, matching
        # system_config_v1_support._fsync_directory.
        if os.name != "nt":
            directory_descriptor = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(directory_descriptor)
            finally:
                os.close(directory_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
