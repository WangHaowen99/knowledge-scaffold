"""Concrete AsyncAgentRuntime backed by the native-async Jiuwen Engine."""

from __future__ import annotations

import asyncio
import copy
import json
import threading
import uuid
from builtins import BaseExceptionGroup
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Collection, Mapping, Optional

from agent.exceptions import (
    AgentLoopStalledError,
    ModelContentFilteredError,
    ModelInvocationError,
    ModelOutputTruncatedError,
    WorkflowCancelledError,
)
from utils.security.redaction import redact_sensitive_text
from agent.script_argument_errors import safe_script_argument_error

from .adapter import PublicEventAdapter
from .async_utils import call_async_or_thread
from .contracts import (
    RuntimeErrorInfo,
    RuntimeEventSink,
    RuntimeHistoryMessage,
    RuntimeInvocationContext,
    RuntimeRecoveryAction,
    RuntimeResult,
)
from .events import RuntimeEvent
from .jiuwen_events import JiuwenRuntimeEventBridge
from .lifecycle import JiuwenRuntimeLifecycleError, get_jiuwen_runner_lifecycle
from .preflight_cache import materialize_preflight_product

_STATE_SNAPSHOT_CALLBACK: ContextVar[Any] = ContextVar(
    "jiuwen_state_snapshot_callback",
    default=None,
)

_CONTINUE_RECOVERY_ACTION = RuntimeRecoveryAction(
    message=(
        "Continue the previous task from where it was interrupted, using the "
        "preserved context. Do not repeat steps that have already been completed."
    )
)
_HISTORY_MESSAGE_FIELDS = frozenset(
    {
        "role",
        "content",
        "tool_calls",
        "tool_call_id",
        "name",
        "reasoning_content",
    }
)


def _validated_history_messages(value: Any) -> list[RuntimeHistoryMessage]:
    history: list[RuntimeHistoryMessage] = []
    for item in value or []:
        try:
            history.append(RuntimeHistoryMessage.model_validate(item))
        except ValueError:
            continue
    return history


def _build_resumable_llm_context(messages: Any) -> list[dict[str, Any]]:
    """Return the longest provider-valid history prefix after a failed run."""
    if not isinstance(messages, list):
        return []

    checkpoint: list[dict[str, Any]] = []
    pending_tool_call_ids: set[str] = set()
    pending_start: int | None = None

    for raw_message in messages:
        if not isinstance(raw_message, Mapping):
            break
        candidate = {key: copy.deepcopy(value) for key, value in raw_message.items() if key in _HISTORY_MESSAGE_FIELDS}
        if candidate.get("content") is None:
            candidate["content"] = ""
        try:
            message = RuntimeHistoryMessage.model_validate(candidate).model_dump(
                mode="python",
                exclude_none=True,
            )
        except ValueError:
            break

        role = message["role"]
        if pending_tool_call_ids:
            if role != "tool":
                break
            tool_call_id = str(message.get("tool_call_id") or "").strip()
            if tool_call_id not in pending_tool_call_ids:
                break
            checkpoint.append(message)
            pending_tool_call_ids.remove(tool_call_id)
            if not pending_tool_call_ids:
                pending_start = None
            continue

        if role == "tool":
            break

        checkpoint.append(message)
        tool_calls = message.get("tool_calls") if role == "assistant" else None
        if not tool_calls:
            continue
        if not isinstance(tool_calls, list):
            checkpoint.pop()
            break
        tool_call_ids = [
            str(tool_call.get("id") or "").strip() for tool_call in tool_calls if isinstance(tool_call, Mapping)
        ]
        if (
            len(tool_call_ids) != len(tool_calls)
            or not all(tool_call_ids)
            or len(set(tool_call_ids)) != len(tool_call_ids)
        ):
            checkpoint.pop()
            break
        pending_tool_call_ids = set(tool_call_ids)
        pending_start = len(checkpoint) - 1

    if pending_tool_call_ids and pending_start is not None:
        return checkpoint[:pending_start]
    return checkpoint


@contextmanager
def solver_state_snapshot_callback_context(callback: Any):
    """Bind the durable pre-tool state barrier for the current Agent task."""

    token = _STATE_SNAPSHOT_CALLBACK.set(callback)
    try:
        yield
    finally:
        _STATE_SNAPSHOT_CALLBACK.reset(token)


def _accepted_preflight_model_profile(
    context: RuntimeInvocationContext,
) -> Mapping[str, Any] | None:
    snapshot = context.preflight_snapshot
    if (
        snapshot is None
        or snapshot.workspace_id != context.workspace_id
        or not isinstance(snapshot.model_profile, Mapping)
    ):
        return None

    from config.model_profile_refs import model_profile_ref_to_dict

    try:
        expected = model_profile_ref_to_dict(context.model_profile_ref or context.model_profile_id)
        actual = model_profile_ref_to_dict(
            snapshot.model_profile.get("model_profile_ref") or snapshot.model_profile.get("id")
        )
    except (TypeError, ValueError):
        return None
    if expected is None or actual != expected:
        return None
    return snapshot.model_profile


def _accepted_preflight_ontology_snapshot(
    context: RuntimeInvocationContext,
) -> Any:
    snapshot = _matching_preflight_ontology_products(context)
    return snapshot if snapshot is not None and isinstance(snapshot.ontology_manifest, Mapping) else None


def _matching_preflight_ontology_products(
    context: RuntimeInvocationContext,
) -> Any:
    """Match frozen root/preload products while tolerating legacy snapshots."""

    snapshot = context.preflight_snapshot
    arguments = dict(context.arguments or {})
    selected_ontology_id = str(arguments.get("selected_ontology_id") or "").strip()
    selected_ontology_version = str(
        arguments.get("selected_ontology_version") or arguments.get("selected_version") or ""
    ).strip()
    if snapshot is None:
        return None
    if snapshot.workspace_id != context.workspace_id:
        return None
    if not selected_ontology_id:
        return None
    if not selected_ontology_version:
        return None
    if snapshot.ontology_id != selected_ontology_id:
        return None
    if snapshot.ontology_version != selected_ontology_version:
        return None
    manifest = snapshot.ontology_manifest
    if manifest is None:
        return snapshot
    if not isinstance(manifest, Mapping):
        return None
    if str(manifest.get("id") or "").strip() != selected_ontology_id:
        return None
    if str(manifest.get("version") or "").strip() != selected_ontology_version:
        return None
    return snapshot


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
        memory_manager_provider: Optional[Callable[[RuntimeInvocationContext], Any]] = None,
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
        self._allowed_lifecycle_modes = frozenset(str(mode or "").strip() for mode in allowed_lifecycle_modes)
        if self._allowed_lifecycle_modes != {"async"}:
            raise ValueError("allowed_lifecycle_modes must contain only async")
        self.max_event_buffer_size = max(1, int(max_event_buffer_size))
        self._runs: dict[str, _ActiveJiuwenRun] = {}
        self._runs_lock = threading.Lock()

    def active_run_count(self) -> int:
        """Return the number of active native runtime runs."""
        with self._runs_lock:
            return len(self._runs)

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
                raise RuntimeError("Jiuwen runtime already has an active run '{}'".format(context.run_id))
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
                engine_state: dict[str, Any] | None = None
                with self._runs_lock:
                    active.producer_started = True
                    cancel_requested = active.cancel_requested
                bridge.publish(bridge.turn_started())
                try:
                    if cancel_requested:
                        raise asyncio.CancelledError
                    engine_state = await self._prepare_engine_state(context)
                    runtime_arguments = dict(context.arguments or {})
                    has_solver_package_context = bool(
                        str(runtime_arguments.get("selected_ontology_id") or "").strip()
                        and str(
                            runtime_arguments.get("selected_ontology_version")
                            or runtime_arguments.get("selected_version")
                            or ""
                        ).strip()
                    )
                    solver_requirement = None
                    solver_guard = None
                    if has_solver_package_context:
                        from agent.core.shared.solver_route_guard import (
                            SolverRouteExecutionGuard,
                            solver_route_requirement_from_state,
                        )

                        solver_requirement = solver_route_requirement_from_state(engine_state)
                        if solver_requirement is not None:
                            solver_guard = SolverRouteExecutionGuard(solver_requirement)

                    def observe_protocol_event(
                        event_type: str,
                        payload: Mapping[str, Any],
                    ) -> None:
                        if solver_guard is not None:
                            solver_guard.observe(
                                {
                                    "type": event_type,
                                    "payload": dict(payload or {}),
                                }
                            )
                        bridge.protocol_event(event_type, payload)

                    direct_arguments = engine_state.get("_solver_direct_arguments")
                    from agent.core.shared.tool_context import (
                        ontology_preflight_tool_context,
                    )

                    with ontology_preflight_tool_context(_accepted_preflight_ontology_snapshot(context)):
                        if solver_requirement is not None and isinstance(
                            direct_arguments,
                            Mapping,
                        ):
                            final_state = await self._execute_direct_solver(
                                engine_state,
                                context=context,
                                requirement=solver_requirement,
                                arguments=direct_arguments,
                                observer=bridge,
                                protocol_event_observer=observe_protocol_event,
                            )
                        else:
                            engine = self._create_engine(context)
                            from agent.core.jiuwen_engine import (
                                jiuwen_preflight_llm_config_context,
                            )

                            with jiuwen_preflight_llm_config_context(_accepted_preflight_model_profile(context)):
                                final_state = await engine.arun(
                                    engine_state,
                                    caller="main",
                                    event_writer=bridge.engine_event,
                                    snapshot_callback=_STATE_SNAPSHOT_CALLBACK.get(),
                                    observer=bridge,
                                    token_callback=bridge.token_callback,
                                    tool_event_emitter=None,
                                    protocol_event_observer=observe_protocol_event,
                                    event_source_node="jiuwen_react",
                                    subagent_manager=subagent_manager,
                                )
                    pending_interaction = final_state.get("pending_user_interaction")
                    if solver_guard is not None:
                        solver_guard.ensure_terminal(
                            allow_awaiting_input=(
                                isinstance(pending_interaction, Mapping) and bool(pending_interaction)
                            )
                        )
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
                    bridge.finish(bridge.turn_finished(terminal_status, terminal_payload))
                except (WorkflowCancelledError, asyncio.CancelledError):
                    await finish_cancelled()
                except Exception as exc:
                    if self._cancel_requested(active):
                        await finish_cancelled()
                    elif self._commit_terminal(active):
                        error = self._runtime_error(exc)
                        terminal_payload: dict[str, Any] = {"error": error.model_dump(mode="json")}
                        if (
                            isinstance(
                                exc,
                                (ModelOutputTruncatedError, ModelContentFilteredError),
                            )
                            and exc.partial_output
                        ):
                            terminal_payload["content"] = exc.partial_output
                        if engine_state is not None:
                            checkpoint = _build_resumable_llm_context(self._history_from_state(engine_state))
                            if checkpoint:
                                terminal_payload["history_messages"] = checkpoint
                            read_documents = engine_state.get("read_documents")
                            if isinstance(read_documents, Mapping):
                                terminal_payload["read_documents"] = dict(read_documents)
                            if checkpoint and error.retriable:
                                terminal_payload["recovery"] = _CONTINUE_RECOVERY_ACTION.model_dump(mode="json")
                        bridge.finish(
                            bridge.turn_finished(
                                "failed",
                                terminal_payload,
                            )
                        )
                finally:
                    bridge.close()

            try:
                async with asyncio.TaskGroup() as task_group:
                    producer = task_group.create_task(produce())
                    producer.set_name("jiuwen-runtime-{}".format(context.run_id))
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
            return bool(exc.exceptions) and all(cls._contains_only_generator_exit(child) for child in exc.exceptions)
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
            self._memory_manager_provider(context) if self._memory_manager_provider is not None else self.memory_manager
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

    async def _execute_direct_solver(
        self,
        state: dict[str, Any],
        *,
        context: RuntimeInvocationContext,
        requirement: Any,
        arguments: Mapping[str, Any],
        observer: Any,
        protocol_event_observer: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        """Execute validated structured Solver arguments without a model hop."""

        from agent.core.shared.state_updates import apply_tool_batch_result_to_state
        from agent.tools.base import ToolContext, ToolInvocation
        from agent.tools.executor import aexecute_tool_batch

        tool_call_id = "solver-{}".format(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                "{}:{}:{}".format(
                    context.run_id,
                    context.turn_id,
                    requirement.operation_key,
                ),
            ).hex
        )
        messages = list(state.get("messages") or [])
        messages.append({"role": "user", "content": context.query})
        messages.append(
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": tool_call_id,
                        "type": "function",
                        "function": {
                            "name": requirement.tool_name,
                            "arguments": json.dumps(
                                dict(arguments),
                                ensure_ascii=False,
                                separators=(",", ":"),
                            ),
                        },
                    }
                ],
            }
        )
        state["messages"] = messages
        await self._emit_state_snapshot(state, context.run_id)

        selected_ontology_id = str(dict(state.get("user_context") or {}).get("selected_ontology_id") or "").strip()
        selected_ontology_version = str(
            dict(state.get("user_context") or {}).get("selected_ontology_version")
            or dict(state.get("user_context") or {}).get("selected_version")
            or ""
        ).strip()
        memory_manager = (
            self._memory_manager_provider(context) if self._memory_manager_provider is not None else self.memory_manager
        )
        from agent.core.shared.tool_context import (
            current_ontology_preflight_tool_fields,
        )

        tool_context = ToolContext(
            memory_manager=memory_manager,
            messages=messages,
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            agent_config=self.agent_config,
            caller="main",
            user_id=context.username,
            agent_mode=context.agent_mode,
            agent_iteration=1,
            workspace_id=context.workspace_id,
            read_documents=state.get("read_documents"),
            selected_ontology_id=selected_ontology_id or None,
            selected_ontology_version=selected_ontology_version or None,
            model_profile_id=context.model_profile_id,
            observer=observer,
            supports_user_interaction=context.supports_user_interaction,
            traceparent=context.traceparent or None,
            protocol_event_observer=protocol_event_observer,
            **current_ontology_preflight_tool_fields(),
        )
        batch_result = await aexecute_tool_batch(
            [
                ToolInvocation(
                    id=tool_call_id,
                    name=requirement.tool_name,
                    arguments=dict(arguments),
                )
            ],
            tool_context,
        )
        apply_tool_batch_result_to_state(state, batch_result)
        final_output = state.get("final_output")
        if final_output not in (None, ""):
            assistant_content = (
                final_output
                if isinstance(final_output, str)
                else json.dumps(
                    final_output,
                    ensure_ascii=False,
                    default=str,
                    separators=(",", ":"),
                )
            )
            state.setdefault("messages", []).append({"role": "assistant", "content": assistant_content})
        await self._emit_state_snapshot(state, context.run_id)
        return state

    @staticmethod
    async def _emit_state_snapshot(
        state: Mapping[str, Any],
        run_id: str,
    ) -> None:
        callback = _STATE_SNAPSHOT_CALLBACK.get()
        if not callable(callback):
            return
        await call_async_or_thread(
            callback,
            copy.deepcopy(dict(state)),
            run_id=run_id,
            task_name="jiuwen-state-snapshot",
            offload_kind="short",
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
                "openJiuwen Runner mode {!r} is not allowed for this runtime".format(snapshot.mode)
            )
        if snapshot.owner_loop_id != current_loop_id:
            raise JiuwenRuntimeLifecycleError("openJiuwen Runner is bound to a different event loop")

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
            message.model_dump(mode="python", exclude_none=True) for message in context.history_messages
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
        selected_ontology_id = str(arguments.get("selected_ontology_id") or "").strip()
        selected_ontology_version = str(
            arguments.get("selected_ontology_version") or arguments.get("selected_version") or ""
        ).strip()
        solver_requirement = None
        if selected_ontology_id and selected_ontology_version:
            from agent.core.shared.solver_route_guard import (
                bind_solver_route_requirement,
                resolve_solver_route_requirement,
                should_suppress_solver_tools,
            )

            solver_kwargs = {
                "user_request": context.query,
                "arguments": arguments,
                "selected_ontology_id": selected_ontology_id,
                "selected_ontology_version": selected_ontology_version,
                "workspace_id": context.workspace_id,
            }
            preflight = _matching_preflight_ontology_products(context)
            if preflight is not None:
                solver_kwargs["root_payload"] = (
                    preflight.root_payload if isinstance(preflight.root_payload, Mapping) else {}
                )
                solver_kwargs["solver_capabilities"] = (
                    preflight.solver_capabilities
                    if isinstance(preflight.solver_capabilities, (tuple, list))
                    else ()
                )
            solver_requirement = resolve_solver_route_requirement(**solver_kwargs)
            user_context = bind_solver_route_requirement(
                user_context,
                solver_requirement,
                suppress_solver_tools=(solver_requirement is None and should_suppress_solver_tools(context.query)),
            )
        model_query = context.query
        if context.interaction_cancellation:
            model_query = "<user-interaction-cancellation>\n{}\n</user-interaction-cancellation>\n\n{}".format(
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
            model_query = "{}\n\n<user-interaction-response>\n{}\n</user-interaction-response>".format(
                context.query,
                json.dumps(
                    context.interaction_response,
                    ensure_ascii=False,
                    default=str,
                ),
            ).strip()
        state = {
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
        preflight = _matching_preflight_ontology_products(context)
        if preflight is not None:
            state["_selected_ontology_context_cache_key"] = (
                context.workspace_id,
                selected_ontology_id,
                selected_ontology_version,
            )
            state["_selected_ontology_context_cache"] = preflight.selected_ontology_context
            if preflight.preload_records:
                state["_preflight_selected_ontology_preload_records"] = materialize_preflight_product(
                    preflight.preload_records
                )
        if solver_requirement is not None and solver_requirement.direct_arguments is not None:
            state["_solver_direct_arguments"] = copy.deepcopy(solver_requirement.direct_arguments)
        return state

    @staticmethod
    def build_engine_state_for_test(context: RuntimeInvocationContext) -> dict[str, Any]:
        return JiuwenAsyncAgentRuntime._build_engine_state(context)

    async def _prepare_engine_state(
        self,
        context: RuntimeInvocationContext,
    ) -> dict[str, Any]:
        arguments = dict(context.arguments or {})
        selected_ontology_id = str(arguments.get("selected_ontology_id") or "").strip()
        selected_ontology_version = str(
            arguments.get("selected_ontology_version") or arguments.get("selected_version") or ""
        ).strip()
        if not selected_ontology_id or not selected_ontology_version:
            return self._build_engine_state(context)
        return await call_async_or_thread(
            self._build_engine_state,
            context,
            run_id=context.run_id,
            task_name="solver-route-resolution",
            offload_kind="tool",
        )

    @staticmethod
    def _completed_payload(
        state: Mapping[str, Any],
        context: RuntimeInvocationContext,
    ) -> dict[str, Any]:
        final_output = state.get("final_output")
        if isinstance(final_output, Mapping):
            content = str(
                final_output.get("text") or final_output.get("output") or final_output.get("content") or final_output
            )
        else:
            content = str(final_output or "")
        payload: dict[str, Any] = {"content": content}
        payload["history_messages"] = JiuwenAsyncAgentRuntime._history_from_state(state)
        if isinstance(state.get("read_documents"), Mapping):
            payload["read_documents"] = dict(state["read_documents"])
        if isinstance(state.get("evolution_recall_records"), list):
            payload["evolution_recall_records"] = [
                dict(item) for item in state["evolution_recall_records"] if isinstance(item, Mapping)
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
                dict(item) for item in state["evolution_recall_records"] if isinstance(item, Mapping)
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
        history: list[dict[str, Any]] = []
        for message in list(state.get("messages") or []):
            if not isinstance(message, Mapping):
                continue
            if str(message.get("role") or "") not in {"user", "assistant", "tool"}:
                continue
            history.append(dict(message))
        current_query = str(state.get("user_request") or "").strip()
        if not current_query:
            return history

        latest_user_index = None
        for index in range(len(history) - 1, -1, -1):
            if str(history[index].get("role") or "") == "user":
                latest_user_index = index
                break
        if latest_user_index is not None:
            latest_content = str(history[latest_user_index].get("content") or "")
            if latest_content == current_query:
                return history
            if dict(state.get("user_context") or {}).get("interaction_response"):
                base_query = current_query.split(
                    "\n\n<user-interaction-response>\n",
                    1,
                )[0]
                if latest_content == base_query:
                    history[latest_user_index] = {
                        **history[latest_user_index],
                        "content": current_query,
                    }
                    return history

        history.append({"role": "user", "content": current_query})
        return history

    @staticmethod
    def history_from_state_for_test(state: Mapping[str, Any]) -> list[dict[str, Any]]:
        return JiuwenAsyncAgentRuntime._history_from_state(state)

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
        if isinstance(exc, BaseExceptionGroup):
            child_errors = [
                JiuwenAsyncAgentRuntime._runtime_error(child)
                for child in exc.exceptions
                if isinstance(child, Exception)
            ]
            if child_errors and all(error.error_code != "INTERNAL_ERROR" for error in child_errors):
                if all(error.error_code == "AGENT_LOOP_STALLED" for error in child_errors):
                    return child_errors[-1]
                first = child_errors[0]
                first_payload = first.model_dump(mode="json")
                if all(item.model_dump(mode="json") == first_payload for item in child_errors[1:]):
                    return first

        exception_type = type(exc)
        is_solver_guard_error = (
            exception_type.__name__ == "SolverRouteGuardError"
            and exception_type.__module__ == "agent.core.shared.solver_route_guard"
        )
        if is_solver_guard_error:
            details = {}
            phase = str(getattr(exc, "phase", "") or "").strip()
            if phase:
                details["phase"] = phase
            return RuntimeErrorInfo(
                error_code=str(getattr(exc, "code", "solver_route_guard_failed") or "solver_route_guard_failed"),
                message=redact_sensitive_text(str(getattr(exc, "public_message", exc) or exc)).strip()[:1000],
                retriable=bool(getattr(exc, "retryable", False)),
                source="tool",
                details=details,
            )
        if isinstance(exc, AgentLoopStalledError):
            validation_error = safe_script_argument_error(
                exc.details.get("last_argument_validation")
            )
            return RuntimeErrorInfo(
                error_code="AGENT_LOOP_STALLED",
                message=redact_sensitive_text(exc.public_message).strip()[:1000],
                retriable=False,
                source="tool",
                details={"last_argument_validation": validation_error} if validation_error else {},
            )
        if isinstance(exc, ModelInvocationError):
            category, status_code = JiuwenAsyncAgentRuntime._classify_runtime_error(exc.cause)
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

        safe_public_message = str(getattr(exc, "public_message", None) or "").strip()
        if safe_public_message:
            explicit_code = str(getattr(exc, "error_code", None) or type(exc).__name__).strip()
            source = (
                "tool"
                if str(getattr(exc, "tool_name", None) or "").strip()
                else JiuwenAsyncAgentRuntime._error_source_from_code(explicit_code)
            )
            return RuntimeErrorInfo(
                error_code=explicit_code[:160],
                message=redact_sensitive_text(safe_public_message).strip()[:1000],
                retriable=bool(getattr(exc, "retriable", False)),
                source=source,
                details={},
            )

        explicit_code = str(getattr(exc, "error_code", None) or "").strip()
        if explicit_code:
            source = JiuwenAsyncAgentRuntime._error_source_from_code(explicit_code)
            public_message = getattr(exc, "public_message", None)
            message = str(
                public_message
                or {
                    "model": "模型服务请求失败",
                    "tool": "工具执行失败",
                    "runtime": "Agent Runtime 执行失败",
                }[source]
            )
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
    def runtime_error_for_test(exc: Exception) -> RuntimeErrorInfo:
        return JiuwenAsyncAgentRuntime._runtime_error(exc)

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
        if (
            status_code in {408, 504}
            or any(isinstance(item, (TimeoutError, asyncio.TimeoutError)) for item in chain)
            or any("timeout" in name for name in type_names)
        ):
            return "timeout", status_code
        if status_code == 429 or any("ratelimit" in name or "rate_limit" in name for name in type_names):
            return "rate_limit", status_code
        if status_code in {401, 403} or any(
            "authentication" in name or "permissiondenied" in name for name in type_names
        ):
            return "authentication", status_code
        if JiuwenAsyncAgentRuntime._contains_connection_error(chain, type_names):
            return "connection", status_code
        if status_code is not None:
            return (
                "provider_retryable" if status_code == 409 or 500 <= status_code < 600 else "provider",
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
        payload = PublicEventAdapter(include_trace=include_trace).adapt_runtime_result_payload(event)
        history = _validated_history_messages(event.payload.get("history_messages"))
        read_documents = (
            dict(event.payload["read_documents"]) if isinstance(event.payload.get("read_documents"), Mapping) else None
        )
        if event.status in {"completed", "awaiting_input"}:
            return RuntimeResult(
                status=event.status,
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
        error_payload = payload.get("error") or {
            "error_code": "INTERNAL_ERROR",
            "message": "Agent Runtime 执行失败",
            "retriable": False,
            "source": "runtime",
            "details": {},
        }
        recovery = None
        if event.status == "failed" and isinstance(payload.get("recovery"), Mapping):
            try:
                recovery = RuntimeRecoveryAction.model_validate(payload["recovery"])
            except ValueError:
                recovery = None
        return RuntimeResult(
            status=event.status,
            content=payload.get("content"),
            structured_content=payload.get("structured_content"),
            usage=payload.get("usage"),
            trace=payload.get("trace"),
            error=RuntimeErrorInfo.model_validate(error_payload),
            recovery=recovery,
            history_messages=history,
            read_documents=read_documents,
        )
