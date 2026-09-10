"""Tests for local observability perf analysis."""

from __future__ import annotations

import json
from datetime import datetime

from utils.perf_analysis.aggregate import (
    build_global_overview,
    build_run_detail,
    build_session_analysis,
)
from utils.perf_analysis.reader import LocalPerfLogReader
from utils.perf_analysis.service import check_session_availability


def _ts_ms(year: int, month: int, day: int, hour: int = 0, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute).timestamp() * 1000)


def _record(
    run_id: str,
    *,
    ts_ms: int,
    kind: str = "run_summary",
    event: str = "run.finished",
    session_id: str = "sess-a",
    turn_id: int = 1,
    status: str = "success",
    metrics: dict | None = None,
    labels: dict | None = None,
    caller: str | None = None,
    iteration: int | None = None,
) -> dict:
    record = {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": kind,
        "event": event,
        "ts_ms": ts_ms,
        "run_id": run_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "status": status,
        "metrics": metrics or {},
        "labels": labels or {},
    }
    if caller:
        record["caller"] = caller
    if iteration is not None:
        record["iteration"] = iteration
    return record


def _metric_record(
    ts_ms: int,
    name: str,
    value,
    *,
    status: str = "success",
    tool_name: str | None = None,
    token_type: str | None = None,
) -> dict:
    labels = {
        "agent.workspace_id": "ws-a",
        "agent.status": status,
        "agent.model": "glm-5",
        "agent.model_provider": "glm",
        "agent.agent_mode": "thinking",
    }
    if tool_name:
        labels["agent.tool.name"] = tool_name
    if token_type:
        labels["agent.token.type"] = token_type
    return {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": "metric",
        "event": "metric.recorded",
        "name": name,
        "ts_ms": ts_ms,
        "workspace_id": "ws-a",
        "value": value,
        "labels": labels,
    }


def _write_records(path, records) -> None:
    path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )


def _sample_perf_records() -> list[dict]:
    labels = {
        "workspace_id": "ws-a",
        "model": "glm-5",
        "model_provider": "glm",
        "agent_mode": "thinking",
    }
    return [
        _record(
            "run-1",
            ts_ms=_ts_ms(2026, 5, 22, 9),
            turn_id=1,
            metrics={
                "total_duration_ms": 1000,
                "llm_wall_total_ms": 700,
                "tool_call_total_ms": 120,
                "main_iterations": 1,
            },
            labels=labels,
        ),
        _record(
            "run-2",
            ts_ms=_ts_ms(2026, 5, 22, 10),
            turn_id=2,
            metrics={
                "total_duration_ms": 2000,
                "llm_wall_total_ms": 800,
                "tool_call_total_ms": 310,
                "main_iterations": 1,
            },
            labels=labels,
        ),
        _record(
            "run-2",
            ts_ms=_ts_ms(2026, 5, 22, 10, 1),
            kind="agent_iteration",
            event="agent_iteration.prepared",
            turn_id=2,
            caller="main",
            iteration=1,
            metrics={"prompt_build_ms": 40, "llm_input_chars": 4800},
        ),
        _record(
            "run-2",
            ts_ms=_ts_ms(2026, 5, 22, 10, 2),
            kind="agent_iteration",
            event="agent_iteration.finished",
            turn_id=2,
            caller="main",
            iteration=1,
            metrics={"total_elapsed_ms": 2000, "tools_window_ms": 310},
        ),
        _record(
            "run-2",
            ts_ms=_ts_ms(2026, 5, 22, 10, 3),
            kind="llm_call",
            event="llm.finished",
            turn_id=2,
            caller="main",
            iteration=1,
            metrics={
                "wall_duration_ms": 800,
                "prompt_chars": 4700,
                "prompt_tokens": 3000,
            },
            labels={"model_provider": "glm"},
        ),
        _record(
            "run-2",
            ts_ms=_ts_ms(2026, 5, 22, 10, 4),
            kind="tool_call",
            event="tool.finished",
            turn_id=2,
            caller="main",
            iteration=1,
            status="completed",
            metrics={"elapsed_ms": 310},
            labels={"tool_name": "ontology_context", "tool_call_id": "call-1"},
        ),
        _record(
            "run-2",
            ts_ms=_ts_ms(2026, 5, 22, 10, 5),
            kind="runtime_phase",
            event="runtime_phase.finished",
            turn_id=2,
            metrics={"duration_ms": 35},
            labels={"phase_name": "chat_session_compact", "component": "persistence"},
        ),
        _record(
            "run-other",
            ts_ms=_ts_ms(2026, 5, 22, 11),
            session_id="sess-b",
            metrics={"total_duration_ms": 9000, "llm_wall_total_ms": 8500},
            labels={**labels, "workspace_id": "ws-b"},
        ),
    ]


def test_perf_reader_scans_requested_date_window_and_reports_bad_lines(tmp_path):
    _write_records(
        tmp_path / "otel_trace-2026-05-21.log",
        [_record("run-old", ts_ms=_ts_ms(2026, 5, 21, 9))],
    )
    (tmp_path / "otel_trace-2026-05-22.log").write_text(
        json.dumps(_record("run-new", ts_ms=_ts_ms(2026, 5, 22, 9))) + "\nnot-json\n",
        encoding="utf-8",
    )

    result = LocalPerfLogReader(log_dir=tmp_path).read_records(
        start_ms=_ts_ms(2026, 5, 22),
        end_ms=_ts_ms(2026, 5, 22, 23, 59),
    )

    assert [record["run_id"] for record in result.records] == ["run-new"]
    assert result.diagnostics["scanned_files"] == 1
    assert result.diagnostics["parse_error_count"] == 1


def test_perf_reader_skips_unknown_schema_versions(tmp_path):
    _write_records(
        tmp_path / "otel_trace-2026-05-22.log",
        [{**_record("run-v3", ts_ms=_ts_ms(2026, 5, 22, 9)), "schema_version": 3}],
    )

    result = LocalPerfLogReader(log_dir=tmp_path).read_records(
        start_ms=_ts_ms(2026, 5, 22),
        end_ms=_ts_ms(2026, 5, 22, 23, 59),
    )

    assert result.records == []
    assert result.diagnostics["unknown_schema_count"] == 1


def test_perf_reader_has_record_stops_after_first_match(tmp_path):
    records = [
        _record("run-a", ts_ms=_ts_ms(2026, 5, 22, 9), kind="runtime_phase"),
        _record("run-b", ts_ms=_ts_ms(2026, 5, 22, 10), session_id="sess-b"),
        _record("run-c", ts_ms=_ts_ms(2026, 5, 22, 11), session_id="sess-c"),
    ]
    _write_records(tmp_path / "otel_trace-2026-05-22.log", records)

    result = LocalPerfLogReader(log_dir=tmp_path).has_record(
        start_ms=_ts_ms(2026, 5, 22),
        end_ms=_ts_ms(2026, 5, 22, 23, 59),
        session_id="sess-b",
        kinds={"run_summary"},
    )

    assert [record["run_id"] for record in result.records] == ["run-b"]
    assert result.diagnostics["scanned_files"] == 1


def test_check_session_availability_reads_only_run_summaries(tmp_path, monkeypatch):
    _write_records(
        tmp_path / "otel_trace-2026-05-22.log",
        [
            _record("run-a", ts_ms=_ts_ms(2026, 5, 22, 9), kind="runtime_phase"),
            _record("run-a", ts_ms=_ts_ms(2026, 5, 22, 10)),
        ],
    )
    monkeypatch.setattr(
        "utils.perf_analysis.service._reader",
        lambda workspace_id=None: LocalPerfLogReader(log_dir=tmp_path),
    )

    result = check_session_availability(
        "sess-a",
        start_ms=_ts_ms(2026, 5, 22),
        end_ms=_ts_ms(2026, 5, 22, 23, 59),
    )

    assert result["available"] is True
    assert result["session_id"] == "sess-a"


def test_build_session_analysis_groups_turns_and_totals_run_summaries():
    analysis = build_session_analysis(_sample_perf_records(), session_id="sess-a")

    assert analysis["summary"]["run_count"] == 2
    assert analysis["summary"]["total_duration_ms"] == 3000
    assert [row["turn_id"] for row in analysis["trends"]["turns"]] == [1, 2]
    assert analysis["trends"]["turns"][-1]["prompt_chars_max"] == 4700
    assert analysis["runs"][0]["run_id"] == "run-2"


def test_build_run_detail_splits_iterations_llm_tools_and_phases():
    detail = build_run_detail(_sample_perf_records(), run_id="run-2")

    assert detail["run"]["run_id"] == "run-2"
    assert detail["breakdown"] == {
        "llm_wall_ms": 800,
        "tool_ms": 310,
        "runtime_phase_ms": 35,
        "other_ms": 855,
    }
    assert detail["iterations"][0]["iteration"] == 1
    assert detail["llm_calls"][0]["metrics"]["wall_duration_ms"] == 800
    assert detail["tools"][0]["labels"]["tool_name"] == "ontology_context"
    assert detail["runtime_phases"][0]["labels"]["phase_name"] == "chat_session_compact"


def test_build_run_detail_preserves_reasoning_rollup_fields():
    records = [
        _record(
            "run-reasoning",
            ts_ms=_ts_ms(2026, 5, 22, 12),
            metrics={"total_duration_ms": 1000},
        ),
        _record(
            "run-reasoning",
            ts_ms=_ts_ms(2026, 5, 22, 12, 1),
            kind="llm_call",
            event="llm.finished",
            metrics={"reasoning_chars": 120, "first_reasoning_ms": 80},
        ),
        _record(
            "run-reasoning",
            ts_ms=_ts_ms(2026, 5, 22, 12, 2),
            kind="llm_call",
            event="llm.finished",
            metrics={"reasoning_chars": 30, "first_reasoning_ms": 120},
        ),
    ]

    detail = build_run_detail(records, run_id="run-reasoning")

    assert detail["reasoning"] == {
        "had_reasoning": True,
        "calls_with_reasoning": 2,
        "total_reasoning_chars": 150,
        "first_reasoning_ms": 80,
    }


def test_build_run_detail_groups_llm_and_tools_by_iteration_call_chain():
    detail = build_run_detail(_sample_perf_records(), run_id="run-2")

    assert len(detail["iteration_calls"]) == 1
    chain = detail["iteration_calls"][0]
    assert chain["caller"] == "main"
    assert chain["iteration"] == 1
    assert chain["llm_calls"][0]["metrics"]["wall_duration_ms"] == 800
    assert chain["tools"][0]["labels"]["tool_call_id"] == "call-1"
    assert chain["summary"] == {
        "llm_wall_ms": 800,
        "tool_count": 1,
    }


def test_build_global_overview_filters_workspace_and_ranks_tools():
    overview = build_global_overview(_sample_perf_records(), workspace_id="ws-a")

    assert overview["summary"]["run_count"] == 2
    assert overview["summary"]["total_duration_ms"]["p90"] >= 1000
    assert overview["tools"][0]["tool_name"] == "ontology_context"
    assert overview["runtime_phases"][0]["phase_name"] == "chat_session_compact"
    assert [run["run_id"] for run in overview["slow_runs"]] == ["run-2", "run-1"]


def test_perf_analysis_uses_otel_metrics_when_run_summaries_are_missing():
    first_ts = _ts_ms(2026, 5, 22, 9)
    second_ts = _ts_ms(2026, 5, 22, 10)
    records = [
        _metric_record(first_ts, "agent.run.count", 1),
        _metric_record(
            first_ts,
            "agent.run.duration",
            {
                "count": 1,
                "sum": 1000,
                "min": 1000,
                "max": 1000,
                "bucket_counts": [0, 1],
                "explicit_bounds": [500],
            },
        ),
        _metric_record(
            first_ts,
            "agent.llm.failure.count",
            1,
            status="error",
        ),
        _metric_record(
            first_ts,
            "agent.llm.token.count",
            14,
            token_type="total_tokens",
        ),
        _metric_record(
            first_ts,
            "agent.tool.call.count",
            1,
            tool_name="search",
        ),
        _metric_record(
            first_ts,
            "agent.tool.duration",
            {"count": 1, "sum": 120, "min": 120, "max": 120},
            tool_name="search",
        ),
        _metric_record(
            first_ts,
            "agent.tool.failure.count",
            1,
            status="error",
            tool_name="search",
        ),
        _metric_record(second_ts, "agent.run.count", 1, status="error"),
        _metric_record(
            second_ts,
            "agent.run.duration",
            {"count": 1, "sum": 3000, "min": 3000, "max": 3000},
            status="error",
        ),
    ]

    overview = build_global_overview(
        records,
        workspace_id="ws-a",
        model_provider="glm",
        agent_mode="thinking",
    )

    assert overview["summary"]["run_count"] == 2
    assert overview["summary"]["success_count"] == 1
    assert overview["summary"]["error_count"] == 1
    assert overview["summary"]["total_tokens"] == 14
    assert overview["summary"]["llm_failures"] == 1
    assert overview["summary"]["tool_failures"] == 1
    assert overview["summary"]["total_duration_ms"]["p90"] > 1000
    assert overview["tools"] == [
        {
            "tool_name": "search",
            "count": 1,
            "total_ms": 120,
            "max_ms": 120,
            "avg_ms": 120,
        }
    ]


def test_run_timeline_includes_safe_span_events_without_duplicate_end_events():
    ts_ms = _ts_ms(2026, 5, 22, 12)
    span_event = {
        "schema_version": 2,
        "record_type": "opentelemetry",
        "kind": "span_event",
        "event": "tool.progress",
        "ts_ms": ts_ms + 10,
        "run_id": "run-events",
        "session_id": "sess-a",
        "trace_id": "1" * 32,
        "span_id": "2" * 16,
        "labels": {"tool_name": "search"},
        "metrics": {},
    }
    duplicate_finished = {
        **span_event,
        "event": "tool.finished",
        "ts_ms": ts_ms + 20,
    }
    records = [
        _record(
            "run-events",
            ts_ms=ts_ms,
            metrics={"total_duration_ms": 20},
        ),
        {
            **span_event,
            "kind": "tool_call",
            "event": "tool.finished",
            "ts_ms": ts_ms + 20,
        },
        {
            "schema_version": 2,
            "record_type": "opentelemetry",
            "kind": "span",
            "event": "span.ended",
            "ts_ms": ts_ms + 20,
            "run_id": "run-events",
            "session_id": "sess-a",
            "trace_id": "1" * 32,
            "span_id": "2" * 16,
            "labels": {},
            "metrics": {"duration_ms": 20},
            "events": [span_event, duplicate_finished],
        },
    ]

    detail = build_run_detail(records, run_id="run-events")

    assert sum(row["event"] == "tool.progress" for row in detail["timeline"]) == 1
    assert sum(row["event"] == "tool.finished" for row in detail["timeline"]) == 1


def test_perf_analysis_falls_back_to_stream_llm_duration_when_wall_missing():
    records = [
        _record(
            "run-stream-only",
            ts_ms=_ts_ms(2026, 5, 22, 12),
            metrics={
                "total_duration_ms": 10_000,
                "llm_stream_total_ms": 4_200,
                "tool_call_total_ms": 500,
            },
        ),
        _record(
            "run-stream-only",
            ts_ms=_ts_ms(2026, 5, 22, 12, 1),
            kind="llm_call",
            event="llm.finished",
            metrics={"duration_ms": 4_200, "prompt_chars": 100},
        ),
    ]

    session = build_session_analysis(records, session_id="sess-a")
    detail = build_run_detail(records, run_id="run-stream-only")
    overview = build_global_overview(records)

    assert session["summary"]["llm_wall_total_ms"] == 4_200
    assert session["runs"][0]["llm_wall_total_ms"] == 4_200
    assert detail["breakdown"]["llm_wall_ms"] == 4_200
    assert overview["summary"]["llm_wall_ms"]["p50"] == 4_200


def test_build_run_detail_falls_back_to_tool_records_for_breakdown_tool_ms():
    records = [
        _record(
            "run-tool-fallback",
            ts_ms=_ts_ms(2026, 5, 22, 12),
            metrics={
                "total_duration_ms": 2000,
                "llm_wall_total_ms": 700,
            },
        ),
        _record(
            "run-tool-fallback",
            ts_ms=_ts_ms(2026, 5, 22, 12, 1),
            kind="tool_call",
            event="tool.finished",
            metrics={"elapsed_ms": 120},
            labels={"tool_name": "ontology_context"},
        ),
        _record(
            "run-tool-fallback",
            ts_ms=_ts_ms(2026, 5, 22, 12, 2),
            kind="tool_call",
            event="tool.finished",
            metrics={"duration_ms": 80},
            labels={"tool_name": "business_data_search"},
        ),
    ]

    detail = build_run_detail(records, run_id="run-tool-fallback")

    assert detail["breakdown"] == {
        "llm_wall_ms": 700,
        "tool_ms": 200,
        "runtime_phase_ms": 0,
        "other_ms": 1100,
    }


def test_perf_analysis_aggregates_llm_cache_metrics_by_tokens():
    records = [
        _record(
            "run-cache",
            ts_ms=_ts_ms(2026, 5, 22, 12),
            metrics={
                "total_duration_ms": 1000,
                "llm_wall_total_ms": 500,
                "llm_cache_hit_tokens": 15,
                "llm_cache_total_input_tokens": 30,
                "llm_cache_hit_rate": 0.5,
            },
        ),
        _record(
            "run-cache",
            ts_ms=_ts_ms(2026, 5, 22, 12, 1),
            kind="llm_call",
            event="llm.finished",
            metrics={
                "wall_duration_ms": 200,
                "cache_hit_tokens": 10,
                "cache_total_input_tokens": 10,
                "cache_hit_rate": 1.0,
            },
        ),
        _record(
            "run-cache",
            ts_ms=_ts_ms(2026, 5, 22, 12, 2),
            kind="llm_call",
            event="llm.finished",
            metrics={
                "wall_duration_ms": 300,
                "cache_hit_tokens": 5,
                "cache_total_input_tokens": 20,
                "cache_hit_rate": 0.25,
            },
        ),
    ]

    session = build_session_analysis(records, session_id="sess-a")
    detail = build_run_detail(records, run_id="run-cache")
    overview = build_global_overview(records)

    assert session["runs"][0]["llm_cache_hit_tokens"] == 15
    assert session["runs"][0]["llm_cache_total_input_tokens"] == 30
    assert session["runs"][0]["llm_cache_hit_rate"] == 0.5
    assert detail["run"]["llm_cache_hit_tokens"] == 15
    assert detail["run"]["llm_cache_total_input_tokens"] == 30
    assert detail["run"]["llm_cache_hit_rate"] == 0.5
    assert overview["summary"]["llm_cache_hit_tokens"] == 15
    assert overview["summary"]["llm_cache_total_input_tokens"] == 30
    assert overview["summary"]["llm_cache_hit_rate"] == 0.5


def test_local_otel_store_rolls_partitions_and_rebuilds_index(tmp_path):
    from integration.observability.local_storage import IndexedLocalOTelStore

    store = IndexedLocalOTelStore(tmp_path, retention_days=30, max_bytes=1024)
    first = _record("run-index-a", ts_ms=_ts_ms(2026, 5, 22, 12))
    second = _record("run-index-b", ts_ms=_ts_ms(2026, 5, 22, 12, 1))
    first["padding"] = "a" * 600
    second["padding"] = "b" * 600
    store.append(first)
    store.append(second)

    partitions = sorted(tmp_path.glob("otel_trace-2026-05-22.log*"))
    data_partitions = [path for path in partitions if not path.name.endswith(".idx")]
    assert [path.name for path in data_partitions] == [
        "otel_trace-2026-05-22.log",
        "otel_trace-2026-05-22.log.1",
    ]

    for path in tmp_path.glob("*.idx"):
        path.unlink()
    result = LocalPerfLogReader(log_dir=tmp_path).read_records(
        start_ms=_ts_ms(2026, 5, 22),
        end_ms=_ts_ms(2026, 5, 23),
        run_id="run-index-b",
    )
    assert [record["run_id"] for record in result.records] == ["run-index-b"]
    assert len(list(tmp_path.glob("*.idx"))) == 2


def test_local_otel_store_repairs_unindexed_data_before_append(tmp_path):
    from integration.observability.local_storage import (
        IndexedLocalOTelStore,
        read_or_rebuild_index,
    )

    store = IndexedLocalOTelStore(tmp_path, retention_days=30)
    records = [
        _record("run-index-{}".format(suffix), ts_ms=_ts_ms(2026, 5, 22, 12, minute))
        for minute, suffix in enumerate(("a", "b", "c"))
    ]
    store.append(records[0])
    data_path = tmp_path / "otel_trace-2026-05-22.log"
    with data_path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(records[1]) + "\n")

    store.append(records[2])

    sidecar_path = data_path.with_name(data_path.name + ".idx")
    raw_entries = [json.loads(line) for line in sidecar_path.read_text(encoding="utf-8").splitlines()]
    assert [entry["run_id"] for entry in raw_entries] == [
        "run-index-a",
        "run-index-b",
        "run-index-c",
    ]

    entries = read_or_rebuild_index(data_path)
    assert [entry["run_id"] for entry in entries] == [
        "run-index-a",
        "run-index-b",
        "run-index-c",
    ]
    assert entries[0]["offset"] == 0
    assert all(current["data_size"] == following["offset"] for current, following in zip(entries, entries[1:]))

    index_lines = sidecar_path.read_text(encoding="utf-8").splitlines()
    sidecar_path.write_text("\n".join((index_lines[0], index_lines[2])) + "\n", encoding="utf-8")

    rebuilt_entries = read_or_rebuild_index(data_path)
    assert [entry["run_id"] for entry in rebuilt_entries] == [
        "run-index-a",
        "run-index-b",
        "run-index-c",
    ]
