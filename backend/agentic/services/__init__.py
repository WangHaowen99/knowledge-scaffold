"""Agentic services module."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .llm import LLMPurpose as LLMPurpose
    from .llm import LLMResponse as LLMResponse
    from .llm import LLMRouter as LLMRouter
    from .llm import LLMService as LLMService
    from .mindsdb import MindsDBClient as MindsDBClient
    from .mindsdb import MindsDBConfig as MindsDBConfig
    from .mindsdb import MindsDBError as MindsDBError
    from .data_provider_client import DataProviderClient as DataProviderClient
    from .data_provider_client import DataProviderError as DataProviderError
    from .nl2sql import NL2SQLClient as NL2SQLClient
    from .nl2sql import NL2SQLConfig as NL2SQLConfig
    from .nl2sql import NL2SQLError as NL2SQLError


def __getattr__(name: str) -> Any:
    if name in {"LLMPurpose", "LLMService", "LLMResponse", "LLMRouter"}:
        from .llm import LLMPurpose, LLMResponse, LLMRouter, LLMService

        return {
            "LLMPurpose": LLMPurpose,
            "LLMService": LLMService,
            "LLMResponse": LLMResponse,
            "LLMRouter": LLMRouter,
        }[name]
    if name in {"MindsDBClient", "MindsDBConfig", "MindsDBError"}:
        from .mindsdb import MindsDBClient, MindsDBConfig, MindsDBError

        return {
            "MindsDBClient": MindsDBClient,
            "MindsDBConfig": MindsDBConfig,
            "MindsDBError": MindsDBError,
        }[name]
    if name in {"DataProviderClient", "DataProviderError"}:
        from .data_provider_client import DataProviderClient, DataProviderError

        return {
            "DataProviderClient": DataProviderClient,
            "DataProviderError": DataProviderError,
        }[name]
    if name in {"NL2SQLClient", "NL2SQLConfig", "NL2SQLError"}:
        from .nl2sql import NL2SQLClient, NL2SQLConfig, NL2SQLError

        return {
            "NL2SQLClient": NL2SQLClient,
            "NL2SQLConfig": NL2SQLConfig,
            "NL2SQLError": NL2SQLError,
        }[name]
    raise AttributeError("module 'agentic.services' has no attribute '{}'".format(name))


__all__ = [
    "LLMPurpose",
    "LLMService",
    "LLMResponse",
    "LLMRouter",
    "MindsDBClient",
    "MindsDBConfig",
    "MindsDBError",
    "DataProviderClient",
    "DataProviderError",
    "NL2SQLClient",
    "NL2SQLConfig",
    "NL2SQLError",
]
