# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Internal schema tools for graph providers."""

from __future__ import annotations

from ..backends.base import GraphBackend
from ..models import GraphScope


def get_nodes_schema(
    backend: GraphBackend,
    *,
    scope: GraphScope,
    element_types: list[str] | None = None,
) -> dict:
    return backend.get_nodes_schema(scope=scope, element_types=element_types)


def get_edges_schema(
    backend: GraphBackend,
    *,
    scope: GraphScope,
    relation_types: list[str] | None = None,
) -> dict:
    return backend.get_edges_schema(scope=scope, relation_types=relation_types)


def get_object_types(
    backend: GraphBackend,
    *,
    scope: GraphScope,
    element_types: list[str] | None = None,
) -> dict:
    """Compatibility alias for object-type schema discovery."""

    return get_nodes_schema(backend, scope=scope, element_types=element_types)


def get_relation_types(
    backend: GraphBackend,
    *,
    scope: GraphScope,
    relation_types: list[str] | None = None,
) -> dict:
    """Compatibility alias for relation-type schema discovery."""

    return get_edges_schema(backend, scope=scope, relation_types=relation_types)
