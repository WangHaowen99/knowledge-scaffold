# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Internal pattern search tool adapter."""

from __future__ import annotations

from typing import Any

from ..backends.base import GraphBackend
from ..models import GraphBackendResult, GraphScope
from ..property_types import GraphFilterTypeResolver, normalize_pattern_filters


def pattern_search(
    *,
    backend: GraphBackend,
    path_pattern: list[Any],
    return_vars: list[str] | None = None,
    return_properties: Any | None = None,
    limit: int = 100,
    scope: GraphScope,
) -> GraphBackendResult:
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=scope,
        provider=getattr(backend, "provider", ""),
    )
    resolver.prepare_pattern_targets(
        path_pattern,
        return_properties=return_properties,
    )
    path_pattern = normalize_pattern_filters(
        path_pattern,
        resolver,
        return_properties=return_properties,
    )
    return backend.pattern_search(
        path_pattern=path_pattern,
        return_vars=return_vars,
        limit=limit,
        scope=scope,
        return_properties=return_properties,
    )
