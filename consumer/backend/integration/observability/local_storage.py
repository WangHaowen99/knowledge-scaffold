"""Append-only local OTel partitions with rebuildable sidecar indexes."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import date, datetime, timedelta
import json
import os
from pathlib import Path
import re
import threading
from typing import Any, Iterable, Iterator, Optional

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows fallback
    fcntl = None

from utils.logging import DEFAULT_MAX_BYTES

_FILE_LOCK = threading.RLock()
_PARTITION_PATTERN = re.compile(r"^otel_trace-(\d{4}-\d{2}-\d{2})\.log(?:\.(\d+))?$")


class IndexedLocalOTelStore:
    def __init__(
        self,
        log_dir: str | os.PathLike[str],
        *,
        retention_days: int,
        max_bytes: int = DEFAULT_MAX_BYTES,
    ) -> None:
        self.log_dir = Path(log_dir)
        self.log_dir.mkdir(parents=True, exist_ok=True)
        self.retention_days = max(1, int(retention_days))
        self.max_bytes = max(1024, int(max_bytes))
        self.lock_path = self.log_dir / ".otel_trace.lock"
        self._last_pruned_day = ""

    def append(self, record: dict[str, Any]) -> None:
        self.append_many((record,))

    def append_many(self, records: Iterable[dict[str, Any]]) -> None:
        prepared = [
            (
                record,
                (json.dumps(record, ensure_ascii=True, default=str) + "\n").encode(
                    "utf-8"
                ),
                _record_day(record),
            )
            for record in records
        ]
        if not prepared:
            return
        with local_file_lock(self.lock_path):
            for record, payload, day in prepared:
                self._append_unlocked(record, payload=payload, day=day)

    def _append_unlocked(
        self,
        record: dict[str, Any],
        *,
        payload: bytes,
        day: str,
    ) -> None:
        self._prune(day)
        data_path = self._writable_partition(day, len(payload))
        with data_path.open("ab") as stream:
            offset = stream.tell()
            stream.write(payload)
            data_size = stream.tell()
        _append_index_entry(
            data_path,
            _index_entry(
                record,
                offset=offset,
                length=len(payload),
                data_size=data_size,
            ),
        )

    def _writable_partition(self, day: str, payload_size: int) -> Path:
        base = self.log_dir / "otel_trace-{}.log".format(day)
        partitions = [
            path
            for path in self.log_dir.glob("{}*".format(base.name))
            if _partition_number(path, base.name) is not None
        ]
        if not partitions:
            return base
        current = max(partitions, key=lambda path: _partition_number(path, base.name) or 0)
        _repair_trailing_index(current)
        try:
            current_size = current.stat().st_size
        except OSError:
            current_size = 0
        if current_size == 0 or current_size + payload_size <= self.max_bytes:
            return current
        next_number = (_partition_number(current, base.name) or 0) + 1
        return Path("{}.{}".format(base, next_number))

    def _prune(self, current_day: str) -> None:
        if self._last_pruned_day == current_day:
            return
        try:
            cutoff = datetime.strptime(current_day, "%Y-%m-%d").date() - timedelta(
                days=self.retention_days - 1
            )
        except ValueError:
            return
        for path in self.log_dir.iterdir():
            match = _PARTITION_PATTERN.match(path.name)
            if not match:
                continue
            try:
                partition_day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
            except ValueError:
                continue
            if partition_day >= cutoff:
                continue
            _unlink(path)
            _unlink(index_path(path))
        self._last_pruned_day = current_day


def read_or_rebuild_index(data_path: Path) -> list[dict[str, Any]]:
    lock_path = data_path.parent / ".otel_trace.lock"
    with local_file_lock(lock_path):
        try:
            source_size = data_path.stat().st_size
        except OSError:
            return []
        entries = _load_index(data_path, source_size=source_size)
        if entries is not None:
            return entries
        return _rebuild_index(data_path, source_size=source_size)


def index_path(data_path: Path) -> Path:
    return Path("{}.idx".format(data_path))


@contextmanager
def local_file_lock(lock_path: Path) -> Iterator[None]:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _FILE_LOCK:
        with lock_path.open("a+", encoding="utf-8") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _record_day(record: dict[str, Any]) -> str:
    try:
        timestamp_ms = int(record.get("ts_ms") or 0)
    except (TypeError, ValueError):
        timestamp_ms = 0
    if timestamp_ms > 0:
        return datetime.fromtimestamp(timestamp_ms / 1000).date().isoformat()
    return date.today().isoformat()


def _partition_number(path: Path, base_name: str) -> Optional[int]:
    if path.name == base_name:
        return 0
    prefix = base_name + "."
    suffix = path.name[len(prefix):] if path.name.startswith(prefix) else ""
    return int(suffix) if suffix.isdigit() else None


def _index_entry(
    record: Any,
    *,
    offset: int,
    length: int,
    data_size: int,
) -> dict[str, Any]:
    is_mapping = isinstance(record, dict)
    entry = {
        "offset": int(offset),
        "length": int(length),
        "data_size": int(data_size),
        "valid": is_mapping,
        "supported": bool(
            is_mapping
            and record.get("schema_version") == 2
            and record.get("record_type") == "opentelemetry"
        ),
    }
    if is_mapping:
        for name in (
            "ts_ms",
            "run_id",
            "session_id",
            "trace_id",
            "span_id",
            "status",
            "kind",
        ):
            value = record.get(name)
            if value is not None:
                entry[name] = value
    return entry


def _append_index_entry(data_path: Path, entry: dict[str, Any]) -> None:
    payload = (json.dumps(entry, ensure_ascii=True, default=str) + "\n").encode("utf-8")
    with index_path(data_path).open("ab") as stream:
        stream.write(payload)


def _load_index(data_path: Path, *, source_size: int) -> Optional[list[dict[str, Any]]]:
    path = index_path(data_path)
    if not path.exists():
        return [] if source_size == 0 else None
    entries: list[dict[str, Any]] = []
    expected_offset = 0
    try:
        with path.open("r", encoding="utf-8") as lines:
            for line in lines:
                entry = json.loads(line)
                if not isinstance(entry, dict):
                    return None
                offset = int(entry.get("offset"))
                length = int(entry.get("length"))
                data_size = int(entry.get("data_size"))
                if (
                    offset != expected_offset
                    or length <= 0
                    or data_size != offset + length
                ):
                    return None
                entries.append(entry)
                expected_offset = data_size
    except (OSError, TypeError, ValueError):
        return None
    if source_size == 0:
        return [] if not entries else None
    if not entries or int(entries[-1].get("data_size") or -1) != source_size:
        return None
    return entries


def _repair_trailing_index(data_path: Path) -> None:
    try:
        source_size = data_path.stat().st_size
    except OSError:
        return
    if _last_indexed_data_size(data_path) == source_size:
        return
    _rebuild_index(data_path, source_size=source_size)


def _last_indexed_data_size(data_path: Path) -> Optional[int]:
    path = index_path(data_path)
    if not path.exists():
        return 0 if not data_path.exists() or data_path.stat().st_size == 0 else None
    try:
        with path.open("rb") as stream:
            stream.seek(0, os.SEEK_END)
            position = stream.tell()
            if position == 0:
                return 0
            buffer = b""
            while position > 0:
                chunk_size = min(4096, position)
                position -= chunk_size
                stream.seek(position)
                buffer = stream.read(chunk_size) + buffer
                lines = [line for line in buffer.splitlines() if line.strip()]
                if lines and (position == 0 or len(lines) > 1):
                    entry = json.loads(lines[-1].decode("utf-8"))
                    if not isinstance(entry, dict):
                        return None
                    return int(entry.get("data_size"))
    except (OSError, TypeError, ValueError):
        return None
    return None


def _rebuild_index(data_path: Path, *, source_size: int) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    try:
        with data_path.open("rb") as lines:
            while True:
                offset = lines.tell()
                raw = lines.readline()
                if not raw:
                    break
                try:
                    record = json.loads(raw.decode("utf-8").strip())
                except (UnicodeDecodeError, json.JSONDecodeError):
                    record = None
                entries.append(
                    _index_entry(
                        record,
                        offset=offset,
                        length=len(raw),
                        data_size=lines.tell(),
                    )
                )
    except OSError:
        return []

    target = index_path(data_path)
    temporary = Path("{}.tmp.{}.{}".format(target, os.getpid(), threading.get_ident()))
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            for entry in entries:
                stream.write(json.dumps(entry, ensure_ascii=True, default=str) + "\n")
        os.replace(temporary, target)
    except OSError:
        _unlink(temporary)
    if entries and int(entries[-1].get("data_size") or -1) != source_size:
        return []
    return entries


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        return
    except OSError:
        return
