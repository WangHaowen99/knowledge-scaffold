from __future__ import annotations

import base64
import json
import stat
import sys
import tarfile
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrate_auth_workspace import execute_migration  # noqa: E402


def _hash(seed: str) -> str:
    salt = base64.b64encode(f"salt-{seed}".encode()).decode()
    digest = base64.b64encode(f"digest-{seed}".encode()).decode()
    return f"pbkdf2_sha256$600000${salt}${digest}"


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def _user(username: str = "admin", **fields):
    return {
        "username": username,
        "password_hash": _hash(username),
        "role": "admin" if username == "admin" else "user",
        "must_change_password": False,
        "password_changed_at": "2026-01-01T00:00:00+00:00",
        **fields,
    }


def _reconciliation_record(
    username: str = "alice",
    status: str = "cleanup_pending",
    record_id: str = "a" * 32,
    **fields,
):
    payload = {
        "id": record_id,
        "username": username,
        "status": status,
        "identity_deleted": True,
        "created_at": "2026-08-04T00:00:00+00:00",
        "updated_at": "2026-08-04T00:00:00+00:00",
        "attempts": 1,
        "completed_steps": [],
        "errors": {},
    }
    payload.update(fields)
    return payload


class MigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.backups = self.root / "deploy" / "backups"

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def execute(self, *, dry_run: bool, writes_stopped: bool = False):
        return execute_migration(
            self.root,
            dry_run=dry_run,
            writes_stopped=writes_stopped,
            backup_root=self.backups,
            from_version="old",
            to_version="new",
        )

    def test_dry_run_plans_copy_without_writing(self) -> None:
        _write_json(self.root / ".onto-runtime" / "users.json", {"users": [_user()]})

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(report["users"]["action"], "would_copy_runtime_to_auth")
        self.assertFalse((self.root / ".onto-auth" / "users.json").exists())
        self.assertEqual(list(self.backups.glob("*.tar.gz")), [])

    def test_apply_copies_atomically_backs_up_and_is_idempotent(self) -> None:
        source = self.root / ".onto-runtime" / "users.json"
        _write_json(source, {"users": [_user()]})
        workspace = self.root / ".onto-runtime" / "workspaces" / "public" / "workspace.json"
        _write_json(workspace, {"id": "public", "name": "Public"})
        keys = self.root / ".onto-auth" / "keys" / "private.pem"
        keys.parent.mkdir(parents=True)
        keys.write_text("private-key", encoding="utf-8")

        code, report = self.execute(dry_run=False, writes_stopped=True)

        destination = self.root / ".onto-auth" / "users.json"
        self.assertEqual(code, 0)
        self.assertEqual(source.read_bytes(), destination.read_bytes())
        self.assertEqual(stat.S_IMODE(destination.stat().st_mode), 0o600)
        self.assertEqual(report["workspace_acl"]["public_workspaces"], ["public"])
        self.assertFalse(report["workspace_acl"]["visibility_changed"])
        self.assertEqual(report["users"]["token_version_defaulted_to_one"], 1)
        snapshot = Path(report["backup"])
        with tarfile.open(snapshot, "r:gz") as archive:
            names = archive.getnames()
        self.assertIn(".onto-runtime/users.json", names)
        self.assertIn(".onto-runtime/workspaces", names)
        self.assertIn(".onto-auth/keys", names)

        before = destination.read_bytes()
        second_code, second = self.execute(dry_run=False, writes_stopped=True)
        self.assertEqual(second_code, 0)
        self.assertEqual(second["users"]["action"], "in_sync")
        self.assertEqual(destination.read_bytes(), before)

    def test_missing_token_version_equals_explicit_one(self) -> None:
        local = _user()
        remote = {**local, "token_version": 1}
        _write_json(self.root / ".onto-runtime" / "users.json", {"users": [local]})
        _write_json(self.root / ".onto-auth" / "users.json", {"users": [remote]})

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(report["users"]["action"], "in_sync")
        self.assertEqual(report["users"]["field_differences"], [])

    def test_diverged_stores_fail_with_redacted_field_diff(self) -> None:
        local = _user()
        remote = {**local, "password_hash": _hash("changed"), "role": "user"}
        _write_json(self.root / ".onto-runtime" / "users.json", {"users": [local]})
        _write_json(self.root / ".onto-auth" / "users.json", {"users": [remote]})

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "USER_STORES_DIVERGED")
        rendered = json.dumps(report)
        self.assertNotIn(local["password_hash"], rendered)
        self.assertNotIn(remote["password_hash"], rendered)
        self.assertIn("sha256:", rendered)

    def test_invalid_hash_fails_closed(self) -> None:
        _write_json(
            self.root / ".onto-runtime" / "users.json",
            {"users": [{**_user(), "password_hash": "plaintext"}]},
        )

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "INVALID_USER_STORE")

    def test_orphan_acl_and_same_name_reconciliation_block_startup(self) -> None:
        _write_json(self.root / ".onto-auth" / "users.json", {"users": [_user(), _user("alice")]})
        workspace_path = (
            self.root / ".onto-runtime" / "workspaces" / "private" / "workspace.json"
        )
        _write_json(workspace_path, {"id": "private", "owner": "missing", "members": ["ghost"]})
        _write_json(
            self.root / ".onto-runtime" / "system" / "user-deletion-reconciliation.json",
            {
                "version": 1,
                "records": [_reconciliation_record(status="pending")],
            },
        )
        before = workspace_path.read_bytes()

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "WORKSPACE_ACL_RECONCILIATION_REQUIRED")
        self.assertIn("不会自动修改", report["error"]["detail"])
        self.assertIn("unknown_member", report["error"]["suggestion"])
        self.assertIn("unknown_owner", report["error"]["suggestion"])
        self.assertIn("转移给已验证账号", report["error"]["suggestion"])
        self.assertNotIn("owner 设为 null", report["error"]["suggestion"])
        self.assertIn("workspace_acl.clean=true", report["error"]["suggestion"])
        self.assertEqual(workspace_path.read_bytes(), before)
        finding_types = {item["type"] for item in report["workspace_acl"]["findings"]}
        self.assertEqual(
            finding_types,
            {
                "unknown_owner",
                "unknown_member",
                "pending_user_deletion_reconciliation",
                "same_name_account_inheritance_risk",
            },
        )

    def test_invalid_reconciliation_schema_is_not_reported_as_pending(self) -> None:
        _write_json(self.root / ".onto-auth" / "users.json", {"users": [_user(), _user("alice")]})
        invalid_record = _reconciliation_record()
        invalid_record.pop("status")
        _write_json(
            self.root / ".onto-runtime" / "system" / "user-deletion-reconciliation.json",
            {"version": 1, "records": [invalid_record]},
        )

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "WORKSPACE_ACL_RECONCILIATION_REQUIRED")
        self.assertEqual(
            report["workspace_acl"]["findings"],
            [{
                "workspace_id": "<system>",
                "type": "invalid_user_deletion_reconciliation",
                "detail": "records[0].status must be a non-empty string",
            }],
        )

    def test_acl_references_with_surrounding_whitespace_are_invalid(self) -> None:
        _write_json(
            self.root / ".onto-auth" / "users.json",
            {"users": [_user(), _user("alice"), _user("bob")]},
        )
        _write_json(
            self.root / ".onto-runtime" / "workspaces" / "private" / "workspace.json",
            {"id": "private", "owner": " alice ", "members": [" bob "]},
        )

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 2)
        self.assertFalse(report["workspace_acl"]["clean"])
        self.assertEqual(
            {item["type"] for item in report["workspace_acl"]["findings"]},
            {"invalid_owner", "invalid_member"},
        )

    def test_empty_owner_is_invalid_instead_of_public(self) -> None:
        _write_json(self.root / ".onto-auth" / "users.json", {"users": [_user()]})
        _write_json(
            self.root / ".onto-runtime" / "workspaces" / "private" / "workspace.json",
            {"id": "private", "owner": "", "members": []},
        )

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 2)
        self.assertEqual(report["workspace_acl"]["public_workspaces"], [])
        self.assertEqual(report["workspace_acl"]["findings"][0]["type"], "invalid_owner")

    def test_apply_orphan_acl_stays_blocked_without_modifying_workspace(self) -> None:
        _write_json(self.root / ".onto-auth" / "users.json", {"users": [_user()]})
        workspace_path = (
            self.root / ".onto-runtime" / "workspaces" / "private" / "workspace.json"
        )
        _write_json(
            workspace_path,
            {"id": "private", "owner": "admin", "members": ["ghost"]},
        )
        before = workspace_path.read_bytes()

        code, report = self.execute(dry_run=False, writes_stopped=True)

        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "WORKSPACE_ACL_RECONCILIATION_REQUIRED")
        self.assertEqual(workspace_path.read_bytes(), before)
        self.assertIsNotNone(report["backup"])

    def test_apply_requires_explicit_stop_confirmation(self) -> None:
        code, report = self.execute(dry_run=False, writes_stopped=False)

        self.assertEqual(code, 2)
        self.assertEqual(report["error"]["code"], "WRITES_NOT_STOPPED")

    def test_public_workspace_without_user_store_allows_auth_initialization(self) -> None:
        _write_json(
            self.root / ".onto-runtime" / "workspaces" / "public" / "workspace.json",
            {"id": "public", "name": "Public"},
        )

        code, report = self.execute(dry_run=True)

        self.assertEqual(code, 0)
        self.assertEqual(report["users"]["action"], "initialize_auth_on_start")
        self.assertEqual(report["workspace_acl"]["public_workspaces"], ["public"])


if __name__ == "__main__":
    unittest.main()
