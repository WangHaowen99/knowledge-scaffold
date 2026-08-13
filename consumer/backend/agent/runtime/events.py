"""Runtime and public event models shared by the async integration lines."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


RuntimeStatus = Literal[
    "queued",
    "running",
    "awaiting_input",
    "completed",
    "failed",
    "cancelled",
]

RuntimeEventType = Literal[
    "turn.started",
    "model.started",
    "token.delta",
    "reasoning.delta",
    "tool.started",
    "tool.finished",
    "interaction.requested",
    "turn.finished",
]

PublicEventType = Literal[
    "run.started",
    "turn.started",
    "model.started",
    "token.delta",
    "reasoning.delta",
    "tool.started",
    "tool.finished",
    "interaction.requested",
    "interaction.resumed",
    "turn.finished",
]

_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled"})
_STATUS_ALIASES = {"success": "completed", "error": "failed"}


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _non_empty(value: str, info) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("{} must not be empty".format(info.field_name))
    return normalized


def _validate_turn_finished(
    event_type: str,
    status: str,
    payload: dict[str, Any],
) -> None:
    if event_type != "turn.finished":
        return
    if status not in _TERMINAL_STATUSES:
        raise ValueError("turn.finished requires a terminal status")
    payload_status = payload.get("status")
    if payload_status is None:
        return
    normalized = str(payload_status).strip().lower()
    normalized = _STATUS_ALIASES.get(normalized, normalized)
    if normalized != status:
        raise ValueError("turn.finished payload status must match event status")


class RuntimeEvent(BaseModel):
    """Internal event emitted by line A before public sanitization."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: RuntimeEventType
    status: RuntimeStatus
    run_id: str
    session_id: str | None = None
    turn_id: int | None = Field(default=None, ge=1)
    source: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)

    _validate_run_id = field_validator("run_id")(_non_empty)
    _validate_created_at = field_validator("created_at")(_non_empty)

    @model_validator(mode="after")
    def _validate_terminal_event(self) -> "RuntimeEvent":
        _validate_turn_finished(self.event_type, self.status, self.payload)
        return self


class PublicEventDraft(BaseModel):
    """Sanitized event awaiting line-B identity/sequence allocation."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    session_id: str | None = None
    turn_id: int | None = Field(default=None, ge=1)
    event_type: PublicEventType
    status: RuntimeStatus
    source: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)
    event_id: None = None
    seq: None = None

    _validate_run_id = field_validator("run_id")(_non_empty)
    _validate_created_at = field_validator("created_at")(_non_empty)

    @model_validator(mode="after")
    def _validate_terminal_event(self) -> "PublicEventDraft":
        _validate_turn_finished(self.event_type, self.status, self.payload)
        return self


class PublicEvent(BaseModel):
    """Persistable event after line B allocates identity and sequence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    event_id: str
    run_id: str
    session_id: str | None = None
    turn_id: int | None = Field(default=None, ge=1)
    seq: int = Field(ge=1)
    event_type: PublicEventType
    status: RuntimeStatus
    source: dict[str, Any] | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=utc_now_iso)

    _validate_event_id = field_validator("event_id")(_non_empty)
    _validate_run_id = field_validator("run_id")(_non_empty)
    _validate_created_at = field_validator("created_at")(_non_empty)

    @model_validator(mode="after")
    def _validate_terminal_event(self) -> "PublicEvent":
        _validate_turn_finished(self.event_type, self.status, self.payload)
        return self
