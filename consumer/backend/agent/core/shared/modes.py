"""Shared agent mode normalization."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

VALID_AGENT_MODES = {"quick", "thinking", "multi_agent"}


def resolve_agent_mode(user_context: Any) -> str:
    ctx = dict(user_context or {}) if isinstance(user_context, Mapping) else {}
    raw_mode = str(ctx.get("agent_mode") or "").strip().lower()
    if raw_mode in VALID_AGENT_MODES:
        return raw_mode
    return "quick"
