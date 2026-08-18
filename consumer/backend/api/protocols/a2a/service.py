"""A2A adapter service: create, inspect, and cancel projected tasks."""

from __future__ import annotations

import uuid
from typing import Any, Optional

from agent.core.shared.modes import resolve_agent_mode
from llm.llm_request_mode import normalize_llm_request_mode
from utils.identifiers import validate_session_id
from api.protocols.a2a.models import (
    A2ATaskCreateRequest,
    A2ATaskSnapshot,
    A2ATaskState,
)
from agent.runtime.errors import RunNotFoundError


class A2ATaskService:
    """Service facade that adapts internal runs to A2A task semantics."""

    def __init__(
        self,
        chat_store,
        user_id: Optional[str],
        workspace_id: Optional[str],
        async_manager: Any,
        async_call: Any,
    ):
        self.chat_store = chat_store
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.async_manager = async_manager
        self.async_call = async_call
        if async_manager is None or not callable(async_call):
            raise ValueError("A2A requires the canonical async run manager")

    def create_task(self, req: A2ATaskCreateRequest) -> A2ATaskSnapshot:
        task_id = self._allocate_task_id(req.task_id)
        session_id = validate_session_id(req.session_id or str(uuid.uuid4()))

        if not self.chat_store.get_session(session_id):
            self.chat_store.create_session(session_id=session_id)

        from agent.runtime.contracts import RunScope, RuntimeInvocationContext

        context_data = dict(req.context or {})
        supports_user_interaction = bool(
            context_data.get("supports_user_interaction", False)
        )
        raw_arguments = context_data.get("arguments")
        if raw_arguments is not None and not isinstance(raw_arguments, dict):
            raise ValueError("arguments must be an object or null")
        context = RuntimeInvocationContext(
            run_id=task_id,
            session_id=session_id,
            turn_id=1,
            workspace_id=str(self.workspace_id or ""),
            username=str(self.user_id or ""),
            query=req.message,
            agent_mode=resolve_agent_mode(context_data),
            llm_request_mode=normalize_llm_request_mode(
                context_data.get("llm_request_mode")
            ),
            model_profile_id=(
                str(context_data.get("model_profile_id") or "").strip() or None
            ),
            arguments=dict(raw_arguments) if raw_arguments is not None else None,
            include_trace=False,
            supports_user_interaction=supports_user_interaction,
        )
        self.async_call(
            self.async_manager.create_run(
                context,
                RunScope(
                    username=str(self.user_id or ""),
                    workspace_id=str(self.workspace_id or ""),
                    scopes=("agent:run", "agent:read", "agent:cancel"),
                ),
            )
        )
        created = self.get_task(task_id, session_id=session_id)
        return A2ATaskSnapshot(
            id=task_id,
            session_id=session_id,
            state=created.state,
            created_at=created.created_at,
            updated_at=created.updated_at,
            metadata={"mode": "async_run_manager"},
        )

    def resolve_session_id(self, task_id: str, *, session_id: Optional[str] = None) -> Optional[str]:
        normalized_session_id = validate_session_id(session_id) if session_id else None
        if normalized_session_id:
            return normalized_session_id

        from agent.runtime.contracts import RunScope

        snapshot = self.async_call(
            self.async_manager.get_run(
                task_id,
                RunScope(
                    username=str(self.user_id or ""),
                    workspace_id=str(self.workspace_id or ""),
                    scopes=("agent:read",),
                ),
            )
        )
        return snapshot.session_id if snapshot is not None else None

    def get_task(self, task_id: str, *, session_id: Optional[str] = None) -> A2ATaskSnapshot:
        from agent.runtime.contracts import RunScope

        snapshot = self.async_call(
            self.async_manager.get_run(
                task_id,
                RunScope(
                    username=str(self.user_id or ""),
                    workspace_id=str(self.workspace_id or ""),
                    scopes=("agent:read",),
                ),
            )
        )
        if snapshot is not None:
            result = snapshot.result.to_dict() if snapshot.result else None
            error = snapshot.error.message if snapshot.error else None
            return A2ATaskSnapshot(
                id=task_id,
                session_id=snapshot.session_id,
                state=_map_async_status(snapshot.status),
                created_at=snapshot.created_at,
                updated_at=snapshot.updated_at,
                completed_at=(
                    snapshot.updated_at
                    if snapshot.status.value in {"completed", "failed", "cancelled"}
                    else None
                ),
                error=error,
                result=result,
                metadata={"mode": "async_run_manager", "last_seq": snapshot.last_seq},
            )
        return A2ATaskSnapshot(
            id=task_id,
            session_id=session_id,
            state=A2ATaskState.NOT_FOUND,
            error="Task not found",
        )

    def cancel_task(self, task_id: str) -> bool:
        from agent.runtime.contracts import RunScope

        try:
            snapshot = self.async_call(
                self.async_manager.cancel_run(
                    task_id,
                    RunScope(
                        username=str(self.user_id or ""),
                        workspace_id=str(self.workspace_id or ""),
                        scopes=("agent:cancel",),
                    ),
                    reason="a2a_cancel",
                )
            )
        except RunNotFoundError:
            return False
        return snapshot is not None

    def _allocate_task_id(self, requested_task_id: Optional[str]) -> str:
        normalized = str(requested_task_id or "").strip()
        if not normalized:
            return str(uuid.uuid4())
        from agent.runtime.contracts import RunScope

        existing = self.async_call(
            self.async_manager.get_run(
                normalized,
                RunScope(
                    username=str(self.user_id or ""),
                    workspace_id=str(self.workspace_id or ""),
                    scopes=("agent:read",),
                ),
            )
        )
        if existing is not None:
            raise ValueError("task_id already exists")
        return normalized


def _map_async_status(status: Any) -> A2ATaskState:
    normalized = str(getattr(status, "value", status) or "")
    return {
        "queued": A2ATaskState.PENDING,
        "running": A2ATaskState.IN_PROGRESS,
        "awaiting_input": A2ATaskState.IN_PROGRESS,
        "completed": A2ATaskState.COMPLETED,
        "failed": A2ATaskState.FAILED,
        "cancelled": A2ATaskState.CANCELED,
    }.get(normalized, A2ATaskState.NOT_FOUND)
