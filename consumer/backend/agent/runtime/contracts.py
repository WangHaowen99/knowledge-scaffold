"""Stable contracts for the native runtime and consumer run platform."""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .preflight_cache import freeze_preflight_product

if TYPE_CHECKING:
    from .events import RuntimeEvent


RuntimeTerminalStatus = Literal["awaiting_input", "completed", "failed", "cancelled"]
LlmRequestMode = Literal["sync", "stream"]
RuntimeHistoryRole = Literal["user", "assistant", "tool"]
USER_WORKSPACE_RUN_CONCURRENCY_LIMIT = 100


@dataclass(frozen=True, slots=True)
class RunPreflightSnapshot:
    """Exact immutable products admitted for one run request."""

    workspace_id: str
    workspace_config_generation: int
    system_config_generation: int
    ontology_id: str = ""
    ontology_version: str = ""
    mutation_revision: int = 0
    ontology_manifest: Mapping[str, Any] | None = field(default=None, repr=False)
    root_payload: Mapping[str, Any] | None = field(default=None, repr=False)
    ontology_source_signature: tuple[object, ...] | None = field(
        default=None,
        repr=False,
    )
    ontology_content_revision: str | None = field(default=None, repr=False)
    script_bindings: Mapping[str, Mapping[str, Any]] = field(
        default_factory=dict,
        repr=False,
    )
    execute_script_tool_schema: Mapping[str, Any] | None = field(
        default=None,
        repr=False,
    )
    solver_capabilities: tuple[Mapping[str, Any], ...] = field(
        default_factory=tuple,
        repr=False,
    )
    selected_ontology_context: str | None = field(default=None, repr=False)
    preload_records: tuple[Mapping[str, Any], ...] = field(
        default_factory=tuple,
        repr=False,
    )
    model_profile: Mapping[str, Any] | None = field(default=None, repr=False)
    deterministic_candidate: bool = False

    def __post_init__(self) -> None:
        workspace = str(self.workspace_id or "").strip()
        if not workspace:
            raise ValueError("workspace_id must not be empty")
        object.__setattr__(self, "workspace_id", workspace)
        for field_name in (
            "workspace_config_generation",
            "system_config_generation",
            "mutation_revision",
        ):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        object.__setattr__(self, "ontology_id", str(self.ontology_id or "").strip())
        object.__setattr__(
            self,
            "ontology_version",
            str(self.ontology_version or "").strip(),
        )
        for field_name in (
            "ontology_manifest",
            "root_payload",
            "execute_script_tool_schema",
            "model_profile",
        ):
            value = getattr(self, field_name)
            if value is not None:
                object.__setattr__(
                    self,
                    field_name,
                    freeze_preflight_product(value),
                )
        if self.ontology_source_signature is not None:
            object.__setattr__(
                self,
                "ontology_source_signature",
                freeze_preflight_product(self.ontology_source_signature),
            )
        if self.ontology_content_revision is not None:
            revision = str(self.ontology_content_revision or "").strip()
            if (
                len(revision) != 71
                or not revision.startswith("sha256:")
                or any(character not in "0123456789abcdef" for character in revision[7:])
            ):
                raise ValueError("ontology_content_revision must be a sha256 digest")
            object.__setattr__(self, "ontology_content_revision", revision)
        object.__setattr__(
            self,
            "script_bindings",
            freeze_preflight_product(self.script_bindings),
        )
        object.__setattr__(
            self,
            "solver_capabilities",
            tuple(freeze_preflight_product(capability) for capability in self.solver_capabilities),
        )
        object.__setattr__(
            self,
            "preload_records",
            tuple(freeze_preflight_product(record) for record in self.preload_records),
        )


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
    agent_mode: str = "quick"
    llm_request_mode: LlmRequestMode = "stream"
    model_profile_id: str | None = None
    requested_operation_key: str | None = Field(default=None, exclude=True)
    arguments: dict[str, Any] | None = None
    history_messages: list[RuntimeHistoryMessage] = Field(default_factory=list)
    model_profile_ref: dict[str, Any] | None = Field(default=None, exclude=True)
    model_provider: str | None = Field(default=None, exclude=True)
    preflight_snapshot: RunPreflightSnapshot | None = Field(
        default=None,
        exclude=True,
        repr=False,
    )
    memory_context: str | None = Field(default=None, exclude=True)
    read_documents: dict[str, Any] | None = Field(default=None, exclude=True)
    interaction_cancellation: dict[str, Any] | None = Field(
        default=None,
        exclude=True,
    )
    include_trace: bool = False
    supports_user_interaction: bool = True
    allow_existing_run_id: bool = False
    traceparent: str = ""
    interaction_response: dict[str, Any] | None = None
    concurrency_limit: int = Field(default=1, ge=1)
    admission_timeout_seconds: float | None = Field(default=None, gt=0)
    timeout_seconds: float | None = Field(default=None, gt=0)

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

    @field_validator("requested_operation_key")
    @classmethod
    def _normalize_requested_operation_key(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return str(value).strip() or None


class RuntimeErrorInfo(BaseModel):
    """Sanitized runtime error independent from HTTP and protocol errors."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    error_code: str
    message: str
    retriable: bool = False
    source: str = "runtime"
    details: dict[str, Any] = Field(default_factory=dict)

    _validate_error_code = field_validator("error_code")(_require_non_empty)
    _validate_message = field_validator("message")(_require_non_empty)
    _validate_source = field_validator("source")(_require_non_empty)

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class RuntimeRecoveryAction(BaseModel):
    """Recovery metadata with separate UI and model-facing text."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    action: Literal["continue"] = "continue"
    label: str = Field(
        default="继续执行",
        description="User-visible button label; never added to model context.",
    )
    message: str = Field(
        description="Instruction submitted as a new user message to the model.",
    )

    _validate_label = field_validator("label")(_require_non_empty)
    _validate_message = field_validator("message")(_require_non_empty)


class RuntimeResult(BaseModel):
    """Terminal result returned by ``AsyncAgentRuntime.arun``."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    status: RuntimeTerminalStatus
    content: str | None = None
    structured_content: dict[str, Any] | list[Any] | None = None
    usage: dict[str, Any] | None = None
    trace: dict[str, Any] | None = None
    error: RuntimeErrorInfo | None = None
    recovery: RuntimeRecoveryAction | None = None
    history_messages: list[RuntimeHistoryMessage] = Field(
        default_factory=list,
        exclude=True,
    )
    read_documents: dict[str, Any] | None = Field(default=None, exclude=True)
    evolution_recall_records: list[dict[str, Any]] = Field(
        default_factory=list,
        exclude=True,
    )

    @model_validator(mode="after")
    def _validate_terminal_shape(self) -> "RuntimeResult":
        if self.status == "completed" and self.error is not None:
            raise ValueError("completed result must not contain error")
        if self.status == "failed" and self.error is None:
            raise ValueError("failed result requires error")
        if self.recovery is not None and self.status != "failed":
            raise ValueError("recovery action is only valid for failed results")
        if self.recovery is not None and (self.error is None or not self.error.retriable):
            raise ValueError("recovery action requires a retriable error")
        return self

    def to_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json", exclude_none=True)


class RuntimeEventSink(Protocol):
    """Backpressure-aware destination for line-A runtime events."""

    async def emit(
        self,
        event: "RuntimeEvent",
    ) -> None: ...


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


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_INPUT = "awaiting_input"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


TERMINAL_RUN_STATUSES = {
    RunStatus.AWAITING_INPUT,
    RunStatus.COMPLETED,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


@dataclass(frozen=True)
class RunScope:
    username: str
    workspace_id: str
    scopes: tuple[str, ...] = ()

    def owns(self, snapshot: "RunSnapshot") -> bool:
        return (
            bool(self.username)
            and bool(self.workspace_id)
            and self.username == snapshot.scope.username
            and self.workspace_id == snapshot.scope.workspace_id
        )

    def allows(self, required_scope: str) -> bool:
        return not self.scopes or required_scope in self.scopes


@dataclass(frozen=True)
class RunSnapshot:
    run_id: str
    session_id: str
    turn_id: int
    status: RunStatus
    created_at: str
    updated_at: str
    last_seq: int
    scope: RunScope
    context: RuntimeInvocationContext
    result: RuntimeResult | None = None
    error: RuntimeErrorInfo | None = None
    pending_interaction: dict[str, Any] | None = None
    cancel_reason: str | None = None

    def to_view(self) -> "RunView":
        return RunView(
            run_id=self.run_id,
            session_id=self.session_id,
            turn_id=self.turn_id,
            status=self.status.value,
            last_seq=self.last_seq,
            created_at=self.created_at,
            updated_at=self.updated_at,
            result=self.result.to_dict() if self.result is not None else None,
            error=self.error.to_dict() if self.error is not None else None,
            pending_interaction=(dict(self.pending_interaction) if self.pending_interaction is not None else None),
        )


@dataclass(frozen=True)
class RunView:
    run_id: str
    session_id: str
    turn_id: int
    status: str
    last_seq: int
    created_at: str
    updated_at: str
    result: dict[str, Any] | None
    error: dict[str, Any] | None
    pending_interaction: dict[str, Any] | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "status": self.status,
            "last_seq": self.last_seq,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "result": self.result,
            "error": self.error,
            "pending_interaction": self.pending_interaction,
        }


if TYPE_CHECKING:
    from .events import PublicEventDraft


class PublicEventSink(Protocol):
    async def emit(
        self,
        draft: "PublicEventDraft",
    ) -> None: ...


# Re-export event models for the public A/B import surface without duplicating
# their schema definitions in this module.
from .events import PublicEvent, PublicEventDraft  # noqa: E402, F401
