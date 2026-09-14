# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared helpers for memory manager backends."""

from __future__ import annotations

from datetime import datetime
import json
import re
from typing import Any, Dict, Optional, Tuple

from agent.memory.models import MemoryHit


_UNSAFE_NAME_CHARS = re.compile(r'[\x00-\x1f/\\:*?"<>|]+')


def safe_name(value: Optional[str]) -> str:
    """Filesystem/URI-safe identifier that preserves CJK and other unicode characters.

    Strips only path separators, control characters, and Windows-reserved chars.
    Collapses whitespace runs to a single underscore. Returns 'default' for empty input.
    """
    raw = str(value or "").strip()
    if not raw:
        return "default"
    sanitized = _UNSAFE_NAME_CHARS.sub("_", raw)
    sanitized = re.sub(r"\s+", "_", sanitized)
    sanitized = sanitized.strip("._")
    return sanitized or "default"


def serialize_plain_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def utc_now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def render_memory_note(
    *,
    user_id: str,
    content: str,
    memory_type: str,
    scope: str,
    project_key: Optional[str],
    app_session_id: Optional[str],
    source: str,
    hit_count: int = 0,
    archived: bool = False,
    created_at: Optional[str] = None,
    updated_at: Optional[str] = None,
) -> str:
    del project_key, app_session_id
    now = utc_now_iso()
    created_at = str(created_at or now)
    updated_at = str(updated_at or created_at)
    metadata = {
        "memory_type": memory_type,
        "scope": scope,
        "hit_count": int(hit_count),
        "archived": bool(archived),
        "source": source,
        "stored_at": created_at,
        "created_at": created_at,
        "updated_at": updated_at,
        "user_id": user_id,
    }
    lines = ["---"]
    for key, value in metadata.items():
        lines.append("{}: {}".format(key, json.dumps(value, ensure_ascii=False)))
    lines.extend(["---", "", content.strip(), ""])
    return "\n".join(lines)


def parse_frontmatter(text: str) -> Tuple[Dict[str, Any], str]:
    normalized = str(text or "")
    if not normalized.startswith("---\n"):
        return {}, normalized.strip()
    parts = normalized.split("\n---\n", 1)
    if len(parts) != 2:
        return {}, normalized.strip()
    raw_meta = parts[0].splitlines()[1:]
    body = parts[1].strip()
    metadata: Dict[str, Any] = {}
    for line in raw_meta:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        raw_value = value.strip()
        try:
            metadata[key] = json.loads(raw_value)
        except Exception:
            metadata[key] = raw_value.strip('"')
    return metadata, body


def memory_priority(hit: MemoryHit) -> int:
    """Lower values rank higher for prompt injection."""
    if hit.memory_type == "preference":
        return 0
    if hit.memory_type == "profile":
        return 1
    return 2
