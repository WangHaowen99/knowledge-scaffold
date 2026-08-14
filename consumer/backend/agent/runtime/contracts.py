"""Stable A/B contracts for invoking an asynchronous Agent runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

if TYPE_CHECKING:
    from .events import RuntimeEvent


RuntimeTerminalStatus = Literal["completed", "failed", "cancelled"]
LlmRequestMode = Literal["sync", "stream"]
RuntimeHistoryRole = Literal["user", "assistant", "tool"]


def _require_non_empty(value: str, info) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError("{} must not be empty".format(info.field_name))
    return normalized


class RuntimeHistoryMessage(BaseModel):
    """Provider-neutral persisted message passed from line B to line A."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    role: RuntimeHistoryRole
    content: str | list[dict[str, Any]] = ""
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None
    name: str | None = None
    reasoning_content: str | None = None

    @field_validator("tool_call_id", "name", "reasoning_content")
    @classmethod
    def _normalize_optional_text(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None

    @model_validator(mode="after")
    def _validate_role_shape(self) -> "RuntimeHistoryMessage":
        if self.role == "user":
            forbidden_fields = (
                self.tool_calls,
                self.tool_call_id,
                self.name,
                self.reasoning_content,
            )
            if any(forbidden_fields):
                raise ValueError("user history message cannot contain tool or reasoning fields")
        elif self.role == "assistant":
            if self.tool_call_id or self.name:
                raise ValueError("assistant history message cannot identify a tool result")
        elif not self.tool_call_id:
            raise ValueError("tool history message requires tool_call_id")
        elif self.tool_calls or self.reasoning_content:
            raise ValueError("tool history message cannot contain assistant-only fields")
        return self


class RuntimeInvocationContext(BaseModel):
    """Immutable business context passed from line B to line A."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    session_id: str
    turn_id: int = Field(ge=1)
    workspace_id: str
    username: str
    query: str
    agent_mode: str
    llm_request_mode: LlmRequestMode
    model_profile_id: str | None = None
    arguments: dict[str, Any] | None = None
    history_messages: list[RuntimeHistoryMessage] = Field(default_factory=list)
    include_trace: bool = False

    _validate_run_id = field_validator("run_id")(_require_non_empty)
    _validate_session_id = field_validator("session_id")(_require_non_empty)
    _validate_workspace_id = field_validator("workspace_id")(_require_non_empty)
    _validate_username = field_validator("username")(_require_non_empty)
    _validate_query = field_validator("query")(_require_non_empty)
    _validate_agent_mode = field_validator("agent_mode")(_require_non_empty)

    @field_validator("model_profile_id")
    @classmethod
    def _normalize_model_profile_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None


class RuntimeErrorInfo(BaseModel):
    """Sanitized runtime error independent from HTTP and protocol errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: str
    message: str
    retriable: bool = False
    source: str
    details: dict[str, Any] = Field(default_factory=dict)

    _validate_error_code = field_validator("error_code")(_require_non_empty)
    _validate_message = field_validator("message")(_require_non_empty)
    _validate_source = field_validator("source")(_require_non_empty)


class RuntimeResult(BaseModel):
    """Terminal result returned by ``AsyncAgentRuntime.arun``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RuntimeTerminalStatus
    content: str | None = None
    structured_content: dict[str, Any] | list[Any] | None = None
    usage: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    error: RuntimeErrorInfo | None = None

    @model_validator(mode="after")
    def _validate_terminal_shape(self) -> "RuntimeResult":
        if self.status == "completed" and self.error is not None:
            raise ValueError("completed result must not contain error")
        if self.status in {"failed", "cancelled"} and self.error is None:
            raise ValueError("{} result requires error".format(self.status))
        return self


class RuntimeEventSink(Protocol):
    """Backpressure-aware destination for line-A runtime events."""

    async def emit(self, event: "RuntimeEvent") -> None: ...


class AsyncAgentRuntime(Protocol):
    """Logical interface line A exposes to line B."""

    async def arun(
        self,
        context: RuntimeInvocationContext,
        sink: RuntimeEventSink,
    ) -> RuntimeResult: ...

    def astream(
        self,
        context: RuntimeInvocationContext,
    ) -> AsyncIterator["RuntimeEvent"]: ...

    async def cancel(self, run_id: str, reason: str) -> bool: ...

    async def release(self, jiuwen_session_id: str) -> None: ...
