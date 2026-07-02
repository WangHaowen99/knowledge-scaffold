"""Internal property search tool adapter."""

from __future__ import annotations

from ..backends.base import GraphBackend
from typing import Any

from ..models import GraphBackendResult, GraphScope


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
    return backend.property_filter(
        element_class=element_class,
        element_type=element_type,
        filter_dict=filter_dict,
        return_properties=return_properties,
        limit=limit,
        offset=offset,
        scope=scope,
    )
