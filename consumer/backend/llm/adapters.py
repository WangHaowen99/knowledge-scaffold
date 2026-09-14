# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Adapters for OpenAI SDK objects."""

from __future__ import annotations

from typing import Any


def to_plain_data(value: Any) -> Any:
    """Convert OpenAI SDK/Pydantic objects into plain Python containers."""
    if isinstance(value, dict):
        return {key: to_plain_data(item) for key, item in value.items()}
    if isinstance(value, list):
        return [to_plain_data(item) for item in value]
    if hasattr(value, "model_dump"):
        return to_plain_data(value.model_dump())
    if hasattr(value, "__dict__") and not isinstance(value, type):
        return {
            key: to_plain_data(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value
