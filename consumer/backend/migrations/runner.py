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
import logging
import os
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
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
logger = logging.getLogger(__name__)


class StartupMigrationError(RuntimeError):
    """Raised when a registered migration prevents safe application startup."""


MigrationExecutor = Callable[..., tuple[int, dict[str, Any]]]


class MigrationPhase(str, Enum):
    """Where a migration is safe to run."""

    STARTUP = "startup"
    DEPLOYMENT = "deployment"
    EXTERNAL_ADMIN = "external-admin"


class MigrationFailurePolicy(str, Enum):
    """Whether a failed migration makes the current runtime unsafe."""

    BLOCKING = "blocking"
    NON_BLOCKING = "non-blocking"


class MigrationRunContext(str, Enum):
    """Caller context used to select registered migration phases."""

    STARTUP = "startup"
    DEPLOYMENT = "deployment"
    EXTERNAL_ADMIN = "external-admin"


@dataclass(frozen=True)
class RegisteredMigration:
    migration_id: str
    module: str
    phase: MigrationPhase
    failure_policy: MigrationFailurePolicy


# Concrete migration modules are registered only here. Remove an entry together
# with its module when the supported compatibility window closes.
REGISTERED_MIGRATIONS = (
    RegisteredMigration(
        migration_id="ontology-data-sources-direct-v2",
        module="migrations.ontology_data_sources_direct_v2",
        phase=MigrationPhase.STARTUP,
        failure_policy=MigrationFailurePolicy.BLOCKING,
    ),
    RegisteredMigration(
        migration_id="observability-workspace-config-v1",
        module="migrations.observability_workspace_config_v1",
        phase=MigrationPhase.STARTUP,
        # Observability can safely degrade to the disabled workspace default.
        # Keeping the global source document makes a later startup retryable.
        failure_policy=MigrationFailurePolicy.NON_BLOCKING,
    ),
    RegisteredMigration(
        migration_id="system-config-model-profiles-v1",
        module="migrations.system_config_model_profiles_v1",
        phase=MigrationPhase.STARTUP,
        # Strict runtime reads reject task_llm, legacy default ids, dangling
        # refs, and conflicting profiles; startup is unsafe until conversion.
        failure_policy=MigrationFailurePolicy.BLOCKING,
    ),
    RegisteredMigration(
        migration_id="system-config-secret-encryption-v1",
        module="migrations.system_config_secret_encryption_v1",
        phase=MigrationPhase.STARTUP,
        # Runtime reads reject plaintext or unverifiable ciphertext, so a key
        # or encryption failure must stop the new runtime.
        failure_policy=MigrationFailurePolicy.BLOCKING,
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


def _report_error_codes(report: dict[str, Any]) -> list[str]:
    values = report.get("error_codes") if isinstance(report, dict) else None
    codes = [str(value).strip() for value in (values or []) if str(value).strip()]
    error = report.get("error") if isinstance(report, dict) else None
    if isinstance(error, dict):
        code = str(error.get("code") or "").strip()
        if code and code not in codes:
            codes.append(code)
    return codes


def _failure_message(migration_id: str, report: dict[str, Any]) -> str:
    codes = _report_error_codes(report)
    return "{}：{}".format(
        migration_id,
        ",".join(codes) if codes else "MIGRATION_FAILED",
    )


def _selected_migrations(
    run_context: MigrationRunContext,
) -> tuple[RegisteredMigration, ...]:
    if run_context == MigrationRunContext.STARTUP:
        phases = {MigrationPhase.STARTUP}
    elif run_context == MigrationRunContext.DEPLOYMENT:
        phases = {MigrationPhase.STARTUP, MigrationPhase.DEPLOYMENT}
    else:
        phases = {MigrationPhase.EXTERNAL_ADMIN}
    return tuple(migration for migration in REGISTERED_MIGRATIONS if migration.phase in phases)


def _normalize_migration_report(
    migration: RegisteredMigration,
    report: dict[str, Any],
    *,
    code: int,
) -> dict[str, Any]:
    reported_id = str(report.get("migration_id") or "").strip()
    if reported_id != migration.migration_id:
        raise RuntimeError(
            "迁移报告 ID 不匹配：registry={} report={}".format(
                migration.migration_id,
                reported_id or "<missing>",
            )
        )
    failed = code != 0 or not report.get("ok", False)
    report.setdefault("scope", {"type": "runtime"})
    report.setdefault("counts", {})
    report.setdefault("backup_paths", [])
    report.setdefault("error_codes", ["MIGRATION_FAILED"] if failed else [])
    report.setdefault("failed_object_ids", [])
    report.setdefault("errors", [])
    report.setdefault("warnings", [])
    report["execution_phase"] = migration.phase.value
    report["failure_policy"] = migration.failure_policy.value
    return report


def _execute_registered_migrations(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str,
    to_version: str,
    run_context: MigrationRunContext,
) -> tuple[list[dict[str, Any]], list[str], list[str]]:
    reports: list[dict[str, Any]] = []
    errors: list[str] = []
    warnings: list[str] = []
    for migration in _selected_migrations(run_context):
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
            report = _normalize_migration_report(migration, report, code=code)
        except Exception as exc:
            report = {
                "schema_version": 1,
                "migration_id": migration.migration_id,
                "ok": False,
                "dry_run": dry_run,
                "errors": ["迁移执行异常（{}）".format(type(exc).__name__)],
                "error_codes": ["MIGRATION_EXECUTION_EXCEPTION"],
                "failed_object_ids": [migration.migration_id],
                "scope": {"type": "runtime"},
                "counts": {},
                "backup_paths": [],
                "warnings": [],
            }
            code = 2
            report = _normalize_migration_report(migration, report, code=code)
        reports.append(report)
        report_warnings = [
            str(item).strip()
            for item in (report.get("warnings") or [])
            if str(item).strip()
        ]
        warnings.extend(
            "{}：{}".format(migration.migration_id, item)
            for item in report_warnings
        )
        if code == 0 and report.get("ok", False):
            continue

        failure = _failure_message(migration.migration_id, report)
        if migration.failure_policy == MigrationFailurePolicy.NON_BLOCKING:
            warnings.append(failure)
            continue
        errors.append(failure)
        break
    return reports, errors, warnings


def execute_registered_migrations(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
    run_context: MigrationRunContext | str = MigrationRunContext.DEPLOYMENT,
) -> tuple[int, dict[str, Any]]:
    """Run the migration registry for deployment or another controlled caller."""
    resolved_root = _resolve_runtime_root(runtime_root)
    try:
        resolved_context = MigrationRunContext(run_context)
    except ValueError:
        report = {
            "schema_version": 1,
            "ok": False,
            "dry_run": dry_run,
            "runtime_root": str(resolved_root),
            "run_context": str(run_context),
            "from_version": from_version,
            "to_version": to_version,
            "migrations": [],
            "errors": ["迁移运行上下文无效"],
            "warnings": [],
            "error_codes": ["INVALID_RUN_CONTEXT"],
            "failed_object_ids": [],
            "error": {"code": "INVALID_RUN_CONTEXT"},
        }
        return 2, report
    if not dry_run and not writes_stopped:
        report = {
            "schema_version": 1,
            "ok": False,
            "dry_run": False,
            "runtime_root": str(resolved_root),
            "run_context": resolved_context.value,
            "from_version": from_version,
            "to_version": to_version,
            "migrations": [],
            "errors": ["正式迁移前必须确认旧版本已停止写入"],
            "warnings": [],
            "error_codes": ["WRITES_NOT_STOPPED"],
            "failed_object_ids": [],
            "error": {"code": "WRITES_NOT_STOPPED"},
        }
        return 2, report

    def run() -> tuple[list[dict[str, Any]], list[str], list[str]]:
        return _execute_registered_migrations(
            resolved_root,
            dry_run=dry_run,
            writes_stopped=writes_stopped,
            from_version=from_version,
            to_version=to_version,
            run_context=resolved_context,
        )

    try:
        if dry_run:
            reports, errors, warnings = run()
        else:
            with _migration_lock(resolved_root):
                reports, errors, warnings = run()
    except Exception as exc:
        reports = []
        errors = ["无法获取或执行迁移锁（{}）".format(type(exc).__name__)]
        warnings = []

    error_codes = sorted(
        {
            code
            for migration_report in reports
            for code in _report_error_codes(migration_report)
            if code
        }
    )
    failed_object_ids = sorted(
        {
            str(object_id)
            for migration_report in reports
            for object_id in (migration_report.get("failed_object_ids") or [])
            if str(object_id).strip()
        }
    )
    if errors and not error_codes:
        error_codes = ["MIGRATION_LOCK_FAILED"]

    report = {
        "schema_version": 1,
        "ok": not errors,
        "dry_run": dry_run,
        "runtime_root": str(resolved_root),
        "run_context": resolved_context.value,
        "from_version": from_version,
        "to_version": to_version,
        "migrations": reports,
        "errors": errors,
        "warnings": warnings,
        "error_codes": error_codes,
        "failed_object_ids": failed_object_ids,
    }
    return (0 if report["ok"] else 2), report


def run_startup_migrations(runtime_root: Optional[Path] = None) -> dict[str, Any]:
    """Apply registered migrations before application services are constructed."""
    code, report = execute_registered_migrations(
        _resolve_runtime_root(runtime_root),
        dry_run=False,
        writes_stopped=True,
        run_context=MigrationRunContext.STARTUP,
    )
    if code != 0 or not report.get("ok", False):
        detail = "；".join(str(item) for item in report.get("errors", []) if str(item).strip())
        raise StartupMigrationError(
            "启动迁移失败{}".format("：{}".format(detail) if detail else "")
        )
    for warning in report.get("warnings", []):
        logger.warning("Non-blocking startup migration warning: %s", warning)
    return report


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_report_atomic(path: Path, report: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name("{}.{}.tmp".format(path.name, uuid.uuid4().hex))
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        stream = os.fdopen(descriptor, "w", encoding="utf-8")
        descriptor = None
        with stream:
            stream.write(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
        _fsync_directory(path.parent)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        temporary.unlink(missing_ok=True)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runtime-root", type=Path, required=True)
    parser.add_argument("--report", type=Path)
    parser.add_argument("--from-version", default="")
    parser.add_argument("--to-version", default="")
    parser.add_argument(
        "--run-context",
        choices=[context.value for context in MigrationRunContext],
        default=MigrationRunContext.DEPLOYMENT.value,
        help="generic registry selection; deployment excludes external-admin migrations",
    )
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
        run_context=args.run_context,
    )
    if args.report:
        _write_report_atomic(args.report, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
