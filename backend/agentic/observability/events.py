"""Runtime observability event contract.

Core agent code emits these vendor-neutral events. Exporters translate them to
provider-specific trace/span records outside the main execution path.
This module defines the contract only; it does not queue or export events.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, Optional


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
    payload: Dict[str, Any] = field(default_factory=dict)
    timestamp_ms: int = field(default_factory=lambda: int(time.time() * 1000))
