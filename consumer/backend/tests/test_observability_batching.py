from __future__ import annotations

from internal_access import internal
import inspect
import json
import threading
import time
from contextlib import contextmanager
from typing import Any

from integration.observability import RuntimeEvent, RuntimeEventType


def _event(index: int) -> RuntimeEvent:
    return RuntimeEvent(
        type=RuntimeEventType.LLM_FINISHED,
        run_id=f"run_synthetic_{index}",
        session_id=f"session_synthetic_{index}",
        payload={"status": "success"},
    )


def _otel_record(index: int) -> dict[str, Any]:
    return {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": "span",
        "ts_ms": 4102444800000 + index,
        "run_id": f"run_synthetic_{index}",
        "span_id": f"{index:016x}",
    }


def test_batch_span_processor_defaults_and_shutdown_budget(monkeypatch):
    from integration.observability import otel

    for name in (
        "OTEL_BSP_MAX_QUEUE_SIZE",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_BSP_SCHEDULE_DELAY",
        "OTEL_BSP_EXPORT_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    assert internal(otel).batch_span_processor_options() == {
        "max_queue_size": 2048,
        "max_export_batch_size": 256,
        "schedule_delay_millis": 300,
        "export_timeout_millis": 5000,
    }

    from integration.observability.bus import ObservabilityBus

    assert inspect.signature(ObservabilityBus.close).parameters["timeout"].default == 5.0


def test_batch_span_processor_validates_operational_overrides(monkeypatch, caplog):
    from integration.observability import otel

    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "1024")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "128")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "250")
    monkeypatch.setenv("OTEL_BSP_EXPORT_TIMEOUT", "4000")
    assert internal(otel).batch_span_processor_options() == {
        "max_queue_size": 1024,
        "max_export_batch_size": 128,
        "schedule_delay_millis": 250,
        "export_timeout_millis": 4000,
    }

    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "128")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "512")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "invalid")
    monkeypatch.setenv("OTEL_BSP_EXPORT_TIMEOUT", "0")
    assert internal(otel).batch_span_processor_options() == {
        "max_queue_size": 128,
        "max_export_batch_size": 128,
        "schedule_delay_millis": 300,
        "export_timeout_millis": 5000,
    }
    caplog.clear()
    monkeypatch.setenv("OTEL_BSP_MAX_QUEUE_SIZE", "999999999")
    monkeypatch.setenv("OTEL_BSP_MAX_EXPORT_BATCH_SIZE", "999999999")
    monkeypatch.setenv("OTEL_BSP_SCHEDULE_DELAY", "999999999")
    monkeypatch.setenv("OTEL_BSP_EXPORT_TIMEOUT", "999999999")
    assert internal(otel).batch_span_processor_options() == {
        "max_queue_size": 65536,
        "max_export_batch_size": 4096,
        "schedule_delay_millis": 5000,
        "export_timeout_millis": 5000,
    }
    option_names = (
        "OTEL_BSP_MAX_QUEUE_SIZE",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_BSP_SCHEDULE_DELAY",
        "OTEL_BSP_EXPORT_TIMEOUT",
    )
    assert all(name in caplog.text for name in option_names)


def test_batch_span_processor_factory_builds_a_real_bounded_processor(monkeypatch):
    from integration.observability import otel
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SpanExporter, SpanExportResult

    for name in (
        "OTEL_BSP_MAX_QUEUE_SIZE",
        "OTEL_BSP_MAX_EXPORT_BATCH_SIZE",
        "OTEL_BSP_SCHEDULE_DELAY",
        "OTEL_BSP_EXPORT_TIMEOUT",
    ):
        monkeypatch.delenv(name, raising=False)

    class Exporter(SpanExporter):
        def export(self, spans):
            del spans
            return SpanExportResult.SUCCESS

    processor = internal(otel).create_batch_span_processor(Exporter())
    try:
        assert isinstance(processor, BatchSpanProcessor)
        state = internal(processor).batch_processor
        assert internal(state).max_queue_size == 2048
        assert internal(state).max_export_batch_size == 256
        assert internal(state).schedule_delay_millis == 300
        assert internal(state).export_timeout_millis == 5000
        assert processor.shutdown_timeout_millis == 5000
    finally:
        processor.shutdown()


def test_batch_span_processor_shutdown_is_bounded_for_blocking_exporter():
    from integration.observability import otel
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanExporter, SpanExportResult

    entered = threading.Event()
    release = threading.Event()

    class BlockingExporter(SpanExporter):
        def export(self, spans):
            del spans
            entered.set()
            release.wait(timeout=2.0)
            return SpanExportResult.SUCCESS

    processor = internal(otel).create_batch_span_processor(
        BlockingExporter(),
        env={
            "OTEL_BSP_MAX_QUEUE_SIZE": "1",
            "OTEL_BSP_MAX_EXPORT_BATCH_SIZE": "1",
            "OTEL_BSP_SCHEDULE_DELAY": "1",
            "OTEL_BSP_EXPORT_TIMEOUT": "50",
        },
    )
    provider = TracerProvider(shutdown_on_exit=False)
    provider.add_span_processor(processor)
    provider.get_tracer("synthetic.test").start_span("synthetic.span").end()
    assert entered.wait(timeout=1.0)

    started = time.monotonic()
    processor.shutdown()
    elapsed = time.monotonic() - started
    release.set()
    internal(internal(processor).batch_processor).worker_thread.join(timeout=1.0)

    assert processor.shutdown_timeout_millis == 50
    assert elapsed < 0.5


def test_indexed_local_store_append_many_uses_one_file_lock(tmp_path, monkeypatch):
    from integration.observability import local_storage

    lock_entries = 0

    @contextmanager
    def counting_lock(_path):
        nonlocal lock_entries
        lock_entries += 1
        yield

    monkeypatch.setattr(local_storage, "local_file_lock", counting_lock)
    store = local_storage.IndexedLocalOTelStore(
        tmp_path,
        retention_days=30,
        max_bytes=1024 * 1024,
    )
    records = [_otel_record(1), _otel_record(2), _otel_record(3)]

    store.append_many(records)

    assert lock_entries == 1
    data_paths = list(tmp_path.glob("otel_trace-*.log"))
    assert len(data_paths) == 1
    assert [json.loads(line) for line in data_paths[0].read_text(encoding="utf-8").splitlines()] == records
    assert len(local_storage.read_or_rebuild_index(data_paths[0])) == 3


def test_local_exporter_export_batch_uses_store_batch_path(tmp_path):
    from integration.observability.exporters.local.exporter import LocalPerfExporter

    class RecordingStore:
        def __init__(self) -> None:
            self.single_records: list[dict[str, Any]] = []
            self.batches: list[list[dict[str, Any]]] = []

        def append(self, record):
            self.single_records.append(record)

        def append_many(self, records):
            self.batches.append(list(records))

    exporter = LocalPerfExporter({"local": {}}, log_dir=tmp_path)
    store = RecordingStore()
    internal(exporter).store = store
    records = [_otel_record(1), _otel_record(2)]
    events = [
        RuntimeEvent(
            type="otel.span.exported",
            run_id=record["run_id"],
            payload={"otel_record": record},
        )
        for record in records
    ]

    exporter.export_batch(events)

    assert store.single_records == []
    assert store.batches == [records]


def test_workspace_fanout_prefers_batch_and_isolates_exporter_failures():
    from integration.observability.bus import BatchExportOutcome
    from integration.observability.workspace_router import _WorkspaceExporterFanout

    events = [_event(1), _event(2)]

    class FailingBatchExporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export_batch(self, batch):
            list(batch)
            raise RuntimeError("synthetic batch failure")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            raise AssertionError(f"batch path was not used for {event.run_id}")

    class BatchExporter:
        def __init__(self) -> None:
            self.batches: list[list[RuntimeEvent]] = []
            self.single_events: list[RuntimeEvent] = []

        def export_batch(self, batch):
            self.batches.append(list(batch))

        def export(self, event):
            self.single_events.append(event)

    class LegacyExporter:
        def __init__(self) -> None:
            self.events: list[RuntimeEvent] = []

        def export(self, event):
            self.events.append(event)

    batch_exporter = BatchExporter()
    legacy_exporter = LegacyExporter()
    fanout = _WorkspaceExporterFanout([FailingBatchExporter(), batch_exporter, legacy_exporter])

    outcome = fanout.export_batch(iter(events))

    assert batch_exporter.batches == [events]
    assert batch_exporter.single_events == []
    assert legacy_exporter.events == events
    assert outcome == BatchExportOutcome(
        succeeded_deliveries=3,
        failed_deliveries=1,
    )
    assert outcome.result == "partial_failure"


def test_bus_prefers_export_batch_and_reports_deterministic_queue_stats():
    from integration.observability.bus import ObservabilityBus

    completed = threading.Event()

    class BatchExporter:
        metric_name = "local"

        def __init__(self) -> None:
            self.batches: list[list[RuntimeEvent]] = []
            self.single_events: list[RuntimeEvent] = []

        def export_batch(self, batch):
            self.batches.append(list(batch))

        def export(self, event):
            self.single_events.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    exporter = BatchExporter()
    bus = ObservabilityBus(exporter)
    events = [_event(1), _event(2)]
    try:
        assert bus.emit_batch(lambda: iter(events), on_complete=completed.set) is True
        assert completed.wait(timeout=1.0)
    finally:
        bus.close()

    assert exporter.batches == [events]
    assert exporter.single_events == []
    assert bus.stats_snapshot() == {
        "accepted": 1,
        "completed": 1,
        "exported": 1,
        "failed": 0,
        "dropped": 0,
        "current_depth": 0,
        "peak_depth": 1,
        "shutdown_drain_timeout": 0,
    }


def test_bus_marks_partially_failed_fanout_batch_as_failed():
    from integration.observability.bus import ObservabilityBus
    from integration.observability.workspace_router import _WorkspaceExporterFanout

    completed = threading.Event()

    class FailingExporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export_batch(self, batch):
            list(batch)
            raise RuntimeError("synthetic exporter failure")

    class SuccessfulExporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export_batch(self, batch):
            list(batch)

    fanout = _WorkspaceExporterFanout([FailingExporter(), SuccessfulExporter()])
    bus = ObservabilityBus(fanout)
    try:
        assert bus.emit_batch(lambda: iter([_event(1)]), on_complete=completed.set)
        assert completed.wait(timeout=1.0)
    finally:
        bus.close()

    stats = bus.stats_snapshot()
    assert stats["accepted"] == 1
    assert stats["completed"] == 1
    assert stats["exported"] == 0
    assert stats["failed"] == 1


def test_bus_batch_fallback_isolates_individual_export_failures():
    from integration.observability.bus import ObservabilityBus

    completed = threading.Event()

    class LegacyExporter:
        def __init__(self) -> None:
            self.events: list[RuntimeEvent] = []

        def export(self, event):
            if event.run_id == "run_synthetic_1":
                raise RuntimeError("synthetic event failure")
            self.events.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    exporter = LegacyExporter()
    bus = ObservabilityBus(exporter)
    try:
        assert bus.emit_batch(
            lambda: iter([_event(1), _event(2)]),
            on_complete=completed.set,
        )
        assert completed.wait(timeout=1.0)
    finally:
        bus.close()

    assert [event.run_id for event in exporter.events] == ["run_synthetic_2"]


def test_bus_drop_and_shutdown_drain_timeout_stats_are_deterministic():
    from integration.observability.bus import ObservabilityBus

    entered = threading.Event()
    release = threading.Event()

    class BlockingExporter:
        metric_name = "local"

        def __init__(self) -> None:
            self.events: list[RuntimeEvent] = []

        def export(self, event):
            entered.set()
            assert release.wait(timeout=2.0)
            self.events.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    exporter = BlockingExporter()
    bus = ObservabilityBus(exporter, queue_size=1, batch_queue_size=1)
    assert bus.emit(_event(1)) is True
    assert entered.wait(timeout=1.0)
    assert bus.emit(_event(2)) is True
    assert bus.emit(_event(3)) is False

    before_close = bus.stats_snapshot()
    assert before_close["accepted"] == 2
    assert before_close["dropped"] == 1
    assert before_close["current_depth"] == 1
    assert before_close["peak_depth"] == 1

    bus.close(timeout=0.01)
    assert bus.stats_snapshot()["shutdown_drain_timeout"] == 1
    release.set()
    deadline = time.monotonic() + 1.0
    while len(exporter.events) < 2 and time.monotonic() < deadline:
        time.sleep(0.01)

    final = bus.stats_snapshot()
    assert len(exporter.events) == 2
    assert final["accepted"] == 2
    assert final["completed"] == 2
    assert final["exported"] == 2
    assert final["failed"] == 0
    assert final["dropped"] == 1
    assert final["current_depth"] == 0
    assert final["shutdown_drain_timeout"] == 1


def test_gauge_single_flight_keeps_emit_bounded_and_converges_after_perturbation():
    from integration.observability.bus import ObservabilityBus

    blocked = threading.Event()
    release = threading.Event()
    depth_values: list[int] = []
    first_depth = True

    def recorder(name, value, **_labels):
        nonlocal first_depth
        if name != "current_depth":
            return
        if first_depth:
            first_depth = False
            blocked.set()
            assert release.wait(timeout=2.0)
        depth_values.append(int(value))

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    bus = ObservabilityBus(
        Exporter(),
        metric_recorder=recorder,
        queue_size=128,
    )
    first_result: list[bool] = []
    emitter = threading.Thread(target=lambda: first_result.append(bus.emit(_event(1))))
    emitter.start()
    try:
        assert blocked.wait(timeout=1.0)
        emitter.join(timeout=0.1)
        first_emit_was_bounded = not emitter.is_alive()

        started = time.monotonic()
        results = [bus.emit(_event(index)) for index in range(2, 52)]
        perturbation_elapsed = time.monotonic() - started
    finally:
        release.set()
        emitter.join(timeout=1.0)
        bus.close()

    assert first_emit_was_bounded
    assert perturbation_elapsed < 0.2
    assert first_result == [True]
    assert all(results)
    stats = bus.stats_snapshot()
    assert stats["accepted"] == 51
    assert stats["completed"] == 51
    assert stats["dropped"] == 0
    assert stats["current_depth"] == 0
    assert depth_values[-1] == 0


def test_batch_metric_labels_are_low_cardinality(monkeypatch):
    from integration.observability import otel

    recorded: list[tuple[str, float, dict[str, str]]] = []

    class Instrument:
        def __init__(self, name: str, kind: str) -> None:
            self.name = name
            self.kind = kind

        def add(self, value, attributes):
            recorded.append((self.name, value, dict(attributes)))

        def record(self, value, attributes):
            recorded.append((self.name, value, dict(attributes)))

        def set(self, value, attributes):
            recorded.append((self.name, value, dict(attributes)))

    class Meter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_counter(self, name, **_kwargs):
            return Instrument(name, "counter")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_histogram(self, name, **_kwargs):
            return Instrument(name, "histogram")

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def create_gauge(self, name, **_kwargs):
            return Instrument(name, "gauge")

    instruments = internal(otel).create_metric_instruments(Meter())
    monkeypatch.setattr(otel, "_get_metric_instruments", lambda _router_id="": instruments)

    for metric_name in (
        "accepted",
        "completed",
        "exported",
        "failed",
        "dropped",
        "current_depth",
        "peak_depth",
        "shutdown_drain_timeout",
    ):
        otel.record_observability_batch_metric(
            metric_name,
            1,
            exporter="workspace_demo",
            result="run_synthetic_1",
        )

    assert instruments["observability.current_depth"].kind == "gauge"
    assert instruments["observability.peak_depth"].kind == "gauge"
    assert len(recorded) == 8
    for _name, _value, labels in recorded:
        assert set(labels) == {"exporter", "result"}
        assert labels == {"exporter": "custom", "result": "unknown"}
        assert not {"workspace", "user", "run", "session"} & set(labels)


def test_batch_metrics_route_out_of_band_and_persist_low_cardinality_labels(
    tmp_path,
    monkeypatch,
):
    from integration.observability.bus import ObservabilityBus
    from integration.observability.exporters.local.exporter import LocalPerfExporter
    from integration.observability.otel import (
        force_flush_runtime_metrics,
        record_observability_batch_metric,
        shutdown_runtime_metrics,
    )
    from integration.observability.workspace_router import WorkspaceObservabilityRouter

    router = WorkspaceObservabilityRouter()
    exporter = LocalPerfExporter({"local": {}}, log_dir=tmp_path)
    bus = ObservabilityBus(
        exporter,
        router_id=router.router_id,
        workspace_id="workspace_demo",
    )
    monkeypatch.setattr(router, "_get_observer", lambda *_args, **_kwargs: bus)
    try:
        record_observability_batch_metric(
            "accepted",
            1,
            exporter="local",
            result="success",
            router_id=router.router_id,
            workspace_id="workspace_demo",
        )
        record_observability_batch_metric(
            "current_depth",
            3,
            exporter="local",
            result="snapshot",
            router_id=router.router_id,
            workspace_id="workspace_demo",
        )
        assert force_flush_runtime_metrics(router.router_id)
    finally:
        shutdown_runtime_metrics(router.router_id)
        bus.close()
        router.close()

    records = [
        json.loads(line)
        for path in tmp_path.glob("otel_trace-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    metric_records = [record for record in records if record.get("kind") == "metric"]
    assert {record["name"] for record in metric_records} >= {
        "agent.observability.export.accepted",
        "agent.observability.queue.current_depth",
    }
    for record in metric_records:
        assert record["labels"] in (
            {"exporter": "local", "result": "success"},
            {"exporter": "local", "result": "snapshot"},
        )
        assert "workspace_id" not in record


def test_batch_metrics_share_one_provider_and_reader_per_router():
    from integration.observability import otel
    from integration.observability.workspace_router import WorkspaceObservabilityRouter

    router = WorkspaceObservabilityRouter()
    workspace_ids = [f"workspace_synthetic_{index:03d}" for index in range(50)]
    try:
        for workspace_id in workspace_ids:
            assert internal(otel).get_batch_metric_instruments(
                router_id=router.router_id,
                workspace_id=workspace_id,
            )

        recorders = [
            recorder
            for key, recorder in internal(otel).batch_metric_recorders.items()
            if key == router.router_id or (isinstance(key, tuple) and key[0] == router.router_id)
        ]
        assert len(recorders) == 1
        assert len({id(internal(recorder).provider) for recorder in recorders}) == 1
        assert len({id(internal(recorder).reader) for recorder in recorders}) == 1
        recorder = recorders[0]
        assert len(internal(recorder).workspace_by_scope) == 50
        workspace_by_scope = internal(recorder).workspace_by_scope
        assert all(
            workspace_id not in scope_name
            for scope_name, workspace_id in workspace_by_scope.items()
        )
    finally:
        otel.shutdown_runtime_metrics(router.router_id)
        router.close()


def test_batch_metric_shutdown_timeout_retains_recorder_for_retry(monkeypatch):
    from integration.observability import otel

    router_id = "router_synthetic_retry"

    class Recorder:
        def __init__(self) -> None:
            self.results = [False, True]
            self.calls = 0

        def shutdown(self, *, timeout_millis):
            assert timeout_millis > 0
            self.calls += 1
            return self.results.pop(0)

    recorder = Recorder()
    recorders = {router_id: recorder}
    monkeypatch.setattr(otel, "_batch_metric_recorders", recorders)
    monkeypatch.setattr(otel, "_router_metric_recorders", {})

    assert otel.shutdown_runtime_metrics(router_id, timeout_millis=50) is False
    assert recorders[router_id] is recorder
    assert otel.shutdown_runtime_metrics(router_id, timeout_millis=50) is True
    assert router_id not in recorders
    assert recorder.calls == 2


def test_router_metric_recorders_propagate_provider_shutdown_failure():
    from integration.observability import otel

    class Provider:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def shutdown(self, *, timeout_millis):
            assert timeout_millis == 50
            return False

    for recorder_type in (internal(otel).RouterMetricRecorder, internal(otel).RouterBatchMetricRecorder):
        recorder = object.__new__(recorder_type)
        internal(recorder).provider = Provider()

        assert recorder.shutdown(timeout_millis=50) is False


def test_metric_shutdown_retains_blocked_reader_until_retry(monkeypatch):
    from integration.observability import otel
    from integration.observability.workspace_router import WorkspaceObservabilityRouter

    entered = threading.Event()
    release = threading.Event()
    router = WorkspaceObservabilityRouter()

    def blocked_export(*_args, **_kwargs):
        entered.set()
        assert release.wait(timeout=2.0)

    monkeypatch.setattr(otel, "_metric_export_interval_ms", lambda: 1)
    monkeypatch.setattr(router, "export_otel_metric", blocked_export)
    otel.record_observability_batch_metric(
        "accepted",
        1,
        exporter="local",
        result="success",
        router_id=router.router_id,
        workspace_id="workspace_synthetic_blocked_reader",
    )
    recorder = internal(otel).batch_metric_recorders[router.router_id]
    reader_thread = internal(internal(recorder).reader).daemon_thread
    assert entered.wait(timeout=1.0)

    first_result = otel.shutdown_runtime_metrics(
        router.router_id,
        timeout_millis=50,
    )
    retained_after_timeout = internal(otel).batch_metric_recorders.get(router.router_id) is recorder
    alive_after_timeout = reader_thread is not None and reader_thread.is_alive()

    release.set()
    if reader_thread is not None:
        reader_thread.join(timeout=1.0)
    second_result = otel.shutdown_runtime_metrics(
        router.router_id,
        timeout_millis=50,
    )
    router.close(timeout=1.0)

    assert first_result is False
    assert retained_after_timeout is True
    assert alive_after_timeout is True
    assert second_result is True
    assert router.router_id not in internal(otel).batch_metric_recorders


def test_metric_shutdown_retries_only_recorders_that_failed(monkeypatch):
    from integration.observability import otel

    router_id = "router_synthetic_partial_retry"

    class Recorder:
        def __init__(self, results):
            self.results = list(results)
            self.calls = 0

        def shutdown(self, *, timeout_millis):
            assert timeout_millis > 0
            self.calls += 1
            return self.results.pop(0)

    runtime_recorder = Recorder([True])
    batch_recorder = Recorder([False, True])
    runtime_recorders = {router_id: runtime_recorder}
    batch_recorders = {router_id: batch_recorder}
    monkeypatch.setattr(otel, "_router_metric_recorders", runtime_recorders)
    monkeypatch.setattr(otel, "_batch_metric_recorders", batch_recorders)

    assert otel.shutdown_runtime_metrics(router_id, timeout_millis=50) is False
    assert router_id not in runtime_recorders
    assert batch_recorders[router_id] is batch_recorder

    assert otel.shutdown_runtime_metrics(router_id, timeout_millis=50) is True
    assert router_id not in batch_recorders
    assert runtime_recorder.calls == 1
    assert batch_recorder.calls == 2


def test_workspace_router_close_drains_bus_before_metric_shutdown(monkeypatch):
    from integration.observability import otel
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.bus import ObservabilityBus
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    workspace_id = "workspace_synthetic_close_gate"
    gauge_entered = threading.Event()
    release_gauge = threading.Event()
    gauge_completed = threading.Event()
    bus_close_entered = threading.Event()
    shutdown_called = threading.Event()
    close_errors: list[BaseException] = []
    close_results: list[bool] = []
    shutdown_before_gauge_completion: list[bool] = []
    close_thread = None

    def gated_recorder(name, value, **labels):
        if name == "current_depth":
            gauge_entered.set()
            assert release_gauge.wait(timeout=10.0)
        otel.record_observability_batch_metric(name, value, **labels)
        if name == "current_depth":
            gauge_completed.set()

    class Exporter:
        metric_name = "local"

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    router = WorkspaceObservabilityRouter()
    try:
        otel.record_observability_batch_metric(
            "accepted",
            1,
            exporter="local",
            result="success",
            router_id=router.router_id,
            workspace_id=workspace_id,
        )
        initial_recorder = internal(otel).batch_metric_recorders[router.router_id]
        initial_reader_thread = internal(internal(initial_recorder).reader).daemon_thread

        bus = ObservabilityBus(
            Exporter(),
            metric_recorder=gated_recorder,
            router_id=router.router_id,
            workspace_id=workspace_id,
            flush_interval_seconds=10,
        )
        internal(router).observers_by_workspace[workspace_id] = bus
        real_bus_close = bus.close
        real_shutdown = workspace_router_module.shutdown_runtime_metrics

        def tracked_bus_close(*args, **kwargs):
            bus_close_entered.set()
            return real_bus_close(*args, **kwargs)

        def tracked_shutdown(*args, **kwargs):
            shutdown_before_gauge_completion.append(not gauge_completed.is_set())
            shutdown_called.set()
            return real_shutdown(*args, **kwargs)

        monkeypatch.setattr(bus, "close", tracked_bus_close)
        monkeypatch.setattr(
            workspace_router_module,
            "force_flush_open_telemetry",
            lambda *args, **kwargs: True,
        )
        monkeypatch.setattr(
            workspace_router_module,
            "force_flush_runtime_metrics",
            lambda *args, **kwargs: True,
        )
        monkeypatch.setattr(workspace_router_module, "shutdown_runtime_metrics", tracked_shutdown)

        assert bus.emit(_event(901)) is True
        assert gauge_entered.wait(timeout=5.0)

        def close_router():
            try:
                close_results.append(router.close(timeout=5.0))
            except BaseException as exc:  # pragma: no cover - surfaced below
                close_errors.append(exc)

        close_thread = threading.Thread(target=close_router)
        close_thread.start()
        assert bus_close_entered.wait(timeout=5.0)
        assert not shutdown_called.is_set()
        release_gauge.set()
        close_thread.join(timeout=5.0)

        assert not close_thread.is_alive()
        assert close_errors == []
        assert close_results == [True]
        assert shutdown_before_gauge_completion == [False]
        assert shutdown_called.is_set()
        assert resolve_workspace_router(router.router_id) is None
        assert router.router_id not in internal(otel).router_metric_recorders
        assert router.router_id not in internal(otel).batch_metric_recorders
        assert initial_reader_thread is None or not initial_reader_thread.is_alive()
    finally:
        release_gauge.set()
        if close_thread is not None and close_thread.ident is not None:
            close_thread.join(timeout=5.0)
        assert router.close(timeout=5.0) is True, "Test router cleanup did not complete"


def test_repeated_observer_reset_does_not_accumulate_metric_readers(monkeypatch):
    from integration.observability import otel
    from integration.observability import provider as provider_module
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    monkeypatch.setattr(provider_module, "_observer", None)
    baseline_reader_threads = {
        id(thread) for thread in threading.enumerate() if thread.name == "OtelPeriodicExportingMetricReader"
    }

    for index in range(3):
        router = WorkspaceObservabilityRouter()
        workspace_id = f"workspace_synthetic_reset_{index}"
        otel.record_observability_batch_metric(
            "accepted",
            1,
            exporter="local",
            result="success",
            router_id=router.router_id,
            workspace_id=workspace_id,
        )
        recorder = internal(otel).batch_metric_recorders[router.router_id]
        reader_thread = internal(internal(recorder).reader).daemon_thread
        with internal(provider_module).observer_lock:
            internal(provider_module).observer = router

        provider_module.reset_observer(timeout=1.0)

        assert resolve_workspace_router(router.router_id) is None
        assert router.router_id not in internal(otel).router_metric_recorders
        assert router.router_id not in internal(otel).batch_metric_recorders
        assert reader_thread is None or not reader_thread.is_alive()

    remaining_reader_threads = {
        id(thread) for thread in threading.enumerate() if thread.name == "OtelPeriodicExportingMetricReader"
    }
    assert remaining_reader_threads == baseline_reader_threads


def test_workspace_router_zero_budget_unregisters_without_metric_resources():
    from integration.observability import otel
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    router = WorkspaceObservabilityRouter()

    router.close(timeout=0)

    assert resolve_workspace_router(router.router_id) is None
    assert router.router_id not in internal(otel).router_metric_recorders
    assert router.router_id not in internal(otel).batch_metric_recorders


def test_workspace_router_failed_metric_shutdown_is_retryable_without_recreation(
    monkeypatch,
):
    from integration.observability import otel
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    shutdown_results = [False, True]
    shutdown_calls: list[int] = []

    def shutdown_metrics(_router_id, *, timeout_millis):
        shutdown_calls.append(timeout_millis)
        return shutdown_results.pop(0)

    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_open_telemetry",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_runtime_metrics",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "shutdown_runtime_metrics",
        shutdown_metrics,
    )
    router = WorkspaceObservabilityRouter()

    assert router.close(timeout=1.0) is False

    assert resolve_workspace_router(router.router_id) is router
    assert router.accepts_new_metric_recorders is False
    assert (
        internal(otel).get_batch_metric_instruments(
            router_id=router.router_id,
            workspace_id="workspace_synthetic_shutdown_retry",
        )
        == {}
    )
    assert router.router_id not in internal(otel).batch_metric_recorders

    assert router.close(timeout=1.0) is True

    assert resolve_workspace_router(router.router_id) is None
    assert len(shutdown_calls) == 2
    assert all(timeout_millis > 0 for timeout_millis in shutdown_calls)


def test_dispatcher_and_provider_retry_incomplete_observer_shutdown(monkeypatch):
    from integration.observability import provider as provider_module
    from integration.observability.dispatcher import DispatchSink, RuntimeEventDispatcher

    class RetryObserver:
        def __init__(self):
            self.close_calls = 0

        def close(self, timeout=1.0):
            del timeout
            self.close_calls += 1
            return self.close_calls > 1

    observer = RetryObserver()
    dispatcher = RuntimeEventDispatcher([DispatchSink(observer)])

    assert dispatcher.close(timeout=0.1) is False
    assert dispatcher.close(timeout=0.1) is True

    observer = RetryObserver()
    monkeypatch.setattr(provider_module, "_observer", observer)
    monkeypatch.setattr(provider_module, "_retiring_observers", [], raising=False)

    assert provider_module.shutdown_observer(timeout=0.1) is False
    assert observer.close_calls == 1
    assert provider_module.shutdown_observer(timeout=0.1) is True
    assert observer.close_calls == 2


def test_provider_shutdown_shares_one_deadline_across_retry_queue(monkeypatch):
    from integration.observability import provider as provider_module

    observed_timeouts = []

    class Observer:
        def __init__(self, *, delay=0.0):
            self.delay = delay

        def close(self, timeout=1.0):
            observed_timeouts.append(timeout)
            if self.delay:
                time.sleep(min(self.delay, timeout))
            return False

    first = Observer(delay=0.03)
    second = Observer()
    monkeypatch.setattr(provider_module, "_retiring_observers", [first])
    monkeypatch.setattr(provider_module, "_observer", second)

    assert provider_module.shutdown_observer(timeout=0.05) is False
    assert len(observed_timeouts) == 2
    assert observed_timeouts[0] <= 0.051
    assert observed_timeouts[1] < 0.04


def test_workspace_router_close_preserves_buffered_span_flush(monkeypatch):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.otel_data import OTelSpanSnapshot
    from integration.observability.workspace_router import WorkspaceObservabilityRouter

    exported: list[RuntimeEvent] = []

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            exported.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    router = WorkspaceObservabilityRouter()
    run_id = "run_synthetic_close_flush"
    workspace_id = "workspace_synthetic_close_flush"

    def build_exporter(_workspace_id):
        return Exporter()

    internal(router).build_exporter = build_exporter
    internal(router).run_workspaces[run_id] = workspace_id
    internal(router).pending_spans_by_run[run_id] = [
        OTelSpanSnapshot(
            name="agent.run",
            trace_id="1" * 32,
            span_id="2" * 16,
            parent_span_id=None,
            start_ms=100,
            end_ms=200,
            status="ok",
            attributes={
                "agent.run_id": run_id,
                "agent.workspace_id": workspace_id,
            },
            events=(),
            scope_name="test",
        )
    ]
    internal(router).pending_span_count = 1
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_open_telemetry",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_runtime_metrics",
        lambda *args, **kwargs: True,
    )

    router.close(timeout=1.0)

    assert [event.type for event in exported] == ["otel.span.exported"]
    assert internal(router).pending_span_count == 0


def test_workspace_router_pending_span_saturation_exports_bounded_metrics(
    monkeypatch,
):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.otel_data import OTelSpanSnapshot
    from integration.observability.workspace_router import WorkspaceObservabilityRouter

    recorded = []
    monkeypatch.setattr(
        workspace_router_module,
        "record_observability_batch_metric",
        lambda name, value, **labels: recorded.append((name, value, labels)),
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_open_telemetry",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_runtime_metrics",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "shutdown_runtime_metrics",
        lambda *args, **kwargs: True,
    )
    router = WorkspaceObservabilityRouter(
        max_pending_spans_per_run=3,
        max_pending_spans_total=2,
    )
    try:
        for index in range(3):
            internal(router).buffer_pending_span(
                OTelSpanSnapshot(
                    name=f"agent.tool.{index}",
                    trace_id="1" * 32,
                    span_id=f"{index + 1:016x}",
                    parent_span_id=None,
                    start_ms=100 + index,
                    end_ms=200 + index,
                    status="ok",
                    attributes={
                        "agent.run_id": "run-saturation",
                        "agent.workspace_id": "workspace-saturation",
                    },
                    events=(),
                    scope_name="test",
                )
            )

        assert router.stats_snapshot() == {
            "pending_spans": 2,
            "pending_runs": 1,
            "pending_span_dropped": 1,
            "pending_span_peak": 2,
            "max_pending_spans_per_run": 3,
            "max_pending_spans_total": 2,
        }
        assert any(
            name == "dropped"
            and value == 1
            and labels["exporter"] == "otel_pending_spans"
            and labels["result"] == "queue_full"
            for name, value, labels in recorded
        )
        assert any(
            name == "current_depth"
            and value == 2
            and labels["exporter"] == "otel_pending_spans"
            and labels["result"] == "snapshot"
            for name, value, labels in recorded
        )
    finally:
        router.close(timeout=1.0)


def test_workspace_router_close_preserves_span_arriving_during_force_flush(
    monkeypatch,
):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.otel_data import OTelSpanSnapshot
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    exported: list[RuntimeEvent] = []
    run_id = "run_synthetic_force_flush_arrival"
    workspace_id = "workspace_synthetic_force_flush_arrival"
    span = OTelSpanSnapshot(
        name="agent.run",
        trace_id="3" * 32,
        span_id="4" * 16,
        parent_span_id=None,
        start_ms=100,
        end_ms=200,
        status="ok",
        attributes={
            "agent.run_id": run_id,
            "agent.workspace_id": workspace_id,
        },
        events=(),
        scope_name="test",
    )

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            exported.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    router = WorkspaceObservabilityRouter()

    def build_exporter(_workspace_id):
        return Exporter()

    internal(router).build_exporter = build_exporter

    def flush_with_first_span(*_args, **_kwargs):
        router.export_otel_span(span)
        return True

    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_open_telemetry",
        flush_with_first_span,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_runtime_metrics",
        lambda *args, **kwargs: True,
    )

    router.close(timeout=1.0)

    assert [event.type for event in exported] == ["otel.span.exported"]
    assert internal(router).pending_span_count == 0
    assert resolve_workspace_router(router.router_id) is None


def test_workspace_router_close_budget_is_not_blocked_by_runtime_consumer(
    monkeypatch,
):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.otel_data import OTelSpanSnapshot
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        register_run_runtime_event_consumer,
        resolve_workspace_router,
    )

    exported: list[RuntimeEvent] = []
    consumed: list[RuntimeEvent] = []
    entered = threading.Event()
    release = threading.Event()
    run_id = "run_synthetic_blocked_close_consumer"
    workspace_id = "workspace_synthetic_blocked_close_consumer"

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            exported.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def flush(self):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    def blocked_consumer(event):
        consumed.append(event)
        entered.set()
        assert release.wait(timeout=2.0)

    router = WorkspaceObservabilityRouter()

    def build_exporter(_workspace_id):
        return Exporter()

    internal(router).build_exporter = build_exporter
    internal(router).run_workspaces[run_id] = workspace_id
    internal(router).pending_spans_by_run[run_id] = [
        OTelSpanSnapshot(
            name="agent.run",
            trace_id="5" * 32,
            span_id="6" * 16,
            parent_span_id=None,
            start_ms=100,
            end_ms=200,
            status="ok",
            attributes={
                "agent.run_id": run_id,
                "agent.workspace_id": workspace_id,
            },
            events=(),
            scope_name="test",
        )
    ]
    internal(router).pending_span_count = 1
    register_run_runtime_event_consumer(run_id, blocked_consumer)
    monkeypatch.setattr(
        workspace_router_module,
        "runtime_events_from_spans",
        lambda _spans: [_event(902)],
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_open_telemetry",
        lambda *args, **kwargs: True,
    )
    monkeypatch.setattr(
        workspace_router_module,
        "force_flush_runtime_metrics",
        lambda *args, **kwargs: True,
    )

    fallback_release = threading.Timer(0.3, release.set)
    fallback_release.daemon = True
    fallback_release.start()
    started = time.monotonic()
    router.close(timeout=0.05)
    elapsed = time.monotonic() - started

    try:
        assert entered.wait(timeout=0.1)
        assert elapsed < 0.15
        assert resolve_workspace_router(router.router_id) is router
        close_worker = internal(router).close_worker
        assert close_worker is not None and close_worker.is_alive()

        router.close(timeout=0.05)

        assert internal(router).close_worker is close_worker
        assert close_worker.is_alive()

        release.set()
        router.close(timeout=1.0)

        assert resolve_workspace_router(router.router_id) is None
        assert len(consumed) == 1
        assert [event.type for event in exported] == [
            "otel.span.exported",
            RuntimeEventType.LLM_FINISHED,
        ]
        assert internal(router).pending_span_count == 0
    finally:
        fallback_release.cancel()
        release.set()


def test_workspace_router_close_lock_acquisition_uses_remaining_budget():
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    router = WorkspaceObservabilityRouter()
    internal(router).close_lock.acquire()
    fallback_release = threading.Timer(0.3, internal(router).close_lock.release)
    fallback_release.daemon = True
    fallback_release.start()

    started = time.monotonic()
    router.close(timeout=0.05)
    elapsed = time.monotonic() - started

    try:
        assert elapsed < 0.15
        assert resolve_workspace_router(router.router_id) is router
    finally:
        fallback_release.cancel()
        if internal(router).close_lock.locked():
            internal(router).close_lock.release()
        router.close(timeout=1.0)


def test_observability_bus_exporter_close_failure_is_retryable():
    from integration.observability.bus import ObservabilityBus

    class Exporter:
        def __init__(self):
            self.close_calls = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            return None

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("synthetic close failure")

    exporter = Exporter()
    bus = ObservabilityBus(exporter, metric_recorder=lambda *_args, **_kwargs: None)

    assert bus.close(timeout=1.0) is False
    assert exporter.close_calls == 1
    assert bus.close(timeout=1.0) is True
    assert exporter.close_calls == 2


def test_observability_bus_close_retry_does_not_wait_on_blocked_exporter_close():
    from integration.observability.bus import ObservabilityBus

    entered = threading.Event()
    release = threading.Event()

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            return None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            entered.set()
            assert release.wait(timeout=2.0)

    bus = ObservabilityBus(
        Exporter(),
        metric_recorder=lambda *_args, **_kwargs: None,
    )
    fallback_release = threading.Timer(0.3, release.set)
    fallback_release.daemon = True
    fallback_release.start()

    try:
        assert bus.close(timeout=0.05) is False
        assert entered.wait(timeout=0.1)

        started = time.monotonic()
        assert bus.close(timeout=0.05) is False
        elapsed = time.monotonic() - started

        assert elapsed < 0.15
    finally:
        fallback_release.cancel()
        release.set()
        bus.close(timeout=1.0)


def test_observability_bus_failed_close_retry_stays_within_budget():
    from integration.observability.bus import ObservabilityBus

    retry_entered = threading.Event()
    release = threading.Event()

    class Exporter:
        def __init__(self):
            self.close_calls = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            return None

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("synthetic first close failure")
            retry_entered.set()
            assert release.wait(timeout=2.0)

    exporter = Exporter()
    bus = ObservabilityBus(exporter, metric_recorder=lambda *_args, **_kwargs: None)
    fallback_release = threading.Timer(0.3, release.set)
    fallback_release.daemon = True
    fallback_release.start()

    try:
        assert bus.close(timeout=1.0) is False

        started = time.monotonic()
        assert bus.close(timeout=0.05) is False
        elapsed = time.monotonic() - started

        assert retry_entered.wait(timeout=0.1)
        assert elapsed < 0.15
    finally:
        fallback_release.cancel()
        release.set()
        bus.close(timeout=1.0)

    assert exporter.close_calls == 2


def test_workspace_exporter_fanout_retries_only_failed_child_close():
    from integration.observability.workspace_router import _WorkspaceExporterFanout

    class Exporter:
        def __init__(self, *, fail_once=False):
            self.fail_once = fail_once
            self.close_calls = 0

        def close(self):
            self.close_calls += 1
            if self.fail_once and self.close_calls == 1:
                raise RuntimeError("synthetic child close failure")

    closed = Exporter()
    retrying = Exporter(fail_once=True)
    fanout = _WorkspaceExporterFanout([closed, retrying])

    assert fanout.close() is False
    assert (closed.close_calls, retrying.close_calls) == (1, 1)
    assert fanout.close() is True
    assert (closed.close_calls, retrying.close_calls) == (1, 2)


def test_workspace_router_retains_bus_when_exporter_close_fails(monkeypatch):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.bus import ObservabilityBus
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    workspace_id = "workspace_synthetic_close_retry"
    shutdown_calls = []

    class Exporter:
        def __init__(self):
            self.close_calls = 0

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, _event):
            return None

        def close(self):
            self.close_calls += 1
            if self.close_calls == 1:
                raise RuntimeError("synthetic close failure")

    exporter = Exporter()
    bus = ObservabilityBus(exporter, metric_recorder=lambda *_args, **_kwargs: None)
    router = WorkspaceObservabilityRouter()
    internal(router).observers_by_workspace[workspace_id] = bus
    monkeypatch.setattr(workspace_router_module, "force_flush_open_telemetry", lambda **_kwargs: True)
    monkeypatch.setattr(workspace_router_module, "force_flush_runtime_metrics", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(
        workspace_router_module,
        "shutdown_runtime_metrics",
        lambda *_args, **_kwargs: shutdown_calls.append(True) or True,
    )

    router.close(timeout=1.0)

    assert resolve_workspace_router(router.router_id) is router
    assert internal(router).observers_by_workspace[workspace_id] is bus
    assert shutdown_calls == []

    router.close(timeout=1.0)

    assert resolve_workspace_router(router.router_id) is None
    assert exporter.close_calls == 2
    assert shutdown_calls == [True]


def test_workspace_router_force_flush_failure_retains_late_span_for_retry(monkeypatch):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.otel_data import OTelSpanSnapshot
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    exported = []
    run_id = "run_synthetic_failed_force_flush"
    workspace_id = "workspace_synthetic_failed_force_flush"
    span = OTelSpanSnapshot(
        name="agent.run",
        trace_id="7" * 32,
        span_id="8" * 16,
        parent_span_id=None,
        start_ms=100,
        end_ms=200,
        status="ok",
        attributes={
            "agent.run_id": run_id,
            "agent.workspace_id": workspace_id,
        },
        events=(),
        scope_name="test",
    )

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            exported.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    router = WorkspaceObservabilityRouter()

    def build_exporter(_workspace_id):
        return Exporter()

    internal(router).build_exporter = build_exporter
    flush_results = [False, True]

    def flush_trace(**_kwargs):
        if len(flush_results) == 2:
            router.export_otel_span(span)
        return flush_results.pop(0)

    monkeypatch.setattr(workspace_router_module, "force_flush_open_telemetry", flush_trace)
    monkeypatch.setattr(workspace_router_module, "force_flush_runtime_metrics", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workspace_router_module, "shutdown_runtime_metrics", lambda *_args, **_kwargs: True)

    router.close(timeout=1.0)

    assert resolve_workspace_router(router.router_id) is router
    assert internal(router).draining_otel is True
    assert internal(router).pending_span_count == 1
    assert exported == []

    router.close(timeout=1.0)

    assert resolve_workspace_router(router.router_id) is None
    assert internal(router).pending_span_count == 0
    assert [event.type for event in exported] == ["otel.span.exported"]


def test_workspace_router_force_flush_exception_keeps_drain_gate_open(monkeypatch):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.workspace_router import (
        WorkspaceObservabilityRouter,
        resolve_workspace_router,
    )

    router = WorkspaceObservabilityRouter()
    metric_flush_calls = 0

    def flush_metrics(*_args, **_kwargs):
        nonlocal metric_flush_calls
        metric_flush_calls += 1
        if metric_flush_calls == 1:
            raise RuntimeError("synthetic metric flush failure")
        return True

    monkeypatch.setattr(workspace_router_module, "force_flush_open_telemetry", lambda **_kwargs: True)
    monkeypatch.setattr(workspace_router_module, "force_flush_runtime_metrics", flush_metrics)
    monkeypatch.setattr(workspace_router_module, "shutdown_runtime_metrics", lambda *_args, **_kwargs: True)

    router.close(timeout=1.0)

    assert resolve_workspace_router(router.router_id) is router
    assert internal(router).draining_otel is True

    router.close(timeout=1.0)

    assert resolve_workspace_router(router.router_id) is None
    assert metric_flush_calls == 2


def test_workspace_router_materializes_first_no_run_span_only_during_close_drain(
    monkeypatch,
):
    from integration.observability import workspace_router as workspace_router_module
    from integration.observability.otel_data import OTelSpanSnapshot
    from integration.observability.workspace_router import WorkspaceObservabilityRouter

    exported = []
    workspace_id = "workspace_synthetic_no_run_close"
    span = OTelSpanSnapshot(
        name="background.work",
        trace_id="9" * 32,
        span_id="a" * 16,
        parent_span_id=None,
        start_ms=100,
        end_ms=200,
        status="ok",
        attributes={"agent.workspace_id": workspace_id},
        events=(),
        scope_name="test",
    )

    class Exporter:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def export(self, event):
            exported.append(event)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def close(self):
            return None

    router = WorkspaceObservabilityRouter()
    build_calls = []

    def build_exporter(resolved_workspace_id):
        build_calls.append(resolved_workspace_id)
        return Exporter()

    internal(router).build_exporter = build_exporter

    def flush_trace(**_kwargs):
        router.export_otel_span(span)
        return True

    monkeypatch.setattr(workspace_router_module, "force_flush_open_telemetry", flush_trace)
    monkeypatch.setattr(workspace_router_module, "force_flush_runtime_metrics", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(workspace_router_module, "shutdown_runtime_metrics", lambda *_args, **_kwargs: True)

    router.close(timeout=1.0)
    router.export_otel_span(span)

    assert build_calls == [workspace_id]
    assert [event.type for event in exported] == ["otel.span.exported"]
