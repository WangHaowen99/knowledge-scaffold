from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from preflight_config import validate_config  # noqa: E402


VALID_ENV = """\
AUTH_SERVER_URL=http://auth-server:5056
AUTH_SERVER_ADMIN_TOKEN=auth-admin-token-that-is-long
MCP_SERVER_PORT=7488
MCP_TOKEN_SECRET=mcp-signing-secret-that-is-independent-and-long
USER_MAX_COUNT=10
AUTH_USER_MAX_COUNT=10
"""


class PreflightConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.env_file = self.root / "backend" / ".env"
        self.env_file.parent.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_valid_config_without_graph_connections(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")

        report = validate_config(self.env_file, self.root / ".onto-runtime")

        self.assertTrue(report["ok"])
        self.assertEqual(report["graph_connection_count"], 0)
        self.assertEqual(report["auth"]["issuer"], "")
        self.assertEqual(report["auth"]["audience"], "")

    def test_rejects_divergent_limits_and_reused_secret(self) -> None:
        shared = "shared-secret-that-is-long-enough-for-both"
        self.env_file.write_text(
            VALID_ENV.replace("auth-admin-token-that-is-long", shared)
            .replace("mcp-signing-secret-that-is-independent-and-long", shared)
            .replace("AUTH_USER_MAX_COUNT=10", "AUTH_USER_MAX_COUNT=11"),
            encoding="utf-8",
        )

        report = validate_config(self.env_file, self.root / ".onto-runtime")

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertIn("mcp_secret_reuses_auth_admin_token", error_types)
        self.assertIn("user_limit_mismatch", error_types)

    def test_graph_config_requires_scope_and_encrypted_secret(self) -> None:
        self.env_file.write_text(VALID_ENV, encoding="utf-8")
        path = self.root / ".onto-runtime" / "workspaces" / "demo" / "data-connections.json"
        path.parent.mkdir(parents=True)
        path.write_text(
            json.dumps({
                "connections": [{
                    "id": "graph",
                    "provider": "neo4j",
                    "config": {
                        "uri": "bolt://neo4j:7687",
                        "username": "neo4j",
                        "password": "plaintext",
                    },
                }],
            }),
            encoding="utf-8",
        )

        report = validate_config(self.env_file, self.root / ".onto-runtime")

        self.assertFalse(report["ok"])
        error_types = {item["type"] for item in report["errors"]}
        self.assertEqual(error_types, {"plaintext_graph_secret", "missing_graph_scope_field"})
        self.assertEqual(report["warnings"][0]["field"], "ontology_property")


if __name__ == "__main__":
    unittest.main()
