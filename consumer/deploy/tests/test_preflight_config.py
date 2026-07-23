from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "backend"))

from preflight_config import (  # noqa: E402
    _load_compose_config_json,
    _ontology_has_bound_python_script,
    _release_host_ports,
    _release_sandbox_structured_source_errors,
    validate_config,
)
from ontology.ontology_catalog import load_script_catalog  # noqa: E402


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


def _compose_loader(
    environment: dict[str, str] | None = None,
    source_environment: dict[str, str] | list[str] | None = None,
):
    resolved_environment = dict(SAFE_BACKEND_ENV if environment is None else environment)
    resolved_source_environment = (
        dict(SAFE_BACKEND_ENV) if source_environment is None else source_environment
    )

    def _load(_compose_file: Path, _env_file: Path):
        effective_config = {
            "services": {
                "backend": {
                    "environment": resolved_environment,
                    "ports": [{"target": 5055, "published": "5100"}],
                },
                "mcp": {"ports": [{"target": 7488, "published": "9000"}]},
                "frontend": {"ports": [{"target": 80, "published": "6600"}]},
            },
        }
        source_config = {
            "services": {
                "backend": {
                    "environment": resolved_source_environment,
                },
            },
        }
        return effective_config, _release_sandbox_structured_source_errors(source_config)

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
        compose_source_environment: dict[str, str] | list[str] | None = None,
        pre_migration: bool = False,
    ) -> dict:
        return validate_config(
            self.env_file,
            self.root / ".onto-runtime",
            compose_file=self.compose_file,
            pre_migration=pre_migration,
            compose_config_loader=_compose_loader(
                compose_environment,
                compose_source_environment,
            ),
        )

    def write_bound_script_ontology(self, workspace_id: str, ontology_id: str) -> None:
        scripts_dir = self.write_script_descriptor(
            workspace_id,
            ontology_id,
            "maintenance.md",
            "---\nresource: script\nkey: maintenance\n"
            "bound_operation: maintenance\nentrypoint: maintenance.py\n---\n",
        )
        (scripts_dir / "maintenance.py").write_text("print('ok')\n", encoding="utf-8")

    def write_script_descriptor(
        self,
        workspace_id: str,
        ontology_id: str,
        filename: str,
        content: str,
    ) -> Path:
        scripts_dir = (
            self.root
            / ".onto-runtime"
            / "workspaces"
            / workspace_id
            / "ontologies"
            / ontology_id
            / "versions"
            / "1.0.0"
            / "scripts"
        )
        scripts_dir.mkdir(parents=True, exist_ok=True)
        (scripts_dir / filename).write_text(content, encoding="utf-8")
        return scripts_dir

    def write_data_source(
        self,
        workspace_id: str,
        ontology_id: str,
        provider: str,
        config: dict,
    ) -> None:
        path = (
            self.root
            / ".onto-runtime"
            / "workspaces"
            / workspace_id
            / "ontology-data-sources.json"
        )
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps({
                "version": 2,
                "sources": {
                    ontology_id: {
                        "mode": "direct_provider",
                        "provider": provider,
                        "config": config,
                    },
                },
            }),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_compose_config_loader_uses_docker_compose_resolved_json(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        legacy_env_file = self.root / ".env"
        legacy_env_file.write_text(
            "BACKEND_PORT=5100\nFRONTEND_PORT=6600\n",
            encoding="utf-8",
        )
        calls = []

        def fake_run(command, **kwargs):
            calls.append((command, kwargs))
            if command[-1] == "version":
                return SimpleNamespace(returncode=0, stdout="Docker Compose v2", stderr="")
            environment = dict(SAFE_BACKEND_ENV)
            if "--no-interpolate" not in command:
                environment["SANDBOX_ENABLED"] = "false"
                environment["SANDBOX_FALLBACK_LOCAL"] = "true"
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({
                    "services": {
                        "backend": {
                            "environment": environment,
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
                (self.env_file, legacy_env_file),
            )

        self.assertEqual(errors, [])
        self.assertEqual(
            config["services"]["backend"]["environment"]["SANDBOX_ENABLED"],
            "false",
        )
        compose_command = calls[1][0]
        env_file_indexes = [
            index for index, value in enumerate(compose_command) if value == "--env-file"
        ]
        self.assertEqual(len(env_file_indexes), 2)
        self.assertEqual(
            compose_command[env_file_indexes[0] + 1],
            str(self.env_file.resolve()),
        )
        self.assertEqual(
            compose_command[env_file_indexes[1] + 1],
            str(legacy_env_file.resolve()),
        )
        self.assertIn("--format", compose_command)
        self.assertIn("json", compose_command)
        source_command = calls[2][0]
        self.assertIn("--no-interpolate", source_command)
        self.assertIn("--no-env-resolution", source_command)

    def test_release_host_ports_use_resolved_compose_mappings(self) -> None:
        config, _errors = _compose_loader()(self.compose_file, self.env_file)

        self.assertEqual(
            _release_host_ports(config),
            {"backend": 5100, "mcp": 9000, "frontend": 6600},
        )

    def test_compose_config_loader_rejects_nonliteral_structured_source(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")

        def fake_run(command, **kwargs):
            if command[-1] == "version":
                return SimpleNamespace(returncode=0, stdout="Docker Compose v2", stderr="")
            environment = dict(SAFE_BACKEND_ENV)
            if "--no-interpolate" in command:
                environment["SANDBOX_ENABLED"] = "${SANDBOX_ENABLED:-false}"
            return SimpleNamespace(
                returncode=0,
                stdout=json.dumps({"services": {"backend": {"environment": environment}}}),
                stderr="",
            )

        with patch("preflight_config.subprocess.run", fake_run):
            _config, errors = _load_compose_config_json(self.compose_file, self.env_file)

        self.assertIn(
            "unsupported_release_sandbox_setting_source",
            {item["type"] for item in errors},
        )

    def test_compose_v1_binary_is_not_used_for_release_preflight(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        calls = []

        def fake_run(command, **kwargs):
            calls.append(command)
            raise OSError("docker compose v2 unavailable")

        with patch("preflight_config.subprocess.run", fake_run):
            config, errors = _load_compose_config_json(self.compose_file, self.env_file)

        self.assertIsNone(config)
        self.assertEqual({item["type"] for item in errors}, {"compose_config_unavailable"})
        self.assertTrue(calls)
        self.assertTrue(all(command[:2] == ["docker", "compose"] for command in calls))

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

        report = self.validate(compose_source_environment=[
            "SANDBOX_ENABLED",
            "SANDBOX_FALLBACK_LOCAL=false",
            "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false",
        ])

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

        report = self.validate(compose_source_environment=[
            "SANDBOX_ENABLED=true",
            "SANDBOX_FALLBACK_LOCAL=${SANDBOX_FALLBACK_LOCAL}",
            "SANDBOX_SECRET_ALLOW_PLAIN_HTTP=false",
        ])

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

    def test_preflight_blocks_bound_scripts_using_unsupported_sandbox_provider(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "ontology-data-sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "version": 2,
                "sources": {"graph-maintenance": {
                    "mode": "direct_provider",
                    "provider": "neo4j",
                    "config": {
                        "uri": "bolt://neo4j:7687",
                        "username": "neo4j",
                        "password": "enc:v1:ciphertext",
                        "workspace_property": "workspace_id",
                        "ontology_property": "ontology_id",
                    },
                }},
            }),
            encoding="utf-8",
        )
        self.write_bound_script_ontology("demo", "graph-maintenance")

        report = self.validate(pre_migration=True)

        self.assertFalse(report["ok"])
        finding = next(
            item
            for item in report["errors"]
            if item["type"] == "unsupported_sandbox_script_provider"
        )
        self.assertEqual(finding["workspace_id"], "demo")
        self.assertEqual(finding["ontology_id"], "graph-maintenance")
        self.assertEqual(finding["provider"], "neo4j")
        self.assertEqual(report["sandbox_incompatible_script_provider_count"], 1)

    def test_preflight_allows_bound_scripts_using_supported_sandbox_provider(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "ontology-data-sources.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "version": 2,
                "sources": {"sql-maintenance": {
                    "mode": "direct_provider",
                    "provider": "nl2sql_http",
                    "config": {"url": "https://nl2sql.example"},
                }},
            }),
            encoding="utf-8",
        )
        self.write_bound_script_ontology("demo", "sql-maintenance")

        report = self.validate(pre_migration=True)

        self.assertTrue(report["ok"])
        self.assertEqual(report["sandbox_incompatible_script_provider_count"], 0)

    def test_preflight_blocks_nl2sql_http_token_over_plain_http(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.write_data_source(
            "demo",
            "sql-maintenance",
            "nl2sql_http",
            {
                "url": "http://nl2sql.internal",
                "token": "enc:v1:test-token",
            },
        )
        self.write_bound_script_ontology("demo", "sql-maintenance")

        report = self.validate(pre_migration=True)

        self.assertFalse(report["ok"])
        finding = next(
            item
            for item in report["errors"]
            if item["type"] == "sandbox_script_plain_http_secret"
        )
        self.assertEqual(finding["provider"], "nl2sql_http")
        self.assertEqual(finding["field"], "token")
        self.assertIn("HTTPS", finding["detail"])
        self.assertNotIn("test-token", json.dumps(report))

    def test_preflight_blocks_authenticated_mindsdb_over_plain_http(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        self.write_data_source(
            "demo",
            "mindsdb-maintenance",
            "mindsdb",
            {
                "url": "http://mindsdb.internal",
                "requires_auth": True,
                "password": "enc:v1:test-password",
            },
        )
        self.write_bound_script_ontology("demo", "mindsdb-maintenance")

        report = self.validate(pre_migration=True)

        self.assertFalse(report["ok"])
        finding = next(
            item
            for item in report["errors"]
            if item["type"] == "sandbox_script_plain_http_secret"
        )
        self.assertEqual(finding["provider"], "mindsdb")
        self.assertEqual(finding["field"], "password")
        self.assertIn("HTTPS", finding["detail"])
        self.assertNotIn("test-password", json.dumps(report))

    def test_preflight_rejects_supported_provider_configs_unusable_by_sandbox(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        cases = {
            "invalid-url": (
                "nl2sql_http",
                {"url": "", "token": ""},
                "invalid_sandbox_script_provider_url",
            ),
            "invalid-port": (
                "mindsdb",
                {
                    "url": "https://mindsdb.example:bad",
                    "requires_auth": True,
                    "password": "enc:v1:test-password",
                },
                "invalid_sandbox_script_provider_url",
            ),
            "out-of-range-port": (
                "nl2sql_http",
                {"url": "https://nl2sql.example:70000", "token": ""},
                "invalid_sandbox_script_provider_url",
            ),
            "malformed-ipv6": (
                "mindsdb",
                {
                    "url": "https://[::1",
                    "requires_auth": True,
                    "password": "enc:v1:test-password",
                },
                "invalid_sandbox_script_provider_url",
            ),
            "url-control-character": (
                "nl2sql_http",
                {"url": "https://nl2sql.example\n.invalid", "token": ""},
                "invalid_sandbox_script_provider_url",
            ),
            "missing-password": (
                "mindsdb",
                {"url": "https://mindsdb.example", "requires_auth": True},
                "missing_sandbox_script_provider_secret",
            ),
            "invalid-auth-flag": (
                "mindsdb",
                {"url": "https://mindsdb.example", "requires_auth": "sometimes"},
                "invalid_sandbox_script_provider_auth_flag",
            ),
        }
        for ontology_id, (provider, config, expected_type) in cases.items():
            with self.subTest(ontology_id=ontology_id):
                self.write_data_source("demo", ontology_id, provider, config)
                self.write_bound_script_ontology("demo", ontology_id)

                report = self.validate(pre_migration=True)

                self.assertFalse(report["ok"])
                self.assertIn(expected_type, {item["type"] for item in report["errors"]})

    def test_preflight_allows_plain_http_without_provider_credentials(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        cases = {
            "anonymous-nl2sql": (
                "nl2sql_http",
                {"url": "http://nl2sql.internal", "token": ""},
            ),
            "anonymous-mindsdb": (
                "mindsdb",
                {"url": "http://mindsdb.internal", "requires_auth": False},
            ),
        }
        for ontology_id, (provider, config) in cases.items():
            with self.subTest(ontology_id=ontology_id):
                self.write_data_source("demo", ontology_id, provider, config)
                self.write_bound_script_ontology("demo", ontology_id)

                report = self.validate(pre_migration=True)

                self.assertTrue(report["ok"])
                self.assertEqual(report["sandbox_incompatible_script_provider_count"], 0)

    def test_preflight_allows_legacy_false_mindsdb_auth_values_before_migration(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        for raw_value in ("false", "0", "off", "no"):
            with self.subTest(raw_value=raw_value):
                ontology_id = "legacy-false-{}".format(raw_value)
                self.write_data_source(
                    "demo",
                    ontology_id,
                    "mindsdb",
                    {
                        "url": "http://mindsdb.internal",
                        "requires_auth": raw_value,
                    },
                )
                self.write_bound_script_ontology("demo", ontology_id)

                report = self.validate(pre_migration=True)

                self.assertTrue(report["ok"])
                self.assertEqual(report["sandbox_incompatible_script_provider_count"], 0)

    def test_preflight_ignores_non_executable_markdown_content(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        workspace_id = "demo"
        cases = {
            "index-only": (
                "index.md",
                "---\nbound_operation: maintenance\nentrypoint: maintenance.py\n---\n",
            ),
            "body-example": (
                "example.md",
                "# Example\n\n```yaml\nbound_operation: maintenance\n"
                "entrypoint: maintenance.py\n```\n",
            ),
            "empty-binding": (
                "example.md",
                "---\nbound_operation: # optional\nentrypoint: maintenance.py\n---\n",
            ),
        }
        sources = {}
        for ontology_id, (filename, content) in cases.items():
            self.write_script_descriptor(
                workspace_id,
                ontology_id,
                filename,
                content,
            )
            sources[ontology_id] = {
                "mode": "direct_provider",
                "provider": "neo4j",
                "config": {"uri": "bolt://neo4j.internal:7687"},
            }
        source_path = (
            self.root
            / ".onto-runtime"
            / "workspaces"
            / workspace_id
            / "ontology-data-sources.json"
        )
        source_path.write_text(
            json.dumps({"version": 2, "sources": sources}),
            encoding="utf-8",
        )

        report = self.validate(pre_migration=True)

        self.assertTrue(report["ok"])
        self.assertEqual(report["sandbox_incompatible_script_provider_count"], 0)

    def test_preflight_script_detection_matches_runtime_catalog_semantics(self) -> None:
        cases = {
            "index.md": (
                "---\nbound_operation: maintenance\nentrypoint: maintenance.py\n---\n",
                False,
            ),
            "body.md": (
                "# Example\n\nbound_operation: maintenance\nentrypoint: maintenance.py\n",
                False,
            ),
            "empty.md": (
                "---\nbound_operation: # optional\nentrypoint: maintenance.py\n---\n",
                False,
            ),
            "valid.md": (
                "---\nbound_operation: maintenance\nentrypoint: maintenance.py\n---\n",
                True,
            ),
        }
        for filename, (content, expected) in cases.items():
            with self.subTest(filename=filename):
                ontology_root = self.root / "contract" / filename.replace(".md", "")
                scripts_dir = ontology_root / "scripts"
                scripts_dir.mkdir(parents=True)
                (scripts_dir / filename).write_text(content, encoding="utf-8")
                runtime_bound = any(
                    entry.get("_is_bound_operation")
                    for entry in load_script_catalog(ontology_root)
                )
                preflight_bound = _ontology_has_bound_python_script(ontology_root)
                self.assertEqual(runtime_bound, expected)
                self.assertEqual(preflight_bound, runtime_bound)

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
