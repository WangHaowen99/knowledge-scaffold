"""Remove the obsolete workspace observability mode field."""

from __future__ import annotations

import json
import os
import stat
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


MIGRATION_ID = "observability-workspace-mode-v2"
MARKER_RELATIVE_PATH = Path("config") / "migrations" / f"{MIGRATION_ID}.json"


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    del writes_stopped, from_version, to_version
    root = Path(runtime_root).expanduser().resolve()
    marker_path = root / MARKER_RELATIVE_PATH
    try:
        marker = _read_marker(marker_path)
    except Exception as exc:
        return 2, _report(
            marker_path=marker_path,
            dry_run=dry_run,
            ok=False,
            counts={},
            errors=["读取 workspace 观测配置失败（{}）".format(type(exc).__name__)],
            error_codes=["OBSERVABILITY_MODE_MIGRATION_READ_FAILED"],
        )

    if marker and marker.get("status") == "completed":
        return 0, _report(
            marker_path=marker_path,
            dry_run=dry_run,
            ok=True,
            counts={"workspace_configs_updated": 0},
            status="complete",
        )

    try:
        changes = _collect_changes(root)
    except Exception as exc:
        return 2, _report(
            marker_path=marker_path,
            dry_run=dry_run,
            ok=False,
            counts={},
            errors=["读取 workspace 观测配置失败（{}）".format(type(exc).__name__)],
            error_codes=["OBSERVABILITY_MODE_MIGRATION_READ_FAILED"],
        )

    counts = {"workspace_configs_found": len(changes), "workspace_configs_updated": 0}
    if dry_run:
        return 0, _report(
            marker_path=marker_path,
            dry_run=True,
            ok=True,
            counts=counts,
            status="dry-run-ready" if changes else "noop",
        )

    original_bytes: dict[Path, bytes] = {}
    written: list[Path] = []
    try:
        for path, source, target in changes:
            original_bytes[path] = source
            _write_bytes_atomic(path, target, mode=stat.S_IMODE(path.stat().st_mode))
            written.append(path)
        _write_json_atomic(
            marker_path,
            {
                "schema_version": 1,
                "migration_id": MIGRATION_ID,
                "status": "completed",
                "completed_at": _utc_now(),
            },
        )
    except Exception as exc:
        for path in reversed(written):
            try:
                _write_bytes_atomic(path, original_bytes[path], mode=stat.S_IMODE(path.stat().st_mode))
            except Exception:
                pass
        return 2, _report(
            marker_path=marker_path,
            dry_run=False,
            ok=False,
            counts=counts,
            errors=["删除 workspace 观测 mode 失败（{}）".format(type(exc).__name__)],
            error_codes=["OBSERVABILITY_MODE_MIGRATION_WRITE_FAILED"],
        )

    counts["workspace_configs_updated"] = len(changes)
    return 0, _report(
        marker_path=marker_path,
        dry_run=False,
        ok=True,
        counts=counts,
        status="completed" if changes else "noop",
    )


def _collect_changes(root: Path) -> list[tuple[Path, bytes, bytes]]:
    workspaces_root = root / "workspaces"
    if not workspaces_root.exists():
        return []
    if workspaces_root.is_symlink() or not workspaces_root.is_dir():
        raise OSError("workspaces 根目录无效")

    changes: list[tuple[Path, bytes, bytes]] = []
    for workspace_dir in sorted(workspaces_root.iterdir(), key=lambda item: item.name):
        if workspace_dir.is_symlink() or not workspace_dir.is_dir():
            continue
        path = workspace_dir / "observability_config.json"
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            raise OSError("workspace 观测配置必须是普通文件")
        source = path.read_bytes()
        document = json.loads(source.decode("utf-8"))
        if not isinstance(document, dict):
            raise ValueError("workspace 观测配置必须是对象")
        if "mode" not in document:
            continue
        if document.get("mode") not in {"local", "langfuse"}:
            raise ValueError("workspace 观测配置 mode 无效")
        document.pop("mode", None)
        target = (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        changes.append((path, source, target))
    return changes


def _read_marker(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise OSError("migration marker 必须是普通文件")
    marker = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(marker, dict) or marker.get("migration_id") != MIGRATION_ID:
        raise ValueError("migration marker 无效")
    if marker.get("status") != "completed":
        raise ValueError("migration marker 状态无效")
    return marker


def _write_bytes_atomic(path: Path, payload: bytes, *, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            descriptor = -1
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    _write_bytes_atomic(
        path,
        (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _report(
    *,
    marker_path: Path,
    dry_run: bool,
    ok: bool,
    counts: dict[str, int],
    status: str = "failed",
    errors: list[str] | None = None,
    error_codes: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": ok,
        "dry_run": dry_run,
        "status": status,
        "scope": {"type": "workspaces"},
        "counts": counts,
        "backup_paths": [],
        "failed_object_ids": [],
        "error_codes": error_codes or [],
        "errors": errors or [],
        "warnings": [],
        "marker_path": str(marker_path),
    }
