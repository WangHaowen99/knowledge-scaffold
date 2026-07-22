from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from preflight_config import _load_compose_config_json, validate_config  # noqa: E402


VALID_ENV = """\
AUTH_SERVER_URL=http://auth-server:5056
AUTH_SERVER_ADMIN_TOKEN=auth-admin-token-that-is-long
MCP_SERVER_PORT=7488
MCP_TOKEN_SECRET=mcp-signing-secret-that-is-independent-and-long
USER_MAX_COUNT=10
AUTH_USER_MAX_COUNT=10
"""


SAFE_RELEASE_COMPOSE = """\
services:
  backend:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - PROJECT_ROOT=/app/.onto-runtime
      - SANDBOX_ENABLED=true
      - SANDBOX_FALLBACK_LOCAL=false
      - SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
  mcp:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - PROJECT_ROOT=/app/.onto-runtime
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
"""


SAFE_BACKEND_ENV = {
    "PROJECT_ROOT": "/app/.onto-runtime",
    "SANDBOX_ENABLED": "true",
    "SANDBOX_FALLBACK_LOCAL": "false",
    "SANDBOX_SECRET_ALLOW_PLAIN_HTTP": "false",
    "CHAT_STORAGE_DIR": "/app/.onto-runtime/data/chats",
    "EXECUTION_STORAGE_DIR": "/app/.onto-runtime/data/executions",
}


def _compose_loader(environment: dict[str, str] | None = None):
    resolved_environment = dict(SAFE_BACKEND_ENV if environment is None else environment)

    def _load(_compose_file: Path, _env_file: Path):
        return {
            "services": {
                "backend": {
                    "environment": resolved_environment,
                },
            },
        }, []

    return _load


class PreflightConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "backend" / ".env"
        self.env_file.parent.mkdir(parents=True)
        self.compose_file = self.root / "deploy" / "docker-compose.release.yml"
        self.compose_file.parent.mkdir(parents=True, exist_ok=True)
        self.compose_file.write_text(SAFE_RELEASE_COMPOSE, encoding="utf-8")

    def validate(
        self,
        *,
        compose_environment: dict[str, str] | None = None,
        pre_migration: bool = False,
    ) -> dict:
        return validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
            pre_migration=pre_migration,
            compose_config_loader=_compose_loader(compose_environment),
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_compose_config_loader_uses_docker_compose_resolved_json(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[-1] == "version":
                return SimpleNamespace(returncode=0, stdout="Docker Compose v2", stderr="")
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "services": {
                        "backend": {
                            "environment": {
                                "SANDBOX_ENABLED": "false",
                                "SANDBOX_FALLBACK_LOCAL": "true",
                            },
                        },
                    },
                }),
                stderr="",
            )

        with patch(
            "preflight_config._compose_command_candidates",
            return_value=(("docker", "compose"),),
        ), patch("preflight_config.subprocess.run", fake_run):
            config, errors = _load_compose_config_json(
                self.compose_file,
                self.env_file,
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            config["services"]["backend"]["environment"]["SANDBOX_ENABLED"],
            "false",
        )
        compose_command = calls[1][0]
        self.assertIn("--env-file", compose_command)
        self.assertIn("--format", compose_command)
        self.assertIn("json", compose_command)

    def test_valid_config_without_graph_connections(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")

        report = self.validate()

        self.assertTrue(report["ok"])
        self.assertEqual(report["graph_data_source_count"], 0)
        self.assertEqual(
            report["sandbox"],
            {
                "enabled": "true",
                "fallback_local": "false",
                "secret_allow_plain_http": "false",
            },
        )
        self.assertEqual(report["auth"]["issuer"], "")
        self.assertEqual(report["auth"]["audience"], "")

    def test_rejects_missing_effective_release_sandbox_settings(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.compose_file.write_text(
            SAFE_RELEASE_COMPOSE.replace("      - SANDBOX_ENABLED=true\n", ""),
            encoding="utf-8",
        )

        compose_environment = dict(SAFE_BACKEND_ENV)
        compose_environment.pop("SANDBOX_ENABLED")
        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        self.assertIn("missing_release_sandbox_setting", {item["type"] for item in report["errors"]})

    def test_rejects_release_sandbox_drift_from_compose(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.compose_file.write_text(
            SAFE_RELEASE_COMPOSE.replace(
                "      - SANDBOX_FALLBACK_LOCAL=false",
                "      - SANDBOX_FALLBACK_LOCAL=true",
            ),
            encoding="utf-8",
        )

        compose_environment = dict(SAFE_BACKEND_ENV)
        compose_environment["SANDBOX_FALLBACK_LOCAL"] = "true"
        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        self.assertIn("invalid_release_sandbox_setting", {item["type"] for item in report["errors"]})

    def test_rejects_inline_mapping_backend_sandbox_drift_from_compose_config(self) -> None:
        self.env_file.write_text(
            VALID_ENV
            + "SANDBOX_ENABLED=true\n"
            + "SANDBOX_FALLBACK_LOCAL=false\n"
            + "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false\n",
            encoding="utf-8",
        )
        self.compose_file.write_text(
            """\
services:
  backend:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment: {SANDBOX_ENABLED: false, SANDBOX_FALLBACK_LOCAL: true, SANDBOX_SECRET_ALLOW_PLAIN_HTTP: false}
""",
            encoding="utf-8",
        )
        compose_environment = dict(SAFE_BACKEND_ENV)
        compose_environment["SANDBOX_ENABLED"] = "false"
        compose_environment["SANDBOX_FALLBACK_LOCAL"] = "true"

        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertIn("invalid_release_sandbox_setting", error_types)

    def test_rejects_quoted_list_backend_sandbox_drift_from_compose_config(self) -> None:
        self.env_file.write_text(
            VALID_ENV
            + "SANDBOX_ENABLED=true\n"
            + "SANDBOX_FALLBACK_LOCAL=false\n"
            + "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false\n",
            encoding="utf-8",
        )
        self.compose_file.write_text(
            """\
services:
  backend:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - "SANDBOX_ENABLED=false"
      - "SANDBOX_FALLBACK_LOCAL=true"
      - "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false"
""",
            encoding="utf-8",
        )
        compose_environment = dict(SAFE_BACKEND_ENV)
        compose_environment["SANDBOX_ENABLED"] = "false"
        compose_environment["SANDBOX_FALLBACK_LOCAL"] = "true"

        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        self.assertIn("invalid_release_sandbox_setting", {item["type"] for item in report["errors"]})

    def test_rejects_anchor_backend_sandbox_drift_from_compose_config(self) -> None:
        self.env_file.write_text(
            VALID_ENV
            + "SANDBOX_ENABLED=true\n"
            + "SANDBOX_FALLBACK_LOCAL=false\n"
            + "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false\n",
            encoding="utf-8",
        )
        self.compose_file.write_text(
            """\
x-backend-env: &backend_env
  SANDBOX_ENABLED: false
  SANDBOX_FALLBACK_LOCAL: true
  SANDBOX_SECRET_ALLOW_PLAIN_HTTP: false
services:
  backend:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment: *backend_env
""",
            encoding="utf-8",
        )
        compose_environment = dict(SAFE_BACKEND_ENV)
        compose_environment["SANDBOX_ENABLED"] = "false"
        compose_environment["SANDBOX_FALLBACK_LOCAL"] = "true"

        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        self.assertIn("invalid_release_sandbox_setting", {item["type"] for item in report["errors"]})

    def test_rejects_env_file_resolved_backend_sandbox_drift_from_compose_config(self) -> None:
        self.env_file.write_text(
            VALID_ENV
            + "SANDBOX_ENABLED=false\n"
            + "SANDBOX_FALLBACK_LOCAL=true\n"
            + "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false\n",
            encoding="utf-8",
        )
        self.compose_file.write_text(
            """\
services:
  backend:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    env_file:
      - ./backend/.env
    environment:
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
""",
            encoding="utf-8",
        )
        compose_environment = dict(SAFE_BACKEND_ENV)
        compose_environment["SANDBOX_ENABLED"] = "false"
        compose_environment["SANDBOX_FALLBACK_LOCAL"] = "true"

        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        self.assertIn("invalid_release_sandbox_setting", {item["type"] for item in report["errors"]})

    def test_rejects_duplicate_backend_release_sandbox_setting(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.compose_file.write_text(
            SAFE_RELEASE_COMPOSE.replace(
                "      - SANDBOX_FALLBACK_LOCAL=false\n",
                "      - SANDBOX_FALLBACK_LOCAL=false\n      - SANDBOX_FALLBACK_LOCAL=true\n",
            ),
            encoding="utf-8",
        )

        report = self.validate()

        self.assertFalse(report["ok"])
        self.assertIn("duplicate_release_sandbox_setting", {item["type"] for item in report["errors"]})

    def test_rejects_bare_backend_release_sandbox_setting(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.compose_file.write_text(
            SAFE_RELEASE_COMPOSE.replace(
                "      - SANDBOX_ENABLED=true",
                "      - SANDBOX_ENABLED",
            ),
            encoding="utf-8",
        )

        report = self.validate()

        self.assertFalse(report["ok"])
        self.assertIn(
            "unsupported_release_sandbox_setting_source",
            {item["type"] for item in report["errors"]},
        )

    def test_other_service_sandbox_setting_does_not_mask_backend_env_drift(self) -> None:
        self.env_file.write_text(
            VALID_ENV + "SANDBOX_FALLBACK_LOCAL=true\n",
            encoding="utf-8",
        )
        self.compose_file.write_text(
            """\
services:
  backend:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - PROJECT_ROOT=/app/.onto-runtime
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
  mcp:
    volumes:
      - ./.onto-runtime:/app/.onto-runtime
    environment:
      - PROJECT_ROOT=/app/.onto-runtime
      - SANDBOX_ENABLED=true
      - SANDBOX_FALLBACK_LOCAL=false
      - SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false
      - CHAT_STORAGE_DIR=/app/.onto-runtime/data/chats
      - EXECUTION_STORAGE_DIR=/app/.onto-runtime/data/executions
""",
            encoding="utf-8",
        )

        compose_environment = {
            "PROJECT_ROOT": "/app/.onto-runtime",
            "SANDBOX_FALLBACK_LOCAL": "true",
            "CHAT_STORAGE_DIR": "/app/.onto-runtime/data/chats",
            "EXECUTION_STORAGE_DIR": "/app/.onto-runtime/data/executions",
        }
        report = self.validate(compose_environment=compose_environment)

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertIn("missing_release_sandbox_setting", error_types)
        self.assertIn("invalid_release_sandbox_setting", error_types)

    def test_backend_interpolated_sandbox_setting_is_rejected(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.compose_file.write_text(
            SAFE_RELEASE_COMPOSE.replace(
                "      - SANDBOX_FALLBACK_LOCAL=false",
                "      - SANDBOX_FALLBACK_LOCAL=${SANDBOX_FALLBACK_LOCAL}",
            ),
            encoding="utf-8",
        )

        report = self.validate()

        self.assertFalse(report["ok"])
        self.assertIn(
            "unsupported_release_sandbox_setting_source",
            {item["type"] for item in report["errors"]},
        )

    def test_rejects_divergent_limits_and_reused_secret(self) -> None:
        shared = "shared-secret-that-is-long-enough-for-both"
        self.env_file.write_text(
            VALID_ENV.replace("auth-admin-token-that-is-long", shared)
            .replace("mcp-signing-secret-that-is-independent-and-long", shared)
            .replace("AUTH_USER_MAX_COUNT=10", "AUTH_USER_MAX_COUNT=11"),
            encoding="utf-8",
        )

        report = self.validate()

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertIn("mcp_secret_reuses_auth_admin_token", error_types)
        self.assertIn("user_limit_mismatch", error_types)

    def test_graph_config_requires_scope_and_encrypted_secret(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "ontology-data-sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "version": 2,
                "sources": {"demo-ontology": {
                    "mode": "direct_provider",
                    "provider": "neo4j",
                    "config": {
                        "uri": "bolt://neo4j:7687",
                        "username": "neo4j",
                        "password": "plaintext",
                    },
                }},
            }),
            encoding="utf-8",
        )

        report = self.validate()

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertEqual(error_types, {"plaintext_graph_secret", "missing_graph_scope_field"})
        self.assertEqual(report["warnings"][0]["field"], "ontology_property")

    def test_pre_migration_defers_persisted_data_validation_to_migration_runner(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "ontology-data-sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
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
                            "password": "plaintext",
                        },
                    },
                },
            }),
            encoding="utf-8",
        )

        report = self.validate(pre_migration=True)

        self.assertTrue(report["ok"])
        self.assertEqual(report["phase"], "pre_migration")
        self.assertEqual(report["persisted_data_validation"], "deferred")
        self.assertIsNone(report["graph_data_source_count"])

        strict = self.validate()
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

        report = self.validate()

        self.assertFalse(report["ok"])
        error_types = [item["type"] for item in report["errors"]]
        self.assertEqual(error_types.count("relative_storage_path"), 2)

    def test_preflight_defers_legacy_relative_storage_paths_during_pre_migration(self) -> None:
        self.env_file.write_text(
            VALID_ENV + "CHAT_STORAGE_DIR=./data/chats\nEXECUTION_STORAGE_DIR=./data/executions\n",
            encoding="utf-8",
        )

        report = self.validate(pre_migration=True)

        self.assertTrue(report["ok"])
        warning_types = {item["type"] for item in report["warnings"]}
        self.assertIn("relative_storage_path", warning_types)

    def test_preflight_rejects_storage_paths_outside_runtime_root(self) -> None:
        self.env_file.write_text(
            VALID_ENV
            + "CHAT_STORAGE_DIR=/app/external/chats\nEXECUTION_STORAGE_DIR=/app/external/executions\n",
            encoding="utf-8",
        )

        report = self.validate()

        self.assertFalse(report["ok"])
        error_types = [item["type"] for item in report["errors"]]
        self.assertEqual(error_types.count("storage_path_outside_runtime_root"), 2)


if __name__ == "__main__":
    unittest.main()
