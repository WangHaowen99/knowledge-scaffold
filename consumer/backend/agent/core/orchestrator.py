"""Business/context services used by the canonical async runtime.

Run and Turn lifecycle ownership intentionally lives in ``agent.runtime`` and
``ExecutionStore``. This object only assembles consumer-specific stores,
memory, evolution, and admission policy dependencies.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional

from agent.evolution import (
    PatcherConfig,
    ReflectionWorker,
    build_reflection_snapshot,
    get_proposal_buffer,
    init_operation_patcher,
)
from agent.memory import MemoryFactory
from agent.runtime.async_utils import run_sync_compat
from agent.runtime.preflight_cache import ImmutablePreflightCache
from config import AgentConfig, AppConfig
from persistence.chat_store import ChatStore, ToolOutputLimits
from persistence.execution_store import ExecutionStore
from persistence.run_queue import TokenBucketRateLimiter
from utils.logging import get_logger
from workspace.user_store_manager import UserStoreManager


logger = get_logger("orchestrator")


class AgenticOrchestrator:
    """Consumer business dependencies for the lifespan-owned async runtime."""

    def __init__(
        self,
        chat_store: Optional[ChatStore] = None,
        execution_store: Optional[ExecutionStore] = None,
        user_store_manager: Optional[UserStoreManager] = None,
        agent_config: Optional[AgentConfig] = None,
        memory_factory: Optional[MemoryFactory] = None,
    ) -> None:
        self.agent_config = agent_config or AgentConfig.from_env()
        app_config = (
            AppConfig.from_env()
            if chat_store is None or execution_store is None
            else None
        )
        self.chat_store = chat_store or ChatStore(
            app_config.chat_storage_dir,
            tool_output_limits=ToolOutputLimits.from_config(
                self.agent_config.limits
            ),
        )
        self.execution_store = execution_store or ExecutionStore(
            app_config.execution_storage_dir
        )
        self.user_store_manager = user_store_manager
        self.memory_factory = memory_factory or MemoryFactory()
        self._preflight_cache = ImmutablePreflightCache()
        self.run_rate_limiter = TokenBucketRateLimiter(
            rate_per_second=self.agent_config.limits.run_tps,
            burst=self.agent_config.limits.run_tps,
        )

        self.passive_reflection_enabled = bool(
            self.agent_config.evolution_passive_reflection_enabled
        )
        self.reflection_worker = (
            ReflectionWorker(
                max_workers=max(
                    1,
                    int(
                        getattr(
                            self.agent_config,
                            "reflection_max_workers",
                            2,
                        )
                        or 2
                    ),
                ),
            )
            if self.passive_reflection_enabled
            else None
        )
        self.operation_patcher = init_operation_patcher(
            config=PatcherConfig(
                min_occurrences=self.agent_config.evolution_min_occurrences,
                min_applied=self.agent_config.evolution_min_applied,
                max_rejection=self.agent_config.evolution_max_rejection,
                cooldown_sec=self.agent_config.evolution_patch_cooldown_sec,
                sweep_interval_sec=(
                    self.agent_config.evolution_sweep_interval_sec
                ),
                auto_apply=self.agent_config.evolution_patch_auto_apply,
            ),
        )
        logger.info("AgenticOrchestrator business services initialized")

    def bind_runtime_supervisor(self, supervisor: Any) -> None:
        """Bind derived business work to the ASGI-owned task supervisor."""

        self.operation_patcher.bind_supervisor(supervisor)
        if self.reflection_worker is not None:
            self.reflection_worker.bind_supervisor(supervisor)

    @staticmethod
    def _infer_toolbased_ontology_version(
        workspace_id: Optional[str],
        read_documents: Optional[Dict[str, Any]],
    ) -> Optional[str]:
        """Return the sole ontology version actually read during a Turn."""

        if not isinstance(read_documents, dict) or not workspace_id:
            return None
        by_path = read_documents.get("by_path") or {}
        ontology_versions: Dict[str, set[str]] = {}
        if isinstance(by_path, dict):
            for record in by_path.values():
                if not isinstance(record, dict):
                    continue
                ontology_id = str(record.get("ontology_id") or "").strip()
                version = str(record.get("ontology_version") or "").strip()
                if ontology_id and version:
                    ontology_versions.setdefault(ontology_id, set()).add(
                        version
                    )
        if len(ontology_versions) != 1:
            return None
        versions = next(iter(ontology_versions.values()))
        return next(iter(versions)) if len(versions) == 1 else None

    def _get_stores(
        self,
        user_id: Optional[str],
        workspace_id: Optional[str] = None,
    ) -> tuple[ChatStore, ExecutionStore]:
        if user_id and self.user_store_manager:
            normalized_workspace = workspace_id or "default"
            return (
                self.user_store_manager.get_chat_store(
                    user_id,
                    normalized_workspace,
                ),
                self.user_store_manager.get_execution_store(
                    user_id,
                    normalized_workspace,
                ),
            )
        return self.chat_store, self.execution_store

    def get_stores(
        self,
        user_id: Optional[str],
        workspace_id: Optional[str] = None,
    ) -> tuple[ChatStore, ExecutionStore]:
        """Resolve the stores for a user/workspace through the public runtime API."""
        return self._get_stores(user_id, workspace_id)

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
        deleted = chat_store.prune_old_sessions(max_sessions)
        for session_id in deleted:
            execution_store.delete_session(session_id)
        return deleted

    def _get_memory_manager(self, user_id: Optional[str]):
        try:
            return self.memory_factory.for_user(user_id or "anonymous")
        except Exception:
            logger.warning(
                "Failed to resolve memory manager for user=%s",
                user_id,
                exc_info=True,
            )
            return None

    def get_memory_manager(self, user_id: Optional[str]):
        """Resolve the memory manager for a user without exposing factory internals."""
        return self._get_memory_manager(user_id)

    def _build_protocol_preflight_request(
        self,
        *,
        user_request: str,
        user_context: Optional[Dict[str, Any]],
        arguments: Optional[Dict[str, Any]],
        session_id: str,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> tuple[Any | None, str, str, str | None]:
        """Resolve dynamic request facts without reading package files."""

        from agent.direct_execution.contracts import (
            DeterministicRequest,
        )
        from agent.direct_execution.models import RuntimeContext
        from utils.identifiers import validate_session_id

        chat_store, execution_store = self._get_stores(user_id, workspace_id)
        normalized_session_id = validate_session_id(session_id)
        context = dict(user_context or {})
        request_arguments = (
            dict(arguments)
            if isinstance(arguments, dict)
            else dict(context.get("arguments") or {})
        )
        metadata_reader = getattr(chat_store, "get_session_metadata", None)
        if callable(metadata_reader):
            metadata = dict(metadata_reader(normalized_session_id) or {})
        else:
            # Compatibility for lightweight protocol adapters used by embedders.
            session = chat_store.get_session(normalized_session_id)
            metadata = dict(getattr(session, "metadata", {}) or {})
        selected_ontology_id = str(
            metadata.get("selected_ontology_id")
            or context.get("selected_ontology_id")
            or request_arguments.get("selected_ontology_id")
            or ""
        ).strip()
        stored_profile = str(metadata.get("model_profile_id") or "").strip() or None
        if not selected_ontology_id:
            return None, "", "", stored_profile
        selected_version = str(
            metadata.get("selected_ontology_version")
            or context.get("selected_ontology_version")
            or context.get("selected_version")
            or request_arguments.get("selected_ontology_version")
            or request_arguments.get("selected_version")
            or ""
        ).strip()
        interaction_response = context.get("interaction_response")
        if interaction_response is not None and not isinstance(
            interaction_response,
            dict,
        ):
            return None, selected_ontology_id, selected_version, stored_profile
        turn_count_reader = getattr(execution_store, "get_turn_count", None)
        if callable(turn_count_reader):
            turn_count = int(turn_count_reader(normalized_session_id))
        else:
            # Compatibility for protocol adapters that predate the summary index.
            turn_count = len(
                list(
                    (
                        execution_store.get_session_history(normalized_session_id)
                        or {}
                    ).get("turns")
                    or []
                )
            )
        checkpoint = (
            execution_store.get_pending_deterministic_checkpoint(
                normalized_session_id
            )
            if interaction_response
            else None
        )
        request = DeterministicRequest(
            user_request=str(user_request or ""),
            context=context,
            arguments=request_arguments,
            runtime_context=RuntimeContext(
                session_id=normalized_session_id,
                run_id="protocol-preflight",
                turn_id=turn_count + 1,
                user_id=user_id,
                workspace_id=workspace_id or None,
                selected_ontology_id=selected_ontology_id,
                selected_ontology_version=selected_version or None,
            ),
            checkpoint=checkpoint,
            interaction_response=(
                dict(interaction_response)
                if isinstance(interaction_response, dict)
                else None
            ),
        )
        return (
            request,
            selected_ontology_id,
            selected_version,
            stored_profile,
        )

    def _resolve_protocol_preflight(
        self,
        *,
        user_request: str,
        user_context: Optional[Dict[str, Any]],
        arguments: Optional[Dict[str, Any]],
        session_id: str,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> tuple[bool, Any, str | None]:
        """Resolve request-local routing state and one exact ontology manifest."""

        from agent.direct_execution.service import DeterministicExecutionService
        from ontology.ontologies import get_runtime_ontology_manifest

        request, ontology_id, ontology_version, stored_profile = (
            self._build_protocol_preflight_request(
                user_request=user_request,
                user_context=user_context,
                arguments=arguments,
                session_id=session_id,
                user_id=user_id,
                workspace_id=workspace_id,
            )
        )
        if request is None or not ontology_id:
            return False, None, stored_profile
        ontology_manifest = get_runtime_ontology_manifest(
            ontology_id,
            version=ontology_version or None,
            workspace_id=workspace_id,
        )
        if ontology_manifest is None:
            return False, None, stored_profile
        return (
            DeterministicExecutionService().is_potentially_applicable(
                request,
                ontology_manifest=ontology_manifest,
            ),
            ontology_manifest,
            stored_profile,
        )

    def is_deterministic_request_potentially_applicable(
        self,
        *,
        user_request: str,
        user_context: Optional[Dict[str, Any]],
        arguments: Optional[Dict[str, Any]],
        session_id: str,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ) -> bool:
        """Return whether protocol admission may safely defer LLM validation."""

        candidate, _manifest, _stored_profile = self._resolve_protocol_preflight(
            user_request=user_request,
            user_context=user_context,
            arguments=arguments,
            session_id=session_id,
            user_id=user_id,
            workspace_id=workspace_id,
        )
        return candidate

    def build_run_preflight_snapshot(
        self,
        *,
        user_request: str,
        user_context: Optional[Dict[str, Any]],
        arguments: Optional[Dict[str, Any]],
        session_id: str,
        model_profile_id: Optional[str] = None,
        user_id: Optional[str] = None,
        workspace_id: Optional[str] = None,
    ):
        """Build the exact package/profile products accepted for one request."""

        from agent.runtime.preflight import build_run_preflight_snapshot
        from agent.direct_execution.service import DeterministicExecutionService
        from config.system_config import resolve_effective_llm_profile_config
        from ontology.ontologies import get_runtime_ontology_manifest

        normalized_workspace = str(workspace_id or "").strip() or "default"
        request, ontology_id, ontology_version, stored_profile = (
            self._build_protocol_preflight_request(
                user_request=user_request,
                user_context=user_context,
                arguments=arguments,
                session_id=session_id,
                user_id=user_id,
                workspace_id=normalized_workspace,
            )
        )
        requested_profile = str(model_profile_id or "").strip() or None
        profile_ref = requested_profile or stored_profile

        def load_manifest():
            if not ontology_id:
                return None
            return get_runtime_ontology_manifest(
                ontology_id,
                version=ontology_version or None,
                workspace_id=normalized_workspace,
            )

        def resolve_candidate(manifest: Any) -> bool:
            return bool(
                request is not None
                and manifest is not None
                and DeterministicExecutionService().is_potentially_applicable(
                    request,
                    ontology_manifest=manifest,
                )
            )

        def load_model_profile() -> Mapping[str, Any]:
            return resolve_effective_llm_profile_config(
                profile_ref,
                workspace_id=normalized_workspace,
                allow_default_fallback=not bool(requested_profile),
            )

        return build_run_preflight_snapshot(
            workspace_id=normalized_workspace,
            ontology_id=ontology_id,
            ontology_version=ontology_version,
            ontology_manifest_loader=load_manifest if ontology_id else None,
            deterministic_candidate_loader=resolve_candidate,
            model_profile_loader=load_model_profile,
            cache=self._preflight_cache,
        )

    def _record_evolution_recall_outcomes(
        self,
        state: Mapping[str, Any],
    ) -> None:
        records = state.get("evolution_recall_records") or []
        if not records:
            return
        try:
            buffer = get_proposal_buffer()
        except Exception:
            logger.warning(
                "Failed to open evolution buffer for outcome tracking",
                exc_info=True,
            )
            return
        for record in records:
            if not isinstance(record, dict):
                continue
            fingerprints = [
                str(fingerprint).strip()
                for fingerprint in record.get("fingerprints") or []
                if str(fingerprint).strip()
            ]
            if not fingerprints:
                continue
            try:
                buffer.record_outcome(
                    fingerprints,
                    success=True,
                    workspace_id=(
                        str(record.get("workspace_id") or "").strip() or None
                    ),
                    ontology_id=(
                        str(record.get("ontology_id") or "").strip() or None
                    ),
                    operation_key=(
                        str(record.get("operation_key") or "").strip() or None
                    ),
                )
            except Exception:
                logger.warning(
                    "Failed to record evolution recall outcome",
                    exc_info=True,
                )

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

    def build_memory_context(
        self,
        memory_manager: Any,
        *,
        query: str,
        allow_injection: bool,
    ) -> str:
        """Build injectable memory context through the public runtime API."""
        return self._build_memory_context(
            memory_manager,
            query=query,
            allow_injection=allow_injection,
        )

    async def _submit_reflection(
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
        if (
            not self.passive_reflection_enabled
            or self.reflection_worker is None
            or not ontology_id
        ):
            return
        try:
            def build_snapshot():
                snapshot_messages = copy.deepcopy(list(messages or []))
                final_text = (
                    final_output
                    if isinstance(final_output, str)
                    else json.dumps(
                        final_output,
                        ensure_ascii=False,
                        default=str,
                    )
                    if final_output
                    else ""
                )
                return build_reflection_snapshot(
                    run_id=run_id,
                    session_id=session_id,
                    turn_id=turn_id,
                    user_id=user_id,
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    status=status,
                    user_request=user_request,
                    final_output=final_text,
                    messages=snapshot_messages,
                    iteration_count=self._count_iterations(snapshot_messages),
                    duration_ms=duration_ms,
                )

            snapshot = await run_sync_compat(
                build_snapshot,
                run_id=run_id,
                task_name="reflection-snapshot",
                offload_kind="tool",
            )
            self.reflection_worker.submit(snapshot)
        except Exception:
            logger.warning(
                "Failed to submit reflection for run=%s",
                run_id,
                exc_info=True,
            )

    def vote_on_memory_records(
        self,
        records: list[dict[str, Any]],
        *,
        vote: str,
        previous_vote: Optional[str] = None,
    ) -> int:
        """Apply feedback to durable Turn recall anchors."""

        normalized_vote = str(vote or "").strip().lower()
        normalized_previous = str(previous_vote or "").strip().lower() or None
        if normalized_vote not in {"up", "down", "none"}:
            return 0
        if normalized_previous not in {"up", "down"}:
            normalized_previous = None
        if normalized_vote == normalized_previous:
            return 0

        grouped: Dict[tuple[Optional[str], str, str], set[str]] = {}
        for record in records or []:
            if not isinstance(record, dict):
                continue
            workspace_id = str(record.get("workspace_id") or "").strip() or None
            ontology_id = str(record.get("ontology_id") or "").strip()
            operation_key = str(record.get("operation_key") or "").strip()
            fingerprints = {
                str(fingerprint).strip()
                for fingerprint in record.get("fingerprints") or []
                if str(fingerprint).strip()
            }
            if ontology_id and operation_key and fingerprints:
                grouped.setdefault(
                    (workspace_id, ontology_id, operation_key),
                    set(),
                ).update(fingerprints)
        if not grouped:
            return 0

        try:
            buffer = get_proposal_buffer()
            updated = 0
            for (
                workspace_id,
                ontology_id,
                operation_key,
            ), fingerprints in grouped.items():
                buffer.record_vote(
                    sorted(fingerprints),
                    vote=normalized_vote,
                    previous_vote=normalized_previous,
                    workspace_id=workspace_id,
                    ontology_id=ontology_id,
                    operation_key=operation_key,
                )
                updated += len(fingerprints)
            return updated
        except Exception:
            logger.warning("vote_on_memory_records failed", exc_info=True)
            return 0

    @staticmethod
    def _count_iterations(messages: List[Dict[str, Any]]) -> int:
        return sum(
            1
            for message in messages or []
            if isinstance(message, dict) and message.get("role") == "assistant"
        )
