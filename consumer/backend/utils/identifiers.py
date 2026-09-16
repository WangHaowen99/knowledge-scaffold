# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Validation and filesystem-safe keys for externally controlled identifiers."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Union


_SESSION_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_LEGACY_SAFE_USER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")
_USER_PREFIX_PATTERN = re.compile(r"[^a-z0-9._-]+")
SESSION_ID_MAX_LENGTH = 128


def validate_session_id(value: object) -> str:
    """Return a validated session id or raise ``ValueError``.

    Session ids are used as directory names by the persistence layer. Keeping
    the accepted alphabet small also makes identifiers portable across Linux,
    macOS, Windows, URLs, and protocol adapters.
    """
    if not isinstance(value, str):
        raise ValueError("session_id must be a string")

    session_id = value.strip()
    if not session_id:
        raise ValueError("session_id is required")
    if session_id != value:
        raise ValueError("session_id must not contain leading or trailing whitespace")
    if len(session_id) > SESSION_ID_MAX_LENGTH:
        raise ValueError("session_id exceeds {} characters".format(SESSION_ID_MAX_LENGTH))
    if not _SESSION_ID_PATTERN.fullmatch(session_id):
        raise ValueError(
            "session_id may contain only letters, numbers, '.', '_' and '-'"
        )
    if session_id in {".", ".."}:
        raise ValueError("session_id is invalid")
    return session_id


def resolve_session_storage_dir(base_dir: Union[str, Path], session_id: object) -> Path:
    """Resolve a session directory and prove it remains below ``base_dir``."""
    normalized = validate_session_id(session_id)
    base = Path(base_dir).resolve()
    candidate = (base / normalized).resolve()
    try:
        candidate.relative_to(base)
    except ValueError as exc:
        raise ValueError("session_id resolves outside the storage directory") from exc
    return candidate


def safe_user_storage_key(user_id: object) -> str:
    """Build a stable, collision-resistant directory key for a user id.

    Existing lowercase ASCII user directories keep their legacy names. Other
    identifiers receive a normalized display prefix plus a digest of the exact
    original id, so distinct usernames cannot collapse onto the same path.
    """
    raw = str(user_id or "user").strip() or "user"
    if _LEGACY_SAFE_USER_PATTERN.fullmatch(raw) and raw not in {".", ".."}:
        return raw

    prefix = _USER_PREFIX_PATTERN.sub("_", raw.lower()).strip("._-")
    prefix = (prefix or "user")[:40]
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    # ``~`` is outside the legacy-safe username alphabet, keeping the encoded
    # namespace disjoint from every username that retains its original path.
    return "~u-{}-{}".format(prefix, digest)
