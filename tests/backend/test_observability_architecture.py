import logging
import threading
import time
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _reset_observability_provider_between_tests():
    from agentic.observability.provider import shutdown_observer

    shutdown_observer(timeout=1)
    yield
    shutdown_observer(timeout=1)


def test_business_layers_do_not_reference_langfuse_trace_ids():
    repo_root = Path(__file__).resolve().parents[3]
    checked_paths = [
        repo_root / "runtime/agent/agentic/orchestrator.py",
        repo_root / "runtime/agent/agentic/loop/jiuwen_adapter.py",
        repo_root / "runtime/agent/agentic/services/llm/service.py",
        repo_root / "runtime/tools/agentic/tools/base.py",
        repo_root / "runtime/tools/agentic/tools/spawn_sub_agent.py",
        repo_root / "runtime/agent/agentic/sub_agents/base.py",
        repo_root / "runtime/agent/agentic/sub_agents/manager.py",
        repo_root / "runtime/agent/agentic/utils/reasoning_summary.py",
    ]

    offenders = [
        str(path.relative_to(repo_root))
        for path in checked_paths
        if "langfuse_trace_id" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_observability_bus_emit_does_not_wait_for_exporter():
    from agentic.observability import ObservabilityBus, RuntimeEvent, RuntimeEventType

    class SlowExporter:
        def export(self, event):
            time.sleep(0.2)

        def flush(self):
            pass

    bus = ObservabilityBus(SlowExporter(), queue_size=8, flush_interval_seconds=10)
    started_at = time.perf_counter()
    try:
        accepted = bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_STARTED,
                run_id="run-1",
                session_id="session-1",
                payload={"user_query": "hello"},
            )
        )
        emit_elapsed = time.perf_counter() - started_at
    finally:
        bus.close(timeout=1)

    assert accepted is True
    assert emit_elapsed < 0.05


def test_observability_bus_close_exits_after_full_queue_drains():
    from agentic.observability import ObservabilityBus, RuntimeEvent, RuntimeEventType

    first_export_started = threading.Event()
    release_first_export = threading.Event()

    class BlockingExporter:
        def __init__(self):
            self.export_count = 0

        def export(self, event):
            del event
            self.export_count += 1
            if self.export_count == 1:
                first_export_started.set()
                release_first_export.wait(timeout=1)

        def flush(self):
            pass

    exporter = BlockingExporter()
    bus = ObservabilityBus(exporter, queue_size=1, flush_interval_seconds=10)
    try:
        assert bus.emit(RuntimeEvent(type=RuntimeEventType.RUN_STARTED, run_id="run-1")) is True
        assert first_export_started.wait(timeout=1)
        assert bus.emit(RuntimeEvent(type=RuntimeEventType.RUN_FINISHED, run_id="run-1")) is True

        bus.close(timeout=0.01)
        release_first_export.set()
        bus._worker.join(timeout=0.2)

        assert not bus._worker.is_alive()
    finally:
        release_first_export.set()
        bus.close(timeout=1)


def test_observability_bus_closes_exporter_after_worker_stops():
    from agentic.observability import ObservabilityBus

    class ClosingExporter:
        def __init__(self):
            self.closed = False

        def export(self, event):
            del event

        def flush(self):
            pass

        def close(self):
            self.closed = True

    exporter = ClosingExporter()
    bus = ObservabilityBus(exporter, queue_size=1, flush_interval_seconds=10)

    bus.close(timeout=1)

    assert exporter.closed is True


def test_local_perf_exporter_writes_metrics_without_rich_llm_content(tmp_path, monkeypatch):
    import json

    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.local import exporter as local_exporter_module

    monkeypatch.setattr(local_exporter_module, "LOG_DIR", str(tmp_path))
    exporter = local_exporter_module.LocalPerfExporter({"local": {}})
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_FINISHED,
            run_id="run-local",
            session_id="session-local",
            turn_id=2,
            span_key="llm:main:1",
            payload={
                "status": RuntimeStatus.SUCCESS,
                "caller": "main",
                "iteration": 1,
                "prompt_text": "secret prompt",
                "completion_text": "secret answer",
                "reasoning_text": "secret reasoning",
                "stream_duration_ms": 21,
                "wall_duration_ms": 34,
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 4,
                    "total_tokens": 14,
                    "prompt_cache_hit_tokens": 6,
                    "prompt_cache_miss_tokens": 4,
                    "prompt_tokens_details": {"cached_tokens": 6},
                    "cache_hit_tokens": 6,
                    "cache_miss_tokens": 4,
                    "cache_total_input_tokens": 10,
                    "cache_hit_rate": 0.6,
                },
            },
        )
    )
    exporter.close()

    records = [
        json.loads(line)
        for path in tmp_path.glob("agent_perf-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert records == [
        {
            "schema_version": 1,
            "kind": "llm_call",
            "event": "llm.finished",
            "ts_ms": records[0]["ts_ms"],
            "run_id": "run-local",
            "session_id": "session-local",
            "turn_id": 2,
            "span_key": "llm:main:1",
            "status": "success",
            "caller": "main",
            "iteration": 1,
            "metrics": {
                "stream_duration_ms": 21,
                "wall_duration_ms": 34,
                "prompt_tokens": 10,
                "completion_tokens": 4,
                "total_tokens": 14,
                "cache_hit_tokens": 6,
                "cache_miss_tokens": 4,
                "cache_total_input_tokens": 10,
                "cache_hit_rate": 0.6,
                "prompt_chars": len("secret prompt"),
                "completion_chars": len("secret answer"),
                "reasoning_chars": len("secret reasoning"),
            },
            "labels": {},
        }
    ]
    assert "secret prompt" not in records[0]
    assert "secret answer" not in records[0]
    assert "secret reasoning" not in records[0]


def test_local_perf_exporter_projects_glm_cache_usage(tmp_path, monkeypatch):
    import json

    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.local import exporter as local_exporter_module

    monkeypatch.setattr(local_exporter_module, "LOG_DIR", str(tmp_path))
    exporter = local_exporter_module.LocalPerfExporter({"local": {}})
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_FINISHED,
            run_id="run-glm-cache",
            session_id="session-local",
            turn_id=2,
            payload={
                "status": RuntimeStatus.SUCCESS,
                "usage": {
                    "prompt_tokens": 5992,
                    "completion_tokens": 8,
                    "total_tokens": 6000,
                    "prompt_tokens_details": {"cached_tokens": 5952},
                    "cache_hit_tokens": 5952,
                    "cache_total_input_tokens": 5992,
                    "cache_hit_rate": 5952 / 5992,
                },
            },
        )
    )
    exporter.close()

    records = [
        json.loads(line)
        for path in tmp_path.glob("agent_perf-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert records[0]["metrics"]["cache_hit_tokens"] == 5952
    assert records[0]["metrics"]["cache_total_input_tokens"] == 5992
    assert records[0]["metrics"]["cache_hit_rate"] == 5952 / 5992
    assert "cache_miss_tokens" not in records[0]["metrics"]


def test_local_perf_exporter_does_not_parse_provider_specific_cache_fields(tmp_path, monkeypatch):
    import json

    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.local import exporter as local_exporter_module

    monkeypatch.setattr(local_exporter_module, "LOG_DIR", str(tmp_path))
    exporter = local_exporter_module.LocalPerfExporter({"local": {}})
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_FINISHED,
            run_id="run-raw-provider-cache",
            session_id="session-local",
            turn_id=2,
            payload={
                "status": RuntimeStatus.SUCCESS,
                "usage": {
                    "prompt_tokens": 6250,
                    "completion_tokens": 8,
                    "total_tokens": 6258,
                    "prompt_cache_hit_tokens": 6144,
                    "prompt_cache_miss_tokens": 106,
                    "prompt_tokens_details": {"cached_tokens": 6144},
                },
            },
        )
    )
    exporter.close()

    records = [
        json.loads(line)
        for path in tmp_path.glob("agent_perf-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]

    assert records[0]["metrics"]["prompt_tokens"] == 6250
    assert "cache_hit_tokens" not in records[0]["metrics"]
    assert "cache_total_input_tokens" not in records[0]["metrics"]
    assert "cache_hit_rate" not in records[0]["metrics"]


def test_local_perf_exporter_projects_runtime_perf_families(tmp_path, monkeypatch):
    import json

    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.local import exporter as local_exporter_module

    monkeypatch.setattr(local_exporter_module, "LOG_DIR", str(tmp_path))
    exporter = local_exporter_module.LocalPerfExporter({"local": {}})
    events = [
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "workspace_id": "ws-a",
                "model": "glm-5",
                "model_provider": "glm",
                "agent_mode": "thinking",
                "selected_ontology_id": "example",
            },
        ),
        RuntimeEvent(
            type=RuntimeEventType.AGENT_ITERATION_PREPARED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "caller": "main",
                "iteration": 2,
                "prompt_build_ms": 17,
                "llm_input_chars": 84,
                "tools_count": 4,
            },
        ),
        RuntimeEvent(
            type=RuntimeEventType.AGENT_ITERATION_FINISHED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "caller": "main",
                "iteration": 2,
                "status": "continue",
                "total_elapsed_ms": 55,
                "tool_names": ["search"],
            },
        ),
        RuntimeEvent(
            type=RuntimeEventType.LLM_FINISHED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "status": RuntimeStatus.SUCCESS,
                "caller": "main",
                "iteration": 2,
                "duration_ms": 44,
                "stream_duration_ms": 45,
                "prompt_text": "prompt",
                "completion_text": "answer",
            },
        ),
        RuntimeEvent(
            type=RuntimeEventType.RUNTIME_PHASE_FINISHED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "phase_name": "llm_context_save",
                "component": "persistence",
                "status": RuntimeStatus.SUCCESS,
                "duration_ms": 9,
                "message_count": 6,
            },
        ),
        RuntimeEvent(
            type=RuntimeEventType.TOOL_FINISHED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "caller": "main",
                "iteration": 2,
                "tool_name": "search",
                "tool_call_id": "call-1",
                "status": "completed",
                "elapsed_ms": 31,
                "output_preview": "private output",
            },
        ),
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            run_id="run-local-families",
            session_id="session-local",
            turn_id=3,
            payload={
                "status": RuntimeStatus.SUCCESS,
                "duration_ms": 100,
                "final_answer": "private answer",
            },
        ),
    ]

    for event in events:
        exporter.export(event)
    exporter.close()

    records = [
        json.loads(line)
        for path in tmp_path.glob("agent_perf-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    assert [record["kind"] for record in records] == [
        "run",
        "agent_iteration",
        "agent_iteration",
        "llm_call",
        "runtime_phase",
        "tool_call",
        "run",
        "run_summary",
    ]
    assert records[1]["metrics"]["prompt_build_ms"] == 17
    assert records[2]["metrics"]["tool_names"] == ["search"]
    assert records[3]["metrics"]["duration_ms"] == 44
    assert records[3]["metrics"]["stream_duration_ms"] == 45
    assert records[4]["labels"]["phase_name"] == "llm_context_save"
    assert records[5]["metrics"]["output_chars"] == len("private output")
    assert records[-1]["metrics"]["total_duration_ms"] == 100
    assert records[-1]["metrics"]["llm_calls"] == 1
    assert records[-1]["metrics"]["llm_stream_total_ms"] == 45
    assert records[-1]["metrics"]["llm_wall_total_ms"] == 44
    assert records[-1]["labels"] == {
        "workspace_id": "ws-a",
        "model": "glm-5",
        "model_provider": "glm",
        "agent_mode": "thinking",
        "selected_ontology_id": "example",
    }
    assert "private output" not in records[5]
    assert "private answer" not in records[-2]


def test_local_perf_exporter_projects_direct_execution_trace(tmp_path, monkeypatch):
    import json

    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.local import exporter as local_exporter_module
    from agentic.observability.traces.direct_execution import DirectExecutionRuntimeTrace

    class ExportingObserver:
        enabled = True

        def __init__(self, exporter):
            self.exporter = exporter

        def emit(self, event):
            self.exporter.export(event)

    monkeypatch.setattr(local_exporter_module, "LOG_DIR", str(tmp_path))
    exporter = local_exporter_module.LocalPerfExporter({"local": {}})
    observer = ExportingObserver(exporter)

    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            run_id="run-direct-local",
            session_id="session-direct",
            turn_id=4,
            payload={"workspace_id": "ws-a", "agent_mode": "quick"},
        )
    )
    trace = DirectExecutionRuntimeTrace(
        observer=observer,
        run_id="run-direct-local",
        session_id="session-direct",
        turn_id=4,
    )
    with trace.direct_execution_scope(ontology_id="example", operation_key="query_flight"):
        with trace.script_scope(
            script_name="query_flight.py",
            script_path="scripts/query_flight.py",
            ontology_id="example",
            operation_key="query_flight",
            arguments={"batch_no": "BATCH123"},
        ) as script_trace:
            script_trace.record_result(output="ok", is_error=False, elapsed_ms=12)
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            run_id="run-direct-local",
            session_id="session-direct",
            turn_id=4,
            payload={"status": RuntimeStatus.SUCCESS, "duration_ms": 30},
        )
    )
    exporter.close()

    records = [
        json.loads(line)
        for path in tmp_path.glob("agent_perf-*.log")
        for line in path.read_text(encoding="utf-8").splitlines()
    ]
    direct_tool = next(
        record
        for record in records
        if record["kind"] == "tool_call" and record["event"] == "tool.finished"
    )
    direct_batch = next(
        record
        for record in records
        if record["kind"] == "tool_batch" and record["event"] == "tool_batch.finished"
    )

    assert direct_tool["caller"] == "direct"
    assert direct_tool["iteration"] == 0
    assert direct_tool["metrics"]["elapsed_ms"] == 12
    assert direct_tool["labels"]["display_name"] == "execute_script:query_flight.py"
    assert direct_tool["labels"]["script_name"] == "query_flight.py"
    assert direct_batch["caller"] == "direct"
    assert direct_batch["iteration"] == 0
    assert records[-1]["kind"] == "run_summary"
    assert records[-1]["metrics"]["tool_calls_finished"] == 1


def test_jiuwen_adapter_uses_semantic_trace_facade_for_observability():
    repo_root = Path(__file__).resolve().parents[3]
    adapter_text = (repo_root / "runtime/agent/agentic/loop/jiuwen_adapter.py").read_text(encoding="utf-8")

    assert "AgentRuntimeTrace" in adapter_text
    assert "RuntimeEvent(" not in adapter_text
    assert "RuntimeEventType." not in adapter_text


def test_runtime_code_uses_observability_facades_not_raw_events():
    repo_root = Path(__file__).resolve().parents[3]
    checked_paths = [
        repo_root / "runtime/agent/agentic/orchestrator.py",
        repo_root / "runtime/agent/agentic/loop/jiuwen_adapter.py",
        repo_root / "runtime/direct_execution/agentic/direct_execution/runner.py",
        repo_root / "runtime/agent/agentic/utils/reasoning_summary.py",
    ]

    offenders = [
        str(path.relative_to(repo_root))
        for path in checked_paths
        if "RuntimeEvent(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_runtime_code_does_not_use_legacy_perf_logger():
    repo_root = Path(__file__).resolve().parents[3]
    checked_paths = [
        repo_root / "runtime/agent/agentic/orchestrator.py",
        repo_root / "runtime/direct_execution/agentic/direct_execution/runner.py",
        repo_root / "runtime/agent/agentic/loop/jiuwen_adapter.py",
    ]

    offenders = [
        str(path.relative_to(repo_root))
        for path in checked_paths
        if "perf_log(" in path.read_text(encoding="utf-8")
        or "utils.perf_logging" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
    assert not (repo_root / "runtime/agent/agentic/utils/perf_logging.py").exists()


def test_jiuwen_adapter_uses_trace_facade_without_enabled_branching():
    repo_root = Path(__file__).resolve().parents[3]
    adapter_text = (repo_root / "runtime/agent/agentic/loop/jiuwen_adapter.py").read_text(encoding="utf-8")

    assert "AgentRuntimeTrace(" in adapter_text
    assert "runtime_trace.enabled" not in adapter_text


def test_agent_runtime_trace_finishes_agent_loop_once():
    from agentic.observability import AgentRuntimeTrace, RuntimeEventType, RuntimeStatus

    events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            events.append(event)
            return True

    trace = AgentRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-once",
        session_id="session-once",
        turn_id=1,
        caller="main",
    )

    trace.agent_loop_finished(RuntimeStatus.ERROR)
    trace.agent_loop_finished(RuntimeStatus.SUCCESS)

    assert [event.type for event in events] == [RuntimeEventType.AGENT_LOOP_FINISHED]
    assert events[0].payload["status"] == RuntimeStatus.ERROR


def test_agent_runtime_trace_scope_finishes_success_and_error():
    from agentic.observability import AgentRuntimeTrace, RuntimeEventType, RuntimeStatus

    success_events = []
    error_events = []

    class RecordingObserver:
        enabled = True

        def __init__(self, target):
            self.target = target

        def emit(self, event):
            self.target.append(event)
            return True

    success_trace = AgentRuntimeTrace(
        observer=RecordingObserver(success_events),
        run_id="run-scope-success",
        session_id="session-scope",
        turn_id=1,
        caller="main",
    )
    with success_trace.agent_loop_scope(agent_mode="quick"):
        pass

    assert [event.type for event in success_events] == [
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    ]
    assert success_events[-1].payload["status"] == RuntimeStatus.SUCCESS

    error_trace = AgentRuntimeTrace(
        observer=RecordingObserver(error_events),
        run_id="run-scope-error",
        session_id="session-scope",
        turn_id=1,
        caller="main",
    )
    with pytest.raises(RuntimeError, match="boom"):
        with error_trace.agent_loop_scope(agent_mode="quick"):
            raise RuntimeError("boom")

    assert [event.type for event in error_events] == [
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    ]
    assert error_events[-1].payload["status"] == RuntimeStatus.ERROR


def test_agent_runtime_trace_scopes_mark_cancellation_explicitly():
    from agentic.exceptions import WorkflowCancelledError
    from agentic.observability import AgentRuntimeTrace, RuntimeEventType, RuntimeStatus

    events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            events.append(event)
            return True

    trace = AgentRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-cancelled-scope",
        session_id="session-cancelled-scope",
        turn_id=1,
        caller="main",
    )

    with pytest.raises(WorkflowCancelledError):
        with trace.agent_loop_scope(agent_mode="quick"):
            raise WorkflowCancelledError("cancelled")

    assert [event.type for event in events] == [
        RuntimeEventType.AGENT_LOOP_STARTED,
        RuntimeEventType.AGENT_LOOP_FINISHED,
    ]
    assert events[-1].payload["status"] == RuntimeStatus.CANCELLED

    llm_events = []
    llm_trace = AgentRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-cancelled-llm",
        session_id="session-cancelled-scope",
        turn_id=1,
        caller="main",
    )
    events.clear()

    with pytest.raises(WorkflowCancelledError):
        with llm_trace.llm_call_scope(
            iteration=1,
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            model="test-model",
        ):
            raise WorkflowCancelledError("cancelled")
    llm_events.extend(events)

    assert [event.type for event in llm_events] == [
        RuntimeEventType.LLM_STARTED,
        RuntimeEventType.LLM_FAILED,
    ]
    assert llm_events[-1].payload["status"] == RuntimeStatus.CANCELLED


def test_agent_runtime_trace_evaluates_lazy_llm_payload_only_when_enabled():
    from agentic.observability import AgentRuntimeTrace, NOOP_OBSERVER

    events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            events.append(event)
            return True

    trace = AgentRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-lazy-enabled",
        session_id="session-lazy",
        turn_id=1,
        caller="main",
    )
    trace.llm_finished(
        iteration=1,
        usage={},
        prompt_text=lambda: "lazy prompt",
        completion_text=lambda: "lazy completion",
    )

    assert events[0].payload["prompt_text"] == "lazy prompt"
    assert events[0].payload["completion_text"] == "lazy completion"

    disabled_trace = AgentRuntimeTrace(
        observer=NOOP_OBSERVER,
        run_id="run-lazy-disabled",
        session_id="session-lazy",
        turn_id=1,
        caller="main",
    )
    disabled_trace.llm_finished(
        iteration=1,
        usage={},
        prompt_text=lambda: (_ for _ in ()).throw(AssertionError("should not build prompt")),
        completion_text=lambda: (_ for _ in ()).throw(AssertionError("should not build completion")),
    )


def test_agent_runtime_trace_lazy_payload_failure_does_not_escape():
    from agentic.observability import AgentRuntimeTrace

    events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            events.append(event)
            return True

    trace = AgentRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-lazy-error",
        session_id="session-lazy",
        turn_id=1,
        caller="main",
    )
    trace.llm_finished(
        iteration=1,
        usage={},
        prompt_text=lambda: (_ for _ in ()).throw(RuntimeError("prompt failed")),
        completion_text=lambda: "completion",
    )

    assert events[0].payload["prompt_text"] == ""
    assert events[0].payload["completion_text"] == "completion"


def test_agent_runtime_trace_llm_call_scope_records_success_and_error():
    from agentic.observability import AgentRuntimeTrace, RuntimeEventType, RuntimeStatus

    success_events = []
    error_events = []

    class RecordingObserver:
        enabled = True

        def __init__(self, target):
            self.target = target

        def emit(self, event):
            self.target.append(event)
            return True

    success_trace = AgentRuntimeTrace(
        observer=RecordingObserver(success_events),
        run_id="run-llm-scope-success",
        session_id="session-llm-scope",
        turn_id=1,
        caller="main",
    )
    with success_trace.llm_call_scope(
        iteration=2,
        messages=[{"role": "user", "content": "hello"}],
        tools=[{"name": "search"}],
        model="test-model",
    ) as llm_trace:
        llm_trace.finish(
            usage={"total_tokens": 3},
            prompt_text=lambda: "lazy prompt",
            completion_text=lambda: "lazy completion",
        )

    assert [event.type for event in success_events] == [
        RuntimeEventType.LLM_STARTED,
        RuntimeEventType.LLM_FINISHED,
    ]
    assert success_events[0].payload["iteration"] == 2
    assert success_events[0].payload["total_messages"] == 1
    assert success_events[0].payload["tools_count"] == 1
    assert success_events[0].payload["model"] == "test-model"
    assert success_events[1].payload["status"] == RuntimeStatus.SUCCESS
    assert success_events[1].payload["prompt_text"] == "lazy prompt"
    assert success_events[1].payload["completion_text"] == "lazy completion"

    error_trace = AgentRuntimeTrace(
        observer=RecordingObserver(error_events),
        run_id="run-llm-scope-error",
        session_id="session-llm-scope",
        turn_id=1,
        caller="main",
    )
    with pytest.raises(RuntimeError, match="llm boom"):
        with error_trace.llm_call_scope(
            iteration=3,
            messages=[{"role": "user", "content": "hello"}],
            tools=[],
            model="test-model",
        ):
            raise RuntimeError("llm boom")

    assert [event.type for event in error_events] == [
        RuntimeEventType.LLM_STARTED,
        RuntimeEventType.LLM_FAILED,
    ]
    assert error_events[-1].payload["status"] == RuntimeStatus.ERROR


def test_agent_runtime_trace_tool_batch_scope_records_success_and_error():
    from agentic.observability import AgentRuntimeTrace, RuntimeEventType, RuntimeStatus

    success_events = []
    streamed_events = []
    error_events = []

    class RecordingObserver:
        enabled = True

        def __init__(self, target):
            self.target = target

        def emit(self, event):
            self.target.append(event)
            return True

    success_trace = AgentRuntimeTrace(
        observer=RecordingObserver(success_events),
        run_id="run-tool-scope-success",
        session_id="session-tool-scope",
        turn_id=1,
        caller="main",
    )
    with success_trace.tool_batch_scope(iteration=2) as tool_trace:
        tool_trace.finish(
            tool_calls_count=2,
            tool_names=["search", "read"],
            duration_ms=12,
        )

    assert [event.type for event in success_events] == [
        RuntimeEventType.TOOL_BATCH_STARTED,
        RuntimeEventType.TOOL_BATCH_FINISHED,
    ]
    assert success_events[0].payload["iteration"] == 2
    assert success_events[1].payload["status"] == RuntimeStatus.SUCCESS
    assert success_events[1].payload["tool_calls_count"] == 2
    assert success_events[1].payload["tool_names"] == ["search", "read"]
    assert success_events[1].payload["duration_ms"] == 12

    streamed_trace = AgentRuntimeTrace(
        observer=RecordingObserver(streamed_events),
        run_id="run-tool-scope-streamed",
        session_id="session-tool-scope",
        turn_id=1,
        caller="main",
    )
    with streamed_trace.continued_tool_batch_scope(iteration=4) as tool_trace:
        tool_trace.finish(
            tool_calls_count=1,
            tool_names=["streamed"],
            duration_ms=5,
        )

    assert [event.type for event in streamed_events] == [RuntimeEventType.TOOL_BATCH_FINISHED]

    error_trace = AgentRuntimeTrace(
        observer=RecordingObserver(error_events),
        run_id="run-tool-scope-error",
        session_id="session-tool-scope",
        turn_id=1,
        caller="main",
    )
    with pytest.raises(RuntimeError, match="tool boom"):
        with error_trace.tool_batch_wait_scope(iteration=3):
            raise RuntimeError("tool boom")

    assert [event.type for event in error_events] == [RuntimeEventType.TOOL_BATCH_FAILED]
    assert error_events[-1].payload["status"] == RuntimeStatus.ERROR


def test_agent_runtime_trace_records_iteration_perf_events():
    from agentic.observability import AgentRuntimeTrace, RuntimeEventType

    events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            events.append(event)
            return True

    trace = AgentRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-iteration",
        session_id="session-iteration",
        turn_id=1,
        caller="main",
    )

    trace.iteration_prepared(
        iteration=2,
        purpose="main",
        agent_mode="thinking",
        prompt_build_ms=17,
        system_prompt_chars=32,
        sliding_window_applied=True,
        llm_message_count=3,
        llm_input_chars=44,
        tools_count=2,
    )
    trace.iteration_finished(
        iteration=2,
        status="continue",
        prompt_build_ms=17,
        llm_stream_ms=20,
        llm_wall_ms=27,
        tools_window_ms=11,
        tools_overlap_llm_ms=4,
        tools_after_llm_ms=7,
        tool_calls_count=1,
        tool_names=["search"],
        total_ms=38,
    )

    assert [event.type for event in events] == [
        RuntimeEventType.AGENT_ITERATION_PREPARED,
        RuntimeEventType.AGENT_ITERATION_FINISHED,
    ]
    assert events[0].parent_key == trace.loop_span_key
    assert events[0].payload["agent_mode"] == "thinking"
    assert events[1].payload["llm_wall_ms"] == 27
    assert events[1].payload["tool_names"] == ["search"]


def test_run_runtime_trace_phase_scope_records_success():
    from agentic.observability import RunRuntimeTrace, RuntimeEventType, RuntimeStatus

    events = []

    class RecordingObserver:
        enabled = True

        def emit(self, event):
            events.append(event)
            return True

    trace = RunRuntimeTrace(
        observer=RecordingObserver(),
        run_id="run-phase",
        session_id="session-phase",
        turn_id=1,
    )

    with trace.phase_scope(
        phase_name="llm_context_save",
        component="persistence",
        metrics={"message_count": 4},
    ):
        pass

    assert [event.type for event in events] == [
        RuntimeEventType.RUNTIME_PHASE_STARTED,
        RuntimeEventType.RUNTIME_PHASE_FINISHED,
    ]
    assert events[-1].payload["status"] == RuntimeStatus.SUCCESS
    assert events[-1].payload["phase_name"] == "llm_context_save"
    assert events[-1].payload["message_count"] == 4
    assert events[-1].payload["duration_ms"] >= 0


def test_observer_provider_uses_system_config_exporter_registry(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    class FakeExporter:
        def __init__(self, config):
            self.config = config

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": true,
    "mode": "fake",
    "langfuse": {
      "public_key": "pk-test",
      "secret_key": "sk-test"
    }
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "fake", FakeExporter)

    observer = provider_mod.get_observer()
    try:
        assert observer._exporter.__class__ is FakeExporter
        assert observer._exporter.config["mode"] == "fake"
        assert observer._exporter.config["langfuse"]["secret_key"] == "sk-test"
        assert observer._queue.maxsize == 1000
        assert observer._flush_interval_seconds == 5.0
    finally:
        provider_mod.shutdown_observer(timeout=1)


def test_observer_provider_selects_exporter_from_observability_mode(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    class FakeExporter:
        def __init__(self, config):
            self.config = config

        def export(self, event):
            del event

        def flush(self):
            pass

        def close(self):
            pass

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": true,
    "mode": "fake"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "fake", FakeExporter)

    observer = provider_mod.get_observer()
    try:
        assert observer._exporter.__class__ is FakeExporter
        assert observer._exporter.config["mode"] == "fake"
        assert observer._queue.maxsize == 1000
        assert observer._flush_interval_seconds == 5.0
    finally:
        provider_mod.shutdown_observer(timeout=1)


def test_observer_provider_reuses_process_scoped_observer(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    created_exporters = []

    class FakeExporter:
        def __init__(self, config):
            self.config = config
            created_exporters.append(self)

        def export(self, event):
            del event

        def flush(self):
            pass

        def close(self):
            pass

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": true,
    "mode": "fake"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "fake", FakeExporter)
    provider_mod.shutdown_observer(timeout=1)

    observer_one = provider_mod.get_observer()
    observer_two = provider_mod.get_observer()
    try:
        assert observer_one is observer_two
        assert len(created_exporters) == 1
    finally:
        provider_mod.shutdown_observer(timeout=1)


def test_observer_provider_reset_closes_current_observer_and_rebuilds(tmp_path, monkeypatch):
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    created_exporters = []

    class FakeExporter:
        def __init__(self, config):
            self.config = config
            self.closed = False
            created_exporters.append(self)

        def export(self, event):
            del event

        def flush(self):
            pass

        def close(self):
            self.closed = True

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": true,
    "mode": "fake"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "fake", FakeExporter)
    provider_mod.shutdown_observer(timeout=1)

    observer_one = provider_mod.get_observer()
    provider_mod.reset_observer(timeout=1)
    observer_two = provider_mod.get_observer()
    try:
        assert observer_two is not observer_one
        assert len(created_exporters) == 2
        assert created_exporters[0].closed is True
        assert created_exporters[1].closed is False
    finally:
        provider_mod.shutdown_observer(timeout=1)


def test_runtime_refresh_resets_observability_provider(monkeypatch):
    import agentic.app as app_mod

    calls = []
    runtime = {
        "auth_manager": object(),
        "user_store_manager": object(),
        "llm_router": object(),
        "config": object(),
        "agent_config": object(),
    }

    class FakeApp:
        config = {"LLM_ROUTER": object()}

    def fake_build_runtime_services(previous_llm_router=None):
        del previous_llm_router
        calls.append("build")
        return runtime

    def fake_update_runtime_dependencies(*args, **kwargs):
        del args, kwargs
        calls.append("update")

    def fake_reset_observer(timeout=1.0):
        calls.append(("reset", timeout))

    monkeypatch.setattr(app_mod, "build_runtime_services", fake_build_runtime_services)
    monkeypatch.setattr(app_mod, "update_runtime_dependencies", fake_update_runtime_dependencies)
    monkeypatch.setattr(app_mod, "reset_observer", fake_reset_observer, raising=False)

    app_mod.refresh_runtime_services(FakeApp())

    assert calls == ["build", "update", ("reset", 1.0)]


def test_observer_provider_ignores_environment_variables(tmp_path, monkeypatch):
    from agentic.observability import NOOP_OBSERVER
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    class FakeExporter:
        pass

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_dir / "system_config.json")
    (config_dir / "system_config.json").write_text(
        '{"observability": {"enabled": false, "mode": "fake"}}',
        encoding="utf-8",
    )
    monkeypatch.setenv("OBSERVABILITY_MODE", "fake")
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "fake", FakeExporter)

    assert provider_mod.get_observer() is NOOP_OBSERVER


def test_observer_provider_returns_noop_for_unknown_configured_mode(tmp_path, monkeypatch, caplog):
    from agentic.observability import NOOP_OBSERVER
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": true,
    "mode": "unknown-mode"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)

    with caplog.at_level(logging.WARNING, logger="observability.provider"):
        assert provider_mod.get_observer() is NOOP_OBSERVER

    assert "Unknown observability exporter mode=unknown-mode" in caplog.text


def test_observer_provider_warns_when_exporter_is_unavailable(tmp_path, monkeypatch, caplog):
    from agentic.observability import NOOP_OBSERVER
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    class UnavailableExporter:
        available = False

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "enabled": true,
    "mode": "unavailable"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "unavailable", lambda config: UnavailableExporter())

    with caplog.at_level(logging.WARNING, logger="observability.provider"):
        assert provider_mod.get_observer() is NOOP_OBSERVER

    assert "Observability exporter mode=unavailable is unavailable" in caplog.text


def test_observer_provider_returns_noop_when_config_load_fails(monkeypatch):
    from agentic.observability import NOOP_OBSERVER
    import agentic.observability.provider as provider_mod

    def fail_load_system_config():
        raise RuntimeError("bad config")

    monkeypatch.setattr(provider_mod, "load_system_config", fail_load_system_config)

    assert provider_mod.get_observer() is NOOP_OBSERVER


def test_observer_provider_returns_noop_when_exporter_creation_fails(tmp_path, monkeypatch):
    from agentic.observability import NOOP_OBSERVER
    import agentic.config.system_config as system_config_module
    import agentic.observability.provider as provider_mod

    def fail_exporter(config):
        del config
        raise RuntimeError("bad exporter")

    config_dir = tmp_path / "config"
    config_dir.mkdir(parents=True, exist_ok=True)
    config_path = config_dir / "system_config.json"
    config_path.write_text(
        """
{
  "observability": {
    "mode": "failing"
  }
}
""".strip(),
        encoding="utf-8",
    )

    monkeypatch.setattr(system_config_module, "CONFIG_DIR", config_dir)
    monkeypatch.setattr(system_config_module, "CONFIG_PATH", config_path)
    monkeypatch.setitem(provider_mod.EXPORTER_REGISTRY, "failing", fail_exporter)

    assert provider_mod.get_observer() is NOOP_OBSERVER


def test_exporter_registry_uses_provider_factories():
    from agentic.observability.exporters import EXPORTER_REGISTRY

    assert EXPORTER_REGISTRY["langfuse"].__name__ == "create_langfuse_exporter"


def test_langfuse_exporter_accepts_adapter_not_config():
    from agentic.observability.exporters.langfuse import LangfuseExporter

    class FakeAdapter:
        available = True

        def flush(self):
            return None

    with pytest.raises(TypeError):
        LangfuseExporter()

    exporter = LangfuseExporter(FakeAdapter())

    assert exporter.available is True


def test_langfuse_exporter_closes_trace_adapter():
    from agentic.observability.exporters.langfuse import LangfuseExporter

    class FakeAdapter:
        available = True

        def __init__(self):
            self.closed = False

        def close(self):
            self.closed = True

    adapter = FakeAdapter()
    exporter = LangfuseExporter(adapter)

    exporter.close()

    assert adapter.closed is True


def test_langfuse_exporter_preserves_existing_trace_granularity(monkeypatch):
    from agentic.observability import (
        RuntimeEvent,
        RuntimeEventType,
        RuntimeRequestLabel,
        RuntimeStatus,
    )
    from agentic.observability.exporters.langfuse import LangfuseExporter

    calls = []

    def record(name):
        def _inner(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name == "create_trace":
                return "trace-1"
            if name in {"create_span", "create_llm_span"}:
                return kwargs.get("name")
            return None

        return _inner

    class FakeAdapter:
        create_trace = staticmethod(record("create_trace"))
        create_span = staticmethod(record("create_span"))
        create_llm_span = staticmethod(record("create_llm_span"))
        end_observation = staticmethod(record("end_observation"))
        update_llm_span_tokens = staticmethod(record("update_llm_span_tokens"))
        update_trace_output = staticmethod(record("update_trace_output"))
        flush = staticmethod(record("flush"))

    exporter = LangfuseExporter(FakeAdapter())
    events = [
        RuntimeEvent(type=RuntimeEventType.RUN_STARTED, run_id="run-1", session_id="s1", payload={"user_query": "hi"}),
        RuntimeEvent(type=RuntimeEventType.AGENT_LOOP_STARTED, run_id="run-1", session_id="s1", span_key="loop:main", payload={"caller": "main"}),
        RuntimeEvent(type=RuntimeEventType.LLM_STARTED, run_id="run-1", session_id="s1", span_key="llm:1", parent_key="loop:main", payload={"caller": "main", "iteration": 1, "model": "m"}),
        RuntimeEvent(type=RuntimeEventType.LLM_FINISHED, run_id="run-1", session_id="s1", span_key="llm:1", payload={"usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3}}),
        RuntimeEvent(type=RuntimeEventType.TOOL_BATCH_STARTED, run_id="run-1", session_id="s1", span_key="tools:1", parent_key="loop:main", payload={"caller": "main", "iteration": 1}),
        RuntimeEvent(type=RuntimeEventType.TOOL_BATCH_FINISHED, run_id="run-1", session_id="s1", span_key="tools:1", payload={"status": RuntimeStatus.SUCCESS}),
        RuntimeEvent(type=RuntimeEventType.LLM_STARTED, run_id="run-1", session_id="s1", span_key="reasoning:abc", payload={"request_label": RuntimeRequestLabel.REASONING_SUMMARY, "model": "m"}),
        RuntimeEvent(type=RuntimeEventType.LLM_FINISHED, run_id="run-1", session_id="s1", span_key="reasoning:abc", payload={"usage": {}}),
        RuntimeEvent(type=RuntimeEventType.RUN_FINISHED, run_id="run-1", session_id="s1", payload={"status": RuntimeStatus.SUCCESS, "final_answer": "done"}),
    ]

    for event in events:
        exporter.export(event)
    exporter.flush()

    call_names = [name for name, _, _ in calls]
    assert call_names.count("create_trace") == 1
    assert any(
        name == "create_span"
        and kwargs["name"] == "agent_loop:main"
        and kwargs["metadata"]["caller"] == "main"
        for name, _, kwargs in calls
    )
    assert any(name == "create_llm_span" and kwargs["name"] == "llm:main_iteration" for name, _, kwargs in calls)
    assert any(name == "create_span" and kwargs["name"] == "tools:main_iteration_1" for name, _, kwargs in calls)
    assert any(name == "create_llm_span" and kwargs["name"] == "llm:reasoning_summary" for name, _, kwargs in calls)
    assert "update_trace_output" in call_names
    assert call_names[-1] == "flush"


def test_langfuse_exporter_records_detailed_llm_tool_and_run_payloads():
    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.langfuse import LangfuseExporter

    calls = []

    def record(name):
        def _inner(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name == "create_trace":
                return "trace-1"
            if name in {"create_span", "create_llm_span"}:
                return kwargs["name"]
            return None

        return _inner

    class FakeAdapter:
        available = True
        create_trace = staticmethod(record("create_trace"))
        create_span = staticmethod(record("create_span"))
        create_llm_span = staticmethod(record("create_llm_span"))
        end_observation = staticmethod(record("end_observation"))
        update_llm_span_tokens = staticmethod(record("update_llm_span_tokens"))
        update_trace_output = staticmethod(record("update_trace_output"))

    exporter = LangfuseExporter(FakeAdapter())

    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            run_id="run-1",
            session_id="session-1",
            turn_id=2,
            timestamp_ms=1000,
            payload={
                "user_query": "hello",
                "workspace_id": "ws-1",
                "model": "model-a",
                "model_provider": "openai",
            },
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.AGENT_LOOP_STARTED,
            run_id="run-1",
            session_id="session-1",
            span_key="loop:main",
            payload={"caller": "main"},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_STARTED,
            run_id="run-1",
            session_id="session-1",
            span_key="llm:main:1",
            parent_key="loop:main",
            payload={
                "caller": "main",
                "iteration": 1,
                "model": "model-a",
                "api_params": {"stream": True, "temperature": 0.2},
                "input": {
                    "latest_input": "hello",
                    "messages": [{"role": "user", "content": "hello"}],
                    "tools": [{"type": "function", "function": {"name": "read_file"}}],
                },
            },
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_BATCH_STARTED,
            run_id="run-1",
            session_id="session-1",
            span_key="tools:main:1",
            parent_key="loop:main",
            payload={"caller": "main", "iteration": 1},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_STARTED,
            run_id="run-1",
            session_id="session-1",
            span_key="tool:main:1:read_file:call-1",
            parent_key="tools:main:1",
            timestamp_ms=1200,
            payload={"tool_name": "read_file", "tool_call_id": "call-1", "input": {"path": "a.md"}},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_PROGRESS,
            run_id="run-1",
            session_id="session-1",
            span_key="tool:main:1:read_file:call-1",
            payload={"message": "chunk", "progress": {"text": "streamed"}},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_FINISHED,
            run_id="run-1",
            session_id="session-1",
            span_key="tool:main:1:read_file:call-1",
            timestamp_ms=1300,
            payload={
                "tool_name": "read_file",
                "tool_call_id": "call-1",
                "status": "completed",
                "output_preview": "file content",
                "elapsed_ms": 100,
            },
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_FINISHED,
            run_id="run-1",
            session_id="session-1",
            span_key="llm:main:1",
            payload={
                "usage": {"prompt_tokens": 3, "completion_tokens": 4, "total_tokens": 7},
                "completion_text": "done",
                "reasoning_text": "think",
                "output": {
                    "content": "done",
                    "reasoning_content": "think",
                    "tool_calls": [{"id": "call-1"}],
                },
                "duration_ms": 250,
            },
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            run_id="run-1",
            session_id="session-1",
            payload={"status": RuntimeStatus.SUCCESS, "final_answer": "done", "duration_ms": 500},
        )
    )

    llm_start = next(call for call in calls if call[0] == "create_llm_span")
    assert llm_start[2]["input"]["latest_input"] == "hello"
    assert llm_start[2]["input"]["messages"][0]["content"] == "hello"
    assert llm_start[2]["metadata"]["api_params"]["temperature"] == 0.2

    tool_end = [
        call for call in calls
        if call[0] == "end_observation" and call[2].get("output", {}).get("tool_call_id") == "call-1"
    ][0]
    assert tool_end[2]["output"]["output_preview"] == "file content"
    assert tool_end[2]["output"]["progress_events"][0]["progress"]["text"] == "streamed"

    llm_end = next(call for call in calls if call[0] == "update_llm_span_tokens")
    assert llm_end[1][2]["total_tokens"] == 7
    assert llm_end[2]["output"]["content"] == "done"
    assert llm_end[2]["output"]["metrics"]["duration_ms"] == 250

    trace_output = next(call for call in calls if call[0] == "update_trace_output")
    assert trace_output[1][1]["duration_ms"] == 500
    assert trace_output[1][1]["usage"]["total_tokens"] == 7
    assert trace_output[1][1]["main_iterations"] == 1


def test_langfuse_exporter_names_jiuwen_llm_child_calls_with_call_index():
    from agentic.observability import RuntimeEvent, RuntimeEventType
    from agentic.observability.exporters.langfuse import LangfuseExporter

    calls = []

    def record(name):
        def _inner(*args, **kwargs):
            calls.append((name, args, kwargs))
            if name == "create_trace":
                return "trace-1"
            if name in {"create_span", "create_llm_span"}:
                return kwargs["name"]
            return None

        return _inner

    class FakeAdapter:
        available = True
        create_trace = staticmethod(record("create_trace"))
        create_span = staticmethod(record("create_span"))
        create_llm_span = staticmethod(record("create_llm_span"))
        end_observation = staticmethod(record("end_observation"))
        update_llm_span_tokens = staticmethod(record("update_llm_span_tokens"))
        update_trace_output = staticmethod(record("update_trace_output"))

    exporter = LangfuseExporter(FakeAdapter())
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            run_id="run-jiuwen-llm-names",
            session_id="session-jiuwen-llm-names",
            payload={"user_query": "hi"},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.LLM_STARTED,
            run_id="run-jiuwen-llm-names",
            session_id="session-jiuwen-llm-names",
            span_key="llm:main:1:2",
            payload={"caller": "main", "iteration": 1, "call_index": 2, "model": "model-a"},
        )
    )

    llm_start = next(call for call in calls if call[0] == "create_llm_span")
    assert llm_start[2]["name"] == "llm:main_iteration_1_call_2"
    assert "call_2" in llm_start[2]["tags"]


def test_tool_protocol_events_emit_runtime_tool_events():
    from agentic.config import AgentConfig
    from agentic.observability import RuntimeEventType
    from agentic.tools.base import ToolContext
    from agentic.tools.events import emit_tool_finished, emit_tool_started

    emitted = []

    class FakeObserver:
        enabled = True

        def emit(self, event):
            emitted.append(event)

    context = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-1",
        session_id="session-1",
        turn_id=2,
        ov_session_id=None,
        agent_config=AgentConfig(
            llm_connection_max_retries=1,
            main_agent_max_iterations=3,
            sub_agent_max_iterations=2,
            sub_agent_max_concurrency=2,
            sub_agent_max_depth=2,
            parallel_default_max_workers=2,
            context_max_rounds=5,
            context_prune_tools=set(),
        ),
        caller="subagent",
        agent_iteration=3,
        event_emitter=lambda event_type, payload: None,
        subagent_lineage={"subagent_id": "sub-1", "task_id": "task-1", "depth": 1},
        observer=FakeObserver(),
    )

    emit_tool_started(
        context=context,
        tool_name="read_file",
        tool_call_id="call-1",
        tool_input={"path": "a.md"},
        message="read",
    )
    emit_tool_finished(
        context=context,
        tool_name="read_file",
        tool_call_id="call-1",
        status="completed",
        raw_output="ok",
        error=None,
        elapsed_ms=12,
    )

    assert [event.type for event in emitted] == [
        RuntimeEventType.TOOL_STARTED,
        RuntimeEventType.TOOL_FINISHED,
    ]
    assert emitted[0].span_key == "tool:subagent:sub-1:3:read_file:call-1"
    assert emitted[0].parent_key == "tools:subagent:sub-1:3"
    assert emitted[0].payload["subagent_id"] == "sub-1"


def test_langfuse_exporter_maps_tool_batch_failed_to_error_by_default():
    from agentic.observability import RuntimeEvent, RuntimeEventType
    from agentic.observability.exporters.langfuse import LangfuseExporter

    calls = []

    class FakeAdapter:
        available = True

        @staticmethod
        def create_trace(*args, **kwargs):
            del args, kwargs
            return "trace-1"

        @staticmethod
        def create_span(*args, **kwargs):
            del args
            return kwargs["name"]

        @staticmethod
        def end_observation(*args, **kwargs):
            calls.append((args, kwargs))

    exporter = LangfuseExporter(FakeAdapter())
    exporter.export(RuntimeEvent(type=RuntimeEventType.RUN_STARTED, run_id="run-1", session_id="s1"))
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_BATCH_STARTED,
            run_id="run-1",
            session_id="s1",
            span_key="tools:1",
            payload={"caller": "main", "iteration": 1},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_BATCH_FAILED,
            run_id="run-1",
            session_id="s1",
            span_key="tools:1",
            payload={},
        )
    )

    assert calls[-1][1]["status"] == "error"


def test_langfuse_exporter_uses_generic_tool_display_name():
    from agentic.observability import RuntimeEvent, RuntimeEventType
    from agentic.observability.exporters.langfuse import LangfuseExporter

    span_calls = []

    class FakeAdapter:
        available = True

        @staticmethod
        def create_trace(*args, **kwargs):
            del args, kwargs
            return "trace-1"

        @staticmethod
        def create_span(*args, **kwargs):
            del args
            span_calls.append(kwargs)
            return "span-1"

    exporter = LangfuseExporter(FakeAdapter())
    exporter.export(RuntimeEvent(type=RuntimeEventType.RUN_STARTED, run_id="run-1", session_id="s1"))
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.TOOL_STARTED,
            run_id="run-1",
            session_id="s1",
            span_key="tool:1",
            payload={
                "tool_name": "execute_script",
                "display_name": "execute_script:create_order.py",
                "script_name": "create_order.py",
            },
        )
    )

    assert span_calls[-1]["name"] == "tool:execute_script:create_order.py"
    assert span_calls[-1]["tags"] == ["tool", "execute_script"]


def test_langfuse_exporter_releases_run_bookkeeping_after_terminal_spans_finish():
    from agentic.observability import RuntimeEvent, RuntimeEventType, RuntimeStatus
    from agentic.observability.exporters.langfuse import LangfuseExporter

    class FakeAdapter:
        available = True

        @staticmethod
        def create_trace(*args, **kwargs):
            del args, kwargs
            return "trace-1"

        @staticmethod
        def create_span(*args, **kwargs):
            del args, kwargs
            return "span-1"

        @staticmethod
        def end_observation(*args, **kwargs):
            del args, kwargs

        @staticmethod
        def update_trace_output(*args, **kwargs):
            del args, kwargs

    exporter = LangfuseExporter(FakeAdapter())
    exporter.export(RuntimeEvent(type=RuntimeEventType.RUN_STARTED, run_id="run-1", session_id="s1"))
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.AGENT_LOOP_STARTED,
            run_id="run-1",
            session_id="s1",
            span_key="loop:main",
            payload={"caller": "main"},
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            run_id="run-1",
            session_id="s1",
            payload={"status": RuntimeStatus.SUCCESS},
        )
    )

    assert exporter._trace_ids_by_run == {"run-1": "trace-1"}
    assert exporter._spans_by_run == {"run-1": {"loop:main": "span-1"}}

    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.AGENT_LOOP_FINISHED,
            run_id="run-1",
            session_id="s1",
            span_key="loop:main",
        )
    )

    assert exporter._trace_ids_by_run == {}
    assert exporter._spans_by_run == {}
    assert exporter._terminal_runs == set()


def test_langfuse_trace_id_is_stable_for_the_same_run_identity():
    from agentic.observability import RuntimeEvent, RuntimeEventType
    from agentic.observability.exporters.langfuse import LangfuseExporter

    trace_ids = []

    class FakeAdapter:
        available = True

        @staticmethod
        def create_trace(*args, **kwargs):
            del args
            trace_ids.append(kwargs["trace_id"])
            return kwargs["trace_id"]

        @staticmethod
        def flush():
            return None

    def create_exporter():
        return LangfuseExporter(FakeAdapter())

    for timestamp_ms in [1000, 2000]:
        create_exporter().export(
            RuntimeEvent(
                type=RuntimeEventType.RUN_STARTED,
                run_id="run-stable",
                session_id="session-stable",
                timestamp_ms=timestamp_ms,
                payload={"user_query": "hello"},
            )
        )

    assert len(trace_ids) == 2
    assert trace_ids[0] == trace_ids[1]


def test_langfuse_langchain_dependency_is_not_required():
    repo_root = Path(__file__).resolve().parents[3]
    requirements = (repo_root / "platform/server/requirements.txt").read_text(encoding="utf-8")

    assert "langfuse-langchain" not in requirements
