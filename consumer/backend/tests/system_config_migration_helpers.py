"""Test helpers for bringing legacy system_config fixtures to startup state."""

from __future__ import annotations

import os
from pathlib import Path


def apply_test_system_config_migrations(
    config_path: Path,
    monkeypatch,
    *,
    runtime_root: Path | None = None,
) -> None:
    """Apply the registered system-config migrations before runtime reads."""
    from migrations.system_config_model_profiles_v1 import execute_migration as execute_model_migration
    from migrations.system_config_model_profile_output_budget_env_v1 import (
        execute_migration as execute_output_budget_env_migration,
    )
    from migrations.system_config_model_profile_output_budget_v1 import (
        execute_migration as execute_output_budget_migration,
    )
    from migrations.system_config_secret_encryption_v1 import execute_migration as execute_secret_migration

    resolved_root = runtime_root or Path(os.environ.get("PROJECT_ROOT") or config_path.parents[1])
    monkeypatch.setenv("PROJECT_ROOT", str(resolved_root))
    monkeypatch.setenv("SYSTEM_CONFIG_PATH", str(config_path))
    for execute in (
        execute_output_budget_env_migration,
        execute_model_migration,
        execute_output_budget_migration,
        execute_secret_migration,
    ):
        code, report = execute(
            resolved_root,
            dry_run=False,
            writes_stopped=True,
        )
        if code != 0:
            raise AssertionError("test system config migration failed: {!r}".format(report))
