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
import threading
from datetime import datetime
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

        session_path = self._get_session_path(session_id)
        if not session_path.exists():
            return None
        return self._load_session(session_id)

    def _save_session(self, session_id: str) -> None:
        """Save session execution history to disk."""
        if session_id not in self._cache:
            return

        session_path = self._get_session_path(session_id)
        history = self._cache[session_id]
        history["updated_at"] = datetime.now().isoformat()

        with open(session_path, "w", encoding="utf-8") as f:
            json.dump(history, f, ensure_ascii=False, indent=2)

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
            if canonical_response is not None:
                pending_turn = history["turns"][-1]
                pending_turn["interaction_response"] = canonical_response
                pending_turn["interaction_consumed_at"] = datetime.now().isoformat()

            # Create new turn record
            turn: TurnExecutionRecord = {
                "turn_id": turn_id,
                "session_id": session_id,
                "run_id": run_id,
                "user_request": user_request,
                "start_time": datetime.now().isoformat(),
                "end_time": None,
                "status": "running",
                "final_output": None,
                "summary": None,
                "ontology_version": ontology_version,
            }

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
            history = self._load_session(session_id)

            # Find the turn
            for turn in history["turns"]:
                if turn["turn_id"] == turn_id:
                    turn["end_time"] = datetime.now().isoformat()
                    turn["status"] = "failed"
                    # Add error to final_output
                    turn["final_output"] = {"error": error}
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
                    break

            self._save_session(session_id)

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
