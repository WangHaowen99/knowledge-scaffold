from internal_access import internal
import json
import multiprocessing
import os
import queue
import sys
import threading
import time
from contextvars import Context
from pathlib import Path

import pytest
from integration.observability import stack_profiler as stack_profiler_module
from integration.observability.bus import ObservabilityBus
from integration.observability.events import RuntimeEvent, RuntimeEventType, RuntimeStatus
from integration.observability.exporters.speedscope.exporter import SpeedscopeExporter
from integration.observability.speedscope_io import speedscope_session_lock_path
from integration.observability.stack_profiler import _RunStackProfile, _write_stack_profile
from integration.observability.traces.run import RunRuntimeTrace
from utils.identifiers import safe_user_storage_key


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
    assert any("agentic.fake.run L10" in frame["name"] for frame in document["shared"]["frames"])


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


def test_speedscope_session_trace_is_partitioned_by_user(tmp_path):
    exporter = SpeedscopeExporter(output_dir=tmp_path)
    _emit_minimal_run(
        exporter,
        run_id="run-alice",
        session_id="session-shared",
        turn_id=1,
        user_id="alice@example.com",
    )
    _emit_minimal_run(
        exporter,
        run_id="run-bob",
        session_id="session-shared",
        turn_id=2,
        user_id="bob@example.com",
    )

    files = sorted(tmp_path.glob("*/session-*.speedscope.json"))
    assert {path.parent.name for path in files} == {
        safe_user_storage_key("alice@example.com"),
        safe_user_storage_key("bob@example.com"),
    }
    assert all("Run " in json.loads(path.read_text(encoding="utf-8"))["profiles"][0]["name"] for path in files)


def test_stack_frame_names_omit_source_text(tmp_path, monkeypatch):
    profile = _new_stack_profile(
        output_dir=tmp_path,
        run_id="run-frame-name",
        session_id="session-frame-name",
        turn_id=3,
    )
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)

    frame_index = profile.frame_index(internal(sys).getframe())

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
    internal(stack_profiler_module).RUNS["run-a"] = profile_a
    internal(stack_profiler_module).RUNS["run-b"] = profile_b
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
        assert internal(stack_profiler_module).THREAD_RUN_COUNTS == {}
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
    internal(stack_profiler_module).RUNS["run-a"] = profile_a
    internal(stack_profiler_module).RUNS["run-b"] = profile_b
    context_a = Context()
    context_b = Context()

    try:
        assert context_a.run(stack_profiler_module.attach_current_thread_stack_profile, "run-a")
        assert context_b.run(stack_profiler_module.attach_current_thread_stack_profile, "run-b")

        internal(stack_profiler_module).sample_profile(profile_a)

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
    internal(stack_profiler_module).RUNS["run-budget"] = profile
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)

    try:
        assert stack_profiler_module.attach_current_thread_stack_profile("run-budget")

        internal(stack_profiler_module).sample_profile(profile)
        internal(stack_profiler_module).sample_profile(profile)

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
    internal(stack_profiler_module).RUNS["run-a"] = profile_a
    internal(stack_profiler_module).RUNS["run-b"] = profile_b
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)

    try:
        assert stack_profiler_module.attach_current_thread_stack_profile("run-a")
        internal(stack_profiler_module).sample_profile(profile_a)
        stack_profiler_module.detach_current_thread_stack_profile("run-a")

        assert stack_profiler_module.attach_current_thread_stack_profile("run-b")
        internal(stack_profiler_module).sample_profile(profile_b)

        assert profile_a.total_samples == 1
        assert profile_b.total_samples == 0
        assert profile_b.thread_profile().dropped_samples == 1
        assert internal(stack_profiler_module).PROCESS_SAMPLE_COUNT == 1

        internal(stack_profiler_module).release_process_samples(profile_a.total_samples)
        internal(stack_profiler_module).sample_profile(profile_b)

        assert profile_b.total_samples == 1
        assert internal(stack_profiler_module).PROCESS_SAMPLE_COUNT == 1
    finally:
        stack_profiler_module.detach_current_thread_stack_profile("run-a")
        stack_profiler_module.detach_current_thread_stack_profile("run-b")
        _reset_stack_profiler_state()


def test_run_trace_close_cleans_started_profile_after_pre_loop_exception(tmp_path, monkeypatch):
    _reset_stack_profiler_state()
    monkeypatch.setenv("LOG_DIR", str(tmp_path))
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
        assert "run-pre-loop-error" not in internal(stack_profiler_module).RUNS

        try:
            raise RuntimeError("before agent loop")
        except RuntimeError:
            trace.close(status=RuntimeStatus.ERROR)

        assert "run-pre-loop-error" not in internal(stack_profiler_module).RUNS
        assert internal(stack_profiler_module).PROCESS_SAMPLE_COUNT == 0
        assert "run-pre-loop-error" not in internal(stack_profiler_module).RUNS

        trace.close(status=RuntimeStatus.ERROR)
        assert internal(stack_profiler_module).PROCESS_SAMPLE_COUNT == 0
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


def test_speedscope_bus_writes_incomplete_trace_when_terminal_event_drops(tmp_path):
    exporter = _BlockingSpeedscopeExporter(tmp_path)
    bus = ObservabilityBus(exporter, queue_size=1, flush_interval_seconds=60.0)
    common = {"run_id": "run-terminal-drop", "session_id": "session-terminal-drop", "turn_id": 1}

    try:
        assert bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_STARTED,
                span_key="run:run-terminal-drop",
                timestamp_ms=1000,
                payload={},
                **common,
            )
        )
        assert exporter.export_started.wait(timeout=2.0)
        assert bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.LLM_STARTED,
                span_key="llm:queued",
                parent_key="run:run-terminal-drop",
                timestamp_ms=1010,
                payload={},
                **common,
            )
        )
        assert not bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_FINISHED,
                span_key="run:run-terminal-drop",
                timestamp_ms=1020,
                payload={"status": RuntimeStatus.SUCCESS, "duration_ms": 20},
                **common,
            )
        )
    finally:
        exporter.release_export.set()
        bus.close(timeout=2.0)

    document = _load_single_speedscope_document(tmp_path)
    profile_names = [profile["name"] for profile in document["profiles"]]
    frame_names = [frame["name"] for frame in document["shared"]["frames"]]

    assert any("Run run-terminal-drop" in name and "[INCOMPLETE]" in name for name in profile_names)
    assert any("INCOMPLETE: dropped 1" in name for name in frame_names)


def test_observability_bus_drop_notice_does_not_wait_for_exporter_lock(tmp_path):
    exporter = _LockHoldingSpeedscopeExporter(tmp_path)
    bus = ObservabilityBus(exporter, queue_size=1, flush_interval_seconds=60.0)
    common = {"run_id": "run-nonblocking-drop", "session_id": "session-nonblocking-drop", "turn_id": 1}

    try:
        assert bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_STARTED,
                span_key="run:run-nonblocking-drop",
                timestamp_ms=1000,
                payload={},
                **common,
            )
        )
        assert exporter.export_started.wait(timeout=2.0)
        assert bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.LLM_STARTED,
                span_key="llm:queued",
                parent_key="run:run-nonblocking-drop",
                timestamp_ms=1010,
                payload={},
                **common,
            )
        )

        started_at = time.perf_counter()
        emitted = bus.emit(
            RuntimeEvent(
                type=RuntimeEventType.RUN_FINISHED,
                span_key="run:run-nonblocking-drop",
                timestamp_ms=1020,
                payload={"status": RuntimeStatus.SUCCESS, "duration_ms": 20},
                **common,
            )
        )
        elapsed_ms = (time.perf_counter() - started_at) * 1000

        assert emitted is False
        assert elapsed_ms < 100
    finally:
        exporter.release_export.set()
        bus.close(timeout=2.0)

    document = _load_single_speedscope_document(tmp_path)
    profile_names = [profile["name"] for profile in document["profiles"]]
    frame_names = [frame["name"] for frame in document["shared"]["frames"]]

    assert any("Run run-nonblocking-drop" in name and "[INCOMPLETE]" in name for name in profile_names)
    assert any("INCOMPLETE: dropped 1" in name for name in frame_names)


def test_speedscope_flush_writes_terminal_pending_drop_without_run_event(tmp_path):
    exporter = SpeedscopeExporter(output_dir=tmp_path)
    exporter.record_drop(
        RuntimeEvent(
            type=RuntimeEventType.RUN_FAILED,
            run_id="run-terminal-only-drop",
            session_id="session-terminal-only-drop",
            turn_id=1,
            span_key="run:run-terminal-only-drop",
            timestamp_ms=2000,
            payload={"status": RuntimeStatus.ERROR, "duration_ms": 10},
        )
    )

    exporter.flush()

    document = _load_single_speedscope_document(tmp_path)
    profile_names = [profile["name"] for profile in document["profiles"]]
    frame_names = [frame["name"] for frame in document["shared"]["frames"]]

    assert internal(exporter).pending_drops == {}
    assert any("Run run-terminal-only-drop" in name and "[INCOMPLETE]" in name for name in profile_names)
    assert any("INCOMPLETE: dropped 1" in name for name in frame_names)


def test_speedscope_pending_drops_are_bounded_and_expire(tmp_path):
    exporter = SpeedscopeExporter(
        output_dir=tmp_path,
        pending_drop_limit=2,
        pending_drop_ttl_seconds=1.0,
    )

    for index in range(3):
        exporter.record_drop(
            RuntimeEvent(
                type=RuntimeEventType.LLM_STARTED,
                run_id="run-pending-{}".format(index),
                session_id="session-pending",
                turn_id=1,
                timestamp_ms=1000 + index,
                payload={},
            )
        )

    assert len(internal(exporter).pending_drops) == 2
    assert "run-pending-0" not in internal(exporter).pending_drops

    internal(exporter).pending_drops["run-pending-1"].updated_at -= 10.0
    exporter.record_drop(
        RuntimeEvent(
            type=RuntimeEventType.LLM_STARTED,
            run_id="run-pending-new",
            session_id="session-pending",
            turn_id=1,
            timestamp_ms=2000,
            payload={},
        )
    )

    assert "run-pending-1" not in internal(exporter).pending_drops


@pytest.mark.parametrize("use_log_dir", [True, False])
def test_speedscope_writers_share_runtime_output_dir(tmp_path, monkeypatch, use_log_dir):
    _reset_stack_profiler_state()
    runtime_root = tmp_path / ".onto-runtime"
    log_dir = tmp_path / "custom-logs"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    if use_log_dir:
        monkeypatch.setenv("LOG_DIR", str(log_dir))
        output_dir = log_dir / "speedscope"
        unexpected_dir = runtime_root / "logs" / "speedscope"
    else:
        monkeypatch.delenv("LOG_DIR", raising=False)
        output_dir = runtime_root / "logs" / "speedscope"
        unexpected_dir = log_dir / "speedscope"
    monkeypatch.setattr(stack_profiler_module, "_include_frame", lambda frame: True)
    session_id = "session-runtime-output-dir"

    try:
        exporter = SpeedscopeExporter()
        _emit_minimal_run(
            exporter,
            run_id="run-evented-output-dir",
            session_id=session_id,
            turn_id=1,
        )

        stack_profiler_module.start_run_stack_profile(
            run_id="run-sampled-output-dir",
            session_id=session_id,
            turn_id=2,
            enabled=True,
        )
        profile = internal(stack_profiler_module).RUNS["run-sampled-output-dir"]
        internal(stack_profiler_module).sample_profile(profile)
        stack_profiler_module.stop_run_stack_profile(
            "run-sampled-output-dir",
            status=RuntimeStatus.SUCCESS,
        )

        document = _load_single_speedscope_document(output_dir)
        profile_names = [profile["name"] for profile in document["profiles"]]

        assert any("Run run-evented-output-dir" in name for name in profile_names)
        assert any("Run run-sampled-output-dir" in name for name in profile_names)
        assert list(unexpected_dir.glob("*.speedscope.json")) == []
    finally:
        _reset_stack_profiler_state()


def test_speedscope_prune_removes_expired_json_and_session_lock(tmp_path):
    exporter = SpeedscopeExporter(output_dir=tmp_path, retention_days=30)
    stale_path = tmp_path / "session-stale-aaaaaaaaaaaa.speedscope.json"
    stale_lock = speedscope_session_lock_path(stale_path)
    current_path = tmp_path / "session-current-bbbbbbbbbbbb.speedscope.json"
    current_lock = speedscope_session_lock_path(current_path)
    stale_path.write_text("{}", encoding="utf-8")
    stale_lock.write_text("", encoding="utf-8")
    current_path.write_text("{}", encoding="utf-8")
    current_lock.write_text("", encoding="utf-8")
    stale_timestamp = time.time() - 31 * 24 * 60 * 60
    os.utime(stale_path, (stale_timestamp, stale_timestamp))
    os.utime(stale_lock, (stale_timestamp, stale_timestamp))

    internal(exporter).prune_old_files(current_path)

    assert not stale_path.exists()
    assert not stale_lock.exists()
    assert current_path.exists()
    assert current_lock.exists()


def test_speedscope_session_file_lock_preserves_profiles_across_processes(tmp_path):
    context_name = "fork" if "fork" in multiprocessing.get_all_start_methods() else "spawn"
    process_context = multiprocessing.get_context(context_name)
    errors = process_context.Queue()
    session_id = "session-cross-process-lock"
    run_ids = []

    for iteration in range(5):
        start_event = process_context.Event()
        iteration_run_ids = [
            "run-cross-process-{}-a".format(iteration),
            "run-cross-process-{}-b".format(iteration),
        ]
        processes = [
            process_context.Process(
                target=_write_run_profiles_in_process,
                args=(str(tmp_path), session_id, run_id, index + 1, start_event, errors),
            )
            for index, run_id in enumerate(iteration_run_ids)
        ]
        for process in processes:
            process.start()
        start_event.set()
        for process in processes:
            process.join(timeout=10.0)
            if process.is_alive():
                process.terminate()
                process.join(timeout=2.0)
                raise AssertionError("child process timed out")
            assert process.exitcode == 0
        run_ids.extend(iteration_run_ids)

    child_errors = []
    while True:
        try:
            child_errors.append(errors.get_nowait())
        except queue.Empty:
            break
    assert child_errors == []

    document = _load_single_speedscope_document(tmp_path)
    profile_names = [profile["name"] for profile in document["profiles"]]
    for run_id in run_ids:
        assert any(name.startswith("Turn") and "Run {}".format(run_id) in name for name in profile_names)
        assert any(name.startswith("Code Lines") and "Run {}".format(run_id) in name for name in profile_names)


def _emit_minimal_run(
    exporter: SpeedscopeExporter,
    *,
    run_id: str,
    session_id: str,
    turn_id: int,
    user_id: str | None = None,
) -> None:
    common = {"run_id": run_id, "session_id": session_id, "turn_id": turn_id}
    exporter.export(
        RuntimeEvent(
            type=RuntimeEventType.RUN_STARTED,
            span_key="run:{}".format(run_id),
            timestamp_ms=1000,
            payload={"user_id": user_id} if user_id else {},
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
    internal(stack_profiler_module).release_process_samples(internal(stack_profiler_module).PROCESS_SAMPLE_COUNT)
    with internal(stack_profiler_module).LOCK:
        internal(stack_profiler_module).RUNS.clear()
    with internal(stack_profiler_module).THREAD_RUN_COUNTS_LOCK:
        internal(stack_profiler_module).THREAD_RUN_COUNTS.clear()
    internal(stack_profiler_module).CONTEXT_BINDINGS.set(())


def _load_single_speedscope_document(output_dir):
    paths = list(output_dir.glob("session-*.speedscope.json"))
    assert len(paths) == 1
    return json.loads(paths[0].read_text(encoding="utf-8"))


def _active_profile(document):
    return document["profiles"][document["activeProfileIndex"]]


def _write_run_profiles_in_process(  # pylint: disable=too-many-arguments,huawei-too-many-arguments
    output_dir, session_id, run_id, turn_id, start_event, errors
):
    try:
        if not start_event.wait(timeout=5.0):
            raise TimeoutError("timed out waiting to start child writer")
        exporter = SpeedscopeExporter(output_dir=output_dir)
        _emit_minimal_run(
            exporter,
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        _write_sampled_code_profile(
            output_dir=Path(output_dir),
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
        )
    except BaseException as exc:
        errors.put("{}: {}".format(type(exc).__name__, exc))
        raise


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


class _LockHoldingSpeedscopeExporter:
    available = True

    def __init__(self, output_dir):
        self.delegate = SpeedscopeExporter(output_dir=output_dir)
        self.export_started = threading.Event()
        self.release_export = threading.Event()

    def export(self, event):
        if not self.export_started.is_set():
            with internal(self.delegate).lock:
                self.export_started.set()
                self.release_export.wait(timeout=2.0)
                self.delegate.export(event)
            return
        self.delegate.export(event)

    def record_drop(self, event):
        self.delegate.record_drop(event)

    def flush(self):
        self.delegate.flush()

    def close(self):
        self.delegate.close()
