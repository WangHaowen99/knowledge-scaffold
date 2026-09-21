# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Run-scoped deterministic provider capability tests."""

from __future__ import annotations

import socket
import threading

import pytest
from agent.direct_execution.provider_broker import (
    DeterministicProviderBroker,
    DeterministicProviderBrokerError,
    _normalize_graph_operation_arguments,
    _provider_config,
    _with_external_filter_result,
)
from agent.tools.business_data.deterministic import DeterministicProviderBinding
from agent.tools.business_data.providers.graph.models import GraphBackendResult, GraphScope
from backend_synthetic_credentials import synthetic_invalid_password
from shared_capabilities.business_data import AuthSpec, EffectiveConnection
from shared_capabilities.ontology_script_sdk.deterministic_provider import (
    DETERMINISTIC_PROVIDER_SOCKET_ENV,
    DeterministicProviderCallError,
    DeterministicProviderClient,
)


def _binding(*, network_endpoint="neo4j+s://192.0.2.24:7687"):
    connection = EffectiveConnection(
        version=1,
        provider="neo4j",
        endpoint="neo4j+s://snapshot.example.com:7687",
        auth=AuthSpec(
            type="mindsdb_password",
            username="operator_a",
            password=synthetic_invalid_password("deterministic_provider_broker-credential-01"),
        ),
        options={
            "database": "snapshot_demo",
            "workspace_id": "workspace_demo",
            "ontology_id": "knowledge_package_demo",
            "ontology_property": "d2o_ontology_id",
            "max_result_rows": 3,
        },
    )
    return DeterministicProviderBinding(
        provider="neo4j",
        source_ref="business_data",
        data_revision="revision_demo",
        scope_digest="sha256:synthetic-scope",
        network_endpoint=network_endpoint,
        effective_connection=connection,
    )


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_broker_executes_only_allowlisted_scoped_operations(tmp_path, monkeypatch):
    calls = []

    class FakeBackend:
        closed = False

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def property_filter(self, **kwargs):
            calls.append(kwargs)
            return GraphBackendResult(
                records=[{"code": "synthetic"}],
                total=1,
            )

        def close(self):
            self.closed = True

    backend = FakeBackend()
    broker = DeterministicProviderBroker(
        binding=_binding(),
        socket_dir=tmp_path,
        backend_factory=lambda _binding_value: backend,
    ).start()
    monkeypatch.setenv(DETERMINISTIC_PROVIDER_SOCKET_ENV, str(broker.socket_path))
    try:
        result = DeterministicProviderClient().execute(
            "property_filter",
            {
                "element_class": "SyntheticNode",
                "element_type": "NODE",
                "filter_dict": {},
                "return_properties": ["code"],
                "limit": 999,
                "offset": -1,
            },
        )
    finally:
        broker.close()

    assert result["records"] == [{"code": "synthetic"}]
    assert calls[0]["limit"] == 3
    assert calls[0]["offset"] == 0
    assert calls[0]["scope"].workspace_value == "workspace_demo"
    assert calls[0]["scope"].ontology_value == "knowledge_package_demo"
    assert backend.closed is True
    assert not broker.socket_path.exists()


def test_broker_normalizes_filter_values_with_backend_physical_types():
    calls = []

    class TypedBackend:
        provider = "neo4j"

        def get_property_types(self, **kwargs):
            calls.append(("get_property_types", kwargs))
            return {
                "property_types": {
                    field: "INTEGER"
                    for field in kwargs["property_names"]
                },
                "complete": True,
            }

    backend = TypedBackend()
    scope = GraphScope(
        workspace_property="workspace_key",
        workspace_value="workspace_demo",
        ontology_property="ontology_key",
        ontology_value="knowledge_package_demo",
    )
    normalized = _normalize_graph_operation_arguments(
        operation="property_filter",
        arguments={
            "element_class": "SyntheticNode",
            "element_type": "NODE",
            "filter_dict": {"event_key": "9007199254740993"},
        },
        backend=backend,
        scope=scope,
    )
    pattern = _normalize_graph_operation_arguments(
        operation="pattern_search",
        arguments={
            "path_pattern": [
                ["SyntheticNode", {"props": {"event_key": "42"}}],
                ["SYNTHETIC_LINK", "->"],
                ["SyntheticTarget"],
            ],
        },
        backend=backend,
        scope=scope,
    )
    shortest = _normalize_graph_operation_arguments(
        operation="shortest_path_search",
        arguments={
            "start_label": "SyntheticNode",
            "start_props": {"start_key": "41"},
            "end_label": "SyntheticTarget",
            "end_props": {"end_key": "42"},
        },
        backend=backend,
        scope=scope,
    )

    assert normalized["filter_dict"] == {"event_key": 9007199254740993}
    assert pattern["path_pattern"][0][1]["props"] == {"event_key": 42}
    assert shortest["start_props"] == {"start_key": 41}
    assert shortest["end_props"] == {"end_key": 42}


def test_broker_aggregate_result_keeps_external_filter_values():
    external_filter = {"event_key": "9007199254740993"}
    backend_filter = {"event_key": 9007199254740993}
    result = GraphBackendResult(
        records=[{
            "value": 1,
            "applied_filter": backend_filter,
            "filter_keys": ["event_key"],
        }],
        payload={
            "result": {
                "value": 1,
                "applied_filter": backend_filter,
                "filter_keys": ["event_key"],
            },
        },
    )

    normalized = _with_external_filter_result(result, external_filter)

    assert normalized.records[0]["applied_filter"] == external_filter
    assert normalized.payload["result"]["applied_filter"] == external_filter


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_broker_rejects_unknown_operations_without_backend_call(tmp_path, monkeypatch):
    backend_calls = []
    broker = DeterministicProviderBroker(
        binding=_binding(),
        socket_dir=tmp_path,
        backend_factory=lambda _binding_value: backend_calls.append(True),
    ).start()
    monkeypatch.setenv(DETERMINISTIC_PROVIDER_SOCKET_ENV, str(broker.socket_path))
    try:
        with pytest.raises(DeterministicProviderCallError) as exc:
            DeterministicProviderClient().execute("write_query", {"query": "DELETE"})
    finally:
        broker.close()

    assert exc.value.code == "provider_arguments_invalid"
    assert backend_calls == []


def test_broker_config_keeps_tls_name_and_uses_only_pinned_connect_address():
    config = _provider_config(_binding())

    assert config["uri"] == "neo4j+s://snapshot.example.com:7687"
    assert config["resolved_uri"] == "neo4j+s://192.0.2.24:7687"
    assert config["password"] == synthetic_invalid_password("deterministic_provider_broker-credential-01")
    assert config["graph_query_timeout_seconds"] == 30.0
    assert config["graph_connect_timeout_seconds"] == 10.0
    assert config["max_retries"] == 0


def test_broker_config_rejects_domain_as_connect_address():
    with pytest.raises(DeterministicProviderBrokerError, match="pinned to an IP"):
        _provider_config(_binding(network_endpoint="neo4j+s://other.example.net:7687"))


@pytest.mark.parametrize(
    "network_endpoint",
    [
        "neo4j+ssc://192.0.2.24:7687",
        "neo4j+s://192.0.2.24:7688",
        "neo4j+s://192.0.2.24:7687/other",
        "neo4j+s://192.0.2.24:7687?target=other",
        "neo4j+s://192.0.2.24:7687#other",
    ],
)
def test_broker_config_rejects_mismatched_pinned_endpoint(network_endpoint):
    with pytest.raises(DeterministicProviderBrokerError, match="does not match"):
        _provider_config(_binding(network_endpoint=network_endpoint))


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_broker_start_closes_socket_when_setup_fails(tmp_path, monkeypatch):
    class FailingSocket:
        closed = False

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def bind(self, _path):
            raise OSError("synthetic bind failure")

        def close(self):
            self.closed = True

    server = FailingSocket()
    monkeypatch.setattr(socket, "socket", lambda *_args: server)
    broker = DeterministicProviderBroker(binding=_binding(), socket_dir=tmp_path)

    with pytest.raises(DeterministicProviderBrokerError, match="could not be created"):
        broker.start()

    assert server.closed is True


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_provider_client_maps_timeout_to_transport_error(tmp_path, monkeypatch):
    class FailingSocket:
        def __enter__(self):
            return self

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def __exit__(self, _exc_type, _exc, _traceback):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def settimeout(self, _timeout):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def connect(self, _path):
            raise TimeoutError("synthetic timeout")

    monkeypatch.setattr(socket, "socket", lambda *_args: FailingSocket())
    monkeypatch.setenv(
        DETERMINISTIC_PROVIDER_SOCKET_ENV,
        str(tmp_path / "provider.sock"),
    )

    with pytest.raises(DeterministicProviderCallError) as exc:
        DeterministicProviderClient().execute("count_search")

    assert exc.value.code == "provider_transport_error"


@pytest.mark.skipif(not hasattr(socket, "AF_UNIX"), reason="Unix sockets unavailable")
def test_broker_close_interrupts_active_backend_call(tmp_path, monkeypatch):
    entered = threading.Event()
    released = threading.Event()

    class BlockingBackend:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def count_search(self, **_kwargs):
            entered.set()
            assert released.wait(timeout=5)
            return GraphBackendResult(total=0)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            released.set()

    broker = DeterministicProviderBroker(
        binding=_binding(),
        socket_dir=tmp_path,
        backend_factory=lambda _binding_value: BlockingBackend(),
    ).start()
    monkeypatch.setenv(DETERMINISTIC_PROVIDER_SOCKET_ENV, str(broker.socket_path))
    client_error = []

    def call_provider():
        try:
            DeterministicProviderClient().execute(
                "count_search",
                {
                    "element_class": "SyntheticNode",
                    "element_type": "NODE",
                    "filter_dict": {},
                },
            )
        except DeterministicProviderCallError as exc:
            client_error.append(exc.code)

    client_thread = threading.Thread(target=call_provider)
    client_thread.start()
    assert entered.wait(timeout=5)

    broker.close()
    client_thread.join(timeout=5)

    assert released.is_set()
    assert not client_thread.is_alive()
    assert client_error
