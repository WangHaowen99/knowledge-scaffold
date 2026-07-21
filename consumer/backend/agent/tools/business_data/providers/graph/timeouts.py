"""Timeout policy for graph providers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


DEFAULT_GRAPH_CONNECT_TIMEOUT_SECONDS = 10.0
DEFAULT_GRAPH_QUERY_TIMEOUT_SECONDS = 30.0
DEFAULT_GRAPH_AGENT_TIMEOUT_SECONDS = 120.0


def resolve_graph_agent_timeout_seconds(provider_config: Mapping[str, Any]) -> float:
    return _positive_seconds(
        provider_config.get("graph_agent_timeout_seconds"),
        default=DEFAULT_GRAPH_AGENT_TIMEOUT_SECONDS,
    )


def resolve_graph_query_timeout_seconds(provider_config: Mapping[str, Any]) -> float:
    configured = provider_config.get("graph_query_timeout_seconds")
    if configured not in (None, ""):
        return _positive_seconds(configured, default=DEFAULT_GRAPH_QUERY_TIMEOUT_SECONDS)

    # Legacy timeout_seconds covered the whole provider. Keep accepting the
    # field, but do not let persisted legacy values widen the query deadline.
    legacy_timeout = _positive_seconds(
        provider_config.get("timeout_seconds"),
        default=DEFAULT_GRAPH_QUERY_TIMEOUT_SECONDS,
    )
    return min(legacy_timeout, DEFAULT_GRAPH_QUERY_TIMEOUT_SECONDS)


def resolve_graph_connect_timeout_seconds(
    provider_config: Mapping[str, Any],
    *,
    query_timeout_seconds: float | None = None,
) -> float:
    configured = provider_config.get("graph_connect_timeout_seconds")
    if configured not in (None, ""):
        return _positive_seconds(configured, default=DEFAULT_GRAPH_CONNECT_TIMEOUT_SECONDS)

    query_timeout = query_timeout_seconds
    if query_timeout is None:
        query_timeout = resolve_graph_query_timeout_seconds(provider_config)
    return min(DEFAULT_GRAPH_CONNECT_TIMEOUT_SECONDS, max(0.001, float(query_timeout)))


def _positive_seconds(value: Any, *, default: float) -> float:
    try:
        seconds = float(value if value not in (None, "") else default)
    except (TypeError, ValueError):
        seconds = default
    if seconds <= 0:
        seconds = default
    return max(0.001, seconds)
