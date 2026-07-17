#!/usr/bin/env python3
"""Shared registry and execution entry point for removable backend migrations.

Application startup and deployment both invoke this module. Concrete migration
code stays in independent modules and is referenced only by REGISTERED_MIGRATIONS,
so retiring a migration does not require changing either entry point.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Any, Callable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows development fallback
    fcntl = None  # type: ignore[assignment]

from config.system_config import ONTO_RUNTIME_ROOT


MIGRATION_LOCK_FILE = "registered-migrations.lock"
_PROCESS_LOCK = Lock()


class StartupMigrationError(RuntimeError):
    """Raised when a registered migration prevents safe application startup."""


MigrationExecutor = Callable[..., tuple[int, dict[str, Any]]]


@dataclass(frozen=True)
class RegisteredMigration:
    migration_id: str
    module: str


# Concrete migration modules are registered only here. Remove an entry together
# with its module when the supported compatibility window closes.
REGISTERED_MIGRATIONS = (
    RegisteredMigration(
        migration_id="ontology-data-sources-direct-v2",
        module="migrations.ontology_data_sources_direct_v2",
    ),
)


def _load_executor(migration: RegisteredMigration) -> MigrationExecutor:
    module = importlib.import_module(migration.module)
    declared_id = str(getattr(module, "MIGRATION_ID", "") or "").strip()
    if declared_id != migration.migration_id:
        raise RuntimeError(
            "迁移模块 ID 不匹配：registry={} module={}".format(
                migration.migration_id,
                declared_id or "<missing>",
            )
        )
    execute = getattr(module, "execute_migration", None)
    if not callable(execute):
        raise RuntimeError("迁移模块缺少 execute_migration: {}".format(migration.module))
    return execute


def _resolve_runtime_root(runtime_root: Optional[Path]) -> Path:
    configured_root = runtime_root or os.getenv("PROJECT_ROOT") or ONTO_RUNTIME_ROOT
    return Path(configured_root).expanduser().resolve()


@contextmanager
def _migration_lock(runtime_root: Path) -> Iterator[Path]:
    lock_path = runtime_root / "config" / MIGRATION_LOCK_FILE
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _PROCESS_LOCK:
        with lock_path.open("a+b") as lock_file:
            try:
                lock_path.chmod(0o600)
            except OSError:
                pass
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield lock_path
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _failure_message(migration_id: str, report: dict[str, Any]) -> str:
    errors = report.get("errors") if isinstance(report, dict) else None
    detail = "；".join(str(item) for item in (errors or []) if str(item).strip())
    return "{}{}".format(
        migration_id,
        "：{}".format(detail) if detail else "：迁移失败",
    )


def _execute_registered_migrations(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str,
    to_version: str,
) -> tuple[list[dict[str, Any]], list[str]]:
    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    for migration in REGISTERED_MIGRATIONS:
        try:
            execute = _load_executor(migration)
            code, report = execute(
                runtime_root,
                dry_run=dry_run,
                writes_stopped=writes_stopped,
                from_version=from_version,
                to_version=to_version,
            )
            if not isinstance(report, dict):
                raise TypeError("迁移报告必须是 object")
        except Exception as exc:
            report = {
                "schema_version": 1,
                "migration_id": migration.migration_id,
                "ok": False,
                "dry_run": dry_run,
                "errors": ["迁移执行异常：{}".format(str(exc))],
            }
            code = 2
        reports.append(report)
        if code != 0 or not report.get("ok", False):
            errors.append(_failure_message(migration.migration_id, report))
            break
    return reports, errors


def execute_registered_migrations(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    """Run the migration registry for deployment or another controlled caller."""
    resolved_root = _resolve_runtime_root(runtime_root)
    if not dry_run and not writes_stopped:
        report = {
            "schema_version": 1,
            "ok": False,
            "dry_run": False,
            "runtime_root": str(resolved_root),
            "from_version": from_version,
            "to_version": to_version,
            "migrations": [],
            "errors": ["正式迁移前必须确认旧版本已停止写入"],
            "error": {"code": "WRITES_NOT_STOPPED"},
        }
        return 2, report

    def run() -> tuple[list[dict[str, Any]], list[str]]:
        return _execute_registered_migrations(
            resolved_root,
            dry_run=dry_run,
            writes_stopped=writes_stopped,
            from_version=from_version,
            to_version=to_version,
        )

    try:
        if dry_run:
            reports, errors = run()
        else:
            with _migration_lock(resolved_root):
                reports, errors = run()
    except Exception as exc:
        reports = []
        errors = ["无法获取或执行迁移锁：{}".format(str(exc))]

    report = {
        "schema_version": 1,
        "ok": not errors,
        "dry_run": dry_run,
        "runtime_root": str(resolved_root),
        "from_version": from_version,
        "to_version": to_version,
        "migrations": reports,
        "errors": errors,
    }
    return (0 if report["ok"] else 2), report


def run_startup_migrations(runtime_root: Optional[Path] = None) -> dict[str, Any]:
    """Apply registered migrations before application services are constructed."""
    code, report = execute_registered_migrations(
        _resolve_runtime_root(runtime_root),
        dry_run=False,
        writes_stopped=True,
    )
    if code != 0 or not report.get("ok", False):
        detail = "；".join(str(item) for item in report.get("errors", []) if str(item).strip())
        raise StartupMigrationError(
            "启动迁移失败{}".format("：{}".format(detail) if detail else "")
        )
    return report


def _write_report_atomic(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    try:
        temporary.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.chmod(0o600)
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default="")
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--confirm-writes-stopped", action="store_true")
    return parser


def main(argv: Optional[list[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    code, report = execute_registered_migrations(
        args.runtime_root,
        dry_run=args.dry_run,
        writes_stopped=args.confirm_writes_stopped,
        from_version=args.from_version,
        to_version=args.to_version,
    )
    if args.report:
        _write_report_atomic(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
