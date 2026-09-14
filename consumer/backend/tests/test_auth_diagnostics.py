# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Authentication failure diagnostics use synthetic data only."""

import asyncio
import threading
from types import SimpleNamespace

import jwt
import pytest
import requests
from fastapi import FastAPI
from fastapi.testclient import TestClient

from agent.runtime.offload import BoundedOffloadPool, OffloadPoolClosed, OffloadPoolSaturated
from api.auth.remote_auth import RemoteAuthBackend
from api.routes.auth import auth_router
from api.testing import TestApplication
from backend_synthetic_credentials import synthetic_secret
from utils.logging import ContextFormatter, JsonFormatter, bind_log_context


_BASE_URL = "http://auth.example.net"
_PASSWORD = synthetic_secret("auth-diagnostic-password")
_ACCESS_TOKEN = synthetic_secret("auth-diagnostic-access")
_REFRESH_TOKEN = synthetic_secret("auth-diagnostic-refresh")
_KEY_ID = "synthetic-private-key-id"
_PRIVATE_BODY = "synthetic-private-response-body"
_PRIVATE_ERROR = f"{_BASE_URL} {_PASSWORD} {_ACCESS_TOKEN} {_PRIVATE_BODY}"


class _Response:
    def __init__(self, status_code=200, payload=None, error=None):
        self.status_code = status_code
        self.payload = payload
        self.error = error

    def json(self):
        if self.error is not None:
            raise self.error
        return self.payload


def _application(backend, *, real_runtime=False):
    app = FastAPI() if real_runtime else TestApplication()
    app.state.services = {"AUTH_MANAGER": backend}
    app.include_router(auth_router)
    return app


def _diagnostic_records(caplog):
    return [record for record in caplog.records if record.name == "auth.diagnostics"]


@pytest.mark.parametrize("operation", ["login", "refresh"])
@pytest.mark.parametrize(
    "case,stage,reason,error_type,upstream_status",
    [
        ("token_timeout", "token_request", "request_failed", "ReadTimeout", None),
        ("token_status", "token_request", "upstream_status", "ValueError", 502),
        ("token_json", "token_response", "invalid_json", "ValueError", 200),
        ("token_pair", "token_response", "invalid_token_pair", "TypeError", 200),
        ("jwt_header", "jwt_validation", "invalid_header", "DecodeError", None),
        ("jwks_timeout", "jwks", "request_failed", "ReadTimeout", None),
        ("jwks_status", "jwks", "upstream_status", "ValueError", 502),
        ("jwks_json", "jwks", "invalid_json", "ValueError", 200),
        ("jwks_shape", "jwks", "invalid_key_set", "ValueError", 200),
        ("jwt_issuer", "jwt_validation", "invalid_claims_or_signature", "InvalidIssuerError", None),
    ],
)
def test_auth_failure_classification_and_redaction(
    operation, case, stage, reason, error_type, upstream_status, monkeypatch, caplog,
):
    token_response = _Response(payload={
        "access_token": _ACCESS_TOKEN,
        "refresh_token": _REFRESH_TOKEN,
        "expires_in": 60,
        "private_body": _PRIVATE_BODY,
    })
    jwks_response = _Response(payload={"keys": []})

    def post(*_args, **_kwargs):
        if case == "token_timeout":
            raise requests.ReadTimeout(_PRIVATE_ERROR)
        return token_response

    def get(*_args, **_kwargs):
        if case == "jwks_timeout":
            raise requests.ReadTimeout(_PRIVATE_ERROR)
        return jwks_response

    backend = RemoteAuthBackend(_BASE_URL, session=SimpleNamespace(post=post, get=get))

    def header(_token):
        if case == "jwt_header":
            raise jwt.DecodeError(_PRIVATE_ERROR)
        return {"alg": "RS256", "kid": _KEY_ID}

    monkeypatch.setattr(jwt, "get_unverified_header", header)
    if case == "token_status":
        token_response.status_code = 502
    elif case == "token_json":
        token_response.error = ValueError(_PRIVATE_ERROR)
    elif case == "token_pair":
        token_response.payload = {"private_body": _PRIVATE_BODY}
    elif case == "jwks_status":
        jwks_response.status_code = 502
    elif case == "jwks_json":
        jwks_response.error = ValueError(_PRIVATE_ERROR)
    elif case == "jwks_shape":
        jwks_response.payload = [_PRIVATE_BODY]
    elif case == "jwt_issuer":
        monkeypatch.setattr(backend, "_signing_key", lambda _kid: object())

        def decode(*_args, **_kwargs):
            raise jwt.InvalidIssuerError(_PRIVATE_ERROR)

        monkeypatch.setattr(jwt, "decode", decode)

    with TestClient(_application(backend)) as client:
        client.cookies.set("onto_refresh_token", _REFRESH_TOKEN)
        response = client.post(f"/auth/{operation}", json={"username": "operator_a", "password": _PASSWORD})

    assert response.status_code == 503
    assert response.json() == {"error": "认证服务暂不可用"}
    records = _diagnostic_records(caplog)
    assert len(records) == 1
    record = records[0]
    assert record.auth_operation == operation
    assert record.auth_stage == stage
    assert record.auth_reason == reason
    assert record.auth_error_type == error_type
    assert record.auth_upstream_status == upstream_status
    assert len(record.auth_attempt_id) == 32
    assert record.auth_elapsed_ms >= 0
    assert not record.exc_info
    rendered = record.getMessage() + JsonFormatter().format(record)
    private_values = (
        _BASE_URL, _PASSWORD, _ACCESS_TOKEN, _REFRESH_TOKEN, _KEY_ID, _PRIVATE_BODY, "operator_a",
    )
    assert all(value not in rendered for value in private_values)


@pytest.mark.parametrize("status", [400, 401])
def test_invalid_credentials_do_not_emit_availability_diagnostics(status, caplog):
    backend = RemoteAuthBackend(
        _BASE_URL,
        session=SimpleNamespace(post=lambda *_args, **_kwargs: _Response(status)),
    )
    with TestClient(_application(backend)) as client:
        response = client.post("/auth/login", json={"username": "operator_a", "password": _PASSWORD})
    assert response.status_code == 401
    assert _diagnostic_records(caplog) == []


def test_auth_diagnostics_excludes_ambient_identity_context(caplog):
    backend = RemoteAuthBackend(
        _BASE_URL,
        session=SimpleNamespace(post=lambda *_args, **_kwargs: _Response(502)),
    )
    with bind_log_context(agent_id="operator_a", session_id=_REFRESH_TOKEN, run_id=_ACCESS_TOKEN):
        with pytest.raises(ValueError, match="认证服务暂不可用"):
            backend.login("operator_a", _PASSWORD)
        records = _diagnostic_records(caplog)
        assert len(records) == 1
        record = records[0]
        rendered = (
            JsonFormatter().format(record)
            + ContextFormatter("%(message)s%(agent_context)s").format(record)
        )
    assert all(value not in rendered for value in ("operator_a", _REFRESH_TOKEN, _ACCESS_TOKEN))
    assert all(field not in record.__dict__ for field in ("agent_id", "session_id", "run_id"))


@pytest.mark.parametrize(
    "failure,status,reason",
    [(None, 503, "runtime_unavailable"), (OffloadPoolClosed, 503, "runtime_draining"),
     (OffloadPoolSaturated, 429, "capacity_exhausted")],
)
def test_runtime_failure_is_distinguished_from_remote_auth(failure, status, reason, caplog):
    app = _application(SimpleNamespace(login=lambda *_args: None), real_runtime=True)
    if failure is not None:
        async def run(*_args):
            raise failure(_PRIVATE_ERROR)

        app.state.runtime_container = SimpleNamespace(external_io=SimpleNamespace(run=run))
    with TestClient(app) as client:
        response = client.post("/auth/login", json={"username": "operator_a", "password": _PASSWORD})
    assert response.status_code == status
    records = _diagnostic_records(caplog)
    assert len(records) == 1
    assert records[0].auth_stage == "runtime"
    assert records[0].auth_reason == reason
    assert _PRIVATE_ERROR not in JsonFormatter().format(records[0])


def test_concurrent_auth_attempts_keep_diagnostics_isolated_across_runtime_pool(caplog):
    from api.auth.diagnostics import auth_diagnostic_context

    barrier = threading.Barrier(2)

    def post(*_args, json, **_kwargs):
        barrier.wait(timeout=5)
        if json["grant_type"] == "password":
            raise requests.ReadTimeout(_PRIVATE_ERROR)
        return _Response(502)

    backend = RemoteAuthBackend(_BASE_URL, session=SimpleNamespace(post=post))

    async def scenario():
        pool = BoundedOffloadPool(
            name="auth-diagnostic-test", max_workers=2, max_pending=2,
            capacity_wait_seconds=5, operation_timeout_seconds=10,
        )

        async def attempt(operation, function, *args):
            with pytest.raises(ValueError, match="认证服务暂不可用"):
                with auth_diagnostic_context(operation):
                    await pool.run(function, *args)

        try:
            await asyncio.gather(
                attempt("login", backend.login, "operator_a", _PASSWORD),
                attempt("refresh", backend.refresh_tokens, _REFRESH_TOKEN),
            )
        finally:
            assert await pool.close(deadline=asyncio.get_running_loop().time() + 5)

    asyncio.run(scenario())
    records = _diagnostic_records(caplog)
    assert len(records) == 2
    assert len({record.auth_attempt_id for record in records}) == 2
    assert {record.auth_operation: record.auth_reason for record in records} == {
        "login": "request_failed", "refresh": "upstream_status",
    }
    assert all(not record.exc_info for record in records)
    # A later direct call starts a fresh context instead of reusing a failed one.

    def unavailable_response(*_args, **_kwargs):
        return _Response(502)

    backend._session.post = unavailable_response
    with pytest.raises(ValueError):
        backend.login("operator_a", _PASSWORD)
    assert len({record.auth_attempt_id for record in _diagnostic_records(caplog)}) == 3
