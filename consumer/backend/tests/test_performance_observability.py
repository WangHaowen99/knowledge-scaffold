from __future__ import annotations

import asyncio
import json

import pytest


def test_request_timing_collector_is_allowlisted_aggregated_and_scoped():
    from integration.observability.performance import (
        get_server_timing_header,
        record_timing,
        request_performance_context,
    )

    assert record_timing("system_config", 99) is False
    assert get_server_timing_header() == ""

    with request_performance_context():
        assert record_timing("system_config", 1.25) is True
        assert record_timing("system_config", 2.75) is True
        assert record_timing("workspace_demo_secret_value", 500) is False
        assert get_server_timing_header() == "system_config;dur=4"

    assert get_server_timing_header() == ""


def test_server_timing_middleware_merges_existing_header_without_identity_values():
    from fastapi import FastAPI, Response
    from fastapi.testclient import TestClient

    from api.middleware import install_http_middleware
    from integration.observability.performance import record_timing

    app = FastAPI()
    install_http_middleware(app)

    @app.get("/items/{item_id}")
    async def timed_route(item_id: str):
        assert item_id == "route-secret-value"
        assert record_timing("system_config", 1.25) is True
        assert record_timing("system_config", 2.75) is True
        assert record_timing(item_id, 900) is False
        return Response(
            "ok",
            headers={"Server-Timing": "upstream;dur=2"},
        )

    with TestClient(app) as client:
        response = client.get(
            "/items/route-secret-value",
            headers={
                "Authorization": "Bearer request-secret-value",
                "X-Workspace-Id": "workspace-secret-value",
                "X-User-Id": "user-secret-value",
                "X-Run-Id": "run-secret-value",
            },
        )

    header = response.headers["server-timing"]
    assert header == "upstream;dur=2, system_config;dur=4"
    assert all(
        value not in header
        for value in (
            "route-secret-value",
            "request-secret-value",
            "workspace-secret-value",
            "user-secret-value",
            "run-secret-value",
        )
    )


def test_server_timing_context_is_cleared_when_request_is_cancelled():
    from integration.observability.performance import record_timing
    from api.middleware import ServerTimingMiddleware

    async def cancelled_app(_scope, _receive, _send):
        assert record_timing("auth", 3) is True
        raise asyncio.CancelledError

    middleware = ServerTimingMiddleware(cancelled_app)

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(
            middleware(
                _http_scope("/cancelled"),
                _empty_receive,
                _discard_send,
            )
        )

    assert record_timing("auth", 3) is False


def test_server_timing_context_is_isolated_between_concurrent_requests():
    from integration.observability.performance import record_timing
    from api.middleware import ServerTimingMiddleware

    first_recorded = asyncio.Event()
    release_first = asyncio.Event()

    async def concurrent_app(scope, _receive, send):
        if scope["path"] == "/first":
            assert record_timing("system_config", 5) is True
            first_recorded.set()
            await release_first.wait()
        else:
            await first_recorded.wait()
            assert record_timing("ontology_view", 7) is True
            release_first.set()
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": [],
            }
        )
        await send({"type": "http.response.body", "body": b""})

    middleware = ServerTimingMiddleware(concurrent_app)

    async def run_requests():
        first_messages = []
        second_messages = []

        async def first_send(message):
            first_messages.append(message)

        async def second_send(message):
            second_messages.append(message)

        await asyncio.gather(
            middleware(_http_scope("/first"), _empty_receive, first_send),
            middleware(_http_scope("/second"), _empty_receive, second_send),
        )
        return first_messages, second_messages

    first_messages, second_messages = asyncio.run(run_requests())

    assert _server_timing_from_messages(first_messages) == "system_config;dur=5"
    assert _server_timing_from_messages(second_messages) == "ontology_view;dur=7"


def test_cache_metric_uses_real_otel_counter_with_only_bounded_labels(monkeypatch):
    from opentelemetry import metrics
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import InMemoryMetricReader

    from integration.observability import performance

    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader], shutdown_on_exit=False)
    monkeypatch.setattr(metrics, "get_meter", provider.get_meter)
    monkeypatch.setattr(performance, "_cache_counter", None, raising=False)

    try:
        assert performance.record_cache_event("workspace_catalog", "hit") is True
        assert performance.record_cache_event("workspace_secret_value", "hit") is False
        assert performance.record_cache_event("workspace_catalog", "user_secret_value") is False

        metrics_data = reader.get_metrics_data()
        exported_metrics = [
            metric
            for resource_metrics in metrics_data.resource_metrics
            for scope_metrics in resource_metrics.scope_metrics
            for metric in scope_metrics.metrics
        ]
        cache_metric = next(
            metric
            for metric in exported_metrics
            if metric.name == "agent.cache.event.count"
        )
        points = list(cache_metric.data.data_points)
    finally:
        provider.shutdown()

    assert len(points) == 1
    assert points[0].value == 1
    assert dict(points[0].attributes) == {
        "cache": "workspace_catalog",
        "result": "hit",
    }
    assert "secret_value" not in repr(metrics_data)


def test_real_cache_components_emit_outcomes_and_request_phase_headers(
    monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from api.middleware import install_http_middleware
    from config import system_config as system_config_module
    from config.config_snapshot import ConfigSnapshotManager, ConfigSourceSignature
    from integration.observability import performance
    from ontology import view_cache as ontology_view_cache_module
    from ontology.view_cache import OntologyViewCache
    from workspace import workspaces as workspaces_module
    from workspace.catalog_snapshot import (
        WorkspaceCatalogData,
        WorkspaceCatalogManager,
        WorkspaceFileSignature,
        WorkspaceSourceSignature,
    )

    class RecordingInstrument:
        def __init__(self):
            self.records = []

        def add(self, value, attributes):
            self.records.append((value, dict(attributes)))

        def record(self, value, attributes):
            self.records.append((value, dict(attributes)))

    cache_counter = RecordingInstrument()
    phase_histogram = RecordingInstrument()
    monkeypatch.setattr(performance, "_cache_counter", cache_counter)
    monkeypatch.setattr(
        performance,
        "_phase_histogram",
        phase_histogram,
        raising=False,
    )

    config_now = [0.0]
    config_signature = ConfigSourceSignature(exists=False)
    config_signature_error = [False]

    def config_signature_loader(_path):
        if config_signature_error[0]:
            raise OSError("synthetic transient signature error")
        return config_signature

    config_manager = ConfigSnapshotManager(
        source_path="synthetic-config.json",
        loader=lambda _environment: {"storage": {"root": "synthetic"}},
        environment={},
        clock=lambda: config_now[0],
        signature_loader=config_signature_loader,
        is_transient_error=lambda exc: isinstance(exc, OSError),
    )
    config_error_manager = ConfigSnapshotManager(
        source_path="synthetic-error.json",
        loader=lambda _environment: (_ for _ in ()).throw(
            RuntimeError("synthetic config load error")
        ),
        environment={},
        signature_loader=lambda _path: config_signature,
    )

    workspace_now = [0.0]
    missing_file = WorkspaceFileSignature(exists=False)
    workspace_signature = WorkspaceSourceSignature(
        runtime_root="synthetic-runtime",
        settings=missing_file,
        workspaces_root=missing_file,
        metadata=(),
    )
    workspace_signature_error = [False]

    def workspace_signature_loader():
        if workspace_signature_error[0]:
            raise OSError("synthetic transient signature error")
        return workspace_signature

    workspace_data = WorkspaceCatalogData(
        source_signature=workspace_signature,
        default_workspace_id="workspace_demo",
        items=({"id": "workspace_demo", "name": "Demo"},),
    )
    workspace_manager = WorkspaceCatalogManager(
        loader=lambda: workspace_data,
        signature_loader=workspace_signature_loader,
        clock=lambda: workspace_now[0],
        is_transient_error=lambda exc: isinstance(exc, OSError),
    )
    workspace_error_manager = WorkspaceCatalogManager(
        loader=lambda: (_ for _ in ()).throw(
            RuntimeError("synthetic workspace load error")
        ),
        signature_loader=lambda: workspace_signature,
    )

    ontology_now = [0.0]
    ontology_signature_error = [False]

    def ontology_signature_loader():
        if ontology_signature_error[0]:
            raise OSError("synthetic transient signature error")
        return "tree-v1"

    ontology_cache = OntologyViewCache(
        clock=lambda: ontology_now[0],
        check_interval_seconds=1.5,
    )
    ontology_error_cache = OntologyViewCache()

    monkeypatch.setenv("PERF_CONFIG_SNAPSHOT_ENABLED", "false")
    monkeypatch.setattr(
        system_config_module,
        "_load_system_config_unlocked",
        lambda: {"storage": {"root": "synthetic"}},
    )
    monkeypatch.setenv("PERF_WORKSPACE_SNAPSHOT_ENABLED", "false")
    monkeypatch.setattr(
        workspaces_module,
        "_list_workspaces_authoritative",
        lambda: [],
    )
    monkeypatch.setenv("PERF_ONTOLOGY_VIEW_CACHE_ENABLED", "false")

    app = FastAPI()
    install_http_middleware(app)

    @app.get("/cache-observability")
    async def cache_observability_route():
        config_manager.load()
        config_manager.load()
        config_now[0] = 2.0
        config_signature_error[0] = True
        config_manager.load()
        with pytest.raises(RuntimeError):
            config_error_manager.load()
        system_config_module.load_system_config()

        workspace_manager.get_snapshot()
        workspace_manager.get_snapshot()
        workspace_now[0] = 2.0
        workspace_signature_error[0] = True
        workspace_manager.get_snapshot()
        with pytest.raises(RuntimeError):
            workspace_error_manager.get_snapshot()
        workspaces_module.list_workspaces()

        load_tree = lambda: [{"name": "synthetic.txt", "type": "file"}]
        ontology_cache.get_package_tree(
            workspace_id="workspace_demo",
            ontology_id="ontology_demo",
            version="v1",
            loader=load_tree,
            signature_loader=ontology_signature_loader,
        )
        ontology_cache.get_package_tree(
            workspace_id="workspace_demo",
            ontology_id="ontology_demo",
            version="v1",
            loader=load_tree,
            signature_loader=ontology_signature_loader,
        )
        ontology_now[0] = 2.0
        ontology_signature_error[0] = True
        ontology_cache.get_package_tree(
            workspace_id="workspace_demo",
            ontology_id="ontology_demo",
            version="v1",
            loader=load_tree,
            signature_loader=ontology_signature_loader,
        )
        with pytest.raises(RuntimeError):
            ontology_error_cache.get_package_tree(
                workspace_id="workspace_demo",
                ontology_id="ontology_error",
                version="v1",
                loader=lambda: (_ for _ in ()).throw(
                    RuntimeError("synthetic ontology load error")
                ),
            )
        ontology_view_cache_module.get_package_tree_cached(
            workspace_id="workspace_demo",
            ontology_id="ontology_bypass",
            version="v1",
            loader=lambda: [],
        )
        return {"ok": True}

    with TestClient(app) as client:
        response = client.get("/cache-observability")

    assert response.status_code == 200
    timing_names = {
        item.split(";", 1)[0]
        for item in response.headers["server-timing"].split(", ")
    }
    assert {"system_config", "workspace_catalog", "ontology_view"} <= timing_names
    outcomes_by_cache = {}
    for _value, attributes in cache_counter.records:
        outcomes_by_cache.setdefault(attributes["cache"], set()).add(
            attributes["result"]
        )
    assert outcomes_by_cache == {
        "system_config": {"bypass", "error", "hit", "miss", "stale"},
        "workspace_catalog": {"bypass", "error", "hit", "miss", "stale"},
        "ontology_view": {"bypass", "error", "hit", "miss", "stale"},
    }


def test_streaming_header_is_pre_start_only_and_late_timing_is_still_metered(
    monkeypatch,
):
    from fastapi import FastAPI
    from fastapi.responses import StreamingResponse
    from fastapi.testclient import TestClient

    from api.middleware import install_http_middleware
    from integration.observability import performance

    class RecordingHistogram:
        def __init__(self):
            self.records = []

        def record(self, value, attributes):
            self.records.append((value, dict(attributes)))

    histogram = RecordingHistogram()
    monkeypatch.setattr(
        performance,
        "_phase_histogram",
        histogram,
        raising=False,
    )

    app = FastAPI()
    install_http_middleware(app)

    @app.get("/stream")
    async def stream_route():
        async def body():
            assert performance.record_timing("handler", 8) is True
            yield b"ok"

        return StreamingResponse(body())

    with TestClient(app) as client:
        response = client.get("/stream")

    assert response.status_code == 200
    assert response.headers.get("server-timing") is None
    assert histogram.records == [(8.0, {"phase": "handler"})]
    assert performance.record_timing("handler", 1) is False


def test_cache_metric_reaches_local_exporter_through_shared_router_without_identity_labels(
    tmp_path,
    monkeypatch,
):
    from integration.observability import performance
    from integration.observability.exporters.local.exporter import LocalPerfExporter
    from integration.observability.otel import force_flush_runtime_metrics
    from integration.observability.workspace_router import WorkspaceObservabilityRouter
    from workspace.workspaces import workspace_scope

    log_dir = tmp_path / "observability"
    local_exporter = LocalPerfExporter(
        {"local": {"retention_days": 30}},
        log_dir=log_dir,
    )
    router = WorkspaceObservabilityRouter()
    monkeypatch.setattr(
        router,
        "_build_exporter",
        lambda _workspace_id: local_exporter,
    )

    try:
        with workspace_scope("workspace_demo"):
            assert performance.record_cache_event("workspace_catalog", "hit") is True
        assert force_flush_runtime_metrics(router.router_id) is True
    finally:
        router.close()

    records = [
        json.loads(line)
        for path in log_dir.glob("otel_trace-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    metric = next(
        record
        for record in records
        if record.get("name") == "agent.cache.event.count"
    )
    assert metric["value"] == 1
    assert metric["labels"] == {
        "cache": "workspace_catalog",
        "result": "hit",
    }
    assert "workspace_id" not in metric
    assert "run_id" not in metric
    assert "user_id" not in metric


def test_local_metric_projection_rejects_unbounded_cache_and_phase_values():
    from integration.observability.otel_data import metric_record

    class SyntheticPoint:
        attributes = {
            "cache": "workspace_secret_value",
            "phase": "route_secret_value",
            "result": "hit",
        }
        time_unix_nano = 1_000_000
        value = 1

    record = metric_record(
        name="agent.cache.event.count",
        point=SyntheticPoint(),
        scope_name="onto.consumer.performance",
    )

    assert record["labels"] == {"result": "hit"}


def test_scoped_metric_failure_falls_back_without_affecting_cache_access(
    monkeypatch,
):
    from integration.observability import otel, performance

    class RecordingCounter:
        def __init__(self):
            self.records = []

        def add(self, value, attributes):
            self.records.append((value, dict(attributes)))

    counter = RecordingCounter()
    monkeypatch.setattr(performance, "_cache_counter", counter)
    monkeypatch.setattr(
        performance,
        "_resolve_metric_route",
        lambda _workspace_id: ("router-demo", "workspace_demo"),
    )
    monkeypatch.setattr(
        otel,
        "record_cache_performance_metric",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            RuntimeError("synthetic metric route failure")
        ),
    )

    assert performance.record_cache_event("workspace_catalog", "hit") is True
    assert counter.records == [
        (1, {"cache": "workspace_catalog", "result": "hit"})
    ]


def _http_scope(path: str) -> dict:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("localhost", 80),
    }


async def _empty_receive() -> dict:
    return {"type": "http.request", "body": b"", "more_body": False}


async def _discard_send(_message: dict) -> None:
    return None


def _server_timing_from_messages(messages: list[dict]) -> str:
    start = next(
        message
        for message in messages
        if message["type"] == "http.response.start"
    )
    headers = {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in start["headers"]
    }
    return headers.get("server-timing", "")
