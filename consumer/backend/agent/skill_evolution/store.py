"""Filesystem store for skill-evolution tasks, temp artifacts, and history.

Layout (mirrors operations-evolution, project-root level, workspace-isolated):

    <runtime_root>/skill-evolution/workspaces/<workspace_id>/
        tasks/<task_id>.json        task state record (atomic write)
        baseline/<task_id>.zip      submit-time baseline copy
        temp/<task_id>/             evolved artifact (extracted, cleared on terminal)
        history/<task_id>.json      approval history (diff snapshot, decision)
        corrupted/<task_id>.json    quarantined unparseable task records
"""

from __future__ import annotations

import json
import os
import shutil
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from utils.logging import get_logger
from workspace.workspaces import get_runtime_root, validate_workspace_id
from agent.skill_evolution.models import PENDING_STATUSES, TERMINAL_STATUSES, TaskRecord

logger = get_logger("skill_evolution.store")

_SKILL_EVOLUTION_DIR_NAME = "skill-evolution"


class PendingTaskExists(RuntimeError):
    """Raised when an ontology already has a pending evolution task."""


def _utc_now() -> str:
    """Return the current UTC timestamp without sub-second precision."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _safe_segment(value: str) -> str:
    """Restrict path segments to filesystem-safe characters."""
    cleaned = "".join(c for c in str(value or "") if c.isalnum() or c in ("-", "_", "."))
    return cleaned or "unknown"


class SkillEvolutionStore:
    """Thread-safe filesystem store for skill-evolution task data."""

    def __init__(self, runtime_root: Optional[str] = None) -> None:
        self._root = Path(runtime_root or get_runtime_root()).resolve()
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ paths
    def root(self) -> Path:
        """Return the root directory for skill-evolution state."""
        return self._root / _SKILL_EVOLUTION_DIR_NAME

    def iter_workspace_ids(self) -> List[str]:
        """List workspace identifiers with persisted evolution state."""
        ws_root = self.root() / "workspaces"
        if not ws_root.exists():
            return []
        return sorted(entry.name for entry in ws_root.iterdir() if entry.is_dir())

    def _workspace_root(self, workspace_id: str) -> Path:
        return self.root() / "workspaces" / _safe_segment(validate_workspace_id(workspace_id))

    def _tasks_dir(self, workspace_id: str) -> Path:
        return self._workspace_root(workspace_id) / "tasks"

    def _baseline_dir(self, workspace_id: str) -> Path:
        return self._workspace_root(workspace_id) / "baseline"

    def _temp_dir(self, workspace_id: str) -> Path:
        return self._workspace_root(workspace_id) / "temp"

    def _history_dir(self, workspace_id: str) -> Path:
        return self._workspace_root(workspace_id) / "history"

    def _corrupted_dir(self, workspace_id: str) -> Path:
        return self._workspace_root(workspace_id) / "corrupted"

    def tasks_dir_for_test(self, workspace_id: str) -> Path:
        """Return the task directory for storage contract assertions."""
        return self._tasks_dir(workspace_id)

    def corrupted_dir_for_test(self, workspace_id: str) -> Path:
        """Return the quarantine directory for storage contract assertions."""
        return self._corrupted_dir(workspace_id)

    def task_path(self, workspace_id: str, task_id: str) -> Path:
        """Return the task record path for a workspace and task."""
        return self._tasks_dir(workspace_id) / f"{_safe_segment(task_id)}.json"

    def baseline_path(self, workspace_id: str, task_id: str) -> Path:
        """Return the immutable baseline archive path for a task."""
        return self._baseline_dir(workspace_id) / f"{_safe_segment(task_id)}.zip"

    def temp_path(self, workspace_id: str, task_id: str) -> Path:
        """Return the temporary extraction directory for a task."""
        return self._temp_dir(workspace_id) / _safe_segment(task_id)

    def history_path(self, workspace_id: str, task_id: str) -> Path:
        """Return the persisted approval-history path for a task."""
        return self._history_dir(workspace_id) / f"{_safe_segment(task_id)}.json"

    # ----------------------------------------------------------------- write
    def save_task(self, record: TaskRecord) -> None:
        """Atomically persist a task record and refresh its update timestamp."""
        record.updated_at = _utc_now()
        path = self.task_path(record.workspace_id, record.task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = record.to_dict()
        # Atomic write: temp file + os.replace
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def create_task(self, record: TaskRecord) -> TaskRecord:
        """Persist a task record under the store lock."""
        with self._lock:
            self.save_task(record)
        return record

    def create_task_if_no_pending(self, record: TaskRecord) -> TaskRecord:
        """Atomically (under the store lock) refuse if a pending task already."""
        # Details: Atomically (under the store lock) refuse if a pending task already exists for this ontology, else
        # create the task. The service-layer gate-4 ``has_pending`` check is a fast-fail to avoid a wasted
        # skill-evolver submit; this is the authoritative check that closes the check-then-act race between
        # has_pending and create_task — two concurrent /start calls can both pass gate4 then both reach create_task.
        # On collision the later one raises (its skill-evolver run is abandoned, same as a local cancel).
        with self._lock:
            if self.has_pending(record.workspace_id, record.ontology_id):
                raise PendingTaskExists(
                    "该本体已有正在进行的进化任务，请先完成审批或取消"
                )
            self.save_task(record)
        return record

    # ------------------------------------------------------------------ read
    def get_task(self, workspace_id: str, task_id: str) -> Optional[TaskRecord]:
        """Load one task record, quarantining malformed state when needed."""
        path = self.task_path(workspace_id, task_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            return TaskRecord.from_dict(data)
        except (OSError, TypeError, ValueError):
            logger.warning("skill-evolution: corrupt task record %s, quarantining", path)
            self._quarantine(workspace_id, task_id, path)
            return None

    def _quarantine(self, workspace_id: str, task_id: str, path: Path) -> None:
        target_dir = self._corrupted_dir(workspace_id)
        target_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, target_dir / path.name)
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("skill-evolution: failed to quarantine %s", path)

    def list_tasks(
        self,
        workspace_id: str,
        *,
        ontology_id: Optional[str] = None,
        pending_only: bool = False,
        triggered_by: Optional[str] = None,
        is_admin: bool = False,
    ) -> List[TaskRecord]:
        """List task records visible under the requested workspace filters."""
        tasks_dir = self._tasks_dir(workspace_id)
        if not tasks_dir.exists():
            return []
        out: List[TaskRecord] = []
        for entry in sorted(tasks_dir.glob("*.json")):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
                record = TaskRecord.from_dict(data)
            except (OSError, TypeError, ValueError):
                logger.warning("skill-evolution: corrupt task record %s, quarantining", entry)
                self._quarantine(workspace_id, entry.stem, entry)
                continue
            if ontology_id and record.ontology_id != ontology_id:
                continue
            if pending_only and record.status not in PENDING_STATUSES:
                continue
            if not is_admin and triggered_by and record.triggered_by != triggered_by:
                continue
            out.append(record)
        out.sort(key=lambda r: r.created_at, reverse=True)
        return out

    def has_pending(self, workspace_id: str, ontology_id: str) -> bool:
        """Return whether an ontology has a non-terminal evolution task."""
        tasks_dir = self._tasks_dir(workspace_id)
        if not tasks_dir.exists():
            return False
        for entry in tasks_dir.glob("*.json"):
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if (
                data.get("ontology_id") == ontology_id
                and data.get("status") in PENDING_STATUSES
            ):
                return True
        return False

    # ----------------------------------------------------------- temp/baseline
    def ensure_temp_dir(self, workspace_id: str, task_id: str) -> Path:
        """Create and return a task's temporary extraction directory."""
        path = self.temp_path(workspace_id, task_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def clean_temp(self, workspace_id: str, task_id: str) -> None:
        """Remove temporary extracted artifacts for a task when present."""
        path = self.temp_path(workspace_id, task_id)
        if path.exists():
            shutil.rmtree(path, ignore_errors=True)

    def save_baseline(self, workspace_id: str, task_id: str, zip_bytes: bytes) -> Path:
        """Atomically persist the submit-time baseline archive."""
        path = self.baseline_path(workspace_id, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(zip_bytes)
        os.replace(tmp, path)
        return path

    def clean_baseline(self, workspace_id: str, task_id: str) -> None:
        """Remove a task's baseline archive when present."""
        path = self.baseline_path(workspace_id, task_id)
        if path.exists():
            try:
                path.unlink()
            except OSError:
                logger.warning("skill-evolution: failed to delete baseline %s", path)

    # --------------------------------------------------------------- history
    def save_history(self, workspace_id: str, task_id: str, payload: Dict[str, Any]) -> None:
        """Atomically persist the approval decision and complete diff payload."""
        path = self.history_path(workspace_id, task_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, path)

    def get_history(self, workspace_id: str, task_id: str) -> Optional[Dict[str, Any]]:
        """Load a task's approval history, returning None when unavailable."""
        path = self.history_path(workspace_id, task_id)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None

    # ------------------------------------------------------- terminal cleanup
    def finalize_artifacts(self, record: TaskRecord) -> None:
        """On terminal status, clear temp + baseline (history retained)."""
        if record.status not in TERMINAL_STATUSES:
            return
        self.clean_temp(record.workspace_id, record.task_id)
        self.clean_baseline(record.workspace_id, record.task_id)

    def delete_task(self, workspace_id: str, task_id: str) -> None:
        """Remove a task record + its history (+ any leftover temp/baseline).

        Only called for terminal tasks. Does NOT revert applied ontology
        changes — an approved task's version stays on the ontology.
        """
        self.clean_temp(workspace_id, task_id)
        self.clean_baseline(workspace_id, task_id)
        for path in (self.task_path(workspace_id, task_id), self.history_path(workspace_id, task_id)):
            if path.exists():
                try:
                    path.unlink()
                except OSError:
                    logger.warning("skill-evolution: failed to delete %s", path)
