from __future__ import annotations

import json

import pytest
from backend_synthetic_credentials import synthetic_invalid_password
from shared_capabilities.business_data import (
    ConnectionEnvelopeError,
    ProviderResult,
    UnsupportedCapabilityError,
)
from shared_capabilities.ontology_script_sdk import (
    DataProviderClient,
    load_effective_connection,
    write_artifact,
)
from shared_capabilities.ontology_script_sdk.connection_env import (
    SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
)


def _envelope(provider="nl2sql_http"):
    return {
        "version": 1,
        "provider": provider,
        "endpoint": "https://provider.example.com",
        "auth": {"type": "bearer", "token": synthetic_invalid_password("ontology_script_sdk-credential-01")},
        "options": {"timeout_seconds": 30},
    }


def test_script_client_is_parameterless():
    with pytest.raises(TypeError):
        DataProviderClient(config={})


def test_write_artifact_uses_only_run_owned_directory(tmp_path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("RUN_ARTIFACT_DIR", str(artifact_root))
    monkeypatch.setenv("RUN_ARTIFACT_MAX_FILE_BYTES", "128")

    name = write_artifact("result.txt", "synthetic result")

    assert name == "result.txt"
    assert (artifact_root / name).read_text(encoding="utf-8") == "synthetic result"


def test_write_artifact_rejects_path_and_overwrite(tmp_path, monkeypatch):
    artifact_root = tmp_path / "artifacts"
    artifact_root.mkdir()
    monkeypatch.setenv("RUN_ARTIFACT_DIR", str(artifact_root))
    monkeypatch.setenv("RUN_ARTIFACT_MAX_FILE_BYTES", "128")

    with pytest.raises(ValueError, match="plain file name"):
        write_artifact("../result.txt", "synthetic result")
    write_artifact("result.txt", "first")
    with pytest.raises(FileExistsError):
        write_artifact("result.txt", "second")


def test_script_client_requires_version_one_envelope(monkeypatch):
    monkeypatch.setenv(
        SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
        json.dumps({**_envelope(), "version": 2}),
    )
    with pytest.raises(ConnectionEnvelopeError, match="version"):
        DataProviderClient()


def test_public_sdk_exposes_effective_connection_for_graph_snapshot_handlers(
    monkeypatch,
):
    envelope = {
        **_envelope("neo4j"),
        "endpoint": "bolt://service.example.net:7687",
        "auth": {
            "type": "mindsdb_password",
            "username": "reader",
            "password": synthetic_invalid_password("ontology_script_sdk-credential-01"),
        },
        "options": {"database": "snapshot_20260808"},
    }
    monkeypatch.setenv(
        SCRIPT_BUSINESS_DATA_CONNECTION_ENV,
        json.dumps(envelope),
    )

    connection = load_effective_connection()

    assert connection.provider == "neo4j"
    assert connection.endpoint == "bolt://service.example.net:7687"
    assert connection.options["database"] == "snapshot_20260808"


def test_script_client_allows_dataagent_and_checks_provider_capabilities(monkeypatch):
    monkeypatch.setenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, json.dumps(_envelope("dataagent")))
    with DataProviderClient() as client:
        assert client.provider == "dataagent"
        with pytest.raises(UnsupportedCapabilityError, match="query_sql"):
            client.query_sql("SELECT 1")


def test_script_client_delegates_query_sql_and_events(monkeypatch):
    monkeypatch.setenv(SCRIPT_BUSINESS_DATA_CONNECTION_ENV, json.dumps(_envelope()))

    class _Provider:
        from shared_capabilities.business_data import ProviderCapabilities

        capabilities = ProviderCapabilities(query=True, query_sql=True, native_streaming=False, probe=True)

        def __init__(self):
            self.closed = False

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def query(self, question):
            return ProviderResult(success=True, data={"question": question})

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def query_sql(self, sql):
            return ProviderResult(success=True, data={"sql": sql})

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
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
