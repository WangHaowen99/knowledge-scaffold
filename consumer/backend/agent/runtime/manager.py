"""Consumer asynchronous run lifecycle, cancellation, HITL, and replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable, Optional

from agent.runtime.contracts import (
    TERMINAL_RUN_STATUSES,
    PublicEventDraft,
    PublicEventSink,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeInvocationContext,
    RuntimeRecoveryAction,
    RuntimeResult,
)
from agent.runtime.errors import (
    RunConflictError,
    RunNotFoundError,
    SessionBusyError,
    WorkspaceBusyError,
    WorkspaceUnavailableError,
)
from agent.runtime.event_bus import AsyncEventBus
from agent.runtime.service import AsyncExecutionService, pending_interaction_from_draft
from agent.runtime.store import InMemoryRunRepository, utc_now_iso
from agent.runtime.supervisor import RunTaskSupervisor
from utils.security.redaction import redact_sensitive_value

logger = logging.getLogger(__name__)


def request_fingerprint(context: RuntimeInvocationContext) -> str:
    canonical = json.dumps(
        {
            "session_id": context.session_id,
            "workspace_id": context.workspace_id,
            "username": context.username,
            "query": context.query,
            "agent_mode": context.agent_mode,
            "llm_request_mode": context.llm_request_mode,
            "model_profile_id": context.model_profile_id,
            "requested_operation_key": context.requested_operation_key,
            "arguments": context.arguments,
            "include_trace": context.include_trace,
            "supports_user_interaction": context.supports_user_interaction,
            "interaction_response": context.interaction_response,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _failed_terminal_payload(
    payload: dict[str, Any],
    *,
    error_code: str,
    message: str,
) -> dict[str, Any]:
    normalized = {
        key: value for key, value in payload.items() if key not in {"content", "result", "structured_content"}
    }
    normalized.update(
        {
            "status": RunStatus.FAILED.value,
            "error": {
                "error_code": error_code,
                "message": message,
                "retriable": False,
                "source": "runtime",
                "details": {},
            },
        }
    )
    return normalized


class _ManagerEventSink(PublicEventSink):
    def __init__(self, manager: "AsyncRunManager", run_id: str) -> None:
        self.manager = manager
        self.run_id = run_id

    async def emit(self, draft: PublicEventDraft) -> None:
        await self.manager.accept_runtime_event(self.run_id, draft)


@dataclass
class _RunEntry:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    cancel_requested: bool = False
    cancel_reason: str = ""
    terminal_committed: bool = False


@dataclass
class _ScopedLockState:
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    leases: int = 0


class AsyncRunManager:
    def __init__(
        self,
        execution_service: AsyncExecutionService,
        repository: Optional[InMemoryRunRepository] = None,
        event_bus: Optional[AsyncEventBus] = None,
        supervisor: Optional[RunTaskSupervisor] = None,
        workspace_available: Optional[Callable[[str], Awaitable[bool]]] = None,
    ) -> None:
        self.execution_service = execution_service
        self.repository = repository or InMemoryRunRepository()
        persist_event = getattr(execution_service, "persist_public_event", None)
        self.event_bus = event_bus or AsyncEventBus(
            self.repository,
            event_persistor=persist_event if callable(persist_event) else None,
        )
        if event_bus is not None and event_bus.event_persistor is None and callable(persist_event):
            event_bus.event_persistor = persist_event
        self.supervisor = supervisor or RunTaskSupervisor()
        self._admission_condition = asyncio.Condition()
        self._admission_waiters: dict[tuple[str, str], list[str]] = {}
        self._admitted_runs: dict[tuple[str, str], dict[str, int]] = {}
        self._entries: dict[str, _RunEntry] = {}
        self._cancel_futures: dict[str, asyncio.Future[RunSnapshot]] = {}
        self._creation_lock = asyncio.Lock()
        self._active_by_session: dict[tuple[str, str, str], str] = {}
        self._session_locks_guard = asyncio.Lock()
        self._session_locks: dict[tuple[str, str, str], _ScopedLockState] = {}
        self._workspace_locks_guard = asyncio.Lock()
        self._workspace_locks: dict[str, _ScopedLockState] = {}
        self._workspace_available = workspace_available

    @property
    def tracked_run_count(self) -> int:
        """Return the number of in-memory run coordination entries."""
        return len(self._entries)

    def has_tracked_run(self, run_id: str) -> bool:
        """Return whether an in-memory coordination entry exists for a run."""
        return str(run_id or "") in self._entries

    async def create_run(
        self,
        context: RuntimeInvocationContext,
        scope: RunScope,
        *,
        idempotency_key: Optional[str] = None,
        session_guarded: bool = False,
    ) -> tuple[RunSnapshot, bool]:
        if not session_guarded:
            async with self.session_guard(
                scope,
                context.session_id,
            ):
                return await self.create_run(
                    context,
                    scope,
                    idempotency_key=idempotency_key,
                    session_guarded=True,
                )
        if not scope.allows("agent:run"):
            raise PermissionError("缺少 agent:run 权限")
        if context.username != scope.username or context.workspace_id != scope.workspace_id:
            raise PermissionError("Runtime 上下文与鉴权范围不匹配")
        now = utc_now_iso()
        snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.QUEUED,
            created_at=now,
            updated_at=now,
            last_seq=0,
            scope=scope,
            context=context,
        )
        fingerprint = request_fingerprint(context)
        async with self._workspace_guard(scope.workspace_id):
            if self._workspace_available is not None and not await self._workspace_available(scope.workspace_id):
                raise WorkspaceUnavailableError(scope.workspace_id)
            async with self._creation_lock:
                if idempotency_key:
                    existing = await self.repository.lookup_idempotency(
                        scope=scope,
                        session_id=context.session_id,
                        idempotency_key=idempotency_key,
                        request_fingerprint=fingerprint,
                    )
                    if existing is not None:
                        return existing, False
                session_key = (
                    scope.username,
                    scope.workspace_id,
                    context.session_id,
                )
                active_run_id = self._active_by_session.get(session_key)
                if active_run_id:
                    active = await self.repository.get(active_run_id)
                    if active is not None and active.status not in TERMINAL_RUN_STATUSES:
                        raise SessionBusyError(active_run_id)
                    self._active_by_session.pop(session_key, None)
                stored, created = await self.repository.create(
                    snapshot,
                    idempotency_key=idempotency_key,
                    request_fingerprint=fingerprint,
                )
                if created:
                    self._entries[context.run_id] = _RunEntry()
                    self._active_by_session[session_key] = context.run_id
                    try:
                        task = self.supervisor.start_run(
                            context.run_id,
                            self._execute(context.run_id),
                        )
                    except BaseException:
                        self._active_by_session.pop(session_key, None)
                        raise
                    task.add_done_callback(
                        lambda completed, run_id=context.run_id: self._task_finished(
                            run_id,
                            completed,
                        )
                    )
        return stored, created

    @asynccontextmanager
    async def session_guard(
        self,
        scope: RunScope,
        session_id: str,
    ) -> AsyncIterator[None]:
        key = (scope.username, scope.workspace_id, str(session_id))
        async with self._session_locks_guard:
            state = self._session_locks.get(key)
            if state is None:
                state = _ScopedLockState()
                self._session_locks[key] = state
            state.leases += 1
        try:
            async with state.lock:
                yield
        finally:
            async with self._session_locks_guard:
                current = self._session_locks.get(key)
                if current is state:
                    state.leases = max(0, state.leases - 1)
                    if not state.leases and not state.lock.locked():
                        self._session_locks.pop(key, None)

    @asynccontextmanager
    async def _workspace_guard(
        self,
        workspace_id: str,
    ) -> AsyncIterator[None]:
        key = str(workspace_id or "").strip()
        async with self._workspace_locks_guard:
            state = self._workspace_locks.get(key)
            if state is None:
                state = _ScopedLockState()
                self._workspace_locks[key] = state
            state.leases += 1
        try:
            async with state.lock:
                yield
        finally:
            async with self._workspace_locks_guard:
                current = self._workspace_locks.get(key)
                if current is state:
                    state.leases = max(0, state.leases - 1)
                    if not state.leases and not state.lock.locked():
                        self._workspace_locks.pop(key, None)

    async def assert_session_available(
        self,
        scope: RunScope,
        session_id: str,
    ) -> None:
        key = (scope.username, scope.workspace_id, str(session_id))
        async with self._creation_lock:
            active_run_id = self._active_by_session.get(key)
            if not active_run_id:
                return
            active = await self.repository.get(active_run_id)
            if active is not None and active.status not in TERMINAL_RUN_STATUSES:
                raise SessionBusyError(active_run_id)
            self._active_by_session.pop(key, None)

    @asynccontextmanager
    async def workspace_deletion_guard(
        self,
        workspace_id: str,
    ) -> AsyncIterator[None]:
        """Block new Run creation while a workspace deletion is validated."""
        normalized_workspace_id = str(workspace_id or "").strip()
        if not normalized_workspace_id:
            raise ValueError("workspace_id is required")
        async with self._workspace_guard(normalized_workspace_id):
            async with self._creation_lock:
                snapshots = await self.repository.list_snapshots()
                active = None
                for snapshot in snapshots:
                    if snapshot.scope.workspace_id != normalized_workspace_id:
                        continue
                    if snapshot.status in TERMINAL_RUN_STATUSES:
                        continue
                    active = snapshot
                    break
                if active is not None:
                    raise WorkspaceBusyError(
                        active.run_id,
                        active.session_id,
                        active.status.value,
                    )
            yield

    async def get_run(
        self,
        run_id: str,
        scope: RunScope,
    ) -> Optional[RunSnapshot]:
        if not scope.allows("agent:read"):
            return None
        snapshot = await self.repository.get(run_id)
        if snapshot is None or not scope.owns(snapshot):
            return None
        return snapshot

    async def list_runs(self, scope: RunScope) -> list[RunSnapshot]:
        if not scope.allows("agent:read"):
            return []
        snapshots = await self.repository.list_snapshots()
        return [snapshot for snapshot in snapshots if scope.owns(snapshot)]

    async def cancel_run(
        self,
        run_id: str,
        scope: RunScope,
        reason: str = "user_requested",
    ) -> RunSnapshot:
        if not scope.allows("agent:cancel"):
            raise RunNotFoundError(run_id)
        visible = await self.repository.get(run_id)
        if visible is None or not scope.owns(visible):
            raise RunNotFoundError(run_id)
        entry = self._entries.setdefault(run_id, _RunEntry())
        async with entry.lock:
            snapshot = await self.repository.get(run_id)
            if snapshot is None or not scope.owns(snapshot):
                raise RunNotFoundError(run_id)
            if snapshot.status in TERMINAL_RUN_STATUSES:
                return snapshot
            cancel_future = self._cancel_futures.get(run_id)
            is_leader = cancel_future is None
            if cancel_future is None:
                cancel_future = asyncio.get_running_loop().create_future()
                self._cancel_futures[run_id] = cancel_future
                entry.cancel_requested = True
                entry.cancel_reason = reason

        if not is_leader:
            return await asyncio.shield(cancel_future)

        try:
            effective_reason = entry.cancel_reason or reason
            task = self.supervisor.get_run_task(run_id)
            if snapshot.status != RunStatus.QUEUED:
                await self._cancel_runtime_safely(run_id, effective_reason)
            if task is not None and task is not asyncio.current_task():
                task.cancel()

            if task is not None and task is not asyncio.current_task():
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=5.0)
                except asyncio.CancelledError:
                    pass
                except asyncio.TimeoutError:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)

            async with entry.lock:
                current = await self.repository.get(run_id)
                if current is None or not scope.owns(current):
                    raise RunNotFoundError(run_id)
                if current.status in TERMINAL_RUN_STATUSES:
                    updated = current
                else:
                    cancelled_result = RuntimeResult(status="cancelled")
                    await self._finalize_aborted_safely(
                        current.context,
                        cancelled_result,
                    )
                    updated = await self._finish(
                        current,
                        status=RunStatus.CANCELLED,
                        result=cancelled_result,
                        error=None,
                        cancel_reason=effective_reason,
                    )
                if not cancel_future.done():
                    cancel_future.set_result(updated)
                return updated
        except BaseException as exc:
            if not cancel_future.done():
                cancel_future.set_exception(exc)
                cancel_future.exception()
            raise
        finally:
            self._cancel_futures.pop(run_id, None)

    async def accept_runtime_event(
        self,
        run_id: str,
        draft: PublicEventDraft,
    ) -> None:
        if await self.repository.get(run_id) is None:
            return
        entry = self._entries.setdefault(run_id, _RunEntry())
        async with entry.lock:
            snapshot_for_filter = await self.repository.get(run_id)
            if snapshot_for_filter is None:
                return
            if draft.event_type == "turn.finished":
                snapshot = snapshot_for_filter
                if snapshot is None or entry.terminal_committed or snapshot.status in TERMINAL_RUN_STATUSES:
                    return
                status = _normalize_runtime_status(draft.status)
                normalized = draft.model_copy(
                    update={
                        "run_id": run_id,
                        "session_id": snapshot.session_id,
                        "status": status.value,
                        "payload": {
                            **dict(draft.payload),
                            "status": status.value,
                        },
                    },
                )
                if status == RunStatus.AWAITING_INPUT and pending_interaction_from_draft(normalized) is None:
                    status = RunStatus.FAILED
                    normalized = normalized.model_copy(
                        update={
                            "status": RunStatus.FAILED.value,
                            "payload": _failed_terminal_payload(
                                dict(normalized.payload),
                                error_code="INVALID_INTERACTION_STATE",
                                message="运行请求交互但未提供有效交互状态",
                            ),
                        },
                    )
                if entry.cancel_requested:
                    status = RunStatus.CANCELLED
                    normalized = normalized.model_copy(
                        update={
                            "status": RunStatus.CANCELLED.value,
                            "payload": {
                                "status": RunStatus.CANCELLED.value,
                                "cancel_reason": entry.cancel_reason,
                            },
                        }
                    )
                result = self._result_from_terminal(normalized)
                pending_interaction = pending_interaction_from_draft(normalized)
                if (
                    status == RunStatus.AWAITING_INPUT
                    and pending_interaction is not None
                    and not snapshot.context.supports_user_interaction
                ):
                    status = RunStatus.FAILED
                    pending_interaction = None
                    normalized = normalized.model_copy(
                        update={
                            "status": RunStatus.FAILED.value,
                            "payload": _failed_terminal_payload(
                                dict(normalized.payload),
                                error_code="INTERACTION_NOT_SUPPORTED",
                                message="当前协议不支持用户交互",
                            ),
                        },
                    )
                    result = self._result_from_terminal(normalized)

                def update(current: RunSnapshot) -> RunSnapshot:
                    return replace(
                        current,
                        turn_id=normalized.turn_id or current.turn_id,
                        status=status,
                        result=result,
                        error=result.error,
                        pending_interaction=pending_interaction,
                        updated_at=utc_now_iso(),
                    )

                _event, updated = await self.event_bus.publish(
                    normalized,
                    snapshot_update=update,
                )
                entry.terminal_committed = True
                await self.event_bus.close(run_id)
                return
            snapshot = snapshot_for_filter
            if snapshot is None:
                return
            if entry.terminal_committed or entry.cancel_requested:
                return
            if snapshot.status in TERMINAL_RUN_STATUSES:
                return
            normalized = draft.model_copy(
                update={"run_id": run_id, "session_id": snapshot.session_id},
            )
            pending_interaction = pending_interaction_from_draft(normalized)
            requests_interaction = normalized.event_type == "interaction.requested"
            has_existing_interaction = snapshot.pending_interaction is not None
            has_new_interaction = pending_interaction is not None
            if requests_interaction and has_existing_interaction and has_new_interaction:
                existing_id = str(snapshot.pending_interaction.get("id") or "")
                new_id = str(pending_interaction.get("id") or "")
                if existing_id != new_id:
                    error = RuntimeErrorInfo(
                        error_code="MULTIPLE_INTERACTIONS_NOT_ALLOWED",
                        message="one turn cannot request more than one interaction",
                        retriable=False,
                    )
                    await self._finish(
                        snapshot,
                        status=RunStatus.FAILED,
                        result=RuntimeResult(status="failed", error=error),
                        error=error,
                    )
                    return
            await self.event_bus.publish(
                normalized,
                snapshot_update=(
                    lambda current: replace(
                        current,
                        turn_id=normalized.turn_id or current.turn_id,
                        pending_interaction=pending_interaction or current.pending_interaction,
                    )
                ),
            )

    async def shutdown(self, *, deadline: Optional[float] = None) -> bool:
        loop = asyncio.get_running_loop()
        effective_deadline = float(deadline) if deadline is not None else loop.time() + 30.0
        snapshots = [
            snapshot
            for snapshot in await self.repository.list_snapshots()
            if snapshot.status not in TERMINAL_RUN_STATUSES
        ]
        if snapshots:
            await asyncio.gather(
                *(
                    self.cancel_run(
                        snapshot.run_id,
                        RunScope(
                            username=snapshot.scope.username,
                            workspace_id=snapshot.scope.workspace_id,
                            scopes=("agent:cancel",),
                        ),
                        reason="service_shutdown",
                    )
                    for snapshot in snapshots
                ),
                return_exceptions=True,
            )
        return await self.supervisor.close(deadline=effective_deadline)

    async def _execute(
        self,
        run_id: str,
    ) -> None:
        snapshot = await self.repository.get(run_id)
        if snapshot is None:
            return
        admission_snapshot = snapshot
        admitted = False
        try:
            admitted = await self._acquire_admission(snapshot)
        except asyncio.CancelledError:
            entry = self._entries.get(run_id)
            cancel_reason = (
                entry.cancel_reason if entry is not None and entry.cancel_reason else "task_cancelled_before_admission"
            )
            await self._finish_if_active(
                run_id,
                status=RunStatus.CANCELLED,
                result=RuntimeResult(status="cancelled"),
                error=None,
                cancel_reason=cancel_reason,
            )
            raise
        except asyncio.TimeoutError:
            error = RuntimeErrorInfo(
                error_code="RUN_ADMISSION_TIMEOUT",
                message="等待执行配额超时",
                retriable=True,
            )
            await self._finish_if_active(
                run_id,
                status=RunStatus.FAILED,
                result=RuntimeResult(status="failed", error=error),
                error=error,
                cancel_reason="admission_timeout",
            )
            return
        try:
            snapshot = await self.repository.get(run_id)
            if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
                return
            prepare_context = getattr(self.execution_service, "prepare_context", None)
            try:
                prepared_context = (
                    await prepare_context(snapshot.context) if callable(prepare_context) else snapshot.context
                )
            except Exception as exc:
                logger.error(
                    "run context preparation failed run_id=%s error_type=%s",
                    run_id,
                    type(exc).__name__,
                )
                error = RuntimeErrorInfo(
                    error_code="SESSION_PREPARE_FAILED",
                    message="会话运行状态准备失败",
                    retriable=False,
                )
                await self._finish_if_active(
                    run_id,
                    status=RunStatus.FAILED,
                    result=RuntimeResult(status="failed", error=error),
                    error=error,
                )
                return
            if prepared_context != snapshot.context:
                snapshot = await self.repository.replace_context(
                    run_id,
                    prepared_context,
                    expected={snapshot.status},
                )
            transitioned: RunSnapshot | None
            try:
                transitioned = await self.repository.transition(
                    run_id,
                    expected={RunStatus.QUEUED},
                    status=RunStatus.RUNNING,
                )
            except RunConflictError:
                transitioned = None
            if transitioned is None:
                return
            snapshot = transitioned
            await self.event_bus.publish(
                PublicEventDraft(
                    schema_version="1.0",
                    run_id=run_id,
                    session_id=snapshot.session_id,
                    turn_id=snapshot.turn_id,
                    event_type="run.started",
                    status="running",
                    source={"node": "consumer", "component": "run_manager"},
                    payload={"status": "running"},
                    created_at=utc_now_iso(),
                )
            )
            sink = _ManagerEventSink(self, run_id)
            phase_start_seq = snapshot.last_seq
            try:
                operation = self.execution_service.execute(snapshot.context, sink)
                if snapshot.context.timeout_seconds is None:
                    runtime_result = await operation
                else:
                    runtime_result = await asyncio.wait_for(
                        operation,
                        timeout=max(0.01, float(snapshot.context.timeout_seconds)),
                    )
                current = await self.repository.get(run_id)
                if current is not None and current.status == RunStatus.AWAITING_INPUT:
                    terminal_emitted = await self.repository.has_event_after(
                        run_id,
                        phase_start_seq,
                        "turn.finished",
                    )
                    if not terminal_emitted and current.pending_interaction is not None:
                        await self._finish_awaiting_input(current, current.pending_interaction)
                elif current is not None and current.status == RunStatus.RUNNING:
                    fallback_status = _normalize_runtime_status(runtime_result.status)
                    runtime_result = runtime_result.model_copy(
                        update={"status": fallback_status.value},
                    )
                    if fallback_status == RunStatus.AWAITING_INPUT:
                        if current.pending_interaction is not None:
                            await self._finish_awaiting_input(
                                current,
                                current.pending_interaction,
                            )
                        else:
                            error = RuntimeErrorInfo(
                                error_code="INVALID_INTERACTION_STATE",
                                message="运行请求交互但未提供有效交互状态",
                                retriable=False,
                            )
                            await self._finish_if_active(
                                run_id,
                                status=RunStatus.FAILED,
                                result=RuntimeResult(status="failed", error=error),
                                error=error,
                            )
                    elif current.status == RunStatus.RUNNING:
                        runtime_error = runtime_result.error
                        if fallback_status == RunStatus.FAILED and runtime_error is None:
                            runtime_error = RuntimeErrorInfo(
                                error_code="INTERNAL_ERROR",
                                message="Runtime 返回失败但未提供错误信息",
                                retriable=False,
                            )
                            runtime_result = runtime_result.model_copy(update={"error": runtime_error})
                        await self._finish_if_active(
                            run_id,
                            status=fallback_status,
                            result=runtime_result,
                            error=runtime_error,
                        )
            except asyncio.TimeoutError:
                await self._cancel_runtime_safely(run_id, "timeout")
                error = RuntimeErrorInfo(
                    error_code="MODEL_TIMEOUT",
                    message="运行超时",
                    retriable=True,
                )
                result = RuntimeResult(status="failed", error=error)
                await self._finalize_aborted_safely(snapshot.context, result)
                await self._finish_if_active(
                    run_id,
                    status=RunStatus.FAILED,
                    result=result,
                    error=error,
                    cancel_reason="timeout",
                )
            except asyncio.CancelledError:
                current = await self.repository.get(run_id)
                entry = self._entries.get(run_id)
                cancel_reason = entry.cancel_reason if entry is not None and entry.cancel_reason else "task_cancelled"
                if (
                    current is None or current.status not in TERMINAL_RUN_STATUSES
                ) and run_id not in self._cancel_futures:
                    await self._cancel_runtime_safely(run_id, cancel_reason)
                result = RuntimeResult(status="cancelled")
                await self._finalize_aborted_safely(snapshot.context, result)
                await self._finish_if_active(
                    run_id,
                    status=RunStatus.CANCELLED,
                    result=result,
                    error=None,
                    cancel_reason=cancel_reason,
                )
                raise
            except Exception:
                error = RuntimeErrorInfo(
                    error_code="INTERNAL_ERROR",
                    message="运行失败",
                    retriable=False,
                )
                result = RuntimeResult(status="failed", error=error)
                await self._finalize_aborted_safely(snapshot.context, result)
                await self._finish_if_active(
                    run_id,
                    status=RunStatus.FAILED,
                    result=result,
                    error=error,
                )
        finally:
            if admitted:
                await self._release_admission(admission_snapshot)
            session_key = (
                admission_snapshot.scope.username,
                admission_snapshot.scope.workspace_id,
                admission_snapshot.session_id,
            )
            async with self._creation_lock:
                if self._active_by_session.get(session_key) == run_id:
                    self._active_by_session.pop(session_key, None)

    async def _acquire_admission(self, snapshot: RunSnapshot) -> bool:
        key = (snapshot.scope.username, snapshot.scope.workspace_id)
        run_id = snapshot.run_id
        limit = max(1, int(snapshot.context.concurrency_limit))
        timeout = snapshot.context.admission_timeout_seconds

        async def wait_until_admitted() -> None:
            async with self._admission_condition:
                waiters = self._admission_waiters.setdefault(key, [])
                if run_id not in waiters:
                    waiters.append(run_id)
                try:
                    while True:
                        active = self._admitted_runs.setdefault(key, {})
                        # A stricter active protocol request must not be bypassed
                        # by another surface supplying a larger per-user limit.
                        effective_limit = min([limit, *active.values()])
                        if waiters and waiters[0] == run_id and len(active) < effective_limit:
                            waiters.pop(0)
                            if not waiters:
                                self._admission_waiters.pop(key, None)
                            active[run_id] = limit
                            self._admission_condition.notify_all()
                            return
                        await self._admission_condition.wait()
                except BaseException:
                    if run_id in waiters:
                        waiters.remove(run_id)
                    if not waiters:
                        self._admission_waiters.pop(key, None)
                    self._admission_condition.notify_all()
                    raise

        if timeout is None:
            await wait_until_admitted()
        else:
            await asyncio.wait_for(
                wait_until_admitted(),
                timeout=max(0.01, float(timeout)),
            )
        return True

    async def _release_admission(self, snapshot: RunSnapshot) -> None:
        key = (snapshot.scope.username, snapshot.scope.workspace_id)
        async with self._admission_condition:
            active = self._admitted_runs.get(key)
            if active is not None:
                active.pop(snapshot.run_id, None)
                if not active:
                    self._admitted_runs.pop(key, None)
            self._admission_condition.notify_all()

    async def _cancel_runtime_safely(self, run_id: str, reason: str) -> bool:
        try:
            return bool(await self.execution_service.cancel(run_id, reason))
        except Exception:
            # Durable consumer state still has to reach a terminal state even
            # when Runtime cleanup reports an error.
            return False

    async def _finalize_aborted_safely(
        self,
        context: RuntimeInvocationContext,
        result: RuntimeResult,
    ) -> bool:
        finalize_aborted = getattr(self.execution_service, "finalize_aborted", None)
        if not callable(finalize_aborted):
            return False
        try:
            await finalize_aborted(context, result)
            return True
        except Exception:
            return False

    def _task_finished(self, run_id: str, completed: asyncio.Task[Any]) -> None:
        del completed
        cleanup = self._cleanup_finished_run(run_id)
        try:
            self.supervisor.start_derived(
                cleanup,
                name="consumer-run-cleanup-{}".format(run_id),
            )
        except RuntimeError:
            cleanup.close()

    async def _cleanup_finished_run(self, run_id: str) -> None:
        # Yield once so cancel/interact continuations already waiting on the
        # current lock can complete before its registry entry is discarded.
        await asyncio.sleep(0)
        if self.supervisor.get_run_task(run_id) is not None:
            return
        entry = self._entries.get(run_id)
        if entry is None:
            return
        async with entry.lock:
            if self.supervisor.get_run_task(run_id) is not None or self._entries.get(run_id) is not entry:
                return
            snapshot = await self.repository.get(run_id)
            if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
                self._entries.pop(run_id, None)
                self._cancel_futures.pop(run_id, None)

    async def cleanup_finished_run(self, run_id: str) -> None:
        """Discard terminal in-memory state after in-flight continuations finish."""
        await self._cleanup_finished_run(run_id)

    async def _finish_awaiting_input(
        self,
        snapshot: RunSnapshot,
        pending_interaction: dict[str, Any],
    ) -> None:
        await self.accept_runtime_event(
            snapshot.run_id,
            PublicEventDraft(
                schema_version="1.0",
                run_id=snapshot.run_id,
                session_id=snapshot.session_id,
                turn_id=snapshot.turn_id,
                event_type="turn.finished",
                status=RunStatus.AWAITING_INPUT.value,
                source={"node": "consumer", "component": "run_manager"},
                payload={
                    "status": RunStatus.AWAITING_INPUT.value,
                    "final_output": {
                        "type": "user_interaction",
                        "text": str(pending_interaction.get("prompt") or ""),
                        "interaction": dict(pending_interaction),
                        "options": list(pending_interaction.get("options") or []),
                    },
                },
                created_at=utc_now_iso(),
            ),
        )

    async def _finish_if_active(
        self,
        run_id: str,
        *,
        status: RunStatus,
        result: RuntimeResult,
        error: Optional[RuntimeErrorInfo],
        cancel_reason: Optional[str] = None,
    ) -> Optional[RunSnapshot]:
        if await self.repository.get(run_id) is None:
            return None
        entry = self._entries.setdefault(run_id, _RunEntry())
        async with entry.lock:
            snapshot = await self.repository.get(run_id)
            if snapshot is None or entry.terminal_committed or snapshot.status in TERMINAL_RUN_STATUSES:
                return snapshot
            return await self._finish(
                snapshot,
                status=status,
                result=result,
                error=error,
                cancel_reason=cancel_reason,
            )

    async def _finish(
        self,
        snapshot: RunSnapshot,
        *,
        status: RunStatus,
        result: RuntimeResult,
        error: Optional[RuntimeErrorInfo],
        cancel_reason: Optional[str] = None,
    ) -> RunSnapshot:
        entry = self._entries.setdefault(snapshot.run_id, _RunEntry())
        if entry.terminal_committed:
            return await self.repository.get(snapshot.run_id) or snapshot
        if entry.cancel_requested and status != RunStatus.CANCELLED:
            status = RunStatus.CANCELLED
            result = RuntimeResult(status="cancelled")
            error = None
            cancel_reason = entry.cancel_reason or cancel_reason or "cancelled"
        payload: dict[str, Any] = {"status": status.value}
        if result.content is not None:
            payload["final_output"] = result.content
        elif result.structured_content is not None:
            payload["final_output"] = result.structured_content
        if result.usage is not None:
            payload["usage"] = dict(result.usage)
        if snapshot.context.include_trace and result.trace is not None:
            payload["trace"] = redact_sensitive_value(
                result.trace,
                max_collection_items=128,
            )
        if error is not None:
            payload["error"] = error.message
            payload["error_code"] = error.error_code
        if result.recovery is not None:
            payload["recovery"] = result.recovery.model_dump(mode="json")
        if cancel_reason:
            payload["cancel_reason"] = cancel_reason

        def update(current: RunSnapshot) -> RunSnapshot:
            if current.status in TERMINAL_RUN_STATUSES:
                return current
            return replace(
                current,
                status=status,
                result=result,
                error=error,
                pending_interaction=None,
                cancel_reason=cancel_reason,
                updated_at=utc_now_iso(),
            )

        _event, updated = await self.event_bus.publish(
            PublicEventDraft(
                schema_version="1.0",
                run_id=snapshot.run_id,
                session_id=snapshot.session_id,
                turn_id=snapshot.turn_id,
                event_type="turn.finished",
                status=status.value,
                source={"node": "consumer", "component": "run_manager"},
                payload=payload,
                created_at=utc_now_iso(),
            ),
            snapshot_update=update,
        )
        entry.terminal_committed = True
        await self.event_bus.close(snapshot.run_id)
        return updated

    async def _replace_context(
        self,
        run_id: str,
        context: RuntimeInvocationContext,
    ) -> None:
        await self.repository.replace_context(
            run_id,
            context,
            expected={RunStatus.RUNNING},
        )

    @staticmethod
    def _result_from_terminal(draft: PublicEventDraft) -> RuntimeResult:
        status = _normalize_runtime_status(draft.status)
        final_output = draft.payload.get("content")
        if final_output is None:
            final_output = draft.payload.get("final_output")
        explicit_structured = draft.payload.get("structured_content")
        if isinstance(final_output, str):
            content = final_output
            structured = explicit_structured if isinstance(explicit_structured, (dict, list)) else None
        elif isinstance(final_output, dict):
            content = str(final_output.get("text") or final_output.get("content") or "") or None
            structured = explicit_structured if isinstance(explicit_structured, (dict, list)) else final_output
        elif isinstance(explicit_structured, (dict, list)):
            content = None
            structured = explicit_structured
        else:
            content = None
            structured = None
        error = None
        if status == RunStatus.FAILED:
            raw_error = draft.payload.get("error")
            if isinstance(raw_error, dict):
                try:
                    error = RuntimeErrorInfo.model_validate(raw_error)
                except ValueError:
                    error = None
            if error is None:
                error = RuntimeErrorInfo(
                    error_code=str(draft.payload.get("error_code") or "INTERNAL_ERROR"),
                    message=str(raw_error or "运行失败"),
                    retriable=False,
                    source="runtime",
                )
        recovery = None
        if status == RunStatus.FAILED and isinstance(
            draft.payload.get("recovery"),
            dict,
        ):
            try:
                recovery = RuntimeRecoveryAction.model_validate(draft.payload["recovery"])
            except ValueError:
                recovery = None
        return RuntimeResult(
            status=status.value,
            content=content,
            structured_content=structured,
            usage=(dict(draft.payload.get("usage")) if isinstance(draft.payload.get("usage"), dict) else None),
            trace=(dict(draft.payload.get("trace")) if isinstance(draft.payload.get("trace"), dict) else None),
            error=error,
            recovery=recovery,
        )


def _normalize_runtime_status(value: Any) -> RunStatus:
    normalized = str(getattr(value, "value", value) or "").strip().lower()
    if normalized in {"success", "completed", "finished"}:
        return RunStatus.COMPLETED
    if normalized in {"error", "failed", "failure"}:
        return RunStatus.FAILED
    if normalized in {"cancelled", "canceled"}:
        return RunStatus.CANCELLED
    if normalized in {"awaiting", "awaiting_input"}:
        return RunStatus.AWAITING_INPUT
    return RunStatus.FAILED
