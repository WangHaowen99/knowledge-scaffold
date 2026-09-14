# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Deterministic async runtime used by line-B development and tests."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any

from .contracts import RuntimeErrorInfo, RuntimeEventSink, RuntimeInvocationContext, RuntimeResult
from .events import RuntimeEvent


@dataclass
class _MockRun:
    context: RuntimeInvocationContext
    cancelled: bool = False
    cancel_reason: str = "cancelled"
    terminal: bool = False
    interaction_id: str | None = None
    interaction_value: Any = None
    resume_event: asyncio.Event = field(default_factory=asyncio.Event)


class ListRuntimeEventSink:
    """Simple backpressure-aware sink that records emitted events."""

    def __init__(self) -> None:
        self.events: list[RuntimeEvent] = []

    async def emit(self, event: RuntimeEvent) -> None:
        self.events.append(event)
        await asyncio.sleep(0)


class FixedAsyncAgentRuntime:
    """Scripted Runtime Mock covering success, failure, cancel and HITL."""

    def __init__(self) -> None:
        self._runs: dict[str, _MockRun] = {}
        self.released_session_ids: list[str] = []

    async def arun(
        self,
        context: RuntimeInvocationContext,
        sink: RuntimeEventSink,
    ) -> RuntimeResult:
        terminal_event: RuntimeEvent | None = None
        async for event in self.astream(context):
            await sink.emit(event)
            if event.event_type == "turn.finished":
                terminal_event = event
        if terminal_event is None:
            return RuntimeResult(
                status="failed",
                error=RuntimeErrorInfo(
                    error_code="MOCK_MISSING_TERMINAL",
                    message="Mock runtime ended without terminal event",
                    retriable=False,
                    source="runtime",
                ),
            )
        return self._result_from_terminal(terminal_event)

    async def astream(self, context: RuntimeInvocationContext):
        if context.run_id in self._runs:
            raise RuntimeError(
                "Mock runtime already has an active run '{}'".format(
                    context.run_id
                )
            )
        try:
            async for event in self._astream_impl(context):
                yield event
        finally:
            self._runs.pop(context.run_id, None)

    async def _astream_impl(self, context: RuntimeInvocationContext):
        scenario = str((context.arguments or {}).get("mock_scenario") or "success")
        if scenario not in {"success", "tool_failure", "awaiting_input", "cancelled"}:
            raise ValueError("Unsupported mock_scenario: {}".format(scenario))

        run = _MockRun(context=context)
        self._runs[context.run_id] = run
        yield self._event(context, "turn.started", "running", {"turn_id": context.turn_id}, "agent")
        await asyncio.sleep(0)

        if scenario == "cancelled":
            run.cancelled = True
            run.cancel_reason = "mock requested cancellation"

        if run.cancelled:
            yield self._cancelled_event(run)
            run.terminal = True
            return

        if scenario == "awaiting_input":
            run.interaction_id = "interaction_{}".format(context.run_id)
            yield self._event(
                context,
                "interaction.requested",
                "awaiting_input",
                {
                    "interaction_id": run.interaction_id,
                    "type": "single_choice",
                    "options": [
                        {"option_id": "continue", "label": "继续", "value": "continue"}
                    ],
                },
                "interaction",
            )
            await run.resume_event.wait()
            if run.cancelled:
                yield self._cancelled_event(run)
                run.terminal = True
                return
            yield self._event(
                context,
                "token.delta",
                "running",
                {"text": "已收到用户输入"},
                "model",
            )
            yield self._completed_event(context, content="交互已恢复")
            run.terminal = True
            return

        yield self._event(context, "model.started", "running", {}, "model")
        yield self._event(
            context,
            "tool.started",
            "running",
            {"name": "mock_tool", "tool_call_id": "tool_01"},
            "tool",
        )

        if scenario == "tool_failure":
            error = RuntimeErrorInfo(
                error_code="TOOL_EXECUTION_FAILED",
                message="Mock tool failed",
                retriable=False,
                source="tool",
                details={"tool_call_id": "tool_01"},
            )
            yield self._event(
                context,
                "tool.finished",
                "running",
                {
                    "name": "mock_tool",
                    "tool_call_id": "tool_01",
                    "status": "failed",
                    "elapsed_ms": 1,
                    "error": error.model_dump(mode="json"),
                },
                "tool",
            )
            yield self._failed_event(context, error)
            run.terminal = True
            return

        yield self._event(
            context,
            "tool.finished",
            "running",
            {
                "name": "mock_tool",
                "tool_call_id": "tool_01",
                "status": "completed",
                "elapsed_ms": 1,
                "output_preview": "ok",
            },
            "tool",
        )
        yield self._event(
            context,
            "token.delta",
            "running",
            {"text": "mock answer"},
            "model",
        )
        yield self._completed_event(context, content="mock answer")
        run.terminal = True

    async def cancel(self, run_id: str, reason: str) -> bool:
        run = self._runs.get(str(run_id or "").strip())
        if run is None or run.terminal:
            return False
        run.cancelled = True
        run.cancel_reason = str(reason or "cancelled")
        run.resume_event.set()
        return True

    async def release(self, jiuwen_session_id: str) -> None:
        normalized = str(jiuwen_session_id or "").strip()
        if not normalized:
            raise ValueError("jiuwen_session_id must not be empty")
        self.released_session_ids.append(normalized)
        await asyncio.sleep(0)

    async def resume_interaction(self, run_id: str, interaction_id: str, value: Any) -> bool:
        run = self._runs.get(str(run_id or "").strip())
        if run is None or run.terminal or run.interaction_id != interaction_id:
            return False
        run.interaction_value = value
        run.resume_event.set()
        return True

    @staticmethod
    def _event(
        context: RuntimeInvocationContext,
        event_type: str,
        status: str,
        payload: dict[str, Any],
        component: str,
    ) -> RuntimeEvent:
        return RuntimeEvent(
            event_type=event_type,
            status=status,
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            source={"node": "mock_runtime", "component": component},
            payload=payload,
        )

    def _completed_event(self, context: RuntimeInvocationContext, *, content: str) -> RuntimeEvent:
        return self._event(
            context,
            "turn.finished",
            "completed",
            {
                "status": "completed",
                "content": content,
                "usage": {"input_tokens": 0, "output_tokens": 0},
            },
            "agent",
        )

    def _failed_event(
        self,
        context: RuntimeInvocationContext,
        error: RuntimeErrorInfo,
    ) -> RuntimeEvent:
        return self._event(
            context,
            "turn.finished",
            "failed",
            {"status": "failed", "error": error.model_dump(mode="json")},
            "agent",
        )

    def _cancelled_event(self, run: _MockRun) -> RuntimeEvent:
        error = RuntimeErrorInfo(
            error_code="RUN_CANCELLED",
            message=run.cancel_reason,
            retriable=False,
            source="runtime",
        )
        return self._event(
            run.context,
            "turn.finished",
            "cancelled",
            {"status": "cancelled", "error": error.model_dump(mode="json")},
            "agent",
        )

    @staticmethod
    def _result_from_terminal(event: RuntimeEvent) -> RuntimeResult:
        payload = event.payload
        status = str(payload.get("status") or event.status)
        if status == "completed":
            return RuntimeResult(
                status="completed",
                content=payload.get("content"),
                structured_content=payload.get("structured_content"),
                usage=payload.get("usage"),
                trace=payload.get("trace"),
            )
        error_payload = payload.get("error") or {
            "error_code": "MOCK_RUNTIME_ERROR",
            "message": "Mock runtime failed",
            "retriable": False,
            "source": "runtime",
            "details": {},
        }
        return RuntimeResult(
            status=status,
            error=RuntimeErrorInfo.model_validate(error_payload),
        )
