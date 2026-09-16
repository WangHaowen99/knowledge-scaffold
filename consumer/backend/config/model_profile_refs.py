# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Scoped model profile reference helpers."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal


ModelProfileScope = Literal["global", "workspace"]


@dataclass(frozen=True)
class ModelProfileRef:
    scope: ModelProfileScope
    id: str


def _normalize_ref_id(value: Any) -> str:
    ref_id = str(value or "").strip()
    if not ref_id:
        raise ValueError("model profile id 不能为空")
    return ref_id


def _normalize_scope(value: Any) -> ModelProfileScope:
    scope = str(value or "").strip().lower()
    if scope not in {"global", "workspace"}:
        raise ValueError("model profile scope 仅支持 global | workspace")
    return scope  # type: ignore[return-value]


def _parsed_model_profile_ref(scope: Any, ref_id: Any) -> ModelProfileRef:
    if not isinstance(scope, str) or not isinstance(ref_id, str):
        raise ValueError("model profile reference scope and id must be strings")
    return ModelProfileRef(
        scope=_normalize_scope(scope),
        id=_normalize_ref_id(ref_id),
    )


def parse_model_profile_ref(
    value: str | Mapping[str, Any] | ModelProfileRef | None,
) -> ModelProfileRef | None:
    """Parse legacy bare ids and new scoped ids."""
    if value is None:
        return None
    if isinstance(value, ModelProfileRef):
        return _parsed_model_profile_ref(value.scope, value.id)
    if isinstance(value, Mapping):
        keys = tuple(value.keys())
        if any(not isinstance(key, str) for key in keys) or set(keys) != {
            "scope",
            "id",
        }:
            raise ValueError("model profile reference must contain only scope and id")
        return _parsed_model_profile_ref(value["scope"], value["id"])
    if not isinstance(value, str):
        raise ValueError("model profile reference must be a string or object")

    raw = value.strip()
    if not raw:
        return None
    for scoped_prefix in ("global:", "workspace:"):
        if raw.startswith(scoped_prefix):
            scope, ref_id = raw.split(":", 1)
            return ModelProfileRef(scope=_normalize_scope(scope), id=_normalize_ref_id(ref_id))

    return ModelProfileRef(scope="global", id=_normalize_ref_id(raw))


def encode_model_profile_ref(ref: ModelProfileRef | Mapping[str, Any] | str) -> str:
    """Return global:<id> or workspace:<id>."""
    parsed = parse_model_profile_ref(ref)
    if parsed is None:
        return ""
    return f"{parsed.scope}:{parsed.id}"


def model_profile_ref_to_dict(
    ref: ModelProfileRef | Mapping[str, Any] | str | None,
) -> dict[str, str] | None:
    parsed = parse_model_profile_ref(ref)
    if parsed is None:
        return None
    return {"scope": parsed.scope, "id": parsed.id}
