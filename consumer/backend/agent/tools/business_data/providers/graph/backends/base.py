"""Graph backend protocol and errors."""

from __future__ import annotations

from typing import Any, Protocol

from ..models import GraphBackendResult, GraphScope


class GraphBackendError(RuntimeError):
    """Raised when a graph backend query fails."""

    error_code = "GRAPH_BACKEND_ERROR"
    retryable = True


class GraphBackendTimeoutError(GraphBackendError):
    """Raised when a graph backend query exceeds its configured timeout."""

    error_code = "GRAPH_BACKEND_TIMEOUT"
    retryable = True


class GraphBackendUnsupportedError(GraphBackendError):
    """Raised when a graph backend does not support a requested operation."""

    error_code = "UNSUPPORTED_GRAPH_BACKEND"
    retryable = False


class GraphBackend(Protocol):
    def get_nodes_schema(
        self,
        *,
        scope: GraphScope,
        element_types: list[str] | None = None,
    ) -> dict:
        ...

    def get_edges_schema(
        self,
        *,
        scope: GraphScope,
        relation_types: list[str] | None = None,
    ) -> dict:
        ...

    def property_filter(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        return_properties: list[str] | None,
        limit: int,
        offset: int,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def count_search(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def aggregate_search(
        self,
        *,
        element_class: str,
        element_type: str,
        target_property: str,
        agg: str,
        filter_dict: dict[str, Any] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def sorted_search(
        self,
        *,
        element_class: str,
        element_type: str,
        filter_dict: dict[str, Any] | None,
        return_properties: list[str] | None,
        sort_by: list[str] | None,
        ascending: bool,
        limit: int,
        offset: int,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def pattern_search(
        self,
        *,
        path_pattern: list[Any],
        return_vars: list[str] | None,
        limit: int,
        scope: GraphScope,
        return_properties: Any | None = None,
    ) -> GraphBackendResult:
        ...

    def hop_search(
        self,
        *,
        entity_instance_id: str,
        hop_num: int,
        accurate_flag: bool,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def shortest_path_search(
        self,
        *,
        start_label: str,
        start_props: dict[str, Any],
        end_label: str,
        end_props: dict[str, Any],
        max_depth: int,
        directed: bool,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def list_logic_titles(
        self,
        *,
        entity_type: str = "",
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def get_logic_content(
        self,
        *,
        entity_instance_ids: list[str],
        logic_titles: list[str] | None,
        scope: GraphScope,
    ) -> GraphBackendResult:
        ...

    def get_element_info(
        self,
        *,
        element_instance_id: str,
        element_class: str = "",
        element_type: str = "",
        scope: GraphScope,
    ) -> dict:
        ...

    def close(self) -> None:
        ...
