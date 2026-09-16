# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Credential-safe parsing helpers for business-data service endpoints."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qsl, unquote, urlencode, urlsplit


_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
    }
)
_SENSITIVE_QUERY_SUFFIXES = (
    "_credential",
    "_key",
    "_password",
    "_secret",
    "_signature",
    "_token",
)


def _normalized_query_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().casefold()).strip("_")


def is_sensitive_endpoint_query_key(value: Any) -> bool:
    normalized = _normalized_query_key(value)
    compact = normalized.replace("_", "")
    return (
        normalized in _SENSITIVE_QUERY_KEYS
        or compact in _SENSITIVE_QUERY_KEYS
        or any(normalized.endswith(suffix) for suffix in _SENSITIVE_QUERY_SUFFIXES)
        or compact.endswith(("credential", "password", "secret", "signature", "token"))
    )


def endpoint_has_forbidden_auth_material(value: Any) -> bool:
    """Return whether an endpoint embeds credentials or a URI fragment."""
    endpoint = str(value or "").strip()
    if not endpoint:
        return False
    # ``urlsplit("scheme:user:pass@host")`` treats everything after the first
    # colon as a path, leaving username/password/netloc empty. Endpoints do not
    # have a legitimate use for a raw user-info delimiter or fragment marker,
    # so reject them before relying on the parsed authority.
    if "@" in endpoint or "#" in endpoint:
        return True
    try:
        parsed = urlsplit(endpoint)
        if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
            return True
        if parsed.fragment:
            return True
        return any(
            is_sensitive_endpoint_query_key(key)
            for key, _value in parse_qsl(parsed.query, keep_blank_values=True)
        )
    except ValueError:
        return "@" in endpoint or "#" in endpoint


def publicize_endpoint(value: Any) -> Any:
    """Remove embedded credentials from legacy endpoint values."""
    endpoint = str(value or "")
    if not endpoint_has_forbidden_auth_material(endpoint):
        return value
    try:
        parsed = urlsplit(endpoint)
        if "@" in endpoint and "@" not in parsed.netloc:
            return "[redacted-endpoint]"
        netloc = parsed.netloc.rsplit("@", 1)[-1] if "@" in parsed.netloc else parsed.netloc
        safe_query = urlencode(
            [
                (key, item)
                for key, item in parse_qsl(parsed.query, keep_blank_values=True)
                if not is_sensitive_endpoint_query_key(key)
            ],
            doseq=True,
        )
        return parsed._replace(netloc=netloc, query=safe_query, fragment="").geturl()
    except ValueError:
        return "[redacted-endpoint]"


def endpoint_secret_values(value: Any) -> tuple[str, ...]:
    """Extract credential literals from legacy or injected endpoint values."""
    endpoint = str(value or "")
    if not endpoint:
        return ()
    values: set[str] = set()
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        if "@" in endpoint or "#" in endpoint:
            values.add(endpoint)
        return tuple(values)

    forbidden = False
    if parsed.username is not None or parsed.password is not None or "@" in parsed.netloc:
        forbidden = True
        for candidate in (parsed.username, parsed.password):
            normalized = str(candidate or "")
            if normalized:
                values.add(normalized)
                values.add(unquote(normalized))
    elif "@" in endpoint:
        # Preserve enough literals to redact legacy schemeless endpoint errors
        # without attempting to reinterpret the malformed value as a valid URI.
        forbidden = True
        userinfo = endpoint.rsplit("@", 1)[0]
        for candidate in (userinfo, userinfo.rsplit(":", 1)[-1]):
            normalized = str(candidate or "")
            if normalized:
                values.add(normalized)
                values.add(unquote(normalized))
    for key, candidate in parse_qsl(parsed.query, keep_blank_values=True):
        if not is_sensitive_endpoint_query_key(key):
            continue
        forbidden = True
        normalized = str(candidate or "")
        if normalized:
            values.add(normalized)
            values.add(unquote(normalized))
    if parsed.fragment:
        forbidden = True
        values.add(parsed.fragment)
        values.add(unquote(parsed.fragment))
    if forbidden:
        values.add(endpoint)
    return tuple(sorted((item for item in values if item), key=lambda item: (-len(item), item)))
