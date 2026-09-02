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
from contextlib import contextmanager
import json
import logging
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from uuid import uuid4

from agent.state import (
    SessionExecutionHistory,
    TurnExecutionRecord,
)
from persistence.session_summary_index import SessionSummaryIndex
from utils.identifiers import resolve_session_storage_dir, validate_session_id
from utils.read_docs import normalize_read_documents_registry


logger = logging.getLogger(__name__)


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


class TurnOwnershipError(RuntimeError):
    """Raised when a run attempts to update another run's turn."""


class SolverCheckpointError(ValueError):
    """Raised when a durable Solver continuation checkpoint conflicts."""


class _ProtocolIdCoordinator:
    def __init__(self) -> None:
        self.lock = threading.RLock()
        self.reservations: Dict[tuple[str, str], str] = {}
        # These process-wide reservations coordinate the asynchronous admission
        # path with synchronous derived-work pruning.  The storage directory is
        # the coordinator key, so separate user/workspace stores share state.
        self.session_admissions: Dict[str, str] = {}
        self.session_prunes: set[str] = set()


_PROTOCOL_ID_COORDINATORS_LOCK = threading.Lock()
_PROTOCOL_ID_COORDINATORS: Dict[str, _ProtocolIdCoordinator] = {}


def _protocol_id_coordinator(storage_dir: Path) -> _ProtocolIdCoordinator:
    storage_key = os.path.normcase(str(storage_dir.resolve()))
    with _PROTOCOL_ID_COORDINATORS_LOCK:
        coordinator = _PROTOCOL_ID_COORDINATORS.get(storage_key)
        if coordinator is None:
            coordinator = _ProtocolIdCoordinator()
            _PROTOCOL_ID_COORDINATORS[storage_key] = coordinator
        return coordinator


_SOLVER_CHECKPOINT_STATE_FIELDS = frozenset(
    {
        "run_id",
        "session_id",
        "current_turn_id",
        "workspace_id",
        "user_id",
        "user_request",
        "user_context",
        "user_lang",
        "messages",
        "read_documents",
        "results",
        "completed_ids",
        "failed_ids",
        "errors",
        "evolution_recall_records",
        "debug",
        "final_output",
    }
)
_SOLVER_CHECKPOINT_USER_CONTEXT_FIELDS = frozenset(
    {
        "agent_mode",
        "lang",
        "llm_request_mode",
        "model_profile_id",
        "model_profile_ref",
        "selected_ontology_id",
        "selected_ontology_version",
        "selected_version",
    }
)


def _json_clone(value: Any, label: str) -> Any:
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False))
    except (TypeError, ValueError) as exc:
        raise SolverCheckpointError("{} must be JSON serializable".format(label)) from exc


def _solver_checkpoint_payload(checkpoint: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(checkpoint, dict):
        raise SolverCheckpointError("solver checkpoint must be an object")
    if set(checkpoint) != {"schema_version", "stage", "tool_call", "state"}:
        raise SolverCheckpointError("solver checkpoint fields are invalid")
    if checkpoint.get("schema_version") != "agent.solver-checkpoint.v1":
        raise SolverCheckpointError("solver checkpoint schema is unsupported")
    if checkpoint.get("stage") not in {"waiting_solver", "react"}:
        raise SolverCheckpointError("solver checkpoint stage is invalid")
    tool_call = checkpoint.get("tool_call")
    if not isinstance(tool_call, dict) or set(tool_call) != {"id", "name", "arguments"}:
        raise SolverCheckpointError("solver checkpoint ToolCall is invalid")
    if not str(tool_call.get("id") or "").strip() or not str(tool_call.get("name") or "").strip():
        raise SolverCheckpointError("solver checkpoint ToolCall identity is missing")
    if not isinstance(tool_call.get("arguments"), dict):
        raise SolverCheckpointError("solver checkpoint ToolCall arguments are invalid")
    state = checkpoint.get("state")
    if not isinstance(state, dict) or set(state) - _SOLVER_CHECKPOINT_STATE_FIELDS:
        raise SolverCheckpointError("solver checkpoint state fields are invalid")
    filtered_state = dict(state)
    user_context = filtered_state.get("user_context")
    if isinstance(user_context, dict):
        filtered_state["user_context"] = {
            key: value
            for key, value in user_context.items()
            if key in _SOLVER_CHECKPOINT_USER_CONTEXT_FIELDS
        }
    elif user_context is not None:
        raise SolverCheckpointError("solver checkpoint user context is invalid")
    return _json_clone(
        {
            "schema_version": checkpoint["schema_version"],
            "stage": checkpoint["stage"],
            "tool_call": tool_call,
            "state": filtered_state,
        },
        "solver checkpoint",
    )


INTERACTION_CANCELLED_MESSAGE = "我已主动取消上一条交互请求，未提供所请求的信息或选择。"
INTERACTION_CANCELLED_NOTICE = "用户取消了操作"


def _fsync_directory(path: Path) -> None:
    """Durably publish an atomic replacement on supported platforms."""
    if os.name == "nt":  # pragma: no cover - Windows cannot open directories.
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


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


def _turn_interaction(
    turn: TurnExecutionRecord,
) -> Optional[Dict[str, Any]]:
    interaction = turn.get("pending_interaction")
    if isinstance(interaction, dict):
        return interaction
    final_output = turn.get("final_output")
    interaction = (
        final_output.get("interaction")
        if isinstance(final_output, dict)
        else None
    )
    return interaction if isinstance(interaction, dict) else None


def _latest_recoverable_interaction_claim(
    history: SessionExecutionHistory,
) -> tuple[
    Optional[TurnExecutionRecord],
    Optional[TurnExecutionRecord],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
]:
    """Return an interrupted deterministic response claim, if one exists."""

    turns = list(history.get("turns") or [])
    if not turns:
        return None, None, None, None
    claimed_turn = turns[-1]
    claimed_response = claimed_turn.get("prior_interaction_response")
    if claimed_turn.get("status") != "running" or not isinstance(
        claimed_response,
        dict,
    ):
        return None, None, None, None
    source_turn_id = claimed_turn.get("prior_interaction_turn_id")
    source_turn = None
    if source_turn_id is not None:
        for turn in reversed(turns[:-1]):
            if turn.get("turn_id") == source_turn_id:
                source_turn = turn
                break
    interaction = _turn_interaction(source_turn) if source_turn else None
    if source_turn is None or interaction is None:
        return None, None, None, None
    if source_turn.get("status") != "awaiting_input":
        return None, None, None, None
    if source_turn.get("interaction_response") != claimed_response:
        return None, None, None, None
    if not source_turn.get("interaction_consumed_at"):
        return None, None, None, None
    return claimed_turn, source_turn, interaction, claimed_response


def _is_standard_json_value(value: Any) -> bool:
    if value is None or isinstance(value, (str, bool, int)):
        return True
    if isinstance(value, float):
        try:
            json.dumps(value, allow_nan=False)
        except (TypeError, ValueError, OverflowError):
            return False
        return True
    if isinstance(value, list):
        return all(_is_standard_json_value(item) for item in value)
    if isinstance(value, dict):
        return all(
            isinstance(key, str) and _is_standard_json_value(item)
            for key, item in value.items()
        )
    return False


def _validated_interaction_response(
    history: SessionExecutionHistory,
    response: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    _pending_turn, interaction = _latest_pending_interaction(history)
    claimed_response: Optional[Dict[str, Any]] = None
    if interaction is None:
        _claimed_turn, _source_turn, interaction, claimed_response = (
            _latest_recoverable_interaction_claim(history)
        )
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
        if "value" not in response or response.get("value") is None:
            raise InteractionCheckpointError("input interaction requires a JSON value")
        value = response["value"]
        try:
            valid_json_value = _is_standard_json_value(value)
        except RecursionError:
            valid_json_value = False
        if not valid_json_value:
            raise InteractionCheckpointError("input interaction requires a JSON value")
        if isinstance(value, str) and not value.strip():
            raise InteractionCheckpointError("input interaction value must not be empty")
        canonical["value"] = deepcopy(value)
        if claimed_response is not None and canonical != claimed_response:
            raise InteractionCheckpointError(
                "interaction response does not match the in-progress recovery claim"
            )
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
    if claimed_response is not None and canonical != claimed_response:
        raise InteractionCheckpointError(
            "interaction response does not match the in-progress recovery claim"
        )
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

    def __init__(
        self,
        storage_dir: str,
        *,
        summary_index: Optional[SessionSummaryIndex] = None,
    ):
        """
        Initialize the ExecutionStore.

        Args:
            storage_dir: Directory for storing execution records.
        """
        self.storage_dir = Path(storage_dir)
        self.storage_dir.mkdir(parents=True, exist_ok=True)

        # Thread safety — per-session locks for concurrent access
        self._lock = threading.Lock()
        self._session_locks: Dict[str, threading.RLock] = {}
        self._protocol_id_coordinator = _protocol_id_coordinator(self.storage_dir)

        # In-memory cache for active sessions (session_id -> SessionExecutionHistory)
        self._cache: Dict[str, SessionExecutionHistory] = {}

        # Track current turn for each session (session_id -> turn_id)
        self._current_turns: Dict[str, int] = {}
        self._corrupt_sessions: Dict[str, str] = {}
        self._summary_index = summary_index

    @property
    def session_summary_index(self) -> Optional[SessionSummaryIndex]:
        return self._summary_index

    @staticmethod
    def _runtime_fields(history: SessionExecutionHistory) -> Dict[str, Any]:
        turns = list(history.get("turns") or [])
        latest = turns[-1] if turns else None
        if not isinstance(latest, dict):
            return {
                "active_run_id": None,
                "last_run_status": "idle",
                "pending_interaction": None,
                "turn_count": len(turns),
            }

        raw_status = str(latest.get("status") or "idle")
        status = "completed" if raw_status == "success" else raw_status
        if raw_status == "failed" and str(latest.get("error_code") or "") == "SERVICE_RESTARTED":
            status = "interrupted"
        interaction = None
        if raw_status == "awaiting_input" and not latest.get("interaction_consumed_at"):
            interaction = _turn_interaction(latest)
        active_run_id = None
        if raw_status == "running" or interaction is not None:
            active_run_id = str(latest.get("run_id") or "").strip() or None
        return {
            "active_run_id": active_run_id,
            "last_run_status": status or "idle",
            "pending_interaction": copy.deepcopy(interaction),
            "turn_count": len(turns),
        }

    def _project_runtime(
        self,
        session_id: str,
        history: SessionExecutionHistory,
        *,
        projection_token: str | None = None,
    ) -> bool:
        if self._summary_index is None:
            return True
        try:
            projected = self._summary_index.update_runtime(
                session_id,
                **self._runtime_fields(history),
                projection_token=projection_token,
            )
            if not projected:
                raise RuntimeError("chat summary is unavailable for runtime projection")
            if projection_token is not None:
                self._summary_index.complete_source_projection(projection_token)
            return True
        except Exception:
            logger.warning("Session summary execution projection failed for %s", session_id)
            self._abandon_runtime_projection(projection_token)
            try:
                self._summary_index.mark_degraded("execution_projection_failed")
            except Exception:
                pass
            return False

    def _begin_runtime_projection(self) -> str | None:
        if self._summary_index is None:
            return None
        begin = getattr(self._summary_index, "begin_source_projection", None)
        if not callable(begin):
            return None
        try:
            return begin()
        except Exception:
            logger.warning("Session summary execution marker could not be published")
            try:
                self._summary_index.mark_degraded("execution_projection_marker_failed")
            except Exception:
                pass
            return None

    def _abandon_runtime_projection(self, projection_token: str | None) -> None:
        if self._summary_index is None or projection_token is None:
            return
        abandon = getattr(self._summary_index, "abandon_source_projection", None)
        if not callable(abandon):
            return
        try:
            abandon(projection_token)
        except Exception:
            try:
                self._summary_index.mark_degraded(
                    "execution_projection_marker_failed"
                )
            except Exception:
                pass

    def get_session_summary_runtime(self, session_id: str) -> Dict[str, Any]:
        """Return execution-owned fields for an explicit index rebuild."""

        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if history is None:
                return self._runtime_fields({"turns": []})
            return self._runtime_fields(history)

    def try_get_session_summary_runtime(
        self,
        session_id: str,
    ) -> Dict[str, Any] | None:
        """Read runtime fields only when doing so cannot wait on admission."""

        session_lock = self._lock_for_session(session_id)
        if not session_lock.acquire(blocking=False):
            return None
        try:
            if session_id in self._corrupt_sessions:
                raise ExecutionStoreCorruptionError(
                    "execution history is isolated as corrupt"
                )
            history = self._cache.get(session_id)
            if history is None:
                session_path = (
                    resolve_session_storage_dir(self.storage_dir, session_id)
                    / "execution.json"
                )
                try:
                    with session_path.open("r", encoding="utf-8") as source:
                        document = json.load(source)
                except FileNotFoundError:
                    document = None
                if document is not None:
                    if not isinstance(document, dict):
                        raise ExecutionStoreCorruptionError(
                            "execution history root must be an object"
                        )
                    history = _normalize_loaded_history(document)
            if history is None:
                return self._runtime_fields({"turns": []})
            return self._runtime_fields(history)
        finally:
            session_lock.release()

    def reserve_session_admission(self, session_id: str) -> Optional[str]:
        """Reserve a session before request-local run preflight begins.

        The reservation is intentionally process-local, matching the current
        single-worker runtime contract.  It closes the otherwise unprotected
        interval between metadata/preflight reads and durable turn creation.
        """

        normalized = validate_session_id(session_id)
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            if (
                normalized in coordinator.session_admissions
                or normalized in coordinator.session_prunes
            ):
                return None
            token = uuid4().hex
            coordinator.session_admissions[normalized] = token
            return token

    def release_session_admission(
        self,
        session_id: str,
        reservation_id: str,
    ) -> bool:
        """Release one matching request-local session admission reservation."""

        normalized = validate_session_id(session_id)
        token = str(reservation_id or "").strip()
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            if coordinator.session_admissions.get(normalized) != token:
                return False
            coordinator.session_admissions.pop(normalized, None)
            return True

    @contextmanager
    def _consume_session_admission(
        self,
        session_id: str,
        reservation_id: str | None,
    ):
        """Validate and consume a lease around the durable turn commit."""

        token = str(reservation_id or "").strip()
        if not token:
            yield
            return
        normalized = validate_session_id(session_id)
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            if coordinator.session_admissions.get(normalized) != token:
                raise RuntimeError("session admission reservation is unavailable")
            try:
                yield
            finally:
                if coordinator.session_admissions.get(normalized) == token:
                    coordinator.session_admissions.pop(normalized, None)

    @contextmanager
    def session_prune_guard(self, session_id: str):
        """Coordinate one prune candidate with request admission.

        The yielded value is ``False`` when a request already owns the session
        admission lease.  Callers must skip all work in that case.  Once the
        prune marker is installed, a new admission cannot acquire a lease
        until the candidate transaction has released its per-session lock.
        """

        normalized = validate_session_id(session_id)
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            if (
                normalized in coordinator.session_admissions
                or normalized in coordinator.session_prunes
            ):
                yield False
                return
            coordinator.session_prunes.add(normalized)
        session_lock = self._lock_for_session(normalized)
        session_lock.acquire()
        try:
            yield True
        finally:
            session_lock.release()
            with coordinator.lock:
                coordinator.session_prunes.discard(normalized)

    def is_session_runtime_protected(self, session_id: str) -> bool:
        with self._lock_for_session(session_id):
            fields = self.get_session_summary_runtime(session_id)
            status = str(fields.get("last_run_status") or "idle")
            return bool(
                fields.get("active_run_id")
                or fields.get("pending_interaction")
                or status not in {
                    "idle",
                    "completed",
                    "failed",
                    "cancelled",
                    "interrupted",
                }
            )

    @property
    def corruption_count(self) -> int:
        return len(self._corrupt_sessions)

    @property
    def healthy(self) -> bool:
        return not self._corrupt_sessions

    def _lock_for_session(self, session_id: str) -> threading.RLock:
        session_id = validate_session_id(session_id)
        with self._lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.RLock()
            return self._session_locks[session_id]

    def deterministic_terminal_transaction(self, session_id: str) -> threading.RLock:
        """Serialize one cross-store terminal commit or recovery per session."""
        return self._lock_for_session(session_id)

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
                    return self._save_session(
                        session_id,
                        normalized,
                        project_runtime=True,
                    )
                self._cache[session_id] = normalized
                return normalized
            except (json.JSONDecodeError, OSError, ExecutionStoreCorruptionError) as exc:
                suffix = datetime.now().strftime("%Y%m%d%H%M%S%f")
                quarantine_path = session_path.with_name(
                    "execution.json.corrupt.{}.{}".format(suffix, uuid4().hex)
                )
                try:
                    os.replace(session_path, quarantine_path)
                    _fsync_directory(session_path.parent)
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

        session_path = (
            resolve_session_storage_dir(self.storage_dir, session_id)
            / "execution.json"
        )
        if not session_path.exists():
            return None
        return self._load_session(session_id)

    def _save_session(
        self,
        session_id: str,
        history: Optional[SessionExecutionHistory] = None,
        *,
        project_runtime: bool = False,
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
        try:
            projection_token = (
                self._begin_runtime_projection() if project_runtime else None
            )
        except Exception:
            self._cache.pop(session_id, None)
            raise
        temp_path: Path | None = None
        try:
            committed = copy.deepcopy(source)
            committed["updated_at"] = datetime.now().isoformat()
            session_path = self._get_session_path(session_id)
            temp_path = session_path.with_name(
                ".execution.json.{}.tmp".format(uuid4().hex)
            )
            with open(temp_path, "x", encoding="utf-8") as f:
                json.dump(
                    committed,
                    f,
                    ensure_ascii=False,
                    indent=2,
                    allow_nan=False,
                )
                f.flush()
                os.fsync(f.fileno())
            os.replace(temp_path, session_path)
            _fsync_directory(session_path.parent)
        except BaseException:
            self._abandon_runtime_projection(projection_token)
            self._cache.pop(session_id, None)
            raise
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)
        self._cache[session_id] = committed
        if project_runtime:
            self._project_runtime(
                session_id,
                committed,
                projection_token=projection_token,
            )
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
        run_id: Optional[str] = None,
        idempotency_key_hash: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
        admission_reservation_id: Optional[str] = None,
        ontology_version: Optional[str] = None,
        protocol_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[TurnExecutionRecord, bool]:
        """Atomically admit one ordinary request and allocate its Turn and Run."""

        with self._consume_session_admission(
            session_id,
            admission_reservation_id,
        ), self._lock_for_session(session_id):
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
            allocated_run_id = str(run_id or "").strip() or "agentic-{}".format(
                uuid4().hex
            )
            if any(
                str(item.get("run_id") or "") == allocated_run_id
                for item in history.get("turns", [])
            ):
                raise ValueError("run_id is already allocated")
            started_at = datetime.now().isoformat()
            turn: TurnExecutionRecord = {
                "turn_id": turn_id,
                "session_id": session_id,
                "run_id": allocated_run_id,
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
                    "run_id": allocated_run_id,
                    "turn_id": turn_id,
                }
            )
            committed = self._save_session(
                session_id,
                history,
                project_runtime=True,
            )
            self._current_turns[session_id] = turn_id
            return copy.deepcopy(committed["turns"][-1]), True

    @staticmethod
    def _protocol_run_key(
        protocol: str,
        external_run_id: str,
    ) -> tuple[str, str]:
        normalized_protocol = str(protocol or "").strip().lower()
        normalized_external_id = str(external_run_id or "").strip()
        if not normalized_protocol:
            raise ValueError("protocol must not be empty")
        if not normalized_external_id:
            raise ValueError("external_run_id must not be empty")
        return normalized_protocol, normalized_external_id

    def reserve_protocol_run_id(
        self,
        protocol: str,
        external_run_id: str,
    ) -> Optional[str]:
        """Reserve one protocol identifier before request-scoped preflight."""

        key = self._protocol_run_key(protocol, external_run_id)
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            _session_id, existing = self._find_durable_turn_by_protocol_id(*key)
            if existing is not None or key in coordinator.reservations:
                return None
            reservation_id = uuid4().hex
            coordinator.reservations[key] = reservation_id
            return reservation_id

    def release_protocol_run_id(
        self,
        protocol: str,
        external_run_id: str,
        reservation_id: str,
    ) -> bool:
        """Release a matching in-memory reservation without durable mutation."""

        key = self._protocol_run_key(protocol, external_run_id)
        normalized_reservation = str(reservation_id or "").strip()
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            if coordinator.reservations.get(key) != normalized_reservation:
                return False
            coordinator.reservations.pop(key, None)
            return True

    def create_turn_for_reserved_protocol_run(
        self,
        *,
        protocol: str,
        external_run_id: str,
        reservation_id: str,
        session_id: str,
        user_request: str,
        run_id: Optional[str] = None,
        ontology_version: Optional[str] = None,
        protocol_metadata: Optional[Dict[str, Any]] = None,
    ) -> tuple[TurnExecutionRecord, bool]:
        """Atomically consume a reservation and persist one protocol Turn."""

        key = self._protocol_run_key(protocol, external_run_id)
        normalized_reservation = str(reservation_id or "").strip()
        coordinator = self._protocol_id_coordinator
        with coordinator.lock:
            if coordinator.reservations.get(key) != normalized_reservation:
                raise RuntimeError("protocol run reservation is unavailable")
            try:
                _existing_session_id, existing = (
                    self._find_durable_turn_by_protocol_id(*key)
                )
                if existing is not None:
                    return existing, False
                metadata = dict(protocol_metadata or {})
                metadata["protocol"] = key[0]
                metadata["external_run_id"] = key[1]
                return self.create_turn_for_run(
                    session_id=session_id,
                    user_request=user_request,
                    run_id=run_id,
                    ontology_version=ontology_version,
                    protocol_metadata=metadata,
                )
            finally:
                if coordinator.reservations.get(key) == normalized_reservation:
                    coordinator.reservations.pop(key, None)

    def consume_interaction_and_create_turn(
        self,
        *,
        session_id: str,
        interaction_id: str,
        user_request: str,
        interaction_response: Dict[str, Any],
        idempotency_key_hash: Optional[str] = None,
        request_fingerprint: Optional[str] = None,
        admission_reservation_id: Optional[str] = None,
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
        with self._consume_session_admission(
            session_id,
            admission_reservation_id,
        ), self._lock_for_session(session_id):
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
            committed = self._save_session(
                session_id,
                history,
                project_runtime=True,
            )
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
            turn, _canonical_response = self._start_turn_unlocked(
                history,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                user_request=user_request,
                ontology_version=ontology_version,
                interaction_response=interaction_response,
            )
            return turn

    def start_next_turn(
        self,
        session_id: str,
        run_id: str,
        user_request: str,
        ontology_version: Optional[str] = None,
        interaction_response: Optional[Dict[str, Any]] = None,
    ) -> tuple[TurnExecutionRecord, Optional[Dict[str, Any]]]:
        """Atomically allocate and start the next turn for one session."""
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            turn_id = _next_turn_id(history)
            return self._start_turn_unlocked(
                history,
                session_id=session_id,
                turn_id=turn_id,
                run_id=run_id,
                user_request=user_request,
                ontology_version=ontology_version,
                interaction_response=interaction_response,
            )

    def _start_turn_unlocked(
        self,
        history: SessionExecutionHistory,
        *,
        session_id: str,
        turn_id: int,
        run_id: str,
        user_request: str,
        ontology_version: Optional[str],
        interaction_response: Optional[Dict[str, Any]],
    ) -> tuple[TurnExecutionRecord, Optional[Dict[str, Any]]]:
        expected_turn_id = _next_turn_id(history)
        if int(turn_id) != expected_turn_id:
            raise ValueError(
                "turn_id must be allocated monotonically by ExecutionStore"
            )
        canonical_response = _validated_interaction_response(
            history,
            interaction_response,
        )
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
        committed = self._save_session(
            session_id,
            history,
            project_runtime=True,
        )
        self._current_turns[session_id] = turn_id
        return copy.deepcopy(committed["turns"][-1]), canonical_response

    def validate_interaction_response(
        self,
        session_id: str,
        response: Optional[Dict[str, Any]],
    ) -> Optional[Dict[str, Any]]:
        """Validate a response against the latest durable checkpoint without consuming it."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            return _validated_interaction_response(history, response)

    def get_pending_deterministic_checkpoint(
        self,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return the latest resumable deterministic checkpoint without consuming it."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            for turn in reversed(list(history.get("turns") or [])):
                if str(turn.get("status") or "").strip() != "awaiting_input":
                    continue
                checkpoint = turn.get("deterministic_checkpoint")
                if isinstance(checkpoint, dict):
                    return dict(checkpoint)
                return None
        return None

    def get_pending_deterministic_terminal(
        self,
        session_id: str,
    ) -> Optional[Dict[str, Any]]:
        """Return a crash-recoverable deterministic terminal intent."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            pending = history.get("pending_deterministic_terminal")
            return deepcopy(pending) if isinstance(pending, dict) else None

    def get_pending_deterministic_artifacts(
        self,
        session_id: str,
    ) -> List[Dict[str, Any]]:
        """Return crash-recoverable deterministic artifact transactions."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            pending = history.get("pending_deterministic_artifacts")
            if pending is None:
                return []
            if not isinstance(pending, dict) or pending.get("version") != 1:
                raise RuntimeError("deterministic artifact journal is invalid")
            transactions = pending.get("transactions")
            if not isinstance(transactions, dict) or any(
                not isinstance(transaction, dict)
                for transaction in transactions.values()
            ):
                raise RuntimeError("deterministic artifact journal is invalid")
            return [
                deepcopy(transaction)
                for _run_id, transaction in sorted(transactions.items())
            ]

    def reserve_deterministic_artifact(
        self,
        session_id: str,
        turn_id: int,
        run_id: str,
        output_ref: str,
    ) -> None:
        """Durably reserve one output ref before ChatStore publishes its files."""
        self.reserve_deterministic_artifacts(
            session_id,
            turn_id,
            run_id,
            [output_ref],
        )

    def reserve_deterministic_artifacts(
        self,
        session_id: str,
        turn_id: int,
        run_id: str,
        output_refs: List[str],
    ) -> None:
        """Durably reserve a batch of refs with one journal write and fsync."""
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise ValueError("run_id is required")
        normalized_refs = []
        for output_ref in output_refs:
            normalized_ref = str(output_ref or "").strip().lower()
            if len(normalized_ref) != 32 or any(
                character not in "0123456789abcdef"
                for character in normalized_ref
            ):
                raise ValueError("output_ref is invalid")
            if normalized_ref not in normalized_refs:
                normalized_refs.append(normalized_ref)
        if not normalized_refs:
            return

        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            turn = next(
                (
                    item
                    for item in history.get("turns", [])
                    if item.get("turn_id") == turn_id
                ),
                None,
            )
            if turn is None or str(turn.get("run_id") or "") != normalized_run_id:
                raise RuntimeError("deterministic artifact turn is missing")
            if str(turn.get("status") or "") != "running":
                raise RuntimeError("deterministic artifact turn is not running")

            updated_history = deepcopy(history)
            pending = updated_history.get("pending_deterministic_artifacts")
            if pending is None:
                pending = {"version": 1, "transactions": {}}
                updated_history["pending_deterministic_artifacts"] = pending
            elif not isinstance(pending, dict) or pending.get("version") != 1:
                raise RuntimeError("deterministic artifact journal is invalid")
            transactions = pending.get("transactions")
            if not isinstance(transactions, dict):
                raise RuntimeError("deterministic artifact journal is invalid")
            transaction = transactions.get(normalized_run_id)
            if transaction is None:
                transaction = {
                    "turn_id": turn_id,
                    "run_id": normalized_run_id,
                    "output_refs": [],
                }
                transactions[normalized_run_id] = transaction
            elif not isinstance(transaction, dict):
                raise RuntimeError("deterministic artifact transaction is invalid")
            elif transaction.get("turn_id") != turn_id:
                raise RuntimeError("deterministic artifact transaction is invalid")
            elif str(transaction.get("run_id") or "") != normalized_run_id:
                raise RuntimeError("deterministic artifact transaction is invalid")
            elif not isinstance(transaction.get("output_refs"), list):
                raise RuntimeError("deterministic artifact transaction is invalid")
            for normalized_ref in normalized_refs:
                if normalized_ref not in transaction["output_refs"]:
                    transaction["output_refs"].append(normalized_ref)

            self._cache[session_id] = updated_history
            try:
                self._save_session(session_id)
            except Exception:
                self._cache[session_id] = history
                raise

    def release_deterministic_artifacts(
        self,
        session_id: str,
        run_id: str,
        output_refs: List[str],
    ) -> None:
        """Remove refs already rolled back and discard an empty transaction."""
        normalized_run_id = str(run_id or "").strip()
        released = {
            str(output_ref or "").strip().lower()
            for output_ref in output_refs
            if str(output_ref or "").strip()
        }
        if not normalized_run_id or not released:
            return
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            pending = history.get("pending_deterministic_artifacts")
            transactions = (
                pending.get("transactions") if isinstance(pending, dict) else None
            )
            transaction = (
                transactions.get(normalized_run_id)
                if isinstance(transactions, dict)
                else None
            )
            if not isinstance(transaction, dict):
                return

            updated_history = deepcopy(history)
            updated_pending = updated_history["pending_deterministic_artifacts"]
            updated_transactions = updated_pending["transactions"]
            updated_transaction = updated_transactions[normalized_run_id]
            remaining = [
                output_ref
                for output_ref in updated_transaction.get("output_refs", [])
                if str(output_ref).lower() not in released
            ]
            if remaining:
                updated_transaction["output_refs"] = remaining
            else:
                updated_transactions.pop(normalized_run_id, None)
                if not updated_transactions:
                    updated_history.pop("pending_deterministic_artifacts", None)

            self._cache[session_id] = updated_history
            try:
                self._save_session(session_id)
            except Exception:
                self._cache[session_id] = history
                raise

    def commit_deterministic_artifacts(
        self,
        session_id: str,
        turn_id: int,
        run_id: str,
    ) -> None:
        """Transfer a journaled transaction to one committed successful turn."""
        normalized_run_id = str(run_id or "").strip()
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            pending = history.get("pending_deterministic_artifacts")
            transactions = (
                pending.get("transactions") if isinstance(pending, dict) else None
            )
            transaction = (
                transactions.get(normalized_run_id)
                if isinstance(transactions, dict)
                else None
            )
            if not isinstance(transaction, dict):
                return
            turn = next(
                (
                    item
                    for item in history.get("turns", [])
                    if item.get("turn_id") == turn_id
                ),
                None,
            )
            if turn is None:
                raise RuntimeError("deterministic artifacts have no committed success")
            if str(turn.get("run_id") or "") != normalized_run_id:
                raise RuntimeError("deterministic artifacts have no committed success")
            if str(turn.get("status") or "") != "success":
                raise RuntimeError("deterministic artifacts have no committed success")
            if transaction.get("turn_id") != turn_id:
                raise RuntimeError("deterministic artifacts have no committed success")

            updated_history = deepcopy(history)
            updated_transactions = updated_history[
                "pending_deterministic_artifacts"
            ]["transactions"]
            updated_transactions.pop(normalized_run_id, None)
            if not updated_transactions:
                updated_history.pop("pending_deterministic_artifacts", None)
            self._cache[session_id] = updated_history
            try:
                self._save_session(session_id)
            except Exception:
                self._cache[session_id] = history
                raise

    def prepare_deterministic_terminal(
        self,
        session_id: str,
        turn_id: int,
        *,
        status: str,
        final_output: Dict[str, Any],
        llm_context: List[Dict[str, Any]],
        events: List[Dict[str, Any]],
        summary: Optional[str] = None,
        deterministic_checkpoint: Optional[Dict[str, Any]] = None,
    ) -> None:
        """Persist terminal intent before touching the independent chat log."""
        if status not in {"success", "awaiting_input", "failed", "cancelled"}:
            raise ValueError("unsupported deterministic terminal status")
        if not isinstance(final_output, dict):
            raise ValueError("deterministic terminal final_output must be an object")
        if not isinstance(llm_context, list):
            raise ValueError("deterministic terminal llm_context must be a list")
        event_ids: List[str] = []
        for event in events:
            data = event.get("data") if isinstance(event, dict) else None
            event_id = (
                str(data.get("event_id") or "").strip()
                if isinstance(data, dict)
                else ""
            )
            if not event_id:
                raise ValueError("deterministic terminal events require event_id")
            event_ids.append(event_id)
        if not event_ids or len(set(event_ids)) != len(event_ids):
            raise ValueError("deterministic terminal event_ids must be non-empty and unique")
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            turn = next(
                (
                    item
                    for item in history.get("turns", [])
                    if item.get("turn_id") == turn_id
                ),
                None,
            )
            if turn is None:
                raise RuntimeError("deterministic terminal turn is missing")
            pending = history.get("pending_deterministic_terminal")
            if isinstance(pending, dict) and pending.get("turn_id") != turn_id:
                raise RuntimeError("another deterministic terminal commit is pending")
            updated_history = deepcopy(history)
            updated_history["pending_deterministic_terminal"] = {
                "version": 1,
                "turn_id": turn_id,
                "status": status,
                "completed_at": _utc_now_iso(),
                "final_output": deepcopy(final_output),
                "summary": summary,
                "llm_context": deepcopy(llm_context),
                "events": deepcopy(events),
                "deterministic_checkpoint": (
                    deepcopy(deterministic_checkpoint)
                    if deterministic_checkpoint is not None
                    else None
                ),
            }
            self._cache[session_id] = updated_history
            try:
                self._save_session(session_id)
            except Exception:
                self._cache[session_id] = history
                raise

    def commit_deterministic_terminal(self, session_id: str, turn_id: int) -> None:
        """Atomically finalize the turn/context and clear its terminal intent."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            pending = history.get("pending_deterministic_terminal")
            if not isinstance(pending, dict) or pending.get("turn_id") != turn_id:
                raise RuntimeError("deterministic terminal intent is missing")
            status = str(pending.get("status") or "")
            updated_history = deepcopy(history)
            updated_pending = updated_history["pending_deterministic_terminal"]
            turn = next(
                (
                    item
                    for item in updated_history.get("turns", [])
                    if item.get("turn_id") == turn_id
                ),
                None,
            )
            if turn is None:
                raise RuntimeError("deterministic terminal turn is missing")
            turn["end_time"] = updated_pending.get("completed_at") or _utc_now_iso()
            turn["status"] = status
            turn["final_output"] = deepcopy(updated_pending.get("final_output"))
            turn["summary"] = updated_pending.get("summary")
            checkpoint = updated_pending.get("deterministic_checkpoint")
            if status == "awaiting_input" and isinstance(checkpoint, dict):
                turn["deterministic_checkpoint"] = deepcopy(checkpoint)
            else:
                turn.pop("deterministic_checkpoint", None)
            updated_history["session_history"] = deepcopy(
                updated_pending.get("llm_context") or []
            )
            updated_history.pop("pending_deterministic_terminal", None)
            self._cache[session_id] = updated_history
            try:
                self._save_session(session_id, project_runtime=True)
            except Exception:
                self._cache[session_id] = history
                raise

    def abort_deterministic_terminal(self, session_id: str, turn_id: int) -> None:
        """Discard an intent whose chat persistence failed synchronously."""
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            pending = history.get("pending_deterministic_terminal")
            if not isinstance(pending, dict) or pending.get("turn_id") != turn_id:
                return
            updated_history = deepcopy(history)
            updated_history.pop("pending_deterministic_terminal", None)
            self._cache[session_id] = updated_history
            try:
                self._save_session(session_id)
            except Exception:
                self._cache[session_id] = history
                raise

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
            self._save_session(session_id, history, project_runtime=True)
            return cancellation

    def complete_turn(
        self,
        session_id: str,
        turn_id: int,
        final_output: Optional[Dict[str, Any]],
        summary: Optional[str],
        *,
        run_id: Optional[str] = None,
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

            turn = self._turn_for_update(history, turn_id, run_id)
            if turn is not None and str(turn.get("status") or "") not in {
                "success", "failed", "cancelled", "awaiting_input"
            }:
                turn["end_time"] = datetime.now().isoformat()
                turn["status"] = "success"
                turn["final_output"] = final_output
                turn["summary"] = summary

            self._save_session(session_id, history, project_runtime=True)

    def fail_turn(
        self,
        session_id: str,
        turn_id: int,
        error: Any,
        *,
        run_id: Optional[str] = None,
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

            turn = self._turn_for_update(history, turn_id, run_id)
            if turn is not None and str(turn.get("status") or "") not in {
                "success", "failed", "cancelled", "awaiting_input"
            }:
                turn["end_time"] = datetime.now().isoformat()
                turn["status"] = "failed"
                output = (
                    copy.deepcopy(error)
                    if isinstance(error, dict)
                    else {"error": str(error)}
                )
                nested_error = output.get("error")
                error_code = (
                    nested_error.get("error_code") or nested_error.get("code")
                    if isinstance(nested_error, dict)
                    else output.get("error_code")
                )
                turn["error_code"] = str(error_code or "RUNTIME_FAILED")
                turn["final_output"] = output

            self._save_session(session_id, history, project_runtime=True)

    def cancel_turn(
        self,
        session_id: str,
        turn_id: int,
        reason: str,
        final_output: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
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

            turn = self._turn_for_update(history, turn_id, run_id)
            if turn is not None and str(turn.get("status") or "") not in {
                "success", "failed", "cancelled", "awaiting_input"
            }:
                turn["end_time"] = datetime.now().isoformat()
                turn["status"] = "cancelled"
                turn["final_output"] = final_output or {"cancel_reason": reason}
                if summary is not None:
                    turn["summary"] = summary

            self._save_session(session_id, history, project_runtime=True)

    def reconcile_canonical_terminal(
        self,
        session_id: str,
        turn_id: int,
        *,
        run_id: str,
        status: str,
        final_output: Dict[str, Any],
        error_code: Optional[str] = None,
    ) -> bool:
        """Make the durable Turn match the Manager's canonical terminal event.

        Normal terminal helpers deliberately reject terminal rewrites. This
        method is narrower: it is called only while EventBus serializes the one
        accepted ``turn.finished`` event, after cancellation priority has been
        resolved by AsyncRunManager.
        """

        if status not in {"success", "failed", "cancelled", "awaiting_input"}:
            raise ValueError("unsupported canonical terminal status")
        if not isinstance(final_output, dict):
            raise ValueError("canonical final_output must be an object")

        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            turn = self._turn_for_update(history, turn_id, run_id)
            if turn is None:
                raise TurnOwnershipError("canonical terminal turn is missing")
            existing_error_code = str(turn.get("error_code") or "")
            target_error_code = (
                str(error_code or "RUNTIME_FAILED") if status == "failed" else ""
            )
            if (
                str(turn.get("status") or "") == status
                and turn.get("final_output") == final_output
                and existing_error_code == target_error_code
            ):
                return False

            turn["end_time"] = datetime.now().isoformat()
            turn["status"] = status
            turn["final_output"] = copy.deepcopy(final_output)
            if status == "failed":
                turn["error_code"] = str(error_code or "RUNTIME_FAILED")
            else:
                turn.pop("error_code", None)
            if status == "awaiting_input":
                interaction = final_output.get("interaction")
                if not isinstance(interaction, dict):
                    interaction = final_output.get("pending_user_interaction")
                if not isinstance(interaction, dict):
                    raise InteractionCheckpointError(
                        "pending interaction checkpoint is invalid",
                        code="INTERACTION_CONFLICT",
                    )
                turn["pending_interaction"] = copy.deepcopy(interaction)
            else:
                turn.pop("pending_interaction", None)
                turn.pop("deterministic_checkpoint", None)
            if status != "success":
                turn["summary"] = None

            self._save_session(session_id, history, project_runtime=True)
            return True

    def await_turn(
        self,
        session_id: str,
        turn_id: int,
        final_output: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        *,
        run_id: Optional[str] = None,
        deterministic_checkpoint: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Mark a turn as waiting for user input.

        The turn has stopped streaming, but the business workflow can continue
        in a later turn once the user submits the requested input or decision.
        """
        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))

            turn = self._turn_for_update(history, turn_id, run_id)
            if turn is not None and str(turn.get("status") or "") not in {
                "success", "failed", "cancelled", "awaiting_input"
            }:
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
                if deterministic_checkpoint is not None:
                    turn["deterministic_checkpoint"] = copy.deepcopy(
                        deterministic_checkpoint
                    )
                if summary is not None:
                    turn["summary"] = summary

            self._save_session(session_id, history, project_runtime=True)

    @staticmethod
    def _turn_for_update(
        history: SessionExecutionHistory,
        turn_id: int,
        run_id: Optional[str],
    ) -> Optional[TurnExecutionRecord]:
        matching_turns = [
            turn
            for turn in history.get("turns", [])
            if turn.get("turn_id") == turn_id
        ]
        if run_id is None:
            return matching_turns[0] if matching_turns else None
        if not matching_turns:
            return None
        for turn in matching_turns:
            if turn.get("run_id") == run_id:
                return turn
        raise TurnOwnershipError(
            "turn does not belong to run '{}'".format(run_id)
        )

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

    def _find_durable_turn_by_protocol_id(
        self,
        normalized_protocol: str,
        normalized_external_id: str,
    ) -> tuple[Optional[str], Optional[TurnExecutionRecord]]:
        if not self.storage_dir.exists():
            return None, None
        session_dirs = sorted(
            (path for path in self.storage_dir.iterdir() if path.is_dir()),
            key=lambda path: path.name,
        )
        for session_dir in session_dirs:
            try:
                session_id = validate_session_id(session_dir.name)
                session_path = (
                    resolve_session_storage_dir(self.storage_dir, session_id)
                    / "execution.json"
                )
            except ValueError:
                continue
            if not session_path.is_file():
                continue
            try:
                with open(session_path, "r", encoding="utf-8") as stream:
                    history = json.load(stream)
            except (json.JSONDecodeError, OSError) as exc:
                raise ExecutionStoreCorruptionError(
                    "execution history is corrupt during protocol lookup"
                ) from exc
            if not isinstance(history, dict) or not isinstance(
                history.get("turns"),
                list,
            ):
                raise ExecutionStoreCorruptionError(
                    "execution history is invalid during protocol lookup"
                )
            for turn in reversed(history["turns"]):
                if not isinstance(turn, dict):
                    continue
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
                self._save_session(session_id, history, project_runtime=True)
                return
            raise KeyError(turn_id)

    def reconcile_interrupted_turns(self, session_id: str) -> int:
        """Mark legacy inflight Turns interrupted without recreating execution."""

        with self._lock_for_session(session_id):
            history = copy.deepcopy(self._load_session(session_id))
            interrupted = _mark_interrupted_turns(history)
            if interrupted:
                self._save_session(session_id, history, project_runtime=True)
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
        *,
        run_id: Optional[str] = None,
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

            turn = self._turn_for_update(history, turn_id, run_id)
            return copy.deepcopy(turn) if turn is not None else None

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

    def save_waiting_solver_checkpoint(
        self,
        session_id: str,
        *,
        run_id: str,
        turn_id: int,
        checkpoint: Dict[str, Any],
    ) -> Dict[str, Any]:
        canonical = _solver_checkpoint_payload(checkpoint)
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise SolverCheckpointError("solver checkpoint run_id is required")
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            turn = None
            for item in history.get("turns", []):
                if item.get("turn_id") != turn_id:
                    continue
                if str(item.get("run_id") or "") == normalized_run_id:
                    turn = item
                    break
            if turn is None or turn.get("status") != "running":
                raise SolverCheckpointError("solver checkpoint turn is not running")
            state = canonical["state"]
            if (
                str(state.get("run_id") or "") != normalized_run_id
                or str(state.get("session_id") or "") != session_id
                or int(state.get("current_turn_id") or 0) != int(turn_id)
            ):
                raise SolverCheckpointError("solver checkpoint state identity drifted")
            existing = turn.get("solver_checkpoint")
            existing_checkpoint = existing.get("checkpoint") if isinstance(existing, dict) else None
            existing_call = (
                existing_checkpoint.get("tool_call")
                if isinstance(existing_checkpoint, dict)
                else None
            )
            existing_call_id = (
                str(existing_call.get("id") or "").strip()
                if isinstance(existing_call, dict)
                else ""
            )
            new_call_id = str(canonical["tool_call"]["id"]).strip()
            existing_inbox = existing.get("tool_result_inbox") if isinstance(existing, dict) else None
            inbox = (
                _json_clone(existing_inbox, "solver tool result inbox")
                if (
                    existing_call_id == new_call_id
                    and isinstance(existing_inbox, dict)
                    and str(existing_inbox.get("tool_call_id") or "").strip()
                    == new_call_id
                )
                else None
            )
            turn["solver_checkpoint"] = {
                "checkpoint": canonical,
                "tool_result_inbox": inbox,
                "updated_at": datetime.now().isoformat(),
            }
            self._save_session(session_id)
            return _json_clone(canonical, "solver checkpoint")

    def get_waiting_solver_checkpoint(
        self,
        session_id: str,
        *,
        run_id: str,
    ) -> Optional[Dict[str, Any]]:
        normalized_run_id = str(run_id or "").strip()
        if not normalized_run_id:
            raise SolverCheckpointError("solver checkpoint run_id is required")
        with self._lock_for_session(session_id):
            history = self._get_existing_session_unlocked(session_id)
            if not history:
                return None
            for turn in reversed(history.get("turns", [])):
                if str(turn.get("run_id") or "") != normalized_run_id:
                    continue
                record = turn.get("solver_checkpoint")
                if not isinstance(record, dict):
                    return None
                return _json_clone(record, "solver checkpoint record")
            return None

    def put_solver_tool_result(
        self,
        session_id: str,
        *,
        run_id: str,
        turn_id: int,
        tool_call_id: str,
        result_digest: str,
        result: Dict[str, Any],
    ) -> Dict[str, Any]:
        normalized_run_id = str(run_id or "").strip()
        normalized_tool_call_id = str(tool_call_id or "").strip()
        normalized_digest = str(result_digest or "").strip()
        if not normalized_run_id or not normalized_tool_call_id or not normalized_digest:
            raise SolverCheckpointError("solver tool result identity is required")
        canonical_result = _json_clone(result, "solver tool result")
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            turn = None
            for item in history.get("turns", []):
                if item.get("turn_id") != turn_id:
                    continue
                if str(item.get("run_id") or "") == normalized_run_id:
                    turn = item
                    break
            record = turn.get("solver_checkpoint") if isinstance(turn, dict) else None
            if not isinstance(record, dict):
                raise SolverCheckpointError("solver checkpoint is missing")
            checkpoint = record.get("checkpoint")
            call = checkpoint.get("tool_call") if isinstance(checkpoint, dict) else None
            if not isinstance(call, dict) or str(call.get("id") or "") != normalized_tool_call_id:
                raise SolverCheckpointError("solver tool result ToolCall drifted")
            inbox = record.get("tool_result_inbox")
            if inbox is not None:
                if (
                    not isinstance(inbox, dict)
                    or inbox.get("result_digest") != normalized_digest
                    or inbox.get("result") != canonical_result
                ):
                    raise SolverCheckpointError("solver tool result conflicts with inbox")
                return _json_clone(inbox, "solver tool result inbox")
            inbox = {
                "tool_call_id": normalized_tool_call_id,
                "result_digest": normalized_digest,
                "result": canonical_result,
                "created_at": datetime.now().isoformat(),
            }
            record["tool_result_inbox"] = inbox
            record["updated_at"] = datetime.now().isoformat()
            self._save_session(session_id)
            return _json_clone(inbox, "solver tool result inbox")

    def complete_waiting_solver_checkpoint(
        self,
        session_id: str,
        *,
        run_id: str,
        turn_id: int,
    ) -> None:
        normalized_run_id = str(run_id or "").strip()
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)
            turn = None
            for item in history.get("turns", []):
                if item.get("turn_id") != turn_id:
                    continue
                if str(item.get("run_id") or "") == normalized_run_id:
                    turn = item
                    break
            if isinstance(turn, dict) and "solver_checkpoint" in turn:
                turn.pop("solver_checkpoint", None)
                self._save_session(session_id)

    def delete_session(self, session_id: str) -> bool:
        """
        Delete all execution records for a session.

        Args:
            session_id: The session ID

        Returns:
            True if deleted, False if not found
        """
        deleted = False
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
                deleted = True

        if deleted and self._summary_index is not None:
            try:
                self._summary_index.delete(session_id)
            except Exception:
                logger.warning(
                    "Session summary execution delete projection failed for %s",
                    session_id,
                )
                try:
                    self._summary_index.mark_degraded("execution_projection_failed")
                except Exception:
                    pass
        return deleted

    # =========================================================================
    # Turn Count
    # =========================================================================

    def get_turn_count(self, session_id: str) -> int:
        """Return the number of turns recorded for a session."""
        if self._summary_index is not None:
            try:
                if self._summary_index.load_persisted_for_read():
                    summary = self._summary_index.get_summary(session_id)
                    if summary is not None:
                        return summary.turn_count
                    return 0
            except Exception:
                # The index is derived state; authoritative history remains the fallback.
                pass
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
