"""Run state storage for workflow execution tracking."""

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, List, Optional
import threading

from utils.logging import get_logger

logger = get_logger("stores.run")


class RunStatus(str, Enum):
    """Status of a workflow run."""

    PENDING = "pending"
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING = "awaiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class Run:
    """Represents a single workflow run."""

    run_id: str
    user_request: str
    status: RunStatus = RunStatus.PENDING
    created_at: datetime = field(default_factory=datetime.utcnow)
    updated_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    result: Optional[Dict[str, Any]] = None
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


class RunStore:
    """
    In-memory store for workflow run state.

    Thread-safe storage for tracking run status, results, and metadata.
    For MVP, uses in-memory storage. Can be extended to use Redis/DB.
    """

    def __init__(self) -> None:
        self._runs: Dict[str, Run] = {}
        self._lock = threading.Lock()
        self._active_run_counts: Dict[str, int] = {}
        self._active_run_counts_lock = threading.Lock()

    def create_run(
        self,
        run_id: str,
        user_request: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Run:
        """
        Create a new run entry.

        Args:
            run_id: Unique run identifier
            user_request: The user's original request
            metadata: Optional additional metadata

        Returns:
            Created Run object
        """
        with self._lock:
            existing = self._runs.get(run_id)
            if existing:
                was_running = existing.status == RunStatus.RUNNING
                previous_user_id = existing.metadata.get("user_id")
                merged_metadata = dict(existing.metadata)
                merged_metadata.update(metadata or {})
                existing.user_request = user_request
                existing.status = RunStatus.RUNNING
                existing.updated_at = datetime.utcnow()
                existing.completed_at = None
                existing.result = None
                existing.error = None
                existing.metadata = merged_metadata
                user_id = merged_metadata.get("user_id")
                if was_running and previous_user_id and previous_user_id != user_id:
                    with self._active_run_counts_lock:
                        self._active_run_counts[previous_user_id] = max(
                            0,
                            self._active_run_counts.get(previous_user_id, 0) - 1,
                        )
                if user_id and (not was_running or user_id != previous_user_id):
                    with self._active_run_counts_lock:
                        self._active_run_counts[user_id] = self._active_run_counts.get(user_id, 0) + 1
                logger.info("Updated existing run %s", run_id)
                return existing

            run = Run(
                run_id=run_id,
                user_request=user_request,
                status=RunStatus.RUNNING,
                metadata=metadata or {},
            )
            self._runs[run_id] = run
            user_id = (metadata or {}).get("user_id")
            if user_id:
                with self._active_run_counts_lock:
                    self._active_run_counts[user_id] = self._active_run_counts.get(user_id, 0) + 1
            logger.info("Created run %s", run_id)
            return run

    def queue_run(
        self,
        run_id: str,
        user_request: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Run:
        """Create or refresh a run that is waiting for an execution slot."""
        with self._lock:
            existing = self._runs.get(run_id)
            if existing is not None:
                if existing.status == RunStatus.RUNNING:
                    raise ValueError("cannot queue a running run")
                existing.user_request = user_request
                existing.status = RunStatus.QUEUED
                existing.updated_at = datetime.utcnow()
                existing.completed_at = None
                existing.result = None
                existing.error = None
                existing.metadata.update(metadata or {})
                return existing

            run = Run(
                run_id=run_id,
                user_request=user_request,
                status=RunStatus.QUEUED,
                metadata=metadata or {},
            )
            self._runs[run_id] = run
            logger.info("Queued run %s", run_id)
            return run

    def get_run(self, run_id: str) -> Optional[Run]:
        """Get a run by ID."""
        with self._lock:
            return self._runs.get(run_id)

    def update_status(
        self,
        run_id: str,
        status: RunStatus,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Run]:
        """
        Update run status.

        Args:
            run_id: Run identifier
            status: New status
            metadata: Optional metadata to merge

        Returns:
            Updated Run or None if not found
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                logger.warning("Run %s not found for status update", run_id)
                return None

            run.status = status
            run.updated_at = datetime.utcnow()
            if metadata:
                run.metadata.update(metadata)

            logger.info("Updated run %s status to %s", run_id, status)
            return run

    def update_metadata(
        self,
        run_id: str,
        metadata: Dict[str, Any],
    ) -> Optional[Run]:
        """
        Update run metadata without changing status.

        Args:
            run_id: Run identifier
            metadata: Metadata to merge into existing run metadata

        Returns:
            Updated Run or None if not found
        """
        if not metadata:
            return self.get_run(run_id)
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                logger.warning("Run %s not found for metadata update", run_id)
                return None
            run.metadata.update(metadata)
            run.updated_at = datetime.utcnow()
            return run

    def _decrement_active_count(self, run: Run) -> None:
        """Decrement the per-user active-run counter if the run was RUNNING."""
        was_running = run.status == RunStatus.RUNNING
        if not was_running:
            return
        user_id = run.metadata.get("user_id")
        if user_id:
            with self._active_run_counts_lock:
                self._active_run_counts[user_id] = max(0, self._active_run_counts.get(user_id, 0) - 1)

    def complete_run(
        self,
        run_id: str,
        result: Dict[str, Any],
    ) -> Optional[Run]:
        """
        Mark run as completed.

        Args:
            run_id: Run identifier
            result: Final result data

        Returns:
            Updated Run or None if not found
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                logger.warning("Run %s not found for completion", run_id)
                return None

            self._decrement_active_count(run)
            run.status = RunStatus.COMPLETED
            run.result = result
            run.completed_at = datetime.utcnow()
            run.updated_at = run.completed_at

            logger.info("Completed run %s", run_id)
        return run

    def await_run(
        self,
        run_id: str,
        result: Dict[str, Any],
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Run]:
        """Mark a run as waiting for user input."""
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                logger.warning("Run %s not found for awaiting state", run_id)
                return None

            self._decrement_active_count(run)
            run.status = RunStatus.AWAITING
            run.result = result
            run.updated_at = datetime.utcnow()
            if metadata:
                run.metadata.update(metadata)

            logger.info("Run %s is awaiting user input", run_id)
        return run

    def fail_run(
        self,
        run_id: str,
        error: str,
    ) -> Optional[Run]:
        """
        Mark run as failed.

        Args:
            run_id: Run identifier
            error: Error message

        Returns:
            Updated Run or None if not found
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                logger.warning("Run %s not found for failure", run_id)
                return None

            self._decrement_active_count(run)
            run.status = RunStatus.FAILED
            run.error = error
            run.completed_at = datetime.utcnow()
            run.updated_at = run.completed_at

            logger.error("Failed run %s: %s", run_id, error)
        return run

    def cancel_run(
        self,
        run_id: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> Optional[Run]:
        """
        Mark run as cancelled.

        Args:
            run_id: Run identifier

        Returns:
            Updated Run or None if not found
        """
        with self._lock:
            run = self._runs.get(run_id)
            if not run:
                return None

            self._decrement_active_count(run)
            run.status = RunStatus.CANCELLED
            run.completed_at = datetime.utcnow()
            run.updated_at = run.completed_at
            if metadata:
                run.metadata.update(metadata)

            logger.info("Cancelled run %s", run_id)
        return run

    def list_runs(
        self,
        status: Optional[RunStatus] = None,
        limit: int = 100,
    ) -> List[Run]:
        """
        List runs, optionally filtered by status.

        Args:
            status: Optional status filter
            limit: Maximum number of runs to return

        Returns:
            List of Run objects
        """
        with self._lock:
            runs = list(self._runs.values())

            if status:
                runs = [r for r in runs if r.status == status]

            # Sort by created_at descending
            runs.sort(key=lambda r: r.created_at, reverse=True)

            return runs[:limit]

    def get_active_run_count(self, user_id: str) -> int:
        """Return the number of currently RUNNING runs for a user.

        O(1) lookup — does not iterate the full run map.
        """
        with self._active_run_counts_lock:
            return self._active_run_counts.get(user_id, 0)

    def delete_run(self, run_id: str) -> bool:
        """
        Delete a run.

        Args:
            run_id: Run identifier

        Returns:
            True if deleted, False if not found
        """
        removed = False
        with self._lock:
            if run_id in self._runs:
                del self._runs[run_id]
                removed = True
        if removed:
            logger.info("Deleted run %s", run_id)
        return removed
