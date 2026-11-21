# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared helpers for the Jiuwen runtime."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any, Dict, Optional


class JiuwenBackendUnavailable(RuntimeError):
    """Raised when the openJiuwen runtime dependency is unavailable."""


def model_safe_tool_call_id(value: str) -> str:
    """Keep paired call IDs stable within the model API's 64-character limit."""
    if len(value) <= 64:
        return value
    return "call_" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:56]


def format_jiuwen_import_error(exc: ImportError) -> str:
    missing = getattr(exc, "name", None)
    detail = " Missing module: {}.".format(missing) if missing else " {}".format(str(exc))
    return (
        "Jiuwen ReAct runtime requires openJiuwen dependencies. "
        "Install backend dependencies from backend/requirements.txt before running the main agent loop."
        "{}".format(detail)
    )


def read_field(payload: Any, name: str, default: Optional[Any] = None) -> Any:
    if isinstance(payload, Mapping):
        return payload.get(name, default)
    return getattr(payload, name, default)


def to_mapping(payload: Any) -> Dict[str, Any]:
    if isinstance(payload, Mapping):
        return dict(payload)
    model_dump = getattr(payload, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    return {}


def object_to_dict(value: Any) -> Dict[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return dict(value)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        return dict(dumped) if isinstance(dumped, Mapping) else {}
    if hasattr(value, "__dict__"):
        return {
            key: item
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return {}


def coerce_optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def normalize_arguments(arguments: Any) -> str:
    if arguments is None:
        return "{}"
    if isinstance(arguments, str):
        return arguments
    return json.dumps(arguments, ensure_ascii=False, separators=(",", ":"))
