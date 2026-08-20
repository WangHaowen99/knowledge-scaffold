"""
Persistent storage for workflow execution records and LLM conversation context.

This module provides the ExecutionStore class for:
1. Storing workflow execution history (turn status, output, metadata)
2. Managing LLM conversation context (session_history for multi-turn memory)

Storage structure:
    {execution_storage_dir}/
        {session_id}/
            execution.json    # SessionExecutionHistory + session_history
"""

import copy
import json
import os
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from agent.state import (
    SessionExecutionHistory,
    TurnExecutionRecord,
)
from utils.identifiers import resolve_session_storage_dir, validate_session_id
from utils.read_docs import normalize_read_documents_registry


class InteractionCheckpointError(ValueError):
    """Raised when a user response does not match the durable checkpoint."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "INTERACTION_CONFLICT",
        existing_run_id: Optional[str] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.existing_run_id = existing_run_id


class IdempotencyConflictError(ValueError):
    """Raised when an idempotency key is reused for another request."""

    code = "IDEMPOTENCY_CONFLICT"


class ExecutionStoreCorruptionError(RuntimeError):
    """Raised after a corrupt execution file is isolated."""

    code = "EXECUTION_STORE_CORRUPT"


INTERACTION_CANCELLED_MESSAGE = "我已主动取消上一条交互请求，未提供所请求的信息或选择。"
INTERACTION_CANCELLED_NOTICE = "用户取消了操作"


def _latest_pending_interaction(
    history: SessionExecutionHistory,
) -> tuple[Optional[TurnExecutionRecord], Optional[Dict[str, Any]]]:
    turns = list(history.get("turns") or [])
    if not turns:
        return None, None

    for turn in reversed(turns):
        if turn.get("status") != "awaiting_input" or turn.get("interaction_consumed_at"):
            continue
        interaction = turn.get("pending_interaction")
        if not isinstance(interaction, dict):
            final_output = turn.get("final_output")
            interaction = (
                final_output.get("interaction")
                if isinstance(final_output, dict)
                else None
            )
        if not isinstance(interaction, dict):
            raise InteractionCheckpointError(
                "pending interaction checkpoint is invalid",
                code="INTERACTION_CONFLICT",
            )
        return turn, interaction
    return None, None


def _validated_interaction_response(
    history: SessionExecutionHistory,
    response: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    _pending_turn, interaction = _latest_pending_interaction(history)
    if interaction is None:
        if response is not None:
            raise InteractionCheckpointError("session has no pending interaction")
        return None
    if response is None:
        raise InteractionCheckpointError("session is awaiting an interaction response")

    expected_id = str(interaction.get("id") or "").strip()
    response_id = str(response.get("interaction_id") or "").strip()
    if not expected_id or response_id != expected_id:
        raise InteractionCheckpointError("interaction_id does not match the pending interaction")

    expected_type = str(interaction.get("type") or "").strip()
    response_type = str(response.get("type") or expected_type).strip()
    if response_type != expected_type:
        raise InteractionCheckpointError("interaction type does not match the pending interaction")

    canonical: Dict[str, Any] = {
        "interaction_id": expected_id,
        "type": expected_type,
    }
    if expected_type == "input":
        if "value" not in response or not isinstance(response.get("value"), str):
            raise InteractionCheckpointError("input interaction requires a text value")
        value = response["value"]
        if not value.strip():
            raise InteractionCheckpointError("input interaction value must not be empty")
        canonical["value"] = value
        return canonical

    options = interaction.get("options")
    if not isinstance(options, list) or not options:
        raise InteractionCheckpointError("pending interaction has no selectable options")

    option_id = str(response.get("option_id") or "").strip()
    selected = None
    if option_id:
        selected = next(
            (
                option for option in options
                if isinstance(option, dict) and str(option.get("id") or "").strip() == option_id
            ),
            None,
        )
    elif "value" in response:
        selected = next(
            (
                option for option in options
                if isinstance(option, dict) and option.get("value") == response.get("value")
            ),
            None,
        )

    if not isinstance(selected, dict):
        raise InteractionCheckpointError("selected option is not part of the pending interaction")
    if "value" in response and response.get("value") != selected.get("value"):
        raise InteractionCheckpointError("option_id and value refer to different options")

    canonical["option_id"] = str(selected.get("id") or "").strip()
    canonical["value"] = selected.get("value")
    return canonical


def _normalize_loaded_history(
    history: SessionExecutionHistory,
) -> SessionExecutionHistory:
    """Normalize additive Turn fields without weakening corrupt-file handling."""

    normalized = copy.deepcopy(history)
    turns = normalized.get("turns")
    if not isinstance(turns, list):
        raise ExecutionStoreCorruptionError("execution history turns must be a list")
    for turn in turns:
        if not isinstance(turn, dict):
            raise ExecutionStoreCorruptionError("execution history turn must be an object")
        if isinstance(turn.get("pending_interaction"), dict):
            continue
        final_output = turn.get("final_output")
        interaction = (
            final_output.get("interaction")
            if isinstance(final_output, dict)
            else None
        )
        if isinstance(interaction, dict):
            turn["pending_interaction"] = copy.deepcopy(interaction)
    normalized.setdefault("session_history", [])
    normalized.setdefault("read_documents", normalize_read_documents_registry(None))
    return normalized


def _turn_for_idempotency_key(
    history: SessionExecutionHistory,
    idempotency_key_hash: Optional[str],
) -> Optional[TurnExecutionRecord]:
    normalized_key = str(idempotency_key_hash or "").strip()
    if not normalized_key:
        return None
    for turn in history.get("turns", []):
        if str(turn.get("idempotency_key_hash") or "") == normalized_key:
            return turn
    return None


def _next_turn_id(history: SessionExecutionHistory) -> int:
    return max(
        (int(turn.get("turn_id") or 0) for turn in history.get("turns", [])),
        default=0,
    ) + 1


def _mark_interrupted_turns(history: SessionExecutionHistory) -> int:
    interrupted = 0
    ended_at = datetime.now().isoformat()
    for turn in history.get("turns", []):
        if str(turn.get("status") or "") not in {"queued", "running"}:
            continue
        turn["status"] = "failed"
        turn["end_time"] = ended_at
        turn["error_code"] = "SERVICE_RESTARTED"
        turn["final_output"] = {
            "error": "Execution was interrupted by a service restart",
            "error_code": "SERVICE_RESTARTED",
        }
        interrupted += 1
    return interrupted


def _turn_for_interaction(
    history: SessionExecutionHistory,
    interaction_id: str,
) -> tuple[Optional[TurnExecutionRecord], Optional[Dict[str, Any]]]:
    requested_id = str(interaction_id or "").strip()
    for turn in reversed(history.get("turns", [])):
        interaction = turn.get("pending_interaction")
        if not isinstance(interaction, dict):
            final_output = turn.get("final_output")
            interaction = (
                final_output.get("interaction")
                if isinstance(final_output, dict)
                else None
            )
        if (
            isinstance(interaction, dict)
            and str(interaction.get("id") or "").strip() == requested_id
        ):
            return turn, interaction
    return None, None


def _canonical_response_for_interaction(
    interaction: Dict[str, Any],
    response: Dict[str, Any],
) -> Dict[str, Any]:
    validation_history: SessionExecutionHistory = {
        "session_id": "validation",
        "created_at": "",
        "updated_at": "",
        "turns": [
            {
                "turn_id": 1,
                "session_id": "validation",
                "run_id": "validation",
                "user_request": "validation",
                "start_time": "",
                "end_time": "",
                "status": "awaiting_input",
                "final_output": None,
                "summary": None,
                "ontology_version": None,
                "pending_interaction": copy.deepcopy(interaction),
            }
        ],
        "session_history": [],
        "read_documents": normalize_read_documents_registry(None),
    }
    canonical = _validated_interaction_response(validation_history, response)
    if canonical is None:
        raise InteractionCheckpointError("interaction response is required")
    return canonical


class ExecutionStore:
    """
    Persistent storage for workflow execution records and LLM context.

    Responsibilities:
    1. Record turn-level workflow execution results
    2. Manage LLM conversation context (session_history for multi-turn memory)

    Thread-safe for concurrent access within a single process.
    """

    def __init__(self, storage_dir: str):
        """
        Initialize the ExecutionStore.

        Args:
            storage_dir: Directory for storing execution records.
        """
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Thread safety — per-session locks for concurrent access
        self._lock = threading.Lock()
        self._session_locks: Dict[str, threading.Lock] = {}

        # In-memory cache for active sessions (session_id -> SessionExecutionHistory)
        self._cache: Dict[str, SessionExecutionHistory] = {}

        # Track current turn for each session (session_id -> turn_id)
        self._current_turns: Dict[str, int] = {}
        self._corrupt_sessions: Dict[str, str] = {}

    @property
    def corruption_count(self) -> int:
        return len(self._corrupt_sessions)

    @property
    def healthy(self) -> bool:
        return not self._corrupt_sessions

    def _lock_for_session(self, session_id: str) -> threading.Lock:
        session_id = validate_session_id(session_id)
        with self._lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def _get_session_path(self, session_id: str) -> Path:
        """Get the file path for a session's execution history."""
        session_dir = resolve_session_storage_dir(self.storage_dir, session_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / "execution.json"

    def _load_session(self, session_id: str) -> SessionExecutionHistory:
        """Load session execution history from disk or create new."""
        session_id = validate_session_id(session_id)
        if session_id in self._corrupt_sessions:
            raise ExecutionStoreCorruptionError(
                "execution history is isolated as corrupt"
            )
        if session_id in self._cache:
            return self._cache[session_id]

        session_path = self._get_session_path(session_id)
        if session_path.exists():
            try:
                with open(session_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if not isinstance(data, dict):
                    raise ExecutionStoreCorruptionError(
                        "execution history root must be an object"
                    )
                normalized = _normalize_loaded_history(data)
                if _mark_interrupted_turns(normalized):
                    return self._save_session(session_id, normalized)
                self._cache[session_id] = normalized
                return normalized
            except (json.JSONDecodeError, OSError, ExecutionStoreCorruptionError) as exc:
                suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
                quarantine_path = session_path.with_name(
                    "execution.json.corrupt.{}.{}".format(suffix, uuid4().hex)
                )
                try:
                    os.replace(session_path, quarantine_path)
                    directory_fd = os.open(str(session_path.parent), os.O_RDONLY)
                    try:
                        os.fsync(directory_fd)
                    finally:
                        os.close(directory_fd)
                finally:
                    self._corrupt_sessions[session_id] = str(quarantine_path)
                raise ExecutionStoreCorruptionError(
                    "execution history is corrupt and was isolated"
                ) from exc

        # Create new session history
        now = datetime.now().isoformat()
        history: SessionExecutionHistory = {
            "session_id": session_id,
            "created_at": now,
            "updated_at": now,
            "turns": [],
            "session_history": [],  # LLM conversation context
            "read_documents": normalize_read_documents_registry(None),
        }
        self._cache[session_id] = history
        return history

    def _get_existing_session_unlocked(self, session_id: str) -> Optional[SessionExecutionHistory]:
        """Return existing session history while caller owns self._lock."""
        if session_id in self._corrupt_sessions:
            raise ExecutionStoreCorruptionError(
                "execution history is isolated as corrupt"
            )
        if session_id in self._cache:
            return self._cache[session_id]

        session_path = self._get_session_path(session_id)
        if not session_path.exists():
            return None
        return self._load_session(session_id)

    def _save_session(
        self,
        session_id: str,
        history: Optional[SessionExecutionHistory] = None,
    ) -> SessionExecutionHistory:
        """Atomically commit one session and publish the cache only after fsync."""

        session_id = validate_session_id(session_id)
        if session_id in self._corrupt_sessions:
            raise ExecutionStoreCorruptionError(
                "execution history is isolated as corrupt"
            )
        source = history if history is not None else self._cache.get(session_id)
        if source is None:
            raise KeyError(session_id)
        committed = copy.deepcopy(source)
        committed["updated_at"] = datetime.now().isoformat()
        session_path = self._get_session_path(session_id)
        temp_path = session_path.with_name(
            ".execution.json.{}.tmp".format(uuid4().hex)
        )
        try:
            with open(temp_path, "x", encoding="utf-8") as f:
                json.dump(committed, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, session_path)
            directory_fd = os.open(str(session_path.parent), os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except BaseException:
            self._cache.pop(session_id, None)
            raise
        finally:
            try:
                temp_path.unlink()
            except FileNotFoundError:
                pass
        self._cache[session_id] = committed
        return committed

    # =========================================================================
    # Turn Management
    # =========================================================================

    def lookup_idempotent_turn(
        self,
        *,
        session_id: str,
        idempotency_key_hash: Optional[str],
        request_fingerprint: Optional[str],
    ) -> Optional[TurnExecutionRecord]:
        """Return an exact durable admission retry without mutating state."""

        if not str(idempotency_key_hash or "").strip():
            return None
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if history is None:
                return None
            existing = _turn_for_idempotency_key(
                history,
                idempotency_key_hash,
            )
            if existing is None:
                return None
            if str(existing.get("request_fingerprint") or "") != str(
                request_fingerprint or ""
            ):
                raise IdempotencyConflictError(
                    "idempotency key was already used for another request"
                )
            return copy.deepcopy(existing)

    def lookup_consumed_interaction_turn(
        self,
        *,
        session_id: str,
        interaction_id: str,
        interaction_response: Dict[str, Any],
    ) -> Optional[TurnExecutionRecord]:
        """Return the Run created by an exact repeated HITL answer.

        This read-only check lets admission retries win before session-busy and
        capacity checks without consuming a previously unanswered checkpoint.
        """

        requested_id = str(interaction_id or "").strip()
        if not requested_id:
            raise InteractionCheckpointError(
                "interaction_id is required",
                code="INTERACTION_CONFLICT",
            )
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if history is None:
                raise InteractionCheckpointError(
                    "session has no matching interaction",
                    code="INTERACTION_NOT_FOUND",
                )
            source_turn, interaction = _turn_for_interaction(
                history,
                requested_id,
            )
            if source_turn is None or interaction is None:
                raise InteractionCheckpointError(
                    "session has no matching interaction_id",
                    code="INTERACTION_NOT_FOUND",
                )
            canonical_response = _canonical_response_for_interaction(
                interaction,
                dict(interaction_response or {}),
            )
            if not source_turn.get("interaction_consumed_at"):
                return None
            consumed_by_turn_id = source_turn.get(
                "interaction_consumed_by_turn_id"
            )
            existing_turn = next(
                (
                    turn
                    for turn in history.get("turns", [])
                    if turn.get("turn_id") == consumed_by_turn_id
                ),
                None,
            )
            existing_run_id = (
                str(existing_turn.get("run_id") or "") or None
                if existing_turn is not None
                else None
            )
            if source_turn.get("interaction_response") == canonical_response:
                if existing_turn is not None:
                    return copy.deepcopy(existing_turn)
                raise InteractionCheckpointError(
                    "interaction was cancelled and created no run",
                    code="INTERACTION_ALREADY_CONSUMED",
                )
            raise InteractionCheckpointError(
                "interaction was already consumed by another response",
                code="INTERACTION_ALREADY_CONSUMED",
                existing_run_id=existing_run_id,
            )

    def create_turn_for_run(
        self,
        *,
        session_id: str,
        user_request: str,
        idempotency_key_hash: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
        ontology_version: Optional[str] = None,
        protocol_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[TurnExecutionRecord, bool]:
        """Atomically admit one ordinary request and allocate its Turn and Run."""

        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            existing = _turn_for_idempotency_key(history, idempotency_key_hash)
            if existing is not None:
                if str(existing.get("request_fingerprint") or "") != str(
                    request_fingerprint or ""
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already used for another request"
                    )
                return copy.deepcopy(existing), False

            pending_turn, _pending = _latest_pending_interaction(history)
            if pending_turn is not None:
                raise InteractionCheckpointError(
                    "session is awaiting an interaction response",
                    code="INTERACTION_PENDING",
                )

            turn_id = _next_turn_id(history)
            run_id = "agentic-{}".format(uuid4().hex)
            started_at = datetime.now().isoformat()
            turn: TurnExecutionRecord = {
                "turn_id": turn_id,
                "session_id": session_id,
                "run_id": run_id,
                "user_request": str(user_request or ""),
                "start_time": started_at,
                "end_time": None,
                "status": "running",
                "final_output": None,
                "summary": None,
                "ontology_version": ontology_version,
            }
            if idempotency_key_hash:
                turn["idempotency_key_hash"] = str(idempotency_key_hash)
                turn["request_fingerprint"] = str(request_fingerprint or "")
            if protocol_metadata:
                turn["protocol_metadata"] = copy.deepcopy(protocol_metadata)
            history["turns"].append(turn)
            history.setdefault("session_history", []).append(
                {
                    "role": "user",
                    "content": str(user_request or ""),
                    "run_id": run_id,
                    "turn_id": turn_id,
                }
            )
            committed = self._save_session(session_id, history)
            self._current_turns[session_id] = turn_id
            return copy.deepcopy(committed["turns"][-1]), True

    def consume_interaction_and_create_turn(
        self,
        *,
        session_id: str,
        interaction_id: str,
        user_request: str,
        interaction_response: Dict[str, Any],
        idempotency_key_hash: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
        ontology_version: Optional[str] = None,
        protocol_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[TurnExecutionRecord, bool]:
        """Consume one durable HITL checkpoint and create exactly one new Run."""

        requested_id = str(interaction_id or "").strip()
        if not requested_id:
            raise InteractionCheckpointError(
                "interaction_id is required",
                code="INTERACTION_CONFLICT",
            )
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            existing_by_key = _turn_for_idempotency_key(
                history,
                idempotency_key_hash,
            )
            if existing_by_key is not None:
                if str(existing_by_key.get("request_fingerprint") or "") != str(
                    request_fingerprint or ""
                ):
                    raise IdempotencyConflictError(
                        "idempotency key was already used for another request"
                    )
                return copy.deepcopy(existing_by_key), False

            source_turn, interaction = _turn_for_interaction(history, requested_id)
            if source_turn is None or interaction is None:
                raise InteractionCheckpointError(
                    "session has no matching interaction_id",
                    code="INTERACTION_NOT_FOUND",
                )
            response = dict(interaction_response or {})
            if str(response.get("interaction_id") or "").strip() != requested_id:
                raise InteractionCheckpointError(
                    "interaction_id does not match the pending interaction",
                    code="INTERACTION_CONFLICT",
                )
            canonical_response = _canonical_response_for_interaction(
                interaction,
                response,
            )

            consumed_at = source_turn.get("interaction_consumed_at")
            if consumed_at:
                existing_run_id = None
                consumed_by_turn_id = source_turn.get(
                    "interaction_consumed_by_turn_id"
                )
                existing_turn = next(
                    (
                        turn
                        for turn in history.get("turns", [])
                        if turn.get("turn_id") == consumed_by_turn_id
                    ),
                    None,
                )
                if existing_turn is not None:
                    existing_run_id = str(existing_turn.get("run_id") or "") or None
                if source_turn.get("interaction_response") == canonical_response:
                    if existing_turn is None:
                        raise InteractionCheckpointError(
                            "interaction was cancelled and created no run",
                            code="INTERACTION_ALREADY_CONSUMED",
                        )
                    return copy.deepcopy(existing_turn), False
                raise InteractionCheckpointError(
                    "interaction was already consumed by another response",
                    code="INTERACTION_ALREADY_CONSUMED",
                    existing_run_id=existing_run_id,
                )

            if source_turn.get("status") != "awaiting_input":
                raise InteractionCheckpointError(
                    "interaction checkpoint is not awaiting input",
                    code="INTERACTION_CONFLICT",
                )

            turn_id = _next_turn_id(history)
            run_id = "agentic-{}".format(uuid4().hex)
            started_at = datetime.now().isoformat()
            turn: TurnExecutionRecord = {
                "turn_id": turn_id,
                "session_id": session_id,
                "run_id": run_id,
                "user_request": str(user_request or ""),
                "start_time": started_at,
                "end_time": None,
                "status": "running",
                "final_output": None,
                "summary": None,
                "ontology_version": ontology_version,
            }
            if idempotency_key_hash:
                turn["idempotency_key_hash"] = str(idempotency_key_hash)
                turn["request_fingerprint"] = str(request_fingerprint or "")
            if protocol_metadata:
                turn["protocol_metadata"] = copy.deepcopy(protocol_metadata)
            source_turn["interaction_response"] = canonical_response
            source_turn["interaction_consumed_at"] = started_at
            source_turn["interaction_consumed_by_turn_id"] = turn_id
            history["turns"].append(turn)
            history.setdefault("session_history", []).append(
                {
                    "role": "user",
                    "content": str(user_request or ""),
                    "interaction_response": copy.deepcopy(canonical_response),
                    "run_id": run_id,
                    "turn_id": turn_id,
                }
            )
            committed = self._save_session(session_id, history)
            self._current_turns[session_id] = turn_id
            return copy.deepcopy(committed["turns"][-1]), True

    def start_turn(
        self,
        session_id: str,
        turn_id: int,
        run_id: str,
        user_request: str,
        ontology_version: Optional[str] = None,
        interaction_response: Optional[Dict[str, Any]] = None,
    ) -> TurnExecutionRecord:
        """
        Initialize a new turn record.

        Args:
            session_id: The session ID
            turn_id: The turn number within the session
            run_id: The unique run ID for this execution
            user_request: The user's request text
            ontology_version: The ontology active_version active when the turn
                started. Stamped per-turn so skill-evolution can verify the
                session's trace matches the current active version.

        Returns:
            The newly created TurnExecutionRecord
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            expected_turn_id = _next_turn_id(history)
            if int(turn_id) != expected_turn_id:
                raise ValueError(
                    "turn_id must be allocated monotonically by ExecutionStore"
                )
            canonical_response = _validated_interaction_response(history, interaction_response)
            started_at = datetime.now().isoformat()
            if canonical_response is not None:
                pending_turn, _pending = _latest_pending_interaction(history)
                if pending_turn is None:
                    raise InteractionCheckpointError(
                        "session has no pending interaction",
                        code="INTERACTION_NOT_FOUND",
                    )
                pending_turn["interaction_response"] = canonical_response
                pending_turn["interaction_consumed_at"] = started_at
                pending_turn["interaction_consumed_by_turn_id"] = turn_id

            # Create new turn record
            turn: TurnExecutionRecord = {
                "turn_id": turn_id,
                "session_id": session_id,
                "run_id": run_id,
                "user_request": user_request,
                "start_time": started_at,
                "end_time": None,
                "status": "running",
                "final_output": None,
                "summary": None,
                "ontology_version": ontology_version,
            }
            history["turns"].append(turn)
            self._current_turns[session_id] = turn_id
            committed = self._save_session(session_id, history)

            return copy.deepcopy(committed["turns"][-1])

    def validate_interaction_response(
        self,
        session_id: str,
        response: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Validate a response against the latest durable checkpoint without consuming it."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            return _validated_interaction_response(history, response)

    def cancel_pending_interaction(
        self,
        session_id: str,
        interaction_id: str,
    ) -> Dict[str, Any]:
        """Cancel a durable interaction for one-time injection into the next user turn."""
        requested_id = str(interaction_id or "").strip()
        if not requested_id:
            raise InteractionCheckpointError("interaction_id is required")

        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))

            source_turn, interaction = _turn_for_interaction(
                history,
                requested_id,
            )
            if source_turn is None or interaction is None:
                raise InteractionCheckpointError(
                    "session has no matching interaction_id",
                    code="INTERACTION_NOT_FOUND",
                )
            existing_response = source_turn.get("interaction_response")
            if source_turn.get("interaction_consumed_at"):
                if (
                    isinstance(existing_response, dict)
                    and existing_response.get("action") == "cancel"
                ):
                    return copy.deepcopy(existing_response)
                consumed_by_turn_id = source_turn.get(
                    "interaction_consumed_by_turn_id"
                )
                existing_turn = next(
                    (
                        turn
                        for turn in history.get("turns", [])
                        if turn.get("turn_id") == consumed_by_turn_id
                    ),
                    None,
                )
                raise InteractionCheckpointError(
                    "interaction was already consumed by an answer",
                    code="INTERACTION_ALREADY_CONSUMED",
                    existing_run_id=(
                        str(existing_turn.get("run_id") or "") or None
                        if existing_turn is not None
                        else None
                    ),
                )
            if source_turn.get("status") != "awaiting_input":
                raise InteractionCheckpointError(
                    "interaction checkpoint is not awaiting input",
                    code="INTERACTION_CONFLICT",
                )

            cancelled_at = datetime.now().isoformat()
            cancellation = {
                "interaction_id": requested_id,
                "type": str(interaction.get("type") or "input").strip() or "input",
                "action": "cancel",
                "cancelled": True,
                "cancelled_by": "user",
                "cancelled_at": cancelled_at,
                "message": INTERACTION_CANCELLED_MESSAGE,
                "display_message": INTERACTION_CANCELLED_NOTICE,
                "run_id": source_turn.get("run_id"),
                "turn_id": source_turn.get("turn_id"),
            }

            source_turn["interaction_response"] = cancellation
            source_turn["interaction_consumed_at"] = cancelled_at
            source_turn["interaction_cancelled_at"] = cancelled_at
            source_turn.pop("interaction_consumed_by_turn_id", None)
            self._save_session(session_id, history)
            return cancellation

    def complete_turn(
        self,
        session_id: str,
        turn_id: int,
        final_output: Optional[Dict[str, Any]],
        summary: Optional[str],
    ) -> None:
        """
        Finalize a turn with output and summary.

        Args:
            session_id: The session ID
            turn_id: The turn number
            final_output: The final output from the workflow
            summary: Summary for history context
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))

            # Find the turn
            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "success"
                    turn["final_output"] = final_output
                    turn["summary"] = summary
                    break

            self._save_session(session_id, history)

    def fail_turn(
        self,
        session_id: str,
        turn_id: int,
        error: str,
    ) -> None:
        """
        Mark a turn as failed.

        Args:
            session_id: The session ID
            turn_id: The turn number
            error: The error message
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))

            # Find the turn
            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "failed"
                    turn["error_code"] = "RUNTIME_FAILED"
                    # Add error to final_output
                    turn["final_output"] = {"error": error}
                    break

            self._save_session(session_id, history)

    def cancel_turn(
        self,
        session_id: str,
        turn_id: int,
        reason: str,
        final_output: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> None:
        """
        Mark a turn as cancelled.

        Args:
            session_id: The session ID
            turn_id: The turn number
            reason: Cancellation reason (e.g., user_stop, disconnect)
            final_output: Optional partial output snapshot
            summary: Optional turn summary
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))

            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "cancelled"
                    turn["final_output"] = final_output or {"cancel_reason": reason}
                    if summary is not None:
                        turn["summary"] = summary
                    break

            self._save_session(session_id, history)

    def await_turn(
        self,
        session_id: str,
        turn_id: int,
        final_output: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
    ) -> None:
        """
        Mark a turn as waiting for user input.

        The turn has stopped streaming, but the business workflow can continue
        in a later turn once the user submits the requested input or decision.
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))

            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "awaiting_input"
                    turn["final_output"] = final_output or {"status": "awaiting_input"}
                    output = turn["final_output"]
                    interaction = (
                        output.get("interaction")
                        if isinstance(output, dict)
                        else None
                    )
                    if not isinstance(interaction, dict) and isinstance(output, dict):
                        interaction = output.get("pending_user_interaction")
                    if not isinstance(interaction, dict):
                        raise InteractionCheckpointError(
                            "pending interaction checkpoint is invalid",
                            code="INTERACTION_CONFLICT",
                        )
                    turn["pending_interaction"] = copy.deepcopy(interaction)
                    if summary is not None:
                        turn["summary"] = summary
                    break

            self._save_session(session_id, history)

    def set_turn_feedback(
        self,
        session_id: str,
        turn_id: int,
        vote: str,
    ) -> bool:
        """Persist turn feedback independently from evolution proposal state."""

        normalized = str(vote or "").strip().lower()
        if normalized not in {"up", "down", "none"}:
            raise ValueError("vote must be one of: down, none, up")
        with self._lock_for_session(session_id):
            existing = self._get_existing_session_unlocked(session_id)
            history = copy.deepcopy(existing) if existing is not None else None
            if not history:
                return False
            for turn in history.get("turns", []):
                if turn.get("turn_id") != turn_id:
                    continue
                if normalized == "none":
                    turn.pop("feedback", None)
                else:
                    turn["feedback"] = {
                        "vote": normalized,
                        "ts": datetime.now().isoformat(),
                    }
                self._save_session(session_id, history)
                return True
            return False

    def set_turn_evolution_records(
        self,
        session_id: str,
        turn_id: int,
        records: List[Dict[str, Any]],
    ) -> bool:
        """Persist recall anchors on the authoritative Turn record."""

        normalized = [
            copy.deepcopy(record)
            for record in records
            if isinstance(record, dict)
        ]
        with self._lock_for_session(session_id):
            existing = self._get_existing_session_unlocked(session_id)
            history = copy.deepcopy(existing) if existing is not None else None
            if not history:
                return False
            for turn in history.get("turns", []):
                if turn.get("turn_id") != turn_id:
                    continue
                if normalized:
                    turn["evolution_recall_records"] = normalized
                else:
                    turn.pop("evolution_recall_records", None)
                self._save_session(session_id, history)
                return True
            return False

    def set_turn_evolution_feedback_vote(
        self,
        session_id: str,
        turn_id: int,
        vote: str,
    ) -> bool:
        """Record the last applied memory vote without a second Run store."""

        normalized = str(vote or "").strip().lower()
        if normalized not in {"up", "down", "none"}:
            raise ValueError("vote must be one of: down, none, up")
        with self._lock_for_session(session_id):
            existing = self._get_existing_session_unlocked(session_id)
            history = copy.deepcopy(existing) if existing is not None else None
            if not history:
                return False
            for turn in history.get("turns", []):
                if turn.get("turn_id") != turn_id:
                    continue
                if normalized == "none":
                    turn.pop("evolution_feedback_vote", None)
                else:
                    turn["evolution_feedback_vote"] = normalized
                self._save_session(session_id, history)
                return True
            return False

    def stamp_turn_ontology_version(
        self,
        session_id: str,
        turn_id: int,
        ontology_version: Optional[str],
    ) -> None:
        """
        Update a turn's ontology_version stamp after the run completes.

        Used for tool-based sessions (no session-level selected ontology): the
        start_turn stamp is None, so after the run we infer the ontology the
        agent actually used (from read_documents) and stamp its active_version
        here. This lets the skill-evolution version-alignment gate cover
        tool-based sessions too.
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            for turn in history.get("turns", []):
                if turn.get("turn_id") == turn_id:
                    turn["ontology_version"] = ontology_version
                    break
            self._save_session(session_id, history)

    # =========================================================================
    # Retrieval
    # =========================================================================

    def get_turn_by_run_id(
        self,
        session_id: str,
        run_id: str,
    ) -> Optional[TurnExecutionRecord]:
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if history is None:
                return None
            for turn in history.get("turns", []):
                if str(turn.get("run_id") or "") == str(run_id or ""):
                    return copy.deepcopy(turn)
            return None

    def find_turn_by_run_id(
        self,
        run_id: str,
    ) -> tuple[Optional[str], Optional[TurnExecutionRecord]]:
        """Find one durable Run without rebuilding any live runtime state."""

        session_ids = set(self._cache)
        if self.storage_dir.exists():
            session_ids.update(
                path.name
                for path in self.storage_dir.iterdir()
                if path.is_dir()
            )
        for session_id in sorted(session_ids):
            try:
                turn = self.get_turn_by_run_id(session_id, run_id)
            except ValueError:
                continue
            if turn is not None:
                return session_id, turn
        return None, None

    def find_turn_by_protocol_id(
        self,
        protocol: str,
        external_run_id: str,
    ) -> tuple[Optional[str], Optional[TurnExecutionRecord]]:
        """Resolve a protocol identifier from durable Turn metadata."""

        normalized_protocol = str(protocol or "").strip().lower()
        normalized_external_id = str(external_run_id or "").strip()
        if not normalized_protocol or not normalized_external_id:
            return None, None
        session_ids = set(self._cache)
        if self.storage_dir.exists():
            session_ids.update(
                path.name
                for path in self.storage_dir.iterdir()
                if path.is_dir()
            )
        for session_id in sorted(session_ids):
            try:
                history = self.get_session_history(session_id)
            except ValueError:
                continue
            if history is None:
                continue
            for turn in reversed(history.get("turns", [])):
                metadata = turn.get("protocol_metadata")
                if not isinstance(metadata, dict):
                    continue
                if str(metadata.get("protocol") or "").strip().lower() != normalized_protocol:
                    continue
                candidate = str(
                    metadata.get("external_run_id")
                    or metadata.get("external_id")
                    or ""
                ).strip()
                if candidate == normalized_external_id:
                    return session_id, copy.deepcopy(turn)
        return None, None

    def list_turns_by_protocol(
        self,
        protocol: str,
    ) -> List[TurnExecutionRecord]:
        """List the newest durable Turn for each external protocol Run."""

        normalized_protocol = str(protocol or "").strip().lower()
        if not normalized_protocol:
            return []
        latest: Dict[str, TurnExecutionRecord] = {}
        session_ids = set(self._cache)
        if self.storage_dir.exists():
            session_ids.update(
                path.name
                for path in self.storage_dir.iterdir()
                if path.is_dir()
            )
        for session_id in sorted(session_ids):
            try:
                history = self.get_session_history(session_id)
            except ValueError:
                continue
            if history is None:
                continue
            for turn in history.get("turns", []):
                metadata = turn.get("protocol_metadata")
                if not isinstance(metadata, dict):
                    continue
                if str(metadata.get("protocol") or "").strip().lower() != normalized_protocol:
                    continue
                external_id = str(
                    metadata.get("external_run_id")
                    or metadata.get("external_id")
                    or ""
                ).strip()
                if not external_id:
                    continue
                current = latest.get(external_id)
                if current is None or str(turn.get("start_time") or "") > str(
                    current.get("start_time") or ""
                ):
                    latest[external_id] = copy.deepcopy(turn)
        return sorted(
            latest.values(),
            key=lambda turn: str(turn.get("start_time") or ""),
            reverse=True,
        )

    def pending_interaction(
        self,
        session_id: str,
    ) -> tuple[Optional[TurnExecutionRecord], Optional[Dict[str, Any]]]:
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if history is None:
                return None, None
            turn, interaction = _latest_pending_interaction(history)
            return copy.deepcopy(turn), copy.deepcopy(interaction)

    def mark_turn_task_start_failed(
        self,
        session_id: str,
        turn_id: int,
    ) -> None:
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            for turn in history.get("turns", []):
                if turn.get("turn_id") != turn_id:
                    continue
                turn["status"] = "failed"
                turn["end_time"] = datetime.now().isoformat()
                turn["error_code"] = "TASK_START_FAILED"
                turn["final_output"] = {
                    "error": "Execution task could not be started",
                    "error_code": "TASK_START_FAILED",
                }
                self._save_session(session_id, history)
                return
            raise KeyError(turn_id)

    def reconcile_interrupted_turns(self, session_id: str) -> int:
        """Mark legacy inflight Turns interrupted without recreating execution."""

        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            interrupted = _mark_interrupted_turns(history)
            if interrupted:
                self._save_session(session_id, history)
            return interrupted

    def get_session_history(
        self,
        session_id: str,
    ) -> Optional[SessionExecutionHistory]:
        """
        Get complete session execution history.

        Args:
            session_id: The session ID

        Returns:
            SessionExecutionHistory or None if not found
        """
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            return copy.deepcopy(history) if history is not None else None

    def get_turn(
        self,
        session_id: str,
        turn_id: int,
    ) -> Optional[TurnExecutionRecord]:
        """
        Get a specific turn's execution record.

        Args:
            session_id: The session ID
            turn_id: The turn number

        Returns:
            TurnExecutionRecord or None if not found
        """
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if not history:
                return None

            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    return copy.deepcopy(turn)

            return None

    def get_recent_turns(
        self,
        session_id: str,
        limit: int = 10,
    ) -> List[TurnExecutionRecord]:
        """
        Get recent turns for a session.

        Args:
            session_id: The session ID
            limit: Maximum number of turns to return

        Returns:
            List of recent TurnExecutionRecords (most recent first)
        """
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if not history:
                return []

            # Return most recent turns
            turns = history["turns"]
            return copy.deepcopy(list(reversed(turns[-limit:])))

    def list_turns_by_status(
        self,
        session_id: str,
        status: Optional[str] = None,
    ) -> List[TurnExecutionRecord]:
        """List structured turns newest first, optionally filtered by status."""

        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if not history:
                return []
            turns = list(history.get("turns", []))
            normalized_status = str(status or "").strip()
            if normalized_status:
                turns = [
                    turn
                    for turn in turns
                    if str(turn.get("status") or "") == normalized_status
                ]
            return copy.deepcopy(list(reversed(turns)))

    def delete_session(self, session_id: str) -> bool:
        """
        Delete all execution records for a session.

        Args:
            session_id: The session ID

        Returns:
            True if deleted, False if not found
        """
        with self._lock_for_session(session_id):
            # Remove from cache
            if session_id in self._cache:
                del self._cache[session_id]
            if session_id in self._current_turns:
                del self._current_turns[session_id]

            # Remove from disk
            session_dir = resolve_session_storage_dir(self.storage_dir, session_id)
            if session_dir.exists():
                import shutil
                shutil.rmtree(session_dir)
                return True

            return False

    # =========================================================================
    # Turn Count
    # =========================================================================

    def get_turn_count(self, session_id: str) -> int:
        """Return the number of turns recorded for a session."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            return len(history.get("turns", []))

    # =========================================================================
    # LLM Context Management (session_history)
    # =========================================================================

    def get_llm_context(self, session_id: str) -> List[Dict[str, Any]]:
        """
        Get the LLM conversation context for a session.

        This is the session_history used for multi-turn conversation memory,
        injected into workflow state for LLM context.

        Args:
            session_id: The session ID

        Returns:
            List of conversation history entries for LLM context
        """
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            return copy.deepcopy(history.get("session_history", []))

    def set_llm_context(
        self,
        session_id: str,
        session_history: List[Dict[str, Any]],
    ) -> bool:
        """
        Set the LLM conversation context for a session.

        Called after workflow completion to persist updated conversation history.

        Args:
            session_id: The session ID
            session_history: Updated conversation history for LLM context

        Returns:
            True if successful
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            history["session_history"] = copy.deepcopy(session_history)
            self._save_session(session_id, history)
            return True

    def get_read_documents_context(self, session_id: str) -> Dict[str, Any]:
        """
        Get the structured read-document ledger for a session.

        This ledger is kept separately from LLM messages so tool guards can
        validate document prerequisites across turns without parsing tool text.
        """
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            return copy.deepcopy(
                normalize_read_documents_registry(history.get("read_documents"))
            )

    def set_read_documents_context(
        self,
        session_id: str,
        read_documents: Optional[Dict[str, Any]],
    ) -> bool:
        """
        Persist the structured read-document ledger for future turns.
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            history["read_documents"] = normalize_read_documents_registry(read_documents)
            self._save_session(session_id, history)
            return True

    def append_to_llm_context(
        self,
        session_id: str,
        entry: Dict[str, Any],
    ) -> bool:
        """
        Append a single entry to the LLM conversation context.

        Args:
            session_id: The session ID
            entry: A conversation history entry (e.g., {"role": "user", "content": "..."})

        Returns:
            True if successful
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            if "session_history" not in history:
                history["session_history"] = []
            history["session_history"].append(copy.deepcopy(entry))
            self._save_session(session_id, history)
            return True
