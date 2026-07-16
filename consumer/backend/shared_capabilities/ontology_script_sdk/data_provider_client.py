"""Parameterless business-data facade for local and sandbox scripts."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from shared_capabilities.business_data import (
    ProviderEvent,
    ProviderResult,
    create_provider_client,
    iter_query_events,
    require_capability,
)

from .connection_env import load_effective_connection


class DataProviderClient:
    def __init__(self) -> None:
        self.connection = load_effective_connection()
        self.provider = self.connection.provider
        self._client = create_provider_client(self.connection)

    def query(self, question: str) -> ProviderResult:
        self._require("query")
        return self._client.query(question)

    def query_sql(self, sql: str) -> ProviderResult:
        self._require("query_sql")
        return self._client.query_sql(sql)

    def iter_query_events(self, question: str) -> Iterator[ProviderEvent]:
        self._require("iter_query_events")
        yield from iter_query_events(self._client, question)

    def probe(self, timeout_seconds: float | None = None) -> ProviderResult:
        self._require("probe")
        return self._client.probe(timeout_seconds)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()

    def __enter__(self) -> "DataProviderClient":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        del exc_type, exc, traceback
        self.close()

    def _require(self, operation: str) -> None:
        require_capability(self._client.capabilities, operation)
