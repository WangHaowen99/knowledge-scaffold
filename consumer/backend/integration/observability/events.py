"""Runtime observability event contract.

Core agent code emits these vendor-neutral events. Exporters translate them to
provider-specific trace/span records outside the main execution path.
This module defines the contract only; it does not queue or export events.
"""

from __future__ import annotations

import time
from dataclasses import InitVar, dataclass, field
from typing import Any, Dict, Optional

from integration.observability.identity import resolve_runtime_identity


class RuntimeEventType:
    RUN_STARTED = "run.started"
    RUN_FINISHED = "run.finished"
    RUN_FAILED = "run.failed"
    RUN_CANCELLED = "run.cancelled"
    AGENT_LOOP_STARTED = "agent_loop.started"
    AGENT_LOOP_FINISHED = "agent_loop.finished"
    AGENT_ITERATION_PREPARED = "agent_iteration.prepared"
    AGENT_ITERATION_FINISHED = "agent_iteration.finished"
    LLM_STARTED = "llm.started"
    LLM_FINISHED = "llm.finished"
    LLM_FAILED = "llm.failed"
    TOOL_BATCH_STARTED = "tool_batch.started"
    TOOL_BATCH_FINISHED = "tool_batch.finished"
    TOOL_BATCH_FAILED = "tool_batch.failed"
    TOOL_STARTED = "tool.started"
    TOOL_PROGRESS = "tool.progress"
    TOOL_FINISHED = "tool.finished"
    RUNTIME_PHASE_STARTED = "runtime_phase.started"
    RUNTIME_PHASE_FINISHED = "runtime_phase.finished"
    RUNTIME_PHASE_FAILED = "runtime_phase.failed"


class RuntimeRequestLabel:
    REASONING_SUMMARY = "reasoning_summary"


class RuntimeStatus:
    SUCCESS = "success"
    ERROR = "error"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class RuntimeEvent:
    type: str
    run_id: str
    session_id: Optional[str] = None
    turn_id: Optional[int] = None
    span_key: Optional[str] = None
    parent_key: Optional[str] = None
    trace_id: Optional[str] = None
    span_id: Optional[str] = None
    parent_span_id: Optional[str] = None
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
    _resolve_identity: InitVar[bool] = True

    def __post_init__(self, _resolve_identity: bool) -> None:
        if not _resolve_identity:
            return
        payload = self.payload if isinstance(self.payload, dict) else {}
        trace_id, span_id, parent_span_id = resolve_runtime_identity(
            self.run_id,
            span_key=self.span_key,
            parent_key=self.parent_key,
            trace_id=self.trace_id or payload.get("external_trace_id"),
            parent_span_id=self.parent_span_id or payload.get("external_parent_span_id"),
            event_type=self.type,
            timestamp_ms=self.timestamp_ms,
            start_time_ms=(
                self.timestamp_ms - int(payload.get("duration_ms") or 0)
                if self.type.endswith(("finished", "failed", "cancelled"))
                else None
            ),
            materialize_span=False,
        )
        if not self.trace_id:
            object.__setattr__(self, "trace_id", trace_id)
        if not self.span_id:
            object.__setattr__(self, "span_id", span_id)
        if self.parent_span_id is None:
            object.__setattr__(self, "parent_span_id", parent_span_id)

    @classmethod
    def from_otel(cls, **kwargs: Any) -> "RuntimeEvent":
        """Build an exporter-side event without starting another OTel span."""
        return cls(**kwargs, _resolve_identity=False)
