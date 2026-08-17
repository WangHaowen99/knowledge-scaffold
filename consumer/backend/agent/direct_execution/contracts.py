"""Typed contracts for the deterministic execution branch."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Optional, Union

from agent.direct_execution.models import RuntimeContext


@dataclass(frozen=True)
class LockedDependency:
    kind: Literal["provider", "artifact"]
    ref: str
    revision: str
    scope_digest: str = ""

    def to_dict(self) -> dict[str, Any]:
        value = {"kind": self.kind, "ref": self.ref, "revision": self.revision}
        if self.scope_digest:
            value["scope_digest"] = self.scope_digest
        return value


@dataclass(frozen=True)
class DeterministicRequest:
    user_request: str
    context: dict[str, Any]
    arguments: dict[str, Any]
    runtime_context: RuntimeContext
    checkpoint: Optional[dict[str, Any]] = None
    interaction_response: Optional[dict[str, Any]] = None


@dataclass(frozen=True)
class NotApplicable:
    reason: str


@dataclass(frozen=True)
class AwaitingInput:
    interaction: dict[str, Any]
    checkpoint: dict[str, Any]
    operation_key: str


@dataclass(frozen=True)
class Completed:
    envelope: dict[str, Any]
    operation_key: str
    elapsed_ms: int
    events: tuple[dict[str, Any], ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class Failed:
    code: str
    message: str
    operation_key: str = ""
    handler_execution_count: int = 0
    internal_detail: Optional[str] = None

    def public_result(self) -> dict[str, Any]:
        return {
            "status": "failed",
            "text": self.message,
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": False,
            },
            "execution": {
                "type": "deterministic",
                "authoritative": False,
                "complete": False,
                "operation_key": self.operation_key,
                "handler_execution_count": self.handler_execution_count,
            },
        }


DeterministicOutcome = Union[NotApplicable, AwaitingInput, Completed, Failed]
