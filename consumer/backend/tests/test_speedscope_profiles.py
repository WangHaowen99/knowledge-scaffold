from contextvars import Context
import json
import sys
import threading

from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from integration.observability import stack_profiler as stack_profiler_module
from integration.observability.bus import ObservabilityBus
from integration.observability.exporters.speedscope.exporter import SpeedscopeExporter
from integration.observability.traces.run import RunRuntimeTrace
from integration.observability.stack_profiler import _RunStackProfile, _write_stack_profile


def test_stack_profiler_merges_sampled_profile_into_evented_session(tmp_path):
    exporter = SpeedscopeExporter(output_dir=tmp_path)
    _emit_minimal_run(exporter, run_id="run-merge", session_id="session-merge", turn_id=1)

    _write_sampled_code_profile(
        output_dir=tmp_path,
        run_id="run-merge",
        session_id="session-merge",
        turn_id=1,
    )

    document = _load_single_speedscope_document(tmp_path)
    profiles = document["profiles"]

    assert {profile["type"] for profile in profiles} == {"evented", "sampled"}
    assert _active_profile(document)["type"] == "sampled"
    assert _active_profile(document)["name"].startswith("Code Lines / Turn 1 / Run run-merge")
    assert any(
        "agentic.fake.run L10" in frame["name"]
        for frame in document["shared"]["frames"]
    )


def test_speedscope_event_rewrite_preserves_active_sampled_profile(tmp_path):
    exporter = SpeedscopeExporter(output_dir=tmp_path)
    _emit_minimal_run(exporter, run_id="run-rewrite", session_id="session-rewrite", turn_id=2)
    _write_sampled_code_profile(
        output_dir=tmp_path,
        run_id="run-rewrite",
        session_id="session-rewrite",
        turn_id=2,
    )
    before = _load_single_speedscope_document(tmp_path)
    active_name = _active_profile(before)["name"]

    exporter.flush()

    after = _load_single_speedscope_document(tmp_path)
    assert _active_profile(after)["name"] == active_name
    assert len([profile for profile in after["profiles"] if profile["type"] == "sampled"]) == 1
    assert len([profile for profile in after["profiles"] if profile["type"] == "evented"]) >= 1


def test_stack_frame_names_omit_source_text(tmp_path, monkeypatch):
    profile = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-frame-name",
        session_id="session-frame-name",
        turn_id=3,
    )
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)

    frame_index = profile.frame_index(sys._getframe())

    frame_name = profile.frames[frame_index]["name"]
    assert frame_name.startswith("test_speedscope_profiles.")
    assert " L" in frame_name
    assert " | " not in frame_name


def test_context_bindings_do_not_leak_for_interleaved_runs(tmp_path):
    _reset_stack_profiler_state()
    profile_a = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-a",
        session_id="session-a",
        turn_id=1,
    )
    profile_b = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-b",
        session_id="session-b",
        turn_id=1,
    )
    stack_profiler_module._RUNS["run-a"] = profile_a
    stack_profiler_module._RUNS["run-b"] = profile_b
    context_a = Context()
    context_b = Context()

    try:
        assert context_a.run(stack_profiler_module.attach_current_thread_stack_profile, "run-a")
        assert context_b.run(stack_profiler_module.attach_current_thread_stack_profile, "run-b")
        assert profile_a.thread_profile().active_count == 1
        assert profile_b.thread_profile().active_count == 1

        context_a.run(stack_profiler_module.detach_current_thread_stack_profile, "run-a")

        assert profile_a.thread_profile().active_count == 0
        assert profile_b.thread_profile().active_count == 1

        context_b.run(stack_profiler_module.detach_current_thread_stack_profile, "run-b")

        assert profile_a.thread_profile().active_count == 0
        assert profile_b.thread_profile().active_count == 0
        assert stack_profiler_module._THREAD_RUN_COUNTS == {}
    finally:
        _reset_stack_profiler_state()


def test_shared_event_loop_thread_sampling_is_skipped_when_multiple_runs_are_bound(tmp_path):
    _reset_stack_profiler_state()
    profile_a = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-a",
        session_id="session-a",
        turn_id=1,
    )
    profile_b = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-b",
        session_id="session-b",
        turn_id=1,
    )
    stack_profiler_module._RUNS["run-a"] = profile_a
    stack_profiler_module._RUNS["run-b"] = profile_b
    context_a = Context()
    context_b = Context()

    try:
        assert context_a.run(stack_profiler_module.attach_current_thread_stack_profile, "run-a")
        assert context_b.run(stack_profiler_module.attach_current_thread_stack_profile, "run-b")

        stack_profiler_module._sample_profile(profile_a)

        thread_profile = profile_a.thread_profile()
        assert thread_profile.samples == []
        assert thread_profile.skipped_shared_thread_samples == 1
    finally:
        context_a.run(stack_profiler_module.detach_current_thread_stack_profile, "run-a")
        context_b.run(stack_profiler_module.detach_current_thread_stack_profile, "run-b")
        _reset_stack_profiler_state()


def test_stack_sample_budget_is_per_run_not_per_thread(tmp_path, monkeypatch):
    _reset_stack_profiler_state()
    profile = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-budget",
        session_id="session-budget",
        turn_id=1,
        max_samples=1,
    )
    stack_profiler_module._RUNS["run-budget"] = profile
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)

    try:
        assert stack_profiler_module.attach_current_thread_stack_profile("run-budget")

        stack_profiler_module._sample_profile(profile)
        stack_profiler_module._sample_profile(profile)

        thread_profile = profile.thread_profile()
        assert profile.total_samples == 1
        assert len(thread_profile.samples) == 1
        assert thread_profile.dropped_samples == 1
    finally:
        stack_profiler_module.detach_current_thread_stack_profile("run-budget")
        _reset_stack_profiler_state()


def test_process_sample_budget_limits_concurrent_runs(tmp_path, monkeypatch):
    _reset_stack_profiler_state()
    profile_a = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-a",
        session_id="session-a",
        turn_id=1,
        max_samples=10,
        process_max_samples=1,
    )
    profile_b = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-b",
        session_id="session-b",
        turn_id=1,
        max_samples=10,
        process_max_samples=1,
    )
    stack_profiler_module._RUNS["run-a"] = profile_a
    stack_profiler_module._RUNS["run-b"] = profile_b
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)

    try:
        assert stack_profiler_module.attach_current_thread_stack_profile("run-a")
        stack_profiler_module._sample_profile(profile_a)
        stack_profiler_module.detach_current_thread_stack_profile("run-a")

        assert stack_profiler_module.attach_current_thread_stack_profile("run-b")
        stack_profiler_module._sample_profile(profile_b)

        assert profile_a.total_samples == 1
        assert profile_b.total_samples == 0
        assert profile_b.thread_profile().dropped_samples == 1
        assert stack_profiler_module._PROCESS_SAMPLE_COUNT == 1

        stack_profiler_module._release_process_samples(profile_a.total_samples)
        stack_profiler_module._sample_profile(profile_b)

        assert profile_b.total_samples == 1
        assert stack_profiler_module._PROCESS_SAMPLE_COUNT == 1
    finally:
        stack_profiler_module.detach_current_thread_stack_profile("run-a")
        stack_profiler_module.detach_current_thread_stack_profile("run-b")
        _reset_stack_profiler_state()


def test_run_trace_close_cleans_started_profile_after_pre_loop_exception(tmp_path, monkeypatch):
    _reset_stack_profiler_state()
    monkeypatch.setenv("SPEEDSCOPE_TRACE_ENABLED", "true")
    monkeypatch.setenv("SPEEDSCOPE_STACK_SAMPLE_INTERVAL_MS", "1000")
    monkeypatch.setattr(stack_profiler_module, "LOG_DIR", str(tmp_path))
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)
    trace = RunRuntimeTrace(
        observer=_CapturingObserver(),
        run_id="run-pre-loop-error",
        session_id="session-pre-loop-error",
        turn_id=1,
    )

    try:
        trace.started(
            user_id="user",
            user_query="query",
            workspace_id="workspace",
        )
        profile = stack_profiler_module._RUNS["run-pre-loop-error"]
        stack_profiler_module._sample_profile(profile)
        assert stack_profiler_module._PROCESS_SAMPLE_COUNT >= 1

        try:
            raise RuntimeError("before agent loop")
        except RuntimeError:
            trace.close(status=RuntimeStatus.ERROR)

        assert "run-pre-loop-error" not in stack_profiler_module._RUNS
        assert stack_profiler_module._PROCESS_SAMPLE_COUNT == 0
        assert profile.sampler_thread is not None
        assert not profile.sampler_thread.is_alive()

        trace.close(status=RuntimeStatus.ERROR)
        assert stack_profiler_module._PROCESS_SAMPLE_COUNT == 0
    finally:
        _reset_stack_profiler_state()


def test_speedscope_bus_marks_trace_incomplete_when_queue_drops_event(tmp_path):
    exporter = _BlockingSpeedscopeExporter(tmp_path)
    bus = ObservabilityBus(exporter, queue_size=1, flush_interval_seconds=60.0)
    common = {"run_id": "run-drop", "session_id": "session-drop", "turn_id": 1}

    try:
        assert bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_STARTED,
                span_key="run:run-drop",
                timestamp_ms=1000,
                payload={},
                **common,
            )
        )
        assert exporter.export_started.wait(timeout=2.0)
        assert bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_FINISHED,
                span_key="run:run-drop",
                timestamp_ms=1020,
                payload={"status": RuntimeStatus.SUCCESS, "duration_ms": 20},
                **common,
            )
        )
        assert not bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.LLM_STARTED,
                span_key="llm:dropped",
                parent_key="run:run-drop",
                timestamp_ms=1010,
                payload={},
                **common,
            )
        )
    finally:
        exporter.release_export.set()
        bus.close(timeout=2.0)

    document = _load_single_speedscope_document(tmp_path)
    profile_names = [profile["name"] for profile in document["profiles"]]
    frame_names = [frame["name"] for frame in document["shared"]["frames"]]

    assert any("[INCOMPLETE]" in name for name in profile_names)
    assert any("INCOMPLETE: dropped 1" in name for name in frame_names)


def _emit_minimal_run(
    exporter: SpeedscopeExporter,
    *,
    run_id: str,
    session_id: str,
    turn_id: int,
) -> None:
    common = {"run_id": run_id, "session_id": session_id, "turn_id": turn_id}
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            span_key="run:{}".format(run_id),
            timestamp_ms=1000,
            payload={},
            **common,
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.AGENT_LOOP_STARTED,
            span_key="agent_loop:main",
            parent_key="run:{}".format(run_id),
            timestamp_ms=1010,
            payload={"caller": "main", "agent_mode": "quick"},
            **common,
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.AGENT_LOOP_FINISHED,
            span_key="agent_loop:main",
            timestamp_ms=1020,
            payload={"status": RuntimeStatus.SUCCESS, "caller": "main"},
            **common,
        )
    )
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FINISHED,
            span_key="run:{}".format(run_id),
            timestamp_ms=1030,
            payload={"status": RuntimeStatus.SUCCESS, "duration_ms": 30},
            **common,
        )
    )


def _write_sampled_code_profile(
    *,
    output_dir,
    run_id: str,
    session_id: str,
    turn_id: int,
) -> None:
    profile = _RunStackProfile(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        start_ns=0,
        end_ns=5_000_000,
        output_dir=output_dir,
        max_samples=1000,
        sample_interval_ms=5.0,
    )
    sample = [
        profile.synthetic_frame_index("THREAD / MainThread", "meta-wise/thread"),
        profile.synthetic_frame_index("STATE / CPU_RUN", "meta-wise/execution-state"),
        profile.synthetic_frame_index(
            "agentic.fake.run L10",
            "agentic/fake.py:10",
        ),
    ]
    thread_profile = profile.thread_profile()
    thread_profile.samples.append(sample)
    thread_profile.weights.append(5.0)

    _write_stack_profile(profile, status=RuntimeStatus.SUCCESS)


def _new_stack_profile(
    *,
    output_dir,
    run_id: str,
    session_id: str,
    turn_id: int,
    max_samples: int = 1000,
    process_max_samples: int = 1000,
) -> _RunStackProfile:
    return _RunStackProfile(
        run_id=run_id,
        session_id=session_id,
        turn_id=turn_id,
        start_ns=0,
        end_ns=5_000_000,
        output_dir=output_dir,
        max_samples=max_samples,
        process_max_samples=process_max_samples,
        sample_interval_ms=5.0,
    )


def _reset_stack_profiler_state():
    stack_profiler_module._release_process_samples(stack_profiler_module._PROCESS_SAMPLE_COUNT)
    with stack_profiler_module._LOCK:
        stack_profiler_module._RUNS.clear()
    with stack_profiler_module._THREAD_RUN_COUNTS_LOCK:
        stack_profiler_module._THREAD_RUN_COUNTS.clear()
    stack_profiler_module._CONTEXT_BINDINGS.set(())


def _load_single_speedscope_document(output_dir):
    paths = list(output_dir.glob("session-*.speedscope.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _active_profile(document):
    return document["profiles"][document["activeProfileIndex"]]


class _CapturingObserver:
    enabled = True

    def __init__(self):
        self.events = []

    def emit(self, event):
        self.events.append(event)


class _BlockingSpeedscopeExporter:
    available = True

    def __init__(self, output_dir):
        self.delegate = SpeedscopeExporter(output_dir=output_dir)
        self.export_started = threading.Event()
        self.release_export = threading.Event()

    def export(self, event):
        if not self.export_started.is_set():
            self.export_started.set()
            self.release_export.wait(timeout=2.0)
        self.delegate.export(event)

    def record_drop(self, event):
        self.delegate.record_drop(event)

    def flush(self):
        self.delegate.flush()

    def close(self):
        self.delegate.close()
