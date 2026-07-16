"""Provider client registry using only explicit effective connections."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from .connection import EffectiveConnection
from .errors import UnsupportedProviderError

ProviderFactory = Callable[[EffectiveConnection], Any]
_PROVIDERS: dict[str, ProviderFactory] = {}


def register_provider(name: str, factory: ProviderFactory) -> None:
    key = str(name or "").strip().lower()
    if not key or key in _PROVIDERS:
        raise ValueError("invalid or duplicate provider: {}".format(key))
    _PROVIDERS[key] = factory


def create_provider_client(connection: EffectiveConnection) -> Any:
    factory = _PROVIDERS.get(connection.provider)
    if factory is None:
        raise UnsupportedProviderError(
            "unsupported data provider '{}'".format(connection.provider)
        )
    return factory(connection)


