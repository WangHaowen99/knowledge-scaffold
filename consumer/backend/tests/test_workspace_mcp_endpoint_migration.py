from __future__ import annotations

import json

import pytest
from migrations.workspace_mcp_endpoint_id_v1 import MIGRATION_ID, execute_migration
from workspace.workspaces import (
    WorkspaceMCPEndpointError,
    create_workspace,
    ensure_all_workspace_mcp_endpoint_ids,
    generate_mcp_endpoint_id,
    get_workspace_mcp_endpoint_id,
    update_workspace,
    validate_mcp_endpoint_id,
)


def _write_workspace(runtime_root, workspace_id, *, endpoint_id=None):
    workspace_dir = runtime_root / "workspaces" / workspace_id
    workspace_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "id": workspace_id,
        "name": workspace_id,
        "description": "",
        "created_at": "2026-07-28T00:00:00+00:00",
        "updated_at": "2026-07-28T00:00:00+00:00",
        "owner": None,
        "members": [],
    }
    if endpoint_id is not None:
        payload["mcp_endpoint_id"] = endpoint_id
    metadata_path = workspace_dir / "workspace.json"
    metadata_path.write_text(json.dumps(payload), encoding="utf-8")
    return metadata_path


def test_mcp_endpoint_id_format_and_collision_retry(monkeypatch):
    values = iter(("a" * 16, "b" * 16))
    monkeypatch.setattr(
        "workspace.workspaces.secrets.token_hex",
        lambda _size: next(values),
    )

    endpoint_id = generate_mcp_endpoint_id({"a" * 16})

    assert endpoint_id == "b" * 16
    assert validate_mcp_endpoint_id(endpoint_id) == endpoint_id
    with pytest.raises(ValueError):
        validate_mcp_endpoint_id("ABC")


def test_workspace_mcp_endpoint_migration_is_persistent_and_idempotent(
    tmp_path,
    monkeypatch,
):
    runtime_root = tmp_path / ".onto-runtime"
    first_path = _write_workspace(runtime_root, "workspace-a")
    second_path = _write_workspace(runtime_root, "workspace-b", endpoint_id="b" * 16)
    legacy_bytes = first_path.read_bytes()
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    monkeypatch.setattr(
        "workspace.workspaces.secrets.token_hex",
        lambda _size: "a" * 16,
    )

    code, report = execute_migration(
        runtime_root,
        dry_run=False,
        writes_stopped=True,
    )

    assert code == 0
    assert report["migration_id"] == MIGRATION_ID
    assert report["counts"]["created"] == 1
    assert get_workspace_mcp_endpoint_id("workspace-a") == "a" * 16
    assert get_workspace_mcp_endpoint_id("workspace-b") == "b" * 16
    assert (
        first_path.with_name(
            "workspace.json.pre-workspace-mcp-endpoint-id-v1.bak",
        ).read_bytes()
        == legacy_bytes
    )
    first_bytes = first_path.read_bytes()
    second_bytes = second_path.read_bytes()

    code, repeated = execute_migration(
        runtime_root,
        dry_run=False,
        writes_stopped=True,
    )

    assert code == 0
    assert repeated["counts"]["created"] == 0
    assert first_path.read_bytes() == first_bytes
    assert second_path.read_bytes() == second_bytes
    assert (runtime_root / "workspace-mcp-endpoints.lock").is_file()
    assert (runtime_root / "config" / "migrations" / "workspace-mcp-endpoint-id-v1.json").is_file()


def test_workspace_mcp_endpoint_dry_run_does_not_modify_metadata(tmp_path):
    runtime_root = tmp_path / ".onto-runtime"
    metadata_path = _write_workspace(runtime_root, "workspace-a")
    original = metadata_path.read_bytes()

    code, report = execute_migration(
        runtime_root,
        dry_run=True,
        writes_stopped=False,
    )

    assert code == 0
    assert report["counts"]["created"] == 1
    assert metadata_path.read_bytes() == original
    assert not (runtime_root / "workspace-mcp-endpoints.lock").exists()
    assert not (runtime_root / "config" / "migrations" / "workspace-mcp-endpoint-id-v1.json").exists()


def test_workspace_mcp_endpoint_migration_rejects_duplicate_ids(tmp_path):
    runtime_root = tmp_path / ".onto-runtime"
    duplicate = "d" * 16
    _write_workspace(runtime_root, "workspace-a", endpoint_id=duplicate)
    _write_workspace(runtime_root, "workspace-b", endpoint_id=duplicate)

    result = ensure_all_workspace_mcp_endpoint_ids(runtime_root)

    assert result["ok"] is False
    assert result["error_codes"] == ["WORKSPACE_MCP_ENDPOINT_DUPLICATE"]
    assert result["failed_object_ids"] == ["workspace-a", "workspace-b"]

    with pytest.raises(WorkspaceMCPEndpointError) as exc_info:
        get_workspace_mcp_endpoint_id("workspace-a")
    assert exc_info.value.code == "WORKSPACE_MCP_ENDPOINT_DUPLICATE"


def test_new_workspaces_get_unique_stable_endpoint_ids(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    values = iter(("a" * 16, "b" * 16))
    monkeypatch.setattr(
        "workspace.workspaces.secrets.token_hex",
        lambda _size: next(values),
    )

    create_workspace("Alpha", workspace_id="alpha", auto_set_default=False)
    create_workspace("Beta", workspace_id="beta", auto_set_default=False)
    alpha_endpoint_id = get_workspace_mcp_endpoint_id("alpha")
    beta_endpoint_id = get_workspace_mcp_endpoint_id("beta")

    update_workspace("alpha", name="Alpha Renamed", description="updated")

    assert alpha_endpoint_id == "a" * 16
    assert beta_endpoint_id == "b" * 16
    assert alpha_endpoint_id != beta_endpoint_id
    assert get_workspace_mcp_endpoint_id("alpha") == alpha_endpoint_id


def test_new_workspace_creation_rejects_unmigrated_metadata(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    _write_workspace(runtime_root, "legacy")

    with pytest.raises(WorkspaceMCPEndpointError) as exc_info:
        create_workspace("New", workspace_id="new", auto_set_default=False)

    assert exc_info.value.code == "WORKSPACE_MCP_ENDPOINT_MISSING"


def test_registered_startup_migration_contains_workspace_endpoint_migration():
    from migrations.runner import (
        REGISTERED_MIGRATIONS,
        MigrationFailurePolicy,
        MigrationPhase,
    )

    registration = next(item for item in REGISTERED_MIGRATIONS if item.migration_id == MIGRATION_ID)

    assert registration.phase == MigrationPhase.STARTUP
    assert registration.failure_policy == MigrationFailurePolicy.BLOCKING
