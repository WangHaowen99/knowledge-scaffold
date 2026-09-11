"""Process-local synthetic credentials for solver TCK tests."""

from __future__ import annotations

import secrets


CONTROL_TOKEN = secrets.token_urlsafe(32)
SERVICE_TOKEN = secrets.token_urlsafe(32)
