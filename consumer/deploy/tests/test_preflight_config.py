from __future__ import annotations

import json
import unittest
from collections.abc import Callable as CallableABC
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from typing import Union, get_origin
from unittest.mock import patch

from deploy_synthetic_credentials import (  # noqa: E402
    AUTH_ADMIN_TOKEN,
    BOOTSTRAP_ADMIN_PASSWORD,
    MCP_TOKEN_SECRET,
    PLAINTEXT_GRAPH_SECRET,
    SHARED_SECRET,
    valid_environment,
)
from deploy.preflight_config import (
    ComposeConfigLoader,
    ComposeEnvFiles,
    _load_compose_config_json,
    _release_host_ports,
    validate_config,
)

VALID_ENV = valid_environment()


class PreflightConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "backend" / ".env"
        self.env_file.parent.mkdir(parents=True)
        self.compose_file = self.root / "deploy" / "docker-compose.release.yml"
        self.compose_file.parent.mkdir(parents=True, exist_ok=True)
        self.compose_file.write_text(
            """\
services:
  backend:
    ports:
      - "${BACKEND_PORT:-5100}:5055"
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - PROJECT_ROOT=/app/.onto-runtime
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
  mcp:
    ports:
      - "${MCP_SERVER_PORT:-9000}:7488"
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - PROJECT_ROOT=/app/.onto-runtime
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
  frontend:
    ports:
      - "${FRONTEND_PORT:-6600}:80"
""",
            encoding="utf-8",
        )
        self.compose_config = {
            "services": {
                "backend": {"ports": [{"target": 5055, "published": "5100"}]},
                "mcp": {"ports": [{"target": 7488, "published": "9000"}]},
                "frontend": {"ports": [{"target": 80, "published": "6600"}]},
            },
        }
        loader_patch = patch(
            "deploy.preflight_config._load_compose_config_json",
            return_value=(self.compose_config, []),
        )
        loader_patch.start()
        self.addCleanup(loader_patch.stop)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_runtime_type_aliases_are_python39_compatible(self) -> None:
        self.assertIs(get_origin(ComposeEnvFiles), Union)
        self.assertIs(get_origin(ComposeConfigLoader), CallableABC)

    def test_compose_config_loader_uses_all_release_env_files(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        legacy_env_file = self.root / ".env"
        legacy_env_file.write_text("MCP_SERVER_PORT=9000\n", encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[-1] == "version":
                return SimpleNamespace(returncode=0, stdout="Docker Compose v2", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps(self.compose_config),
                stderr="",
            )

        with patch("deploy.preflight_config.subprocess.run", fake_run):
            config, errors = _load_compose_config_json(
                self.compose_file,
                (self.env_file, legacy_env_file),
            )

        self.assertEqual(errors, [])
        self.assertEqual(config, self.compose_config)
        compose_command = calls[1]
        env_indexes = [index for index, value in enumerate(compose_command) if value == "--env-file"]
        self.assertEqual(len(env_indexes), 2)
        self.assertEqual(compose_command[env_indexes[0] + 1], str(self.env_file.resolve()))
        self.assertEqual(compose_command[env_indexes[1] + 1], str(legacy_env_file.resolve()))

    def test_compose_config_loader_falls_back_to_docker_compose_when_plugin_is_unavailable(
        self,
    ) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        legacy_env_file = self.root / ".env"
        legacy_env_file.write_text(
            "BACKEND_PORT=5100\nFRONTEND_PORT=6600\nMCP_SERVER_PORT=9000\n",
            encoding="utf-8",
        )
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[:2] == ["docker", "compose"] and command[-1] == "version":
                return SimpleNamespace(returncode=1, stdout="", stderr="missing plugin")
            if command[0] == "docker-compose" and command[-1] == "version":
                return SimpleNamespace(returncode=0, stdout="docker-compose version 1.29.2, build test", stderr="")
            if command[0] == "docker-compose" and "config" in command and "--format" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="unknown flag: --format")
            if command[0] == "docker-compose" and "config" in command:
                return SimpleNamespace(
                    returncode=0, stdout="services:\n  backend:\n    ports:\n      - 5100:5055/tcp\n", stderr=""
                )
            raise AssertionError(f"unexpected command: {command}")

        with patch("deploy.preflight_config.subprocess.run", fake_run):
            config, errors = _load_compose_config_json(
                self.compose_file,
                (self.env_file, legacy_env_file),
            )

        self.assertEqual(errors, [])
        self.assertEqual(config, self.compose_config)
        self.assertEqual(calls[0], ["docker", "compose", "version"])
        self.assertEqual(calls[1], ["docker-compose", "version"])
        self.assertEqual(calls[2][0], "docker-compose")
        self.assertIn("--format", calls[2])
        self.assertEqual(calls[3][0], "docker-compose")
        self.assertIn("config", calls[3])

    def test_compose_config_loader_reports_failure_when_legacy_compose_config_validation_fails(
        self,
    ) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        calls: list[list[str]] = []

        def fake_run(command, **_kwargs):
            calls.append(command)
            if command[:2] == ["docker", "compose"] and command[-1] == "version":
                return SimpleNamespace(returncode=1, stdout="", stderr="missing plugin")
            if command[0] == "docker-compose" and command[-1] == "version":
                return SimpleNamespace(returncode=0, stdout="docker-compose version 1.29.2, build test", stderr="")
            if command[0] == "docker-compose" and "config" in command and "--format" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="unknown flag: --format")
            if command[0] == "docker-compose" and "config" in command:
                return SimpleNamespace(returncode=1, stdout="", stderr="compose file invalid")
            raise AssertionError(f"unexpected command: {command}")

        with patch("deploy.preflight_config.subprocess.run", fake_run):
            config, errors = _load_compose_config_json(
                self.compose_file,
                self.env_file,
            )

        self.assertIsNone(config)
        self.assertEqual(errors[0]["type"], "compose_config_unavailable")
        self.assertEqual(errors[0]["detail"], "compose config failed")
        self.assertEqual(calls[0], ["docker", "compose", "version"])
        self.assertEqual(calls[1], ["docker-compose", "version"])
        self.assertEqual(calls[2][0], "docker-compose")
        self.assertIn("--format", calls[2])
        self.assertEqual(calls[3][0], "docker-compose")
        self.assertIn("config", calls[3])

    def test_release_host_ports_use_resolved_compose_mappings(self) -> None:
        self.assertEqual(
            _release_host_ports(self.compose_config),
            {"backend": 5100, "mcp": 9000, "frontend": 6600},
        )

    def test_valid_config_without_graph_connections(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["graph_data_source_count"], 0)
        self.assertEqual(report["auth"]["issuer"], "")
        self.assertEqual(report["auth"]["audience"], "")

    def test_legacy_user_limits_are_ignored(self) -> None:
        self.env_file.write_text(
            VALID_ENV.replace("AUTH_USER_MAX_COUNT=10", "AUTH_USER_MAX_COUNT=11"),
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(
            report["limits"],
            {
                "user_max_count": 0,
                "auth_user_max_count": 0,
            },
        )

    def test_missing_user_limits_default_to_unlimited(self) -> None:
        self.env_file.write_text(
            VALID_ENV.replace("USER_MAX_COUNT=10\n", "").replace("AUTH_USER_MAX_COUNT=10\n", ""),
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["limits"]["user_max_count"], 0)
        self.assertEqual(report["limits"]["auth_user_max_count"], 0)

    def test_rejects_reused_secret(self) -> None:
        self.env_file.write_text(
            VALID_ENV.replace(AUTH_ADMIN_TOKEN, SHARED_SECRET)
            .replace(MCP_TOKEN_SECRET, SHARED_SECRET)
            .replace("AUTH_USER_MAX_COUNT=10", "AUTH_USER_MAX_COUNT=11"),
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertIn("mcp_secret_reuses_auth_admin_token", error_types)
        self.assertNotIn("user_limit_mismatch", error_types)

    def test_rejects_missing_or_reused_bootstrap_password(self) -> None:
        self.env_file.write_text(
            VALID_ENV.replace(f"AUTH_BOOTSTRAP_ADMIN_PASSWORD={BOOTSTRAP_ADMIN_PASSWORD}\n", ""),
            encoding="utf-8",
        )
        missing = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )
        self.assertIn(
            "weak_or_missing_bootstrap_admin_password",
            {item["type"] for item in missing["errors"]},
        )

        self.env_file.write_text(
            VALID_ENV.replace(BOOTSTRAP_ADMIN_PASSWORD, AUTH_ADMIN_TOKEN),
            encoding="utf-8",
        )
        reused = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )
        self.assertIn(
            "bootstrap_password_reuses_auth_admin_token",
            {item["type"] for item in reused["errors"]},
        )

    def test_existing_auth_user_store_does_not_require_bootstrap_password(self) -> None:
        self.env_file.write_text(
            VALID_ENV.replace(f"AUTH_BOOTSTRAP_ADMIN_PASSWORD={BOOTSTRAP_ADMIN_PASSWORD}\n", ""),
            encoding="utf-8",
        )
        user_store = self.root / ".onto-auth" / "users.json"
        user_store.parent.mkdir()
        user_store.write_text('{"users": []}', encoding="utf-8")

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertTrue(report["ok"])

    def test_graph_config_requires_scope_and_encrypted_secret(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "ontology-data-sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 2,
                    "sources": {
                        "demo-ontology": {
                            "mode": "direct_provider",
                            "provider": "neo4j",
                            "config": {
                                "uri": "bolt://neo4j:7687",
                                "username": "neo4j",
                                "password": PLAINTEXT_GRAPH_SECRET,
                            },
                        }
                    },
                }
            ),
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertEqual(error_types, {"plaintext_graph_secret", "missing_graph_scope_field"})
        self.assertEqual(report["warnings"][0]["field"], "ontology_property")
        self.assertEqual(report["warnings"][0]["reason"], "missing")
        self.assertEqual(report["warnings"][0]["effect"], "ontology_scope_disabled")

        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["sources"]["demo-ontology"]["config"]["ontology_property"] = ""
        path.write_text(json.dumps(payload), encoding="utf-8")

        blank_report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertEqual(blank_report["warnings"][0]["reason"], "blank")
        self.assertEqual(
            blank_report["warnings"][0]["effect"],
            "ontology_scope_disabled",
        )

    def test_pre_migration_defers_persisted_data_validation_to_migration_runner(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "ontology-data-sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "sources": {
                        "legacy": {
                            "mode": "connection_ref",
                            "connection_id": "shared-graph",
                        },
                        "direct": {
                            "mode": "direct_provider",
                            "provider": "neo4j",
                            "config": {
                                "uri": "bolt://neo4j:7687",
                                "username": "neo4j",
                                "password": PLAINTEXT_GRAPH_SECRET,
                            },
                        },
                    },
                }
            ),
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
            pre_migration=True,
        )

        self.assertTrue(report["ok"])
        self.assertEqual(report["phase"], "pre_migration")
        self.assertEqual(report["persisted_data_validation"], "deferred")
        self.assertIsNone(report["graph_data_source_count"])

        strict = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )
        self.assertFalse(strict["ok"])
        error_types = {item["type"] for item in strict["errors"]}
        self.assertEqual(
            error_types,
            {
                "legacy_data_source_mode",
                "plaintext_graph_secret",
                "missing_graph_scope_field",
            },
        )

    def test_preflight_rejects_relative_storage_paths_after_migration_window(self) -> None:
        self.env_file.write_text(
            VALID_ENV + "CHAT_STORAGE_DIR=./data/chats\nEXECUTION_STORAGE_DIR=./data/executions\n",
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertFalse(report["ok"])
        error_types = [item["type"] for item in report["errors"]]
        self.assertEqual(error_types.count("relative_storage_path"), 2)

    def test_preflight_defers_legacy_relative_storage_paths_during_pre_migration(self) -> None:
        self.env_file.write_text(
            VALID_ENV + "CHAT_STORAGE_DIR=./data/chats\nEXECUTION_STORAGE_DIR=./data/executions\n",
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
            pre_migration=True,
        )

        self.assertTrue(report["ok"])
        warning_types = {item["type"] for item in report["warnings"]}
        self.assertIn("relative_storage_path", warning_types)

    def test_preflight_rejects_storage_paths_outside_runtime_root(self) -> None:
        self.env_file.write_text(
            VALID_ENV + "CHAT_STORAGE_DIR=/app/external/chats\nEXECUTION_STORAGE_DIR=/app/external/executions\n",
            encoding="utf-8",
        )

        report = validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
        )

        self.assertFalse(report["ok"])
        error_types = [item["type"] for item in report["errors"]]
        self.assertEqual(error_types.count("storage_path_outside_runtime_root"), 2)


if __name__ == "__main__":
    unittest.main()
