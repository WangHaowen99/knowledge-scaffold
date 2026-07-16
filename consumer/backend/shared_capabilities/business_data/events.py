"""Uniform event adaptation for native and blocking providers."""

from __future__ import annotations

from collections.abc import Iterator

from .contracts import ProviderEvent, QueryProvider


def iter_query_events(provider: QueryProvider, question: str) -> Iterator[ProviderEvent]:
    if provider.capabilities.native_streaming:
        native_iterator = getattr(provider, "_iter_native_query_events", None)
        if not callable(native_iterator):
            raise TypeError("native streaming provider has no native event iterator")
        yield from native_iterator(question)
        return

    yield ProviderEvent(type="start")
    result = provider.query(question)
    if result.success:
        yield ProviderEvent(type="final", content=result.message, result=result)
    else:
        yield ProviderEvent(type="error", content=result.error, result=result)
    yield ProviderEvent(type="done")


