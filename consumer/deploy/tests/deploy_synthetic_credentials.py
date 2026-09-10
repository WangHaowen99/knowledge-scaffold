"""Process-local synthetic credentials for consumer deployment tests."""

from __future__ import annotations

import secrets

AUTH_ADMIN_TOKEN = secrets.token_urlsafe(32)
BOOTSTRAP_ADMIN_PASSWORD = f"Aa1!{secrets.token_urlsafe(18)}"
MCP_TOKEN_SECRET = secrets.token_urlsafe(32)
SHARED_SECRET = secrets.token_urlsafe(32)
PLAINTEXT_GRAPH_SECRET = secrets.token_urlsafe(24)


def valid_environment() -> str:
    """Build a complete synthetic environment file for preflight checks."""
    return (
        "AUTH_SERVER_URL=https://service.example.net\n"
        f"AUTH_SERVER_ADMIN_TOKEN={AUTH_ADMIN_TOKEN}\n"
        f"AUTH_BOOTSTRAP_ADMIN_PASSWORD={BOOTSTRAP_ADMIN_PASSWORD}\n"
        "MCP_SERVER_PORT=7488\n"
        f"MCP_TOKEN_SECRET={MCP_TOKEN_SECRET}\n"
        "USER_MAX_COUNT=10\n"
        "AUTH_USER_MAX_COUNT=10\n"
    )
