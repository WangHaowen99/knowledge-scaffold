"""Consumer execution service over the native openJiuwen runtime contract."""

from __future__ import annotations

import json
import time
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
    RuntimeResult,
)
from agent.runtime.events import RuntimeEvent
from utils.logging import get_logger


logger = get_logger("runtime.service")


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


def _direct_terminal_event(
    context: RuntimeInvocationContext,
    *,
    status: str,
    content: str,
    error: RuntimeErrorInfo | None = None,
) -> RuntimeEvent:
    payload: dict[str, Any] = {
        "status": status,
        "content": content,
        "history_messages": _history_with_turn(context, content),
        "read_documents": dict(context.read_documents or {}),
    }
    if error is not None:
        payload["error"] = error.model_dump(mode="json")
    return RuntimeEvent(
        event_type="turn.finished",
        status=status,
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        source={"node": "direct_execution", "component": "service"},
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
    )


def _chat_event_envelope(event: PublicEvent) -> dict[str, Any]:
    try:
        created_at = datetime.fromisoformat(event.created_at.replace("Z", "+00:00"))
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=timezone.utc)
        timestamp_ms = int(created_at.timestamp() * 1000)
    except (TypeError, ValueError):
        timestamp_ms = int(time.time() * 1000)
    return {
        "event": "agent.v2",
        "data": {
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
        },
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

    Runtime unit tests and small protocol tools can run without Flask; in that
    environment there is no request-scoped workspace module to bind.
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
    if status in {"completed", "awaiting_input"}:
        history: list[RuntimeHistoryMessage] = []
        for item in event.payload.get("history_messages") or []:
            try:
                history.append(RuntimeHistoryMessage.model_validate(item))
            except ValueError:
                continue
        return RuntimeResult(
            status=status,
            content=payload.get("content"),
            structured_content=payload.get("structured_content"),
            usage=payload.get("usage"),
            trace=payload.get("trace"),
            history_messages=history,
            read_documents=(
                dict(event.payload["read_documents"])
                if isinstance(event.payload.get("read_documents"), Mapping)
                else None
            ),
            evolution_recall_records=[
                dict(item)
                for item in event.payload.get("evolution_recall_records") or []
                if isinstance(item, Mapping)
            ],
        )
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
            self.interaction_drafts.append(draft)
            return
        if event.event_type == "turn.finished":
            if self.terminal_draft is not None:
                raise RuntimeError("Agent Runtime 产生了多个终态事件")
            self.terminal_draft = draft
            return
        await self.sink.emit(draft)

    async def emit_deferred(self, *, status: str) -> None:
        if status == "awaiting_input":
            for draft in self.interaction_drafts:
                await self.sink.emit(draft)
        if self.terminal_draft is not None:
            await self.sink.emit(self.terminal_draft)


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
        *,
        resumed: bool = False,
    ) -> RuntimeInvocationContext:
        if self.session_adapter is None:
            return context
        return await self.session_adapter.prepare(context, resumed=resumed)

    async def execute(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
    ) -> RuntimeResult:
        adapter = PublicEventAdapter(include_trace=context.include_trace)
        direct_events = None
        if self.session_adapter is not None and context.interaction_response is None:
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
            async for event in self.runtime.astream(context):
                draft = adapter.adapt(event)
                if draft is not None and event.event_type == "interaction.requested":
                    interaction_drafts.append(draft)
                elif draft is not None and event.event_type != "turn.finished":
                    await sink.emit(draft)
                if event.event_type == "turn.finished":
                    if terminal is not None:
                        raise RuntimeError("Agent Runtime 产生了多个终态事件")
                    terminal = event
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
            await self._finalize(context, result)
            if result.status == "awaiting_input":
                for interaction_draft in interaction_drafts:
                    await sink.emit(interaction_draft)
            if terminal is not None:
                terminal_draft = adapter.adapt(terminal)
                if terminal_draft is not None:
                    await sink.emit(terminal_draft)
            return result

        runtime_sink = _PublicRuntimeEventSink(adapter, sink)
        result = await self.runtime.arun(
            context,
            runtime_sink,
        )
        await self._finalize(context, result)
        await runtime_sink.emit_deferred(status=result.status)
        return result

    async def _consume_events(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
        adapter: PublicEventAdapter,
        events: list[RuntimeEvent],
    ) -> RuntimeResult:
        terminal: RuntimeEvent | None = None
        for event in events:
            if event.event_type == "turn.finished":
                if terminal is not None:
                    raise RuntimeError("直接执行产生了多个终态事件")
                terminal = event
                continue
            draft = adapter.adapt(event)
            if draft is not None:
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
        await self._finalize(context, result)
        if terminal is not None:
            terminal_draft = adapter.adapt(terminal)
            if terminal_draft is not None:
                await sink.emit(terminal_draft)
        return result

    async def cancel(self, run_id: str, reason: str) -> bool:
        return await self.runtime.cancel(run_id, reason)

    async def interact(
        self,
        context: RuntimeInvocationContext,
        sink: PublicEventSink,
        interaction_response: dict[str, Any],
    ) -> RuntimeResult:
        resumed = context.with_interaction(
            query=str(
                interaction_response.get("display_text")
                or interaction_response.get("value")
                or ""
            ),
            interaction_response=interaction_response,
        )
        return await self.execute(resumed, sink)

    async def _finalize(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> None:
        if self.session_adapter is not None:
            await self.session_adapter.finalize(context, result)

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
        try:
            from agent.tools.events import tool_output_artifact_writer_context
        except ImportError:
            tool_output_artifact_writer_context = None

        chat_store, _execution_store = self._stores(context)
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
        with _workspace_context(context.workspace_id), artifact_scope:
            yield

    async def prepare(
        self,
        context: RuntimeInvocationContext,
        *,
        resumed: bool = False,
    ) -> RuntimeInvocationContext:
        from agent.core.shared.modes import VALID_AGENT_MODES
        from config.model_profile_refs import model_profile_ref_to_dict
        from config.system_config import resolve_effective_llm_profile_config

        _resolve_workspace_scope()
        try:
            prepared = await run_sync_compat(
                self._prepare_sync,
                context,
                resumed,
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
        resumed: bool,
        valid_agent_modes: set[str] | frozenset[str],
        model_profile_ref_to_dict: Callable[[Any], Any],
        resolve_effective_llm_profile_config: Callable[..., Any],
    ) -> RuntimeInvocationContext:
        try:
            return self._prepare_sync_impl(
                context,
                resumed,
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
            with _workspace_context(context.workspace_id):
                run_store = getattr(
                    self._orchestrator_provider(),
                    "run_store",
                    None,
                )
                if run_store is not None:
                    run_store.fail_run(context.run_id, "会话运行状态准备失败")
        except Exception:
            logger.warning(
                "Failed to compensate prepared run projection run_id=%s",
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

    async def try_direct_execute(
        self,
        context: RuntimeInvocationContext,
    ) -> list[RuntimeEvent] | None:
        orchestrator = self._orchestrator_provider()
        agent_config = getattr(orchestrator, "agent_config", None)
        direct_config = getattr(agent_config, "direct_execution", None)
        if direct_config is None or not bool(getattr(direct_config, "enabled", False)):
            return None

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
        }
        _resolve_workspace_scope()
        return await run_sync_compat(
            self._try_direct_execute_sync,
            context,
            dependencies,
            run_id=context.run_id,
            task_name="consumer-direct-execution",
        )

    async def persist_event(self, event: PublicEvent) -> None:
        terminal = (
            event.event_type == "turn.finished"
            and event.status != "awaiting_input"
        )
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
        resumed: bool,
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
            if resumed and not context.interaction_response:
                raise ValueError("恢复运行缺少交互响应")
            if not resumed and context.interaction_response:
                raise ValueError("首次运行不能携带交互响应")

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
            if requested_ontology_id and turn_count > 0 and not locked_ontology_id:
                raise ValueError(
                    "selected_ontology_id can only be set before the first user turn"
                )

            selected_ontology = None
            selected_ontology_id = locked_ontology_id or requested_ontology_id
            selected_ontology_version = locked_ontology_version
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
            selected_profile = None
            if hasattr(orchestrator, "agent_config"):
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

            started_turn, interaction_response = execution_store.start_next_turn(
                session_id=context.session_id,
                run_id=context.run_id,
                user_request=context.query,
                ontology_version=selected_ontology_version or None,
                interaction_response=(
                    dict(context.interaction_response)
                    if context.interaction_response
                    else None
                ),
            )
            turn_id = int(started_turn["turn_id"])
            allocated_context = context.model_copy(update={"turn_id": turn_id})
            with self._context_lock:
                self._pending_prepare_turns[context.run_id] = (
                    allocated_context,
                    turn_id,
                )
            interaction_cancellation = (
                started_turn.get("prior_interaction_cancellation")
                if isinstance(started_turn, Mapping)
                else None
            )

            run_store = getattr(orchestrator, "run_store", None)
            if run_store is not None:
                run_metadata = {
                    "source": "async_runtime",
                    "session_id": context.session_id,
                    "turn_id": turn_id,
                    "user_id": context.username,
                    "workspace_id": context.workspace_id,
                    "agent_mode": effective_agent_mode,
                }
                if effective_profile_id:
                    run_metadata["model_profile_id"] = effective_profile_id
                if effective_profile_ref:
                    run_metadata["model_profile_ref"] = effective_profile_ref
                if model_provider:
                    run_metadata["model_provider"] = model_provider
                if selected_ontology_id:
                    run_metadata["selected_ontology_id"] = selected_ontology_id
                if selected_ontology_version:
                    run_metadata["selected_ontology_version"] = selected_ontology_version
                if context.traceparent:
                    run_metadata["traceparent"] = context.traceparent
                run_store.create_run(
                    context.run_id,
                    context.query,
                    metadata=run_metadata,
                )

            set_active_run = getattr(chat_store, "set_active_run", None)
            if callable(set_active_run):
                set_active_run(context.session_id, context.run_id)
            message_metadata = {
                "run_id": context.run_id,
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
            _chat_store, execution_store = self._stores(context)
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
                execution_store.fail_turn(
                    context.session_id,
                    context.turn_id,
                    error=(
                        result.error.message
                        if result.error is not None
                        else "运行失败"
                    ),
                    run_id=context.run_id,
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
                run_store = getattr(orchestrator, "run_store", None)
                if run_store is not None:
                    if result.evolution_recall_records:
                        run_store.update_metadata(
                            context.run_id,
                            {
                                "evolution_recall_records": list(
                                    result.evolution_recall_records
                                )
                            },
                        )
                    if result.status == "awaiting_input":
                        run_store.await_run(
                            context.run_id,
                            result.to_dict(),
                            metadata={
                                "pending_user_interaction": final_output.get(
                                    "interaction"
                                )
                            },
                        )
                    elif result.status == "completed":
                        run_store.complete_run(context.run_id, result.to_dict())
                    elif result.status == "cancelled":
                        run_store.cancel_run(
                            context.run_id,
                            metadata={"cancel_reason": "runtime_cancelled"},
                        )
                    else:
                        run_store.fail_run(
                            context.run_id,
                            (
                                result.error.message
                                if result.error is not None
                                else "运行失败"
                            ),
                        )
            except Exception:
                self._log_terminal_projection_failure(context.run_id, "run_store")

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

        orchestrator = self._orchestrator_provider()
        agent_config = getattr(orchestrator, "agent_config", None)
        direct_config = getattr(agent_config, "direct_execution", None)
        if direct_config is None or not bool(getattr(direct_config, "enabled", False)):
            return None

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

            chat_store, _execution_store = self._stores(context)
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
            events.extend(
                event
                for item in raw_events
                if (event := _runtime_event_from_legacy(item, context)) is not None
            )
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
        chat_store, _execution_store = self._stores(context)
        envelope = _chat_event_envelope(event)
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
