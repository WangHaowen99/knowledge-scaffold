"""Forward-only migration of legacy system model fields to profile refs."""

from __future__ import annotations

from pathlib import Path

from migrations.system_config_v1_support import (
    execute_document_migration,
    plan_model_document,
)


MIGRATION_ID = "system-config-model-profiles-v1"
BACKUP_SUFFIX = f"pre-{MIGRATION_ID}"


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict]:
    """Preflight or apply the model/profile conversion as one durable step."""
    return execute_document_migration(
        runtime_root,
        migration_id=MIGRATION_ID,
        backup_suffix=BACKUP_SUFFIX,
        planner=plan_model_document,
        dry_run=dry_run,
        writes_stopped=writes_stopped,
        from_version=from_version,
        to_version=to_version,
    )
