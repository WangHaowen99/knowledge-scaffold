# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Provision private Consumer workspaces owned by one evaluation run."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from api.auth.user_deletion import identity_reference_guard
from api.workspace_acl_service import create_workspace_with_validated_owner
from workspace.workspaces import (
    WorkspaceAuthorizationError,
    WorkspaceNotFoundError,
    get_workspace,
)


def provision_evaluation_workspace(
    auth_backend: Any,
    *,
    owner: str,
    run_id: str,
) -> dict:
    """Create for this run, or recover its own space after a retry.

    Never select a default, personal or modeling workspace. The deterministic
    identifier is stable across interrupted requests and concurrent retries.
    """
    if not isinstance(run_id, str) or not re.fullmatch(r"[a-f0-9]{32}", run_id):
        raise ValueError("Invalid evaluation run")
    if not isinstance(owner, str) or not owner or owner != owner.strip():
        raise ValueError("Authenticated evaluation owner is required")
    suffix = hashlib.sha256((owner + ":" + run_id).encode()).hexdigest()[:32]
    workspace_id = "eval-" + suffix

    def existing_workspace():
        current = get_workspace(workspace_id)
        if current.get("owner") != owner or current.get("members"):
            raise WorkspaceAuthorizationError("Evaluation workspace ownership conflict")
        return {"created": False, "workspace": current}

    with identity_reference_guard((owner,)):
        try:
            return existing_workspace()
        except WorkspaceNotFoundError:
            pass
        try:
            created = create_workspace_with_validated_owner(
                auth_backend,
                name="Evaluation " + run_id[:8],
                description="Service-managed evaluation workspace",
                workspace_id=workspace_id,
                owner=owner,
                auto_set_default=False,
                set_as_default=False,
                enforce_workspace_limit=False,
            )
        except ValueError as exc:
            # Another process may have created the same run's space under the
            # storage lock. Only that exact private space is eligible for reuse.
            try:
                return existing_workspace()
            except WorkspaceNotFoundError:
                raise exc
        return {"created": True, "workspace": created}
