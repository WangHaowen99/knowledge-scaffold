# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""ASGI route contracts for lifespan-owned bounded blocking work."""

from types import SimpleNamespace

from agent.runtime.offload import OffloadPoolClosed, OffloadPoolSaturated
from api.dependencies import run_in_runtime_pool
from api.testing import TestApplication
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient


def _app(application):
    @application.get("/probe")
    async def probe(request: Request):
        result = await run_in_runtime_pool(
            request,
            "short_io",
            lambda: "inline",
        )
        return {"result": result}

    return application


def test_production_route_fails_closed_without_runtime_container():
    response = TestClient(_app(FastAPI())).get("/probe")

    assert response.status_code == 503
    assert response.json() == {"detail": "Runtime is not available"}


def test_isolated_route_test_may_execute_inline_without_lifespan():
    response = TestClient(_app(TestApplication())).get("/probe")

    assert response.status_code == 200
    assert response.json() == {"result": "inline"}


def test_route_uses_runtime_container_instead_of_legacy_runtime():
    calls = []

    class _Pool:
        def __init__(self, label):
            self.label = label

        async def run(self, function, *args, **kwargs):
            calls.append(self.label)
            return function(*args, **kwargs)

    app = _app(TestApplication())
    app.state.runtime_container = SimpleNamespace(short_io=_Pool("owned"))
    app.state.agent_execution_runtime = SimpleNamespace(short_io=_Pool("legacy"))

    response = TestClient(app).get("/probe")

    assert response.status_code == 200
    assert calls == ["owned"]


def test_route_maps_pool_capacity_and_drain_errors():
    class _RejectedPool:
        def __init__(self, error):
            self.error = error

        async def run(self, *_args, **_kwargs):
            raise self.error

    for error, expected_status, expected_detail in (
        (
            OffloadPoolSaturated("full"),
            429,
            "Runtime offload capacity is exhausted",
        ),
        (
            OffloadPoolClosed("closed"),
            503,
            "Runtime is draining",
        ),
    ):
        app = _app(TestApplication())
        app.state.runtime_container = SimpleNamespace(
            short_io=_RejectedPool(error),
        )

        response = TestClient(app).get("/probe")

        assert response.status_code == expected_status
        assert response.json() == {"detail": expected_detail}
