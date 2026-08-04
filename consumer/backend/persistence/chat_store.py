"""Chat history storage for frontend display and conversation rendering.

This store is responsible ONLY for:
- Frontend chat history display
- Session management (create, list, delete)
- Message persistence for UI rendering

For LLM context and conversation memory, use ExecutionStore.
"""

from __future__ import annotations

import json
import os
import hashlib
import re
import shutil
import threading
import time
import uuid
from copy import deepcopy
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.identifiers import resolve_session_storage_dir, validate_session_id
from utils.logging import get_logger

logger = get_logger("stores.chat")

_SNAPSHOT_FILENAME = "snapshot.json"
_EVENT_LOG_FILENAME = "events.jsonl"
_TOOL_OUTPUTS_DIRNAME = "tool-outputs"
_TOOL_OUTPUT_REF_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_TOOL_OUTPUT_METADATA_MAX_BYTES = 64 * 1024
_INTERACTION_CANCELLED_EVENT_TYPE = "interaction_cancelled"
_LEGACY_MIGRATED_SUFFIX = ".migrated"
_STORAGE_VERSION = "append-only-v1"
_SUPPORTED_EVENT_NAMES = {"chat_message", "agent.v2"}
_POST_TERMINAL_MODEL_EVENT_TYPES = {"token.delta", "reasoning.summary"}


def _public_chat_events(events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Hide model output emitted after a terminal interaction boundary.

    Older openJiuwen runs could start one extra model iteration after
    request_user_interaction had already ended the turn. Keep the durable raw
    events for diagnostics, but never expose those trailing model deltas to
    conversation clients.
    """
    public_events: List[Dict[str, Any]] = []
    terminal_runs: set[str] = set()
    for event in events or []:
        data = event.get("data") if isinstance(event, dict) else None
        if (
            not isinstance(data, dict)
            or event.get("event") != "agent.v2"
            or data.get("protocol") != "agent-stream.v2"
        ):
            public_events.append(event)
            continue

        run_id = str(data.get("run_id") or "").strip()
        event_type = str(data.get("type") or "").strip()
        payload = data.get("payload") if isinstance(data.get("payload"), dict) else {}
        if (
            run_id
            and event_type == "iteration.completed"
            and str(payload.get("status") or "").strip() == "terminal_tool_batch"
        ):
            terminal_runs.add(run_id)

        if run_id in terminal_runs and event_type in _POST_TERMINAL_MODEL_EVENT_TYPES:
            continue

        public_events.append(event)
        if run_id and event_type == "turn.finished":
            terminal_runs.discard(run_id)
    return public_events


@dataclass(frozen=True)
class ToolOutputLimits:
    """Storage quotas for durable, truncated tool outputs."""

    max_output_bytes: int = 8 * 1024 * 1024
    max_run_bytes: int = 64 * 1024 * 1024
    max_session_bytes: int = 256 * 1024 * 1024
    max_run_count: int = 64
    max_session_count: int = 256

    @classmethod
    def from_config(cls, config: Any) -> "ToolOutputLimits":
        return cls(
            max_output_bytes=max(
                1,
                int(getattr(config, "tool_output_max_bytes", cls.max_output_bytes)),
            ),
            max_run_bytes=max(
                1,
                int(getattr(config, "tool_output_run_max_bytes", cls.max_run_bytes)),
            ),
            max_session_bytes=max(
                1,
                int(
                    getattr(
                        config,
                        "tool_output_session_max_bytes",
                        cls.max_session_bytes,
                    )
                ),
            ),
            max_run_count=max(
                1,
                int(getattr(config, "tool_output_run_max_count", cls.max_run_count)),
            ),
            max_session_count=max(
                1,
                int(
                    getattr(
                        config,
                        "tool_output_session_max_count",
                        cls.max_session_count,
                    )
                ),
            ),
        )


# ============================================================================
# Internal Append-Only File-Based Storage
# ============================================================================

@dataclass
class _FileChatSession:
    """Internal: Represents a chat session snapshot."""

    id: str
    title: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    session_history: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    active_run_id: Optional[str] = None
    storage_version: str = _STORAGE_VERSION

    @property
    def history(self) -> List[Dict[str, str]]:
        """Get chat messages from events."""
        msgs = []
        for event in self.events:
            if event.get("event") == "chat_message":
                msgs.append(event.get("data", {}))
        return msgs

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "_FileChatSession":
        return cls(
            id=data.get("id", ""),
            title=data.get("title", ""),
            metadata=data.get("metadata", {}) or {},
            events=data.get("events", []),
            session_history=data.get("session_history", []),
            created_at=data.get("created_at", ""),
            updated_at=data.get("updated_at", ""),
            active_run_id=data.get("active_run_id"),
            storage_version=data.get("storage_version", _STORAGE_VERSION),
        )


class _AppendOnlyChatStore:
    """
    Internal: Thread-safe append-only file store for chat sessions.

    Each session uses a small snapshot plus an append-only JSONL event log:
    - {storage_dir}/{session_id}/snapshot.json
    - {storage_dir}/{session_id}/events.jsonl
    """

    def __init__(
        self,
        storage_dir: str,
        *,
        tool_output_limits: Optional[ToolOutputLimits] = None,
    ) -> None:
        self._storage_dir = str(Path(storage_dir).resolve())
        self._tool_output_limits = tool_output_limits or ToolOutputLimits()
        self._lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._ensure_storage_dir()

    def _lock_for_session(self, session_id: str) -> threading.Lock:
        session_id = validate_session_id(session_id)
        with self._lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def _ensure_storage_dir(self) -> None:
        """Create storage directory if it doesn't exist."""
        os.makedirs(self._storage_dir, exist_ok=True)

    def _session_dir(self, session_id: str) -> str:
        return str(resolve_session_storage_dir(self._storage_dir, session_id))

    def _snapshot_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), _SNAPSHOT_FILENAME)

    def _event_log_path(self, session_id: str) -> str:
        return os.path.join(self._session_dir(session_id), _EVENT_LOG_FILENAME)

    def _tool_output_run_dir(self, session_id: str, run_id: str) -> Path:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        session_dir = Path(self._session_dir(session_id)).resolve()
        run_key = hashlib.sha256(normalized_run_id.encode("utf-8")).hexdigest()
        candidate = (session_dir / _TOOL_OUTPUTS_DIRNAME / run_key).resolve()
        candidate.relative_to(session_dir)
        return candidate

    def _tool_output_path(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Path:
        normalized_ref = str(output_ref or "").strip().lower()
        if not _TOOL_OUTPUT_REF_PATTERN.fullmatch(normalized_ref):
            raise ValueError("output_ref is invalid")
        return self._tool_output_run_dir(session_id, run_id) / "{}.json".format(
            normalized_ref
        )

    def _tool_output_content_path(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Path:
        return self._tool_output_path(session_id, run_id, output_ref).with_suffix(".txt")

    @staticmethod
    def _artifact_storage_size(metadata_path: Path) -> int:
        size = metadata_path.stat().st_size
        content_path = metadata_path.with_suffix(".txt")
        if content_path.is_file():
            size += content_path.stat().st_size
        return size

    def _artifact_usage_unlocked(self, session_id: str, run_id: str) -> Dict[str, int]:
        session_root = Path(self._session_dir(session_id)) / _TOOL_OUTPUTS_DIRNAME
        run_dir = self._tool_output_run_dir(session_id, run_id)
        usage = {
            "run_bytes": 0,
            "run_count": 0,
            "session_bytes": 0,
            "session_count": 0,
        }
        if not session_root.is_dir():
            return usage
        for metadata_path in session_root.glob("*/*.json"):
            if not metadata_path.is_file():
                continue
            try:
                artifact_size = self._artifact_storage_size(metadata_path)
            except OSError:
                continue
            usage["session_bytes"] += artifact_size
            usage["session_count"] += 1
            if metadata_path.parent == run_dir:
                usage["run_bytes"] += artifact_size
                usage["run_count"] += 1
        return usage

    def _load_tool_output_metadata_unlocked(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Optional[Dict[str, Any]]:
        path = self._tool_output_path(session_id, run_id, output_ref)
        if not path.is_file():
            return None
        try:
            file_size = path.stat().st_size
            max_legacy_bytes = (
                self._tool_output_limits.max_output_bytes * 6
                + _TOOL_OUTPUT_METADATA_MAX_BYTES
            )
            if file_size > max_legacy_bytes:
                logger.warning(
                    "Rejected oversized tool output artifact session=%s run=%s ref=%s",
                    session_id,
                    run_id,
                    output_ref,
                )
                return None
            with open(path, "r", encoding="utf-8") as f:
                record = json.load(f)
        except (IOError, OSError, json.JSONDecodeError):
            logger.warning(
                "Failed to read tool output artifact session=%s run=%s ref=%s",
                session_id,
                run_id,
                output_ref,
            )
            return None
        if not isinstance(record, dict):
            return None
        if (
            record.get("session_id") != session_id
            or record.get("run_id") != run_id
            or record.get("output_ref") != str(output_ref or "").strip().lower()
        ):
            logger.warning(
                "Rejected mismatched tool output artifact session=%s run=%s ref=%s",
                session_id,
                run_id,
                output_ref,
            )
            return None
        try:
            output_size = int(record.get("output_size"))
        except (TypeError, ValueError):
            return None
        if output_size < 0 or output_size > self._tool_output_limits.max_output_bytes:
            return None
        return record

    def _legacy_session_path(self, session_id: str) -> str:
        normalized = validate_session_id(session_id)
        return os.path.join(self._storage_dir, "{}.json".format(normalized))

    def _legacy_migrated_path(self, session_id: str) -> str:
        return "{}{}".format(
            self._legacy_session_path(session_id),
            _LEGACY_MIGRATED_SUFFIX,
        )

    def _now_iso(self) -> str:
        """Get current timestamp in ISO format."""
        return datetime.utcnow().isoformat() + "Z"

    def _ensure_session_dir_unlocked(self, session_id: str) -> None:
        os.makedirs(self._session_dir(session_id), exist_ok=True)

    def _read_snapshot_unlocked(self, session_id: str) -> Optional[_FileChatSession]:
        """Read a session snapshot or migrate the legacy flat JSON file."""
        path = self._snapshot_path(session_id)
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    return _FileChatSession.from_dict(json.load(f))
            except (json.JSONDecodeError, IOError):
                logger.warning("Failed to read chat snapshot for session %s", session_id)
                return None

        return self._migrate_legacy_session_unlocked(session_id)

    def _write_snapshot_unlocked(self, session: _FileChatSession) -> None:
        """Atomically write the compact snapshot."""
        self._ensure_session_dir_unlocked(session.id)
        session.storage_version = _STORAGE_VERSION
        path = self._snapshot_path(session.id)
        tmp_path = "{}.tmp".format(path)
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(session.to_dict(), f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _truncate_event_log_unlocked(self, session_id: str) -> None:
        self._ensure_session_dir_unlocked(session_id)
        with open(self._event_log_path(session_id), "w", encoding="utf-8"):
            pass

    def _migrate_legacy_session_unlocked(
        self,
        session_id: str,
    ) -> Optional[_FileChatSession]:
        """Move an old {session_id}.json file into the append-only layout."""
        legacy_path = self._legacy_session_path(session_id)
        if not os.path.exists(legacy_path):
            return None

        try:
            with open(legacy_path, "r", encoding="utf-8") as f:
                session = _FileChatSession.from_dict(json.load(f))
        except (json.JSONDecodeError, IOError):
            logger.warning("Failed to read legacy chat session %s", session_id)
            return None

        self._write_snapshot_unlocked(session)
        self._truncate_event_log_unlocked(session.id)

        try:
            os.replace(legacy_path, self._legacy_migrated_path(session_id))
        except OSError:
            logger.warning("Failed to mark legacy chat session %s as migrated", session_id)

        return session

    def _read_event_log_unlocked(self, session_id: str) -> List[Dict[str, Any]]:
        """Read raw appended events from JSONL."""
        path = self._event_log_path(session_id)
        if not os.path.exists(path):
            return []

        events: List[Dict[str, Any]] = []
        try:
            with open(path, "r", encoding="utf-8") as f:
                for line_number, line in enumerate(f, start=1):
                    raw = line.strip()
                    if not raw:
                        continue
                    try:
                        event = json.loads(raw)
                    except json.JSONDecodeError:
                        logger.warning(
                            "Skipping invalid chat event log line session=%s line=%s",
                            session_id,
                            line_number,
                        )
                        continue
                    if isinstance(event, dict):
                        events.append(event)
        except IOError:
            logger.warning("Failed to read chat event log for session %s", session_id)

        return events

    def _append_events_to_log_unlocked(
        self,
        session_id: str,
        events: List[Dict[str, Any]],
    ) -> None:
        self._ensure_session_dir_unlocked(session_id)
        with open(self._event_log_path(session_id), "a", encoding="utf-8") as f:
            for event in events:
                f.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")))
                f.write("\n")

    def _valid_events(self, events: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        valid_events: List[Dict[str, Any]] = []
        for event in events:
            if not isinstance(event, dict):
                logger.warning("Ignoring non-dict event for persistence: %r", event)
                continue

            event_name = event.get("event")
            if event_name not in _SUPPORTED_EVENT_NAMES:
                logger.warning(
                    "Ignoring unsupported event type for persistence: %s",
                    event_name,
                )
                continue

            valid_events.append(deepcopy(event))

        return valid_events

    def _session_ids_unlocked(self) -> List[str]:
        if not os.path.exists(self._storage_dir):
            return []

        session_ids = set()
        for name in os.listdir(self._storage_dir):
            candidate_id = name[:-5] if name.endswith(".json") else name
            try:
                validate_session_id(candidate_id)
            except ValueError:
                logger.warning("Ignoring invalid chat session storage entry: %s", name)
                continue
            path = os.path.join(self._storage_dir, name)
            if os.path.islink(path):
                logger.warning("Ignoring symlinked chat session storage entry: %s", name)
                continue
            if os.path.isdir(path) and os.path.exists(
                os.path.join(path, _SNAPSHOT_FILENAME)
            ):
                session_ids.add(name)
            elif name.endswith(".json"):
                session_ids.add(name[:-5])

        return list(session_ids)

    def _replay_event_log_unlocked(
        self,
        snapshot: _FileChatSession,
    ) -> _FileChatSession:
        session = _FileChatSession.from_dict(snapshot.to_dict())
        for event in self._read_event_log_unlocked(session.id):
            self._apply_event_to_session(session, event)
        return session

    def _get_session_unlocked(
        self,
        session_id: str,
    ) -> Optional[_FileChatSession]:
        snapshot = self._read_snapshot_unlocked(session_id)
        if not snapshot:
            return None
        return self._replay_event_log_unlocked(snapshot)

    @staticmethod
    def _session_sort_key(session: _FileChatSession) -> tuple[bool, str]:
        return (
            bool((session.metadata or {}).get("pinned")),
            session.updated_at or session.created_at,
        )

    def list_sessions(self) -> List[_FileChatSession]:
        """List all chat sessions, with pinned sessions first, then newest first."""
        with self._lock:
            sessions = []
            for session_id in self._session_ids_unlocked():
                session = self._get_session_unlocked(session_id)
                if session:
                    sessions.append(session)

            sessions.sort(key=self._session_sort_key, reverse=True)
            return sessions

    def get_session(self, session_id: str) -> Optional[_FileChatSession]:
        """Get a specific chat session by ID."""
        with self._lock_for_session(session_id):
            return self._get_session_unlocked(session_id)

    def create_session(
        self,
        title: str = "",
        session_id: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> _FileChatSession:
        """Create a new chat session."""
        session_id = validate_session_id(session_id or str(uuid.uuid4()))
        with self._lock_for_session(session_id):
            now = self._now_iso()
            session = _FileChatSession(
                id=session_id,
                title=title,
                metadata=dict(metadata or {}),
                events=[],
                session_history=[],
                created_at=now,
                updated_at=now,
            )
            self._write_snapshot_unlocked(session)
            self._truncate_event_log_unlocked(session_id)
            return session

    def update_session(
        self,
        session_id: str,
        title: Optional[str] = None,
        session_history: Optional[List[Dict[str, Any]]] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[_FileChatSession]:
        """Update a chat session's title and/or session_history."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            if title is not None:
                session.title = title
            if session_history is not None:
                session.session_history = session_history
            if metadata is not None:
                session.metadata = dict(metadata)
            session.updated_at = self._now_iso()

            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def merge_session_metadata(
        self,
        session_id: str,
        metadata: Dict[str, Any],
    ) -> Optional[_FileChatSession]:
        """Merge metadata into an existing session without overwriting other keys."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            session.metadata = {
                **dict(session.metadata or {}),
                **dict(metadata or {}),
            }
            session.updated_at = self._now_iso()
            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def set_session_pinned(
        self,
        session_id: str,
        pinned: bool,
    ) -> Optional[_FileChatSession]:
        """Persist the UI pin state without changing chat recency."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            metadata = dict(session.metadata or {})
            metadata["pinned"] = bool(pinned)
            if pinned:
                metadata["pinned_at"] = self._now_iso()
            else:
                metadata.pop("pinned_at", None)
            session.metadata = metadata

            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def delete_session(self, session_id: str) -> bool:
        """Delete a chat session."""
        with self._lock_for_session(session_id):
            deleted = False
            session_dir = self._session_dir(session_id)
            if os.path.isdir(session_dir):
                shutil.rmtree(session_dir)
                deleted = True

            for path in (
                self._legacy_session_path(session_id),
                self._legacy_migrated_path(session_id),
            ):
                if os.path.exists(path):
                    os.remove(path)
                    deleted = True

        self._cleanup_session_lock(session_id)
        return deleted

    def _cleanup_session_lock(self, session_id: str) -> None:
        with self._lock:
            self._session_locks.pop(session_id, None)

    def append_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[_FileChatSession]:
        """Append a message to a chat session's history (as an event)."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            # Append as event to preserve order relative to other events
            event_data = {"role": role, "content": content}
            if metadata:
                event_data["metadata"] = metadata
            event = {
                "event": "chat_message",
                "data": event_data,
                "ts": int(time.time() * 1000)
            }
            self._append_events_to_log_unlocked(session_id, [event])
            session.updated_at = self._now_iso()

            # Auto-set title from first user message if empty
            if not session.title and role == "user" and content:
                # Use first 50 chars of first message as title
                session.title = content[:50] + ("..." if len(content) > 50 else "")

            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def record_interaction_cancellation(
        self,
        session_id: str,
        interaction_id: str,
        content: str,
        *,
        cancelled_at: str,
    ) -> Optional[_FileChatSession]:
        """Append one durable cancellation marker for an interaction."""
        normalized_id = str(interaction_id or "").strip()
        if not normalized_id:
            raise ValueError("interaction_id is required")

        with self._lock_for_session(session_id):
            snapshot = self._read_snapshot_unlocked(session_id)
            if not snapshot:
                return None

            current = self._replay_event_log_unlocked(snapshot)
            for event in current.events:
                if event.get("event") != "chat_message":
                    continue
                data = event.get("data") if isinstance(event.get("data"), dict) else {}
                metadata = (
                    data.get("metadata")
                    if isinstance(data.get("metadata"), dict)
                    else {}
                )
                if (
                    metadata.get("type") == _INTERACTION_CANCELLED_EVENT_TYPE
                    and str(metadata.get("interaction_id") or "").strip() == normalized_id
                ):
                    return current

            event = {
                "event": "chat_message",
                "data": {
                    "role": "system",
                    "content": str(content or ""),
                    "metadata": {
                        "type": _INTERACTION_CANCELLED_EVENT_TYPE,
                        "interaction_id": normalized_id,
                        "cancelled_at": str(cancelled_at or ""),
                    },
                },
                "ts": int(time.time() * 1000),
            }
            self._append_events_to_log_unlocked(session_id, [event])
            snapshot.updated_at = self._now_iso()
            self._write_snapshot_unlocked(snapshot)
            return self._get_session_unlocked(session_id)

    def _apply_event_to_session(
        self,
        session: _FileChatSession,
        event: Dict[str, Any],
    ) -> bool:
        """Apply one persisted event to an in-memory session."""
        event_name = event.get("event")
        if event_name not in {"chat_message", "agent.v2"}:
            logger.warning("Ignoring unsupported event type for persistence: %s", event_name)
            return False

        if event_name == "agent.v2" and session.events:
            data = event.get("data") or {}
            event_type = data.get("type")

            if event_type in ("token.delta", "subagent.delta"):
                payload = data.get("payload") or {}
                last_event = session.events[-1]
                last_data = last_event.get("data") or {}

                # Check if we can merge with the previous event
                if (
                    last_event.get("event") == "agent.v2"
                    and last_data.get("type") == event_type
                ):
                    last_payload = last_data.get("payload") or {}

                    can_merge = False
                    if event_type == "token.delta":
                        can_merge = last_payload.get("channel") == payload.get("channel")
                    elif event_type == "subagent.delta":
                        can_merge = (
                            last_payload.get("channel") == payload.get("channel")
                            and last_payload.get("agent_type") == payload.get("agent_type")
                            and last_payload.get("subagent_id") == payload.get("subagent_id")
                        )

                    if can_merge:
                        # Merge text
                        last_payload["text"] = str(last_payload.get("text", "")) + str(payload.get("text", ""))
                        # Update envelope metadata
                        last_data["seq"] = data.get("seq", last_data.get("seq"))
                        last_data["ts"] = data.get("ts", last_data.get("ts"))
                        if "event_id" in data:
                            last_data["event_id"] = data["event_id"]
                        return True

        session.events.append(deepcopy(event))
        return True

    def append_events(
        self,
        session_id: str,
        events: List[Dict[str, Any]],
    ) -> Optional[_FileChatSession]:
        """Append streamed events to the JSONL log without rewriting event history."""
        event_list = self._valid_events(list(events or []))
        if not event_list:
            return self.get_session(session_id)

        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            self._append_events_to_log_unlocked(session_id, event_list)
            # NOTE: snapshot.json is intentionally NOT rewritten here.
            # High-frequency events (e.g. token.delta) would cause excessive I/O.
            # compact_session() folds events into snapshot at turn end,
            # and get_session() replays the JSONL log for a complete view.
            return session

    def append_event(self, session_id: str, event: Dict[str, Any]) -> Optional[_FileChatSession]:
        """Append a streamed event to a chat session."""
        return self.append_events(session_id, [event])

    def store_tool_output(
        self,
        session_id: str,
        run_id: str,
        tool_call_id: Optional[str],
        output: str,
    ) -> Optional[Dict[str, Any]]:
        """Persist a bounded tool output for the lifetime of its chat session."""
        normalized_session_id = validate_session_id(session_id)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        text = str(output or "")
        output_bytes = text.encode("utf-8")
        output_size = len(output_bytes)
        if output_size > self._tool_output_limits.max_output_bytes:
            logger.warning(
                "Skipped tool output artifact above single-output quota session=%s run=%s bytes=%s limit=%s",
                normalized_session_id,
                normalized_run_id,
                output_size,
                self._tool_output_limits.max_output_bytes,
            )
            return None
        output_ref = uuid.uuid4().hex
        record = {
            "version": 2,
            "output_ref": output_ref,
            "session_id": normalized_session_id,
            "run_id": normalized_run_id,
            "tool_call_id": str(tool_call_id or "").strip() or None,
            "content_type": "text/plain; charset=utf-8",
            "output_size": output_size,
            "output_file": "{}.txt".format(output_ref),
            "created_at": self._now_iso(),
        }
        metadata_bytes = json.dumps(
            record,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        projected_size = len(metadata_bytes) + output_size

        with self._lock_for_session(normalized_session_id):
            if not self._read_snapshot_unlocked(normalized_session_id):
                return None
            usage = self._artifact_usage_unlocked(
                normalized_session_id,
                normalized_run_id,
            )
            if (
                usage["run_count"] >= self._tool_output_limits.max_run_count
                or usage["run_bytes"] + projected_size
                > self._tool_output_limits.max_run_bytes
                or usage["session_count"] >= self._tool_output_limits.max_session_count
                or usage["session_bytes"] + projected_size
                > self._tool_output_limits.max_session_bytes
            ):
                logger.warning(
                    "Skipped tool output artifact above run/session quota session=%s run=%s",
                    normalized_session_id,
                    normalized_run_id,
                )
                return None
            path = self._tool_output_path(
                normalized_session_id,
                normalized_run_id,
                output_ref,
            )
            content_path = path.with_suffix(".txt")
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp_path = path.with_suffix(".json.tmp")
            tmp_content_path = content_path.with_suffix(".txt.tmp")
            try:
                with open(tmp_content_path, "wb") as f:
                    f.write(output_bytes)
                with open(tmp_path, "wb") as f:
                    f.write(metadata_bytes)
                os.replace(tmp_content_path, content_path)
                os.replace(tmp_path, path)
            except OSError:
                for candidate in (tmp_path, tmp_content_path, path, content_path):
                    try:
                        candidate.unlink(missing_ok=True)
                    except OSError:
                        pass
                raise

        return {
            "output_ref": output_ref,
            "output_size": record["output_size"],
        }

    def get_tool_output(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Optional[Dict[str, Any]]:
        """Load one bounded output after validating its session and run scope."""
        normalized_session_id = validate_session_id(session_id)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        with self._lock_for_session(normalized_session_id):
            if not self._read_snapshot_unlocked(normalized_session_id):
                return None
            record = self._load_tool_output_metadata_unlocked(
                normalized_session_id,
                normalized_run_id,
                output_ref,
            )
            if record is None:
                return None
            if int(record.get("version") or 1) == 1:
                output = record.get("output")
            else:
                content_path = self._tool_output_content_path(
                    normalized_session_id,
                    normalized_run_id,
                    output_ref,
                )
                try:
                    if content_path.stat().st_size != record["output_size"]:
                        return None
                    with open(content_path, "rb") as f:
                        raw_output = f.read(self._tool_output_limits.max_output_bytes + 1)
                    output = raw_output.decode("utf-8")
                except (IOError, OSError, UnicodeDecodeError):
                    output = None
        if not isinstance(output, str) or record["output_size"] != len(output.encode("utf-8")):
            logger.warning(
                "Rejected corrupt tool output artifact session=%s run=%s ref=%s",
                normalized_session_id,
                normalized_run_id,
                output_ref,
            )
            return None
        return {**record, "output": output}

    def get_tool_output_file(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Optional[Dict[str, Any]]:
        """Return validated metadata and a bounded file path for streaming."""
        normalized_session_id = validate_session_id(session_id)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        with self._lock_for_session(normalized_session_id):
            if not self._read_snapshot_unlocked(normalized_session_id):
                return None
            record = self._load_tool_output_metadata_unlocked(
                normalized_session_id,
                normalized_run_id,
                output_ref,
            )
            if record is None:
                return None
            metadata_path = self._tool_output_path(
                normalized_session_id,
                normalized_run_id,
                output_ref,
            )
            if int(record.get("version") or 1) == 1:
                output = record.get("output")
                if (
                    not isinstance(output, str)
                    or len(output.encode("utf-8")) != record["output_size"]
                ):
                    return None
                artifact_path = metadata_path
                response_content_type = "application/json; charset=utf-8"
            else:
                artifact_path = metadata_path.with_suffix(".txt")
                try:
                    if artifact_path.stat().st_size != record["output_size"]:
                        return None
                except OSError:
                    return None
                response_content_type = (
                    record.get("content_type") or "text/plain; charset=utf-8"
                )
        return {
            **record,
            "artifact_path": str(artifact_path),
            "response_content_type": response_content_type,
        }

    def get_session_history(self, session_id: str) -> List[Dict[str, Any]]:
        """Get stored session history (TurnSummary list) for a session."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return []
            return list(session.session_history)

    def set_session_history(
        self,
        session_id: str,
        session_history: List[Dict[str, Any]],
    ) -> Optional[_FileChatSession]:
        """Persist session history (TurnSummary list) for a session."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            session.session_history = session_history
            session.updated_at = self._now_iso()
            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def set_active_run(self, session_id: str, run_id: str) -> Optional[_FileChatSession]:
        """Set the active run ID for a chat session."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            session.active_run_id = run_id
            session.updated_at = self._now_iso()
            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def clear_active_run(self, session_id: str) -> Optional[_FileChatSession]:
        """Clear the active run ID for a chat session."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None

            session.active_run_id = None
            session.updated_at = self._now_iso()
            self._write_snapshot_unlocked(session)
            return self._get_session_unlocked(session_id)

    def get_active_run(self, session_id: str) -> Optional[str]:
        """Get the active run ID for a chat session."""
        with self._lock_for_session(session_id):
            session = self._read_snapshot_unlocked(session_id)
            if not session:
                return None
            return session.active_run_id

    def compact_session(self, session_id: str) -> Optional[_FileChatSession]:
        """Fold appended events into the snapshot and clear the JSONL log."""
        with self._lock_for_session(session_id):
            session = self._get_session_unlocked(session_id)
            if not session:
                return None

            session.updated_at = self._now_iso()
            self._write_snapshot_unlocked(session)
            self._truncate_event_log_unlocked(session_id)
            return session


# ============================================================================
# Public API Classes
# ============================================================================

@dataclass
class ChatMessage:
    """Represents a single chat message."""

    id: str
    session_id: str
    role: str  # "user", "assistant", "system"
    content: str
    timestamp: datetime = field(default_factory=datetime.utcnow)
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "id": self.id,
            "session_id": self.session_id,
            "role": self.role,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "metadata": self.metadata,
        }


@dataclass
class ChatSession:
    """Represents a chat session with message history."""

    id: str
    title: Optional[str] = None
    created_at: str = ""
    updated_at: str = ""
    active_run_id: Optional[str] = None
    messages: List[ChatMessage] = field(default_factory=list)
    metadata: Dict[str, Any] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def message_count(self) -> int:
        """Get number of messages in session."""
        if self.messages:
            return len(self.messages)
        return int(self.metadata.get("message_count", 0))

    def to_dict(self, include_messages: bool = False) -> Dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "active_run_id": self.active_run_id,
            "message_count": self.message_count,
            "metadata": self.metadata,
        }
        if include_messages:
            result["messages"] = [m.to_dict() for m in self.messages]
            # Include client-safe events while retaining raw persisted events.
            if hasattr(self, "events"):
                result["events"] = _public_chat_events(self.events)
        return result

    @staticmethod
    def _parse_event_timestamp(event: Dict[str, Any]) -> datetime:
        ts = event.get("ts")
        if isinstance(ts, (int, float)):
            return datetime.utcfromtimestamp(ts / 1000)
        return datetime.utcnow()

    @classmethod
    def from_file_session(
        cls,
        session: _FileChatSession,
        *,
        include_messages: bool = False,
        message_limit: Optional[int] = None,
    ) -> "ChatSession":
        messages: List[ChatMessage] = []
        metadata: Dict[str, Any] = {}
        file_metadata = dict(getattr(session, "metadata", {}) or {})
        metadata.update(file_metadata)

        events = getattr(session, "events", []) or []
        if not include_messages:
            metadata["message_count"] = sum(
                1 for event in events if event.get("event") == "chat_message"
            )

        if include_messages:
            index = 0
            for event in events:
                if event.get("event") != "chat_message":
                    continue
                data = event.get("data", {}) or {}
                messages.append(
                    ChatMessage(
                        id="{}:{}".format(session.id, index),
                        session_id=session.id,
                        role=data.get("role", ""),
                        content=data.get("content", ""),
                        timestamp=cls._parse_event_timestamp(event),
                        metadata=data.get("metadata", {}) or {},
                    )
                )
                index += 1

            if message_limit:
                messages = messages[-message_limit:]

        return cls(
            id=session.id,
            title=getattr(session, "title", None),
            created_at=getattr(session, "created_at", ""),
            updated_at=getattr(session, "updated_at", ""),
            active_run_id=getattr(session, "active_run_id", None),
            messages=messages,
            metadata=metadata,
            events=events if include_messages else [],
        )


class ChatStore:
    """
    Append-only file-based store for chat sessions and messages.

    Uses a compact snapshot plus JSONL event log per session.
    """

    def __init__(
        self,
        storage_dir: str,
        *,
        tool_output_limits: Optional[ToolOutputLimits] = None,
    ) -> None:
        self._store = _AppendOnlyChatStore(
            storage_dir,
            tool_output_limits=tool_output_limits,
        )

    def create_session(
        self,
        session_id: Optional[str] = None,
        title: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> ChatSession:
        session = self._store.create_session(
            title=title or "",
            session_id=session_id,
            metadata=metadata,
        )
        return ChatSession.from_file_session(session)

    def get_session(self, session_id: str) -> Optional[ChatSession]:
        session = self._store.get_session(session_id)
        if not session:
            return None
        return ChatSession.from_file_session(session, include_messages=True)

    def add_message(
        self,
        session_id: str,
        role: str,
        content: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[ChatMessage]:
        session = self._store.append_message(
            session_id,
            role,
            content,
            metadata=metadata,
        )
        if not session:
            logger.warning("Session %s not found for message", session_id)
            return None

        message = ChatMessage(
            id=str(uuid.uuid4()),
            session_id=session_id,
            role=role,
            content=content,
            timestamp=datetime.utcnow(),
            metadata=metadata or {},
        )
        return message

    def record_interaction_cancellation(
        self,
        session_id: str,
        interaction_id: str,
        content: str,
        *,
        cancelled_at: str,
    ) -> bool:
        """Persist an idempotent system notice for an interaction cancellation."""
        session = self._store.record_interaction_cancellation(
            session_id,
            interaction_id,
            content,
            cancelled_at=cancelled_at,
        )
        return session is not None

    def get_history(
        self,
        session_id: str,
        limit: Optional[int] = None,
    ) -> List[ChatMessage]:
        session = self._store.get_session(session_id)
        if not session:
            return []
        session_view = ChatSession.from_file_session(
            session,
            include_messages=True,
            message_limit=limit,
        )
        return session_view.messages

    def list_sessions(self, limit: int = 1000) -> List[ChatSession]:
        sessions = self._store.list_sessions()
        result = [ChatSession.from_file_session(s) for s in sessions]
        return result[:limit]

    def prune_old_sessions(self, max_sessions: int) -> List[str]:
        """Delete oldest inactive sessions beyond *max_sessions* and return ids."""
        keep_count = max(0, int(max_sessions))
        sessions = self._store.list_sessions()
        deleted: List[str] = []
        for session in sessions[keep_count:]:
            if getattr(session, "active_run_id", None):
                continue
            if self._store.delete_session(session.id):
                deleted.append(session.id)
        return deleted

    def delete_session(self, session_id: str) -> bool:
        return self._store.delete_session(session_id)

    def update_session_title(self, session_id: str, title: str) -> Optional[ChatSession]:
        session = self._store.update_session(session_id, title=title)
        if not session:
            return None
        return ChatSession.from_file_session(session)

    def merge_session_metadata(
        self,
        session_id: str,
        metadata: Dict[str, Any],
    ) -> Optional[ChatSession]:
        session = self._store.merge_session_metadata(session_id, metadata)
        if not session:
            return None
        return ChatSession.from_file_session(session, include_messages=True)

    def set_session_pinned(self, session_id: str, pinned: bool) -> Optional[ChatSession]:
        session = self._store.set_session_pinned(session_id, pinned)
        if not session:
            return None
        return ChatSession.from_file_session(session)

    def append_event(self, session_id: str, event: Dict[str, Any]) -> bool:
        """Append a stream event to the session for frontend replay."""
        session = self._store.append_event(session_id, event)
        return session is not None

    def append_events(self, session_id: str, events: List[Dict[str, Any]]) -> bool:
        """Append stream events to the session for frontend replay."""
        event_list = list(events or [])
        if not event_list:
            return True
        session = self._store.append_events(session_id, event_list)
        return session is not None

    def store_tool_output(
        self,
        session_id: str,
        run_id: str,
        tool_call_id: Optional[str],
        output: str,
    ) -> Optional[Dict[str, Any]]:
        """Persist complete output while stream events retain only a preview."""
        return self._store.store_tool_output(
            session_id,
            run_id,
            tool_call_id,
            output,
        )

    def get_tool_output(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Optional[Dict[str, Any]]:
        """Return a complete output scoped to this store, session, and run."""
        return self._store.get_tool_output(session_id, run_id, output_ref)

    def get_tool_output_file(
        self,
        session_id: str,
        run_id: str,
        output_ref: str,
    ) -> Optional[Dict[str, Any]]:
        """Return metadata and a path suitable for a streamed response."""
        return self._store.get_tool_output_file(session_id, run_id, output_ref)

    def compact_session(self, session_id: str) -> bool:
        """Fold append-only events into the compact snapshot."""
        session = self._store.compact_session(session_id)
        return session is not None

    def set_active_run(self, session_id: str, run_id: str) -> bool:
        """Mark the session as having a currently running workflow."""
        session = self._store.set_active_run(session_id, run_id)
        return session is not None

    def clear_active_run(self, session_id: str) -> bool:
        """Clear the session's active workflow marker."""
        session = self._store.clear_active_run(session_id)
        return session is not None

    def get_active_run(self, session_id: str) -> Optional[str]:
        """Return the session's active workflow id if one is recorded."""
        return self._store.get_active_run(session_id)
