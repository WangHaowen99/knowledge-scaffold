"""Application services for local observability performance analysis."""

from __future__ import annotations

from datetime import datetime, timezone
import time
from typing import Any, Mapping, Optional

from utils.perf_analysis.aggregate import (
    build_global_overview,
    build_run_detail,
    build_session_analysis,
    filter_summary_rows,
    summary_rows,
)
from utils.perf_analysis.reader import LocalPerfLogReader, MAX_SCAN_DAYS

DEFAULT_WINDOW_MS = 24 * 60 * 60 * 1000
MAX_WINDOW_MS = MAX_SCAN_DAYS * DEFAULT_WINDOW_MS
DEFAULT_SESSION_RUN_LIMIT = 50
MAX_RUN_LIST_LIMIT = 200


def analyze_session(
    session_id: str,
    *,
    start_ms: int,
    end_ms: int,
    limit_runs: int = DEFAULT_SESSION_RUN_LIMIT,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    read_result = _reader(workspace_id=workspace_id).read_records(
        start_ms=start_ms,
        end_ms=end_ms,
        session_id=session_id,
    )
    body = build_session_analysis(read_result.records, session_id=session_id)
    body["window"] = {"from_ms": start_ms, "to_ms": end_ms}
    run_count = len(body.get("runs") or [])
    bounded_limit = parse_limit(limit_runs, default=DEFAULT_SESSION_RUN_LIMIT, maximum=MAX_RUN_LIST_LIMIT)
    body["runs"] = list(body.get("runs") or [])[:bounded_limit]
    body["diagnostics"] = {
        **read_result.diagnostics,
        "truncated": bool(read_result.diagnostics.get("truncated")) or run_count > bounded_limit,
    }
    return body


def check_session_availability(
    session_id: str,
    *,
    start_ms: int,
    end_ms: int,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    read_result = _reader(workspace_id=workspace_id).has_record(
        start_ms=start_ms,
        end_ms=end_ms,
        session_id=session_id,
        kinds={"run_summary"},
    )
    return {
        "session_id": session_id,
        "available": bool(read_result.records),
        "window": {"from_ms": start_ms, "to_ms": end_ms},
        "diagnostics": read_result.diagnostics,
    }


def analyze_run_detail(
    run_id: str,
    *,
    start_ms: int,
    end_ms: int,
    workspace_id: str | None = None,
) -> dict[str, Any]:
    read_result = _reader(workspace_id=workspace_id).read_records(
        start_ms=start_ms,
        end_ms=end_ms,
        run_id=run_id,
    )
    body = build_run_detail(read_result.records, run_id=run_id)
    body["window"] = {"from_ms": start_ms, "to_ms": end_ms}
    body["diagnostics"] = read_result.diagnostics
    return body


def analyze_global_overview(
    *,
    start_ms: int,
    end_ms: int,
    workspace_id: Optional[str] = None,
    status: Optional[str] = None,
    model_provider: Optional[str] = None,
    agent_mode: Optional[str] = None,
) -> dict[str, Any]:
    read_result = _reader(workspace_id=workspace_id).read_records(
        start_ms=start_ms,
        end_ms=end_ms,
        kinds={"run_summary"},
    )
    body = build_global_overview(
        read_result.records,
        workspace_id=workspace_id,
        status=status,
        model_provider=model_provider,
        agent_mode=agent_mode,
    )
    body["window"] = {"from_ms": start_ms, "to_ms": end_ms}
    body["diagnostics"] = read_result.diagnostics
    return body


def analyze_global_runs(
    *,
    start_ms: int,
    end_ms: int,
    workspace_id: Optional[str] = None,
    status: Optional[str] = None,
    model_provider: Optional[str] = None,
    agent_mode: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> dict[str, Any]:
    read_result = _reader(workspace_id=workspace_id).read_records(start_ms=start_ms, end_ms=end_ms)
    runs = filter_summary_rows(
        summary_rows(read_result.records),
        workspace_id=workspace_id,
        status=status,
        model_provider=model_provider,
        agent_mode=agent_mode,
    )
    runs.sort(key=lambda row: (int(row.get("ts_ms") or 0), row.get("run_id") or ""), reverse=True)
    bounded_limit = parse_limit(limit, default=50, maximum=MAX_RUN_LIST_LIMIT)
    bounded_offset = max(_parse_int(offset) or 0, 0)
    page = runs[bounded_offset: bounded_offset + bounded_limit]
    return {
        "window": {"from_ms": start_ms, "to_ms": end_ms},
        "filters": {
            "workspace_id": workspace_id,
            "status": status,
            "model_provider": model_provider,
            "agent_mode": agent_mode,
        },
        "count": len(runs),
        "limit": bounded_limit,
        "offset": bounded_offset,
        "runs": page,
        "diagnostics": {
            **read_result.diagnostics,
            "truncated": bounded_offset + len(page) < len(runs),
        },
    }


def parse_query_window(
    args: Mapping[str, Any],
    *,
    default_window_ms: int = DEFAULT_WINDOW_MS,
) -> tuple[int, int]:
    end_ms = parse_epoch_ms(args.get("to")) or current_epoch_ms()
    start_ms = parse_epoch_ms(args.get("from")) or end_ms - default_window_ms
    return clamp_scan_window(start_ms, end_ms)


def clamp_scan_window(start_ms: int, end_ms: int) -> tuple[int, int]:
    normalized_start, normalized_end = sorted((_parse_int(start_ms) or 0, _parse_int(end_ms) or 0))
    if normalized_end <= 0:
        normalized_end = current_epoch_ms()
    if normalized_start <= 0:
        normalized_start = normalized_end - DEFAULT_WINDOW_MS
    if normalized_end - normalized_start > MAX_WINDOW_MS:
        normalized_start = normalized_end - MAX_WINDOW_MS
    return normalized_start, normalized_end


def parse_epoch_ms(value: Any) -> Optional[int]:
    parsed = _parse_int(value)
    if parsed is not None and parsed > 0:
        return parsed
    text = str(value or "").strip()
    if not text:
        return None
    try:
        normalized = text.replace("Z", "+00:00")
        resolved = datetime.fromisoformat(normalized)
        if resolved.tzinfo is None:
            resolved = resolved.replace(tzinfo=timezone.utc)
        return int(resolved.timestamp() * 1000)
    except ValueError:
        return None


def parse_limit(value: Any, *, default: int, maximum: int) -> int:
    parsed = _parse_int(value)
    if parsed is None:
        return default
    return min(max(parsed, 1), maximum)


def current_epoch_ms() -> int:
    return int(time.time() * 1000)


def _parse_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _reader(*, workspace_id: str | None = None) -> LocalPerfLogReader:
    return LocalPerfLogReader(workspace_id=workspace_id)
