"""Process-local synthetic credentials for auth server tests."""

from __future__ import annotations

import secrets
from functools import lru_cache

from auth_server.policy import _WEAK_PASSWORDS


@lru_cache(maxsize=None)
def synthetic_secret(label: str) -> str:
    """Return one stable synthetic secret for a named test case."""
    del label
    return secrets.token_urlsafe(32)


@lru_cache(maxsize=None)
def synthetic_password(label: str) -> str:
    """Return one stable, policy-compliant synthetic password."""
    del label
    return f"Aa1!{secrets.token_urlsafe(18)}"


def username_password_violation(username: str) -> str:
    """Build a synthetic password that violates whitespace and username rules."""
    return f" {username} "


ADMIN_TOKEN = synthetic_secret("admin-token")
BOOTSTRAP_ADMIN_PASSWORD = synthetic_password("bootstrap-admin")
STRONG_PASSWORD = synthetic_password("strong-1")
NEW_STRONG_PASSWORD = synthetic_password("strong-2")
LEGACY_PASSWORD = synthetic_password("legacy")
INVALID_TOKEN = synthetic_secret("invalid-token")
INVALID_PASSWORD = secrets.token_hex(2)
COMMON_PASSWORD = max(_WEAK_PASSWORDS, key=len).capitalize()
