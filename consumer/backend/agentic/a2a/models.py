"""A2A-facing typed models."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any, Dict, Optional


class A2ATaskState(str, Enum):
    """Normalized task states for A2A-facing APIs."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELED = "canceled"
    NOT_FOUND = "not_found"


@dataclass
class A2ATaskSnapshot:
    """External task view projected from internal run + turn records."""

    id: str
    session_id: Optional[str]
    state: A2ATaskState
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    completed_at: Optional[str] = None
    error: Optional[str] = None
    result: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "session_id": self.session_id,
            "state": self.state.value,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "completed_at": self.completed_at,
            "error": self.error,
            "result": self.result,
            "metadata": self.metadata,
        }


@dataclass
class A2ATaskCreateRequest:
    """Create task request payload (A2A adapter input)."""

    message: str
    session_id: Optional[str] = None
    task_id: Optional[str] = None
    context: Dict[str, Any] = field(default_factory=dict)


def iso_or_none(value: Optional[datetime]) -> Optional[str]:
    if value is None:
        return None
    try:
        return value.isoformat()
    except Exception:
        return None
