"""Internal aggregate graph tool adapters."""

from __future__ import annotations

from typing import Any

from ..backends.base import GraphBackend
from ..models import GraphBackendResult, GraphScope


def count_search(
    *,
    backend: GraphBackend,
    element_class: str,
    element_type: str,
    filter_dict: dict[str, Any] | None = None,
    scope: GraphScope,
) -> GraphBackendResult:
    return backend.count_search(
        element_class=element_class,
        element_type=element_type,
        filter_dict=filter_dict,
        scope=scope,
    )


def aggregate_search(
    *,
    backend: GraphBackend,
    element_class: str,
    element_type: str,
    target_property: str,
    agg: str,
    filter_dict: dict[str, Any] | None = None,
    scope: GraphScope,
) -> GraphBackendResult:
    return backend.aggregate_search(
        element_class=element_class,
        element_type=element_type,
        target_property=target_property,
        agg=agg,
        filter_dict=filter_dict,
        scope=scope,
    )


def sorted_search(
    *,
    backend: GraphBackend,
    element_class: str,
    element_type: str,
    filter_dict: dict[str, Any] | None = None,
    return_properties: list[str] | None = None,
    sort_by: list[str] | None = None,
    ascending: bool = True,
    limit: int = 50,
    offset: int = 0,
    scope: GraphScope,
) -> GraphBackendResult:
    return backend.sorted_search(
        element_class=element_class,
        element_type=element_type,
        filter_dict=filter_dict,
        return_properties=return_properties,
        sort_by=sort_by,
        ascending=ascending,
        limit=limit,
        offset=offset,
        scope=scope,
    )
