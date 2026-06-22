"""Provider-aware data client router for ontology scripts."""

from __future__ import annotations

from typing import Any, Dict, Iterator, Optional

from ..business_data.script_env import load_script_business_data_connection
from .mindsdb import MindsDBClient, MindsDBError
from .nl2sql import NL2SQLClient, NL2SQLError


class DataProviderError(RuntimeError):
    """Raised when data provider operations fail."""


class DataProviderClient:
    """Thin provider router used by generated ontology scripts."""

    def __init__(self, config: Optional[dict] = None):
        connection = _resolve_connection(config)
        self.provider = connection["provider"]
        self.config = connection["config"]
        self._client = _create_provider_client(self.provider, self.config)

    def query_sql(self, sql: str) -> Any:
        return self._client.query_sql(sql)

    def query(self, question: str) -> Dict[str, Any]:
        return self._client.query(question)

    def query_agent_stream(self, question: str) -> Iterator[Dict[str, str]]:
        if not hasattr(self._client, "query_agent_stream"):
            raise DataProviderError(
                "provider '{}' does not support streaming agent queries".format(self.provider)
            )
        yield from self._client.query_agent_stream(question)

    def close(self) -> None:
        close = getattr(self._client, "close", None)
        if callable(close):
            close()


def _resolve_connection(config: Optional[dict]) -> Dict[str, Any]:
    if config:
        provider = str(config.get("provider") or "").strip().lower()
        provider_config = dict(config.get("config") or {})
        if provider:
            return {"provider": provider, "config": provider_config}
        raise DataProviderError("business data connection provider is not configured")

    script_connection = load_script_business_data_connection()
    if script_connection:
        provider = str(script_connection.get("provider") or "").strip().lower()
        if provider:
            return {
                "provider": provider,
                "config": dict(script_connection.get("config") or {}),
            }
        raise DataProviderError("business data connection provider is not configured")

    raise DataProviderError(
        "business data connection is not configured; bind the ontology to a workspace data connection"
    )


def _create_provider_client(provider: str, config: Dict[str, Any]) -> Any:
    if provider == "mindsdb":
        return MindsDBClient({"ontology_search": {"mindsdb": dict(config)}})
    if provider == "nl2sql_http":
        return NL2SQLClient({"ontology_search": {"nl2sql_http": dict(config)}})
    if provider == "nl2sql_template":
        raise DataProviderError("provider 'nl2sql_template' does not support direct SQL execution")
    raise DataProviderError("unsupported data provider '{}'".format(provider or "未配置"))


__all__ = [
    "DataProviderClient",
    "DataProviderError",
    "MindsDBError",
    "NL2SQLError",
]
