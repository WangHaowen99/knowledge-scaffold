"""Data models for skill-evolution tasks."""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Dict, Optional


# Task status lifecycle:
#   submitted -> running -> downloading -> completed(待审批) -> approved / rejected
#        \-> cancelled (user local cancel, from submitted/running)
#        \-> no_change (empty diff, auto terminal, non-blocking)
#        \-> failed (evolution/download/extract/timeout/unreachable)
#        \-> interrupted (service restart, in-flight marked)
PENDING_STATUSES = {"submitted", "running", "downloading", "completed"}
TERMINAL_STATUSES = {
    "approved",
    "rejected",
    "no_change",
    "failed",
    "interrupted",
    "cancelled",
}
APPROVE_DECISIONS = {"approve_overwrite", "approve_new_version"}


@dataclass
class TaskRecord:
    """A single skill-evolution task, persisted as JSON."""

    task_id: str
    workspace_id: str
    ontology_id: str
    session_id: str
    triggered_by: str
    run_id: str
    status: str
    active_version: str
    version_sha: str            # whole-version sha at submit time (drift baseline)
    created_at: str
    updated_at: str
    ontology_name: str = ""
    stage: str = ""                       # submitting/evolving/downloading/diffing
    error_message: str = ""
    failed_stage: str = ""
    change_summary: str = ""
    decided_by: str = ""
    decided_at: str = ""
    decision: str = ""                    # approve_overwrite/approve_new_version/reject
    new_version: str = ""
    trace_size_bytes: int = 0
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskRecord":
        known = {f for f in cls.__dataclass_fields__}  # type: ignore[attr-defined]
        payload = {k: v for k, v in data.items() if k in known}
        # Tolerate older records missing newer fields — dataclass defaults apply.
        return cls(**payload)


@dataclass
class DiffFileEntry:
    """One changed file in an evolution diff."""

    path: str                  # relative to ontology root, e.g. operations/foo.md
    change_type: str           # modified / added / deleted
    original: str
    evolved: str
    added_lines: int = 0
    removed_lines: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class DiffResult:
    """Full diff of an evolution artifact against the submit-time baseline."""

    task_id: str
    ontology_id: str
    change_summary: str
    files: list  # list[DiffFileEntry]
    is_empty: bool

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "ontology_id": self.ontology_id,
            "change_summary": self.change_summary,
            "is_empty": self.is_empty,
            "files": [f.to_dict() if isinstance(f, DiffFileEntry) else f for f in self.files],
        }


@dataclass
class DriftStatus:
    """Result of drift detection at approval time."""

    drifted: bool
    reason: str = ""          # content_modified / active_version_switched / clean
    message: str = ""         # user-facing message
    submit_active_version: str = ""
    current_active_version: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)
