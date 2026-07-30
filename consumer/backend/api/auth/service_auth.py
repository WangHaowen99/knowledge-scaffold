"""Authentication helpers for narrowly scoped service-to-service APIs."""

from __future__ import annotations

import os
import secrets

from flask import Request


def modeling_service_authorized(request: Request) -> bool:
    expected = str(os.getenv("MODELING_SERVICE_TOKEN") or "").strip()
    if len(expected) < 32:
        return False
    header = str(request.headers.get("Authorization") or "")
    supplied = header[7:].strip() if header.startswith("Bearer ") else ""
    return bool(supplied) and secrets.compare_digest(supplied, expected)
