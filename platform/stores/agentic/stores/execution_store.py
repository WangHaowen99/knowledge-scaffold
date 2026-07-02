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

from ..models.state import (
    SessionExecutionHistory,
    TurnExecutionRecord,
)
from ..utils.read_docs import normalize_read_documents_registry


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
        with self._lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def _get_session_path(self, session_id: str) -> Path:
        """Get the file path for a session's execution history."""
        session_dir = self.storage_dir / session_id
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
    ) -> TurnExecutionRecord:
        """
        Initialize a new turn record.

        Args:
            session_id: The session ID
            turn_id: The turn number within the session
            run_id: The unique run ID for this execution
            user_request: The user's request text

        Returns:
            The newly created TurnExecutionRecord
        """
        with self._lock_for_session(session_id):
            history = self._load_session(session_id)

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
            }

            history["turns"].append(turn)
            self._current_turns[session_id] = turn_id
            self._save_session(session_id)

            return turn

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
            session_dir = self.storage_dir / session_id
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
