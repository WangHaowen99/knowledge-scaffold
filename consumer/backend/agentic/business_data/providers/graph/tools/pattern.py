"""Internal pattern search tool adapter."""

from __future__ import annotations

from ..backends.base import GraphBackend
from typing import Any

from ..models import GraphBackendResult, GraphScope


def pattern_search(
    *,
    backend: GraphBackend,
    path_pattern: list[Any],
    return_vars: list[str] | None = None,
    return_properties: Any | None = None,
    limit: int = 100,
    scope: GraphScope,
) -> GraphBackendResult:
    return backend.pattern_search(
        path_pattern=path_pattern,
        return_vars=return_vars,
        limit=limit,
        scope=scope,
        return_properties=return_properties,
    )
