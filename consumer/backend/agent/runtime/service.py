"""Consumer execution service over the native openJiuwen runtime contract."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager, nullcontext
from datetime import datetime, timezone
from typing import Any, Callable

from agent.runtime.adapter import PublicEventAdapter
from agent.runtime.async_utils import run_sync_compat
from agent.runtime.contracts import (
    AsyncAgentRuntime,
    PublicEvent,
    PublicEventDraft,
    PublicEventSink,
    RuntimeErrorInfo,
    RuntimeEventSink,
    RuntimeHistoryMessage,
    RuntimeInvocationContext,
    RuntimeRecoveryAction,
    RuntimeResult,
)
from agent.runtime.events import RuntimeEvent
from utils.logging import get_logger

try:
    from agent.tools.events import tool_output_artifact_writer_context
except ImportError:  # pragma: no cover - minimal runtime-only installations.
    tool_output_artifact_writer_context = None


logger = get_logger("runtime.service")


_SOLVER_CONTINUATION_STATE_FIELDS = (
    "run_id",
    "session_id",
    "current_turn_id",
    "workspace_id",
    "user_id",
    "user_request",
    "user_context",
    "user_lang",
    "messages",
    "read_documents",
    "results",
    "completed_ids",
    "failed_ids",
    "errors",
    "evolution_recall_records",
    "debug",
    "final_output",
)


def _solver_tool_name(name: Any) -> bool:
    normalized = str(name or "").strip()
    return normalized == "solve" or normalized.startswith("solve_")


def _solver_tool_arguments(
    function: Mapping[str, Any],
) -> dict[str, Any] | None:
    raw = function.get("arguments")
    if isinstance(raw, dict):
        return copy.deepcopy(raw)
    if not isinstance(raw, str):
        return None
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _waiting_solver_checkpoint_from_state(
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    messages: list[Mapping[str, Any]] = []
    for message in list(state.get("messages") or []):
        if isinstance(message, Mapping):
            messages.append(message)
    for assistant_index in range(len(messages) - 1, -1, -1):
        assistant = messages[assistant_index]
        if assistant.get("role") != "assistant":
            continue
        calls = assistant.get("tool_calls")
        if not isinstance(calls, list):
            continue
        for raw_call in reversed(calls):
            if not isinstance(raw_call, Mapping):
                continue
            function = raw_call.get("function")
            if not isinstance(function, Mapping):
                continue
            if not _solver_tool_name(function.get("name")):
                continue
            tool_call_id = str(raw_call.get("id") or "").strip()
            arguments = _solver_tool_arguments(function)
            if not tool_call_id or arguments is None:
                return None
            tool_result_index = None
            for index in range(assistant_index + 1, len(messages)):
                message = messages[index]
                if message.get("role") != "tool":
                    continue
                if str(message.get("tool_call_id") or "").strip() == tool_call_id:
                    tool_result_index = index
                    break
            if tool_result_index is not None:
                for index in range(tool_result_index + 1, len(messages)):
                    later_message = messages[index]
                    if later_message.get("role") == "assistant" and not later_message.get(
                        "tool_calls"
                    ):
                        return None
            continuation_state: dict[str, Any] = {}
            for key in _SOLVER_CONTINUATION_STATE_FIELDS:
                if key in state:
                    continuation_state[key] = copy.deepcopy(state.get(key))
            return {
                "schema_version": "agent.solver-checkpoint.v1",
                "stage": (
                    "waiting_solver" if tool_result_index is None else "react"
                ),
                "tool_call": {
                    "id": tool_call_id,
                    "name": str(function.get("name") or "").strip(),
                    "arguments": arguments,
                },
                "state": continuation_state,
            }
    return None


def _append_solver_tool_message(
    checkpoint: Mapping[str, Any],
    inbox: Mapping[str, Any],
) -> dict[str, Any]:
    state = copy.deepcopy(dict(checkpoint.get("state") or {}))
    call = dict(checkpoint.get("tool_call") or {})
    tool_call_id = str(call.get("id") or "").strip()
    tool_name = str(call.get("name") or "").strip()
    result = inbox.get("result")
    inbox_call_id = str(inbox.get("tool_call_id") or "").strip()
    if not tool_call_id or not tool_name or inbox_call_id != tool_call_id:
        raise ValueError("solver checkpoint inbox is invalid")
    if not isinstance(result, Mapping):
        raise ValueError("solver checkpoint inbox is invalid")
    messages = list(state.get("messages") or [])
    has_tool_result = False
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        if message.get("role") != "tool":
            continue
        if str(message.get("tool_call_id") or "").strip() == tool_call_id:
            has_tool_result = True
            break
    if not has_tool_result:
        messages.append(
            {
                "role": "tool",
                "content": json.dumps(
                    dict(result),
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ),
                "tool_call_id": tool_call_id,
                "name": tool_name,
            }
        )
    state["messages"] = messages
    return state


class _MultipleInteractionsRequested(RuntimeError):
    pass


_WORKSPACE_SCOPE: Any = None
_WORKSPACE_SCOPE_RESOLVED = False


def _runtime_history(values: list[Any]) -> list[RuntimeHistoryMessage]:
    history: list[RuntimeHistoryMessage] = []
    for item in values:
        if not isinstance(item, Mapping) or item.get("role") not in {
            "user",
            "assistant",
            "tool",
        }:
            continue
        try:
            history.append(RuntimeHistoryMessage.model_validate(item))
        except ValueError:
            continue
    return history


def _build_tool_output_artifact_writer(
    chat_store: Any,
    *,
    session_id: str,
    run_id: str,
):
    store_tool_output = getattr(chat_store, "store_tool_output", None)
    if not callable(store_tool_output):
        return None

    def write(tool_call_id: str | None, output: str):
        return store_tool_output(
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            output=output,
        )

    return write


def _build_deterministic_artifact_writer(
    chat_store: Any,
    *,
    session_id: str,
    run_id: str,
    execution_store: Any,
    turn_id: int,
):
    store_tool_output = getattr(chat_store, "store_tool_output", None)
    delete_tool_output = getattr(chat_store, "delete_tool_output", None)
    if not callable(store_tool_output):
        return None

    lock = threading.Lock()
    output_refs: set[str] = set()
    reserved_refs: set[str] = set()
    prepared_refs: dict[str, str] = {}
    closed = False
    reserve_artifacts = getattr(
        execution_store,
        "reserve_deterministic_artifacts",
        None,
    )
    release_artifacts = getattr(
        execution_store,
        "release_deterministic_artifacts",
        None,
    )
    journal_enabled = callable(reserve_artifacts) and callable(release_artifacts)

    def prepare_artifacts(names) -> None:
        normalized = [str(name or "").strip() for name in names]
        if not normalized:
            return
        if any(not name for name in normalized) or len(set(normalized)) != len(
            normalized
        ):
            raise ValueError("deterministic artifact names are invalid")
        with lock:
            if closed:
                raise RuntimeError("deterministic artifact writer is closed")
            allocated = {name: uuid.uuid4().hex for name in normalized}
            if journal_enabled:
                execution_store.reserve_deterministic_artifacts(
                    session_id,
                    turn_id,
                    run_id,
                    list(allocated.values()),
                )
            prepared_refs.update(allocated)
            reserved_refs.update(allocated.values())

    def write(name: str, output: str, media_type: str):
        nonlocal closed
        with lock:
            if closed:
                raise RuntimeError("deterministic artifact writer is closed")
            reserved_ref = prepared_refs.pop(str(name), "")
            stored = store_tool_output(
                session_id=session_id,
                run_id=run_id,
                tool_call_id="deterministic:{}".format(name),
                output=output,
                content_type=media_type,
                **(
                    {"output_ref": reserved_ref}
                    if journal_enabled and reserved_ref
                    else {}
                ),
            )
            if isinstance(stored, Mapping):
                output_ref = str(stored.get("output_ref") or "").strip()
                if output_ref:
                    output_refs.add(output_ref)
            if journal_enabled and (
                not isinstance(stored, Mapping)
                or str(stored.get("output_ref") or "").strip() != reserved_ref
            ):
                raise RuntimeError(
                    "deterministic artifact store changed its reserved output_ref"
                )
            return stored

    def rollback(refs=()) -> None:
        nonlocal closed
        with lock:
            closed = True
            candidates = set(reserved_refs)
            candidates.update(output_refs)
            candidates.update(str(item or "").strip() for item in refs)
            deleted_refs: set[str] = set()
            failed_refs: set[str] = set()
            for output_ref in candidates:
                if not output_ref or not callable(delete_tool_output):
                    if output_ref:
                        failed_refs.add(output_ref)
                    continue
                try:
                    deleted = bool(delete_tool_output(
                        session_id=session_id,
                        run_id=run_id,
                        output_ref=output_ref,
                        missing_ok=True,
                    ))
                except Exception:
                    deleted = False
                    logger.warning(
                        "Failed to rollback deterministic artifact run=%s",
                        run_id,
                        exc_info=True,
                    )
                if deleted:
                    deleted_refs.add(output_ref)
                else:
                    failed_refs.add(output_ref)
            released = deleted_refs
            if journal_enabled and released:
                execution_store.release_deterministic_artifacts(
                    session_id,
                    run_id,
                    sorted(released),
                )
            output_refs.clear()
            output_refs.update(failed_refs)
            reserved_refs.intersection_update(failed_refs)
            prepared_refs.clear()

    setattr(write, "rollback", rollback)
    setattr(write, "cancel_and_rollback", rollback)
    setattr(write, "prepare_artifacts", prepare_artifacts)
    return write


def _history_with_turn(
    context: RuntimeInvocationContext,
    content: str,
) -> list[dict[str, Any]]:
    history = [
        item.model_dump(mode="python", exclude_none=True)
        for item in context.history_messages
    ]
    history.append({"role": "user", "content": context.query})
    if content:
        history.append({"role": "assistant", "content": content})
    return history


def _deterministic_final_output(envelope: Mapping[str, Any]) -> dict[str, Any]:
    """Add a readable projection while retaining the deterministic envelope."""

    structured = envelope.get("structured_result")
    text = ""
    if isinstance(structured, Mapping):
        for key in ("text", "formatted_result", "message", "result"):
            value = structured.get(key)
            if isinstance(value, str) and value.strip():
                text = value.strip()
                break
    if not text:
        text = json.dumps(structured, ensure_ascii=False, sort_keys=True, indent=2)
    return {**dict(envelope), "text": text}


def _is_deterministic_result(result: RuntimeResult) -> bool:
    structured = result.structured_content
    execution = (
        structured.get("execution") if isinstance(structured, Mapping) else None
    )
    return bool(
        isinstance(execution, Mapping)
        and str(execution.get("type") or "") == "deterministic"
    )


def _direct_terminal_event(
    context: RuntimeInvocationContext,
    *,
    status: str,
    content: str,
    error: RuntimeErrorInfo | None = None,
    structured_content: dict[str, Any] | None = None,
) -> RuntimeEvent:
    payload: dict[str, Any] = {
        "status": status,
        "content": content,
        "history_messages": _history_with_turn(context, content),
        "read_documents": dict(context.read_documents or {}),
    }
    if error is not None:
        payload["error"] = error.model_dump(mode="json")
    if structured_content is not None:
        payload["structured_content"] = structured_content
    return RuntimeEvent(
        event_type="turn.finished",
        status=status,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        source={"node": "direct_execution", "component": "service"},
        payload=payload,
    )


def _solver_runtime_event(
    context: RuntimeInvocationContext,
    event_type: str,
    payload: Mapping[str, Any],
) -> RuntimeEvent:
    statuses = {
        "solver.input.awaiting_confirmation": "awaiting_input",
        "solver.progress": "running",
        "solver.verification.completed": "completed",
        "solver.verification.rejected": "failed",
        "solver.result.authoritative": "completed",
        "tool.progress": "running",
    }
    return RuntimeEvent(
        event_type=event_type,
        status=statuses.get(event_type, "running"),
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        source={
            "node": "solver_recovery",
            "component": (
                "tool" if event_type.startswith("tool.") else "solver"
            ),
        },
        payload=dict(payload or {}),
    )


def _solver_terminal_event(
    context: RuntimeInvocationContext,
    *,
    state: Mapping[str, Any],
    result: Mapping[str, Any],
) -> RuntimeEvent:
    final_output = state.get("final_output")
    if isinstance(final_output, str):
        content = final_output
    elif final_output in (None, ""):
        content = ""
    else:
        content = json.dumps(final_output, ensure_ascii=False, default=str)
    history_messages = []
    for message in list(state.get("messages") or []):
        if isinstance(message, Mapping):
            history_messages.append(dict(message))
    if content:
        history_messages.append({"role": "assistant", "content": content})
    public_result = dict(result)
    raw_status = str(public_result.get("status") or "").strip().lower()
    if public_result.get("authoritative") is True:
        status = "completed"
        error = None
    elif raw_status == "cancelled":
        status = "cancelled"
        error = None
    else:
        status = "failed"
        code = str(
            public_result.get("code") or "solver_result_rejected"
        ).strip()
        error = RuntimeErrorInfo(
            error_code=code,
            message=content or "The Solver result was not authoritative.",
            retriable=bool(public_result.get("retryable")),
            source="tool",
            details={
                "phase": str(public_result.get("phase") or "").strip(),
            },
        )
    payload: dict[str, Any] = {
        "status": status,
        "content": content,
        "structured_content": {
            "text": content,
            "solver": public_result,
        },
        "history_messages": history_messages,
        "read_documents": dict(state.get("read_documents") or {}),
    }
    if error is not None:
        payload["error"] = error.model_dump(mode="json")
    return RuntimeEvent(
        event_type="turn.finished",
        status=status,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        source={"node": "solver_recovery", "component": "solver"},
        payload=payload,
    )


def _runtime_event_from_legacy(
    value: Any,
    context: RuntimeInvocationContext,
) -> RuntimeEvent | None:
    if not isinstance(value, Mapping):
        return None
    data = value.get("data") if isinstance(value.get("data"), Mapping) else value
    event_type = str(data.get("type") or "")
    if event_type not in {"tool.started", "tool.finished"}:
        return None
    payload = dict(data.get("payload") or {})
    raw_status = str(payload.get("status") or "").strip().lower()
    status = "running" if event_type == "tool.started" else (
        "failed" if raw_status in {"error", "failed"} else "completed"
    )
    return RuntimeEvent(
        event_type=event_type,
        status=status,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        source={"node": "direct_execution", "component": "service"},
        payload=payload,
        span_key=str(data.get("span_key") or "") or None,
        parent_key=str(data.get("parent_key") or "") or None,
        trace_id=str(data.get("trace_id") or "") or None,
        span_id=str(data.get("span_id") or "") or None,
        parent_span_id=str(data.get("parent_span_id") or "") or None,
    )


def _chat_event_envelope(event: PublicEvent) -> dict[str, Any]:
    try:
        created_at = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        timestamp_ms = int(created_at.timestamp() * 1000)
    except (TypeError, ValueError):
        timestamp_ms = int(time.time() * 1000)
    data = {
        "protocol": "agent-stream.v2",
        "event_id": event.event_id,
        "seq": event.seq,
        "ts": timestamp_ms,
        "session_id": event.session_id,
        "run_id": event.run_id,
        "turn_id": event.turn_id,
        "type": event.event_type,
        "source": dict(event.source or {}),
        "payload": dict(event.payload),
    }
    for key in (
        "span_key",
        "parent_key",
        "trace_id",
        "span_id",
        "parent_span_id",
    ):
        value = getattr(event, key, None)
        if value:
            data[key] = value
    return {
        "event": "agent.v2",
        "data": data,
    }


def _resolve_workspace_scope() -> Any:
    global _WORKSPACE_SCOPE, _WORKSPACE_SCOPE_RESOLVED
    if _WORKSPACE_SCOPE_RESOLVED:
        return _WORKSPACE_SCOPE
    try:
        from workspace.workspaces import workspace_scope
    except ImportError:
        workspace_scope = None
    _WORKSPACE_SCOPE = workspace_scope
    _WORKSPACE_SCOPE_RESOLVED = True
    return _WORKSPACE_SCOPE


def _workspace_context(workspace_id: str):
    """Use the real Workspace ContextVar when the web app is installed.

    Runtime unit tests and small protocol tools can run without the ASGI app;
    in that environment there is no request-scoped workspace module to bind.
    """
    workspace_scope = _resolve_workspace_scope()
    if workspace_scope is None:
        return nullcontext()
    return workspace_scope(workspace_id)


def _normalize_llm_request_mode(value: Any, *, default: str = "stream") -> str:
    """Normalize LLM transport mode without importing the web-facing LLM package."""
    default_mode = "sync" if str(default or "").strip().lower() == "sync" else "stream"
    raw = str(value or "").strip().lower()
    if raw in {"stream", "streaming", "sse", "true", "1", "on", "yes"}:
        return "stream"
    if raw in {
        "sync",
        "non-stream",
        "non_stream",
        "nonstream",
        "false",
        "0",
        "off",
        "no",
    }:
        return "sync"
    return default_mode


def _error_from_payload(value: Any) -> RuntimeErrorInfo:
    if isinstance(value, RuntimeErrorInfo):
        return value
    if isinstance(value, Mapping):
        try:
            return RuntimeErrorInfo.model_validate(value)
        except ValueError:
            pass
    return RuntimeErrorInfo(
        error_code="INTERNAL_ERROR",
        message="Agent Runtime 执行失败",
        retriable=False,
        source="runtime",
    )


def _result_from_terminal(
    event: RuntimeEvent,
    adapter: PublicEventAdapter,
) -> RuntimeResult:
    payload = adapter.adapt_runtime_result_payload(event)
    status = event.status
    history: list[RuntimeHistoryMessage] = []
    for item in event.payload.get("history_messages") or []:
        try:
            history.append(RuntimeHistoryMessage.model_validate(item))
        except ValueError:
            continue
    read_documents = (
        dict(event.payload["read_documents"])
        if isinstance(event.payload.get("read_documents"), Mapping)
        else None
    )
    if status in {"completed", "awaiting_input"}:
        return RuntimeResult(
            status=status,
            content=payload.get("content"),
            structured_content=payload.get("structured_content"),
            usage=payload.get("usage"),
            trace=payload.get("trace"),
            history_messages=history,
            read_documents=read_documents,
            evolution_recall_records=[
                dict(item)
                for item in event.payload.get("evolution_recall_records") or []
                if isinstance(item, Mapping)
            ],
        )
    recovery = None
    if status == "failed" and isinstance(payload.get("recovery"), Mapping):
        try:
            recovery = RuntimeRecoveryAction.model_validate(payload["recovery"])
        except ValueError:
            recovery = None
    return RuntimeResult(
        status=status,
        content=(str(payload.get("content")) if payload.get("content") is not None else None),
        structured_content=(
            payload.get("structured_content")
            if isinstance(payload.get("structured_content"), (dict, list))
            else None
        ),
        error=(
            _error_from_payload(payload.get("error"))
            if status == "failed"
            else None
        ),
        recovery=recovery,
        history_messages=history,
        read_documents=read_documents,
    )


def _terminal_draft_from_result(
    context: RuntimeInvocationContext,
    result: RuntimeResult,
    *,
    source: dict[str, Any],
) -> PublicEventDraft:
    payload: dict[str, Any] = {"status": result.status}
    if result.content is not None:
        payload["content"] = result.content
    if result.structured_content is not None:
        payload["structured_content"] = result.structured_content
    if result.usage is not None:
        payload["usage"] = result.usage
    if result.error is not None:
        payload["error"] = result.error.model_dump(mode="json")
    if result.recovery is not None:
        payload["recovery"] = result.recovery.model_dump(mode="json")
    return PublicEventDraft(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        event_type="turn.finished",
        status=result.status,
        source=source,
        payload=payload,
    )


class _PublicRuntimeEventSink(RuntimeEventSink):
    def __init__(
        self,
        adapter: PublicEventAdapter,
        sink: PublicEventSink,
    ) -> None:
        self.adapter = adapter
        self.sink = sink
        self.interaction_drafts: list[PublicEventDraft] = []
        self.terminal_draft: PublicEventDraft | None = None

    async def emit(self, event: RuntimeEvent) -> None:
        draft = self.adapter.adapt(event)
        if draft is None:
            return
        if event.event_type == "interaction.requested":
            if self.interaction_drafts:
                raise _MultipleInteractionsRequested
            self.interaction_drafts.append(draft)
            return
        if event.event_type == "turn.finished":
            if self.terminal_draft is not None:
                raise RuntimeError("Agent Runtime 产生了多个终态事件")
            self.terminal_draft = draft
            return
        await self.sink.emit(draft)

    async def emit_deferred(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> bool:
        if result.status == "awaiting_input":
            for draft in self.interaction_drafts:
                await self.sink.emit(draft)
        if self.terminal_draft is not None:
            terminal_draft = self.terminal_draft
            if result.status != terminal_draft.status:
                terminal_draft = _terminal_draft_from_result(
                    context,
                    result,
                    source=terminal_draft.source,
                )
            await self.sink.emit(terminal_draft)
            return True
        return False


class AsyncExecutionService:
    """Select an A runtime entry and publish only adapted public drafts."""

    def __init__(
        self,
        runtime: AsyncAgentRuntime,
        session_adapter: "OrchestratorSessionAdapter | None" = None,
    ) -> None:
        self.runtime = runtime
        self.session_adapter = session_adapter

    async def prepare_context(
        self,
        context: RuntimeInvocationContext,
    ) -> RuntimeInvocationContext:
        if self.session_adapter is None:
            return context
        return await self.session_adapter.prepare(context)

    async def execute(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
    ) -> RuntimeResult:
        adapter = PublicEventAdapter(include_trace=context.include_trace)
        resumed_solver_events = None
        if self.session_adapter is not None:
            resumed_solver_events = await self.session_adapter.try_resume_solver(
                context
            )
        if resumed_solver_events is not None:
            return await self._consume_events(
                context,
                sink,
                adapter,
                resumed_solver_events,
            )
        direct_events = None
        arguments = dict(context.arguments or {})
        selected_ontology_id = str(
            arguments.get("selected_ontology_id") or ""
        ).strip()
        if self.session_adapter is not None and selected_ontology_id:
            direct_events = await self.session_adapter.try_direct_execute(context)
        if direct_events is not None:
            return await self._consume_events(
                context,
                sink,
                adapter,
                direct_events,
            )

        execution_scope = (
            self.session_adapter.execution_scope(context)
            if self.session_adapter is not None
            else nullcontext()
        )
        with execution_scope:
            return await self._execute_runtime(context, sink, adapter)

    async def _execute_runtime(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
        adapter: PublicEventAdapter,
    ) -> RuntimeResult:
        if context.llm_request_mode == "stream":
            terminal: RuntimeEvent | None = None
            interaction_drafts: list[PublicEventDraft] = []
            try:
                async for event in self.runtime.astream(context):
                    draft = adapter.adapt(event)
                    if draft is not None and event.event_type == "interaction.requested":
                        if interaction_drafts:
                            raise _MultipleInteractionsRequested
                        interaction_drafts.append(draft)
                    elif draft is not None and event.event_type != "turn.finished":
                        await sink.emit(draft)
                    if event.event_type == "turn.finished":
                        if terminal is not None:
                            raise RuntimeError("Agent Runtime 产生了多个终态事件")
                        terminal = event
            except _MultipleInteractionsRequested:
                return await self._finish_multiple_interactions(context, sink)
            if terminal is None:
                result = RuntimeResult(
                    status="failed",
                    error=RuntimeErrorInfo(
                        error_code="RUNTIME_MISSING_TERMINAL",
                        message="Agent Runtime 未产生终态事件",
                        retriable=False,
                        source="runtime",
                    ),
                )
            else:
                result = _result_from_terminal(terminal, adapter)
            result = await self._finalize(context, result)
            if result.status == "awaiting_input":
                for interaction_draft in interaction_drafts:
                    await sink.emit(interaction_draft)
            if terminal is not None:
                if result.status == terminal.status:
                    terminal_draft = adapter.adapt(terminal)
                else:
                    terminal_draft = _terminal_draft_from_result(
                        context,
                        result,
                        source={
                            "node": "consumer",
                            "component": "execution_service",
                        },
                    )
                if terminal_draft is not None:
                    await sink.emit(terminal_draft)
                    await self._submit_reflection(context, result)
            return result

        runtime_sink = _PublicRuntimeEventSink(adapter, sink)
        try:
            result = await self.runtime.arun(
                context,
                runtime_sink,
            )
        except _MultipleInteractionsRequested:
            return await self._finish_multiple_interactions(context, sink)
        result = await self._finalize(context, result)
        terminal_emitted = await runtime_sink.emit_deferred(context, result)
        if terminal_emitted:
            await self._submit_reflection(context, result)
        return result

    async def _finish_multiple_interactions(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
    ) -> RuntimeResult:
        error = RuntimeErrorInfo(
            error_code="MULTIPLE_INTERACTIONS_NOT_ALLOWED",
            message="one turn cannot request more than one interaction",
            retriable=False,
            source="runtime",
        )
        result = RuntimeResult(status="failed", error=error)
        result = await self._finalize(context, result)
        await sink.emit(
            PublicEventDraft(
                run_id=context.run_id,
                session_id=context.session_id,
                turn_id=context.turn_id,
                event_type="turn.finished",
                status="failed",
                source={"node": "consumer", "component": "execution_service"},
                payload={
                    "status": "failed",
                    "error": error.to_dict(),
                },
            )
        )
        await self._submit_reflection(context, result)
        return result

    async def _consume_events(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
        adapter: PublicEventAdapter,
        events: list[RuntimeEvent],
    ) -> RuntimeResult:
        terminal: RuntimeEvent | None = None
        interaction_drafts: list[PublicEventDraft] = []
        for event in events:
            if event.event_type == "turn.finished":
                if terminal is not None:
                    raise RuntimeError("直接执行产生了多个终态事件")
                terminal = event
                continue
            draft = adapter.adapt(event)
            if draft is not None:
                if event.event_type == "interaction.requested":
                    if interaction_drafts:
                        return await self._finish_multiple_interactions(
                            context,
                            sink,
                        )
                    interaction_drafts.append(draft)
                else:
                    await sink.emit(draft)
        if terminal is None:
            result = RuntimeResult(
                status="failed",
                error=RuntimeErrorInfo(
                    error_code="RUNTIME_MISSING_TERMINAL",
                    message="直接执行未产生终态事件",
                    retriable=False,
                    source="runtime",
                ),
            )
        else:
            result = _result_from_terminal(terminal, adapter)
        result = await self._finalize(context, result)
        if result.status == "awaiting_input":
            for interaction_draft in interaction_drafts:
                await sink.emit(interaction_draft)
        if terminal is not None:
            if result.status == terminal.status:
                terminal_draft = adapter.adapt(terminal)
            else:
                terminal_draft = _terminal_draft_from_result(
                    context,
                    result,
                    source={
                        "node": "consumer",
                        "component": "execution_service",
                    },
                )
            await sink.emit(terminal_draft)
            await self._submit_reflection(context, result)
        return result

    async def cancel(self, run_id: str, reason: str) -> bool:
        return await self.runtime.cancel(run_id, reason)

    async def _finalize(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> RuntimeResult:
        if (
            result.status == "awaiting_input"
            and not context.supports_user_interaction
        ):
            result = result.model_copy(
                update={
                    "status": "failed",
                    "content": None,
                    "structured_content": None,
                    "history_messages": [],
                    "error": RuntimeErrorInfo(
                        error_code="INTERACTION_NOT_SUPPORTED",
                        message="当前协议不支持用户交互",
                        retriable=False,
                        source="runtime",
                    ),
                }
            )
        if self.session_adapter is not None:
            await self.session_adapter.finalize(context, result)
        return result

    async def _submit_reflection(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> None:
        if self.session_adapter is not None:
            await self.session_adapter.submit_reflection(context, result)

    async def finalize_aborted(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> None:
        """Persist a phase that B terminated before A returned a result."""
        await self._finalize(context, result)

    async def persist_public_event(self, event: PublicEvent) -> None:
        if self.session_adapter is None:
            return
        await self.session_adapter.persist_event(event)


class OrchestratorSessionAdapter:
    """Reuse consumer stores without invoking the synchronous orchestrator."""

    def __init__(self, orchestrator_provider: Callable[[], Any]) -> None:
        self._orchestrator_provider = orchestrator_provider
        import threading

        self._finalize_lock = threading.Lock()
        self._context_lock = threading.Lock()
        self._run_contexts: dict[str, RuntimeInvocationContext] = {}
        # A turn is allocated before the remaining session projections are
        # written. Keep that allocation visible to the prepare wrapper so a
        # later prepare failure can compensate it by run_id.
        self._pending_prepare_turns: dict[
            str, tuple[RuntimeInvocationContext, int]
        ] = {}

    def memory_manager(self, context: RuntimeInvocationContext) -> Any:
        orchestrator = self._orchestrator_provider()
        return orchestrator._get_memory_manager(context.username)

    @contextmanager
    def execution_scope(self, context: RuntimeInvocationContext):
        """Bind workspace and artifact persistence for the whole Agent task."""
        from agent.runtime.jiuwen import solver_state_snapshot_callback_context

        chat_store, execution_store = self._stores(context)
        artifact_writer = _build_tool_output_artifact_writer(
            chat_store,
            session_id=context.session_id,
            run_id=context.run_id,
        )
        artifact_scope = (
            tool_output_artifact_writer_context(artifact_writer)
            if tool_output_artifact_writer_context is not None
            else nullcontext()
        )
        snapshot_scope = solver_state_snapshot_callback_context(
            self._solver_snapshot_callback(context, execution_store)
        )
        with _workspace_context(context.workspace_id), artifact_scope, snapshot_scope:
            yield

    def _solver_snapshot_callback(
        self,
        context: RuntimeInvocationContext,
        execution_store: Any,
    ) -> Callable[[Mapping[str, Any]], Any]:
        async def persist(snapshot: Mapping[str, Any]) -> None:
            await run_sync_compat(
                self._persist_solver_snapshot_sync,
                context,
                execution_store,
                snapshot,
                run_id=context.run_id,
                task_name="solver-checkpoint-persist",
                offload_kind="short",
            )

        return persist

    @staticmethod
    def _persist_solver_snapshot_sync(
        context: RuntimeInvocationContext,
        execution_store: Any,
        state: Mapping[str, Any],
    ) -> None:
        checkpoint = _waiting_solver_checkpoint_from_state(state)
        if checkpoint is not None:
            checkpoint_state = checkpoint.get("state")
            checkpoint_state = (
                checkpoint_state
                if isinstance(checkpoint_state, Mapping)
                else {}
            )
            identity = (
                str(checkpoint_state.get("run_id") or ""),
                str(checkpoint_state.get("session_id") or ""),
                int(checkpoint_state.get("current_turn_id") or 0),
                str(checkpoint_state.get("workspace_id") or ""),
                str(checkpoint_state.get("user_id") or ""),
            )
            expected = (
                context.run_id,
                context.session_id,
                context.turn_id,
                context.workspace_id,
                context.username,
            )
            if identity != expected:
                raise ValueError("solver checkpoint identity drifted")
            execution_store.save_waiting_solver_checkpoint(
                context.session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
                checkpoint=checkpoint,
            )
            return
        existing = execution_store.get_waiting_solver_checkpoint(
            context.session_id,
            run_id=context.run_id,
        )
        if existing is not None:
            execution_store.complete_waiting_solver_checkpoint(
                context.session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
            )

    async def try_resume_solver(
        self,
        context: RuntimeInvocationContext,
    ) -> list[RuntimeEvent] | None:
        return await run_sync_compat(
            self._resume_solver_sync,
            context,
            run_id=context.run_id,
            task_name="solver-checkpoint-resume",
            offload_kind="tool",
        )

    def _resume_solver_sync(
        self,
        context: RuntimeInvocationContext,
    ) -> list[RuntimeEvent] | None:
        _chat_store, execution_store = self._stores(context)
        record = execution_store.get_waiting_solver_checkpoint(
            context.session_id,
            run_id=context.run_id,
        )
        if not isinstance(record, Mapping):
            return None
        checkpoint = record.get("checkpoint")
        if not isinstance(checkpoint, Mapping):
            raise ValueError("waiting Solver checkpoint is invalid")
        state = checkpoint.get("state")
        state = state if isinstance(state, Mapping) else {}
        identity = (
            str(state.get("run_id") or ""),
            str(state.get("session_id") or ""),
            int(state.get("current_turn_id") or 0),
            str(state.get("workspace_id") or ""),
            str(state.get("user_id") or ""),
        )
        expected = (
            context.run_id,
            context.session_id,
            context.turn_id,
            context.workspace_id,
            context.username,
        )
        if identity != expected:
            raise ValueError("waiting Solver checkpoint identity drifted")
        call = checkpoint.get("tool_call")
        if not isinstance(call, Mapping):
            raise ValueError("waiting Solver ToolCall is invalid")
        expected_tool_call_id = str(call.get("id") or "").strip()
        tool_name = str(call.get("name") or "").strip()
        if not expected_tool_call_id or not tool_name:
            raise ValueError("waiting Solver ToolCall is invalid")

        inbox = record.get("tool_result_inbox")
        if isinstance(inbox, Mapping) and str(
            inbox.get("tool_call_id") or ""
        ).strip() != expected_tool_call_id:
            execution_store.save_waiting_solver_checkpoint(
                context.session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
                checkpoint=dict(checkpoint),
            )
            inbox = None

        progress: list[tuple[str, dict[str, Any]]] = []
        if not isinstance(inbox, Mapping):
            from agent.tools.solver import (
                dispatch_solver_checkpoint_tool,
                recover_solver_tool_result,
            )

            def record_progress(
                event_type: str,
                payload: Mapping[str, Any],
            ) -> None:
                public = dict(payload or {})
                public.update(
                    {
                        "tool_call_id": expected_tool_call_id,
                        "tool_name": tool_name,
                    }
                )
                progress.append((event_type, public))

            recovered = recover_solver_tool_result(
                workspace_id=context.workspace_id,
                owner_id=context.username,
                run_id=context.run_id,
                turn_id=context.turn_id,
                tool_call_id=expected_tool_call_id,
                card_name=tool_name,
                progress_callback=record_progress,
            )
            recovery_state = (
                str(recovered.get("state") or "").strip()
                if isinstance(recovered, Mapping)
                else ""
            )
            if recovery_state == "pending":
                raise RuntimeError("waiting Solver ToolResult is not ready")
            if recovery_state == "conflict":
                raise RuntimeError(
                    "waiting Solver ToolCall identity is inconsistent"
                )
            if recovery_state == "missing" or not isinstance(recovered, Mapping):
                recovered = dispatch_solver_checkpoint_tool(
                    workspace_id=context.workspace_id,
                    owner_id=context.username,
                    session_id=context.session_id,
                    run_id=context.run_id,
                    turn_id=context.turn_id,
                    checkpoint=checkpoint,
                    progress_callback=record_progress,
                )
            if not isinstance(recovered, Mapping):
                raise RuntimeError("waiting Solver ToolResult is not ready")
            inbox = execution_store.put_solver_tool_result(
                context.session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
                tool_call_id=expected_tool_call_id,
                result_digest=str(recovered.get("result_digest") or ""),
                result=dict(recovered.get("result") or {}),
            )

        resumed_state = _append_solver_tool_message(checkpoint, inbox)
        from agent.core.shared.state_updates import apply_state_updates_to_state
        from agent.tools.solver import project_solver_tool_result

        result_payload = inbox.get("result")
        if not isinstance(result_payload, Mapping):
            raise ValueError("waiting Solver ToolResult is invalid")
        solver_outcome = project_solver_tool_result(dict(result_payload))
        if solver_outcome.state_updates:
            apply_state_updates_to_state(
                resumed_state,
                dict(solver_outcome.state_updates),
            )
        if not solver_outcome.terminal:
            raise RuntimeError("waiting Solver ToolResult is not terminal")
        execution_store.save_waiting_solver_checkpoint(
            context.session_id,
            run_id=context.run_id,
            turn_id=context.turn_id,
            checkpoint={
                "schema_version": "agent.solver-checkpoint.v1",
                "stage": "react",
                "tool_call": copy.deepcopy(dict(call)),
                "state": resumed_state,
            },
        )

        events: list[RuntimeEvent] = []
        for event_type, payload in progress:
            if event_type not in {
                "solver.input.awaiting_confirmation",
                "solver.progress",
                "solver.verification.completed",
                "solver.verification.rejected",
                "solver.result.authoritative",
            }:
                event_type = "solver.progress"
            events.append(_solver_runtime_event(context, event_type, payload))
            events.append(_solver_runtime_event(context, "tool.progress", payload))
        authoritative_payload = dict(result_payload)
        authoritative_payload.update(
            {
                "tool_call_id": expected_tool_call_id,
                "tool_name": tool_name,
            }
        )
        if authoritative_payload.get("authoritative") is True and not any(
            event.event_type == "solver.result.authoritative"
            for event in events
        ):
            events.append(
                _solver_runtime_event(
                    context,
                    "solver.result.authoritative",
                    authoritative_payload,
                )
            )
        events.append(
            _solver_terminal_event(
                context,
                state=resumed_state,
                result=result_payload,
            )
        )
        return events

    async def prepare(
        self,
        context: RuntimeInvocationContext,
    ) -> RuntimeInvocationContext:
        from agent.core.shared.modes import VALID_AGENT_MODES
        from config.model_profile_refs import model_profile_ref_to_dict
        from config.system_config import resolve_effective_llm_profile_config

        _resolve_workspace_scope()
        try:
            prepared = await run_sync_compat(
                self._prepare_sync,
                context,
                VALID_AGENT_MODES,
                model_profile_ref_to_dict,
                resolve_effective_llm_profile_config,
                run_id=context.run_id,
                task_name="consumer-session-prepare",
            )
        except BaseException:
            self._abort_pending_prepare(context.run_id)
            raise
        with self._context_lock:
            self._pending_prepare_turns.pop(context.run_id, None)
        return prepared

    def _prepare_sync(
        self,
        context: RuntimeInvocationContext,
        valid_agent_modes: set[str] | frozenset[str],
        model_profile_ref_to_dict: Callable[[Any], Any],
        resolve_effective_llm_profile_config: Callable[..., Any],
    ) -> RuntimeInvocationContext:
        try:
            return self._prepare_sync_impl(
                context,
                valid_agent_modes,
                model_profile_ref_to_dict,
                resolve_effective_llm_profile_config,
            )
        except Exception:
            self._abort_pending_prepare(context.run_id)
            raise

    def _abort_pending_prepare(self, run_id: str) -> None:
        with self._context_lock:
            pending = self._pending_prepare_turns.pop(run_id, None)
            self._run_contexts.pop(run_id, None)
        if pending is None:
            return
        context, turn_id = pending
        chat_store = None
        execution_store = None
        try:
            chat_store, execution_store = self._stores(context)
            with _workspace_context(context.workspace_id):
                execution_store.fail_turn(
                    context.session_id,
                    turn_id,
                    error="会话运行状态准备失败",
                    run_id=context.run_id,
                )
        except Exception:
            logger.warning(
                "Failed to mark prepared turn as failed run_id=%s",
                run_id,
                exc_info=True,
            )
        try:
            if chat_store is None:
                chat_store, _execution_store = self._stores(context)
            with _workspace_context(context.workspace_id):
                get_active_run = getattr(chat_store, "get_active_run", None)
                clear_active_run = getattr(chat_store, "clear_active_run", None)
                if callable(clear_active_run) and (
                    not callable(get_active_run)
                    or get_active_run(context.session_id) in {None, "", context.run_id}
                ):
                    clear_active_run(context.session_id)
        except Exception:
            logger.warning(
                "Failed to clear prepared active run run_id=%s",
                run_id,
                exc_info=True,
            )

    async def finalize(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> None:
        _resolve_workspace_scope()
        parse_execution_summary = None
        if result.status == "completed" and result.content:
            try:
                from agent.evolution import parse_execution_summary
            except ImportError:
                parse_execution_summary = None
        await run_sync_compat(
            self._finalize_sync,
            context,
            result,
            parse_execution_summary,
            run_id=context.run_id,
            task_name="consumer-session-finalize",
        )

    async def submit_reflection(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> None:
        if result.status != "completed":
            return
        _chat_store, execution_store = self._stores(context)
        get_turn = getattr(execution_store, "get_turn", None)
        if not callable(get_turn):
            return
        turn = await run_sync_compat(
            get_turn,
            context.session_id,
            context.turn_id,
            run_id=context.run_id,
            task_name="reflection-terminal-check",
        )
        if not isinstance(turn, Mapping) or str(turn.get("status") or "") != "success":
            return
        orchestrator = self._orchestrator_provider()
        submit = getattr(orchestrator, "_submit_reflection", None)
        if not callable(submit):
            return
        arguments = dict(context.arguments or {})
        ontology_id = str(
            arguments.get("selected_ontology_id") or ""
        ).strip()
        if not ontology_id:
            return
        history_source = result.history_messages or context.history_messages
        messages = [
            message.model_dump(mode="python", exclude_none=True)
            for message in history_source
        ]
        if not result.history_messages:
            messages.append({"role": "user", "content": context.query})
            if result.content:
                messages.append(
                    {"role": "assistant", "content": result.content}
                )
        await submit(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            user_id=context.username,
            workspace_id=context.workspace_id,
            ontology_id=ontology_id,
            status=result.status,
            user_request=context.query,
            final_output=result.content,
            messages=messages,
            duration_ms=0,
        )

    async def try_direct_execute(
        self,
        context: RuntimeInvocationContext,
    ) -> list[RuntimeEvent] | None:
        orchestrator = self._orchestrator_provider()
        agent_config = getattr(orchestrator, "agent_config", None)
        direct_config = getattr(agent_config, "direct_execution", None)
        from agent.direct_execution.contracts import NotApplicable
        from agent.direct_execution.service import DeterministicExecutionService
        from agent.direct_execution import OperationDirectRouter
        from agent.direct_execution.models import RuntimeContext
        from agent.direct_execution.runner import run_direct_script
        from agent.tools.events import tool_output_artifact_writer_context
        from config.system_config import load_system_config

        dependencies = {
            "OperationDirectRouter": OperationDirectRouter,
            "RuntimeContext": RuntimeContext,
            "run_direct_script": run_direct_script,
            "tool_output_artifact_writer_context": tool_output_artifact_writer_context,
            "load_system_config": load_system_config,
            "DeterministicExecutionService": DeterministicExecutionService,
            "NotApplicable": NotApplicable,
            "cancel_event": threading.Event(),
        }
        _resolve_workspace_scope()
        operation = asyncio.create_task(
            run_sync_compat(
                self._try_direct_execute_sync,
                context,
                dependencies,
                run_id=context.run_id,
                task_name="consumer-direct-execution",
            )
        )

        def rollback_cancelled_artifacts(_completed=None) -> None:
            writer = dependencies.get("artifact_writer")
            rollback = getattr(writer, "cancel_and_rollback", None)
            if callable(rollback):
                try:
                    rollback(())
                except Exception:
                    logger.warning(
                        "Failed to rollback cancelled deterministic artifacts run=%s",
                        context.run_id,
                        exc_info=True,
                    )
            if _completed is not None:
                try:
                    _completed.exception()
                except (asyncio.CancelledError, Exception):
                    pass

        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            dependencies["cancel_event"].set()
            try:
                await asyncio.wait_for(
                    asyncio.shield(operation),
                    timeout=min(
                        10.0,
                        max(
                            1.0,
                            float(
                                getattr(
                                    agent_config,
                                    "execute_script_timeout",
                                    10,
                                )
                            ),
                        ),
                    ),
                )
            except TimeoutError:
                rollback_cancelled_artifacts()
                operation.add_done_callback(rollback_cancelled_artifacts)
            except Exception:
                rollback_cancelled_artifacts()
            else:
                rollback_cancelled_artifacts()
            raise

    async def persist_event(self, event: PublicEvent) -> None:
        terminal = event.event_type == "turn.finished"
        try:
            await run_sync_compat(
                self._persist_event_sync,
                event,
                run_id=event.run_id,
                task_name="consumer-event-persistence",
            )
        finally:
            if terminal:
                with self._context_lock:
                    self._run_contexts.pop(event.run_id, None)

    def _stores(self, context: RuntimeInvocationContext) -> tuple[Any, Any]:
        orchestrator = self._orchestrator_provider()
        return orchestrator._get_stores(context.username, context.workspace_id)

    def _prepare_sync_impl(
        self,
        context: RuntimeInvocationContext,
        valid_agent_modes: set[str] | frozenset[str],
        model_profile_ref_to_dict: Callable[[Any], Any],
        resolve_effective_llm_profile_config: Callable[..., Any],
    ) -> RuntimeInvocationContext:
        orchestrator = self._orchestrator_provider()
        chat_store, execution_store = self._stores(context)
        with _workspace_context(context.workspace_id):
            session = chat_store.get_session(context.session_id)
            session_metadata = dict(getattr(session, "metadata", {}) or {})
            turn_count = execution_store.get_turn_count(context.session_id)
            existing_turn = execution_store.get_turn(
                context.session_id,
                context.turn_id,
                run_id=context.run_id,
            )
            if existing_turn is not None:
                allocated_context = context.model_copy(
                    update={"turn_id": int(existing_turn["turn_id"])}
                )
                with self._context_lock:
                    self._pending_prepare_turns[context.run_id] = (
                        allocated_context,
                        int(existing_turn["turn_id"]),
                    )
            prior_turn_count = turn_count - (1 if existing_turn is not None else 0)

            locked_agent_mode = str(session_metadata.get("agent_mode") or "").strip().lower()
            if locked_agent_mode not in valid_agent_modes:
                locked_agent_mode = ""
            if locked_agent_mode and context.agent_mode != locked_agent_mode:
                raise ValueError(
                    "agent_mode is locked for this session: {}".format(
                        locked_agent_mode
                    )
                )
            effective_agent_mode = locked_agent_mode or context.agent_mode
            effective_llm_mode = _normalize_llm_request_mode(
                session_metadata.get("llm_request_mode"),
                default=context.llm_request_mode,
            )

            arguments = dict(context.arguments or {})
            requested_ontology_id = str(
                arguments.get("selected_ontology_id") or ""
            ).strip()
            requested_ontology_version = str(
                arguments.get("selected_ontology_version")
                or arguments.get("selected_version")
                or ""
            ).strip()
            locked_ontology_id = str(
                session_metadata.get("selected_ontology_id") or ""
            ).strip()
            locked_ontology_version = str(
                session_metadata.get("selected_ontology_version")
                or session_metadata.get("selected_version")
                or ""
            ).strip()
            if (
                locked_ontology_id
                and requested_ontology_id
                and requested_ontology_id != locked_ontology_id
            ):
                raise ValueError(
                    "selected_ontology_id is locked for this session: {}".format(
                        locked_ontology_id
                    )
                )
            if (
                locked_ontology_version
                and requested_ontology_version
                and requested_ontology_version != locked_ontology_version
            ):
                raise ValueError(
                    "selected_ontology_version is locked for this session: {}".format(
                        locked_ontology_version
                    )
                )
            if requested_ontology_id and prior_turn_count > 0 and not locked_ontology_id:
                raise ValueError(
                    "selected_ontology_id can only be set before the first user turn"
                )

            selected_ontology = None
            selected_ontology_id = locked_ontology_id or requested_ontology_id
            selected_ontology_version = (
                locked_ontology_version or requested_ontology_version
            )
            if selected_ontology_id:
                from ontology.ontologies import (
                    get_ontology_registry,
                    get_runtime_ontology_manifest,
                )

                if locked_ontology_id:
                    selected_ontology = get_runtime_ontology_manifest(
                        locked_ontology_id,
                        version=locked_ontology_version or None,
                        workspace_id=context.workspace_id,
                    )
                    if selected_ontology is None:
                        raise ValueError(
                            "Locked selected ontology is unavailable: {}".format(
                                locked_ontology_id
                            )
                        )
                elif requested_ontology_version:
                    selected_ontology = get_runtime_ontology_manifest(
                        selected_ontology_id,
                        version=requested_ontology_version,
                        workspace_id=context.workspace_id,
                    )
                    if selected_ontology is None:
                        raise ValueError(
                            "Invalid selected ontology version: {}@{}".format(
                                selected_ontology_id,
                                requested_ontology_version,
                            )
                        )
                    if not selected_ontology.enabled:
                        raise ValueError(
                            "Disabled selected_ontology_id: {}".format(
                                selected_ontology_id
                            )
                        )
                else:
                    registry = get_ontology_registry(workspace_id=context.workspace_id)
                    selected_ontology = registry.get_ontology(selected_ontology_id)
                    if selected_ontology is None:
                        raise ValueError(
                            "Invalid selected_ontology_id: {}".format(
                                selected_ontology_id
                            )
                        )
                    if not selected_ontology.enabled:
                        raise ValueError(
                            "Disabled selected_ontology_id: {}".format(
                                selected_ontology_id
                            )
                        )
                    selected_ontology = get_runtime_ontology_manifest(
                        selected_ontology.id,
                        version=(
                            str(
                                getattr(selected_ontology, "active_version", "")
                                or getattr(selected_ontology, "version", "")
                                or ""
                            ).strip()
                            or None
                        ),
                        workspace_id=context.workspace_id,
                    ) or selected_ontology
                selected_ontology_id = str(selected_ontology.id)
                selected_ontology_version = str(
                    getattr(selected_ontology, "version", "") or ""
                ).strip()
                arguments["selected_ontology_id"] = selected_ontology_id
                if selected_ontology_version:
                    arguments["selected_ontology_version"] = selected_ontology_version
                    arguments["selected_version"] = selected_ontology_version

            requested_profile = context.model_profile_id
            stored_profile = str(
                session_metadata.get("model_profile_id") or ""
            ).strip()
            profile_ref = requested_profile or stored_profile or None
            interaction_response = (
                dict(context.interaction_response)
                if context.interaction_response
                else None
            )
            deterministic_candidate = False
            if selected_ontology_id:
                from agent.direct_execution.contracts import (
                    DeterministicRequest,
                )
                from agent.direct_execution.models import RuntimeContext
                from agent.direct_execution.service import (
                    DeterministicExecutionService,
                )

                checkpoint = (
                    execution_store.get_pending_deterministic_checkpoint(
                        context.session_id
                    )
                    if interaction_response
                    else None
                )
                deterministic_candidate = (
                    DeterministicExecutionService().is_potentially_applicable(
                        DeterministicRequest(
                            user_request=context.query,
                            context={
                                "agent_mode": effective_agent_mode,
                                "llm_request_mode": effective_llm_mode,
                                "requested_operation_key": (
                                    context.requested_operation_key
                                ),
                                "supports_user_interaction": (
                                    context.supports_user_interaction
                                ),
                                **arguments,
                            },
                            arguments=arguments,
                            runtime_context=RuntimeContext(
                                session_id=context.session_id,
                                run_id=context.run_id,
                                turn_id=(
                                    int(existing_turn["turn_id"])
                                    if existing_turn is not None
                                    else turn_count + 1
                                ),
                                user_id=context.username,
                                workspace_id=context.workspace_id,
                                selected_ontology_id=selected_ontology_id,
                                selected_ontology_version=(
                                    selected_ontology_version or None
                                ),
                            ),
                            checkpoint=checkpoint,
                            interaction_response=interaction_response,
                        )
                    )
                )
            selected_profile = None
            if hasattr(orchestrator, "agent_config") and not deterministic_candidate:
                selected_profile = resolve_effective_llm_profile_config(
                    profile_ref,
                    workspace_id=context.workspace_id,
                    allow_default_fallback=not bool(requested_profile),
                )
            effective_profile_id = (
                str(selected_profile.get("id") or "").strip()
                if isinstance(selected_profile, Mapping)
                else (requested_profile or stored_profile or None)
            )
            effective_profile_ref = (
                model_profile_ref_to_dict(
                    selected_profile.get("model_profile_ref")
                    or effective_profile_id
                )
                if isinstance(selected_profile, Mapping) and effective_profile_id
                else None
            )
            model_provider = (
                str(selected_profile.get("provider") or "").strip() or None
                if isinstance(selected_profile, Mapping)
                else None
            )

            metadata = {
                "agent_mode": effective_agent_mode,
                "llm_request_mode": effective_llm_mode,
            }
            if effective_profile_id:
                metadata["model_profile_id"] = effective_profile_id
            if effective_profile_ref:
                metadata["model_profile_ref"] = effective_profile_ref
            if selected_ontology is not None:
                metadata.update(
                    {
                        "selected_ontology_id": selected_ontology_id,
                        "selected_ontology_name": selected_ontology.name,
                        "selected_ontology_version": selected_ontology_version or None,
                    }
                )
            if session is None:
                chat_store.create_session(
                    session_id=context.session_id,
                    metadata=metadata,
                )
                prune = getattr(orchestrator, "_prune_old_sessions", None)
                if callable(prune):
                    prune(
                        chat_store,
                        execution_store,
                        context.username,
                        context.workspace_id,
                    )
            else:
                chat_store.merge_session_metadata(context.session_id, metadata)

            history = _runtime_history(
                execution_store.get_llm_context(context.session_id) or []
            )
            read_documents = execution_store.get_read_documents_context(
                context.session_id
            )
            memory_manager = (
                orchestrator._get_memory_manager(context.username)
                if callable(getattr(orchestrator, "_get_memory_manager", None))
                else None
            )
            memory_context = (
                orchestrator._build_memory_context(
                    memory_manager,
                    query=context.query,
                    allow_injection=True,
                )
                if callable(getattr(orchestrator, "_build_memory_context", None))
                else ""
            )

            if existing_turn is None:
                started_turn, interaction_response = execution_store.start_next_turn(
                    session_id=context.session_id,
                    run_id=context.run_id,
                    user_request=context.query,
                    ontology_version=selected_ontology_version or None,
                    interaction_response=interaction_response,
                )
                turn_id = int(started_turn["turn_id"])
                allocated_context = context.model_copy(update={"turn_id": turn_id})
                with self._context_lock:
                    self._pending_prepare_turns[context.run_id] = (
                        allocated_context,
                        turn_id,
                    )
            else:
                turn_id = int(existing_turn["turn_id"])
                started_turn = existing_turn
            interaction_cancellation = (
                started_turn.get("prior_interaction_cancellation")
                if isinstance(started_turn, Mapping)
                else None
            )

            set_active_run = getattr(chat_store, "set_active_run", None)
            if callable(set_active_run):
                set_active_run(context.session_id, context.run_id)
            message_metadata = {
                "run_id": context.run_id,
                "turn_id": turn_id,
                **(
                    {"model_profile_id": effective_profile_id}
                    if effective_profile_id
                    else {}
                ),
                **(
                    {"model_profile_ref": effective_profile_ref}
                    if effective_profile_ref
                    else {}
                ),
                **(
                    {"model_provider": model_provider}
                    if model_provider
                    else {}
                ),
                **(
                    {"interaction_response": interaction_response}
                    if interaction_response is not None
                    else {}
                ),
            }
            if selected_ontology is not None and turn_id == 1:
                message_metadata.update(
                    {
                        "selected_ontology_id": selected_ontology_id,
                        "selected_ontology_name": selected_ontology.name,
                        "selected_ontology_version": selected_ontology_version or None,
                    }
                )
            upsert_message = getattr(chat_store, "upsert_message", None)
            if callable(upsert_message):
                upsert_message(
                    session_id=context.session_id,
                    role="user",
                    content=context.query,
                    turn_id=turn_id,
                    metadata=message_metadata,
                )
            else:
                chat_store.add_message(
                    session_id=context.session_id,
                    role="user",
                    content=context.query,
                    metadata=message_metadata,
                )
            prepared = context.model_copy(
                update={
                    "turn_id": turn_id,
                    "agent_mode": effective_agent_mode,
                    "llm_request_mode": effective_llm_mode,
                    "model_profile_id": effective_profile_id,
                    "model_profile_ref": effective_profile_ref,
                    "model_provider": model_provider,
                    "arguments": arguments or None,
                    "history_messages": history,
                    "memory_context": memory_context or None,
                    "read_documents": (
                        dict(read_documents)
                        if isinstance(read_documents, Mapping)
                        else None
                    ),
                    "interaction_response": interaction_response,
                    "interaction_cancellation": (
                        dict(interaction_cancellation)
                        if isinstance(interaction_cancellation, Mapping)
                        else None
                    ),
                }
            )
            with self._context_lock:
                self._run_contexts[context.run_id] = prepared
            return prepared

    def _finalize_sync(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
        parse_execution_summary: Callable[[str], Any] | None,
    ) -> None:
        with self._finalize_lock:
            chat_store, execution_store = self._stores(context)
            turn = execution_store.get_turn(
                context.session_id,
                context.turn_id,
                run_id=context.run_id,
            )
            if turn is None:
                return
            current_status = str(turn.get("status") or "")
            if current_status in {"success", "failed", "cancelled"}:
                return
            if current_status == "awaiting_input" and result.status == "awaiting_input":
                return
            if _is_deterministic_result(result):
                # The public EventBus assigns the canonical event_id/seq after
                # finalize(). Keep this turn running until that event and the
                # terminal recovery intent can be committed together.
                try:
                    get_active_run = getattr(chat_store, "get_active_run", None)
                    clear_active_run = getattr(chat_store, "clear_active_run", None)
                    if callable(clear_active_run) and (
                        not callable(get_active_run)
                        or get_active_run(context.session_id)
                        in {None, "", context.run_id}
                    ):
                        clear_active_run(context.session_id)
                except Exception:
                    self._log_terminal_projection_failure(
                        context.run_id,
                        "active_run",
                    )
                return
            try:
                self._finalize_phase_sync(context, result, parse_execution_summary)
            except Exception:
                # The durable turn transition is the authoritative terminal
                # commit. Auxiliary history/run projections are best effort;
                # once the turn is terminal, a projection error must not make
                # the public run appear to have failed after succeeding.
                refreshed = execution_store.get_turn(
                    context.session_id,
                    context.turn_id,
                    run_id=context.run_id,
                )
                terminal_statuses = {"success", "failed", "cancelled"}
                if refreshed is not None and str(refreshed.get("status")) in terminal_statuses:
                    logger.warning(
                        "Terminal projection failed after durable commit run_id=%s",
                        context.run_id,
                        exc_info=True,
                    )
                    return
                raise

    def _finalize_phase_sync(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
        parse_execution_summary: Callable[[str], Any] | None,
    ) -> None:
        orchestrator = self._orchestrator_provider()
        chat_store, execution_store = self._stores(context)
        content = str(result.content or "")
        final_output: dict[str, Any]
        if isinstance(result.structured_content, dict):
            final_output = dict(result.structured_content)
        else:
            final_output = {"text": content}

        with _workspace_context(context.workspace_id):
            summary = None
            if (
                result.status == "completed"
                and content
                and parse_execution_summary is not None
            ):
                try:
                    parsed_summary = parse_execution_summary(content)
                    if parsed_summary:
                        summary = json.dumps(parsed_summary, ensure_ascii=False)
                except Exception:
                    # Summary extraction is an auxiliary projection and must
                    # not prevent the durable turn terminal transition.
                    self._log_terminal_projection_failure(
                        context.run_id,
                        "execution_summary",
                    )

            if result.status == "awaiting_input":
                execution_store.await_turn(
                    context.session_id,
                    context.turn_id,
                    final_output=final_output,
                    summary=None,
                    run_id=context.run_id,
                    deterministic_checkpoint=(
                        final_output.get("deterministic_checkpoint")
                        if isinstance(
                            final_output.get("deterministic_checkpoint"),
                            dict,
                        )
                        else None
                    ),
                )
            elif result.status == "completed":
                execution_store.complete_turn(
                    context.session_id,
                    context.turn_id,
                    final_output=final_output,
                    summary=summary,
                    run_id=context.run_id,
                )
            elif result.status == "cancelled":
                execution_store.cancel_turn(
                    context.session_id,
                    context.turn_id,
                    reason="runtime_cancelled",
                    final_output=final_output,
                    run_id=context.run_id,
                )
            else:
                failure_output: Any
                if result.structured_content is not None:
                    failure_output = final_output
                elif content and result.error is not None:
                    failure_output = {
                        "text": content,
                        "error": result.error.model_dump(mode="json"),
                    }
                else:
                    failure_output = (
                        result.error.message
                        if result.error is not None
                        else "运行失败"
                    )
                execution_store.fail_turn(
                    context.session_id,
                    context.turn_id,
                    error=failure_output,
                    run_id=context.run_id,
                )
            execution_store.complete_waiting_solver_checkpoint(
                context.session_id,
                run_id=context.run_id,
                turn_id=context.turn_id,
            )

            try:
                history_source = result.history_messages or context.history_messages
                history = [
                    message.model_dump(mode="python", exclude_none=True)
                    for message in history_source
                ]
                if not result.history_messages:
                    history.append({"role": "user", "content": context.query})
                    if content:
                        history.append({"role": "assistant", "content": content})
                execution_store.set_llm_context(context.session_id, history)
            except Exception:
                self._log_terminal_projection_failure(context.run_id, "llm_context")

            try:
                read_documents = result.read_documents or context.read_documents
                if isinstance(read_documents, Mapping):
                    execution_store.set_read_documents_context(
                        context.session_id,
                        dict(read_documents),
                    )
                    infer_version = getattr(
                        orchestrator,
                        "_infer_toolbased_ontology_version",
                        None,
                    )
                    if callable(infer_version):
                        ontology_version = infer_version(
                            context.workspace_id,
                            dict(read_documents),
                        )
                        if ontology_version:
                            execution_store.stamp_turn_ontology_version(
                                context.session_id,
                                context.turn_id,
                                ontology_version,
                            )
            except Exception:
                self._log_terminal_projection_failure(context.run_id, "read_documents")

            try:
                if result.evolution_recall_records:
                    execution_store.set_turn_evolution_records(
                        context.session_id,
                        context.turn_id,
                        result.evolution_recall_records,
                    )
                    record_outcomes = getattr(
                        orchestrator,
                        "_record_evolution_recall_outcomes",
                        None,
                    )
                    if callable(record_outcomes) and result.status == "completed":
                        record_outcomes(
                            {
                                "evolution_recall_records": (
                                    result.evolution_recall_records
                                )
                            }
                        )
            except Exception:
                self._log_terminal_projection_failure(context.run_id, "evolution")

            try:
                get_active_run = getattr(chat_store, "get_active_run", None)
                clear_active_run = getattr(chat_store, "clear_active_run", None)
                if callable(clear_active_run) and (
                    not callable(get_active_run)
                    or get_active_run(context.session_id) in {None, "", context.run_id}
                ):
                    clear_active_run(context.session_id)
            except Exception:
                self._log_terminal_projection_failure(context.run_id, "active_run")

    @staticmethod
    def _log_terminal_projection_failure(run_id: str, projection: str) -> None:
        logger.warning(
            "Terminal projection failed run_id=%s projection=%s",
            run_id,
            projection,
            exc_info=True,
        )

    def _try_direct_execute_sync(
        self,
        context: RuntimeInvocationContext,
        dependencies: Mapping[str, Any],
    ) -> list[RuntimeEvent] | None:
        OperationDirectRouter = dependencies["OperationDirectRouter"]
        RuntimeContext = dependencies["RuntimeContext"]
        run_direct_script = dependencies["run_direct_script"]
        tool_output_artifact_writer_context = dependencies[
            "tool_output_artifact_writer_context"
        ]
        load_system_config = dependencies["load_system_config"]
        DeterministicExecutionService = dependencies.get(
            "DeterministicExecutionService"
        )
        NotApplicable = dependencies.get("NotApplicable")

        orchestrator = self._orchestrator_provider()
        agent_config = getattr(orchestrator, "agent_config", None)
        direct_config = getattr(agent_config, "direct_execution", None)
        arguments = dict(context.arguments or {})
        selected_ontology_id = str(
            arguments.get("selected_ontology_id") or ""
        ).strip()
        selected_ontology_version = str(
            arguments.get("selected_ontology_version")
            or arguments.get("selected_version")
            or ""
        ).strip()
        runtime_context = RuntimeContext(
            session_id=context.session_id,
            run_id=context.run_id,
            turn_id=context.turn_id,
            user_id=context.username,
            workspace_id=context.workspace_id,
            selected_ontology_id=selected_ontology_id or None,
            selected_ontology_version=selected_ontology_version or None,
        )
        with _workspace_context(context.workspace_id):
            chat_store, execution_store = self._stores(context)
            checkpoint = (
                execution_store.get_pending_deterministic_checkpoint(
                    context.session_id
                )
                if context.interaction_response
                else None
            )
            from agent.direct_execution.contracts import (
                AwaitingInput,
                Completed,
                DeterministicRequest,
                Failed,
            )

            request = DeterministicRequest(
                user_request=context.query,
                context={
                    "agent_mode": context.agent_mode,
                    "llm_request_mode": context.llm_request_mode,
                    "model_profile_id": context.model_profile_id,
                    "requested_operation_key": context.requested_operation_key,
                    "supports_user_interaction": (
                        context.supports_user_interaction
                    ),
                    **arguments,
                },
                arguments=arguments,
                runtime_context=runtime_context,
                checkpoint=checkpoint,
                interaction_response=(
                    dict(context.interaction_response)
                    if context.interaction_response
                    else None
                ),
            )
            artifact_writer = _build_deterministic_artifact_writer(
                chat_store,
                session_id=context.session_id,
                run_id=context.run_id,
                execution_store=execution_store,
                turn_id=context.turn_id,
            )
            if isinstance(dependencies, dict):
                dependencies["artifact_writer"] = artifact_writer
            system_config = load_system_config()
            outcome = (
                DeterministicExecutionService().execute(
                    request,
                    enabled=bool(
                        getattr(
                            agent_config,
                            "deterministic_execution_enabled",
                            False,
                        )
                    ),
                    project_root=str(system_config["storage"]["project_root"]),
                    timeout_seconds=int(agent_config.execute_script_timeout),
                    artifact_writer=artifact_writer,
                    cancel_event=dependencies.get("cancel_event"),
                )
                if DeterministicExecutionService is not None
                and agent_config is not None
                else None
            )
            if outcome is not None and not isinstance(outcome, NotApplicable):
                events = [
                    RuntimeEvent(
                        event_type="turn.started",
                        status="running",
                        run_id=context.run_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        source={
                            "node": "deterministic_execution",
                            "component": "service",
                        },
                        payload={"turn_id": context.turn_id},
                    ),
                    RuntimeEvent(
                        event_type="tool.started",
                        status="running",
                        run_id=context.run_id,
                        session_id=context.session_id,
                        turn_id=context.turn_id,
                        source={
                            "node": "deterministic_execution",
                            "component": "service",
                        },
                        payload={
                            "name": "execute_script",
                            "tool_call_id": "deterministic",
                            "message": "确定性执行",
                        },
                    ),
                ]
                if isinstance(outcome, Completed):
                    envelope = _deterministic_final_output(outcome.envelope)
                    events.append(
                        RuntimeEvent(
                            event_type="tool.finished",
                            status="completed",
                            run_id=context.run_id,
                            session_id=context.session_id,
                            turn_id=context.turn_id,
                            source={
                                "node": "deterministic_execution",
                                "component": "service",
                            },
                            payload={
                                "name": "execute_script",
                                "tool_call_id": "deterministic",
                                "status": "completed",
                                "elapsed_ms": outcome.elapsed_ms,
                                "output_projection": envelope.get(
                                    "structured_result"
                                ),
                            },
                        )
                    )
                    events.append(
                        _direct_terminal_event(
                            context,
                            status="completed",
                            content=str(envelope.get("text") or ""),
                            structured_content=envelope,
                        )
                    )
                    return events
                if isinstance(outcome, AwaitingInput):
                    interaction = dict(outcome.interaction)
                    prompt = str(interaction.get("prompt") or "")
                    envelope = {
                        "type": "user_interaction",
                        "text": prompt,
                        "interaction": interaction,
                        "options": list(interaction.get("options") or []),
                        "deterministic_checkpoint": dict(outcome.checkpoint),
                        "execution": {
                            "type": "deterministic",
                            "authoritative": False,
                            "complete": False,
                            "operation_key": outcome.operation_key,
                        },
                    }
                    events.append(
                        RuntimeEvent(
                            event_type="interaction.requested",
                            status="awaiting_input",
                            run_id=context.run_id,
                            session_id=context.session_id,
                            turn_id=context.turn_id,
                            source={
                                "node": "deterministic_execution",
                                "component": "service",
                            },
                            payload=interaction,
                        )
                    )
                    events.append(
                        _direct_terminal_event(
                            context,
                            status="awaiting_input",
                            content=prompt,
                            structured_content=envelope,
                        )
                    )
                    return events
                if isinstance(outcome, Failed):
                    rollback = getattr(artifact_writer, "rollback", None)
                    if callable(rollback):
                        rollback(())
                    events.append(
                        _direct_terminal_event(
                            context,
                            status="failed",
                            content=outcome.message,
                            structured_content=outcome.public_result(),
                            error=RuntimeErrorInfo(
                                error_code=outcome.code,
                                message=outcome.message,
                                retriable=False,
                                source="tool",
                            ),
                        )
                    )
                    return events
                raise RuntimeError("deterministic execution returned an unknown outcome")

            if context.interaction_response:
                return None
            if direct_config is None or not bool(
                getattr(direct_config, "enabled", False)
            ):
                return None
            decision = OperationDirectRouter(config=direct_config).route(
                user_request=context.query,
                context={
                    "agent_mode": context.agent_mode,
                    "llm_request_mode": context.llm_request_mode,
                    "model_profile_id": context.model_profile_id,
                    **arguments,
                },
                runtime_context=runtime_context,
                arguments=arguments,
            )
            if decision.route == "fallback_agent_loop":
                return None

            events = [
                RuntimeEvent(
                    event_type="turn.started",
                    status="running",
                    run_id=context.run_id,
                    session_id=context.session_id,
                    turn_id=context.turn_id,
                    source={"node": "direct_execution", "component": "service"},
                    payload={"turn_id": context.turn_id},
                )
            ]
            if decision.route == "missing_args":
                missing = "\n".join(
                    "- {}".format(item)
                    for item in decision.missing_business_arguments
                )
                content = (
                    "当前 {} 流程需要以下参数：\n{}\n\n请补充后我再执行。"
                ).format(str(decision.operation_key or ""), missing)
                events.append(
                    _direct_terminal_event(
                        context,
                        status="completed",
                        content=content,
                    )
                )
                return events

            artifact_writer = _build_tool_output_artifact_writer(
                chat_store,
                session_id=context.session_id,
                run_id=context.run_id,
            )
            project_root = str(load_system_config()["storage"]["project_root"])
            with tool_output_artifact_writer_context(artifact_writer):
                raw_events, direct_result = run_direct_script(
                    decision=decision,
                    runtime_context=runtime_context,
                    project_root=project_root,
                    timeout_seconds=agent_config.execute_script_timeout,
                )
            for item in raw_events:
                event = _runtime_event_from_legacy(item, context)
                if event is not None:
                    events.append(event)
            if direct_result.status == "error":
                events.append(
                    _direct_terminal_event(
                        context,
                        status="failed",
                        content=direct_result.output,
                        error=RuntimeErrorInfo(
                            error_code=str(
                                direct_result.error or "DIRECT_EXECUTION_FAILED"
                            ).upper(),
                            message="直接执行失败",
                            retriable=False,
                            source="tool",
                        ),
                    )
                )
            else:
                events.append(
                    _direct_terminal_event(
                        context,
                        status="completed",
                        content=direct_result.output,
                    )
                )
            return events

    def _persist_event_sync(self, event: PublicEvent) -> None:
        with self._context_lock:
            context = self._run_contexts.get(event.run_id)
        if context is None:
            return
        chat_store, execution_store = self._stores(context)
        envelope = _chat_event_envelope(event)
        terminal_output = event.payload.get("structured_content")
        deterministic_execution = (
            terminal_output.get("execution")
            if isinstance(terminal_output, Mapping)
            else None
        )
        deterministic_source = (
            str((event.source or {}).get("node") or "")
            == "deterministic_execution"
        )
        deterministic_payload = isinstance(
            deterministic_execution,
            Mapping,
        ) and deterministic_execution.get("type") == "deterministic"
        deterministic_terminal = deterministic_source or deterministic_payload
        is_terminal_event = event.event_type == "turn.finished"
        if is_terminal_event and deterministic_terminal:
            from persistence.deterministic_terminal import (
                persist_deterministic_terminal,
            )

            status = {
                "completed": "success",
                "awaiting_input": "awaiting_input",
                "failed": "failed",
                "cancelled": "cancelled",
            }[event.status]
            final_output = (
                dict(terminal_output)
                if isinstance(terminal_output, Mapping)
                else {
                    "status": status,
                    "cancel_reason": event.payload.get("cancel_reason"),
                    "execution": {
                        "type": "deterministic",
                        "authoritative": False,
                        "complete": False,
                    },
                }
            )
            persist_deterministic_terminal(
                execution_store,
                chat_store,
                session_id=context.session_id,
                turn_id=context.turn_id,
                status=status,
                final_output=final_output,
                llm_context=(
                    execution_store.get_llm_context(context.session_id) or []
                ),
                events=[envelope],
                deterministic_checkpoint=(
                    dict(terminal_output["deterministic_checkpoint"])
                    if isinstance(terminal_output, Mapping)
                    and isinstance(
                        terminal_output.get("deterministic_checkpoint"),
                        Mapping,
                    )
                    else None
                ),
            )
            if event.status == "completed":
                execution_store.commit_deterministic_artifacts(
                    context.session_id,
                    context.turn_id,
                    context.run_id,
                )
            compact = getattr(chat_store, "compact_session", None)
            if callable(compact):
                compact(context.session_id)
            return
        if event.event_type == "turn.finished":
            reconcile = getattr(
                execution_store,
                "reconcile_canonical_terminal",
                None,
            )
            if callable(reconcile):
                status = {
                    "completed": "success",
                    "awaiting_input": "awaiting_input",
                    "failed": "failed",
                    "cancelled": "cancelled",
                }[event.status]
                raw_error = event.payload.get("error")
                raw_output = (
                    event.payload.get("structured_content")
                    if event.payload.get("structured_content") is not None
                    else event.payload.get("final_output")
                )
                if raw_output is None:
                    raw_output = event.payload.get("content")
                if isinstance(raw_output, Mapping):
                    final_output = dict(raw_output)
                elif raw_output is not None:
                    final_output = {"text": str(raw_output)}
                elif event.status == "cancelled":
                    final_output = {
                        "cancel_reason": event.payload.get("cancel_reason")
                    }
                elif event.status == "failed":
                    final_output = {
                        "error": (
                            str(raw_error.get("message") or "运行失败")
                            if isinstance(raw_error, Mapping)
                            else str(raw_error or "运行失败")
                        )
                    }
                else:
                    final_output = {"status": status}
                if (
                    event.status == "failed"
                    and event.payload.get("content") is not None
                    and raw_error is not None
                ):
                    final_output["error"] = (
                        dict(raw_error)
                        if isinstance(raw_error, Mapping)
                        else str(raw_error)
                    )
                error_code = (
                    str(raw_error.get("error_code") or "").strip()
                    if isinstance(raw_error, Mapping)
                    else str(event.payload.get("error_code") or "").strip()
                )
                reconcile(
                    context.session_id,
                    context.turn_id,
                    run_id=context.run_id,
                    status=status,
                    final_output=final_output,
                    error_code=error_code or None,
                )
        if not chat_store.append_event(context.session_id, envelope):
            logger.warning(
                "Failed to persist async event for session_id=%s run_id=%s",
                context.session_id,
                event.run_id,
            )
            return
        if event.event_type == "turn.finished":
            compact = getattr(chat_store, "compact_session", None)
            if callable(compact):
                compact(context.session_id)


def pending_interaction_from_draft(
    draft: PublicEventDraft,
) -> dict[str, Any] | None:
    payload = draft.payload if isinstance(draft.payload, Mapping) else {}
    if draft.event_type == "interaction.requested":
        interaction = payload.get("interaction")
        if not isinstance(interaction, Mapping):
            interaction = payload
        interaction_id = str(
            payload.get("interaction_id")
            or interaction.get("interaction_id")
            or interaction.get("id")
            or ""
        ).strip()
        if not interaction_id:
            return None
        options_value = payload.get("options") or interaction.get("options") or []
        return {
            "id": interaction_id,
            "type": str(payload.get("type") or interaction.get("type") or "input"),
            "prompt": str(
                payload.get("prompt")
                or payload.get("question")
                or payload.get("text")
                or interaction.get("prompt")
                or ""
            ),
            "options": (
                list(options_value) if isinstance(options_value, list) else []
            ),
        }
    if draft.event_type != "turn.finished" or draft.status != "awaiting_input":
        return None
    for value in (
        payload.get("structured_content"),
        payload.get("result"),
        payload.get("final_output"),
    ):
        if not isinstance(value, Mapping):
            continue
        interaction = value.get("interaction")
        if isinstance(interaction, Mapping):
            return dict(interaction)
    return None
