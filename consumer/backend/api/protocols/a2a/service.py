"""Native-async A2A projection over canonical Runs and durable Turns."""

from __future__ import annotations

import uuid
from typing import Any

from agent.core.shared.modes import resolve_agent_mode
from agent.runtime.contracts import (
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.errors import RunConflictError, RunNotFoundError
from api.protocols.a2a.models import (
    A2ATaskCreateRequest,
    A2ATaskSnapshot,
    A2ATaskState,
)
from llm.llm_request_mode import normalize_llm_request_mode
from utils.identifiers import validate_session_id


_PROTOCOL = "a2a"


class A2ATaskService:
    """Map A2A task identifiers to server-generated canonical Run IDs."""

    def __init__(
        self,
        *,
        runtime: Any,
        execution_store: Any,
        username: str,
        workspace_id: str,
    ) -> None:
        manager = getattr(runtime, "manager", None)
        short_io = getattr(runtime, "short_io", None)
        if manager is None or short_io is None:
            raise ValueError("A2A requires the canonical runtime container")
        self.runtime = runtime
        self.execution_store = execution_store
        self.username = str(username)
        self.workspace_id = str(workspace_id)
        self.manager = manager

    @property
    def scope(self) -> RunScope:
        return RunScope(
            username=self.username,
            workspace_id=self.workspace_id,
            scopes=("agent:run", "agent:read", "agent:cancel"),
        )

    async def create_task(
        self,
        request: A2ATaskCreateRequest,
    ) -> tuple[A2ATaskSnapshot, RunSnapshot]:
        external_task_id = str(request.task_id or "").strip() or (
            "task_{}".format(uuid.uuid4().hex)
        )
        session_id = validate_session_id(
            request.session_id or "session_{}".format(uuid.uuid4().hex)
        )
        context_values = dict(request.context or {})
        raw_arguments = context_values.get("arguments")
        if raw_arguments is not None and not isinstance(raw_arguments, dict):
            raise ValueError("arguments must be an object or null")
        if await self._find_turn(external_task_id) is not None:
            raise RunConflictError("task_id already exists")

        async with self.manager.session_guard(self.scope, session_id):
            if await self._find_turn(external_task_id) is not None:
                raise RunConflictError("task_id already exists")
            await self.manager.assert_session_available(self.scope, session_id)
            turn, created = await self.runtime.short_io.run(
                self.execution_store.create_turn_for_run,
                session_id=session_id,
                user_request=request.message,
                protocol_metadata={
                    "protocol": _PROTOCOL,
                    "external_run_id": external_task_id,
                },
            )
            if not created:
                raise RunConflictError("task_id already exists")
            context = RuntimeInvocationContext(
                run_id=str(turn["run_id"]),
                session_id=session_id,
                turn_id=int(turn["turn_id"]),
                workspace_id=self.workspace_id,
                username=self.username,
                query=request.message,
                agent_mode=resolve_agent_mode(context_values),
                llm_request_mode=normalize_llm_request_mode(
                    context_values.get("llm_request_mode")
                ),
                model_profile_id=(
                    str(context_values.get("model_profile_id") or "").strip()
                    or None
                ),
                arguments=(dict(raw_arguments) if raw_arguments is not None else None),
                include_trace=False,
                supports_user_interaction=False,
            )
            try:
                snapshot, manager_created = await self.manager.create_run(
                    context,
                    self.scope,
                    session_guarded=True,
                )
            except Exception:
                await self.runtime.short_io.run(
                    self.execution_store.mark_turn_task_start_failed,
                    session_id,
                    int(turn["turn_id"]),
                )
                raise
            if not manager_created:
                await self.runtime.short_io.run(
                    self.execution_store.mark_turn_task_start_failed,
                    session_id,
                    int(turn["turn_id"]),
                )
                raise RunConflictError("canonical run already exists")
        return self.project(snapshot, external_task_id=external_task_id), snapshot

    async def get_task(
        self,
        external_task_id: str,
        *,
        session_id: str | None = None,
    ) -> tuple[A2ATaskSnapshot, RunSnapshot | None]:
        mapping = await self._find_turn(external_task_id)
        if mapping is None:
            return self.not_found(external_task_id, session_id=session_id), None
        mapped_session_id, turn = mapping
        if session_id is not None and mapped_session_id != validate_session_id(session_id):
            return self.not_found(external_task_id, session_id=session_id), None
        snapshot = await self.manager.get_run(str(turn["run_id"]), self.scope)
        if snapshot is not None:
            return self.project(snapshot, external_task_id=external_task_id), snapshot
        return self.project_turn(turn, external_task_id=external_task_id), None

    async def cancel_task(self, external_task_id: str) -> A2ATaskSnapshot:
        view, snapshot = await self.get_task(external_task_id)
        if snapshot is None:
            if view.state is A2ATaskState.NOT_FOUND:
                raise RunNotFoundError(external_task_id)
            return view
        cancelled = await self.manager.cancel_run(
            snapshot.run_id,
            self.scope,
            reason="a2a_cancel",
        )
        return self.project(cancelled, external_task_id=external_task_id)

    async def canonical_run_id(self, external_task_id: str) -> str | None:
        mapping = await self._find_turn(external_task_id)
        return str(mapping[1].get("run_id") or "") or None if mapping else None

    async def _find_turn(self, external_task_id: str):
        session_id, turn = await self.runtime.short_io.run(
            self.execution_store.find_turn_by_protocol_id,
            protocol=_PROTOCOL,
            external_run_id=str(external_task_id),
        )
        if session_id is None or turn is None:
            return None
        return session_id, turn

    @staticmethod
    def not_found(
        external_task_id: str,
        *,
        session_id: str | None = None,
    ) -> A2ATaskSnapshot:
        return A2ATaskSnapshot(
            id=str(external_task_id),
            session_id=session_id,
            state=A2ATaskState.NOT_FOUND,
            error="Task not found",
        )

    @staticmethod
    def project(
        snapshot: RunSnapshot,
        *,
        external_task_id: str,
    ) -> A2ATaskSnapshot:
        if snapshot.status is RunStatus.AWAITING_INPUT:
            state = A2ATaskState.FAILED
            result = None
            error = "User interaction is not supported by A2A"
            error_code = "INTERACTION_NOT_SUPPORTED"
        else:
            state = _map_status(snapshot.status)
            result = snapshot.result.to_dict() if snapshot.result is not None else None
            error = snapshot.error.message if snapshot.error is not None else None
            error_code = snapshot.error.error_code if snapshot.error is not None else None
        metadata = {
            "canonical_run_id": snapshot.run_id,
            "last_seq": snapshot.last_seq,
        }
        if error_code:
            metadata["error_code"] = error_code
        return A2ATaskSnapshot(
            id=external_task_id,
            session_id=snapshot.session_id,
            state=state,
            created_at=snapshot.created_at,
            updated_at=snapshot.updated_at,
            completed_at=(
                snapshot.updated_at
                if snapshot.status in TERMINAL_RUN_STATUSES
                else None
            ),
            error=error,
            result=result,
            metadata=metadata,
        )

    @staticmethod
    def project_turn(
        turn: dict[str, Any],
        *,
        external_task_id: str,
    ) -> A2ATaskSnapshot:
        status = str(turn.get("status") or "")
        if status == "awaiting_input":
            state = A2ATaskState.FAILED
            error = "User interaction is not supported by A2A"
            error_code = "INTERACTION_NOT_SUPPORTED"
        else:
            state = {
                "queued": A2ATaskState.PENDING,
                "running": A2ATaskState.IN_PROGRESS,
                "success": A2ATaskState.COMPLETED,
                "failed": A2ATaskState.FAILED,
                "cancelled": A2ATaskState.CANCELED,
            }.get(status, A2ATaskState.FAILED)
            error_code = str(turn.get("error_code") or "") or None
            output = turn.get("final_output")
            error = (
                str(output.get("error") or "Execution failed")
                if state is A2ATaskState.FAILED and isinstance(output, dict)
                else None
            )
        metadata = {
            "canonical_run_id": str(turn.get("run_id") or ""),
            "durable": True,
        }
        if error_code:
            metadata["error_code"] = error_code
        return A2ATaskSnapshot(
            id=external_task_id,
            session_id=str(turn.get("session_id") or ""),
            state=state,
            created_at=str(turn.get("start_time") or "") or None,
            updated_at=str(turn.get("end_time") or turn.get("start_time") or "") or None,
            completed_at=str(turn.get("end_time") or "") or None,
            error=error,
            result=(
                {"status": "completed", "structured_content": turn.get("final_output")}
                if status == "success" and isinstance(turn.get("final_output"), dict)
                else None
            ),
            metadata=metadata,
        )


def _map_status(status: RunStatus) -> A2ATaskState:
    return {
        RunStatus.QUEUED: A2ATaskState.PENDING,
        RunStatus.RUNNING: A2ATaskState.IN_PROGRESS,
        RunStatus.COMPLETED: A2ATaskState.COMPLETED,
        RunStatus.FAILED: A2ATaskState.FAILED,
        RunStatus.CANCELLED: A2ATaskState.CANCELED,
    }.get(status, A2ATaskState.FAILED)
