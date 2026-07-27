"""Shared loop timing helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Dict, List, Optional


def build_public_iteration_timing(
    *,
    total_elapsed_ms: int,
    model_elapsed_ms: Optional[int],
    tools_elapsed_ms: int,
) -> Dict[str, int]:
    """Build additive public phase timings when direct model timing is available."""
    if model_elapsed_ms is None:
        return {}
    total_ms = max(0, int(total_elapsed_ms or 0))
    model_ms = max(0, int(model_elapsed_ms or 0))
    tools_ms = max(0, int(tools_elapsed_ms or 0))
    return {
        "model_elapsed_ms": model_ms,
        "setup_elapsed_ms": max(0, total_ms - model_ms - tools_ms),
    }


def build_tool_timing_metrics(
    tool_intervals_ms: List[Mapping[str, int]],
    *,
    tools_window_elapsed_ms: int,
    llm_started_at_ms: Optional[int] = None,
    llm_finished_at_ms: Optional[int] = None,
) -> Dict[str, int]:
    intervals = normalize_intervals(tool_intervals_ms)
    merged_tool_intervals = merge_intervals(intervals)
    tools_wall_elapsed_ms = sum_interval_lengths(merged_tool_intervals)
    tools_execution_elapsed_ms = sum(
        max(0, int(interval.get("elapsed_ms", interval["finished_at_ms"] - interval["started_at_ms"])))
        for interval in intervals
    )
    if llm_started_at_ms is None or llm_finished_at_ms is None:
        tools_overlap_llm_ms = 0
    else:
        tools_overlap_llm_ms = intervals_overlap_ms(
            merged_tool_intervals,
            max(0, int(llm_started_at_ms)),
            max(0, int(llm_finished_at_ms)),
        )
    tools_net_wait_ms = max(0, tools_wall_elapsed_ms - tools_overlap_llm_ms)

    return {
        "tools_window_elapsed_ms": max(0, int(tools_window_elapsed_ms or 0)),
        "tools_wall_elapsed_ms": tools_wall_elapsed_ms,
        "tools_execution_elapsed_ms": tools_execution_elapsed_ms,
        "tools_overlap_llm_ms": tools_overlap_llm_ms,
        "tools_net_wait_ms": tools_net_wait_ms,
    }


def tool_window_elapsed_ms(tool_intervals_ms: List[Mapping[str, int]]) -> int:
    intervals = normalize_intervals(tool_intervals_ms)
    if not intervals:
        return 0
    return max(0, max(item["finished_at_ms"] for item in intervals) - min(item["started_at_ms"] for item in intervals))


def normalize_intervals(intervals: List[Mapping[str, int]]) -> List[Dict[str, int]]:
    normalized: List[Dict[str, int]] = []
    for interval in intervals or []:
        try:
            started_at_ms = int(interval.get("started_at_ms"))
            finished_at_ms = int(interval.get("finished_at_ms"))
        except (TypeError, ValueError):
            continue
        if finished_at_ms < started_at_ms:
            started_at_ms, finished_at_ms = finished_at_ms, started_at_ms
        try:
            elapsed_ms = int(interval.get("elapsed_ms"))
        except (TypeError, ValueError):
            elapsed_ms = finished_at_ms - started_at_ms
        normalized.append(
            {
                "started_at_ms": max(0, started_at_ms),
                "finished_at_ms": max(0, finished_at_ms),
                "elapsed_ms": max(0, elapsed_ms),
            }
        )
    return normalized


def merge_intervals(intervals: List[Mapping[str, int]]) -> List[Dict[str, int]]:
    if not intervals:
        return []
    sorted_intervals = sorted(intervals, key=lambda item: (item["started_at_ms"], item["finished_at_ms"]))
    merged: List[Dict[str, int]] = []
    for interval in sorted_intervals:
        if not merged or interval["started_at_ms"] > merged[-1]["finished_at_ms"]:
            merged.append(
                {
                    "started_at_ms": int(interval["started_at_ms"]),
                    "finished_at_ms": int(interval["finished_at_ms"]),
                }
            )
            continue
        merged[-1]["finished_at_ms"] = max(merged[-1]["finished_at_ms"], int(interval["finished_at_ms"]))
    return merged


def sum_interval_lengths(intervals: List[Mapping[str, int]]) -> int:
    return sum(max(0, int(interval["finished_at_ms"]) - int(interval["started_at_ms"])) for interval in intervals)


def intervals_overlap_ms(
    intervals: List[Mapping[str, int]],
    started_at_ms: int,
    finished_at_ms: int,
) -> int:
    if finished_at_ms < started_at_ms:
        started_at_ms, finished_at_ms = finished_at_ms, started_at_ms
    total = 0
    for interval in intervals:
        overlap_start = max(started_at_ms, int(interval["started_at_ms"]))
        overlap_finish = min(finished_at_ms, int(interval["finished_at_ms"]))
        total += max(0, overlap_finish - overlap_start)
    return total
