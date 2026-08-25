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

import json
import os
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from agent.state import (
    SessionExecutionHistory,
    TurnExecutionRecord,
)
from utils.identifiers import resolve_session_storage_dir, validate_session_id
from utils.read_docs import normalize_read_documents_registry


class InteractionCheckpointError(ValueError):
    """Raised when a user response does not match the durable checkpoint."""


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

    latest = turns[-1]
    if latest.get("status") != "awaiting_input" or latest.get("interaction_consumed_at"):
        return None, None

    final_output = latest.get("final_output")
    interaction = final_output.get("interaction") if isinstance(final_output, dict) else None
    if not isinstance(interaction, dict):
        raise InteractionCheckpointError("pending interaction checkpoint is invalid")
    return latest, interaction


def _turn_interaction(turn: TurnExecutionRecord) -> Optional[Dict[str, Any]]:
    final_output = turn.get("final_output")
    interaction = final_output.get("interaction") if isinstance(final_output, dict) else None
    return interaction if isinstance(interaction, dict) else None


def _latest_recoverable_interaction_claim(
    history: SessionExecutionHistory,
) -> tuple[
    Optional[TurnExecutionRecord],
    Optional[TurnExecutionRecord],
    Optional[Dict[str, Any]],
    Optional[Dict[str, Any]],
]:
    """Return a running response claim that can be retried after a crash."""
    turns = list(history.get("turns") or [])
    if not turns:
        return None, None, None, None
    claimed_turn = turns[-1]
    claimed_response = claimed_turn.get("prior_interaction_response")
    if claimed_turn.get("status") != "running" or not isinstance(claimed_response, dict):
        return None, None, None, None

    source_turn_id = claimed_turn.get("prior_interaction_turn_id")
    source_turn = next(
        (
            turn
            for turn in reversed(turns[:-1])
            if source_turn_id is not None and turn.get("turn_id") == source_turn_id
        ),
        None,
    )
    if source_turn is None:
        interaction_id = str(claimed_response.get("interaction_id") or "").strip()
        for turn in reversed(turns[:-1]):
            candidate_interaction = _turn_interaction(turn) or {}
            candidate_id = str(candidate_interaction.get("id") or "").strip()
            if candidate_id == interaction_id:
                source_turn = turn
                break
    interaction = _turn_interaction(source_turn) if source_turn is not None else None
    if source_turn is None or interaction is None:
        return None, None, None, None
    if source_turn.get("status") != "awaiting_input":
        return None, None, None, None
    if source_turn.get("interaction_response") != claimed_response:
        return None, None, None, None
    if not source_turn.get("interaction_consumed_at"):
        return None, None, None, None
    return claimed_turn, source_turn, interaction, claimed_response


def _latest_unconsumed_interaction_cancellation(
    history: SessionExecutionHistory,
) -> tuple[Optional[TurnExecutionRecord], Optional[Dict[str, Any]]]:
    for turn in reversed(list(history.get("turns") or [])):
        response = turn.get("interaction_response")
        if not isinstance(response, dict) or response.get("action") != "cancel":
            continue
        if turn.get("interaction_context_consumed_at"):
            return None, None
        return turn, response
    return None, None


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
        self._session_locks: Dict[str, threading.RLock] = {}

        # In-memory cache for active sessions (session_id -> SessionExecutionHistory)
        self._cache: Dict[str, SessionExecutionHistory] = {}

        # Track current turn for each session (session_id -> turn_id)
        self._current_turns: Dict[str, int] = {}

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
        if session_id in self._cache:
            return self._cache[session_id]

        session_path = self._get_session_path(session_id)
        if session_path.exists():
            try:
                with open(session_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                    self._cache[session_id] = data
                    return data
            except (json.JSONDecodeError, IOError):
                # Corrupted file, create new
                pass

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
        if session_id in self._cache:
            return self._cache[session_id]

        session_path = (
            resolve_session_storage_dir(self.storage_dir, session_id)
            / "execution.json"
        )
        if not session_path.exists():
            return None
        return self._load_session(session_id)

    def _save_session(self, session_id: str) -> None:
        """Atomically save session execution history to disk."""
        if session_id not in self._cache:
            return

        session_path = self._get_session_path(session_id)
        history = self._cache[session_id]
        history["updated_at"] = datetime.now().isoformat()
        temporary_path = session_path.with_name(
            ".{}.{}.{}.tmp".format(
                session_path.name,
                os.getpid(),
                threading.get_ident(),
            )
        )
        try:
            with open(temporary_path, "w", encoding="utf-8") as f:
                json.dump(history, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(temporary_path, session_path)
            _fsync_directory(session_path.parent)
        finally:
            temporary_path.unlink(missing_ok=True)

    # =========================================================================
    # Turn Management
    # =========================================================================

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
            history = self._load_session(session_id)
            canonical_response = _validated_interaction_response(history, interaction_response)
            started_at = datetime.now().isoformat()
            if canonical_response is not None:
                pending_turn, _interaction = _latest_pending_interaction(history)
                if pending_turn is None:
                    claimed_turn, pending_turn, _interaction, claimed_response = (
                        _latest_recoverable_interaction_claim(history)
                    )
                    if (
                        claimed_turn is None
                        or pending_turn is None
                        or claimed_response != canonical_response
                    ):
                        raise InteractionCheckpointError(
                            "interaction recovery claim is unavailable"
                        )
                    claimed_turn["end_time"] = started_at
                    claimed_turn["status"] = "failed"
                    claimed_turn["final_output"] = {
                        "error": {
                            "code": "interaction_resume_interrupted",
                            "message": "The prior interaction resume was interrupted and retried.",
                        }
                    }
                else:
                    pending_turn["interaction_response"] = canonical_response
                    pending_turn["interaction_consumed_at"] = started_at
                pending_turn["interaction_consumed_by_turn_id"] = turn_id
                deterministic_checkpoint = pending_turn.get("deterministic_checkpoint")
            else:
                deterministic_checkpoint = None

            prior_interaction_cancellation = None
            if canonical_response is None:
                cancelled_turn, cancellation = _latest_unconsumed_interaction_cancellation(history)
                if cancelled_turn is not None and cancellation is not None:
                    prior_interaction_cancellation = dict(cancellation)
                    cancelled_turn["interaction_context_consumed_at"] = started_at
                    cancelled_turn["interaction_context_consumed_by_turn_id"] = turn_id

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
            if prior_interaction_cancellation is not None:
                turn["prior_interaction_cancellation"] = prior_interaction_cancellation
            if canonical_response is not None:
                turn["prior_interaction_response"] = dict(canonical_response)
                turn["prior_interaction_turn_id"] = pending_turn.get("turn_id")
            if isinstance(deterministic_checkpoint, dict):
                turn["prior_deterministic_checkpoint"] = dict(deterministic_checkpoint)

            history["turns"].append(turn)
            self._current_turns[session_id] = turn_id
            self._save_session(session_id)

            return turn

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
                self._save_session(session_id)
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
            history = self._load_session(session_id)

            # Retries stay idempotent even when the user has already started a new turn.
            for turn in reversed(list(history.get("turns") or [])):
                response = turn.get("interaction_response")
                if not isinstance(response, dict):
                    continue
                if (
                    response.get("action") == "cancel"
                    and str(response.get("interaction_id") or "").strip() == requested_id
                ):
                    return dict(response)

            pending_turn, interaction = _latest_pending_interaction(history)
            if pending_turn is None or interaction is None:
                raise InteractionCheckpointError("session has no pending interaction")

            expected_id = str(interaction.get("id") or "").strip()
            if not expected_id or requested_id != expected_id:
                raise InteractionCheckpointError(
                    "interaction_id does not match the pending interaction"
                )

            cancelled_at = datetime.now().isoformat()
            cancellation = {
                "interaction_id": expected_id,
                "type": str(interaction.get("type") or "input").strip() or "input",
                "action": "cancel",
                "cancelled": True,
                "cancelled_by": "user",
                "cancelled_at": cancelled_at,
                "message": INTERACTION_CANCELLED_MESSAGE,
                "display_message": INTERACTION_CANCELLED_NOTICE,
                "run_id": pending_turn.get("run_id"),
                "turn_id": pending_turn.get("turn_id"),
            }

            pending_turn["status"] = "cancelled"
            pending_turn["end_time"] = cancelled_at
            pending_turn["interaction_response"] = cancellation
            pending_turn["interaction_consumed_at"] = cancelled_at
            pending_turn["interaction_cancelled_at"] = cancelled_at
            self._save_session(session_id)
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
            history = self._load_session(session_id)

            # Find the turn
            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "success"
                    turn["final_output"] = final_output
                    turn["summary"] = summary
                    break

            self._save_session(session_id)

    def fail_turn(
        self,
        session_id: str,
        turn_id: int,
        error: Any,
    ) -> None:
        """
        Mark a turn as failed.

        Args:
            session_id: The session ID
            turn_id: The turn number
            error: The error message
        """
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)

            # Find the turn
            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    if turn.get("status") in {
                        "success",
                        "failed",
                        "cancelled",
                        "awaiting_input",
                    }:
                        break
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "failed"
                    # Add error to final_output
                    turn["final_output"] = (
                        dict(error) if isinstance(error, dict) else {"error": error}
                    )
                    break

            self._save_session(session_id)

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
            history = self._load_session(session_id)

            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "cancelled"
                    turn["final_output"] = final_output or {"cancel_reason": reason}
                    if summary is not None:
                        turn["summary"] = summary
                    break

            self._save_session(session_id)

    def await_turn(
        self,
        session_id: str,
        turn_id: int,
        final_output: Optional[Dict[str, Any]] = None,
        summary: Optional[str] = None,
        deterministic_checkpoint: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Mark a turn as waiting for user input.

        The turn has stopped streaming, but the business workflow can continue
        in a later turn once the user submits the requested input or decision.
        """
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)

            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "awaiting_input"
                    turn["final_output"] = final_output or {"status": "awaiting_input"}
                    if summary is not None:
                        turn["summary"] = summary
                    if deterministic_checkpoint is not None:
                        turn["deterministic_checkpoint"] = dict(
                            deterministic_checkpoint
                        )
                    break

            self._save_session(session_id)

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
            history = self._get_existing_session_unlocked(session_id)
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
                self._save_session(session_id)
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
            history = self._load_session(session_id)
            for turn in history.get("turns", []):
                if turn.get("turn_id") == turn_id:
                    turn["ontology_version"] = ontology_version
                    break
            self._save_session(session_id)

    # =========================================================================
    # Retrieval
    # =========================================================================

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
            return self._get_existing_session_unlocked(session_id)

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
                    return turn

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
            return list(reversed(turns[-limit:]))

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
            return list(reversed(turns))

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
            return history.get("session_history", [])

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
            history = self._load_session(session_id)
            history["session_history"] = session_history
            self._save_session(session_id)
            return True

    def get_read_documents_context(self, session_id: str) -> Dict[str, Any]:
        """
        Get the structured read-document ledger for a session.

        This ledger is kept separately from LLM messages so tool guards can
        validate document prerequisites across turns without parsing tool text.
        """
        with self._lock:
            history = self._load_session(session_id)
            return normalize_read_documents_registry(history.get("read_documents"))

    def set_read_documents_context(
        self,
        session_id: str,
        read_documents: Optional[Dict[str, Any]],
    ) -> bool:
        """
        Persist the structured read-document ledger for future turns.
        """
        with self._lock:
            history = self._load_session(session_id)
            history["read_documents"] = normalize_read_documents_registry(read_documents)
            self._save_session(session_id)
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
            history = self._load_session(session_id)
            if "session_history" not in history:
                history["session_history"] = []
            history["session_history"].append(entry)
            self._save_session(session_id)
            return True
