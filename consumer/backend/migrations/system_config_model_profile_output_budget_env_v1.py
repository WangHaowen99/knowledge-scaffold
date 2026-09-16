# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Pin historical LLM output-budget environment defaults for one process."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from migrations.system_config_v1_support import MigrationProblem, _problem


MIGRATION_ID = "system-config-model-profile-output-budget-env-v1"
LEGACY_DEFAULT_MAX_TOKENS = frozenset({4096, 8192})
CURRENT_DEFAULT_MAX_TOKENS = 32768
ENV_NAME = "LLM_MAX_TOKENS"


def _normalize_environment_value(raw_value: str | None) -> str:
    value = str(raw_value or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1].strip()
    return value


def _render_report(
    runtime_root: Path,
    *,
    dry_run: bool,
    status: str,
    counts: dict[str, int],
    problems: list[MigrationProblem],
    warnings: list[str],
    from_version: str,
    to_version: str,
    previous_value: int | None = None,
) -> tuple[int, dict[str, Any]]:
    report = {
        "schema_version": 1,
        "migration_id": MIGRATION_ID,
        "ok": not problems,
        "dry_run": dry_run,
        "phase": "dry-run" if dry_run else "apply",
        "status": status,
        "scope": {"type": "process-environment", "variables": [ENV_NAME]},
        "from_version": from_version,
        "to_version": to_version,
        "counts": counts,
        "previous_value": previous_value,
        "current_value": CURRENT_DEFAULT_MAX_TOKENS
        if counts.get("variables_pinned", 0)
        else None,
        "error_codes": sorted({problem.code for problem in problems}),
        "failed_object_ids": sorted({problem.object_id for problem in problems}),
        "errors": [problem.as_dict() for problem in problems],
        "warnings": list(warnings),
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
    """Pin a historical process environment default before config resolution."""

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
                    ENV_NAME,
                    "migration requires stopped writers",
                )
            ],
            warnings=[],
            from_version=from_version,
            to_version=to_version,
        )

    raw_value = os.environ.get(ENV_NAME)
    normalized = _normalize_environment_value(raw_value)
    counts = {
        "variables_scanned": int(bool(normalized)),
        "variables_pinned": 0,
        "custom_below_current_default": 0,
        "invalid_values": 0,
    }
    warnings: list[str] = []
    if not normalized:
        return _render_report(
            resolved_root,
            dry_run=dry_run,
            status="noop",
            counts=counts,
            problems=[],
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )

    try:
        numeric_value = int(normalized)
    except (TypeError, ValueError):
        counts["invalid_values"] = 1
        return _render_report(
            resolved_root,
            dry_run=dry_run,
            status="blocked",
            counts=counts,
            problems=[
                _problem(
                    "SYSTEM_CONFIG_ENV_MAX_TOKENS_INVALID",
                    ENV_NAME,
                    "LLM_MAX_TOKENS must be a positive integer",
                )
            ],
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )
    if numeric_value < 1:
        counts["invalid_values"] = 1
        return _render_report(
            resolved_root,
            dry_run=dry_run,
            status="blocked",
            counts=counts,
            problems=[
                _problem(
                    "SYSTEM_CONFIG_ENV_MAX_TOKENS_INVALID",
                    ENV_NAME,
                    "LLM_MAX_TOKENS must be a positive integer",
                )
            ],
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )
    if numeric_value not in LEGACY_DEFAULT_MAX_TOKENS:
        if numeric_value < CURRENT_DEFAULT_MAX_TOKENS:
            counts["custom_below_current_default"] = 1
            warnings.append(
                "{} uses a custom value below {}".format(
                    ENV_NAME,
                    CURRENT_DEFAULT_MAX_TOKENS,
                )
            )
        return _render_report(
            resolved_root,
            dry_run=dry_run,
            status="noop",
            counts=counts,
            problems=[],
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
        )

    counts["variables_pinned"] = 1
    if dry_run:
        return _render_report(
            resolved_root,
            dry_run=True,
            status="dry-run-ready",
            counts=counts,
            problems=[],
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
            previous_value=numeric_value,
        )

    os.environ[ENV_NAME] = str(CURRENT_DEFAULT_MAX_TOKENS)
    try:
        from config.system_config import invalidate_system_config_snapshot

        invalidate_system_config_snapshot(refresh_environment=True)
    except Exception as exc:
        os.environ[ENV_NAME] = str(raw_value) if raw_value is not None else ""
        return _render_report(
            resolved_root,
            dry_run=False,
            status="blocked",
            counts=counts,
            problems=[
                _problem(
                    "LLM_MAX_TOKENS_ENV_PIN_FAILED",
                    ENV_NAME,
                    "refresh system config environment snapshot failed: {}".format(
                        type(exc).__name__
                    ),
                )
            ],
            warnings=warnings,
            from_version=from_version,
            to_version=to_version,
            previous_value=numeric_value,
        )
    return _render_report(
        resolved_root,
        dry_run=False,
        status="applied",
        counts=counts,
        problems=[],
        warnings=warnings,
        from_version=from_version,
        to_version=to_version,
        previous_value=numeric_value,
    )
