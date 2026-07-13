from __future__ import annotations

import json

import pytest

from scripts.audit_workspace_acl import audit_workspace_acl, load_auth_usernames, main


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _write_workspace(root, workspace_id, **fields):
    payload = {
        "id": workspace_id,
        "name": workspace_id,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
        **fields,
    }
    _write_json(root / workspace_id / "workspace.json", payload)


def test_audit_reports_unknown_owner_member_and_duplicate(tmp_path):
    users_file = tmp_path / "users.json"
    workspaces_root = tmp_path / "workspaces"
    _write_json(users_file, {"users": [{"username": "alice"}, {"username": "bob"}]})
    _write_workspace(
        workspaces_root,
        "private",
        owner="missing-owner",
        members=["alice", "missing-member", "alice"],
    )

    report = audit_workspace_acl(users_file, workspaces_root)

    assert report["clean"] is False
    assert report["auth_user_count"] == 2
    assert report["workspace_count"] == 1
    assert report["findings"] == [
        {"workspace_id": "private", "type": "duplicate_member", "username": "alice"},
        {"workspace_id": "private", "type": "unknown_member", "username": "missing-member"},
        {"workspace_id": "private", "type": "unknown_owner", "username": "missing-owner"},
    ]


def test_legacy_public_workspace_without_acl_fields_is_clean(tmp_path):
    users_file = tmp_path / "users.json"
    workspaces_root = tmp_path / "workspaces"
    _write_json(users_file, {"users": [{"username": "admin"}]})
    _write_workspace(workspaces_root, "legacy")

    report = audit_workspace_acl(users_file, workspaces_root)

    assert report == {
        "clean": True,
        "auth_user_count": 1,
        "workspace_count": 1,
        "pending_reconciliation_count": 0,
        "finding_count": 0,
        "findings": [],
    }


def test_audit_reports_malformed_workspace_metadata_without_stopping(tmp_path):
    users_file = tmp_path / "users.json"
    workspaces_root = tmp_path / "workspaces"
    _write_json(users_file, {"users": [{"username": "admin"}]})
    bad_path = workspaces_root / "broken" / "workspace.json"
    bad_path.parent.mkdir(parents=True)
    bad_path.write_text("{", encoding="utf-8")
    _write_workspace(workspaces_root, "healthy", owner="admin", members=[])

    report = audit_workspace_acl(users_file, workspaces_root)

    assert report["workspace_count"] == 2
    assert report["finding_count"] == 1
    assert report["findings"][0]["workspace_id"] == "broken"
    assert report["findings"][0]["type"] == "invalid_metadata"


def test_audit_reports_workspace_id_directory_mismatch(tmp_path):
    users_file = tmp_path / "users.json"
    workspaces_root = tmp_path / "workspaces"
    _write_json(users_file, {"users": [{"username": "admin"}]})
    _write_workspace(workspaces_root, "directory-id", owner="admin", members=[])
    metadata_path = workspaces_root / "directory-id" / "workspace.json"
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["id"] = "different-id"
    _write_json(metadata_path, metadata)

    report = audit_workspace_acl(users_file, workspaces_root)

    assert report["findings"] == [
        {
            "workspace_id": "directory-id",
            "type": "workspace_id_mismatch",
            "detail": "workspace.json id is 'different-id'",
        }
    ]


def test_invalid_auth_user_file_fails_closed(tmp_path):
    users_file = tmp_path / "users.json"
    _write_json(users_file, {"users": [{"role": "admin"}]})

    with pytest.raises(ValueError, match="has no username"):
        load_auth_usernames(users_file)


def test_audit_reports_pending_user_deletion_reconciliation(tmp_path):
    runtime_root = tmp_path / ".onto-runtime"
    users_file = tmp_path / "users.json"
    workspaces_root = runtime_root / "workspaces"
    _write_json(users_file, {"users": [{"username": "admin"}]})
    _write_workspace(workspaces_root, "shared", owner=None, members=[])
    _write_json(
        runtime_root / "system" / "user-deletion-reconciliation.json",
        {
            "version": 1,
            "records": [
                {
                    "id": "cleanup-1",
                    "username": "alice",
                    "status": "pending",
                }
            ],
        },
    )

    report = audit_workspace_acl(users_file, workspaces_root)

    assert report["clean"] is False
    assert report["pending_reconciliation_count"] == 1
    assert report["findings"] == [
        {
            "workspace_id": "<system>",
            "type": "pending_user_deletion_reconciliation",
            "username": "alice",
            "detail": "reconciliation_id=cleanup-1",
        }
    ]


def test_cli_exit_code_distinguishes_clean_and_findings(tmp_path, capsys):
    users_file = tmp_path / "users.json"
    workspaces_root = tmp_path / "workspaces"
    _write_json(users_file, {"users": [{"username": "admin"}]})
    _write_workspace(workspaces_root, "private", owner="missing", members=[])

    exit_code = main(
        [
            "--auth-users-file",
            str(users_file),
            "--workspaces-root",
            str(workspaces_root),
        ]
    )

    assert exit_code == 2
    assert json.loads(capsys.readouterr().out)["finding_count"] == 1
