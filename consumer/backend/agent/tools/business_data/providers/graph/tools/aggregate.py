# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Internal aggregate graph tool adapters."""

from __future__ import annotations

from typing import Any

from ..backends.base import GraphBackend
from ..models import GraphBackendResult, GraphScope
from ..property_types import GraphFilterTypeResolver


def _normalize_filter(backend, *, element_class: str, element_type: str, filter_dict, scope: GraphScope):
    resolver = GraphFilterTypeResolver(
        backend=backend,
        scope=scope,
        provider=getattr(backend, "provider", ""),
    )
    return resolver.normalize_filter(
        filter_dict or {},
        element_type=element_type,
        element_class=element_class,
    ).filter_dict


def count_search(
    *,
    backend: GraphBackend,
    element_class: str,
    element_type: str,
    filter_dict: dict[str, Any] | None = None,
    scope: GraphScope,
) -> GraphBackendResult:
    filter_dict = _normalize_filter(
        backend,
        element_class=element_class,
        element_type=element_type,
        filter_dict=filter_dict,
        scope=scope,
    )
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
    external_filter = dict(filter_dict or {})
    normalized_filter = _normalize_filter(
        backend,
        element_class=element_class,
        element_type=element_type,
        filter_dict=external_filter,
        scope=scope,
    )
    result = backend.aggregate_search(
        element_class=element_class,
        element_type=element_type,
        target_property=target_property,
        agg=agg,
        filter_dict=normalized_filter,
        scope=scope,
    )
    records = [
        _with_external_filter(record, external_filter)
        for record in result.records or []
    ]
    payload = dict(result.payload or {})
    payload_result = payload.get("result")
    if isinstance(payload_result, dict):
        payload["result"] = _with_external_filter(payload_result, external_filter)
    return GraphBackendResult(
        records=records,
        total=result.total,
        truncated=result.truncated,
        warnings=list(result.warnings or []),
        payload=payload,
    )


def _with_external_filter(
    record: dict[str, Any],
    external_filter: dict[str, Any],
) -> dict[str, Any]:
    copied = dict(record)
    if "applied_filter" in copied:
        copied["applied_filter"] = dict(external_filter)
    if "filter_keys" in copied:
        copied["filter_keys"] = list(external_filter)
    return copied


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
    filter_dict = _normalize_filter(
        backend,
        element_class=element_class,
        element_type=element_type,
        filter_dict=filter_dict,
        scope=scope,
    )
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
