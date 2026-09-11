"""Rebuildable, revisioned session summaries for bounded list operations."""

from __future__ import annotations

import base64
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, replace
import json
import os
from pathlib import Path
import stat
import threading
import time
from types import MappingProxyType
from typing import Any
import uuid


SCHEMA_VERSION = 4
_CURSOR_VERSION = 1
_MAX_CURSOR_LENGTH = 2048
_MAX_REBUILD_ATTEMPTS = 3
_DELTA_SCHEMA_VERSION = 1
_DELTA_COMPACT_THRESHOLD = 128


class SessionSummaryIndexError(RuntimeError):
    """Base failure for the rebuildable summary projection."""


class SessionSummaryIndexWriteError(SessionSummaryIndexError):
    def __init__(self, message: str, *, committed: bool) -> None:
        super().__init__(message)
        self.committed = bool(committed)


class SessionSummaryIndexCorruptionError(SessionSummaryIndexError):
    """The index cannot be trusted and could not be rebuilt."""


class SessionSummaryIndexDirtyError(SessionSummaryIndexError):
    """A source mutation is still outstanding, so the projection is unreadable."""


class SessionSummaryIndexRebuildCancelled(SessionSummaryIndexError):
    """A background rebuild was cancelled before publishing derived state."""


class InvalidSessionCursor(ValueError):
    """The pagination cursor is malformed or no longer matches the index."""


def _validate_optional_non_negative_integer(value: Any, field_name: str) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{field_name} must be a non-negative integer")
    if value < 0:
        raise ValueError(f"{field_name} must be a non-negative integer")


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        if any(not isinstance(key, str) for key in value):
            raise TypeError("session summary metadata keys must be strings")
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    raise TypeError("session summary metadata must contain JSON-compatible values")


def _copy(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _copy(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_copy(item) for item in value]
    return value


def _text(value: Any, field_name: str, *, allow_empty: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{field_name} must be a string")
    if not allow_empty and not value:
        raise ValueError(f"{field_name} is required")
    return value


@dataclass(frozen=True, slots=True)
class SessionSummary:
    session_id: str
    title: str | None
    created_at: str
    updated_at: str
    active_run_id: str | None
    message_count: int
    turn_count: int
    pinned: bool
    metadata: Mapping[str, Any]
    last_run_status: str
    last_event_sequence: int
    pending_interaction: Mapping[str, Any] | None

    @classmethod
    def create(
        cls,
        *,
        session_id: str,
        title: str | None = None,
        created_at: str = "",
        updated_at: str = "",
        active_run_id: str | None = None,
        message_count: int = 0,
        turn_count: int = 0,
        metadata: Mapping[str, Any] | None = None,
        pinned: bool | None = None,
        last_run_status: str = "idle",
        last_event_sequence: int = 0,
        pending_interaction: Mapping[str, Any] | None = None,
    ) -> "SessionSummary":
        resolved_id = _text(session_id, "session_id", allow_empty=False)
        if len(resolved_id) > 256 or any(character in resolved_id for character in "/\\\x00"):
            raise ValueError("session_id is invalid")
        if title is not None and not isinstance(title, str):
            raise TypeError("title must be a string or null")
        if active_run_id is not None and not isinstance(active_run_id, str):
            raise TypeError("active_run_id must be a string or null")
        if not isinstance(message_count, int) or isinstance(message_count, bool) or message_count < 0:
            raise ValueError("message_count must be a non-negative integer")
        if not isinstance(turn_count, int) or isinstance(turn_count, bool) or turn_count < 0:
            raise ValueError("turn_count must be a non-negative integer")
        resolved_status = _text(last_run_status, "last_run_status", allow_empty=False)
        if (
            not isinstance(last_event_sequence, int)
            or isinstance(last_event_sequence, bool)
            or last_event_sequence < 0
        ):
            raise ValueError("last_event_sequence must be a non-negative integer")
        if pending_interaction is not None and not isinstance(pending_interaction, Mapping):
            raise TypeError("pending_interaction must be an object or null")
        source_metadata = dict(metadata or {})
        resolved_pinned = source_metadata.get("pinned") is True if pinned is None else pinned
        if not isinstance(resolved_pinned, bool):
            raise TypeError("pinned must be a boolean")
        source_metadata["pinned"] = resolved_pinned
        return cls(
            session_id=resolved_id,
            title=title,
            created_at=_text(created_at, "created_at"),
            updated_at=_text(updated_at, "updated_at"),
            active_run_id=active_run_id,
            message_count=message_count,
            turn_count=turn_count,
            pinned=resolved_pinned,
            metadata=_freeze(source_metadata),
            last_run_status=resolved_status,
            last_event_sequence=last_event_sequence,
            pending_interaction=(
                _freeze(dict(pending_interaction))
                if pending_interaction is not None
                else None
            ),
        )

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "SessionSummary":
        if not isinstance(value, Mapping):
            raise TypeError("session summary record must be an object")
        allowed = {
            "id",
            "title",
            "created_at",
            "updated_at",
            "active_run_id",
            "message_count",
            "turn_count",
            "pinned",
            "metadata",
            "last_run_status",
            "last_event_sequence",
            "pending_interaction",
        }
        if set(value) != allowed:
            raise ValueError("session summary record fields are invalid")
        metadata = value.get("metadata")
        if not isinstance(metadata, Mapping):
            raise TypeError("session summary metadata must be an object")
        return cls.create(
            session_id=value["id"],
            title=value["title"],
            created_at=value["created_at"],
            updated_at=value["updated_at"],
            active_run_id=value["active_run_id"],
            message_count=value["message_count"],
            turn_count=value["turn_count"],
            pinned=value["pinned"],
            metadata=metadata,
            last_run_status=value["last_run_status"],
            last_event_sequence=value["last_event_sequence"],
            pending_interaction=value["pending_interaction"],
        )

    @classmethod
    def from_session(cls, session: Any) -> "SessionSummary":
        metadata = dict(getattr(session, "metadata", {}) or {})
        message_count = getattr(session, "message_count", metadata.get("message_count", 0))
        return cls.create(
            session_id=str(getattr(session, "id", "") or ""),
            title=getattr(session, "title", None),
            created_at=str(getattr(session, "created_at", "") or ""),
            updated_at=str(getattr(session, "updated_at", "") or ""),
            active_run_id=getattr(session, "active_run_id", None),
            message_count=int(message_count or 0),
            turn_count=int(getattr(session, "turn_count", 0) or 0),
            metadata=metadata,
            last_run_status=str(getattr(session, "last_run_status", "idle") or "idle"),
            last_event_sequence=int(getattr(session, "last_event_sequence", 0) or 0),
            pending_interaction=getattr(session, "pending_interaction", None),
        )

    @property
    def sort_key(self) -> tuple[int, str, str]:
        return (
            1 if self.pinned else 0,
            self.updated_at or self.created_at,
            self.session_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.session_id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_run_id": self.active_run_id,
            "message_count": self.message_count,
            "turn_count": self.turn_count,
            "pinned": self.pinned,
            "metadata": _copy(self.metadata),
            "last_run_status": self.last_run_status,
            "last_event_sequence": self.last_event_sequence,
            "pending_interaction": _copy(self.pending_interaction),
        }


@dataclass(frozen=True, slots=True)
class SessionPage:
    items: tuple[SessionSummary, ...]
    next_cursor: str | None
    has_more: bool
    index_revision: int


@dataclass(frozen=True, slots=True)
class SessionPruneCandidate:
    session_id: str
    expected_updated_at: str


def session_summary_index_enabled() -> bool:
    value = str(os.getenv("PERF_SESSION_SUMMARY_INDEX_ENABLED", "true") or "").strip().lower()
    return value not in {"0", "false", "no", "off"}


class SessionSummaryIndex:
    def __init__(
        self,
        path: str | Path,
        *,
        rebuild_loader: Callable[[], Iterable[SessionSummary | Mapping[str, Any]]] | None = None,
        rebuild_scheduler: Callable[[], bool] | None = None,
    ) -> None:
        self._path = Path(path)
        self._dirty_path = self._path.with_name(f"{self._path.name}.dirty")
        self._delta_path = self._path.with_name(f"{self._path.name}.delta")
        self._rebuild_loader = rebuild_loader
        self._rebuild_scheduler = rebuild_scheduler
        self._lock = threading.RLock()
        self._rebuild_condition = threading.Condition(self._lock)
        self._rebuild_in_progress = False
        self._rebuild_generation = 0
        self._rebuild_failure: BaseException | None = None
        self._loaded = False
        self._records: dict[str, SessionSummary] = {}
        self._index_revision = 0
        self._state = "cold"
        self._last_error = ""
        self._owned_projection_tokens: set[str] = set()
        self._delta_entries = 0

    @property
    def state(self) -> str:
        with self._lock:
            return self._state

    @property
    def last_error(self) -> str:
        with self._lock:
            return self._last_error

    @property
    def index_revision(self) -> int:
        with self._lock:
            self._ensure_loaded_locked()
            return self._index_revision

    def load_persisted_for_read(self) -> bool:
        """Load only a published index; never scan authoritative session facts."""

        with self._lock:
            if self._rebuild_in_progress:
                return False
            if not self._loaded:
                try:
                    document = self._read_document_locked()
                except FileNotFoundError:
                    self._state = "cold"
                    self._last_error = "index_missing"
                    return False
                except Exception:
                    self._state = "degraded"
                    self._last_error = "index_invalid"
                    return False
                if document["schema_version"] != SCHEMA_VERSION:
                    self._state = "degraded"
                    self._last_error = "schema_mismatch"
                    return False
                self._records = document["records"]
                self._index_revision = document["index_revision"]
                self._loaded = True
                self._state = "ready"
                self._last_error = ""
            if self._state != "ready" or self._dirty_marker_present_locked():
                self._state = "degraded"
                if not self._last_error:
                    self._last_error = "source_projection_pending"
                return False
            return True

    def request_rebuild(self) -> bool:
        """Schedule an authoritative rebuild without running it on the caller."""

        scheduler = self._rebuild_scheduler
        if scheduler is None:
            return False
        try:
            return bool(scheduler())
        except Exception:
            self.mark_degraded("rebuild_schedule_failed")
            return False

    def mark_degraded(self, reason: str = "projection_failed") -> None:
        with self._lock:
            self._state = "degraded"
            self._last_error = str(reason or "projection_failed")[:128]

    def begin_source_projection(self) -> str:
        """Durably mark a source mutation before its authoritative fact commit."""

        token = uuid.uuid4().hex
        with self._lock:
            tokens = set(self._read_dirty_tokens_locked())
            tokens.add(token)
            try:
                _write_atomic_json(self._dirty_path, {"tokens": sorted(tokens)})
            except SessionSummaryIndexWriteError as exc:
                if exc.committed or self._dirty_marker_present_locked():
                    self._state = "degraded"
                    self._last_error = "source_projection_marker_uncertain"
                raise
            self._owned_projection_tokens.add(token)
            self._state = "degraded"
            self._last_error = "source_projection_pending"
        return token

    def abandon_source_projection(self, token: str) -> bool:
        """Release process ownership while preserving the durable dirty marker."""

        resolved = _text(token, "projection token", allow_empty=False)
        with self._lock:
            if resolved not in self._owned_projection_tokens:
                return False
            self._owned_projection_tokens.remove(resolved)
            if self._dirty_marker_present_locked():
                self._state = "degraded"
                self._last_error = "source_projection_abandoned"
            return True

    def complete_source_projection(self, token: str) -> bool:
        """Clear only the marker owned by this completed source mutation."""

        resolved = _text(token, "projection token", allow_empty=False)
        with self._lock:
            try:
                completed = self._complete_dirty_token_locked(resolved)
                if completed and not self._dirty_marker_present_locked():
                    self._state = "ready"
                    self._last_error = ""
                return completed
            finally:
                self._owned_projection_tokens.discard(resolved)

    def rebuild(self, *, cancel_event: threading.Event | None = None) -> int:
        with self._lock:
            self._rebuild_or_fail_locked(
                base_revision=self._index_revision,
                cancel_event=cancel_event,
            )
            self._raise_if_dirty_locked()
            return self._index_revision

    def upsert(
        self,
        summary: SessionSummary | Mapping[str, Any],
        *,
        projection_token: str | None = None,
    ) -> int:
        resolved = summary if isinstance(summary, SessionSummary) else SessionSummary.from_dict(summary)
        with self._lock:
            self._ensure_loaded_locked(
                allow_dirty=True,
                resolving_token=projection_token,
            )
            if self._records.get(resolved.session_id) == resolved:
                return self._index_revision
            next_records = dict(self._records)
            next_records[resolved.session_id] = resolved
            self._publish_locked(next_records, self._index_revision + 1)
            return self._index_revision

    def upsert_chat(
        self,
        summary: SessionSummary | Mapping[str, Any],
        *,
        project_active_run: bool = False,
        projection_token: str | None = None,
    ) -> int:
        """Publish chat-owned fields while preserving execution-owned runtime state."""

        resolved = summary if isinstance(summary, SessionSummary) else SessionSummary.from_dict(summary)
        with self._lock:
            self._ensure_loaded_locked(
                allow_dirty=True,
                resolving_token=projection_token,
            )
            current = self._records.get(resolved.session_id)
            if current is not None:
                resolved = replace(
                    resolved,
                    active_run_id=(
                        resolved.active_run_id
                        if project_active_run
                        else current.active_run_id
                    ),
                    last_run_status=current.last_run_status,
                    last_event_sequence=max(
                        resolved.last_event_sequence,
                        current.last_event_sequence,
                    ),
                    pending_interaction=current.pending_interaction,
                    turn_count=current.turn_count,
                )
            if current == resolved:
                return self._index_revision
            next_records = dict(self._records)
            next_records[resolved.session_id] = resolved
            self._publish_delta_locked(
                operation="upsert",
                session_id=resolved.session_id,
                summary=resolved,
                records=next_records,
                revision=self._index_revision + 1,
            )
            return self._index_revision

    def update_runtime(
        self,
        session_id: str,
        *,
        active_run_id: str | None,
        last_run_status: str,
        pending_interaction: Mapping[str, Any] | None,
        last_event_sequence: int | None = None,
        turn_count: int | None = None,
        projection_token: str | None = None,
    ) -> bool:
        """Patch execution-owned fields without replacing chat-owned metadata."""

        resolved_id = _text(session_id, "session_id", allow_empty=False)
        resolved_status = _text(last_run_status, "last_run_status", allow_empty=False)
        if active_run_id is not None and not isinstance(active_run_id, str):
            raise TypeError("active_run_id must be a string or null")
        if pending_interaction is not None and not isinstance(pending_interaction, Mapping):
            raise TypeError("pending_interaction must be an object or null")
        _validate_optional_non_negative_integer(
            last_event_sequence,
            "last_event_sequence",
        )
        _validate_optional_non_negative_integer(turn_count, "turn_count")

        with self._lock:
            self._ensure_loaded_locked(
                allow_dirty=True,
                resolving_token=projection_token,
            )
            current = self._records.get(resolved_id)
            if current is None:
                return False
            updated = replace(
                current,
                active_run_id=active_run_id,
                last_run_status=resolved_status,
                last_event_sequence=(
                    current.last_event_sequence
                    if last_event_sequence is None
                    else last_event_sequence
                ),
                pending_interaction=(
                    _freeze(dict(pending_interaction))
                    if pending_interaction is not None
                    else None
                ),
                turn_count=current.turn_count if turn_count is None else turn_count,
            )
            if updated == current:
                return True
            next_records = dict(self._records)
            next_records[resolved_id] = updated
            self._publish_delta_locked(
                operation="upsert",
                session_id=resolved_id,
                summary=updated,
                records=next_records,
                revision=self._index_revision + 1,
            )
            return True

    def delete(
        self,
        session_id: str,
        *,
        projection_token: str | None = None,
    ) -> bool:
        resolved_id = _text(session_id, "session_id", allow_empty=False)
        with self._lock:
            self._ensure_loaded_locked(
                allow_dirty=True,
                resolving_token=projection_token,
            )
            if resolved_id not in self._records:
                return False
            next_records = dict(self._records)
            next_records.pop(resolved_id)
            self._publish_delta_locked(
                operation="delete",
                session_id=resolved_id,
                summary=None,
                records=next_records,
                revision=self._index_revision + 1,
            )
            return True

    def list_page(self, *, limit: int, cursor: str | None = None) -> SessionPage:
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        with self._lock:
            self._ensure_loaded_locked()
            ordered = sorted(self._records.values(), key=lambda item: item.sort_key, reverse=True)
            start = 0
            if cursor:
                cursor_revision, cursor_key = _decode_cursor(cursor)
                if cursor_revision != self._index_revision:
                    raise InvalidSessionCursor("session cursor is stale")
                try:
                    start = next(
                        index + 1
                        for index, item in enumerate(ordered)
                        if item.sort_key == cursor_key
                    )
                except StopIteration as exc:
                    raise InvalidSessionCursor("session cursor is invalid") from exc
            selected = tuple(ordered[start : start + limit])
            has_more = start + len(selected) < len(ordered)
            next_cursor = (
                _encode_cursor(self._index_revision, selected[-1].sort_key)
                if has_more and selected
                else None
            )
            return SessionPage(
                items=selected,
                next_cursor=next_cursor,
                has_more=has_more,
                index_revision=self._index_revision,
            )

    def get_summary(self, session_id: str) -> SessionSummary | None:
        """Return one immutable summary without sorting or scanning the index."""

        resolved_id = _text(session_id, "session_id", allow_empty=False)
        with self._lock:
            self._ensure_loaded_locked()
            return self._records.get(resolved_id)

    def list_all(self) -> tuple[SessionSummary, ...]:
        """Return the compatibility full list without replaying source logs."""

        with self._lock:
            self._ensure_loaded_locked()
            return tuple(
                sorted(
                    self._records.values(),
                    key=lambda item: item.sort_key,
                    reverse=True,
                )
            )

    def prune_candidates(
        self,
        *,
        max_sessions: int,
        limit: int = 25,
        excluded_session_ids: Iterable[str] = (),
    ) -> tuple[SessionPruneCandidate, ...]:
        """Select oldest removable records while retaining protected sessions."""

        keep_count = max(0, int(max_sessions))
        batch_limit = max(1, int(limit))
        excluded = {
            _text(session_id, "excluded session id", allow_empty=False)
            for session_id in excluded_session_ids
        }
        if not self.load_persisted_for_read():
            raise SessionSummaryIndexDirtyError(
                "session summary index is unavailable for bounded pruning"
            )
        with self._lock:
            if self._state != "ready" or self._dirty_marker_present_locked():
                self._state = "degraded"
                self._last_error = "source_projection_pending"
                raise SessionSummaryIndexDirtyError(
                    "session summary index is unavailable for bounded pruning"
                )
            overflow = max(0, len(self._records) - keep_count)
            if overflow == 0:
                return ()
            ordered = sorted(
                self._records.values(),
                key=lambda item: item.sort_key,
                reverse=True,
            )
            candidates = []
            for summary in reversed(ordered):
                if (
                    summary.session_id in excluded
                    or summary.pinned
                    or summary.active_run_id
                ):
                    continue
                candidates.append(
                    SessionPruneCandidate(
                        session_id=summary.session_id,
                        expected_updated_at=summary.updated_at,
                    )
                )
                if len(candidates) >= min(overflow, batch_limit):
                    break
            return tuple(candidates)

    def _ensure_loaded_locked(
        self,
        *,
        allow_dirty: bool = False,
        resolving_token: str | None = None,
    ) -> None:
        if not self._loaded:
            try:
                document = self._read_document_locked()
            except FileNotFoundError:
                if self._rebuild_loader is None:
                    self._records = {}
                    self._index_revision = 0
                    self._loaded = True
                    self._state = "ready"
                    self._last_error = ""
                elif allow_dirty:
                    self._state = "degraded"
                    self._last_error = "index_not_ready"
                    raise SessionSummaryIndexError(
                        "session summary index is not ready for projection"
                    )
                else:
                    self._rebuild_or_fail_locked(
                        base_revision=0,
                        wait_for_existing=not allow_dirty,
                    )
            except Exception:
                if allow_dirty:
                    self._state = "degraded"
                    self._last_error = "index_invalid"
                    raise SessionSummaryIndexCorruptionError(
                        "session summary index requires a background rebuild"
                    )
                try:
                    self._quarantine_locked()
                except Exception as quarantine_error:
                    self._state = "degraded"
                    self._last_error = "quarantine_failed"
                    raise SessionSummaryIndexCorruptionError(
                        "unsafe session summary index could not be quarantined"
                    ) from quarantine_error
                self._rebuild_or_fail_locked(
                    base_revision=0,
                    wait_for_existing=not allow_dirty,
                )
            else:
                if document["schema_version"] != SCHEMA_VERSION:
                    if allow_dirty:
                        self._state = "degraded"
                        self._last_error = "schema_mismatch"
                        raise SessionSummaryIndexCorruptionError(
                            "session summary index requires a background rebuild"
                        )
                    self._rebuild_or_fail_locked(base_revision=0)
                else:
                    self._records = document["records"]
                    self._index_revision = document["index_revision"]
                    self._loaded = True
                    self._state = "ready"
                    self._last_error = ""

        if self._dirty_marker_present_locked():
            dirty_tokens = set(self._read_dirty_tokens_or_quarantine_locked())
            resolving_only = bool(
                resolving_token
                and dirty_tokens
                and dirty_tokens == {resolving_token}
            )
            if not resolving_only:
                if allow_dirty:
                    self._state = "degraded"
                    self._last_error = "source_projection_pending"
                    raise SessionSummaryIndexDirtyError(
                        "session summary index has an outstanding source projection"
                    )
                self._rebuild_or_fail_locked(
                    base_revision=self._index_revision,
                )
        if not allow_dirty:
            self._raise_if_dirty_locked()

    def _raise_if_dirty_locked(self) -> None:
        if not self._dirty_marker_present_locked():
            return
        self._state = "degraded"
        self._last_error = "source_projection_pending"
        raise SessionSummaryIndexDirtyError(
            "session summary index has an outstanding source projection"
        )

    def _read_document_locked(self) -> dict[str, Any]:
        metadata = self._path.lstat()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise ValueError("session summary index must be a regular file")
        with self._path.open("r", encoding="utf-8") as source:
            raw = json.load(source)
        if not isinstance(raw, Mapping) or set(raw) != {
            "schema_version", "index_revision", "records"
        }:
            raise ValueError("session summary index document is invalid")
        schema_version = raw["schema_version"]
        revision = raw["index_revision"]
        records = raw["records"]
        if not isinstance(schema_version, int) or isinstance(schema_version, bool):
            raise ValueError("session summary schema version is invalid")
        if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
            raise ValueError("session summary revision is invalid")
        if not isinstance(records, list):
            raise ValueError("session summary records must be an array")
        parsed: dict[str, SessionSummary] = {}
        for record in records:
            summary = SessionSummary.from_dict(record)
            if summary.session_id in parsed:
                raise ValueError("session summary ids must be unique")
            parsed[summary.session_id] = summary
        delta_entries = 0
        if self._delta_path.exists() or self._delta_path.is_symlink():
            delta_metadata = self._delta_path.lstat()
            if stat.S_ISLNK(delta_metadata.st_mode) or not stat.S_ISREG(
                delta_metadata.st_mode
            ):
                raise ValueError("session summary delta must be a regular file")
            with self._delta_path.open("r", encoding="utf-8") as delta_source:
                for raw_delta in delta_source:
                    delta = json.loads(raw_delta)
                    if not isinstance(delta, Mapping) or set(delta) not in (
                        {"schema_version", "index_revision", "operation", "session_id", "record"},
                    ):
                        raise ValueError("session summary delta is invalid")
                    if delta["schema_version"] != _DELTA_SCHEMA_VERSION:
                        raise ValueError("session summary delta schema is invalid")
                    delta_revision = delta["index_revision"]
                    if (
                        not isinstance(delta_revision, int)
                        or isinstance(delta_revision, bool)
                        or delta_revision < 1
                    ):
                        raise ValueError("session summary delta revision is invalid")
                    if delta_revision <= revision:
                        continue
                    if delta_revision != revision + 1:
                        raise ValueError("session summary delta revisions are not contiguous")
                    session_id = _text(delta["session_id"], "session_id", allow_empty=False)
                    operation = delta["operation"]
                    if operation == "upsert":
                        summary = SessionSummary.from_dict(delta["record"])
                        if summary.session_id != session_id:
                            raise ValueError("session summary delta id does not match record")
                        parsed[session_id] = summary
                    elif operation == "delete":
                        if delta["record"] is not None:
                            raise ValueError("session summary delete delta must not contain a record")
                        parsed.pop(session_id, None)
                    else:
                        raise ValueError("session summary delta operation is invalid")
                    revision = delta_revision
                    delta_entries += 1
        self._delta_entries = delta_entries
        return {
            "schema_version": schema_version,
            "index_revision": revision,
            "records": parsed,
        }

    def _load_rebuild_records(self) -> dict[str, SessionSummary]:
        source = self._rebuild_loader() if self._rebuild_loader is not None else []
        records: dict[str, SessionSummary] = {}
        for item in source:
            summary = item if isinstance(item, SessionSummary) else SessionSummary.from_dict(item)
            records[summary.session_id] = summary
        return records

    def _read_dirty_tokens_locked(self) -> tuple[str, ...]:
        try:
            metadata = self._dirty_path.lstat()
        except FileNotFoundError:
            return ()
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SessionSummaryIndexCorruptionError(
                "session summary projection marker is unsafe"
            )
        try:
            with self._dirty_path.open("r", encoding="utf-8") as source:
                document = json.load(source)
        except (OSError, json.JSONDecodeError) as exc:
            raise SessionSummaryIndexCorruptionError(
                "session summary projection marker is corrupt"
            ) from exc
        if not isinstance(document, Mapping):
            raise SessionSummaryIndexCorruptionError(
                "session summary projection marker is invalid"
            )
        if set(document) != {"tokens"}:
            raise SessionSummaryIndexCorruptionError(
                "session summary projection marker is invalid"
            )
        tokens = document.get("tokens")
        if not isinstance(tokens, list) or not tokens:
            raise SessionSummaryIndexCorruptionError(
                "session summary projection marker is invalid"
            )
        for token in tokens:
            if not isinstance(token, str) or not token:
                raise SessionSummaryIndexCorruptionError(
                    "session summary projection marker is invalid"
                )
        if len(set(tokens)) != len(tokens):
            raise SessionSummaryIndexCorruptionError(
                "session summary projection marker is invalid"
            )
        return tuple(tokens)

    def _dirty_marker_present_locked(self) -> bool:
        try:
            self._dirty_path.lstat()
        except FileNotFoundError:
            return False
        return True

    def _read_dirty_tokens_or_quarantine_locked(self) -> tuple[str, ...]:
        try:
            return self._read_dirty_tokens_locked()
        except SessionSummaryIndexCorruptionError:
            self._quarantine_dirty_locked()
            return ()

    def _complete_dirty_token_locked(self, token: str) -> bool:
        tokens = set(self._read_dirty_tokens_locked())
        if token not in tokens:
            return False
        tokens.remove(token)
        if tokens:
            _write_atomic_json(self._dirty_path, {"tokens": sorted(tokens)})
            return True
        try:
            self._dirty_path.unlink()
            if os.name != "nt":
                directory_fd = os.open(self._dirty_path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        except FileNotFoundError:
            return False
        return True

    def _rebuild_or_fail_locked(
        self,
        *,
        base_revision: int,
        wait_for_existing: bool = True,
        cancel_event: threading.Event | None = None,
    ) -> None:
        observed_generation = self._rebuild_generation
        if self._rebuild_in_progress:
            if not wait_for_existing:
                raise SessionSummaryIndexError(
                    "session summary index rebuild is already in progress"
                )
            while self._rebuild_in_progress:
                self._rebuild_condition.wait()
            if self._rebuild_generation != observed_generation:
                if self._rebuild_failure is not None:
                    raise SessionSummaryIndexCorruptionError(
                        "session summary index could not be rebuilt"
                    ) from self._rebuild_failure
                return
        self._rebuild_in_progress = True
        self._rebuild_failure = None
        self._state = "rebuilding"
        try:
            attempts = 0
            while attempts < _MAX_REBUILD_ATTEMPTS:
                self._raise_if_rebuild_cancelled(cancel_event)
                attempts += 1
                attempt_revision = self._index_revision
                dirty_tokens = self._read_dirty_tokens_or_quarantine_locked()
                self._lock.release()
                try:
                    records = self._load_rebuild_records()
                finally:
                    self._lock.acquire()

                self._raise_if_rebuild_cancelled(cancel_event)
                current_dirty_tokens = (
                    self._read_dirty_tokens_or_quarantine_locked()
                )
                if (
                    self._index_revision != attempt_revision
                    or current_dirty_tokens != dirty_tokens
                ):
                    if attempts >= _MAX_REBUILD_ATTEMPTS:
                        raise SessionSummaryIndexError(
                            "session summary index rebuild did not converge"
                        )
                    continue

                revision = max(
                    1,
                    int(base_revision) + 1,
                    self._index_revision + 1,
                )
                self._publish_locked(records, revision)
                for token in (
                    set(current_dirty_tokens) - self._owned_projection_tokens
                ):
                    self._complete_dirty_token_locked(token)
                if not self._dirty_marker_present_locked():
                    self._state = "ready"
                self._last_error = ""
                return
        except SessionSummaryIndexRebuildCancelled as exc:
            self._state = "degraded"
            self._last_error = "rebuild_cancelled"
            self._rebuild_failure = exc
            raise
        except SessionSummaryIndexWriteError as exc:
            self._rebuild_failure = exc
            raise
        except Exception as exc:
            self._state = "degraded"
            self._last_error = "rebuild_failed"
            failure = SessionSummaryIndexCorruptionError(
                "session summary index could not be rebuilt"
            )
            self._rebuild_failure = failure
            raise failure from exc
        finally:
            self._rebuild_in_progress = False
            self._rebuild_generation += 1
            self._rebuild_condition.notify_all()

    @staticmethod
    def _raise_if_rebuild_cancelled(
        cancel_event: threading.Event | None,
    ) -> None:
        if cancel_event is not None and cancel_event.is_set():
            raise SessionSummaryIndexRebuildCancelled(
                "session summary index rebuild was cancelled"
            )

    def _publish_locked(self, records: dict[str, SessionSummary], revision: int) -> None:
        document = {
            "schema_version": SCHEMA_VERSION,
            "index_revision": revision,
            "records": [
                summary.to_dict()
                for summary in sorted(records.values(), key=lambda item: item.session_id)
            ],
        }
        try:
            _write_atomic_json(self._path, document)
        except SessionSummaryIndexWriteError as exc:
            if exc.committed:
                self._records = records
                self._index_revision = revision
                self._loaded = True
            self._state = "degraded"
            self._last_error = "publish_failed"
            raise
        self._records = records
        self._index_revision = revision
        self._loaded = True
        self._delta_entries = 0
        try:
            self._delta_path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove compacted session summary delta", exc_info=True)
        if self._dirty_marker_present_locked():
            self._state = "degraded"
            self._last_error = "source_projection_pending"
        else:
            self._state = "ready"
            self._last_error = ""

    def _publish_delta_locked(
        self,
        *,
        operation: str,
        session_id: str,
        summary: SessionSummary | None,
        records: dict[str, SessionSummary],
        revision: int,
    ) -> None:
        if operation not in {"upsert", "delete"}:
            raise ValueError("session summary delta operation is invalid")
        if operation == "upsert" and summary is None:
            raise ValueError("session summary upsert delta requires a record")
        if operation == "delete" and summary is not None:
            raise ValueError("session summary delete delta must not contain a record")
        self._delta_path.parent.mkdir(parents=True, exist_ok=True)
        if self._delta_path.is_symlink():
            raise SessionSummaryIndexWriteError(
                "session summary delta must not be a symbolic link",
                committed=False,
            )
        delta = {
            "schema_version": _DELTA_SCHEMA_VERSION,
            "index_revision": revision,
            "operation": operation,
            "session_id": session_id,
            "record": summary.to_dict() if summary is not None else None,
        }
        try:
            with self._delta_path.open("a", encoding="utf-8", newline="\n") as output:
                json.dump(delta, output, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
                output.write("\n")
                output.flush()
                os.fsync(output.fileno())
        except OSError as exc:
            raise SessionSummaryIndexWriteError(
                "failed to append session summary delta",
                committed=False,
            ) from exc
        self._records = records
        self._index_revision = revision
        self._loaded = True
        self._delta_entries += 1
        self._state = "ready" if not self._dirty_marker_present_locked() else "degraded"
        self._last_error = "" if self._state == "ready" else "source_projection_pending"
        if self._delta_entries >= _DELTA_COMPACT_THRESHOLD:
            try:
                self._publish_locked(records, revision)
            except SessionSummaryIndexWriteError:
                logger.warning("Deferred session summary delta compaction failed", exc_info=True)

    def _quarantine_locked(self) -> None:
        try:
            metadata = self._path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SessionSummaryIndexCorruptionError(
                "unsafe session summary index cannot be quarantined"
            )
        quarantine = self._path.with_name(
            f"{self._path.name}.corrupt.{time.time_ns()}.{uuid.uuid4().hex}"
        )
        os.replace(self._path, quarantine)

    def _quarantine_dirty_locked(self) -> None:
        try:
            metadata = self._dirty_path.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise SessionSummaryIndexCorruptionError(
                "unsafe session summary projection marker cannot be quarantined"
            )
        quarantine = self._dirty_path.with_name(
            f"{self._dirty_path.name}.corrupt.{time.time_ns()}.{uuid.uuid4().hex}"
        )
        os.replace(self._dirty_path, quarantine)


def _write_atomic_json(path: Path, document: Mapping[str, Any]) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.parent.is_symlink():
        raise SessionSummaryIndexWriteError(
            "session summary index parent is unsafe",
            committed=False,
        )
    temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    committed = False
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as output:
            json.dump(document, output, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        committed = True
        if os.name != "nt":
            directory_fd = os.open(target.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
    except Exception as exc:
        raise SessionSummaryIndexWriteError(
            "failed to publish session summary index",
            committed=committed,
        ) from exc
    finally:
        if not committed:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def _encode_cursor(revision: int, sort_key: tuple[int, str, str]) -> str:
    payload = json.dumps(
        {"v": _CURSOR_VERSION, "r": revision, "s": list(sort_key)},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _decode_cursor(cursor: str) -> tuple[int, tuple[int, str, str]]:
    if not isinstance(cursor, str):
        raise InvalidSessionCursor("session cursor is invalid")
    if not cursor:
        raise InvalidSessionCursor("session cursor is invalid")
    if len(cursor) > _MAX_CURSOR_LENGTH:
        raise InvalidSessionCursor("session cursor is invalid")
    try:
        padding = "=" * (-len(cursor) % 4)
        raw = base64.b64decode(
            (cursor + padding).encode("ascii"),
            altchars=b"-_",
            validate=True,
        )
        payload = json.loads(raw.decode("utf-8"))
    except ValueError as exc:
        raise InvalidSessionCursor("session cursor is invalid") from exc
    if not isinstance(payload, Mapping):
        raise InvalidSessionCursor("session cursor is invalid")
    if set(payload) != {"v", "r", "s"}:
        raise InvalidSessionCursor("session cursor is invalid")
    version = payload.get("v")
    revision = payload.get("r")
    sort_key = payload.get("s")
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidSessionCursor("session cursor is invalid")
    if version != _CURSOR_VERSION:
        raise InvalidSessionCursor("session cursor is invalid")
    if not isinstance(revision, int) or isinstance(revision, bool):
        raise InvalidSessionCursor("session cursor is invalid")
    if revision < 0:
        raise InvalidSessionCursor("session cursor is invalid")
    if not isinstance(sort_key, list):
        raise InvalidSessionCursor("session cursor is invalid")
    if len(sort_key) != 3:
        raise InvalidSessionCursor("session cursor is invalid")
    pinned = sort_key[0]
    if not isinstance(pinned, int) or isinstance(pinned, bool):
        raise InvalidSessionCursor("session cursor is invalid")
    if pinned not in {0, 1}:
        raise InvalidSessionCursor("session cursor is invalid")
    if not isinstance(sort_key[1], str):
        raise InvalidSessionCursor("session cursor is invalid")
    if not isinstance(sort_key[2], str):
        raise InvalidSessionCursor("session cursor is invalid")
    return revision, (pinned, sort_key[1], sort_key[2])
