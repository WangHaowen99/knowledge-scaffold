"""Bounded reader for date-partitioned local observability perf logs."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
import json
from pathlib import Path
from typing import Any, Iterable, Optional

from ..utils.logging import LOG_DIR

SUPPORTED_SCHEMA_VERSION = 1
MAX_SCAN_DAYS = 7
_PERF_FILE_STEM = "agent_perf"


@dataclass
class PerfReadResult:
    records: list[dict[str, Any]]
    diagnostics: dict[str, Any]


class LocalPerfLogReader:
    """Read the current local perf schema without exposing files to callers."""

    def __init__(self, *, log_dir: str | Path | None = None) -> None:
        self.log_dir = Path(log_dir or LOG_DIR)

    def read_records(
        self,
        *,
        start_ms: int,
        end_ms: int,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
    ) -> PerfReadResult:
        return self._read_matching_records(
            start_ms=start_ms,
            end_ms=end_ms,
            run_id=run_id,
            session_id=session_id,
            kinds=kinds,
            stop_after_first=False,
        )

    def has_record(
        self,
        *,
        start_ms: int,
        end_ms: int,
        run_id: Optional[str] = None,
        session_id: Optional[str] = None,
        kinds: Optional[Iterable[str]] = None,
    ) -> PerfReadResult:
        return self._read_matching_records(
            start_ms=start_ms,
            end_ms=end_ms,
            run_id=run_id,
            session_id=session_id,
            kinds=kinds,
            stop_after_first=True,
        )

    def _read_matching_records(
        self,
        *,
        start_ms: int,
        end_ms: int,
        run_id: Optional[str],
        session_id: Optional[str],
        kinds: Optional[Iterable[str]],
        stop_after_first: bool,
    ) -> PerfReadResult:
        records: list[dict[str, Any]] = []
        kinds_filter = {str(kind) for kind in kinds or () if str(kind)}
        diagnostics = {
            "scanned_files": 0,
            "scanned_file_names": [],
            "parse_error_count": 0,
            "unknown_schema_count": 0,
            "skipped_record_count": 0,
            "truncated": False,
        }

        normalized_start, normalized_end = sorted((_coerce_ms(start_ms), _coerce_ms(end_ms)))
        for path in self._candidate_files(normalized_start, normalized_end):
            diagnostics["scanned_files"] += 1
            diagnostics["scanned_file_names"].append(path.name)
            try:
                lines = path.open("r", encoding="utf-8")
            except OSError:
                diagnostics["skipped_record_count"] += 1
                continue

            with lines:
                for line in lines:
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        record = json.loads(raw)
                    except json.JSONDecodeError:
                        diagnostics["parse_error_count"] += 1
                        continue
                    if not isinstance(record, dict):
                        diagnostics["skipped_record_count"] += 1
                        continue
                    if record.get("schema_version") != SUPPORTED_SCHEMA_VERSION:
                        diagnostics["unknown_schema_count"] += 1
                        continue
                    timestamp_ms = _read_int(record.get("ts_ms"))
                    if timestamp_ms is None or timestamp_ms < normalized_start or timestamp_ms > normalized_end:
                        diagnostics["skipped_record_count"] += 1
                        continue
                    if run_id and str(record.get("run_id") or "") != run_id:
                        continue
                    if session_id and str(record.get("session_id") or "") != session_id:
                        continue
                    if kinds_filter and str(record.get("kind") or "") not in kinds_filter:
                        continue
                    records.append(record)
                    if stop_after_first:
                        return PerfReadResult(records=records, diagnostics=diagnostics)

        return PerfReadResult(records=records, diagnostics=diagnostics)

    def _candidate_files(self, start_ms: int, end_ms: int) -> list[Path]:
        if not self.log_dir.exists():
            return []

        paths: list[Path] = []
        for day in _iter_local_dates(start_ms, end_ms):
            base_name = "{}-{}.log".format(_PERF_FILE_STEM, day.isoformat())
            for path in sorted(self.log_dir.glob("{}*".format(base_name))):
                if path.is_file() and _is_log_partition(path.name, base_name):
                    paths.append(path)
        return paths


def _coerce_ms(value: Any) -> int:
    parsed = _read_int(value)
    return parsed if parsed is not None else 0


def _read_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _iter_local_dates(start_ms: int, end_ms: int) -> Iterable[date]:
    start_date = datetime.fromtimestamp(start_ms / 1000).date()
    end_date = datetime.fromtimestamp(end_ms / 1000).date()
    current = start_date
    while current <= end_date:
        yield current
        current += timedelta(days=1)


def _is_log_partition(name: str, base_name: str) -> bool:
    if name == base_name:
        return True
    suffix = name[len(base_name):]
    return suffix.startswith(".") and suffix[1:].isdigit()
