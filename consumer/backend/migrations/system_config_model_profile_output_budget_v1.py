# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Migrate historical model-profile output budgets to the current default."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

from migrations.system_config_v1_support import (
    DocumentPlan,
    MigrationProblem,
    execute_document_migration,
)


MIGRATION_ID = "system-config-model-profile-output-budget-v1"
BACKUP_SUFFIX = f"pre-{MIGRATION_ID}"
PROFILES_SECTION = "llm_profiles"
PROFILES_FIELD = "profiles"
LEGACY_DEFAULT_MAX_TOKENS = frozenset({4096, 8192})
CURRENT_DEFAULT_MAX_TOKENS = 32768
LEGACY_TASK_PROFILE_ID = "legacy-task-llm"


def _problem(code: str, object_id: str, detail: str) -> MigrationProblem:
    return MigrationProblem(code=code, object_id=object_id, detail=detail)


def plan_model_profile_output_budget(
    document: dict[str, Any],
    _runtime_root: Path,
    _preview_prerequisites: bool = False,
    object_id: str = "config/system_config.json",
) -> DocumentPlan:
    """Build a pure plan for upgrading historical explicit profile budgets."""
    target = copy.deepcopy(document)
    counts = {
        "profiles_scanned": 0,
        "profiles_migrated": 0,
        "profiles_defaulted": 0,
        "profiles_below_current_default": 0,
        "legacy_task_profiles_defaulted": 0,
        "legacy_task_profiles_below_current_default": 0,
        "legacy_llm_migrated": 0,
        "legacy_llm_below_current_default": 0,
    }
    problems: list[MigrationProblem] = []

    # Some pre-profile installations still keep the active model under the
    # top-level ``llm`` section. Migrate that source as well so it cannot
    # resolve back to the historical budget when no profile list exists.
    legacy_llm = document.get("llm")
    if legacy_llm is not None:
        if not isinstance(legacy_llm, dict):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_LEGACY_LLM_SECTION_TYPE_INVALID",
                    object_id,
                    "llm must be an object",
                )
            )
        else:
            if "max_tokens" in legacy_llm:
                legacy_value = legacy_llm.get("max_tokens")
                if (
                    isinstance(legacy_value, bool)
                    or not isinstance(legacy_value, int)
                    or legacy_value < 1
                ):
                    problems.append(
                        _problem(
                            "SYSTEM_CONFIG_LEGACY_LLM_MAX_TOKENS_INVALID",
                            f"{object_id}:llm.max_tokens",
                            "max_tokens must be a positive integer",
                        )
                    )
                elif legacy_value in LEGACY_DEFAULT_MAX_TOKENS:
                    target["llm"]["max_tokens"] = CURRENT_DEFAULT_MAX_TOKENS
                    counts["legacy_llm_migrated"] += 1
                elif legacy_value < CURRENT_DEFAULT_MAX_TOKENS:
                    counts["legacy_llm_below_current_default"] += 1
            # Keep a missing top-level value missing.  The configuration
            # contract gives environment variables precedence over persisted
            # fields, so materializing 32768 here would overwrite a legitimate
            # custom LLM_MAX_TOKENS value.  The startup runner pins only the
            # two known historical environment defaults before resolution.

    section = document.get(PROFILES_SECTION)
    if section is None:
        return DocumentPlan(target, target != document, counts, problems)
    if not isinstance(section, dict):
        return DocumentPlan(
            target,
            False,
            counts,
            [_problem(
                "SYSTEM_CONFIG_MODEL_PROFILE_SECTION_TYPE_INVALID",
                object_id,
                "llm_profiles must be an object",
            )],
        )

    profiles = section.get(PROFILES_FIELD)
    if profiles is None:
        return DocumentPlan(target, target != document, counts, problems)
    if not isinstance(profiles, list):
        return DocumentPlan(
            target,
            False,
            counts,
            [_problem(
                "SYSTEM_CONFIG_MODEL_PROFILE_LIST_TYPE_INVALID",
                object_id,
                "llm_profiles.profiles must be an array",
            )],
        )

    target_profiles = target[PROFILES_SECTION][PROFILES_FIELD]
    for index, profile in enumerate(profiles):
        if not isinstance(profile, dict):
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_MODEL_PROFILE_TYPE_INVALID",
                    f"{object_id}:profiles[{index}]",
                    "model profile must be an object",
                )
            )
            continue
        counts["profiles_scanned"] += 1
        # The structural migration deliberately preserves the historical
        # task profile budget for compatibility. It is not a global main
        # profile created with the old platform default. Still validate an
        # explicit value so a malformed private file cannot bypass startup
        # checks, and materialize the compatibility default when omitted.
        if profile.get("id") == LEGACY_TASK_PROFILE_ID:
            if "max_tokens" not in profile:
                target_profiles[index]["max_tokens"] = 4096
                counts["legacy_task_profiles_defaulted"] += 1
                continue
            value = profile.get("max_tokens")
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                problems.append(
                    _problem(
                        "SYSTEM_CONFIG_MODEL_PROFILE_MAX_TOKENS_INVALID",
                        f"{object_id}:profiles[{index}].max_tokens",
                        "max_tokens must be a positive integer",
                    )
                )
            elif value < CURRENT_DEFAULT_MAX_TOKENS:
                counts["legacy_task_profiles_below_current_default"] += 1
            continue
        if "max_tokens" not in profile:
            target_profiles[index]["max_tokens"] = CURRENT_DEFAULT_MAX_TOKENS
            counts["profiles_defaulted"] += 1
            continue
        value = profile.get("max_tokens")
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            problems.append(
                _problem(
                    "SYSTEM_CONFIG_MODEL_PROFILE_MAX_TOKENS_INVALID",
                    f"{object_id}:profiles[{index}].max_tokens",
                    "max_tokens must be a positive integer",
                )
            )
            continue
        if value in LEGACY_DEFAULT_MAX_TOKENS:
            target_profiles[index]["max_tokens"] = CURRENT_DEFAULT_MAX_TOKENS
            counts["profiles_migrated"] += 1
        elif value < CURRENT_DEFAULT_MAX_TOKENS:
            # Preserve an intentional custom budget, but expose it in the
            # migration report so operators can review it before deployment.
            counts["profiles_below_current_default"] += 1

    changed = target != document
    return DocumentPlan(target, changed, counts, problems)


def execute_migration(
    runtime_root: Path,
    *,
    dry_run: bool,
    writes_stopped: bool,
    from_version: str = "",
    to_version: str = "",
) -> tuple[int, dict[str, Any]]:
    """Preflight or apply the legacy output-budget migration."""
    code, report = execute_document_migration(
        runtime_root,
        migration_id=MIGRATION_ID,
        backup_suffix=BACKUP_SUFFIX,
        planner=plan_model_profile_output_budget,
        dry_run=dry_run,
        writes_stopped=writes_stopped,
        from_version=from_version,
        to_version=to_version,
    )
    counts = report.get("counts") if isinstance(report, dict) else {}
    low_budget_profiles = int((counts or {}).get("profiles_below_current_default", 0) or 0)
    low_budget_profiles += int(
        (counts or {}).get("legacy_llm_below_current_default", 0) or 0
    )
    if low_budget_profiles:
        report.setdefault("warnings", []).append(
            "{} model profile(s) use a custom max_tokens below {}"
            .format(low_budget_profiles, CURRENT_DEFAULT_MAX_TOKENS)
        )
    return code, report
