"""Migrate historical workspace-private model-profile output budgets."""

from __future__ import annotations

import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from migrations.system_config_v1_support import (
    MigrationProblem,
    _backup_base,
    _create_verified_backup,
    _discover_backups,
    _ensure_private_file,
    _fsync_directory,
    _load_marker,
    _marker_path,
    _object_id,
    _operation_problem,
    _problem,
    _read_regular_file_bytes,
    _write_bytes_atomic,
)


MIGRATION_ID = "workspace-model-profile-output-budget-v1"
BACKUP_SUFFIX = f"pre-{MIGRATION_ID}"
PROFILE_FILE_NAME = "llm_profiles.json"
LEGACY_DEFAULT_MAX_TOKENS = frozenset({4096, 8192})
CURRENT_DEFAULT_MAX_TOKENS = 32768


@dataclass
class WorkspaceFilePlan:
    workspace_id: str
    path: Path
    source_bytes: bytes
    target_bytes: bytes
    changed: bool
    counts: dict[str, int] = field(default_factory=dict)
    problems: list[MigrationProblem] = field(default_factory=list)


def _workspace_dirs(runtime_root: Path) -> list[Path]:
    # Reuse the workspace module's fail-closed path validation instead of
    # duplicating its symlink/junction and metadata checks here.
    from workspace.workspaces import _workspace_dirs_for_runtime_root

    return list(_workspace_dirs_for_runtime_root(runtime_root))


def _profile_plan(
    workspace_id: str,
    path: Path,
    source_bytes: bytes,
    document: dict[str, Any],
    runtime_root: Path,
) -> WorkspaceFilePlan:
    target = copy.deepcopy(document)
    counts = {
        "profiles_scanned": 0,
        "profiles_migrated": 0,
        "profiles_defaulted": 0,
        "profiles_below_current_default": 0,
    }
    problems: list[MigrationProblem] = []
    profiles = document.get("profiles")
    object_id = _object_id(path, runtime_root)
    if profiles is None:
        return WorkspaceFilePlan(
            workspace_id,
            path,
            source_bytes,
            source_bytes,
            False,
            counts,
            problems,
        )
    if not isinstance(profiles, list):
        problems.append(
            _problem(
                "WORKSPACE_MODEL_PROFILE_LIST_TYPE_INVALID",
                object_id,
                "profiles must be an array",
            )
        )
        return WorkspaceFilePlan(
            workspace_id,
            path,
            source_bytes,
            source_bytes,
            False,
            counts,
            problems,
        )

    target_profiles = target["profiles"]
    for index, profile in enumerate(profiles):
        profile_object_id = f"{object_id}:profiles[{index}]"
        if not isinstance(profile, dict):
            problems.append(
                _problem(
                    "WORKSPACE_MODEL_PROFILE_TYPE_INVALID",
                    profile_object_id,
                    "model profile must be an object",
                )
            )
            continue
        counts["profiles_scanned"] += 1
        if "max_tokens" not in profile:
            target_profiles[index]["max_tokens"] = CURRENT_DEFAULT_MAX_TOKENS
            counts["profiles_defaulted"] += 1
            continue
        value = profile.get("max_tokens")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            problems.append(
                _problem(
                    "WORKSPACE_MODEL_PROFILE_MAX_TOKENS_INVALID",
                    f"{profile_object_id}.max_tokens",
                    "max_tokens must be a positive integer",
                )
            )
        elif value in LEGACY_DEFAULT_MAX_TOKENS:
            target_profiles[index]["max_tokens"] = CURRENT_DEFAULT_MAX_TOKENS
            counts["profiles_migrated"] += 1
        elif value < CURRENT_DEFAULT_MAX_TOKENS:
            counts["profiles_below_current_default"] += 1

    target_bytes = (
        json.dumps(target, ensure_ascii=False, indent=2) + "\n"
    ).encode("utf-8")
    return WorkspaceFilePlan(
        workspace_id,
        path,
        source_bytes,
        target_bytes,
        target != document,
        counts,
        problems,
    )


def _load_workspace_plan(
    runtime_root: Path,
) -> tuple[list[WorkspaceFilePlan], list[MigrationProblem], int]:
    plans: list[WorkspaceFilePlan] = []
    problems: list[MigrationProblem] = []
    try:
        workspace_dirs = _workspace_dirs(runtime_root)
    except Exception as exc:
        return [], [
            _operation_problem(
                "WORKSPACE_MODEL_PROFILE_SCAN_FAILED",
                _object_id(runtime_root / "workspaces", runtime_root),
                "scan trusted workspace directories",
                exc,
            )
        ], 0

    for workspace_dir in workspace_dirs:
        workspace_id = workspace_dir.name
        path = workspace_dir / PROFILE_FILE_NAME
        object_id = _object_id(path, runtime_root)
        if not path.exists() and not path.is_symlink():
            continue
        if path.is_symlink() or not path.is_file():
            problems.append(
                _problem(
                    "WORKSPACE_MODEL_PROFILE_UNSAFE_FILE",
                    object_id,
                    "workspace profile file must be a regular file",
                )
            )
            continue
        try:
            source_bytes = _read_regular_file_bytes(path)
            document = json.loads(source_bytes.decode("utf-8"))
        except json.JSONDecodeError as exc:
            problems.append(
                _operation_problem(
                    "WORKSPACE_MODEL_PROFILE_INVALID_JSON",
                    object_id,
                    "parse workspace profile file",
                    exc,
                )
            )
            continue
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(
                _operation_problem(
                    "WORKSPACE_MODEL_PROFILE_READ_FAILED",
                    object_id,
                    "read workspace profile file",
                    exc,
                )
            )
            continue
        if not isinstance(document, dict):
            problems.append(
                _problem(
                    "WORKSPACE_MODEL_PROFILE_DOCUMENT_INVALID",
                    object_id,
                    "workspace profile file must contain a JSON object",
                )
            )
            continue
        plans.append(
            _profile_plan(workspace_id, path, source_bytes, document, runtime_root)
        )
    return plans, problems, len(workspace_dirs)


def _aggregate_counts(plans: list[WorkspaceFilePlan], workspace_count: int) -> dict[str, int]:
    keys = (
        "profiles_scanned",
        "profiles_migrated",
        "profiles_defaulted",
        "profiles_below_current_default",
    )
    counts = {key: 0 for key in keys}
    counts["workspaces_scanned"] = workspace_count
    counts["files_scanned"] = len(plans)
    counts["files_changed"] = sum(plan.changed for plan in plans)
    counts["files_written"] = 0
    for plan in plans:
        for key in keys:
            counts[key] += plan.counts.get(key, 0)
    return counts


def _recovery_backup_paths(
    runtime_root: Path,
    plans: list[WorkspaceFilePlan],
    marker: dict[str, Any] | None,
) -> list[Path]:
    """Collect marker and orphaned backups left by a partial prior apply."""
    paths = [
        Path(path)
        for path in (marker or {}).get("manual_recovery_backups", [])
    ]
    for plan in plans:
        for path in _discover_backups(plan.path, BACKUP_SUFFIX):
            if path not in paths:
                paths.append(path)
    return paths


def _render_report(
    runtime_root: Path,
    *,
    dry_run: bool,
    status: str,
    counts: dict[str, int],
    problems: list[MigrationProblem],
    backup_paths: list[Path],
    planned_backup_paths: list[Path],
    workspace_ids: list[str],
    warnings: list[str] | None = None,
    from_version: str,
    to_version: str,
) -> tuple[int, dict[str, Any]]:
    report = {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": not problems,
        "dry_run": dry_run,
        "phase": "dry-run" if dry_run else "apply",
        "status": status,
        "scope": {"type": "runtime", "workspace_ids": workspace_ids},
        "from_version": from_version,
        "to_version": to_version,
        "counts": counts,
        "backup_paths": [str(path) for path in backup_paths],
        "planned_backup_paths": [str(path) for path in planned_backup_paths],
        "error_codes": sorted({problem.code for problem in problems}),
        "failed_object_ids": sorted({problem.object_id for problem in problems}),
        "errors": [problem.as_dict() for problem in problems],
        "warnings": list(warnings or []),
        "runtime_root": str(runtime_root),
    }
    return (0 if report["ok"] else 2), report


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    """Preflight or apply workspace-private output-budget migration."""
    resolved_root = Path(runtime_root).expanduser().resolve()
    if not dry_run and not writes_stopped:
        return _render_report(
            resolved_root,
            dry_run=False,
            status="blocked",
            counts={},
            problems=[
                _problem(
                    "WRITES_NOT_STOPPED",
                    _object_id(resolved_root, resolved_root),
                    "migration requires stopped writers",
                )
            ],
            backup_paths=[],
            planned_backup_paths=[],
            workspace_ids=[],
            from_version=from_version,
            to_version=to_version,
        )

    marker, marker_problem = _load_marker(resolved_root, MIGRATION_ID)
    if marker_problem:
        return _render_report(
            resolved_root,
            dry_run=dry_run,
            status="blocked",
            counts={},
            problems=[marker_problem],
            backup_paths=[],
            planned_backup_paths=[],
            workspace_ids=[],
            from_version=from_version,
            to_version=to_version,
        )

    plans, problems, workspace_count = _load_workspace_plan(resolved_root)
    all_problems = [*problems, *(problem for plan in plans for problem in plan.problems)]
    counts = _aggregate_counts(plans, workspace_count)
    workspace_ids = sorted({plan.workspace_id for plan in plans})
    warnings: list[str] = []
    low_budget_profiles = counts["profiles_below_current_default"]
    if low_budget_profiles:
        warnings.append(
            "{} workspace model profile(s) use a custom max_tokens below {}"
            .format(low_budget_profiles, CURRENT_DEFAULT_MAX_TOKENS)
        )
    changed_plans = [plan for plan in plans if plan.changed and not plan.problems]
    recovery_backup_paths = _recovery_backup_paths(resolved_root, plans, marker)
    planned_backups = [
        _backup_base(plan.path, BACKUP_SUFFIX) for plan in changed_plans
    ]
    if all_problems:
        return _render_report(
            resolved_root,
            dry_run=dry_run,
            status="blocked",
            counts=counts,
            problems=all_problems,
            backup_paths=[],
            planned_backup_paths=planned_backups,
            workspace_ids=workspace_ids,
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )
    if dry_run:
        return _render_report(
            resolved_root,
            dry_run=True,
            status=(
                "dry-run-ready"
                if changed_plans
                else "already-applied"
                if marker
                else "noop"
            ),
            counts=counts,
            problems=[],
            backup_paths=[],
            planned_backup_paths=planned_backups,
            workspace_ids=workspace_ids,
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )
    if not changed_plans:
        if not marker and recovery_backup_paths:
            try:
                from migrations.system_config_v1_support import _write_marker

                _write_marker(
                    resolved_root,
                    MIGRATION_ID,
                    backup_paths=recovery_backup_paths,
                    counts=counts,
                )
            except Exception as exc:
                return _render_report(
                    resolved_root,
                    dry_run=False,
                    status="blocked",
                    counts=counts,
                    problems=[
                        _operation_problem(
                            "WORKSPACE_MODEL_PROFILE_MARKER_WRITE_FAILED",
                            str(_marker_path(resolved_root, MIGRATION_ID)),
                            "complete workspace profile migration marker",
                            exc,
                        )
                    ],
                    backup_paths=recovery_backup_paths,
                    planned_backup_paths=[],
                    workspace_ids=workspace_ids,
                    warnings=warnings,
                    from_version=from_version,
                    to_version=to_version,
                )
            return _render_report(
                resolved_root,
                dry_run=False,
                status="completed-after-retry",
                counts=counts,
                problems=[],
                backup_paths=recovery_backup_paths,
                planned_backup_paths=[],
                workspace_ids=workspace_ids,
                warnings=warnings,
                from_version=from_version,
                to_version=to_version,
            )
        return _render_report(
            resolved_root,
            dry_run=False,
            status="already-applied" if marker else "noop",
            counts=counts,
            problems=[],
            backup_paths=recovery_backup_paths,
            planned_backup_paths=[],
            workspace_ids=workspace_ids,
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )

    backup_paths: list[Path] = []
    apply_problems: list[MigrationProblem] = []
    for plan in changed_plans:
        object_id = _object_id(plan.path, resolved_root)
        try:
            backup_path, _ = _create_verified_backup(
                plan.path,
                plan.source_bytes,
                _backup_base(plan.path, BACKUP_SUFFIX),
            )
            backup_paths.append(backup_path)
            if _read_regular_file_bytes(plan.path) != plan.source_bytes:
                raise OSError("workspace profile changed during migration")
            _write_bytes_atomic(plan.path, plan.target_bytes)
            if _read_regular_file_bytes(plan.path) != plan.target_bytes:
                raise OSError("workspace profile verification failed")
            _ensure_private_file(plan.path)
            _fsync_directory(plan.path.parent)
            counts["files_written"] += 1
        except Exception as exc:
            apply_problems.append(
                _operation_problem(
                    "WORKSPACE_MODEL_PROFILE_WRITE_FAILED",
                    object_id,
                    "backup and atomically write workspace profile",
                    exc,
                )
            )
            break

    if apply_problems:
        return _render_report(
            resolved_root,
            dry_run=False,
            status="blocked-partial" if counts["files_written"] else "blocked",
            counts=counts,
            problems=apply_problems,
            backup_paths=backup_paths,
            planned_backup_paths=planned_backups,
            workspace_ids=workspace_ids,
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )

    merged_backup_paths = list(recovery_backup_paths)
    for backup_path in backup_paths:
        if backup_path not in merged_backup_paths:
            merged_backup_paths.append(backup_path)

    try:
        from migrations.system_config_v1_support import _write_marker

        _write_marker(
            resolved_root,
            MIGRATION_ID,
            backup_paths=merged_backup_paths,
            counts=counts,
        )
    except Exception as exc:
        return _render_report(
            resolved_root,
            dry_run=False,
            status="blocked",
            counts=counts,
            problems=[
                _operation_problem(
                    "WORKSPACE_MODEL_PROFILE_MARKER_WRITE_FAILED",
                    str(_marker_path(resolved_root, MIGRATION_ID)),
                    "write workspace profile migration marker",
                    exc,
                )
            ],
            backup_paths=merged_backup_paths,
            planned_backup_paths=planned_backups,
            workspace_ids=workspace_ids,
            from_version=from_version,
            to_version=to_version,
        )
    return _render_report(
        resolved_root,
        dry_run=False,
        status="applied",
        counts=counts,
        problems=[],
        backup_paths=merged_backup_paths,
        planned_backup_paths=planned_backups,
        workspace_ids=workspace_ids,
        from_version=from_version,
        to_version=to_version,
    )
