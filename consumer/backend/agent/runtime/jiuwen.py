"""Concrete AsyncAgentRuntime backed by the native-async Jiuwen Engine."""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Collection, Mapping, Optional

from agent.exceptions import ModelInvocationError, WorkflowCancelledError
from utils.security.redaction import redact_sensitive_text

from .adapter import PublicEventAdapter
from .contracts import (
    RuntimeErrorInfo,
    RuntimeEventSink,
    RuntimeHistoryMessage,
    RuntimeInvocationContext,
    RuntimeResult,
)
from .events import RuntimeEvent
from .jiuwen_events import JiuwenRuntimeEventBridge
from .lifecycle import JiuwenRuntimeLifecycleError, get_jiuwen_runner_lifecycle


@dataclass
class _ActiveJiuwenRun:
    task: asyncio.Task[Any] | None = None
    owner_loop: asyncio.AbstractEventLoop | None = None
    producer_started: bool = False
    terminal_committed: bool = False
    cancel_requested: bool = False
    cancel_reason: str = "cancelled"


class JiuwenAsyncAgentRuntime:
    """Expose JiuwenReactLoopEngine through the stable line-A contract."""

    def __init__(
        self,
        *,
        memory_manager: Any = None,
        agent_config: Any = None,
        engine_factory: Optional[Callable[..., Any]] = None,
        memory_manager_provider: Optional[
            Callable[[RuntimeInvocationContext], Any]
        ] = None,
        allowed_lifecycle_modes: Collection[str] = ("async",),
        max_event_buffer_size: int = 1024,
    ) -> None:
        if agent_config is None:
            from config import AgentConfig

            agent_config = AgentConfig.from_env()
        self.memory_manager = memory_manager
        self.agent_config = agent_config
        self._engine_factory = engine_factory
        self._memory_manager_provider = memory_manager_provider
        self._allowed_lifecycle_modes = frozenset(
            str(mode or "").strip() for mode in allowed_lifecycle_modes
        )
        if not self._allowed_lifecycle_modes or not self._allowed_lifecycle_modes.issubset(
            {"async", "legacy"}
        ):
            raise ValueError("allowed_lifecycle_modes must contain async and/or legacy")
        self.max_event_buffer_size = max(1, int(max_event_buffer_size))
        self._runs: dict[str, _ActiveJiuwenRun] = {}
        self._runs_lock = threading.Lock()

    async def arun(
        self,
        context: RuntimeInvocationContext,
        sink: RuntimeEventSink,
    ) -> RuntimeResult:
        terminal: RuntimeEvent | None = None
        async for event in self.astream(context):
            await sink.emit(event)
            if event.event_type == "turn.finished":
                terminal = event
        if terminal is None:
            return RuntimeResult(
                status="failed",
                error=RuntimeErrorInfo(
                    error_code="RUNTIME_MISSING_TERMINAL",
                    message="Jiuwen runtime ended without a terminal event",
                    retriable=False,
                    source="runtime",
                ),
            )
        return self._result_from_terminal(
            terminal,
            include_trace=context.include_trace,
        )

    async def astream(
        self,
        context: RuntimeInvocationContext,
    ) -> AsyncIterator[RuntimeEvent]:
        if self._engine_factory is None:
            self._require_async_lifecycle()

        active = _ActiveJiuwenRun(owner_loop=asyncio.get_running_loop())
        with self._runs_lock:
            if context.run_id in self._runs:
                raise RuntimeError(
                    "Jiuwen runtime already has an active run '{}'".format(
                        context.run_id
                    )
                )
            self._runs[context.run_id] = active
        clear_subagent_manager: Callable[[str | None], None] | None = None
        try:
            if self._engine_factory is None:
                from agent.sub_agents.manager import (
                    clear_subagent_manager as clear_run_subagent_manager,
                    get_subagent_manager,
                )

                clear_subagent_manager = clear_run_subagent_manager
                subagent_manager = get_subagent_manager(
                    context.run_id,
                    self.agent_config,
                )
            else:
                # A caller-provided engine factory is already responsible for
                # its own dependency graph; no sub-agent registry is needed.
                subagent_manager = None
            bridge = JiuwenRuntimeEventBridge(
                context,
                max_buffer_size=self.max_event_buffer_size,
            )

            async def finish_cancelled() -> None:
                if not self._commit_terminal(active):
                    return
                error = RuntimeErrorInfo(
                    error_code="RUN_CANCELLED",
                    message=redact_sensitive_text(active.cancel_reason or "cancelled"),
                    retriable=False,
                    source="runtime",
                )
                bridge.finish(
                    bridge.turn_finished(
                        "cancelled",
                        {"error": error.model_dump(mode="json")},
                    )
                )

            async def produce() -> None:
                with self._runs_lock:
                    active.producer_started = True
                    cancel_requested = active.cancel_requested
                bridge.publish(bridge.turn_started())
                try:
                    if cancel_requested:
                        raise asyncio.CancelledError
                    engine = self._create_engine(context)
                    final_state = await engine.arun(
                        self._build_engine_state(context),
                        caller="main",
                        event_writer=bridge.engine_event,
                        observer=bridge,
                        token_callback=bridge.token_callback,
                        tool_event_emitter=None,
                        protocol_event_observer=bridge.protocol_event,
                        event_source_node="jiuwen_react",
                        subagent_manager=subagent_manager,
                    )
                    pending_interaction = final_state.get("pending_user_interaction")
                    if isinstance(pending_interaction, Mapping) and pending_interaction:
                        terminal_status = "awaiting_input"
                        terminal_payload = self._awaiting_payload(
                            final_state,
                            pending_interaction,
                            context,
                        )
                    else:
                        terminal_status = "completed"
                        terminal_payload = self._completed_payload(final_state, context)
                    if not self._commit_terminal(
                        active,
                        reject_if_cancel_requested=True,
                    ):
                        raise asyncio.CancelledError
                    bridge.finish(
                        bridge.turn_finished(terminal_status, terminal_payload)
                    )
                except (WorkflowCancelledError, asyncio.CancelledError):
                    await finish_cancelled()
                except Exception as exc:
                    if self._cancel_requested(active):
                        await finish_cancelled()
                    elif self._commit_terminal(active):
                        error = self._runtime_error(exc)
                        bridge.finish(
                            bridge.turn_finished(
                                "failed",
                                {"error": error.model_dump(mode="json")},
                            )
                        )
                finally:
                    bridge.close()

            try:
                async with asyncio.TaskGroup() as task_group:
                    producer = task_group.create_task(
                        produce(),
                        name="jiuwen-runtime-{}".format(context.run_id),
                    )
                    active.task = producer
                    while True:
                        event = await bridge.get()
                        if event is None:
                            break
                        if bridge.overflowed:
                            self._commit_terminal(active)
                            if not producer.done():
                                producer.cancel()
                        elif event.event_type == "turn.finished":
                            self._commit_terminal(active)
                        yield event
            except BaseExceptionGroup as exc_group:
                if not self._contains_only_generator_exit(exc_group):
                    raise
        finally:
            task = active.task
            if task is not None and not task.done():
                task.cancel()
            try:
                if clear_subagent_manager is not None:
                    clear_subagent_manager(context.run_id)
            finally:
                with self._runs_lock:
                    if self._runs.get(context.run_id) is active:
                        self._runs.pop(context.run_id, None)

    @classmethod
    def _contains_only_generator_exit(cls, exc: BaseException) -> bool:
        if isinstance(exc, BaseExceptionGroup):
            return bool(exc.exceptions) and all(
                cls._contains_only_generator_exit(child)
                for child in exc.exceptions
            )
        return isinstance(exc, GeneratorExit)

    async def cancel(self, run_id: str, reason: str) -> bool:
        normalized_run_id = str(run_id or "").strip()
        with self._runs_lock:
            active = self._runs.get(normalized_run_id)
            if active is None:
                return False
            if active.terminal_committed:
                return False
            if active.task is not None and active.task.done():
                return False
            active.cancel_requested = True
            active.cancel_reason = str(reason or "cancelled")
            task = active.task if active.producer_started else None
            owner_loop = active.owner_loop
        if task is not None and not task.done():
            current_loop = asyncio.get_running_loop()
            if owner_loop is current_loop:
                task.cancel()
            elif owner_loop is not None and not owner_loop.is_closed():
                owner_loop.call_soon_threadsafe(task.cancel)
        return True

    async def release(self, jiuwen_session_id: str) -> None:
        await get_jiuwen_runner_lifecycle().release(jiuwen_session_id)

    def _create_engine(self, context: RuntimeInvocationContext) -> Any:
        memory_manager = (
            self._memory_manager_provider(context)
            if self._memory_manager_provider is not None
            else self.memory_manager
        )
        if self._engine_factory is not None:
            return self._engine_factory(
                memory_manager=memory_manager,
                agent_config=self.agent_config,
            )
        from agent.core.engine import AgentLoopEngine

        return AgentLoopEngine(
            memory_manager=memory_manager,
            agent_config=self.agent_config,
        )

    def _require_async_lifecycle(self) -> None:
        lifecycle = get_jiuwen_runner_lifecycle()
        snapshot = lifecycle.snapshot()
        current_loop_id = id(asyncio.get_running_loop())
        if snapshot.state != "started":
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner is not started; wrap the application lifespan "
                "with jiuwen_runtime_lifespan() before using JiuwenAsyncAgentRuntime"
            )
        if snapshot.mode not in self._allowed_lifecycle_modes:
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner mode {!r} is not allowed for this runtime".format(
                    snapshot.mode
                )
            )
        if snapshot.owner_loop_id != current_loop_id:
            raise JiuwenRuntimeLifecycleError(
                "openJiuwen Runner is bound to a different event loop"
            )

    def _commit_terminal(
        self,
        active: _ActiveJiuwenRun,
        *,
        reject_if_cancel_requested: bool = False,
    ) -> bool:
        with self._runs_lock:
            if active.terminal_committed:
                return False
            if reject_if_cancel_requested and active.cancel_requested:
                return False
            active.terminal_committed = True
            return True

    def _cancel_requested(self, active: _ActiveJiuwenRun) -> bool:
        with self._runs_lock:
            return active.cancel_requested

    @staticmethod
    def _build_engine_state(context: RuntimeInvocationContext) -> dict[str, Any]:
        arguments = dict(context.arguments or {})
        history_messages = [
            message.model_dump(mode="python", exclude_none=True)
            for message in context.history_messages
        ]
        user_context: dict[str, Any] = {
            "agent_mode": context.agent_mode,
            "llm_request_mode": context.llm_request_mode,
            "supports_user_interaction": context.supports_user_interaction,
        }
        if context.interaction_response:
            user_context["interaction_response"] = dict(context.interaction_response)
        if context.model_profile_id:
            user_context["model_profile_id"] = context.model_profile_id
        if context.model_profile_ref:
            user_context["model_profile_ref"] = dict(context.model_profile_ref)
        if context.model_provider:
            user_context["model_provider"] = context.model_provider
        for key in (
            "selected_ontology_id",
            "selected_ontology_version",
            "selected_version",
        ):
            if arguments.get(key) not in (None, ""):
                user_context[key] = arguments[key]
        model_query = context.query
        if context.interaction_cancellation:
            model_query = (
                "<user-interaction-cancellation>\n{}\n"
                "</user-interaction-cancellation>\n\n{}"
            ).format(
                json.dumps(
                    context.interaction_cancellation,
                    ensure_ascii=False,
                    default=str,
                ),
                model_query,
            ).strip()
        if context.interaction_response:
            # Keep the persisted/displayed user message human-readable while
            # giving the resumed agent the structured checkpoint response it
            # needs to continue the interrupted operation deterministically.
            model_query = (
                "{}\n\n<user-interaction-response>\n{}\n"
                "</user-interaction-response>"
            ).format(
                context.query,
                json.dumps(
                    context.interaction_response,
                    ensure_ascii=False,
                    default=str,
                ),
            ).strip()
        return {
            "run_id": context.run_id,
            "session_id": context.session_id,
            "current_turn_id": context.turn_id,
            "workspace_id": context.workspace_id,
            "user_id": context.username,
            "user_request": model_query,
            "user_context": user_context,
            "runtime_arguments": arguments,
            "messages": history_messages,
            "memory_context": context.memory_context,
            "read_documents": dict(context.read_documents or {}),
            "results": {},
            "completed_ids": [],
            "failed_ids": [],
            "errors": [],
            "final_output": None,
            "debug": False,
        }

    @staticmethod
    def _completed_payload(
        state: Mapping[str, Any],
        context: RuntimeInvocationContext,
    ) -> dict[str, Any]:
        final_output = state.get("final_output")
        if isinstance(final_output, Mapping):
            content = str(
                final_output.get("text")
                or final_output.get("output")
                or final_output.get("content")
                or final_output
            )
        else:
            content = str(final_output or "")
        payload: dict[str, Any] = {"content": content}
        payload["history_messages"] = JiuwenAsyncAgentRuntime._history_from_state(
            state
        )
        if isinstance(state.get("read_documents"), Mapping):
            payload["read_documents"] = dict(state["read_documents"])
        if isinstance(state.get("evolution_recall_records"), list):
            payload["evolution_recall_records"] = [
                dict(item)
                for item in state["evolution_recall_records"]
                if isinstance(item, Mapping)
            ]
        structured = state.get("structured_content")
        if isinstance(structured, (dict, list)):
            payload["structured_content"] = structured
        usage = JiuwenAsyncAgentRuntime._extract_usage(state)
        if usage:
            payload["usage"] = usage
        if context.include_trace and isinstance(state.get("trace"), Mapping):
            payload["trace"] = dict(state["trace"])
        return payload

    @staticmethod
    def _awaiting_payload(
        state: Mapping[str, Any],
        pending_interaction: Mapping[str, Any],
        context: RuntimeInvocationContext,
    ) -> dict[str, Any]:
        final_output = state.get("final_output")
        if not isinstance(final_output, Mapping):
            final_output = {
                "type": "user_interaction",
                "text": str(pending_interaction.get("prompt") or ""),
                "options": list(pending_interaction.get("options") or []),
                "interaction": dict(pending_interaction),
            }
        payload: dict[str, Any] = {
            "content": str(final_output.get("text") or ""),
            "structured_content": dict(final_output),
            "history_messages": JiuwenAsyncAgentRuntime._history_from_state(state),
        }
        if isinstance(state.get("read_documents"), Mapping):
            payload["read_documents"] = dict(state["read_documents"])
        if isinstance(state.get("evolution_recall_records"), list):
            payload["evolution_recall_records"] = [
                dict(item)
                for item in state["evolution_recall_records"]
                if isinstance(item, Mapping)
            ]
        usage = JiuwenAsyncAgentRuntime._extract_usage(state)
        if usage:
            payload["usage"] = usage
        if context.include_trace and isinstance(state.get("trace"), Mapping):
            payload["trace"] = dict(state["trace"])
        return payload

    @staticmethod
    def _history_from_state(
        state: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        history = [
            dict(message)
            for message in list(state.get("messages") or [])
            if isinstance(message, Mapping)
            and str(message.get("role") or "") in {"user", "assistant", "tool"}
        ]
        current_query = str(state.get("user_request") or "").strip()
        if not current_query:
            return history

        if history and str(history[-1].get("role") or "") == "user":
            latest_content = str(history[-1].get("content") or "")
            if latest_content == current_query:
                return history
            if dict(state.get("user_context") or {}).get("interaction_response"):
                base_query = current_query.split(
                    "\n\n<user-interaction-response>\n",
                    1,
                )[0]
                if latest_content == base_query:
                    history[-1] = {**history[-1], "content": current_query}
                    return history

        history.append({"role": "user", "content": current_query})
        return history

    @staticmethod
    def _extract_usage(state: Mapping[str, Any]) -> dict[str, Any] | None:
        for key in ("usage", "token_usage", "llm_usage"):
            usage = state.get(key)
            if isinstance(usage, Mapping):
                return dict(usage)
        results = state.get("results")
        if isinstance(results, Mapping):
            for key in ("usage", "token_usage", "llm_usage"):
                usage = results.get(key)
                if isinstance(usage, Mapping):
                    return dict(usage)
        return None

    @staticmethod
    def _runtime_error(exc: Exception) -> RuntimeErrorInfo:
        if isinstance(exc, ModelInvocationError):
            category, status_code = JiuwenAsyncAgentRuntime._classify_runtime_error(
                exc.cause
            )
            if category == "internal":
                category = "provider"
            error_code, message, retriable = {
                "timeout": ("MODEL_TIMEOUT", "模型调用超时", True),
                "rate_limit": ("MODEL_RATE_LIMITED", "模型服务请求过于频繁", True),
                "authentication": (
                    "AUTHENTICATION_FAILED",
                    "模型服务认证或访问权限校验失败",
                    False,
                ),
                "connection": ("MODEL_CONNECTION_ERROR", "模型服务连接失败", True),
                "provider_retryable": (
                    "MODEL_PROVIDER_ERROR",
                    "模型服务暂时不可用",
                    True,
                ),
                "provider": ("MODEL_PROVIDER_ERROR", "模型服务请求失败", False),
            }[category]
            details = {"http_status": status_code} if status_code is not None else {}
            return RuntimeErrorInfo(
                error_code=error_code,
                message=message,
                retriable=retriable,
                source="model",
                details=details,
            )

        explicit_code = str(getattr(exc, "error_code", None) or "").strip()
        if explicit_code:
            source = JiuwenAsyncAgentRuntime._error_source_from_code(explicit_code)
            public_message = getattr(exc, "public_message", None)
            message = str(public_message or {
                "model": "模型服务请求失败",
                "tool": "工具执行失败",
                "runtime": "Agent Runtime 执行失败",
            }[source])
            message = redact_sensitive_text(message).strip()[:1000]
            return RuntimeErrorInfo(
                error_code=explicit_code[:160],
                message=message,
                retriable=bool(getattr(exc, "retriable", False)),
                source=source,
                details={},
            )

        return RuntimeErrorInfo(
            error_code="INTERNAL_ERROR",
            message="Agent Runtime 执行失败",
            retriable=False,
            source="runtime",
            details={},
        )

    @staticmethod
    def _classify_runtime_error(exc: BaseException) -> tuple[str, int | None]:
        chain = JiuwenAsyncAgentRuntime._exception_chain(exc)
        status_code = None
        for item in chain:
            candidate = JiuwenAsyncAgentRuntime._exception_status_code(item)
            if candidate is not None:
                status_code = candidate
                break
        type_names = {type(item).__name__.lower() for item in chain}
        if any(isinstance(item, (TimeoutError, asyncio.TimeoutError)) for item in chain) or any(
            "timeout" in name for name in type_names
        ):
            return "timeout", status_code
        if status_code == 429 or any("ratelimit" in name or "rate_limit" in name for name in type_names):
            return "rate_limit", status_code
        if status_code in {401, 403} or any(
            "authentication" in name or "permissiondenied" in name
            for name in type_names
        ):
            return "authentication", status_code
        if JiuwenAsyncAgentRuntime._contains_connection_error(chain, type_names):
            return "connection", status_code
        if status_code is not None:
            return (
                "provider_retryable" if status_code >= 500 else "provider",
                status_code,
            )
        return "internal", None

    @staticmethod
    def _contains_connection_error(
        chain: list[BaseException],
        type_names: set[str],
    ) -> bool:
        if any(isinstance(item, ConnectionError) for item in chain):
            return True
        connection_tokens = (
            "connection",
            "connecterror",
            "networkerror",
            "transporterror",
            "connectorerror",
        )
        for name in type_names:
            for token in connection_tokens:
                if token in name:
                    return True
        return False

    @staticmethod
    def _exception_chain(exc: BaseException) -> list[BaseException]:
        chain: list[BaseException] = []
        seen: set[int] = set()
        current: BaseException | None = exc
        while current is not None and id(current) not in seen and len(chain) < 8:
            seen.add(id(current))
            chain.append(current)
            nested = getattr(current, "cause", None)
            if not isinstance(nested, BaseException):
                nested = current.__cause__ or current.__context__
            current = nested if isinstance(nested, BaseException) else None
        return chain

    @staticmethod
    def _exception_status_code(exc: BaseException) -> int | None:
        raw_status = getattr(exc, "status_code", None)
        if raw_status is None:
            response = getattr(exc, "response", None)
            raw_status = getattr(response, "status_code", None)
        try:
            return int(raw_status) if raw_status is not None else None
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _error_source_from_code(error_code: str) -> str:
        normalized = str(error_code or "").upper()
        if normalized.startswith("MODEL_") or normalized in {
            "AUTHENTICATION_FAILED",
            "UNAUTHORIZED",
        }:
            return "model"
        if normalized.startswith("TOOL_") or normalized.startswith("TERMINAL_TOOL_"):
            return "tool"
        return "runtime"

    @staticmethod
    def _result_from_terminal(
        event: RuntimeEvent,
        *,
        include_trace: bool,
    ) -> RuntimeResult:
        payload = PublicEventAdapter(
            include_trace=include_trace
        ).adapt_runtime_result_payload(event)
        if event.status in {"completed", "awaiting_input"}:
            history: list[Any] = []
            for item in event.payload.get("history_messages") or []:
                try:
                    history.append(RuntimeHistoryMessage.model_validate(item))
                except ValueError:
                    continue
            return RuntimeResult(
                status=event.status,
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
        error_payload = payload.get("error") or {
            "error_code": "INTERNAL_ERROR",
            "message": "Agent Runtime 执行失败",
            "retriable": False,
            "source": "runtime",
            "details": {},
        }
        return RuntimeResult(
            status=event.status,
            error=RuntimeErrorInfo.model_validate(error_payload),
        )
