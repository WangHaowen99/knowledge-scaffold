# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Internal property search tool adapter."""

from __future__ import annotations

from typing import Any

from ..backends.base import GraphBackend
from ..models import GraphBackendResult, GraphScope
from ..property_types import GraphFilterTypeResolver


def property_filter(
    *,
    backend: GraphBackend,
    element_class: str,
    element_type: str,
    filter_dict: dict[str, Any] | None = None,
    return_properties: list[str] | None = None,
    limit: int = 100,
    offset: int = 0,
    scope: GraphScope,
) -> GraphBackendResult:
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=scope,
        provider=getattr(backend, "provider", ""),
    )
    normalized_filter = resolver.normalize_filter(
        filter_dict or {},
        element_type=element_type,
        element_class=element_class,
    ).filter_dict
    return backend.property_filter(
        element_class=element_class,
        element_type=element_type,
        filter_dict=normalized_filter,
        return_properties=return_properties,
        limit=limit,
        offset=offset,
        scope=scope,
    )
