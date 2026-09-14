# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import ast
from operator import getitem
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
ROUTES_ROOT = BACKEND_ROOT / "api" / "routes"


def _callable_name(node):
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def test_http_routes_do_not_write_workspace_acl_through_storage_functions():
    violations = []
    required_service_calls = {
        "create_workspace_with_validated_owner",
        "replace_workspace_members",
        "update_workspace_authorized_with_validated_owner",
        "update_workspace_owner_with_validation",
    }
    observed_service_calls = set()

    for path in sorted(ROUTES_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        storage_aliases = set()
        service_aliases = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for name in node.names:
                    if name.name == "workspace.workspaces":
                        storage_aliases.add(name.asname or name.name)
            if isinstance(node, ast.ImportFrom):
                for name in node.names:
                    if (
                        node.module == "workspace.workspaces" or name.name in {"create_workspace", "update_workspace"}
                    ) and name.name in {"create_workspace", "update_workspace"}:
                        storage_aliases.add(name.asname or name.name)
                    if node.module == "api.workspace_acl_service" and name.name in required_service_calls:
                        service_aliases[name.asname or name.name] = name.name

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function_name = _callable_name(node.func)
            callback_name = None
            if function_name == "run_in_runtime_pool" and len(node.args) >= 3:
                callback_name = _callable_name(node.args[2])
            invoked_names = {name for name in (function_name, callback_name) if name}
            observed_service_calls.update(
                getitem(service_aliases, name) for name in invoked_names if name in service_aliases
            )
            unsafe_storage = invoked_names & ({"create_workspace", "update_workspace"} | storage_aliases)
            if unsafe_storage:
                violations.append(
                    f"{path.relative_to(BACKEND_ROOT)}:{node.lineno} calls " f"{sorted(unsafe_storage)[0]}"
                )
            if "update_workspace_authorized" in invoked_names and any(
                keyword.arg is None or keyword.arg in {"owner", "members"} for keyword in node.keywords
            ):
                violations.append(
                    f"{path.relative_to(BACKEND_ROOT)}:{node.lineno} writes ACL via update_workspace_authorized"
                )

    assert violations == []
    assert required_service_calls <= observed_service_calls
