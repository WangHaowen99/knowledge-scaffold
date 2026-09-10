from __future__ import annotations

import json

import pytest

from solver_coordination.connectors.endpoint_policy import EndpointPolicy
from solver_coordination.connectors.errors import (
    ConnectorAuthenticationError,
    ConnectorCapacityError,
    ConnectorContractError,
)
from solver_coordination.connectors.http_transport import HttpSolverTransport


class _Response:
    def __init__(self, status_code, payload, *, headers=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self.content = json.dumps(payload).encode("utf-8")

    def close(self):
        pass


class _StreamingResponse(_Response):
    def __init__(self, status_code, chunks, *, headers=None):
        super().__init__(status_code, {}, headers=headers)
        self.content = None
        self._chunks = chunks
        self.closed = False

    def iter_content(self, chunk_size):
        assert chunk_size > 0
        yield from self._chunks

    def close(self):
        self.closed = True


class _Session:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def request(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def _binding():
    return {
        "endpoint": "https://solver.example.com",
        "service_credential_ref": "secret://solver/service-token",
    }


def _transport(session, *, retries=0):
    return HttpSolverTransport(
        session=session,
        credential_resolver=lambda reference: (
            "synthetic-service-token"
            if reference == "secret://solver/service-token"
            else None
        ),
        endpoint_policy=EndpointPolicy(
            allowed_hosts=("solver.example.com",),
            allowed_ports=(443,),
        ),
        max_retries=retries,
    )


def test_transport_calls_service_api_with_managed_headers():
    session = _Session(_Response(200, {"api_versions": ["ortools.solver.v1"]}))
    transport = _transport(session)

    payload = transport.request(
        method="GET",
        binding=_binding(),
        path="/api/v1/capabilities",
    )

    assert payload == {"api_versions": ["ortools.solver.v1"]}
    call = session.calls[0]
    assert call["url"] == "https://solver.example.com/api/v1/capabilities"
    assert call["headers"]["Authorization"] == "Bearer synthetic-service-token"
    assert call["allow_redirects"] is False


def test_transport_retries_capacity_error_with_the_same_idempotency_key():
    error = {
        "error": {
            "code": "CAPACITY_EXHAUSTED",
            "category": "capacity",
            "message": "service is busy",
            "retryable": True,
            "diagnostic_ref": "diag_synthetic_0001",
        }
    }
    session = _Session(
        _Response(503, error),
        _Response(202, {"job_id": "job_synthetic_0001"}),
    )
    transport = _transport(session, retries=1)

    payload = transport.request(
        method="POST",
        binding=_binding(),
        path="/api/v1/jobs",
        json_payload={"api_version": "ortools.solver.v1"},
        idempotency_key="hmac-sha256:" + "a" * 64,
    )

    assert payload["job_id"] == "job_synthetic_0001"
    assert [call["headers"]["Idempotency-Key"] for call in session.calls] == [
        "hmac-sha256:" + "a" * 64,
        "hmac-sha256:" + "a" * 64,
    ]


def test_transport_maps_service_error_without_propagating_remote_message():
    session = _Session(
        _Response(
            401,
            {
                "error": {
                    "code": "AUTHENTICATION_FAILED",
                    "category": "authentication",
                    "message": "remote detail must stay private",
                    "retryable": False,
                    "diagnostic_ref": "diag_synthetic_0002",
                }
            },
        )
    )

    with pytest.raises(ConnectorAuthenticationError) as raised:
        _transport(session).request(
            method="GET",
            binding=_binding(),
            path="/api/v1/capabilities",
        )

    assert str(raised.value) == "connector authentication failed"
    assert raised.value.diagnostic_ref == "diag_synthetic_0002"
    assert "remote detail" not in str(raised.value)


def test_transport_exhausts_retryable_capacity_as_stable_capacity_error():
    error = {
        "error": {
            "code": "CAPACITY_EXHAUSTED",
            "category": "capacity",
            "message": "busy",
            "retryable": True,
            "diagnostic_ref": "diag_synthetic_0003",
        }
    }
    with pytest.raises(ConnectorCapacityError):
        _transport(_Session(_Response(429, error))).request(
            method="POST",
            binding=_binding(),
            path="/api/v1/jobs",
            json_payload={"api_version": "ortools.solver.v1"},
            idempotency_key="hmac-sha256:" + "b" * 64,
        )


def test_transport_stops_streaming_when_response_exceeds_the_byte_limit():
    response = _StreamingResponse(200, [b'{"value":"', b"x" * 32, b'"}'])
    session = _Session(response)
    transport = HttpSolverTransport(
        session=session,
        credential_resolver=lambda _reference: "synthetic-service-token",
        endpoint_policy=EndpointPolicy(
            allowed_hosts=("solver.example.com",),
            allowed_ports=(443,),
        ),
        max_response_bytes=16,
        max_retries=0,
    )

    with pytest.raises(ConnectorContractError):
        transport.request(
            method="GET",
            binding=_binding(),
            path="/api/v1/capabilities",
        )

    assert response.closed is True
    assert session.calls[0]["stream"] is True
