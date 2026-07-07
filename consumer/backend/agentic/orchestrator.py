"""Agentic workflow orchestrator."""

import copy
from datetime import datetime
import json
import queue
import threading
import time
from typing import Any, Dict, Generator, List, Optional
import uuid

from openai import APIConnectionError, APIStatusError, APITimeoutError

from .config import AgentConfig
from .config.system_config import SystemConfigValidationError, resolve_llm_profile_config
from .direct_execution import OperationDirectRouter
from .direct_execution.models import RuntimeContext
from .direct_execution.runner import run_direct_script
from .evolution import (
    PatcherConfig,
    ReflectionWorker,
    build_reflection_snapshot,
    get_proposal_buffer,
    init_operation_patcher,
    parse_execution_summary,
)
from .loop.jiuwen_adapter import JiuwenReactLoopEngine
from .loop.shared.modes import resolve_agent_mode
from .memory import MemoryFactory
from .models import AgenticState
from .services.llm import get_llm_router
from .observability import RunRuntimeTrace, RuntimeStatus, get_observer
from .stores import (
    RunSlotManager,
    TokenBucketRateLimiter,
    RunStore,
    RunStatus,
    ChatStore,
    ExecutionStore,
    UserStoreManager,
)
from .sub_agents.manager import clear_subagent_manager
from .utils.logging import get_logger
from .utils.llm_request_mode import normalize_llm_request_mode
from .utils.ontologies import get_ontology_registry
from .utils.streaming import build_agent_event, clear_run_sequence, stream_writer_context
from .workspaces import workspace_scope
from .exceptions import InputTokenLimitError, WorkflowCancelledError

logger = get_logger("orchestrator")

_PUBLIC_LLM_ERROR_DETAIL_MAX_LENGTH = 800


def _public_workflow_error_message(exc: Exception) -> str:
    """Return a user-safe workflow error message for known external failures."""
    if isinstance(exc, APIStatusError):
        status_code = getattr(exc, "status_code", None)
        detail = _extract_api_status_error_detail(exc)
        if detail:
            return detail
        return f"LLM request failed (HTTP {status_code or 'unknown'})"

    if isinstance(exc, APITimeoutError):
        return "LLM request timed out"

    if isinstance(exc, APIConnectionError):
        return "LLM connection failed"

    if isinstance(exc, InputTokenLimitError):
        return exc.message

    return "Workflow execution failed"


def _normalize_interaction_response(value: Any) -> Optional[Dict[str, Any]]:
    if not isinstance(value, dict):
        return None

    interaction_id = str(value.get("interaction_id") or value.get("id") or "").strip()
    response_type = str(value.get("type") or "").strip() or None
    option_id = str(value.get("option_id") or "").strip() or None
    response_value = value.get("value")
    if response_value is None:
        response_value = value.get("response")
    if response_value is None:
        response_value = ""

    normalized: Dict[str, Any] = {
        "interaction_id": interaction_id,
        "value": response_value,
    }
    if response_type:
        normalized["type"] = response_type
    if option_id:
        normalized["option_id"] = option_id
    return normalized


def _append_interaction_response_context(
    user_request: str,
    interaction_response: Optional[Dict[str, Any]],
) -> str:
    if not interaction_response:
        return user_request

    payload = json.dumps(interaction_response, ensure_ascii=False, default=str)
    return (
        "{}\n\n<user-interaction-response>\n{}\n</user-interaction-response>"
    ).format(str(user_request or "").strip(), payload).strip()


def _build_awaiting_final_output(
    final_output: Any,
    pending_interaction: Dict[str, Any],
) -> Dict[str, Any]:
    if isinstance(final_output, dict):
        output = dict(final_output)
    else:
        output = {}

    prompt = str(
        output.get("text")
        or pending_interaction.get("prompt")
        or final_output
        or ""
    ).strip()
    options = output.get("options")
    if not isinstance(options, list):
        options = list(pending_interaction.get("options") or [])
    output.update(
        {
            "type": "user_interaction",
            "text": prompt,
            "options": options,
            "interaction": dict(pending_interaction),
        }
    )
    return output


def _extract_api_status_error_detail(exc: APIStatusError) -> str:
    response = getattr(exc, "response", None)
    payload: Any = None
    if response is not None:
        try:
            payload = response.json()
        except Exception:
            payload = None

    detail = _extract_error_message_from_payload(payload)
    if not detail and response is not None:
        detail = str(getattr(response, "text", "") or "").strip()
    if not detail:
        detail = str(exc).strip()
    return _truncate_public_error_detail(detail)


def _extract_error_message_from_payload(payload: Any) -> str:
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict):
            for key in ("message", "detail", "error_description", "code"):
                value = error.get(key)
                if value:
                    return str(value).strip()
        elif isinstance(error, str):
            return error.strip()

        for key in ("message", "detail", "error_description"):
            value = payload.get(key)
            if value:
                return str(value).strip()

    if isinstance(payload, str):
        return payload.strip()

    return ""


def _truncate_public_error_detail(detail: str) -> str:
    detail = detail.strip()
    if len(detail) <= _PUBLIC_LLM_ERROR_DETAIL_MAX_LENGTH:
        return detail
    return f"{detail[:_PUBLIC_LLM_ERROR_DETAIL_MAX_LENGTH]}..."


class _WorkflowFailure:
    def __init__(self, error: Exception) -> None:
        self.error = error


class _StreamEventPersistenceBuffer:
    """Asynchronously persist high-frequency stream events in batches."""

    def __init__(
        self,
        *,
        chat_store: ChatStore,
        session_id: str,
        run_id: str,
        turn_id: int,
        observer: Any = None,
        batch_size: int = 64,
        flush_interval_seconds: float = 0.25,
    ) -> None:
        self._chat_store = chat_store
        self._session_id = session_id
        self._run_id = run_id
        self._turn_id = turn_id
        self._run_trace = RunRuntimeTrace(
            observer=observer,
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
        )
        self._batch_size = max(1, int(batch_size))
        self._flush_interval_seconds = max(0.01, float(flush_interval_seconds))
        self._queue: "queue.Queue[Any]" = queue.Queue()
        self._stop = object()
        self._closed = False
        self._lock = threading.Lock()
        self._worker = threading.Thread(
            target=self._run,
            name="agentic-stream-event-persistence",
            daemon=True,
        )
        self._worker.start()

    def add(self, event: Dict[str, Any]) -> None:
        with self._lock:
            if self._closed:
                logger.warning(
                    "Dropping stream event after persistence buffer closed: run_id=%s session_id=%s",
                    self._run_id,
                    self._session_id,
                )
                return
            self._queue.put(event)

    def close(self) -> None:
        should_join = False
        with self._lock:
            if not self._closed:
                self._closed = True
                self._queue.put(self._stop)
                should_join = True

        if should_join:
            self._worker.join()

    def _run(self) -> None:
        batch: List[Dict[str, Any]] = []
        while True:
            try:
                item = self._queue.get(timeout=self._flush_interval_seconds)
            except queue.Empty:
                self._flush(batch)
                batch = []
                continue

            if item is self._stop:
                break

            batch.append(item)
            if len(batch) >= self._batch_size:
                self._flush(batch)
                batch = []

        self._flush(batch)

    def _flush(self, batch: List[Dict[str, Any]]) -> None:
        if not batch:
            return

        batch_to_save = list(batch)
        try:
            with self._run_trace.phase_scope(
                phase_name="stream_events_batch_save",
                component="persistence",
                metrics={"event_count": len(batch_to_save)},
            ):
                append_events = getattr(self._chat_store, "append_events", None)
                if callable(append_events):
                    saved = append_events(self._session_id, batch_to_save)
                else:
                    saved = True
                    for event in batch_to_save:
                        saved = bool(self._chat_store.append_event(self._session_id, event)) and saved
                if not saved:
                    logger.warning(
                        "Failed to persist stream events batch for missing session %s",
                        self._session_id,
                    )
        except Exception:
            logger.warning(
                "Failed to persist stream events batch for session %s",
                self._session_id,
                exc_info=True,
            )


class AgenticOrchestrator:
    """
    Main orchestrator for the agentic workflow.

    Manages workflow lifecycle, worker registry, and state persistence.
    """

    def __init__(
        self,
        run_store: Optional[RunStore] = None,
        chat_store: Optional[ChatStore] = None,
        execution_store: Optional[ExecutionStore] = None,
        user_store_manager: Optional[UserStoreManager] = None,
        agent_config: Optional[AgentConfig] = None,
        memory_factory: Optional[MemoryFactory] = None,
        jiuwen_agent_factory: Any = None,
    ) -> None:
        """
        Initialize the orchestrator.

        Args:
            run_store: Optional run store (created if None)
            chat_store: Optional chat store (created if None)
            execution_store: Optional execution store for workflow recording (created if None)
            user_store_manager: Optional user store manager for multi-user support
            agent_config: Optional agent configuration (created with defaults if None)
        """
        self.agent_config = agent_config or AgentConfig.from_env()
        self.run_store = run_store or RunStore()
        if chat_store is None or execution_store is None:
            from .workspaces import get_workspace_paths
            _fallback_paths = get_workspace_paths("default")
            _fallback_user = "admin"
        self.chat_store = chat_store or ChatStore(
            str(_fallback_paths.users_runtime_dir / _fallback_user / "chats")
        )
        self.execution_store = execution_store or ExecutionStore(
            str(_fallback_paths.users_runtime_dir / _fallback_user / "executions")
        )
        self.user_store_manager = user_store_manager
        self.memory_factory = memory_factory or MemoryFactory()
        self.jiuwen_agent_factory = jiuwen_agent_factory
        self.slot_manager = RunSlotManager()
        self.run_rate_limiter = TokenBucketRateLimiter(
            rate_per_second=self.agent_config.limits.run_tps,
            burst=self.agent_config.limits.run_tps,
        )

        self.passive_reflection_enabled = bool(self.agent_config.evolution_passive_reflection_enabled)
        self.reflection_worker = (
            ReflectionWorker(
                max_workers=max(1, int(getattr(self.agent_config, "reflection_max_workers", 2) or 2)),
            )
            if self.passive_reflection_enabled
            else None
        )

        # Operation patcher: independent background worker driving buffer →
        # staged .md patches. Wired via the buffer's change_listener.
        self.operation_patcher = init_operation_patcher(
            config=PatcherConfig(
                min_occurrences=self.agent_config.evolution_min_occurrences,
                min_applied=self.agent_config.evolution_min_applied,
                max_rejection=self.agent_config.evolution_max_rejection,
                cooldown_sec=self.agent_config.evolution_patch_cooldown_sec,
                sweep_interval_sec=self.agent_config.evolution_sweep_interval_sec,
                auto_apply=self.agent_config.evolution_patch_auto_apply,
            ),
        )

        logger.info("AgenticOrchestrator initialized with memory factory")

    def _stream_agent_loop(
        self,
        initial_state: AgenticState,
        *,
        memory_manager: Any,
        observer: Any = None,
    ) -> Generator[tuple[str, Any], None, None]:
        event_queue: "queue.Queue[Any]" = queue.Queue()
        done = object()

        def queue_writer(event: Dict[str, Any]) -> None:
            event_queue.put(("custom", event))

        def worker() -> None:
            state: AgenticState = dict(initial_state or {})
            workspace_id = str(state.get("workspace_id") or "").strip() or None
            run_id = str(state.get("run_id") or "")
            session_id = str(state.get("session_id") or "")
            turn_id = int(state.get("current_turn_id") or 0)
            try:
                with workspace_scope(workspace_id):
                    engine = JiuwenReactLoopEngine(
                        memory_manager=memory_manager,
                        agent_config=self.agent_config,
                        agent_factory=self.jiuwen_agent_factory,
                    )
                    with stream_writer_context(queue_writer):
                        engine.run(
                            state,
                            caller="main",
                            event_writer=queue_writer,
                            snapshot_callback=lambda snapshot: event_queue.put(
                                ("values", snapshot)
                            ),
                            iteration_limit=self.agent_config.main_agent_max_iterations,
                            observer=observer,
                            event_source_node="jiuwen_react",
                        )
            except Exception as exc:
                logger.exception("Agent loop execution failed")
                event_queue.put(_WorkflowFailure(exc))
            finally:
                event_queue.put(done)

        worker_thread = threading.Thread(
            target=worker,
            name="agentic-workflow-loop",
            daemon=True,
        )
        worker_thread.start()

        while True:
            item = event_queue.get()
            if item is done:
                break
            if isinstance(item, _WorkflowFailure):
                worker_thread.join()
                raise item.error
            yield item

        worker_thread.join()

    def run(
        self,
        user_request: str,
        user_context: Optional[Dict[str, Any]] = None,
        *,
        arguments: Optional[Dict[str, Any]] = None,
        session_id: str,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
        debug: bool = False,
        requested_run_id: Optional[str] = None,
        allow_existing_acp_run_id: bool = False,
        external_trace_id: Optional[str] = None,
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Execute the agentic workflow.

        Yields SSE-compatible events for real-time streaming to frontend.

        Args:
            user_request: The user's request to process
            user_context: Optional additional context
            arguments: Optional structured business arguments (for direct execution)
            session_id: Chat session ID for history
            user_id: Optional user ID for multi-user support
            debug: Enable debug mode

        Yields:
            Dictionary events for SSE streaming
        """
        chat_store, execution_store = self._get_stores(user_id, workspace_id)
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")

        # =====================================================================
        # Setup phase
        # =====================================================================
        context = dict(user_context or {})
        original_user_request = user_request
        interaction_response = _normalize_interaction_response(context.get("interaction_response"))
        if interaction_response:
            context["interaction_response"] = interaction_response
        requested_llm_request_mode = normalize_llm_request_mode(context.get("llm_request_mode"))
        requested_model_profile_id = str(context.get("model_profile_id") or "").strip()

        selected_ontology_id = str(context.get("selected_ontology_id") or "").strip()
        if selected_ontology_id:
            registry = get_ontology_registry()
            selected_ontology = registry.get_ontology(selected_ontology_id)
            if selected_ontology is None:
                raise ValueError("Invalid selected_ontology_id: {}".format(selected_ontology_id))
            if not selected_ontology.enabled:
                raise ValueError("Disabled selected_ontology_id: {}".format(selected_ontology_id))
            context["selected_ontology_id"] = selected_ontology.id

        stored_messages: List[Dict[str, Any]] = []
        session = chat_store.get_session(session_id)
        if not session:
            selected_profile = resolve_llm_profile_config(
                requested_model_profile_id or None,
                allow_default_fallback=not bool(requested_model_profile_id),
            )
            chat_store.create_session(
                session_id=session_id,
                metadata={
                    "llm_request_mode": requested_llm_request_mode,
                    "model_profile_id": selected_profile["id"],
                },
            )
            self._prune_old_sessions(chat_store, execution_store, user_id, workspace_id)
            session = chat_store.get_session(session_id)
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        context["llm_request_mode"] = normalize_llm_request_mode(
            session_metadata.get("llm_request_mode"),
            default=requested_llm_request_mode,
        )
        profile_from_metadata = str(session_metadata.get("model_profile_id") or "").strip()
        try:
            selected_profile = resolve_llm_profile_config(
                requested_model_profile_id or profile_from_metadata or None,
                allow_default_fallback=not bool(requested_model_profile_id),
            )
        except SystemConfigValidationError:
            if requested_model_profile_id:
                raise
            selected_profile = resolve_llm_profile_config(None)
        context["model_profile_id"] = selected_profile["id"]
        context["model_provider"] = selected_profile.get("provider")
        chat_store.merge_session_metadata(
            session_id,
            {
                "llm_request_mode": context["llm_request_mode"],
                "model_profile_id": selected_profile["id"],
            },
        )
        memory_manager = self._get_memory_manager(user_id)
        ov_session_id = self._ensure_memory_session_id(
            chat_store=chat_store,
            session_id=session_id,
            existing_metadata=session_metadata,
            memory_manager=memory_manager,
        )
        if memory_manager:
            self._append_memory_session_message(
                memory_manager,
                ov_session_id=ov_session_id,
                role="user",
                content=user_request,
                metadata={"app_session_id": session_id},
            )
        memory_context = self._build_memory_context(
            memory_manager,
            query=user_request,
            ov_session_id=ov_session_id,
            allow_injection=True,
        )
        stored_messages = execution_store.get_llm_context(session_id)
        stored_read_documents = execution_store.get_read_documents_context(session_id)
        # Guard against old TurnSummary format
        if stored_messages and isinstance(stored_messages[0], dict) and "role" not in stored_messages[0]:
            stored_messages = []

        current_turn_id = execution_store.get_turn_count(session_id) + 1
        model_user_request = _append_interaction_response_context(
            user_request,
            interaction_response,
        )
        if stored_messages:
            # Continuation of existing conversation: append new user message
            initial_messages = list(stored_messages) + [{"role": "user", "content": model_user_request}]
        else:
            # First turn: let the main agent loop seed system prompt + user message
            initial_messages = []

        logger.info("stored_messages count: %d", len(stored_messages))

        # =====================================================================
        # Shared setup: run record and chat message
        # =====================================================================
        run_id = _resolve_run_id(
            requested_run_id,
            self.run_store,
            allow_existing_acp_run=allow_existing_acp_run_id,
        )
        clear_run_sequence(run_id)

        run_metadata = {}
        run_metadata["session_id"] = session_id
        if user_id:
            run_metadata["user_id"] = user_id
        if workspace_id:
            run_metadata["workspace_id"] = workspace_id
        run_metadata["model_profile_id"] = selected_profile["id"]
        run_metadata["model_provider"] = selected_profile.get("provider")

        self.run_store.create_run(
            run_id=run_id,
            user_request=user_request,
            metadata=run_metadata,
        )

        execution_store.start_turn(
            session_id=session_id,
            turn_id=current_turn_id,
            run_id=run_id,
            user_request=user_request,
        )

        msg_metadata = {
            "run_id": run_id,
            "model_profile_id": selected_profile["id"],
            "model_provider": selected_profile.get("provider"),
        }
        if interaction_response:
            msg_metadata["interaction_response"] = interaction_response
        chat_store.add_message(
            session_id=session_id,
            role="user",
            content=user_request,
            metadata=msg_metadata,
        )

        logger.info("Starting workflow run %s, query: %s", run_id, user_request)
        logger.info("用户接口输入: request=%s arguments=%s (run_id=%s)", user_request, arguments, run_id)

        workflow_started_at = time.perf_counter()
        observer = get_observer()
        run_trace = RunRuntimeTrace(
            observer=observer,
            run_id=run_id,
            session_id=session_id,
            turn_id=current_turn_id,
        )
        run_trace.started(
            user_id=user_id,
            user_query=user_request,
            workspace_id=workspace_id,
            model=selected_profile.get("model"),
            model_provider=selected_profile.get("provider"),
            metadata={
                "turn_id": current_turn_id,
                "model_profile_id": selected_profile["id"],
                "selected_ontology_id": str(context.get("selected_ontology_id") or "").strip() or None,
                "agent_mode": resolve_agent_mode(context),
                "llm_request_mode": str(context.get("llm_request_mode") or "").strip() or None,
                "external_trace_id": external_trace_id,
            },
        )

        # Helper to emit and persist protocol event
        def emit_and_persist(event_type: str, payload: Dict[str, Any], source: Dict[str, str]):
            event = build_agent_event(
                run_id=run_id,
                session_id=session_id,
                turn_id=current_turn_id,
                event_type=event_type,
                source=source,
                payload=payload,
            )
            chat_store.append_event(session_id, event)
            return event

        def compact_chat_session() -> None:
            compact_session = getattr(chat_store, "compact_session", None)
            if not callable(compact_session):
                return

            try:
                with run_trace.phase_scope(
                    phase_name="chat_session_compact",
                    component="persistence",
                ):
                    if not compact_session(session_id):
                        logger.warning(
                            "Chat session compaction skipped for missing session: session_id=%s run_id=%s",
                            session_id,
                            run_id,
                        )
            except Exception:
                logger.warning(
                    "Failed to compact chat session event log: session_id=%s run_id=%s",
                    session_id,
                    run_id,
                    exc_info=True,
                )

        # Emit start event
        start_payload: Dict[str, Any] = {
            "user_preview": user_request[:200],
        }
        if arguments:
            start_payload["arguments"] = arguments

        yield emit_and_persist(
            "turn.started",
            start_payload,
            {"node": "orchestrator", "component": "workflow"},
        )

        # =====================================================================
        # Direct Execution Branch — bypass the Jiuwen ReAct loop for clear-cut
        # script-type ontology operations.
        # =====================================================================
        direct_config = self.agent_config.direct_execution
        logger.info(
            "Direct execution 开关: enabled=%s, selected_ontology_only=%s, confidence_threshold=%.2f (run_id=%s)",
            direct_config.enabled,
            direct_config.selected_ontology_only,
            direct_config.confidence_threshold,
            run_id,
        )
        if direct_config.enabled:
            runtime_ctx = RuntimeContext(
                session_id=session_id,
                run_id=run_id,
                turn_id=current_turn_id,
                user_id=user_id,
                workspace_id=workspace_id or None,
                selected_ontology_id=selected_ontology_id or None,
            )
            router = OperationDirectRouter(
                config=direct_config,
            )
            route_started_at = time.perf_counter()
            try:
                decision = router.route(
                    user_request=original_user_request,
                    context=context,
                    runtime_context=runtime_ctx,
                    arguments=arguments,
                )
            except Exception as exc:
                run_trace.record_phase(
                    phase_name="direct_execution_route",
                    component="direct_execution",
                    status=RuntimeStatus.ERROR,
                    duration_ms=int((time.perf_counter() - route_started_at) * 1000),
                    metrics={
                        "error_type": type(exc).__name__,
                        "error_message": str(exc)[:300],
                    },
                )
                raise

            run_trace.record_phase(
                phase_name="direct_execution_route",
                component="direct_execution",
                duration_ms=int((time.perf_counter() - route_started_at) * 1000),
                metrics={
                    "route": decision.route,
                    "reason": decision.reason,
                    "confidence": decision.confidence,
                    "ontology_id": decision.ontology_id or "",
                    "operation_key": decision.operation_key or "",
                    "missing_business_arguments": list(decision.missing_business_arguments or []),
                    "runtime_requirements": list(decision.runtime_requirements or []),
                },
            )

            if decision.route == "direct_script":
                from .config.system_config import load_system_config
                sys_config = load_system_config()
                project_root = str(sys_config["storage"]["project_root"])
                sse_events, exec_result = run_direct_script(
                    decision=decision,
                    runtime_context=runtime_ctx,
                    project_root=project_root,
                    timeout_seconds=self.agent_config.execute_script_timeout,
                    observer=observer,
                )
                chat_store.append_events(session_id, sse_events)
                for event in sse_events:
                    yield event

                final_output = exec_result.output
                is_error = exec_result.status == "error"
                turn_status = "error" if is_error else "success"

                result = {
                    "direct_execution": True,
                    "status": exec_result.status,
                    "output": exec_result.output,
                    "elapsed_ms": exec_result.elapsed_ms,
                }
                if is_error:
                    result["error"] = exec_result.error

                execution_store.complete_turn(
                    session_id=session_id,
                    turn_id=current_turn_id,
                    final_output=final_output,
                    summary=None,
                )
                execution_store.append_to_llm_context(
                    session_id, {"role": "user", "content": original_user_request},
                )
                execution_store.append_to_llm_context(
                    session_id, {"role": "assistant", "content": final_output},
                )

                if is_error:
                    self.run_store.fail_run(run_id, exec_result.error or "Direct execution failed")
                else:
                    self.run_store.complete_run(run_id, result)

                logger.info(
                    "Direct execution completed: run_id=%s ontology=%s operation=%s status=%s",
                    run_id,
                    decision.ontology_id,
                    decision.operation_key,
                    exec_result.status,
                )
                turn_finished_event = emit_and_persist(
                    "turn.finished",
                    {"status": turn_status, "final_output": final_output},
                    {"node": "direct_execution", "component": "service"},
                )
                if is_error:
                    run_trace.failed(
                        final_answer=final_output,
                        error_type="DirectExecutionError",
                        error_message=exec_result.error or final_output,
                        duration_ms=int((time.perf_counter() - workflow_started_at) * 1000),
                    )
                else:
                    run_trace.finished(
                        final_answer=final_output,
                        duration_ms=int((time.perf_counter() - workflow_started_at) * 1000),
                    )
                yield turn_finished_event
                if ov_session_id:
                    self._append_memory_session_message(
                        memory_manager,
                        ov_session_id=ov_session_id,
                        role="assistant",
                        content=final_output,
                        metadata={"status": turn_status, "turn_id": current_turn_id},
                    )
                    self._append_memory_turn_summary(
                        memory_manager,
                        ov_session_id=ov_session_id,
                        user_request=original_user_request,
                        final_output=final_output,
                        status=turn_status,
                    )
                    self._commit_memory_session(
                        memory_manager, ov_session_id, chat_store, session_id,
                    )
                compact_chat_session()
                return

            if decision.route == "missing_args":
                param_names = ", ".join(
                    "- {}".format(arg)
                    for arg in decision.missing_business_arguments
                )
                final_output = (
                    "当前 " + str(decision.operation_key or "") + " 流程需要以下参数：\n"
                    + param_names + "\n\n请补充后我再执行。"
                )
                result = {
                    "direct_execution": True,
                    "status": "missing_args",
                    "missing_arguments": decision.missing_business_arguments,
                }
                execution_store.complete_turn(
                    session_id=session_id,
                    turn_id=current_turn_id,
                    final_output=final_output,
                    summary=None,
                )
                execution_store.append_to_llm_context(
                    session_id, {"role": "user", "content": original_user_request},
                )
                execution_store.append_to_llm_context(
                    session_id, {"role": "assistant", "content": final_output},
                )
                self.run_store.complete_run(run_id, result)
                turn_finished_event = emit_and_persist(
                    "turn.finished",
                    {"status": "success", "final_output": final_output},
                    {"node": "direct_execution", "component": "service"},
                )
                run_trace.finished(
                    final_answer=final_output,
                    duration_ms=int((time.perf_counter() - workflow_started_at) * 1000),
                )
                yield turn_finished_event
                compact_chat_session()
                return

            # decision.route == "fallback_agent_loop" → continue below

        # =====================================================================
        # Build initial state (agent loop path)
        # =====================================================================
        initial_state: AgenticState = {
            "run_id": run_id,
            "session_id": session_id,
            "user_id": user_id,
            "workspace_id": workspace_id,
            "user_request": model_user_request,
            "user_context": context,
            "user_lang": context.get("lang"),
            "ov_session_id": ov_session_id,
            "memory_context": memory_context or None,
            "results": {},
            "completed_ids": [],
            "failed_ids": [],
            "errors": [],
            "messages": initial_messages,
            "read_documents": stored_read_documents,
            "evolution_recall_records": [],
            "debug": debug,
            "final_output": None,
            "current_turn_id": current_turn_id,
        }

        main_agent_started_at = time.perf_counter()
        # =====================================================================
        # Stream graph execution (shared for both paths)
        # =====================================================================
        event_persistence = _StreamEventPersistenceBuffer(
            chat_store=chat_store,
            session_id=session_id,
            run_id=run_id,
            turn_id=current_turn_id,
            observer=observer,
        )
        try:
            last_state = initial_state
            for event in self._stream_agent_loop(
                initial_state,
                memory_manager=memory_manager,
                observer=observer,
            ):
                run = self.run_store.get_run(run_id)
                if run and run.status == RunStatus.CANCELLED:
                    raise WorkflowCancelledError(
                        "Workflow cancelled by user",
                        details={"run_id": run_id},
                    )
                if isinstance(event, tuple):
                    mode, payload = event
                    if mode == "custom":
                        event_persistence.add(payload)
                        yield payload
                    elif mode == "values":
                        last_state = payload

            event_persistence.close()

            # =================================================================
            # Finalize turn: persist messages and complete records
            # =================================================================
            final_output = last_state.get("final_output")
            result = {
                "completed_tasks": last_state.get("completed_ids", []),
                "failed_tasks": last_state.get("failed_ids", []),
                "results": last_state.get("results", {}),
                "errors": last_state.get("errors", []),
            }

            # Persist full message history for next turn
            final_messages = list(last_state.get("messages", []))
            final_read_documents = last_state.get("read_documents")
            with run_trace.phase_scope(
                phase_name="llm_context_save",
                component="persistence",
                metrics={"message_count": len(final_messages)},
            ):
                execution_store.set_llm_context(session_id, final_messages)
                execution_store.set_read_documents_context(session_id, final_read_documents)

            pending_interaction = last_state.get("pending_user_interaction")
            if isinstance(pending_interaction, dict) and pending_interaction:
                final_output = _build_awaiting_final_output(final_output, pending_interaction)
                awaiting_result = {
                    **result,
                    "status": "awaiting_input",
                    "pending_user_interaction": pending_interaction,
                }
                with run_trace.phase_scope(
                    phase_name="turn_awaiting_input",
                    component="persistence",
                    metrics={"final_output_chars": len(str(final_output or ""))},
                ):
                    execution_store.await_turn(
                        session_id=session_id,
                        turn_id=current_turn_id,
                        final_output=final_output,
                        summary=None,
                    )
                    self.run_store.await_run(
                        run_id,
                        awaiting_result,
                        metadata={"pending_user_interaction": pending_interaction},
                    )

                turn_finished_event = emit_and_persist(
                    "turn.finished",
                    {"status": "awaiting_input", "final_output": final_output},
                    {"node": "orchestrator", "component": "workflow"},
                )
                run_trace.finished(
                    final_answer=(
                        final_output.get("text")
                        if isinstance(final_output, dict)
                        else final_output
                    ),
                    duration_ms=int((time.perf_counter() - main_agent_started_at) * 1000),
                )
                yield turn_finished_event
                return

            self._record_evolution_recall_outcomes(last_state)

            parsed_summary = parse_execution_summary(
                final_output if isinstance(final_output, str) else None
            )
            with run_trace.phase_scope(
                phase_name="turn_complete",
                component="persistence",
                metrics={"final_output_chars": len(str(final_output or ""))},
            ):
                execution_store.complete_turn(
                    session_id=session_id,
                    turn_id=current_turn_id,
                    final_output=final_output,
                    summary=(
                        json.dumps(parsed_summary, ensure_ascii=False)
                        if parsed_summary
                        else None
                    ),
                )

            # Persist recalled fingerprints so /runs/<id>/feedback can vote
            # on the same operation-evolution records injected this turn.
            evo_records = list(last_state.get("evolution_recall_records") or [])
            with run_trace.phase_scope(
                phase_name="run_complete",
                component="persistence",
                metrics={"evolution_recall_count": len(evo_records)},
            ):
                if evo_records:
                    self.run_store.update_metadata(
                        run_id,
                        {"evolution_recall_records": evo_records},
                    )
                self.run_store.complete_run(run_id, result)
            logger.info("Workflow run %s completed successfully", run_id)

            with run_trace.phase_scope(
                phase_name="turn_finished_emit",
                component="workflow",
                metrics={"final_output_chars": len(str(final_output or ""))},
            ):
                turn_finished_event = emit_and_persist(
                    "turn.finished",
                    {"status": "success", "final_output": final_output},
                    {"node": "orchestrator", "component": "workflow"},
                )
            run_trace.finished(
                final_answer=final_output,
                duration_ms=int((time.perf_counter() - main_agent_started_at) * 1000),
            )
            yield turn_finished_event

            self._submit_reflection(
                run_id=run_id,
                session_id=session_id,
                turn_id=current_turn_id,
                user_id=str(user_id or "anonymous"),
                workspace_id=workspace_id,
                ontology_id=str(context.get("selected_ontology_id") or "").strip() or None,
                status="success",
                user_request=original_user_request,
                final_output=final_output,
                messages=last_state.get("messages", []),
                duration_ms=int((time.perf_counter() - main_agent_started_at) * 1000),
            )

            if ov_session_id:
                with run_trace.phase_scope(
                    phase_name="memory_append_assistant_message",
                    component="memory",
                ):
                    self._append_memory_session_message(
                        memory_manager,
                        ov_session_id=ov_session_id,
                        role="assistant",
                        content=final_output,
                        metadata={"status": "success", "turn_id": current_turn_id},
                    )

                with run_trace.phase_scope(
                    phase_name="memory_append_turn_summary",
                    component="memory",
                ):
                    self._append_memory_turn_summary(
                        memory_manager,
                        ov_session_id=ov_session_id,
                        user_request=original_user_request,
                        final_output=final_output,
                        status="success",
                    )

                with run_trace.phase_scope(
                    phase_name="memory_commit_session",
                    component="memory",
                ):
                    self._commit_memory_session(memory_manager, ov_session_id, chat_store, session_id)

        except WorkflowCancelledError as exc:
            event_persistence.close()
            logger.info("Workflow run %s cancelled: %s", run_id, exc.message)
            cancel_reason = "unspecified"
            current_run = self.run_store.get_run(run_id)
            if current_run:
                run_cancel_reason = current_run.metadata.get("cancel_reason")
                if isinstance(run_cancel_reason, str) and run_cancel_reason.strip():
                    cancel_reason = run_cancel_reason
            self.run_store.cancel_run(run_id, metadata={"cancel_reason": cancel_reason})

            # Persist partial messages for user_stop so next turn has context
            if cancel_reason == "user_stop":
                final_messages = list(last_state.get("messages", []))
                execution_store.set_llm_context(session_id, final_messages)
                execution_store.set_read_documents_context(session_id, last_state.get("read_documents"))
                if ov_session_id:
                    self._append_memory_session_message(
                        memory_manager,
                        ov_session_id=ov_session_id,
                        role="assistant",
                        content=last_state.get("final_output"),
                        metadata={"status": "cancelled", "turn_id": current_turn_id},
                    )
                    self._append_memory_turn_summary(
                        memory_manager,
                        ov_session_id=ov_session_id,
                        user_request=original_user_request,
                        final_output=last_state.get("final_output"),
                        status="cancelled",
                    )
                    self._commit_memory_session(memory_manager, ov_session_id, chat_store, session_id)

            cancelled_output = {
                "cancel_reason": cancel_reason,
                "message": exc.message,
                "partial_output": last_state.get("final_output"),
                "completed_tasks": last_state.get("completed_ids", []),
                "failed_tasks": last_state.get("failed_ids", []),
            }
            execution_store.cancel_turn(
                session_id=session_id,
                turn_id=current_turn_id,
                reason=cancel_reason,
                final_output=cancelled_output,
                summary=None,
            )

            run_trace.cancelled(
                final_answer=last_state.get("final_output"),
                cancel_reason=cancel_reason,
                duration_ms=int((time.perf_counter() - main_agent_started_at) * 1000),
                error_type=type(exc).__name__,
                error_message=exc.message,
            )

            yield emit_and_persist(
                "turn.finished",
                {
                    "status": "cancelled",
                    "error": exc.message,
                    "cancel_reason": cancel_reason,
                },
                {"node": "orchestrator", "component": "workflow"},
            )

            # 用户手动终止的 turn 不触发 reflection：中断的轨迹缺少收敛证据，
            # 提炼出的 procedure/pitfall 往往是错的，会污染记忆。

        except Exception as exc:
            event_persistence.close()
            logger.exception("Workflow run %s failed: %s", run_id, exc)
            public_message = _public_workflow_error_message(exc)
            self.run_store.fail_run(run_id, public_message)

            execution_store.fail_turn(
                session_id=session_id,
                turn_id=current_turn_id,
                error=public_message,
            )

            run_trace.failed(
                final_answer=public_message,
                error_type=type(exc).__name__,
                error_message=str(exc),
                duration_ms=int((time.perf_counter() - main_agent_started_at) * 1000),
            )

            yield emit_and_persist(
                "turn.finished",
                {"status": "error", "error": public_message},
                {"node": "orchestrator", "component": "workflow"},
            )

        finally:
            event_persistence.close()
            compact_chat_session()
            get_llm_router().clear_run(run_id)
            clear_subagent_manager(run_id)
            clear_run_sequence(run_id)

    def _get_stores(
        self,
        user_id: Optional[str],
        workspace_id: Optional[str] = None,
    ) -> tuple[ChatStore, ExecutionStore]:
        if user_id and self.user_store_manager:
            return (
                self.user_store_manager.get_chat_store(user_id, workspace_id or "default"),
                self.user_store_manager.get_execution_store(user_id, workspace_id or "default"),
            )
        return self.chat_store, self.execution_store

    def _prune_old_sessions(
        self,
        chat_store: ChatStore,
        execution_store: ExecutionStore,
        user_id: Optional[str],
        workspace_id: Optional[str],
    ) -> list[str]:
        max_sessions = self.agent_config.limits.session_max_per_user_workspace
        if user_id and self.user_store_manager:
            return self.user_store_manager.prune_sessions(
                user_id,
                workspace_id or "default",
                max_sessions=max_sessions,
            )

        deleted_session_ids = chat_store.prune_old_sessions(max_sessions)
        for deleted_session_id in deleted_session_ids:
            execution_store.delete_session(deleted_session_id)
        return deleted_session_ids

    def _get_memory_manager(self, user_id: Optional[str]):
        try:
            return self.memory_factory.for_user(user_id or "anonymous")
        except Exception:
            logger.warning("Failed to resolve memory manager for user=%s", user_id, exc_info=True)
            return None

    def _ensure_memory_session_id(
        self,
        *,
        chat_store: ChatStore,
        session_id: str,
        existing_metadata: Dict[str, Any],
        memory_manager: Any,
    ) -> Optional[str]:
        if memory_manager is None:
            return None
        try:
            ov_session_id = memory_manager.ensure_session(
                app_session_id=session_id,
                existing_session_id=existing_metadata.get("ov_session_id"),
            )
        except Exception:
            logger.warning("Failed to ensure OpenViking session for %s", session_id, exc_info=True)
            return None

        updates = {
            "ov_session_id": ov_session_id,
            "ov_memory_version": str(
                getattr(memory_manager, "memory_version", "") or "openviking-v1"
            ),
        }
        if any(existing_metadata.get(key) != value for key, value in updates.items()):
            chat_store.merge_session_metadata(session_id, updates)
        return ov_session_id

    def _append_memory_session_message(
        self,
        memory_manager: Any,
        *,
        ov_session_id: Optional[str],
        role: str,
        content: Any,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        if not memory_manager:
            return
        try:
            memory_manager.append_session_message(
                ov_session_id=ov_session_id,
                role=role,
                content=content,
                metadata=metadata,
            )
        except Exception:
            logger.warning("Failed to append OpenViking session message", exc_info=True)

    def _append_memory_turn_summary(
        self,
        memory_manager: Any,
        *,
        ov_session_id: Optional[str],
        user_request: str,
        final_output: Any,
        status: str,
    ) -> None:
        if not memory_manager:
            return
        try:
            memory_manager.append_turn_summary(
                ov_session_id=ov_session_id,
                user_request=user_request,
                final_output=final_output,
                status=status,
            )
        except Exception:
            logger.warning("Failed to append OpenViking turn summary", exc_info=True)

    def _commit_memory_session(
        self,
        memory_manager: Any,
        ov_session_id: Optional[str],
        chat_store: ChatStore,
        session_id: str,
    ) -> None:
        if not memory_manager or not ov_session_id:
            return
        try:
            memory_manager.commit_session(ov_session_id)
            chat_store.merge_session_metadata(
                session_id,
                {
                    "ov_last_commit_at": datetime.utcnow().isoformat() + "Z",
                    "ov_last_commit_status": "committed",
                },
            )
        except Exception:
            logger.warning("Failed to commit OpenViking session", exc_info=True)

    def _record_evolution_recall_outcomes(self, state: AgenticState) -> None:
        records = state.get("evolution_recall_records") or []
        if not records:
            return
        try:
            buffer = get_proposal_buffer()
        except Exception:
            logger.warning("Failed to open operation-evolution proposal buffer for outcome tracking", exc_info=True)
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            fingerprints = [
                str(fp).strip()
                for fp in (record.get("fingerprints") or [])
                if str(fp).strip()
            ]
            if not fingerprints:
                continue
            try:
                buffer.record_outcome(
                    fingerprints,
                    success=True,
                    workspace_id=str(record.get("workspace_id") or "").strip() or None,
                    ontology_id=str(record.get("ontology_id") or "").strip() or None,
                    operation_key=str(record.get("operation_key") or "").strip() or None,
                )
            except Exception:
                logger.warning("Failed to record operation-evolution recall outcome", exc_info=True)

    def _build_memory_context(
        self,
        memory_manager: Any,
        *,
        query: str,
        ov_session_id: Optional[str],
        allow_injection: bool,
    ) -> str:
        if not allow_injection or not memory_manager:
            return ""
        try:
            return memory_manager.build_memory_context(
                query=query,
                ov_session_id=ov_session_id,
            )
        except Exception:
            logger.warning("Failed to build memory context", exc_info=True)
            return ""

    def _submit_reflection(
        self,
        *,
        run_id: str,
        session_id: str,
        turn_id: int,
        user_id: str,
        workspace_id: Optional[str],
        ontology_id: Optional[str],
        status: str,
        user_request: str,
        final_output: Any,
        messages: List[Dict[str, Any]],
        duration_ms: int,
    ) -> None:
        if not self.passive_reflection_enabled or self.reflection_worker is None:
            logger.info(
                "reflection skipped run=%s session=%s turn=%s reason=passive_reflection_disabled workspace=%s status=%s",
                run_id,
                session_id,
                turn_id,
                workspace_id,
                status,
            )
            return
        if not ontology_id:
            logger.info(
                "reflection skipped run=%s session=%s turn=%s reason=missing_ontology_id workspace=%s status=%s",
                run_id,
                session_id,
                turn_id,
                workspace_id,
                status,
            )
            return
        try:
            snapshot_messages = copy.deepcopy(list(messages or []))
            final_output_text = final_output if isinstance(final_output, str) else (
                json.dumps(final_output, ensure_ascii=False, default=str) if final_output else ""
            )
            snapshot = build_reflection_snapshot(
                run_id=run_id,
                session_id=session_id,
                turn_id=turn_id,
                user_id=user_id,
                workspace_id=workspace_id,
                ontology_id=ontology_id,
                status=status,
                user_request=user_request,
                final_output=final_output_text,
                messages=snapshot_messages,
                iteration_count=self._count_iterations(snapshot_messages),
                duration_ms=duration_ms,
            )
            self.reflection_worker.submit(snapshot)
        except Exception:
            logger.warning("Failed to submit reflection for run=%s", run_id, exc_info=True)

    def vote_on_run_memories(self, run_id: str, *, vote: str) -> int:
        """Cast 👍/👎 on the proposal fingerprints injected into this run.

        Returns the count of fingerprints actually updated. Returns 0 when
        the run never recalled anything (graceful no-op — no anchor to
        attach the vote to).
        """
        run = self.run_store.get_run(run_id)
        if run is None:
            return 0
        normalized_vote = str(vote or "").strip().lower()
        if normalized_vote not in {"up", "down", "none"}:
            return 0
        previous_vote = str((run.metadata or {}).get("evolution_feedback_vote") or "").strip().lower() or None
        if previous_vote not in {"up", "down"}:
            previous_vote = None
        if normalized_vote == previous_vote:
            return 0

        records = (run.metadata or {}).get("evolution_recall_records") or []
        if not records:
            return 0
        grouped: Dict[tuple, set] = {}
        for r in records:
            if not isinstance(r, dict):
                continue
            ws = (r.get("workspace_id") or "").strip() or None
            onto = (r.get("ontology_id") or "").strip()
            op = (r.get("operation_key") or "").strip()
            if not (onto and op):
                continue
            fps = [
                str(fp).strip()
                for fp in (r.get("fingerprints") or [])
                if str(fp).strip()
            ]
            if not fps:
                continue
            grouped.setdefault((ws, onto, op), set()).update(fps)
        if not grouped:
            return 0
        try:
            buf = get_proposal_buffer()
            updated = 0
            for (ws, onto, op), fps in grouped.items():
                buf.record_vote(
                    list(fps),
                    vote=normalized_vote,
                    previous_vote=previous_vote,
                    workspace_id=ws,
                    ontology_id=onto,
                    operation_key=op,
                )
                updated += len(fps)
            self.run_store.update_metadata(
                run_id,
                {"evolution_feedback_vote": None if normalized_vote == "none" else normalized_vote},
            )
            return updated
        except Exception:
            logger.warning("vote_on_run_memories failed run=%s", run_id, exc_info=True)
            return 0

    @staticmethod
    def _count_iterations(messages: List[Dict[str, Any]]) -> int:
        count = 0
        for msg in messages or []:
            if isinstance(msg, dict) and msg.get("role") == "assistant":
                count += 1
        return count

    def delete_memory_session(
        self,
        session_id: str,
        user_id: Optional[str],
        workspace_id: Optional[str] = None,
    ) -> None:
        chat_store, _execution_store = self._get_stores(user_id, workspace_id)
        session = chat_store.get_session(session_id)
        if not session:
            return
        ov_session_id = str((session.metadata or {}).get("ov_session_id") or "").strip()
        if not ov_session_id:
            return
        memory_manager = self._get_memory_manager(user_id)
        if not memory_manager:
            return
        try:
            memory_manager.delete_session(ov_session_id)
        except Exception:
            logger.warning("Failed to delete OpenViking session %s", ov_session_id, exc_info=True)

    def get_run_status(self, run_id: str) -> Optional[Dict[str, Any]]:
        """Get current status of a run."""
        run = self.run_store.get_run(run_id)
        if not run:
            return None

        return {
            "run_id": run.run_id,
            "status": run.status.value,
            "created_at": run.created_at.isoformat(),
            "updated_at": run.updated_at.isoformat(),
            "completed_at": run.completed_at.isoformat() if run.completed_at else None,
            "result": run.result,
            "error": run.error,
        }

    def cancel_run(self, run_id: str, reason: Optional[str] = None) -> bool:
        """Cancel a running workflow."""
        cancel_reason = reason.strip() if isinstance(reason, str) and reason.strip() else "unspecified"
        existing_run = self.run_store.get_run(run_id)
        if existing_run is None:
            return False

        run = self.run_store.cancel_run(
            run_id,
            metadata={"cancel_reason": cancel_reason},
        )
        if run is None:
            return False
        get_llm_router().cancel_run(run_id)

        return True


def _resolve_run_id(
    requested_run_id: Optional[str],
    run_store: RunStore,
    *,
    allow_existing_acp_run: bool = False,
) -> str:
    normalized = str(requested_run_id or "").strip()
    if normalized:
        existing = run_store.get_run(normalized)
        if existing is None or (allow_existing_acp_run and existing.metadata.get("source") == "acp"):
            return normalized
    return str(uuid.uuid4())
