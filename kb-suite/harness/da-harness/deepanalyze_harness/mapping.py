"""Translate DA SSE events into HarnessProtocol observations.

Level alignment (see the 10-12 integration design):

* protocol Turn  == one DA task (taskId), including answer-resumed segments
* protocol Step  == one DA ``turn`` (a ReAct iteration)
* DA restarts its tool-call numbering after every answer, so item ids carry a
  segment counter (``seg``) bumped by the harness after each POST /answer.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.harness_protocol import (
    ContentBlock,
    ItemEventKind,
    ItemLifecycleEvent,
    MessageRole,
    OutputChannel,
    OutputEvent,
    OutputKind,
    OutputOperation,
    ProviderEvent,
    TurnError,
    TurnEventKind,
    TurnMessage,
    TurnResult,
    TurnStatus,
    TurnTermination,
    TurnTerminationKind,
    TurnUsage,
    UsageUpdatedEvent,
    UsageUpdateMode,
)

PROVIDER = "deepanalyze"
MAX_TOOL_RESULT_CHARS = 20000
SILENT_EVENTS = {
    "content_start", "content_end", "thinking_start", "thinking_end", "heartbeat", "reconnect_done",
}


def json_safe(value: Any) -> Any:
    """Round-trip through JSON so payloads are plain, protocol-acceptable values."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


@dataclass
class Mapped:
    payload: Any
    item_id: str | None = None


@dataclass
class DaTurnState:
    """Accumulates one protocol Turn while DA events stream in."""

    user_text: str
    task_id: str | None = None
    seg: int = 0
    turn: int = 0
    answer: list[str] = field(default_factory=list)
    final_output: str | None = None
    status: str | None = None
    error: TurnError | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    asks: int = 0
    engine: str = "DeepAnalyze"  # name used in error messages
    # DA tool id -> protocol item id for calls still waiting for their result.
    # DA renumbers tools after an answered ask_user, so the result of a call
    # opened before the answer must reuse the item id it started with.
    open_tools: dict[str, str] = field(default_factory=dict)

    def _output_id(self, channel: str) -> str:
        return f"{self.task_id}:{self.seg}:{channel}:{self.turn}"

    def consume(self, event: str, data: dict[str, Any]) -> list[Mapped]:
        if isinstance(data.get("turn"), int):
            self.turn = data["turn"]
        if event == "start":
            self.task_id = str(data.get("taskId") or self.task_id or "")
            return []
        if event in SILENT_EVENTS:
            return []
        if event == "content_delta":
            delta = str(data.get("delta") or "")
            if not delta:
                return []
            self.answer.append(delta)
            return [Mapped(OutputEvent(
                output_id=self._output_id("a"), kind=OutputKind.TEXT, content=delta,
                operation=OutputOperation.DELTA, channel=OutputChannel.ANSWER,
            ))]
        if event == "thinking_delta":
            delta = str(data.get("delta") or "")
            if not delta:
                return []
            return [Mapped(OutputEvent(
                output_id=self._output_id("r"), kind=OutputKind.TEXT, content=delta,
                operation=OutputOperation.DELTA, channel=OutputChannel.REASONING,
            ))]
        if event == "content":
            text = data.get("content")
            if not isinstance(text, str) or not text:
                return []
            return [Mapped(OutputEvent(
                output_id=self._output_id("a"), kind=OutputKind.TEXT, content=text,
                operation=OutputOperation.SNAPSHOT, channel=OutputChannel.ANSWER,
            ))]
        if event == "turn":
            return [Mapped(
                ItemLifecycleEvent(kind=ItemEventKind.COMPLETED, item_type="step", data={"turn": self.turn}),
                item_id=f"{self.task_id}:{self.seg}:step:{self.turn}",
            )]
        if event == "turn_usage":
            usage = data.get("usage") or {}
            delta = {
                "input_tokens": _int(usage.get("input_tokens")),
                "output_tokens": _int(usage.get("output_tokens")),
                "cached_input_tokens": _int(usage.get("cache_tokens")),
                "total_tokens": _int(usage.get("total_tokens")),
            }
            for key, value in delta.items():
                if value is not None:
                    self.usage[key] = self.usage.get(key, 0) + value
            provider_data = {"model_name": usage.get("model_name")} if usage.get("model_name") else {}
            return [Mapped(UsageUpdatedEvent(
                usage=TurnUsage(**delta, provider_data=provider_data), mode=UsageUpdateMode.DELTA,
            ))]
        if event == "tool_call":
            self.tool_calls += 1
            item_id = f"{self.task_id}:{self.seg}:{data.get('id')}"
            self.open_tools[str(data.get("id"))] = item_id
            return [Mapped(
                ItemLifecycleEvent(
                    kind=ItemEventKind.STARTED,
                    item_type="tool",
                    data={"name": str(data.get("toolName") or ""), "arguments": json_safe(data.get("input") or {})},
                ),
                item_id=item_id,
            )]
        if event == "tool_result":
            output = data.get("output")
            if isinstance(output, str) and len(output) > MAX_TOOL_RESULT_CHARS:
                output = output[:MAX_TOOL_RESULT_CHARS] + "…[truncated]"
            item_id = self.open_tools.pop(str(data.get("id")), f"{self.task_id}:{self.seg}:{data.get('id')}")
            return [Mapped(
                ItemLifecycleEvent(
                    kind=ItemEventKind.COMPLETED,
                    item_type="tool",
                    data={"tool_name": str(data.get("toolName") or ""), "result": json_safe(output)},
                ),
                item_id=item_id,
            )]
        if event == "complete":
            if isinstance(data.get("output"), str):
                self.final_output = data["output"]
            return []
        if event == "error":
            self.error = TurnError(
                message=str(data.get("message") or data.get("error") or f"{self.engine} reported an error")[:500],
                code=str(data.get("code") or "") or None,
                category="sdk_error",
                retryable=str(data.get("code") or "") == "ENGINE_BUSY" or None,
            )
            return []
        if event == "done":
            self.status = str(data.get("status") or "completed")
            if self.final_output is None and isinstance(data.get("output"), str):
                self.final_output = data["output"]
            return []
        # Unknown and DA-specific events are kept, not dropped.
        return [Mapped(ProviderEvent(provider=PROVIDER, event_type=event, schema_version="1", payload=json_safe(data)))]

    # -- results -------------------------------------------------------------

    def _messages(self, final_text: str) -> tuple[TurnMessage, ...]:
        messages = [TurnMessage(
            message_id=f"user-{uuid.uuid4().hex[:12]}", role=MessageRole.USER,
            content=(ContentBlock(block_id="b0", kind="text", content=self.user_text),),
        )]
        if final_text:
            messages.append(TurnMessage(
                message_id=f"assistant-{self.task_id or uuid.uuid4().hex[:12]}", role=MessageRole.ASSISTANT,
                content=(ContentBlock(block_id="b0", kind="text", content=final_text),),
            ))
        return tuple(messages)

    def _usage(self) -> TurnUsage | None:
        return TurnUsage(**self.usage) if self.usage else None

    def result(
        self, *, started_at: float, started_monotonic: float, aborted: bool = False,
        stopped: bool = False, failure: TurnError | None = None,
    ) -> tuple[TurnEventKind, TurnResult]:
        elapsed = max(0.0, time.monotonic() - started_monotonic)
        final_text = self.final_output if self.final_output is not None else "".join(self.answer)
        common = {
            "messages": self._messages(final_text),
            "final_output": final_text,
            "usage": self._usage(),
            "started_at": started_at,
            # measured on the monotonic clock: the wall clock can step back mid-turn
            # (NTP / WSL resync), and a completed_at before started_at is rejected
            "completed_at": started_at + elapsed,
            "duration_ms": int(elapsed * 1000),
            "provider_data": json_safe({
                "task_id": self.task_id, "segments": self.seg + 1, "tool_calls": self.tool_calls,
                "asks": self.asks, "da_status": self.status,
            }),
        }
        if aborted or stopped or self.status == "cancelled":
            kind = TurnTerminationKind.HARNESS_STOP if stopped else TurnTerminationKind.USER_ABORT
            return TurnEventKind.ABORTED, TurnResult(
                status=TurnStatus.INTERRUPTED,
                termination=TurnTermination(kind=kind, message=f"{self.engine} task was cancelled"),
                **common,
            )
        error = failure or self.error
        if error is not None or self.status == "failed":
            return TurnEventKind.FAILED, TurnResult(
                status=TurnStatus.FAILED,
                error=error or TurnError(message=f"{self.engine} task failed", category="sdk_error"),
                **common,
            )
        return TurnEventKind.FINISHED, TurnResult(status=TurnStatus.COMPLETED, stop_reason="completed", **common)
