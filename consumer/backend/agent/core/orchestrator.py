"""Agentic workflow orchestrator."""

import copy
import json
import queue
import threading
import time
from typing import Any, Dict, Generator, List, Optional
import uuid

from openai import APIConnectionError, APIStatusError, APITimeoutError

from config import AgentConfig, AppConfig
from config.model_profile_refs import model_profile_ref_to_dict
from config.system_config import (
    SystemConfigValidationError,
    resolve_effective_llm_profile_config,
    resolve_llm_profile_config,
)
from agent.direct_execution import OperationDirectRouter
from agent.direct_execution.models import RuntimeContext
from agent.direct_execution.runner import run_direct_script
from agent.evolution import (
    PatcherConfig,
    ReflectionWorker,
    build_reflection_snapshot,
    get_proposal_buffer,
    init_operation_patcher,
    parse_execution_summary,
)
from agent.core.engine import AgentLoopEngine
from agent.core.shared.modes import VALID_AGENT_MODES, resolve_agent_mode
from agent.memory import MemoryFactory
from agent.state import AgenticState
from llm import get_llm_router
from integration.observability import RunRuntimeTrace, RuntimeStatus, get_observer
from integration.observability.trace_context import parse_traceparent
from persistence.run_queue import RunAdmissionQueue, RunSlotManager
from persistence.run_queue import TokenBucketRateLimiter
from persistence.run_store import RunStore
from persistence.run_store import RunStatus
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore
from workspace.user_store_manager import UserStoreManager
from agent.sub_agents.manager import clear_subagent_manager
from agent.tools.events import tool_output_artifact_writer_context
from utils.logging import get_logger
from utils.identifiers import validate_session_id
from llm.llm_request_mode import normalize_llm_request_mode
from ontology.ontologies import get_ontology_registry
from utils.streaming import build_agent_event, clear_run_sequence, stream_writer_context
from workspace.workspaces import workspace_scope
from agent.exceptions import WorkflowCancelledError

logger = get_logger("orchestrator")

_PUBLIC_LLM_ERROR_DETAIL_MAX_LENGTH = 800


def _build_tool_output_artifact_writer(chat_store, *, session_id: str, run_id: str):
    store_tool_output = getattr(chat_store, "store_tool_output", None)
    if not callable(store_tool_output):
        return None

    def write(tool_call_id: Optional[str], output: str):
        return store_tool_output(
            session_id=session_id,
            run_id=run_id,
            tool_call_id=tool_call_id,
            output=output,
        )

    return write


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

    return "Workflow execution failed"


def _normalize_interaction_response(value: Any) -> Optional[Dict[str, Any]]:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("interaction_response must be an object")

    interaction_id = str(value.get("interaction_id") or value.get("id") or "").strip()
    response_type = str(value.get("type") or "").strip() or None
    option_id = str(value.get("option_id") or "").strip() or None
    normalized: Dict[str, Any] = {"interaction_id": interaction_id}
    if "value" in value:
        normalized["value"] = value.get("value")
    elif "response" in value:
        normalized["value"] = value.get("response")
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
            app_config = AppConfig.from_env()
        self.chat_store = chat_store or ChatStore(app_config.chat_storage_dir)
        self.execution_store = execution_store or ExecutionStore(app_config.execution_storage_dir)
        self.user_store_manager = user_store_manager
        self.memory_factory = memory_factory or MemoryFactory()
        self.slot_manager = RunSlotManager()
        self.run_admission_queue = RunAdmissionQueue(self.slot_manager)
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
            session_id = str(state.get("session_id") or "")
            run_id = str(state.get("run_id") or "")
            turn_id = int(state.get("current_turn_id") or 0)
            try:
                artifact_chat_store, _artifact_execution_store = self._get_stores(
                    str(state.get("user_id") or "").strip() or None,
                    workspace_id,
                )
                artifact_writer = _build_tool_output_artifact_writer(
                    artifact_chat_store,
                    session_id=session_id,
                    run_id=run_id,
                )
                with workspace_scope(workspace_id), tool_output_artifact_writer_context(
                    artifact_writer
                ):
                    engine = AgentLoopEngine(
                        memory_manager=memory_manager,
                        agent_config=self.agent_config,
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

    @staticmethod
    def _infer_toolbased_ontology_version(
        workspace_id: Optional[str],
        read_documents: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """For tool-based sessions (no session-level selected ontology), infer
        the ontology version the agent actually read, from the read_documents
        ledger, and return it for the per-turn ontology_version stamp.

        The version is taken from the ``ontology_version`` field written into
        each read-document record *at read time* (see
        ``build_read_document_record``), NOT by re-reading the manifest at turn
        completion. Re-reading the manifest races: if the active version is
        switched between the agent's read and turn completion, the stamp would
        record the new version the agent never saw, silently defeating gate 3.
        The read-time version is the version actually served to the agent.

        Returns the version when exactly one ontology was read and all its
        records agree on one version. Returns None if 0 or >1 ontologies were
        read, if no record carries a version, or if records disagree on the
        version (a cross-version turn — ambiguous, left to the approval-time
        drift check).
        """
        if not isinstance(read_documents, dict) or not workspace_id:
            return None
        by_path = read_documents.get("by_path") or {}
        onto_versions: Dict[str, set] = {}
        if isinstance(by_path, dict):
            for rec in by_path.values():
                if not isinstance(rec, dict):
                    continue
                oid = str(rec.get("ontology_id") or "").strip()
                if not oid:
                    continue
                version = str(rec.get("ontology_version") or "").strip()
                if not version:
                    continue
                onto_versions.setdefault(oid, set()).add(version)
        if len(onto_versions) != 1:
            return None
        versions = next(iter(onto_versions.values()))
        if len(versions) != 1:
            return None
        return next(iter(versions))

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
        traceparent: Optional[str] = None,
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
        trace_context = parse_traceparent(traceparent)
        chat_store, execution_store = self._get_stores(user_id, workspace_id)
        session_id = validate_session_id(session_id)

        # =====================================================================
        # Setup phase
        # =====================================================================
        context = dict(user_context or {})
        original_user_request = user_request
        interaction_response = _normalize_interaction_response(context.get("interaction_response"))
        requested_llm_request_mode = normalize_llm_request_mode(context.get("llm_request_mode"))
        requested_model_profile_id = str(context.get("model_profile_id") or "").strip()
        requested_model_profile_ref = context.get("model_profile_ref") or requested_model_profile_id
        requested_selected_ontology_id = str(context.get("selected_ontology_id") or "").strip()
        requested_agent_mode_raw = str(context.get("agent_mode") or "").strip().lower()
        requested_agent_mode = resolve_agent_mode(context)

        stored_messages: List[Dict[str, Any]] = []
        session = chat_store.get_session(session_id)
        session_metadata = dict(getattr(session, "metadata", {}) or {})
        locked_selected_ontology_id = str(session_metadata.get("selected_ontology_id") or "").strip()
        locked_agent_mode = str(session_metadata.get("agent_mode") or "").strip().lower()
        if locked_agent_mode not in VALID_AGENT_MODES:
            locked_agent_mode = ""
        if locked_agent_mode and requested_agent_mode_raw and requested_agent_mode != locked_agent_mode:
            raise ValueError("agent_mode is locked for this session: {}".format(locked_agent_mode))
        effective_agent_mode = locked_agent_mode or requested_agent_mode
        context["agent_mode"] = effective_agent_mode

        session_turn_count = execution_store.get_turn_count(session_id)
        interaction_response = execution_store.validate_interaction_response(
            session_id,
            interaction_response,
        )
        if interaction_response:
            context["interaction_response"] = interaction_response
        if locked_selected_ontology_id and requested_selected_ontology_id and (
            requested_selected_ontology_id != locked_selected_ontology_id
        ):
            raise ValueError(
                "selected_ontology_id is locked for this session: {}".format(locked_selected_ontology_id)
            )
        if requested_selected_ontology_id and session_turn_count > 0 and not locked_selected_ontology_id:
            raise ValueError("selected_ontology_id can only be set before the first user turn")

        selected_ontology = None
        selected_ontology_id = locked_selected_ontology_id or requested_selected_ontology_id
        if locked_selected_ontology_id:
            context["selected_ontology_id"] = locked_selected_ontology_id
        elif requested_selected_ontology_id:
            registry = get_ontology_registry()
            selected_ontology = registry.get_ontology(requested_selected_ontology_id)
            if selected_ontology is None:
                raise ValueError("Invalid selected_ontology_id: {}".format(requested_selected_ontology_id))
            if not selected_ontology.enabled:
                raise ValueError("Disabled selected_ontology_id: {}".format(requested_selected_ontology_id))
            context["selected_ontology_id"] = selected_ontology.id

        # Resolve active_version for the per-turn ontology_version stamp. For
        # the locked-ontology branch (selected_ontology not loaded above) read
        # it from the manifest file directly — do NOT re-query the registry,
        # locked sessions must not reload the ontology.
        stamp_ontology_version = None
        if context.get("selected_ontology_id"):
            if selected_ontology is not None:
                stamp_ontology_version = (
                    str(getattr(selected_ontology, "active_version", "") or "").strip() or None
                )
            else:
                try:
                    from workspace.workspaces import get_workspace_ontologies_root
                    from ontology.ontologies import load_ontology_manifest_data
                    onto_dir = get_workspace_ontologies_root(workspace_id) / context["selected_ontology_id"]
                    stamp_ontology_version = (
                        str(load_ontology_manifest_data(onto_dir).get("active_version") or "").strip() or None
                    )
                except Exception:
                    stamp_ontology_version = None

        context["llm_request_mode"] = normalize_llm_request_mode(
            session_metadata.get("llm_request_mode"),
            default=requested_llm_request_mode,
        )
        profile_from_metadata = str(session_metadata.get("model_profile_id") or "").strip()
        profile_ref_from_metadata = session_metadata.get("model_profile_ref") or profile_from_metadata
        has_requested_model_profile = bool(requested_model_profile_id or context.get("model_profile_ref"))
        try:
            if workspace_id:
                selected_profile = resolve_effective_llm_profile_config(
                    requested_model_profile_ref or profile_ref_from_metadata or None,
                    workspace_id=workspace_id,
                    allow_default_fallback=not has_requested_model_profile,
                )
            else:
                selected_profile = resolve_llm_profile_config(
                    requested_model_profile_id or profile_from_metadata or None,
                    allow_default_fallback=not bool(requested_model_profile_id),
                )
        except SystemConfigValidationError:
            if has_requested_model_profile:
                raise
            selected_profile = (
                resolve_effective_llm_profile_config(None, workspace_id=workspace_id)
                if workspace_id
                else resolve_llm_profile_config(None)
            )
        selected_profile_ref = model_profile_ref_to_dict(
            selected_profile.get("model_profile_ref") or selected_profile["id"]
        )
        context["model_profile_id"] = selected_profile["id"]
        context["model_profile_ref"] = selected_profile_ref
        context["model_provider"] = selected_profile.get("provider")
        if not session:
            create_metadata = {
                "agent_mode": effective_agent_mode,
                "llm_request_mode": context["llm_request_mode"],
                "model_profile_id": selected_profile["id"],
                "model_profile_ref": selected_profile_ref,
            }
            if selected_ontology is not None:
                create_metadata.update({
                    "selected_ontology_id": selected_ontology.id,
                    "selected_ontology_name": selected_ontology.name,
                })
            chat_store.create_session(
                session_id=session_id,
                metadata=create_metadata,
            )
            self._prune_old_sessions(chat_store, execution_store, user_id, workspace_id)
            session = chat_store.get_session(session_id)

        session_metadata_update = {
            "agent_mode": effective_agent_mode,
            "llm_request_mode": context["llm_request_mode"],
            "model_profile_id": selected_profile["id"],
            "model_profile_ref": selected_profile_ref,
        }
        if selected_ontology is not None:
            session_metadata_update.update({
                "selected_ontology_id": selected_ontology.id,
                "selected_ontology_name": selected_ontology.name,
            })
        chat_store.merge_session_metadata(
            session_id,
            session_metadata_update,
        )
        memory_manager = self._get_memory_manager(user_id)
        memory_context = self._build_memory_context(
            memory_manager,
            query=user_request,
            allow_injection=True,
        )
        stored_messages = execution_store.get_llm_context(session_id)
        stored_read_documents = execution_store.get_read_documents_context(session_id)
        # Guard against old TurnSummary format
        if stored_messages and isinstance(stored_messages[0], dict) and "role" not in stored_messages[0]:
            stored_messages = []

        current_turn_id = session_turn_count + 1
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
        run_metadata["turn_id"] = current_turn_id
        if user_id:
            run_metadata["user_id"] = user_id
        if workspace_id:
            run_metadata["workspace_id"] = workspace_id
        run_metadata["agent_mode"] = effective_agent_mode
        run_metadata["model_profile_id"] = selected_profile["id"]
        run_metadata["model_profile_ref"] = selected_profile_ref
        run_metadata["model_provider"] = selected_profile.get("provider")
        if trace_context is not None:
            run_metadata.update({
                "traceparent": trace_context.value,
                "external_trace_id": trace_context.trace_id,
                "external_parent_span_id": trace_context.parent_id,
                "external_trace_flags": trace_context.trace_flags,
            })

        execution_store.start_turn(
            session_id=session_id,
            turn_id=current_turn_id,
            run_id=run_id,
            user_request=user_request,
            ontology_version=stamp_ontology_version,
            interaction_response=interaction_response,
        )

        self.run_store.create_run(
            run_id=run_id,
            user_request=user_request,
            metadata=run_metadata,
        )

        msg_metadata = {
            "run_id": run_id,
            "model_profile_id": selected_profile["id"],
            "model_profile_ref": selected_profile_ref,
            "model_provider": selected_profile.get("provider"),
        }
        if selected_ontology is not None and current_turn_id == 1:
            msg_metadata["selected_ontology_id"] = selected_ontology.id
            msg_metadata["selected_ontology_name"] = selected_ontology.name
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
        try:
            run_trace.started(
                user_id=user_id,
                user_query=user_request,
                workspace_id=workspace_id,
                model=selected_profile.get("model"),
                model_provider=selected_profile.get("provider"),
                metadata={
                    "turn_id": current_turn_id,
                    "model_profile_id": selected_profile["id"],
                    "model_profile_ref": selected_profile_ref,
                    "selected_ontology_id": str(context.get("selected_ontology_id") or "").strip() or None,
                    "agent_mode": resolve_agent_mode(context),
                    "llm_request_mode": str(context.get("llm_request_mode") or "").strip() or None,
                    "traceparent": trace_context.value if trace_context is not None else None,
                    "external_trace_id": trace_context.trace_id if trace_context is not None else None,
                    "external_parent_span_id": trace_context.parent_id if trace_context is not None else None,
                    "external_trace_flags": trace_context.trace_flags if trace_context is not None else None,
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
            # Direct Execution Branch — bypass AgentLoopEngine for clear-cut
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
                    from config.system_config import load_system_config
                    sys_config = load_system_config()
                    project_root = str(sys_config["storage"]["project_root"])
                    artifact_writer = _build_tool_output_artifact_writer(
                        chat_store,
                        session_id=session_id,
                        run_id=run_id,
                    )
                    with tool_output_artifact_writer_context(artifact_writer):
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
                    # Re-stamp from read_documents at turn completion. Tool-based
                    # sessions have no start_turn stamp; UI-selected sessions
                    # overwrite the start_turn stamp with the version actually read
                    # (avoids a false block when active switched mid-turn). When
                    # records disagree (a cross-version turn) infer returns None and
                    # the start_turn stamp is preserved, so gate 3 blocks correctly.
                    tool_version = self._infer_toolbased_ontology_version(workspace_id, final_read_documents)
                    if tool_version:
                        execution_store.stamp_turn_ontology_version(session_id, current_turn_id, tool_version)

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
                    tool_version = self._infer_toolbased_ontology_version(workspace_id, last_state.get("read_documents"))
                    if tool_version:
                        execution_store.stamp_turn_ontology_version(session_id, current_turn_id, tool_version)
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
                # Stamp turn version from read_documents (same as success/cancel paths).
                # Defensive: last_state may not be set if the exception was early.
                try:
                    tool_version = self._infer_toolbased_ontology_version(workspace_id, last_state.get("read_documents"))
                    if tool_version:
                        execution_store.stamp_turn_ontology_version(session_id, current_turn_id, tool_version)
                except Exception:
                    pass

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

        finally:
            run_trace.close(status=RuntimeStatus.ERROR)

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
        allow_injection: bool,
    ) -> str:
        if not allow_injection or not memory_manager:
            return ""
        try:
            return memory_manager.build_memory_context(query=query)
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

    def persist_run_feedback(self, run_id: str, *, vote: str) -> bool:
        """Persist feedback on the execution turn regardless of memory recall."""

        run = self.run_store.get_run(run_id)
        if run is None:
            return False
        metadata = dict(run.metadata or {})
        session_id = str(metadata.get("session_id") or "").strip()
        turn_id = metadata.get("turn_id")
        if not session_id or turn_id is None:
            return False
        _chat_store, execution_store = self._get_stores(
            str(metadata.get("user_id") or "").strip() or None,
            str(metadata.get("workspace_id") or "").strip() or None,
        )
        try:
            return execution_store.set_turn_feedback(session_id, int(turn_id), vote)
        except Exception:
            logger.warning("persist_run_feedback failed run=%s", run_id, exc_info=True)
            return False

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
        is_reserved_agentic_run = bool(
            existing
            and existing.metadata.get("source") == "agentic_stream"
            and existing.status in {RunStatus.QUEUED, RunStatus.RUNNING}
        )
        if (
            existing is None
            or is_reserved_agentic_run
            or (allow_existing_acp_run and existing.metadata.get("source") == "acp")
        ):
            return normalized
    return str(uuid.uuid4())
