# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Helpers for controlling request_user_interaction availability."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


INTERACTION_CAPABILITY_KEY = "supports_user_interaction"

_TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
_FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def normalize_user_interaction_capability(value: Any, *, default: bool = True) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in _TRUE_VALUES:
            return True
        if normalized in _FALSE_VALUES:
            return False
    if isinstance(value, (int, float)) and not isinstance(value, bool) and value in (0, 1):
        return bool(value)
    return default


def user_interaction_supported_from_context(
    context: Mapping[str, Any] | None,
    *,
    default: bool = True,
) -> bool:
    if not isinstance(context, Mapping):
        return default
    return normalize_user_interaction_capability(
        context.get(INTERACTION_CAPABILITY_KEY),
        default=default,
    )


def user_interaction_supported_from_state(
    state: Mapping[str, Any] | None,
    *,
    default: bool = True,
) -> bool:
    if not isinstance(state, Mapping):
        return default
    context = state.get("user_context")
    return user_interaction_supported_from_context(
        context if isinstance(context, Mapping) else None,
        default=default,
    )


def set_user_interaction_capability(
    context: MutableMapping[str, Any] | None,
    *,
    supported: bool,
) -> MutableMapping[str, Any]:
    next_context = dict(context or {})
    next_context[INTERACTION_CAPABILITY_KEY] = bool(supported)
    return next_context
