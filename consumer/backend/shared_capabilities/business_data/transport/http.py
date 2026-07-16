"""HTTP lifecycle, retry, and safe error helpers."""

from __future__ import annotations

from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry


def create_retry_session(max_retries: int) -> requests.Session:
    retry = Retry(
        total=max(0, int(max_retries)),
        backoff_factor=0.5,
        status_forcelist=(500, 502, 503, 504),
        allowed_methods=frozenset({"GET", "POST"}),
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def extract_error_detail(response: Any) -> str:
    try:
        payload = response.json()
    except (ValueError, TypeError):
        payload = None
    detail: object = ""
    if isinstance(payload, dict):
        detail = payload.get("detail") or payload.get("error") or payload.get("message") or ""
        if isinstance(detail, dict):
            detail = detail.get("message") or ""
    if not detail:
        detail = str(getattr(response, "text", "") or "").strip()
    return str(detail or "Unknown error")[:200]


def sanitize_public_text(text: object, *sensitive_values: object) -> str:
    sanitized = str(text or "")
    for value in sensitive_values:
        secret = str(value or "").strip()
        if secret:
            sanitized = sanitized.replace(secret, "[REDACTED]")
    return sanitized

