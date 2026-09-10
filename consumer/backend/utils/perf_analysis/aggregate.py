"""Aggregation helpers for local observability perf records."""

from __future__ import annotations

from collections import defaultdict
from math import ceil, floor
from typing import Any, Iterable, Optional

_SUCCESS_STATUSES = {"success", "completed"}
_DISTRIBUTION_LIMITS = (
    (1000, "<1s"),
    (3000, "1-3s"),
    (10000, "3-10s"),
    (30000, "10-30s"),
    (60000, "30-60s"),
)


def build_run_detail(records: Iterable[dict[str, Any]], *, run_id: str) -> dict[str, Any]:
    run_records = sorted(
        [record for record in records if str(record.get("run_id") or "") == run_id],
        key=_record_sort_key,
    )
    summary = _latest_summary(run_records)
    run_record = _run_overview_record(run_records)
    total_duration_ms = _metric(summary, "total_duration_ms") or _metric(run_record, "duration_ms")

    llm_calls = [record for record in run_records if record.get("kind") == "llm_call"]
    llm_cache_metrics = _cache_metrics_from_llm_calls(llm_calls)
    tools = [
        record
        for record in run_records
        if record.get("kind") == "tool_call" and str(record.get("event") or "").endswith("finished")
    ]
    runtime_phases = [
        record
        for record in run_records
        if record.get("kind") == "runtime_phase" and str(record.get("event") or "") != "runtime_phase.started"
    ]
    tool_batches = [record for record in run_records if record.get("kind") == "tool_batch"]
    reasoning_rollup = _reasoning_rollup_from_llm_calls(llm_calls)
    llm_wall_ms = _summary_llm_wall_ms(summary) or sum(_llm_call_wall_ms(record) for record in llm_calls)
    tool_ms = _metric(summary, "tool_call_total_ms") or sum(_tool_call_ms(record) for record in tools)
    runtime_phase_ms = sum(_metric(record, "duration_ms") for record in runtime_phases)
    other_ms = max(0, total_duration_ms - llm_wall_ms - tool_ms - runtime_phase_ms)

    labels = {}
    labels.update(_labels(run_record))
    labels.update(_labels(summary))
    run = {
        "run_id": run_id,
        "session_id": _first_present(summary, run_record, key="session_id"),
        "turn_id": _first_present(summary, run_record, key="turn_id"),
        "status": _first_present(summary, run_record, key="status") or "unknown",
        "ts_ms": _first_present(summary, run_record, key="ts_ms"),
        "workspace_id": labels.get("workspace_id"),
        "model": labels.get("model"),
        "model_provider": labels.get("model_provider"),
        "agent_mode": labels.get("agent_mode"),
        "selected_ontology_id": labels.get("selected_ontology_id"),
        "selected_ontology_version": labels.get("selected_ontology_version"),
        "total_duration_ms": total_duration_ms,
        "llm_cache_hit_tokens": (
            llm_cache_metrics.get("llm_cache_hit_tokens")
            if llm_cache_metrics
            else _metric_optional_int(summary, "llm_cache_hit_tokens")
        ),
        "llm_cache_total_input_tokens": (
            llm_cache_metrics.get("llm_cache_total_input_tokens")
            if llm_cache_metrics
            else _metric_optional_int(summary, "llm_cache_total_input_tokens")
        ),
        "llm_cache_hit_rate": (
            llm_cache_metrics.get("llm_cache_hit_rate")
            if llm_cache_metrics
            else _metric_float(summary, "llm_cache_hit_rate")
        ),
        "prompt_tokens": _metric(summary, "prompt_tokens"),
        "completion_tokens": _metric(summary, "completion_tokens"),
        "total_tokens": _metric(summary, "total_tokens"),
        "llm_failures": _metric(summary, "llm_failures"),
        "tool_failures": _metric(summary, "tool_failures"),
    }
    return {
        "run": run,
        "breakdown": {
            "llm_wall_ms": llm_wall_ms,
            "tool_ms": tool_ms,
            "runtime_phase_ms": runtime_phase_ms,
            "other_ms": other_ms,
        },
        "iterations": _build_iterations(run_records),
        "iteration_calls": _build_iteration_calls(run_records, llm_calls, tools),
        "llm_calls": llm_calls,
        "reasoning": reasoning_rollup,
        "tools": tools,
        "tool_batches": tool_batches,
        "runtime_phases": runtime_phases,
        "timeline": _build_timeline(run_records),
    }


def build_session_analysis(records: Iterable[dict[str, Any]], *, session_id: str) -> dict[str, Any]:
    session_records = [record for record in records if str(record.get("session_id") or "") == str(session_id)]
    summaries = _summary_rows(session_records)
    summaries_by_run = {row["run_id"]: row for row in summaries}
    prompt_metrics = _prompt_metrics_by_run(session_records)
    cache_metrics = _cache_metrics_by_run(session_records)
    trend_rows = []
    for row in sorted(summaries, key=lambda item: (_sort_optional_int(item.get("turn_id")), item["ts_ms"])):
        trend_rows.append({**row, **prompt_metrics.get(row["run_id"], {}), **cache_metrics.get(row["run_id"], {})})

    statuses = [str(row.get("status") or "").lower() for row in summaries]
    return {
        "session_id": session_id,
        "summary": {
            "turn_count": len({row.get("turn_id") for row in summaries if row.get("turn_id") is not None}),
            "run_count": len(summaries),
            "success_count": sum(status in _SUCCESS_STATUSES for status in statuses),
            "error_count": sum(status == "error" for status in statuses),
            "cancelled_count": sum(status == "cancelled" for status in statuses),
            "total_duration_ms": sum(row["total_duration_ms"] for row in summaries),
            "llm_wall_total_ms": sum(row["llm_wall_total_ms"] for row in summaries),
            "prompt_tokens": sum(row["prompt_tokens"] for row in summaries),
            "completion_tokens": sum(row["completion_tokens"] for row in summaries),
            "total_tokens": sum(row["total_tokens"] for row in summaries),
            "llm_failures": sum(row["llm_failures"] for row in summaries),
            "tool_failures": sum(row["tool_failures"] for row in summaries),
        },
        "trends": {"turns": trend_rows},
        "runs": sorted(
            (
                {**row, **prompt_metrics.get(run_id, {}), **cache_metrics.get(run_id, {})}
                for run_id, row in summaries_by_run.items()
            ),
            key=lambda item: (_sort_optional_int(item.get("turn_id")), item["ts_ms"]),
            reverse=True,
        ),
    }


def build_global_overview(
    records: Iterable[dict[str, Any]],
    *,
    workspace_id: Optional[str] = None,
    status: Optional[str] = None,
    model_provider: Optional[str] = None,
    agent_mode: Optional[str] = None,
) -> dict[str, Any]:
    record_list = list(records)
    summaries = filter_summary_rows(
        _summary_rows(record_list),
        workspace_id=workspace_id,
        status=status,
        model_provider=model_provider,
        agent_mode=agent_mode,
    )
    run_ids = {row["run_id"] for row in summaries}
    durations = [row["total_duration_ms"] for row in summaries]
    llm_wall = [row["llm_wall_total_ms"] for row in summaries]
    statuses = [str(row.get("status") or "").lower() for row in summaries]
    cache_hit_tokens = sum(_as_int(row.get("llm_cache_hit_tokens")) for row in summaries)
    cache_total_input_tokens = sum(_as_int(row.get("llm_cache_total_input_tokens")) for row in summaries)
    aggregate_metrics = _aggregate_metric_records(
        record_list,
        workspace_id=workspace_id,
        status=status,
        model_provider=model_provider,
        agent_mode=agent_mode,
    )
    use_aggregate_metrics = any(str(record.get("name") or "") == "agent.run.count" for record in aggregate_metrics)
    if use_aggregate_metrics:
        run_count = _sum_metric_points(aggregate_metrics, "agent.run.count")
        success_count = _sum_metric_points(
            aggregate_metrics,
            "agent.run.count",
            label_name="agent.status",
            label_value="success",
        )
        error_count = _sum_metric_points(
            aggregate_metrics,
            "agent.run.count",
            label_name="agent.status",
            label_value="error",
        )
        cancelled_count = _sum_metric_points(
            aggregate_metrics,
            "agent.run.count",
            label_name="agent.status",
            label_value="cancelled",
        )
        total_duration_percentiles = _metric_histogram_percentiles(
            aggregate_metrics,
            "agent.run.duration",
        )
        llm_wall_percentiles = _metric_histogram_percentiles(
            aggregate_metrics,
            "agent.llm.duration",
        )
        prompt_tokens = _sum_metric_points(
            aggregate_metrics,
            "agent.llm.token.count",
            label_name="agent.token.type",
            label_value="prompt_tokens",
        )
        completion_tokens = _sum_metric_points(
            aggregate_metrics,
            "agent.llm.token.count",
            label_name="agent.token.type",
            label_value="completion_tokens",
        )
        total_tokens = _sum_metric_points(
            aggregate_metrics,
            "agent.llm.token.count",
            label_name="agent.token.type",
            label_value="total_tokens",
        )
        llm_failures = _sum_metric_points(aggregate_metrics, "agent.llm.failure.count")
        tool_failures = _sum_metric_points(aggregate_metrics, "agent.tool.failure.count")
        tools = _rank_aggregate_metric_tools(aggregate_metrics)
    else:
        run_count = len(summaries)
        success_count = sum(item in _SUCCESS_STATUSES for item in statuses)
        error_count = sum(item == "error" for item in statuses)
        cancelled_count = sum(item == "cancelled" for item in statuses)
        total_duration_percentiles = _percentiles(durations)
        llm_wall_percentiles = _percentiles(llm_wall)
        prompt_tokens = sum(row["prompt_tokens"] for row in summaries)
        completion_tokens = sum(row["completion_tokens"] for row in summaries)
        total_tokens = sum(row["total_tokens"] for row in summaries)
        llm_failures = sum(row["llm_failures"] for row in summaries)
        tool_failures = sum(row["tool_failures"] for row in summaries)
        tools = _rank_tools(record_list, run_ids)
    return {
        "filters": {
            "workspace_id": workspace_id,
            "status": status,
            "model_provider": model_provider,
            "agent_mode": agent_mode,
        },
        "summary": {
            "run_count": run_count,
            "success_rate": _ratio(success_count, run_count),
            "success_count": success_count,
            "error_count": error_count,
            "cancelled_count": cancelled_count,
            "total_duration_ms": total_duration_percentiles,
            "llm_wall_ms": llm_wall_percentiles,
            "llm_cache_hit_tokens": cache_hit_tokens,
            "llm_cache_total_input_tokens": cache_total_input_tokens,
            "llm_cache_hit_rate": (
                _ratio(cache_hit_tokens, cache_total_input_tokens) if cache_total_input_tokens else None
            ),
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "llm_failures": llm_failures,
            "tool_failures": tool_failures,
        },
        "trends": _build_hourly_trends(summaries),
        "duration_buckets": _duration_buckets(durations),
        "slow_runs": sorted(summaries, key=lambda item: item["total_duration_ms"], reverse=True)[:20],
        "tools": tools,
        "runtime_phases": _rank_runtime_phases(record_list, run_ids),
        "failures": [row for row in summaries if str(row.get("status") or "").lower() not in _SUCCESS_STATUSES][:20],
    }


def _aggregate_metric_records(
    records: Iterable[dict[str, Any]],
    *,
    workspace_id: Optional[str],
    status: Optional[str],
    model_provider: Optional[str],
    agent_mode: Optional[str],
) -> list[dict[str, Any]]:
    normalized_status = str(status or "").strip().lower()
    if normalized_status == "completed":
        normalized_status = "success"
    matching = []
    for record in records:
        if record.get("kind") != "metric" or str(record.get("run_id") or "").strip():
            continue
        record_workspace_id = str(record.get("workspace_id") or _otel_label(record, "agent.workspace_id") or "")
        if workspace_id and record_workspace_id != workspace_id:
            continue
        record_status = str(_otel_label(record, "agent.status") or "").lower()
        if normalized_status and record_status != normalized_status:
            continue
        record_provider = str(_otel_label(record, "agent.model_provider") or "")
        if model_provider and record_provider != model_provider:
            continue
        record_agent_mode = str(_otel_label(record, "agent.agent_mode") or "")
        if agent_mode and record_agent_mode != agent_mode:
            continue
        matching.append(record)
    return matching


def _sum_metric_points(
    records: Iterable[dict[str, Any]],
    metric_name: str,
    *,
    label_name: Optional[str] = None,
    label_value: Optional[str] = None,
) -> int:
    total = 0
    for record in records:
        if str(record.get("name") or "") != metric_name:
            continue
        if label_name is not None:
            record_label = str(_otel_label(record, label_name) or "")
            if record_label != str(label_value or ""):
                continue
        total += _as_int(_metric_point_total(record))
    return total


def _metric_histogram_percentiles(
    records: Iterable[dict[str, Any]],
    metric_name: str,
) -> dict[str, int]:
    matching = []
    for record in records:
        if str(record.get("name") or "") != metric_name:
            continue
        if isinstance(record.get("value"), dict):
            matching.append(record)
    if not matching:
        return _percentiles([])

    bounds: Optional[list[float]] = None
    bucket_counts: list[int] = []
    maximum = 0
    compatible = True
    for record in matching:
        value = record.get("value") or {}
        current_bounds = [
            float(item)
            for item in value.get("explicit_bounds") or ()
            if isinstance(item, (int, float)) and not isinstance(item, bool)
        ]
        current_counts = [_as_int(item) for item in value.get("bucket_counts") or ()]
        maximum = max(maximum, _as_int(value.get("max")))
        if not current_counts or len(current_counts) != len(current_bounds) + 1:
            compatible = False
            break
        if bounds is None:
            bounds = current_bounds
            bucket_counts = [0] * len(current_counts)
        elif current_bounds != bounds:
            compatible = False
            break
        for index, count in enumerate(current_counts):
            bucket_counts[index] += count

    if compatible and bounds is not None and sum(bucket_counts) > 0:
        return {
            name: _bucket_percentile(bucket_counts, bounds, fraction, maximum=maximum)
            for name, fraction in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99))
        }

    weighted_values = []
    for record in matching:
        count = _as_int(_histogram_count(record))
        if count <= 0:
            continue
        weighted_values.append((_as_int(_histogram_average(record)), count))
    return {
        name: _weighted_percentile(weighted_values, fraction)
        for name, fraction in (("p50", 0.50), ("p90", 0.90), ("p99", 0.99))
    }


def _bucket_percentile(
    counts: list[int],
    bounds: list[float],
    fraction: float,
    *,
    maximum: int,
) -> int:
    target = max(1, ceil(sum(counts) * fraction))
    cumulative = 0
    for index, count in enumerate(counts):
        cumulative += count
        if cumulative < target:
            continue
        if index < len(bounds):
            return round(bounds[index])
        return max(0, maximum)
    return max(0, maximum)


def _weighted_percentile(values: list[tuple[int, int]], fraction: float) -> int:
    if not values:
        return 0
    ordered = sorted((value, count) for value, count in values if count > 0)
    target = max(1, ceil(sum(count for _value, count in ordered) * fraction))
    cumulative = 0
    for value, count in ordered:
        cumulative += count
        if cumulative >= target:
            return value
    return ordered[-1][0]


def _rank_aggregate_metric_tools(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    ranks: dict[str, dict[str, Any]] = {}
    for record in records:
        name = str(record.get("name") or "")
        if name not in {"agent.tool.call.count", "agent.tool.duration"}:
            continue
        tool_name = str(_otel_label(record, "agent.tool.name") or "unknown")
        row = ranks.setdefault(
            tool_name,
            {
                "tool_name": tool_name,
                "call_count": 0,
                "duration_count": 0,
                "total_ms": 0,
                "max_ms": 0,
            },
        )
        if name == "agent.tool.call.count":
            row["call_count"] += _as_int(_metric_point_total(record))
        else:
            row["total_ms"] += _as_int(_histogram_sum(record))
            row["max_ms"] = max(row["max_ms"], _as_int(_histogram_max(record)))
            row["duration_count"] += _as_int(_histogram_count(record))
    resolved = []
    for row in ranks.values():
        call_count = row.pop("call_count")
        duration_count = row.pop("duration_count")
        row["count"] = call_count or duration_count
        row["avg_ms"] = round(row["total_ms"] / row["count"]) if row["count"] else 0
        resolved.append(row)
    return sorted(resolved, key=lambda item: (item["total_ms"], item["max_ms"]), reverse=True)


def filter_summary_rows(
    summaries: Iterable[dict[str, Any]],
    *,
    workspace_id: Optional[str] = None,
    status: Optional[str] = None,
    model_provider: Optional[str] = None,
    agent_mode: Optional[str] = None,
) -> list[dict[str, Any]]:
    normalized_status = str(status or "").strip().lower()
    resolved = []
    for row in summaries:
        workspace_matches = not workspace_id or row.get("workspace_id") == workspace_id
        status_matches = not normalized_status or str(row.get("status") or "").lower() == normalized_status
        provider_matches = not model_provider or row.get("model_provider") == model_provider
        mode_matches = not agent_mode or row.get("agent_mode") == agent_mode
        if workspace_matches and status_matches and provider_matches and mode_matches:
            resolved.append(row)
    return resolved


def summary_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    return _summary_rows(records)


def _summary_rows(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "run_summary":
            continue
        run_id = str(record.get("run_id") or "")
        if not run_id:
            continue
        current = latest.get(run_id)
        if current is None or _record_sort_key(record) >= _record_sort_key(current):
            latest[run_id] = record
    return [_summary_row(record) for record in latest.values()]


def _summary_row(record: dict[str, Any]) -> dict[str, Any]:
    labels = _labels(record)
    return {
        "run_id": str(record.get("run_id") or ""),
        "session_id": record.get("session_id"),
        "turn_id": record.get("turn_id"),
        "status": record.get("status") or "unknown",
        "ts_ms": _as_int(record.get("ts_ms")),
        "total_duration_ms": _metric(record, "total_duration_ms"),
        "llm_wall_total_ms": _summary_llm_wall_ms(record),
        "llm_stream_total_ms": _metric(record, "llm_stream_total_ms"),
        "main_iterations": _metric(record, "main_iterations"),
        "llm_calls": _metric(record, "llm_calls"),
        "tool_calls_finished": _metric(record, "tool_calls_finished"),
        "prompt_tokens": _metric(record, "prompt_tokens"),
        "completion_tokens": _metric(record, "completion_tokens"),
        "total_tokens": _metric(record, "total_tokens"),
        "llm_failures": _metric(record, "llm_failures"),
        "tool_failures": _metric(record, "tool_failures"),
        "llm_cache_hit_tokens": _metric_optional_int(record, "llm_cache_hit_tokens"),
        "llm_cache_total_input_tokens": _metric_optional_int(record, "llm_cache_total_input_tokens"),
        "llm_cache_hit_rate": _metric_float(record, "llm_cache_hit_rate"),
        "workspace_id": labels.get("workspace_id"),
        "model": labels.get("model"),
        "model_provider": labels.get("model_provider"),
        "agent_mode": labels.get("agent_mode"),
        "selected_ontology_id": labels.get("selected_ontology_id"),
        "selected_ontology_version": labels.get("selected_ontology_version"),
    }


def _build_iterations(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    iterations: dict[tuple[str, int], dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "agent_iteration":
            continue
        iteration = _as_optional_int(record.get("iteration"))
        if iteration is None:
            continue
        caller = str(record.get("caller") or "main")
        key = (caller, iteration)
        row = iterations.setdefault(
            key,
            {
                "caller": caller,
                "iteration": iteration,
                "status": None,
                "first_ts_ms": _as_int(record.get("ts_ms")),
                "last_ts_ms": _as_int(record.get("ts_ms")),
                "metrics": {},
            },
        )
        row["first_ts_ms"] = min(row["first_ts_ms"], _as_int(record.get("ts_ms")))
        row["last_ts_ms"] = max(row["last_ts_ms"], _as_int(record.get("ts_ms")))
        row["metrics"].update(_metrics(record))
        if record.get("status"):
            row["status"] = record.get("status")
    return sorted(iterations.values(), key=lambda row: (row["caller"], row["iteration"]))


def _build_iteration_calls(
    records: list[dict[str, Any]],
    llm_calls: list[dict[str, Any]],
    tools: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    groups: dict[tuple[str, int], dict[str, Any]] = {}

    for record in records:
        if record.get("kind") not in {"agent_iteration", "tool_batch"}:
            continue
        key = _iteration_key(record)
        if key is None:
            continue
        group = _iteration_call_group(groups, key)
        if record.get("kind") == "agent_iteration":
            group["iteration_events"].append(record)
        elif record.get("kind") == "tool_batch":
            group["tool_batches"].append(record)

    for record in llm_calls:
        key = _iteration_key(record)
        if key is None:
            continue
        _iteration_call_group(groups, key)["llm_calls"].append(record)

    for record in tools:
        key = _iteration_key(record)
        if key is None:
            continue
        _iteration_call_group(groups, key)["tools"].append(record)

    rows = []
    for (caller, iteration), group in groups.items():
        llm_wall_ms = sum(_llm_call_wall_ms(record) for record in group["llm_calls"])
        rows.append(
            {
                "caller": caller,
                "iteration": iteration,
                "first_ts_ms": _group_first_ts(group),
                "last_ts_ms": _group_last_ts(group),
                "iteration_events": sorted(group["iteration_events"], key=_record_sort_key),
                "llm_calls": sorted(group["llm_calls"], key=_record_sort_key),
                "tool_batches": sorted(group["tool_batches"], key=_record_sort_key),
                "tools": sorted(group["tools"], key=_record_sort_key),
                "summary": {
                    "llm_wall_ms": llm_wall_ms,
                    "tool_count": len(group["tools"]),
                },
            }
        )
    return sorted(rows, key=lambda row: (row["caller"], row["iteration"]))


def _iteration_call_group(groups: dict[tuple[str, int], dict[str, Any]], key: tuple[str, int]) -> dict[str, Any]:
    caller, iteration = key
    return groups.setdefault(
        key,
        {
            "caller": caller,
            "iteration": iteration,
            "iteration_events": [],
            "llm_calls": [],
            "tool_batches": [],
            "tools": [],
        },
    )


def _iteration_key(record: dict[str, Any]) -> Optional[tuple[str, int]]:
    iteration = _as_optional_int(record.get("iteration"))
    if iteration is None:
        return None
    return str(record.get("caller") or "main"), iteration


def _group_records(group: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        *group["iteration_events"],
        *group["llm_calls"],
        *group["tool_batches"],
        *group["tools"],
    ]


def _group_first_ts(group: dict[str, Any]) -> int:
    return min((_as_int(record.get("ts_ms")) for record in _group_records(group)), default=0)


def _group_last_ts(group: dict[str, Any]) -> int:
    return max((_as_int(record.get("ts_ms")) for record in _group_records(group)), default=0)


def _prompt_metrics_by_run(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, int]]:
    by_run: dict[str, dict[str, int]] = defaultdict(dict)
    for record in records:
        run_id = str(record.get("run_id") or "")
        if not run_id:
            continue
        metrics = _metrics(record)
        if record.get("kind") == "llm_call":
            _keep_max(by_run[run_id], "prompt_chars_max", metrics.get("prompt_chars"))
            _keep_max(by_run[run_id], "prompt_tokens_max", metrics.get("prompt_tokens"))
        if record.get("kind") == "agent_iteration":
            _keep_max(by_run[run_id], "llm_input_chars_max", metrics.get("llm_input_chars"))
    return by_run


def _cache_metrics_by_run(records: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    calls_by_run: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        run_id = str(record.get("run_id") or "")
        if run_id and record.get("kind") == "llm_call":
            calls_by_run[run_id].append(record)
    metrics_by_run: dict[str, dict[str, Any]] = {}
    for run_id, calls in calls_by_run.items():
        metrics = _cache_metrics_from_llm_calls(calls)
        if metrics:
            metrics_by_run[run_id] = metrics
    return metrics_by_run


def _reasoning_rollup_from_llm_calls(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    """Summarize reasoning presence and timing without storing reasoning text."""

    calls_with_reasoning = 0
    total_reasoning_chars = 0
    first_reasoning_ms: int | None = None
    for record in records:
        metrics = _metrics(record)
        chars = _as_int(metrics.get("reasoning_chars"))
        if chars <= 0:
            continue
        calls_with_reasoning += 1
        total_reasoning_chars += chars
        call_first = _as_optional_int(metrics.get("first_reasoning_ms"))
        if call_first is not None and (first_reasoning_ms is None or call_first < first_reasoning_ms):
            first_reasoning_ms = call_first
    return {
        "had_reasoning": calls_with_reasoning > 0,
        "calls_with_reasoning": calls_with_reasoning,
        "total_reasoning_chars": total_reasoning_chars,
        "first_reasoning_ms": first_reasoning_ms,
    }


def _cache_metrics_from_llm_calls(records: Iterable[dict[str, Any]]) -> dict[str, Any]:
    hit_tokens = 0
    total_input_tokens = 0
    for record in records:
        metrics = _metrics(record)
        call_total = _as_optional_int(metrics.get("cache_total_input_tokens"))
        if call_total is None:
            continue
        hit_tokens += _as_int(metrics.get("cache_hit_tokens"))
        total_input_tokens += call_total
    if total_input_tokens <= 0:
        return {}
    return {
        "llm_cache_hit_tokens": hit_tokens,
        "llm_cache_total_input_tokens": total_input_tokens,
        "llm_cache_hit_rate": hit_tokens / total_input_tokens,
    }


def _build_timeline(records: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    rows = []
    span_events = []
    for record in records:
        if record.get("kind") == "metric":
            continue
        projected = dict(record)
        if projected.get("kind") == "span":
            span_events.extend(projected.pop("events", ()) or ())
        rows.append(projected)
    seen = {_timeline_identity(record) for record in rows}
    for event in span_events:
        if not isinstance(event, dict):
            continue
        identity = _timeline_identity(event)
        if identity in seen:
            continue
        seen.add(identity)
        rows.append(dict(event))
    return sorted(rows, key=_record_sort_key)


def _timeline_identity(record: dict[str, Any]) -> tuple[str, int, str]:
    return (
        str(record.get("event") or ""),
        _as_int(record.get("ts_ms")),
        str(record.get("span_id") or ""),
    )


def _rank_tools(records: Iterable[dict[str, Any]], run_ids: set[str]) -> list[dict[str, Any]]:
    record_list = list(records)
    ranks: dict[str, dict[str, Any]] = {}
    metric_pairs = _tool_metric_pairs(record_list, run_ids)
    for (_run_id, tool_name), metric_row in metric_pairs.items():
        _merge_tool_rank(
            ranks,
            tool_name=tool_name,
            count=_as_int(metric_row.get("count")),
            total_ms=_as_int(metric_row.get("total_ms")),
            max_ms=_as_int(metric_row.get("max_ms")),
        )

    for record in record_list:
        if record.get("kind") != "tool_call" or str(record.get("run_id") or "") not in run_ids:
            continue
        if not str(record.get("event") or "").endswith("finished"):
            continue
        tool_name = str(_labels(record).get("tool_name") or "unknown")
        if (str(record.get("run_id") or ""), tool_name) in metric_pairs:
            continue
        duration = _metric(record, "elapsed_ms") or _metric(record, "duration_ms")
        _merge_tool_rank(
            ranks,
            tool_name=tool_name,
            count=1,
            total_ms=duration,
            max_ms=duration,
        )
    for row in ranks.values():
        row["avg_ms"] = round(row["total_ms"] / row["count"]) if row["count"] else 0
    return sorted(ranks.values(), key=lambda item: (item["total_ms"], item["max_ms"]), reverse=True)


def _tool_metric_pairs(
    records: Iterable[dict[str, Any]],
    run_ids: set[str],
) -> dict[tuple[str, str], dict[str, int]]:
    pairs: dict[tuple[str, str], dict[str, int]] = {}
    for record in records:
        if record.get("kind") != "metric":
            continue
        run_id = str(record.get("run_id") or "")
        if run_id not in run_ids:
            continue
        name = str(record.get("name") or "")
        if name not in {"agent.tool.call.count", "agent.tool.duration"}:
            continue
        tool_name = str(_otel_label(record, "agent.tool.name") or "unknown")
        row = pairs.setdefault(
            (run_id, tool_name),
            {"count": 0, "duration_count": 0, "total_ms": 0, "max_ms": 0},
        )
        if name == "agent.tool.call.count":
            row["count"] += _as_int(_metric_point_total(record))
        else:
            row["total_ms"] += _as_int(_histogram_sum(record))
            row["max_ms"] = max(row["max_ms"], _as_int(_histogram_max(record)))
            row["duration_count"] += _as_int(_histogram_count(record))
    resolved = {}
    for key, row in pairs.items():
        count = row["count"] or row["duration_count"]
        if count > 0:
            resolved[key] = {**row, "count": count}
    return resolved


def _merge_tool_rank(
    ranks: dict[str, dict[str, Any]],
    *,
    tool_name: str,
    count: int,
    total_ms: int,
    max_ms: int,
) -> None:
    row = ranks.setdefault(
        tool_name,
        {"tool_name": tool_name, "count": 0, "total_ms": 0, "max_ms": 0},
    )
    row["count"] += max(0, int(count))
    row["total_ms"] += max(0, int(total_ms))
    row["max_ms"] = max(row["max_ms"], max(0, int(max_ms)))


def _rank_runtime_phases(records: Iterable[dict[str, Any]], run_ids: set[str]) -> list[dict[str, Any]]:
    ranks: dict[tuple[str, str], dict[str, Any]] = {}
    for record in records:
        if record.get("kind") != "runtime_phase" or str(record.get("run_id") or "") not in run_ids:
            continue
        if str(record.get("event") or "") == "runtime_phase.started":
            continue
        labels = _labels(record)
        phase_name = str(labels.get("phase_name") or "unknown")
        component = str(labels.get("component") or "runtime")
        duration = _metric(record, "duration_ms")
        row = ranks.setdefault(
            (component, phase_name),
            {
                "component": component,
                "phase_name": phase_name,
                "count": 0,
                "total_ms": 0,
                "max_ms": 0,
            },
        )
        row["count"] += 1
        row["total_ms"] += duration
        row["max_ms"] = max(row["max_ms"], duration)
    for row in ranks.values():
        row["avg_ms"] = round(row["total_ms"] / row["count"]) if row["count"] else 0
    return sorted(ranks.values(), key=lambda item: (item["total_ms"], item["max_ms"]), reverse=True)


def _build_hourly_trends(summaries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    buckets: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for row in summaries:
        bucket_ms = floor(_as_int(row.get("ts_ms")) / 3600000) * 3600000
        buckets[bucket_ms].append(row)
    trends = []
    for bucket_ms, rows in sorted(buckets.items()):
        statuses = [str(row.get("status") or "").lower() for row in rows]
        trends.append(
            {
                "bucket_start_ms": bucket_ms,
                "run_count": len(rows),
                "success_count": sum(status in _SUCCESS_STATUSES for status in statuses),
                "p90_total_duration_ms": _percentiles([row["total_duration_ms"] for row in rows])["p90"],
            }
        )
    return trends


def _duration_buckets(values: Iterable[int]) -> list[dict[str, Any]]:
    rows = [{"label": label, "count": 0, "upper_bound_ms": limit} for limit, label in _DISTRIBUTION_LIMITS]
    rows.append({"label": ">=60s", "count": 0, "upper_bound_ms": None})
    for value in values:
        for row in rows:
            limit = row["upper_bound_ms"]
            if limit is None or value < limit:
                row["count"] += 1
                break
    return rows


def _percentiles(values: Iterable[int]) -> dict[str, int]:
    ordered = sorted(int(value) for value in values if int(value) >= 0)
    return {
        "p50": _percentile(ordered, 0.50),
        "p90": _percentile(ordered, 0.90),
        "p99": _percentile(ordered, 0.99),
    }


def _percentile(ordered: list[int], fraction: float) -> int:
    if not ordered:
        return 0
    if len(ordered) == 1:
        return ordered[0]
    position = (len(ordered) - 1) * fraction
    low = floor(position)
    high = min(low + 1, len(ordered) - 1)
    weight = position - low
    return round(ordered[low] * (1 - weight) + ordered[high] * weight)


def _latest_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    summaries = [record for record in records if record.get("kind") == "run_summary"]
    return max(summaries, key=_record_sort_key, default={})


def _run_overview_record(records: list[dict[str, Any]]) -> dict[str, Any]:
    run_records = [record for record in records if record.get("kind") == "run"]
    started = [record for record in run_records if record.get("event") == "run.started"]
    if started:
        return min(started, key=_record_sort_key)
    return max(run_records, key=_record_sort_key, default={})


def _record_sort_key(record: dict[str, Any]) -> tuple[int, str]:
    return _as_int(record.get("ts_ms")), str(record.get("event") or "")


def _metrics(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("metrics") if isinstance(record.get("metrics"), dict) else {}


def _labels(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("labels") if isinstance(record.get("labels"), dict) else {}


def _otel_label(record: dict[str, Any], key: str) -> Any:
    return _labels(record).get(key)


def _metric_point_total(record: dict[str, Any]) -> Optional[int]:
    value = record.get("value")
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, int(value))
    return None


def _histogram_value(record: dict[str, Any], key: str) -> Optional[int]:
    value = record.get("value")
    if not isinstance(value, dict):
        return None
    return _as_optional_int(value.get(key))


def _histogram_count(record: dict[str, Any]) -> Optional[int]:
    return _histogram_value(record, "count")


def _histogram_sum(record: dict[str, Any]) -> Optional[int]:
    return _histogram_value(record, "sum")


def _histogram_max(record: dict[str, Any]) -> Optional[int]:
    return _histogram_value(record, "max")


def _histogram_average(record: dict[str, Any]) -> Optional[int]:
    count = _histogram_count(record)
    total = _histogram_sum(record)
    if count is None or count <= 0 or total is None:
        return None
    return round(total / count)


def _metric(record: dict[str, Any], key: str) -> int:
    return _as_int(_metrics(record).get(key))


def _metric_optional_int(record: dict[str, Any], key: str) -> Optional[int]:
    return _as_optional_int(_metrics(record).get(key))


def _metric_float(record: dict[str, Any], key: str) -> Optional[float]:
    return _as_optional_float(_metrics(record).get(key))


def _summary_llm_wall_ms(record: dict[str, Any]) -> int:
    return _metric(record, "llm_wall_total_ms") or _metric(record, "llm_stream_total_ms")


def _llm_call_wall_ms(record: dict[str, Any]) -> int:
    return (
        _metric(record, "wall_duration_ms") or _metric(record, "duration_ms") or _metric(record, "stream_duration_ms")
    )


def _tool_call_ms(record: dict[str, Any]) -> int:
    return _metric(record, "elapsed_ms") or _metric(record, "duration_ms")


def _as_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _as_optional_int(value: Any) -> Optional[int]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_optional_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _sort_optional_int(value: Any) -> int:
    parsed = _as_optional_int(value)
    return parsed if parsed is not None else -1


def _first_present(*records: dict[str, Any], key: str) -> Any:
    for record in records:
        value = record.get(key)
        if value is not None and value != "":
            return value
    return None


def _keep_max(target: dict[str, int], key: str, value: Any) -> None:
    parsed = _as_optional_int(value)
    if parsed is None:
        return
    target[key] = max(target.get(key, 0), parsed)


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0
