"""Consumer asynchronous run lifecycle, cancellation, HITL, and replay."""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from typing import Any, Optional

from agent.runtime.contracts import (
    PublicEventDraft,
    PublicEventSink,
    RunScope,
    RunSnapshot,
    RunStatus,
    RuntimeErrorInfo,
    RuntimeInvocationContext,
    RuntimeResult,
    TERMINAL_RUN_STATUSES,
)
from agent.runtime.errors import (
    DuplicateInteractionError,
    InteractionConflictError,
    RunNotFoundError,
)
from agent.runtime.event_bus import AsyncEventBus, BlockingSubscription
from agent.runtime.service import AsyncExecutionService, pending_interaction_from_draft
from agent.runtime.store import InMemoryRunRepository, utc_now_iso
from utils.security.redaction import redact_sensitive_value


def request_fingerprint(context: RuntimeInvocationContext) -> str:
    canonical = json.dumps(
        {
            "session_id": context.session_id,
            "turn_id": context.turn_id,
            "workspace_id": context.workspace_id,
            "username": context.username,
            "query": context.query,
            "agent_mode": context.agent_mode,
            "llm_request_mode": context.llm_request_mode,
            "model_profile_id": context.model_profile_id,
            "arguments": context.arguments,
            "include_trace": context.include_trace,
            "supports_user_interaction": context.supports_user_interaction,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


class _ManagerEventSink(PublicEventSink):
    def __init__(self, manager: "AsyncRunManager", run_id: str) -> None:
        self.manager = manager
        self.run_id = run_id

    async def emit(self, draft: PublicEventDraft) -> None:
        await self.manager.accept_runtime_event(self.run_id, draft)


class AsyncRunManager:
    def __init__(
        self,
        execution_service: AsyncExecutionService,
        repository: Optional[InMemoryRunRepository] = None,
        event_bus: Optional[AsyncEventBus] = None,
    ) -> None:
        self.execution_service = execution_service
        self.repository = repository or InMemoryRunRepository()
        persist_event = getattr(execution_service, "persist_public_event", None)
        self.event_bus = event_bus or AsyncEventBus(
            self.repository,
            event_persistor=persist_event if callable(persist_event) else None,
        )
        if event_bus is not None and event_bus.event_persistor is None and callable(
            persist_event
        ):
            event_bus.event_persistor = persist_event
        self._tasks: dict[str, asyncio.Task[Any]] = {}
        self._admission_condition = asyncio.Condition()
        self._admission_waiters: dict[tuple[str, str], list[str]] = {}
        self._admitted_runs: dict[tuple[str, str], dict[str, int]] = {}
        self._admission_waiter_sessions: dict[str, tuple[str, str, str]] = {}
        self._admitted_sessions: dict[tuple[str, str, str], str] = {}
        self._run_locks: dict[str, asyncio.Lock] = {}
        self._cancel_futures: dict[str, asyncio.Future[RunSnapshot]] = {}
        self._cancel_reasons: dict[str, str] = {}

    async def create_run(
        self,
        context: RuntimeInvocationContext,
        scope: RunScope,
        *,
        idempotency_key: Optional[str] = None,
    ) -> tuple[RunSnapshot, bool]:
        if not scope.allows("agent:run"):
            raise PermissionError("缺少 agent:run 权限")
        if (
            context.username != scope.username
            or context.workspace_id != scope.workspace_id
        ):
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
        stored, created = await self.repository.create(
            snapshot,
            idempotency_key=idempotency_key,
            request_fingerprint=request_fingerprint(context),
        )
        if created:
            self._run_locks[context.run_id] = asyncio.Lock()
            task = asyncio.create_task(
                self._execute(context.run_id, resumed=False),
                name="consumer-run-{}".format(context.run_id),
            )
            self._tasks[context.run_id] = task
            task.add_done_callback(
                lambda completed, run_id=context.run_id: self._task_finished(
                    run_id,
                    completed,
                )
            )
        return stored, created

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

    async def subscribe(
        self,
        run_id: str,
        scope: RunScope,
        *,
        after_seq: int = 0,
    ) -> BlockingSubscription:
        if not scope.allows("agent:read"):
            raise RunNotFoundError(run_id)
        snapshot = await self.get_run(run_id, scope)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        return await self.event_bus.subscribe(run_id, after_seq=after_seq)

    async def unsubscribe(self, subscription: BlockingSubscription) -> None:
        await self.event_bus.unsubscribe(subscription)

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
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
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
                self._cancel_reasons[run_id] = reason

        if not is_leader:
            return await asyncio.shield(cancel_future)

        try:
            effective_reason = self._cancel_reasons.get(run_id, reason)
            task = self._tasks.get(run_id)
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

            async with lock:
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
                        cancel_reason=reason,
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
            self._cancel_reasons.pop(run_id, None)

    async def interact(
        self,
        run_id: str,
        scope: RunScope,
        *,
        interaction_id: str,
        value: Any,
        option_id: Optional[str] = None,
    ) -> RunSnapshot:
        if not scope.allows("agent:interact"):
            raise RunNotFoundError(run_id)
        visible = await self.repository.get(run_id)
        if visible is None or not scope.owns(visible):
            raise RunNotFoundError(run_id)
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            snapshot = await self.repository.get(run_id)
            if snapshot is None or not scope.owns(snapshot):
                raise RunNotFoundError(run_id)
            if await self.repository.interaction_consumed(run_id, interaction_id):
                raise DuplicateInteractionError("交互结果已经提交")
            if snapshot.status != RunStatus.AWAITING_INPUT:
                raise InteractionConflictError("当前运行不在等待交互状态")
            response = self._validate_interaction(
                snapshot.pending_interaction,
                interaction_id=interaction_id,
                value=value,
                option_id=option_id,
            )
            if not await self.repository.consume_interaction(run_id, interaction_id):
                raise DuplicateInteractionError("交互结果已经提交")
            await self.event_bus.reopen(run_id)
            resumed = await self.repository.transition(
                run_id,
                expected={RunStatus.AWAITING_INPUT},
                status=RunStatus.RUNNING,
                result=None,
                error=None,
                pending_interaction=None,
            )
            context = resumed.context.with_interaction(
                query=str(response.get("display_text") or response.get("value") or ""),
                interaction_response=response,
            )
            await self._replace_context(run_id, context)
            await self.event_bus.publish(PublicEventDraft(
                schema_version="1.0",
                run_id=run_id,
                session_id=resumed.session_id,
                # prepare_context will allocate the durable next turn before
                # Runtime events are emitted. Keep this control event attached
                # to the checkpoint turn instead of guessing the next turn id.
                turn_id=resumed.turn_id,
                event_type="interaction.resumed",
                status="running",
                source={"node": "consumer", "component": "run_manager"},
                payload={"interaction_id": interaction_id},
                created_at=utc_now_iso(),
            ))
            task = asyncio.create_task(
                self._execute(run_id, resumed=True, interaction_response=response),
                name="consumer-run-resume-{}".format(run_id),
            )
            self._tasks[run_id] = task
            task.add_done_callback(
                lambda completed, current_run_id=run_id: self._task_finished(
                    current_run_id,
                    completed,
                )
            )
            return await self.repository.get(run_id) or resumed

    async def accept_runtime_event(
        self,
        run_id: str,
        draft: PublicEventDraft,
    ) -> None:
        if await self.repository.get(run_id) is None:
            return
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            snapshot_for_filter = await self.repository.get(run_id)
            if snapshot_for_filter is None:
                return
            if draft.event_type == "turn.finished":
                snapshot = snapshot_for_filter
                if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
                    return
                if (
                    snapshot.status == RunStatus.AWAITING_INPUT
                    and await self.repository.latest_event_type(run_id) == "turn.finished"
                ):
                    # A Runtime that returns both an awaiting result and a
                    # synthetic terminal event must not create a second
                    # awaiting terminal for the same execution phase.
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
                            "payload": {
                            **dict(normalized.payload),
                            "status": RunStatus.FAILED.value,
                            "error": {
                                "error_code": "INVALID_INTERACTION_STATE",
                                "message": "运行请求交互但未提供有效交互状态",
                                "retriable": False,
                                "source": "runtime",
                                "details": {},
                            },
                            },
                        },
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
                            "payload": {
                            **dict(normalized.payload),
                            "status": RunStatus.FAILED.value,
                            "error": {
                                "error_code": "INTERACTION_NOT_SUPPORTED",
                                "message": "当前协议不支持用户交互",
                                "retriable": False,
                                "source": "runtime",
                                "details": {},
                            },
                            },
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
                if updated.status in TERMINAL_RUN_STATUSES or updated.status == RunStatus.AWAITING_INPUT:
                    await self.event_bus.close(run_id)
                return
            snapshot = snapshot_for_filter
            if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
                return
            normalized = draft.model_copy(
                update={"run_id": run_id, "session_id": snapshot.session_id},
            )
            pending_interaction = pending_interaction_from_draft(normalized)
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

    async def shutdown(self) -> None:
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
        remaining = [task for task in self._tasks.values() if not task.done()]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.gather(*remaining, return_exceptions=True)

    async def _execute(
        self,
        run_id: str,
        *,
        resumed: bool,
        interaction_response: Optional[dict[str, Any]] = None,
    ) -> None:
        snapshot = await self.repository.get(run_id)
        if snapshot is None:
            return
        admission_snapshot = snapshot
        admitted = False
        try:
            admitted = await self._acquire_admission(snapshot)
        except asyncio.CancelledError:
            cancel_reason = self._cancel_reasons.get(
                run_id,
                "task_cancelled_before_admission",
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
                    await prepare_context(snapshot.context, resumed=resumed)
                    if callable(prepare_context)
                    else snapshot.context
                )
            except Exception:
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
            if not resumed:
                try:
                    snapshot = await self.repository.transition(
                        run_id,
                        expected={RunStatus.QUEUED},
                        status=RunStatus.RUNNING,
                    )
                except Exception:
                    return
                await self.event_bus.publish(PublicEventDraft(
                    schema_version="1.0",
                    run_id=run_id,
                    session_id=snapshot.session_id,
                    turn_id=snapshot.turn_id,
                    event_type="run.started",
                    status="running",
                    source={"node": "consumer", "component": "run_manager"},
                    payload={"status": "running"},
                    created_at=utc_now_iso(),
                ))
            sink = _ManagerEventSink(self, run_id)
            phase_start_seq = snapshot.last_seq
            try:
                if resumed and interaction_response is not None:
                    operation = self.execution_service.interact(
                        snapshot.context,
                        sink,
                        interaction_response,
                    )
                else:
                    operation = self.execution_service.execute(snapshot.context, sink)
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
                            runtime_result = runtime_result.model_copy(
                                update={"error": runtime_error}
                            )
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
                cancel_reason = self._cancel_reasons.get(run_id, "task_cancelled")
                if (
                    (current is None or current.status not in TERMINAL_RUN_STATUSES)
                    and run_id not in self._cancel_futures
                ):
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

    async def _acquire_admission(self, snapshot: RunSnapshot) -> bool:
        key = (snapshot.scope.username, snapshot.scope.workspace_id)
        session_key = (*key, snapshot.session_id)
        run_id = snapshot.run_id
        limit = max(1, int(snapshot.context.concurrency_limit))
        timeout = snapshot.context.admission_timeout_seconds

        async def wait_until_admitted() -> None:
            async with self._admission_condition:
                waiters = self._admission_waiters.setdefault(key, [])
                if run_id not in waiters:
                    waiters.append(run_id)
                self._admission_waiter_sessions[run_id] = session_key
                try:
                    while True:
                        active = self._admitted_runs.setdefault(key, {})
                        # A stricter active protocol request must not be bypassed
                        # by another surface supplying a larger per-user limit.
                        effective_limit = min([limit, *active.values()])
                        first_eligible = next(
                            (
                                waiter
                                for waiter in waiters
                                if self._admission_waiter_sessions.get(waiter)
                                not in self._admitted_sessions
                            ),
                            None,
                        )
                        if first_eligible == run_id and len(active) < effective_limit:
                            waiters.remove(run_id)
                            if not waiters:
                                self._admission_waiters.pop(key, None)
                            self._admission_waiter_sessions.pop(run_id, None)
                            active[run_id] = limit
                            self._admitted_sessions[session_key] = run_id
                            self._admission_condition.notify_all()
                            return
                        await self._admission_condition.wait()
                except BaseException:
                    if run_id in waiters:
                        waiters.remove(run_id)
                    if not waiters:
                        self._admission_waiters.pop(key, None)
                    self._admission_waiter_sessions.pop(run_id, None)
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
            session_key = (
                snapshot.scope.username,
                snapshot.scope.workspace_id,
                snapshot.session_id,
            )
            if self._admitted_sessions.get(session_key) == snapshot.run_id:
                self._admitted_sessions.pop(session_key, None)
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
        if self._tasks.get(run_id) is completed:
            self._tasks.pop(run_id, None)
        asyncio.create_task(
            self._cleanup_finished_run(run_id),
            name="consumer-run-cleanup-{}".format(run_id),
        )

    async def _cleanup_finished_run(self, run_id: str) -> None:
        # Yield once so cancel/interact continuations already waiting on the
        # current lock can complete before its registry entry is discarded.
        await asyncio.sleep(0)
        if run_id in self._tasks:
            return
        lock = self._run_locks.get(run_id)
        if lock is None:
            return
        async with lock:
            if run_id in self._tasks or self._run_locks.get(run_id) is not lock:
                return
            snapshot = await self.repository.get(run_id)
            if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
                self._run_locks.pop(run_id, None)
                self._cancel_futures.pop(run_id, None)
                self._cancel_reasons.pop(run_id, None)

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
        lock = self._run_locks.setdefault(run_id, asyncio.Lock())
        async with lock:
            snapshot = await self.repository.get(run_id)
            if snapshot is None or snapshot.status in TERMINAL_RUN_STATUSES:
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
            structured = (
                explicit_structured
                if isinstance(explicit_structured, (dict, list))
                else None
            )
        elif isinstance(final_output, dict):
            content = str(final_output.get("text") or final_output.get("content") or "") or None
            structured = (
                explicit_structured
                if isinstance(explicit_structured, (dict, list))
                else final_output
            )
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
        return RuntimeResult(
            status=status.value,
            content=content,
            structured_content=structured,
            usage=(
                dict(draft.payload.get("usage"))
                if isinstance(draft.payload.get("usage"), dict)
                else None
            ),
            trace=(
                dict(draft.payload.get("trace"))
                if isinstance(draft.payload.get("trace"), dict)
                else None
            ),
            error=error,
        )

    @staticmethod
    def _validate_interaction(
        pending: Optional[dict[str, Any]],
        *,
        interaction_id: str,
        value: Any,
        option_id: Optional[str],
    ) -> dict[str, Any]:
        if not pending or str(pending.get("id") or "") != interaction_id:
            raise InteractionConflictError("interaction_id 与待处理交互不匹配")
        interaction_type = str(pending.get("type") or "input")
        response: dict[str, Any] = {
            "interaction_id": interaction_id,
            "type": interaction_type,
        }
        if interaction_type == "input":
            if not isinstance(value, str) or not value.strip():
                raise InteractionConflictError("输入内容不能为空")
            response["value"] = value
            response["display_text"] = value
            return response
        options = pending.get("options")
        if not isinstance(options, list):
            raise InteractionConflictError("待处理交互没有可选项")
        selected = next(
            (
                item
                for item in options
                if isinstance(item, dict)
                and (
                    (option_id and str(item.get("id") or "") == option_id)
                    or (not option_id and item.get("value") == value)
                )
            ),
            None,
        )
        if selected is None or (option_id and "value" in selected and selected.get("value") != value):
            raise InteractionConflictError("提交的选项不属于待处理交互")
        response["option_id"] = str(selected.get("id") or "")
        response["value"] = selected.get("value")
        response["display_text"] = str(selected.get("label") or selected.get("value") or "")
        return response


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
