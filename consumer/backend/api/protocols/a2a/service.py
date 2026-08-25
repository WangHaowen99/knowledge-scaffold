"""A2A adapter service: create, inspect, and cancel projected tasks."""

from __future__ import annotations

import threading
import uuid
from contextlib import nullcontext
from typing import Any, Dict, Optional

from agent.core.shared.interaction_policy import set_user_interaction_capability
from persistence.run_store import RunStatus
from utils.identifiers import validate_session_id
from utils.logging import get_logger
from workspace.workspaces import workspace_scope
from integration.observability.trace_context import (
    bind_otel_context,
    bind_otel_traceparent,
    capture_otel_context,
)
from api.protocols.a2a.mapper import map_run_status_to_a2a
from api.protocols.a2a.models import (
    A2ATaskCreateRequest,
    A2ATaskSnapshot,
    A2ATaskState,
    iso_or_none,
)


logger = get_logger("a2a.service")


class A2ATaskService:
    """Service facade that adapts internal runs to A2A task semantics."""

    def __init__(
        self,
        orchestrator,
        chat_store,
        execution_store,
        user_id: Optional[str],
        workspace_id: Optional[str],
        flask_app: Optional[Any] = None,
        traceparent: str = "",
    ):
        self.orchestrator = orchestrator
        self.chat_store = chat_store
        self.execution_store = execution_store
        self.user_id = user_id
        self.workspace_id = workspace_id
        self.flask_app = flask_app
        self.traceparent = str(traceparent or "")

    def create_task(self, req: A2ATaskCreateRequest) -> A2ATaskSnapshot:
        task_id = self._allocate_task_id(req.task_id)
        session_id = validate_session_id(req.session_id or str(uuid.uuid4()))

        if not self.chat_store.get_session(session_id):
            self.chat_store.create_session(session_id=session_id)

        self._launch_background_task(
            task_id=task_id,
            session_id=session_id,
            message=req.message,
            context=dict(req.context or {}),
        )

        return A2ATaskSnapshot(
            id=task_id,
            session_id=session_id,
            state=A2ATaskState.PENDING,
            metadata={"mode": "background"},
        )

    def resolve_session_id(self, task_id: str, *, session_id: Optional[str] = None) -> Optional[str]:
        normalized_session_id = validate_session_id(session_id) if session_id else None
        if normalized_session_id:
            return normalized_session_id

        run = self.orchestrator.run_store.get_run(task_id)
        if not run:
            return None
        return str((getattr(run, "metadata", {}) or {}).get("session_id") or "").strip() or None

    def get_task(self, task_id: str, *, session_id: Optional[str] = None) -> A2ATaskSnapshot:
        run = self.orchestrator.run_store.get_run(task_id)
        if not run:
            return A2ATaskSnapshot(
                id=task_id,
                session_id=session_id,
                state=A2ATaskState.NOT_FOUND,
                error="Task not found",
            )

        resolved_session_id = self.resolve_session_id(task_id, session_id=session_id)
        state = map_run_status_to_a2a(run.status)

        return A2ATaskSnapshot(
            id=task_id,
            session_id=resolved_session_id,
            state=state,
            created_at=iso_or_none(getattr(run, "created_at", None)),
            updated_at=iso_or_none(getattr(run, "updated_at", None)),
            completed_at=iso_or_none(getattr(run, "completed_at", None)),
            error=getattr(run, "error", None),
            error_code=str((getattr(run, "metadata", {}) or {}).get("error_code") or "")
            or None,
            result=getattr(run, "result", None),
            metadata=dict(getattr(run, "metadata", {}) or {}),
        )

    def cancel_task(self, task_id: str) -> bool:
        return bool(self.orchestrator.cancel_run(task_id, reason="a2a_cancel"))

    def _launch_background_task(
        self,
        *,
        task_id: str,
        session_id: str,
        message: str,
        context: Dict[str, Any],
    ) -> None:
        thread = threading.Thread(
            target=self._run_task_with_context,
            kwargs={
                "task_id": task_id,
                "session_id": session_id,
                "message": message,
                "context": dict(context or {}),
                "otel_context": capture_otel_context(),
            },
            name="a2a-task-{}".format(task_id),
            daemon=True,
        )
        thread.start()

    def _run_task_with_context(self, *, otel_context: Any, **kwargs: Any) -> None:
        with bind_otel_context(otel_context):
            self._run_task_in_background(**kwargs)

    def _run_task_in_background(
        self,
        *,
        task_id: str,
        session_id: str,
        message: str,
        context: Dict[str, Any],
    ) -> None:
        try:
            context = set_user_interaction_capability(context, supported=False)
            app_context = self.flask_app.app_context() if self.flask_app is not None else nullcontext()
            with app_context:
                with workspace_scope(self.workspace_id), bind_otel_traceparent(
                    self.traceparent,
                    strict=False,
                ):
                    for _event in self.orchestrator.run(
                        user_request=message,
                        user_context=context,
                        session_id=session_id,
                        user_id=self.user_id,
                        workspace_id=self.workspace_id,
                        requested_run_id=task_id,
                    ):
                        # Background mode currently ignores event forwarding.
                        continue
        except Exception:
            logger.exception("A2A background task %s failed", task_id)
            self._mark_task_failed(task_id=task_id, session_id=session_id, message=message)

    def _mark_task_failed(self, *, task_id: str, session_id: str, message: str) -> None:
        public_error = "Workflow execution failed"
        run = self.orchestrator.run_store.get_run(task_id)
        if run is None:
            self.orchestrator.run_store.create_run(
                run_id=task_id,
                user_request=message,
                metadata=self._build_run_metadata(session_id),
            )
            run = self.orchestrator.run_store.get_run(task_id)

        if run is not None and run.status not in {
            RunStatus.FAILED,
            RunStatus.CANCELLED,
            RunStatus.COMPLETED,
        }:
            self.orchestrator.run_store.fail_run(task_id, public_error)

        turn = self._get_task_turn(session_id, task_id)
        if not turn:
            return

        if str(turn.get("status") or "").strip() != "running":
            return

        turn_id = turn.get("turn_id")
        if isinstance(turn_id, int):
            self.execution_store.fail_turn(session_id=session_id, turn_id=turn_id, error=public_error)

    def _build_run_metadata(self, session_id: str) -> Dict[str, Any]:
        metadata: Dict[str, Any] = {
            "session_id": session_id,
            "mode": "background",
        }
        if self.user_id:
            metadata["user_id"] = self.user_id
        if self.workspace_id:
            metadata["workspace_id"] = self.workspace_id
        return metadata

    def _allocate_task_id(self, requested_task_id: Optional[str]) -> str:
        normalized = str(requested_task_id or "").strip()
        if not normalized:
            return str(uuid.uuid4())
        if self.orchestrator.run_store.get_run(normalized) is not None:
            raise ValueError("task_id already exists")
        return normalized

    def _get_task_turn(self, session_id: Optional[str], task_id: str) -> Optional[Dict[str, Any]]:
        if not session_id:
            return None
        history = self.execution_store.get_session_history(session_id)
        if not history:
            return None
        turns = list(history.get("turns") or [])
        if not turns:
            return None

        normalized_task_id = str(task_id or "").strip()
        for turn in reversed(turns):
            if str((turn or {}).get("run_id") or "").strip() != normalized_task_id:
                continue
            return turn or None
        return None
