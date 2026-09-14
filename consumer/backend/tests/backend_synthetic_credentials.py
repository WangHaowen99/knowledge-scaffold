# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Runtime-generated credentials shared by backend tests.

The values in this module are synthetic, process-local, and never sourced from
an external environment. Tests import them instead of embedding credential-like
literals throughout fixtures and request payloads.
"""

from __future__ import annotations

import secrets
from base64 import b64encode
from functools import lru_cache


@lru_cache(maxsize=None)
def synthetic_secret(label: str) -> str:
    """Return one stable synthetic secret for a named test case."""
    del label
    return secrets.token_urlsafe(32)


@lru_cache(maxsize=None)
def synthetic_password(label: str) -> str:
    """Return one stable, policy-compliant synthetic password for a test case."""
    del label
    return f"Aa1!{secrets.token_urlsafe(18)}"


@lru_cache(maxsize=None)
def synthetic_invalid_password(label: str) -> str:
    """Return a stable value that intentionally fails password policy checks."""
    del label
    return secrets.token_hex(2)


@lru_cache(maxsize=None)
def synthetic_weak_secret(label: str) -> str:
    """Return a stable value for tests that require a deliberately short secret."""
    del label
    return secrets.token_hex(2)


AUTH_SIGNING_SECRET = synthetic_secret("auth-signing")
MCP_TOKEN_SECRET = synthetic_secret("mcp-token")
ADMIN_API_TOKEN = synthetic_secret("admin-api")
API_KEY = synthetic_secret("api-key")
ACCESS_KEY_ID = synthetic_secret("access-key-id")
ACCESS_KEY_SECRET = synthetic_secret("access-key-secret")
ENCRYPTION_KEY = b64encode(secrets.token_bytes(32)).decode("ascii")
BOOTSTRAP_ADMIN_PASSWORD = synthetic_password("bootstrap-admin")
STRONG_PASSWORD = synthetic_password("strong-1")
STRONG_PASSWORD_2 = synthetic_password("strong-2")
STRONG_PASSWORD_3 = synthetic_password("strong-3")
INVALID_PASSWORD = synthetic_invalid_password("invalid")
