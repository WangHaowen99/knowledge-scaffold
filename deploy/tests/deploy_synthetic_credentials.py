"""Process-local synthetic credentials for deployment tests."""

from __future__ import annotations

import secrets

AUTH_ADMIN_TOKEN = secrets.token_urlsafe(32)
BOOTSTRAP_ADMIN_SECRET = f"Aa1!{secrets.token_urlsafe(18)}"
