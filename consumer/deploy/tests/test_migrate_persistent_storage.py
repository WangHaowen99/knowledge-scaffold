from __future__ import annotations

import json
import tarfile
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from migrate_persistent_storage import migrate  # noqa: E402


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class PersistentStorageMigrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_path = self.root / "backend" / ".env"
        self.env_path.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_dry_run_rewrites_legacy_env_and_system_config_without_writing(self) -> None:
        self.env_path.write_text(
            "CHAT_STORAGE_DIR=./data/chats\nEXECUTION_STORAGE_DIR=./data/executions\n",
            encoding="utf-8",
        )
        system_config_path = self.root / ".onto-runtime" / "config" / "system_config.json"
        _write_json(system_config_path, {
            "storage": {
                "chat_storage_dir": "./data/chats",
                "execution_storage_dir": "./data/executions",
            }
        })

        report = migrate(self.root, dry_run=True)

        self.assertTrue(report["changed"])
        self.assertTrue(report["backend_env"]["changed"])
        self.assertTrue(report["system_config"]["changed"])
        self.assertEqual(
            self.env_path.read_text(encoding="utf-8"),
            "CHAT_STORAGE_DIR=./data/chats\nEXECUTION_STORAGE_DIR=./data/executions\n",
        )
        self.assertFalse((self.root / ".onto-runtime" / "data" / "chats").exists())

    def test_apply_writes_canonical_runtime_backed_storage_paths(self) -> None:
        self.env_path.write_text("KEEP=value\n", encoding="utf-8")

        report = migrate(self.root, dry_run=False)

        self.assertTrue(report["changed"])
        rendered_env = self.env_path.read_text(encoding="utf-8")
        self.assertIn("KEEP=value", rendered_env)
        self.assertIn("CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats", rendered_env)
        self.assertIn("EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions", rendered_env)
        self.assertTrue((self.root / ".onto-runtime" / "data" / "chats").is_dir())
        self.assertTrue((self.root / ".onto-runtime" / "data" / "executions").is_dir())

    def test_preserves_existing_safe_absolute_system_config_storage_paths(self) -> None:
        system_config_path = self.root / ".onto-runtime" / "config" / "system_config.json"
        _write_json(system_config_path, {
            "storage": {
                "chat_storage_dir": "/app/.onto-runtime/custom/chats",
                "execution_storage_dir": "/app/.onto-runtime/custom/executions",
            }
        })

        report = migrate(self.root, dry_run=False)

        self.assertTrue(report["backend_env"]["changed"])
        self.assertFalse(report["system_config"]["changed"])
        payload = json.loads(system_config_path.read_text(encoding="utf-8"))
        self.assertEqual(payload["storage"]["chat_storage_dir"], "/app/.onto-runtime/custom/chats")
        self.assertEqual(payload["storage"]["execution_storage_dir"], "/app/.onto-runtime/custom/executions")

    def test_apply_imports_legacy_container_storage_archive(self) -> None:
        backup_dir = self.root / "deploy" / "backups" / "case-1"
        staging_root = backup_dir / "legacy-backend-data"
        (staging_root / "chats" / "sess-a").mkdir(parents=True, exist_ok=True)
        (staging_root / "executions" / "sess-a").mkdir(parents=True, exist_ok=True)
        (staging_root / "chats" / "sess-a" / "snapshot.json").write_text(
            '{"id":"sess-a"}',
            encoding="utf-8",
        )
        (staging_root / "executions" / "sess-a" / "execution.json").write_text(
            '{"session_id":"sess-a","turns":[{"status":"awaiting_input"}]}',
            encoding="utf-8",
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive_path = backup_dir / "legacy-backend-data.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(staging_root, arcname="legacy-backend-data")

        report = migrate(self.root, dry_run=False, legacy_backup_dir=backup_dir)

        self.assertTrue(report["legacy_container_import"]["detected"])
        self.assertTrue(report["legacy_container_import"]["changed"])
        self.assertEqual(report["legacy_container_import"]["chat"]["copied_files"], 1)
        self.assertEqual(report["legacy_container_import"]["execution"]["copied_files"], 1)
        self.assertTrue((self.root / ".onto-runtime" / "data" / "chats" / "sess-a" / "snapshot.json").is_file())
        self.assertTrue((self.root / ".onto-runtime" / "data" / "executions" / "sess-a" / "execution.json").is_file())

    def test_dry_run_reports_legacy_container_storage_import_without_writing(self) -> None:
        backup_dir = self.root / "deploy" / "backups" / "case-2"
        staging_root = backup_dir / "legacy-backend-data"
        (staging_root / "chats" / "sess-b").mkdir(parents=True, exist_ok=True)
        (staging_root / "chats" / "sess-b" / "snapshot.json").write_text(
            '{"id":"sess-b"}',
            encoding="utf-8",
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive_path = backup_dir / "legacy-backend-data.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(staging_root, arcname="legacy-backend-data")

        report = migrate(self.root, dry_run=True, legacy_backup_dir=backup_dir)

        self.assertTrue(report["legacy_container_import"]["detected"])
        self.assertTrue(report["legacy_container_import"]["chat"]["changed"])
        self.assertFalse((self.root / ".onto-runtime" / "data" / "chats" / "sess-b").exists())

    def test_apply_fails_when_legacy_container_storage_conflicts_with_existing_runtime_data(self) -> None:
        backup_dir = self.root / "deploy" / "backups" / "case-3"
        staging_root = backup_dir / "legacy-backend-data"
        (staging_root / "chats" / "sess-c").mkdir(parents=True, exist_ok=True)
        (staging_root / "chats" / "sess-c" / "snapshot.json").write_text(
            '{"id":"sess-c","value":"legacy"}',
            encoding="utf-8",
        )
        backup_dir.mkdir(parents=True, exist_ok=True)
        archive_path = backup_dir / "legacy-backend-data.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            archive.add(staging_root, arcname="legacy-backend-data")
        target_path = self.root / ".onto-runtime" / "data" / "chats" / "sess-c" / "snapshot.json"
        target_path.parent.mkdir(parents=True, exist_ok=True)
        target_path.write_text('{"id":"sess-c","value":"runtime"}', encoding="utf-8")

        with self.assertRaisesRegex(ValueError, "legacy storage import conflicts"):
            migrate(self.root, dry_run=False, legacy_backup_dir=backup_dir)


if __name__ == "__main__":
    unittest.main()
