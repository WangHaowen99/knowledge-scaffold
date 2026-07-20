"""Single provider protocol implementation shared by all business-data callers."""

from .capability import require_capability
from .connection import AuthSpec, EffectiveConnection
from .contracts import ProviderCapabilities, ProviderEvent, ProviderResult
from .errors import (
    BusinessDataProviderError,
    ConnectionEnvelopeError,
    ProviderInternalError,
    ProviderProtocolError,
    ProviderTransportError,
    UnsupportedCapabilityError,
    UnsupportedProviderError,
)
from .events import iter_query_events
from .registry import create_provider_client, register_provider
from .providers import DataAgentProvider, MindsDBProvider, NL2SQLProvider


def _register_builtins() -> None:
    for name, factory in (
        ("mindsdb", MindsDBProvider),
        ("nl2sql_http", NL2SQLProvider),
        ("dataagent", DataAgentProvider),
    ):
        try:
            register_provider(name, factory)
        except ValueError:
            pass


_register_builtins()

__all__ = [
    "AuthSpec",
    "BusinessDataProviderError",
    "ConnectionEnvelopeError",
    "EffectiveConnection",
    "ProviderCapabilities",
    "ProviderEvent",
    "ProviderInternalError",
    "ProviderProtocolError",
    "ProviderResult",
    "ProviderTransportError",
    "DataAgentProvider",
    "MindsDBProvider",
    "NL2SQLProvider",
    "UnsupportedCapabilityError",
    "UnsupportedProviderError",
    "create_provider_client",
    "iter_query_events",
    "register_provider",
    "require_capability",
]
