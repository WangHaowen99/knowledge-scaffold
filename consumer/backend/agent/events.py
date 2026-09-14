# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Typed event models for the agent-stream.v2 protocol."""

from __future__ import annotations

from typing import Any, Dict, Literal, Optional, TypedDict, Union


AgentEventType = Literal[
    "turn.started",
    "turn.finished",
    "iteration.completed",
    "token.delta",
    "reasoning.summary",
    "tool.started",
    "tool.progress",
    "tool.finished",
    "business.checkpoint",
    "solver.input.awaiting_confirmation",
    "solver.progress",
    "solver.verification.completed",
    "solver.verification.rejected",
    "solver.result.authoritative",
    "subagent.started",
    "subagent.delta",
    "subagent.progress",
    "subagent.finished",
    "subagent_group.started",
    "subagent_group.finished",
]


class AgentEventSource(TypedDict, total=False):
    node: str
    component: str


class TurnStartedPayload(TypedDict, total=False):
    user_preview: str


class TurnFinishedPayload(TypedDict, total=False):
    status: Literal["success", "error", "cancelled"]
    final_output: Any
    error: str
    cancel_reason: str
    usage: Dict[str, Any]


class IterationCompletedPayload(TypedDict, total=False):
    iteration: int
    status: str
    model_elapsed_ms: int
    setup_elapsed_ms: int
    llm_elapsed_ms: int
    llm_stream_elapsed_ms: int
    llm_wall_elapsed_ms: int
    tools_elapsed_ms: int
    tools_window_elapsed_ms: int
    tools_wall_elapsed_ms: int
    tools_execution_elapsed_ms: int
    tools_overlap_llm_ms: int
    tools_net_wait_ms: int
    prompt_build_ms: int
    total_elapsed_ms: int
    tool_calls_count: int
    tool_names: list[str]


class TokenDeltaPayload(TypedDict, total=False):
    channel: Literal["content", "reasoning"]
    text: str
    token_index: int
    reasoning_block_id: str


class ReasoningSummaryPayload(TypedDict, total=False):
    channel: Literal["reasoning"]
    summary: str
    reasoning_block_id: str


class ToolStartedPayload(TypedDict, total=False):
    tool_call_id: str
    tool_name: str
    input: Dict[str, Any]
    message: str


class ToolProgressPayload(TypedDict, total=False):
    tool_call_id: str
    tool_name: str
    status: str
    message: str
    progress: Dict[str, Any]


class ToolFinishedPayload(TypedDict, total=False):
    tool_call_id: str
    tool_name: str
    status: Literal["completed", "error", "cancelled"]
    output_preview: str
    output_truncated: bool
    output_ref: str
    output_size: int
    output_projection: Dict[str, Any]
    error: str
    elapsed_ms: int


class BusinessCheckpointPayload(TypedDict, total=False):
    schema_version: int
    tool_call_id: str
    tool_name: str
    stage: str
    stage_title: str
    phase: str
    phase_title: str
    summary: str
    render_intent: Literal[
        "text_summary",
        "table",
        "section_tables",
        "impact_tree",
        "metric_cards",
    ]
    entity_refs: list[str]
    data: Dict[str, Any]


class SubagentStartedPayload(TypedDict, total=False):
    subagent_id: str
    agent_type: str
    task_id: str
    task: str
    task_index: int
    group_id: str
    parent_subagent_id: str
    depth: int
    root_tool_call_id: str
    parent_tool_call_id: str


class SubagentDeltaPayload(TypedDict, total=False):
    subagent_id: str
    agent_type: str
    task_id: str
    channel: Literal["content", "reasoning", "log"]
    text: str
    index: int
    group_id: str
    parent_subagent_id: str
    depth: int
    root_tool_call_id: str
    parent_tool_call_id: str


class SubagentProgressPayload(TypedDict, total=False):
    subagent_id: str
    agent_type: str
    task_id: str
    status: str
    step: str
    meta: Dict[str, Any]
    group_id: str
    parent_subagent_id: str
    depth: int
    root_tool_call_id: str
    parent_tool_call_id: str


class SubagentFinishedPayload(TypedDict, total=False):
    subagent_id: str
    agent_type: str
    task_id: str
    status: Literal["completed", "error"]
    result: Any
    error: str
    group_id: str
    parent_subagent_id: str
    depth: int
    root_tool_call_id: str
    parent_tool_call_id: str


class SubagentGroupTaskBrief(TypedDict, total=False):
    task_id: str
    task: str
    task_index: int


class SubagentGroupStartedPayload(TypedDict, total=False):
    group_id: str
    depth: int
    parent_subagent_id: str
    root_tool_call_id: str
    parent_tool_call_id: str
    task_count: int
    tasks: list


class SubagentGroupFinishedPayload(TypedDict, total=False):
    group_id: str
    depth: int
    status: Literal["completed", "error"]
    task_count: int
    completed_count: int
    error_count: int


AgentEventPayload = Union[
    TurnStartedPayload,
    TurnFinishedPayload,
    IterationCompletedPayload,
    TokenDeltaPayload,
    ReasoningSummaryPayload,
    ToolStartedPayload,
    ToolProgressPayload,
    ToolFinishedPayload,
    BusinessCheckpointPayload,
    SubagentStartedPayload,
    SubagentDeltaPayload,
    SubagentProgressPayload,
    SubagentFinishedPayload,
    SubagentGroupStartedPayload,
    SubagentGroupFinishedPayload,
    Dict[str, Any],
]


class AgentEventEnvelope(TypedDict, total=False):
    protocol: Literal["agent-stream.v2"]
    event_id: str
    seq: int
    ts: int
    session_id: Optional[str]
    run_id: str
    turn_id: int
    type: AgentEventType
    source: AgentEventSource
    payload: Dict[str, Any]
    span_key: Optional[str]
    parent_key: Optional[str]


class AgentStreamEvent(TypedDict):
    event: Literal["agent.v2"]
    data: AgentEventEnvelope
