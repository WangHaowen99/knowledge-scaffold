from __future__ import annotations

import json

import pytest

from shared_capabilities.business_data import (
    ConnectionEnvelopeError,
    ProviderResult,
    UnsupportedCapabilityError,
)
from shared_capabilities.ontology_script_sdk import DataProviderClient
from shared_capabilities.ontology_script_sdk.connection_env import (
    SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
)


def _envelope(provider="nl2sql_http"):
    return {
        "version": 1,
        "provider": provider,
        "endpoint": "https://provider.example.com",
        "auth": {"type": "bearer", "token": "placeholder"},
        "options": {"timeout_seconds": 30},
    }


def test_script_client_is_parameterless():
    with pytest.raises(TypeError):
        DataProviderClient(config={})


def test_script_client_requires_version_one_envelope(monkeypatch):
    monkeypatch.setenv(
        SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
        json.dumps({**_envelope(), "version": 2}),
    )
    with pytest.raises(ConnectionEnvelopeError, match="version"):
        DataProviderClient()


def test_script_client_allows_dataagent_and_checks_provider_capabilities(monkeypatch):
    monkeypatch.setenv(
        SCRIPT_BUSINESS_DATA_CONNECTION_ENV, json.dumps(_envelope("dataagent"))
    )
    with DataProviderClient() as client:
        assert client.provider == "dataagent"
        with pytest.raises(UnsupportedCapabilityError, match="query_sql"):
            client.query_sql("SELECT 1")


def test_script_client_delegates_query_sql_and_events(monkeypatch):
    monkeypatch.setenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, json.dumps(_envelope()))

    class _Provider:
        from shared_capabilities.business_data import ProviderCapabilities

        capabilities = ProviderCapabilities(
            query=True, query_sql=True, native_streaming=False, probe=True
        )

        def query(self, question):
            return ProviderResult(success=True, data={"question": question})

        def query_sql(self, sql):
            return ProviderResult(success=True, data={"sql": sql})

        def probe(self, timeout_seconds=None):
            return ProviderResult(success=True, data={"timeout": timeout_seconds})

        def close(self):
            self.closed = True

    provider = _Provider()
    monkeypatch.setattr(
        "shared_capabilities.ontology_script_sdk.data_provider_client.create_provider_client",
        lambda connection: provider,
    )

    with DataProviderClient() as client:
        assert client.query("q").data == {"question": "q"}
        assert client.query_sql("SELECT 1").data == {"sql": "SELECT 1"}
        assert client.probe(5).data == {"timeout": 5}
        assert [event.type for event in client.iter_query_events("q")] == [
            "start",
            "final",
            "done",
        ]
    assert provider.closed is True
