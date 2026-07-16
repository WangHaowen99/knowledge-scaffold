from __future__ import annotations

import pytest

from shared_capabilities.business_data import (
    ConnectionEnvelopeError,
    EffectiveConnection,
    ProviderCapabilities,
    ProviderEvent,
    ProviderResult,
    UnsupportedCapabilityError,
    iter_query_events,
    require_capability,
)


class _BlockingProvider:
    capabilities = ProviderCapabilities(
        query=True, query_sql=False, native_streaming=False, probe=False
    )

    def __init__(self, result: ProviderResult):
        self.result = result

    def query(self, question: str) -> ProviderResult:
        assert question == "find one"
        return self.result


class _NativeProvider:
    capabilities = ProviderCapabilities(
        query=True, query_sql=False, native_streaming=True, probe=False
    )

    def _iter_native_query_events(self, question: str):
        assert question == "find one"
        yield ProviderEvent(type="start")
        yield ProviderEvent(type="intermediate", content="working")
        yield ProviderEvent(
            type="final", result=ProviderResult(success=True, data=[{"id": 1}])
        )
        yield ProviderEvent(type="done")


def test_effective_connection_round_trips_version_one():
    payload = {
        "version": 1,
        "provider": "mindsdb",
        "endpoint": "https://db.example.com/",
        "auth": {"type": "bearer", "token": "secret"},
        "options": {"timeout_seconds": 120},
    }

    connection = EffectiveConnection.from_dict(payload)

    assert connection.endpoint == "https://db.example.com"
    assert connection.to_dict() == {**payload, "endpoint": "https://db.example.com"}


@pytest.mark.parametrize("version", [None, 0, 2, "1"])
def test_effective_connection_rejects_non_version_one(version):
    with pytest.raises(ConnectionEnvelopeError, match="version"):
        EffectiveConnection.from_dict(
            {
                "version": version,
                "provider": "mindsdb",
                "endpoint": "https://db.example.com",
                "auth": {"type": "none"},
                "options": {},
            }
        )


def test_effective_connection_rejects_unknown_auth_type():
    with pytest.raises(ConnectionEnvelopeError, match="auth"):
        EffectiveConnection.from_dict(
            {
                "version": 1,
                "provider": "mindsdb",
                "endpoint": "https://db.example.com",
                "auth": {"type": "basic"},
                "options": {},
            }
        )


def test_require_capability_rejects_unimplemented_operation():
    capabilities = ProviderCapabilities(
        query=True, query_sql=False, native_streaming=False, probe=True
    )

    with pytest.raises(UnsupportedCapabilityError, match="query_sql"):
        require_capability(capabilities, "query_sql")


def test_iter_query_events_synthesizes_non_streaming_sequence():
    provider = _BlockingProvider(ProviderResult(success=True, data=[{"id": 1}]))

    events = list(iter_query_events(provider, "find one"))

    assert [event.type for event in events] == ["start", "final", "done"]
    assert events[1].result is not None
    assert events[1].result.data == [{"id": 1}]


def test_iter_query_events_preserves_native_sequence():
    events = list(iter_query_events(_NativeProvider(), "find one"))
    assert [event.type for event in events] == [
        "start",
        "intermediate",
        "final",
        "done",
    ]


def test_iter_query_events_turns_failed_result_into_error():
    provider = _BlockingProvider(ProviderResult(success=False, error="failed"))
    events = list(iter_query_events(provider, "find one"))
    assert [event.type for event in events] == ["start", "error", "done"]
    assert events[1].content == "failed"
