from __future__ import annotations

import json
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


if __name__ == "__main__":
    unittest.main()
