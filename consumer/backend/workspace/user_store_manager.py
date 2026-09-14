# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Workspace-aware store manager for chat and execution history."""

from __future__ import annotations

import os
import queue
import threading
import time
from collections.abc import Iterator
from concurrent.futures import Future, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Dict, Optional, Tuple

from persistence.chat_store import ChatStore, ToolOutputLimits
from persistence.execution_store import ExecutionStore
from persistence.deterministic_terminal import (
    recover_pending_deterministic_artifacts,
    recover_pending_deterministic_terminal,
)
from persistence.session_summary_index import (
    SessionSummaryIndex,
    SessionSummaryIndexError,
    session_summary_index_enabled,
)
from utils.identifiers import safe_user_storage_key
from utils.logging import get_logger
from workspace.workspaces import get_workspace_paths, list_workspaces


logger = get_logger("workspace.user_store_manager")
_RECOVERY_SCAN_TICK = object()
_RECOVERY_SCAN_END = object()
_RECOVERY_DISCOVERY_PENDING = object()
_RECOVERY_TARGET_PENDING = object()


class _RecoveryWorker:
    """One daemon worker for deadline-aware recovery operations."""

    def __init__(self, *, name: str) -> None:
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._closed = False
        self._thread = threading.Thread(
            target=self._run,
            name=name,
            daemon=True,
        )
        self._thread.start()

    def submit(self, operation) -> Future:
        future = Future()
        with self._lock:
            if self._closed:
                raise RuntimeError("session recovery discovery worker is closed")
            self._queue.put((future, operation))
        return future

    def shutdown(self, *, cancel_futures: bool = True) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if cancel_futures:
                while True:
                    try:
                        queued = self._queue.get_nowait()
                    except queue.Empty:
                        break
                    if queued is not None:
                        queued[0].cancel()
            self._queue.put(None)

    def _run(self) -> None:
        while True:
            work = self._queue.get()
            if work is None:
                return
            future, operation = work
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(operation())
            except BaseException as exc:
                future.set_exception(exc)


@dataclass(frozen=True, slots=True)
class SessionPruneResult:
    deleted_session_ids: tuple[str, ...]
    has_more: bool
    candidates_exhausted: bool
    no_progress: bool


@dataclass(frozen=True, slots=True)
class _RecoveryTargetResult:
    recovered: bool
    failed: bool


class UserStoreManager:
    def __init__(
        self,
        chat_base_dir: str,
        execution_base_dir: str,
        *,
        tool_output_limits: ToolOutputLimits | None = None,
    ) -> None:
        self._chat_base_dir = Path(chat_base_dir)
        self._execution_base_dir = Path(execution_base_dir)
        self._tool_output_limits = tool_output_limits
        self._chat_stores: Dict[Tuple[str, str], ChatStore] = {}
        self._execution_stores: Dict[Tuple[str, str], ExecutionStore] = {}
        self._summary_indexes: Dict[
            Tuple[str, str], Optional[SessionSummaryIndex]
        ] = {}
        self._store_initialization_locks: Dict[
            Tuple[str, str], threading.Lock
        ] = {}
        self._lock = threading.Lock()
        self._recovery_lock = threading.Lock()
        self._recovery_scan: Iterator[object | tuple[str, str, str]] | None = None
        self._recovery_scan_complete = False
        self._recovery_pending: object | tuple[str, str, str] | None = None
        self._recovery_discovery_executor: _RecoveryWorker | None = None
        self._recovery_discovery_future: Future | None = None
        self._recovery_target_executor: _RecoveryWorker | None = None
        self._recovery_target_future: Future | None = None
        self._recovery_active_target: tuple[str, str, str] | None = None
        self._summary_rebuild_lock = threading.Lock()
        self._summary_rebuild_executor: _RecoveryWorker | None = None
        self._summary_rebuild_futures: Dict[Tuple[str, str], Future] = {}
        self._summary_rebuild_closed = False
        self._summary_rebuild_stop = threading.Event()

    @staticmethod
    def _safe_user_dir(user_id: str) -> str:
        return safe_user_storage_key(user_id)

    def _ensure_stores_locked(
        self,
        *,
        safe_user: str,
        workspace_id: str,
    ) -> tuple[ChatStore, ExecutionStore, Optional[SessionSummaryIndex]]:
        key = (workspace_id, safe_user)
        with self._lock:
            chat_store = self._chat_stores.get(key)
            execution_store = self._execution_stores.get(key)
            if chat_store is not None and execution_store is not None:
                return chat_store, execution_store, self._summary_indexes.get(key)
            initialization_lock = self._store_initialization_locks.setdefault(
                key,
                threading.Lock(),
            )

        with initialization_lock:
            with self._lock:
                chat_store = self._chat_stores.get(key)
                execution_store = self._execution_stores.get(key)
                if chat_store is not None and execution_store is not None:
                    return (
                        chat_store,
                        execution_store,
                        self._summary_indexes.get(key),
                    )

            paths = get_workspace_paths(workspace_id)
            user_runtime_dir = paths.users_runtime_dir / safe_user
            holder: dict[str, object] = {}
            summary_index: Optional[SessionSummaryIndex] = None

            if session_summary_index_enabled():

                def rebuild_summaries():
                    current_chat = holder["chat"]
                    current_execution = holder["execution"]
                    summaries = current_chat.rebuild_session_summaries()
                    rebuilt = []
                    for summary in summaries:
                        runtime_fields = (
                            current_execution.try_get_session_summary_runtime(
                                summary.session_id
                            )
                        )
                        if runtime_fields is None:
                            raise RuntimeError(
                                "session runtime is busy during summary rebuild"
                            )
                        rebuilt.append(replace(summary, **runtime_fields))
                    return rebuilt

                def schedule_rebuild() -> bool:
                    current_index = holder.get("index")
                    if current_index is None:
                        return False
                    return self._schedule_summary_index_rebuild(
                        key,
                        current_index,
                    )

                summary_index = SessionSummaryIndex(
                    user_runtime_dir / "session-summary-index.json",
                    rebuild_loader=rebuild_summaries,
                    rebuild_scheduler=schedule_rebuild,
                )
                holder["index"] = summary_index

            chat_store = ChatStore(
                str(user_runtime_dir / "chats"),
                tool_output_limits=self._tool_output_limits,
                summary_index=summary_index,
            )
            execution_store = ExecutionStore(
                str(user_runtime_dir / "executions"),
                summary_index=summary_index,
            )
            holder.update(chat=chat_store, execution=execution_store)

            with self._lock:
                self._chat_stores[key] = chat_store
                self._execution_stores[key] = execution_store
                self._summary_indexes[key] = summary_index
                return chat_store, execution_store, summary_index

    def get_chat_store(self, user_id: str, workspace_id: str) -> ChatStore:
        safe_user = self._safe_user_dir(user_id)
        chat_store, _execution_store, _summary_index = self._ensure_stores_locked(
            safe_user=safe_user,
            workspace_id=workspace_id,
        )
        return chat_store

    def get_execution_store(self, user_id: str, workspace_id: str) -> ExecutionStore:
        safe_user = self._safe_user_dir(user_id)
        _chat_store, execution_store, _summary_index = self._ensure_stores_locked(
            safe_user=safe_user,
            workspace_id=workspace_id,
        )
        return execution_store

    def get_session_summary_index(
        self,
        user_id: str,
        workspace_id: str,
    ) -> Optional[SessionSummaryIndex]:
        safe_user = self._safe_user_dir(user_id)
        _chat_store, _execution_store, summary_index = self._ensure_stores_locked(
            safe_user=safe_user,
            workspace_id=workspace_id,
        )
        return summary_index

    def _schedule_summary_index_rebuild(
        self,
        key: Tuple[str, str],
        summary_index: SessionSummaryIndex,
    ) -> bool:
        with self._summary_rebuild_lock:
            if self._summary_rebuild_closed:
                return False
            existing = self._summary_rebuild_futures.get(key)
            if existing is not None and not existing.done():
                return True
            if self._summary_rebuild_executor is None:
                self._summary_rebuild_executor = _RecoveryWorker(
                    name="session-summary-rebuild"
                )

            def rebuild() -> bool:
                try:
                    summary_index.rebuild(cancel_event=self._summary_rebuild_stop)
                    return True
                except Exception:
                    if self._summary_rebuild_stop.is_set():
                        return False
                    summary_index.mark_degraded("scheduled_rebuild_failed")
                    logger.warning(
                        "Session summary rebuild failed workspace=%s user=%s",
                        key[0],
                        key[1],
                        exc_info=True,
                    )
                    return False

            future = self._summary_rebuild_executor.submit(rebuild)
            self._summary_rebuild_futures[key] = future

            def release(completed: Future) -> None:
                with self._summary_rebuild_lock:
                    if self._summary_rebuild_futures.get(key) is completed:
                        self._summary_rebuild_futures.pop(key, None)

            future.add_done_callback(release)
            return True

    def close_summary_rebuilds(self, *, wait_seconds: float = 0.25) -> bool:
        """Stop on-demand index rebuilds within one shared deadline."""

        with self._summary_rebuild_lock:
            self._summary_rebuild_closed = True
            self._summary_rebuild_stop.set()
            futures = tuple(self._summary_rebuild_futures.values())
            self._summary_rebuild_futures.clear()
            executor = self._summary_rebuild_executor
            self._summary_rebuild_executor = None
        complete = True
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        for future in futures:
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                complete = False
            except Exception:
                complete = False
        if executor is not None:
            executor.shutdown(cancel_futures=True)
        return complete

    def _iter_recovery_targets(self) -> Iterator[object | tuple[str, str, str]]:
        deferred_users = []
        for workspace in list_workspaces():
            yield _RECOVERY_SCAN_TICK
            workspace_id = str(workspace.get("id") or "").strip()
            if not workspace_id:
                continue
            users_dir = get_workspace_paths(workspace_id).users_runtime_dir
            try:
                user_dirs = os.scandir(users_dir)
            except FileNotFoundError:
                continue
            with user_dirs:
                for user_dir in user_dirs:
                    yield _RECOVERY_SCAN_TICK
                    if user_dir.is_symlink() or not user_dir.is_dir(
                        follow_symlinks=False
                    ):
                        continue
                    safe_user = user_dir.name
                    chat_store, _execution_store, summary_index = (
                        self._ensure_stores_locked(
                            safe_user=safe_user,
                            workspace_id=workspace_id,
                        )
                    )
                    sessions = self._recovery_sessions_for_user(
                        workspace_id,
                        safe_user,
                        chat_store,
                        summary_index,
                    )
                    if sessions is None:
                        deferred_users.append(
                            (workspace_id, safe_user, chat_store, summary_index)
                        )
                        continue
                    for session in sessions:
                        yield workspace_id, safe_user, (
                            session.session_id
                            if summary_index is not None
                            else session.id
                        )

        retry_delay = 0.01
        for _attempt in range(4):
            if not deferred_users:
                break
            pending_users = []
            for workspace_id, safe_user, chat_store, summary_index in deferred_users:
                yield _RECOVERY_SCAN_TICK
                time.sleep(retry_delay)
                sessions = self._recovery_sessions_for_user(
                    workspace_id,
                    safe_user,
                    chat_store,
                    summary_index,
                )
                if sessions is None:
                    pending_users.append(
                        (workspace_id, safe_user, chat_store, summary_index)
                    )
                    continue
                for session in sessions:
                    yield workspace_id, safe_user, (
                        session.session_id
                        if summary_index is not None
                        else session.id
                    )
            deferred_users = pending_users
            retry_delay = min(0.08, retry_delay * 2)

    @staticmethod
    def _recovery_sessions_for_user(
        workspace_id: str,
        safe_user: str,
        chat_store: ChatStore,
        summary_index: SessionSummaryIndex | None,
    ):
        if summary_index is None:
            try:
                return tuple(chat_store.list_sessions())
            except Exception:
                logger.warning(
                    "Session recovery discovery skipped unreadable chat store "
                    "workspace=%s user=%s",
                    workspace_id,
                    safe_user,
                    exc_info=True,
                )
                return ()
        try:
            ready = summary_index.load_persisted_for_read()
        except Exception:
            ready = False
            logger.warning(
                "Session recovery deferred unreadable summary index "
                "workspace=%s user=%s",
                workspace_id,
                safe_user,
                exc_info=True,
            )
        if not ready:
            summary_index.request_rebuild()
            return None
        try:
            return tuple(summary_index.list_all())
        except Exception:
            summary_index.request_rebuild()
            return None

    def _next_recovery_discovery_item(self):
        try:
            return next(self._recovery_scan)
        except StopIteration:
            return _RECOVERY_SCAN_END

    def _poll_recovery_discovery(
        self,
        *,
        deadline: float,
        stop_event: threading.Event | None,
    ):
        if self._recovery_discovery_executor is None:
            self._recovery_discovery_executor = _RecoveryWorker(
                name="session-recovery-discovery"
            )
        if self._recovery_discovery_future is None:
            self._recovery_discovery_future = (
                self._recovery_discovery_executor.submit(
                    self._next_recovery_discovery_item
                )
            )
        while True:
            if stop_event is not None and stop_event.is_set():
                return _RECOVERY_DISCOVERY_PENDING
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _RECOVERY_DISCOVERY_PENDING
            try:
                item = self._recovery_discovery_future.result(
                    timeout=min(0.01, remaining)
                )
            except FutureTimeout:
                continue
            except BaseException:
                self._recovery_discovery_future = None
                raise
            self._recovery_discovery_future = None
            return item

    def _recover_target(
        self,
        target: tuple[str, str, str],
    ) -> _RecoveryTargetResult:
        workspace_id, safe_user, session_id = target
        chat_store, execution_store, _summary_index = (
            self._ensure_stores_locked(
                safe_user=safe_user,
                workspace_id=workspace_id,
            )
        )
        try:
            terminal_recovered = recover_pending_deterministic_terminal(
                execution_store,
                chat_store,
                session_id,
            )
            runtime_fields = execution_store.get_session_summary_runtime(session_id)
            active_run_id = runtime_fields.get("active_run_id")
            if not active_run_id and chat_store.get_active_run(session_id):
                chat_store.clear_active_run(session_id)
            artifacts_recovered = recover_pending_deterministic_artifacts(
                execution_store,
                chat_store,
                session_id,
                run_is_active=lambda run_id: bool(
                    active_run_id and run_id == active_run_id
                ),
            )
            return _RecoveryTargetResult(
                recovered=bool(terminal_recovered or artifacts_recovered),
                failed=False,
            )
        except Exception:
            logger.warning(
                "Session recovery failed workspace=%s session=%s",
                workspace_id,
                session_id,
                exc_info=True,
            )
            return _RecoveryTargetResult(recovered=False, failed=True)

    def _poll_recovery_target(
        self,
        target: tuple[str, str, str],
        *,
        deadline: float,
        stop_event: threading.Event | None,
    ):
        if self._recovery_target_executor is None:
            self._recovery_target_executor = _RecoveryWorker(
                name="session-recovery-target"
            )
        if self._recovery_target_future is None:
            self._recovery_active_target = target
            self._recovery_target_future = self._recovery_target_executor.submit(
                lambda current=target: self._recover_target(current)
            )
        elif self._recovery_active_target != target:
            raise RuntimeError("another session recovery target is already active")
        while True:
            if stop_event is not None and stop_event.is_set():
                return _RECOVERY_TARGET_PENDING
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return _RECOVERY_TARGET_PENDING
            try:
                result = self._recovery_target_future.result(
                    timeout=min(0.01, remaining)
                )
            except FutureTimeout:
                continue
            except BaseException:
                self._recovery_target_future = None
                self._recovery_active_target = None
                raise
            self._recovery_target_future = None
            self._recovery_active_target = None
            return result

    def _finish_recovery_discovery_locked(self) -> None:
        executor = self._recovery_discovery_executor
        self._recovery_discovery_executor = None
        self._recovery_discovery_future = None
        if executor is not None:
            executor.shutdown(cancel_futures=True)

    def _finish_recovery_target_locked(self) -> None:
        executor = self._recovery_target_executor
        self._recovery_target_executor = None
        self._recovery_target_future = None
        self._recovery_active_target = None
        if executor is not None:
            executor.shutdown(cancel_futures=True)

    def close_recovery_discovery(self, *, wait_seconds: float = 0.25) -> bool:
        """Stop scheduling recovery and boundedly drain active worker steps."""

        with self._recovery_lock:
            futures = (
                self._recovery_discovery_future,
                self._recovery_target_future,
            )
            executors = (
                self._recovery_discovery_executor,
                self._recovery_target_executor,
            )
            self._recovery_discovery_executor = None
            self._recovery_discovery_future = None
            self._recovery_target_executor = None
            self._recovery_target_future = None
            self._recovery_active_target = None
            self._recovery_scan = None
            self._recovery_scan_complete = True
            self._recovery_pending = None
        complete = True
        deadline = time.monotonic() + max(0.0, float(wait_seconds))
        for future in futures:
            if future is None:
                continue
            try:
                future.result(timeout=max(0.0, deadline - time.monotonic()))
            except FutureTimeout:
                complete = False
            except Exception:
                complete = False
                logger.warning("Workspace recovery future failed", exc_info=True)
        for executor in executors:
            if executor is not None:
                executor.shutdown(cancel_futures=True)
        return complete

    def recover_pending_sessions_batch(
        self,
        *,
        max_items: int = 25,
        max_seconds: float = 0.25,
        stop_event: threading.Event | None = None,
    ) -> dict[str, int | bool]:
        """Recover one bounded startup batch without delaying app readiness."""

        item_budget = max(1, int(max_items))
        time_budget = max(0.01, float(max_seconds))
        deadline = time.monotonic() + time_budget
        processed = 0
        recovered = 0
        failures = 0
        if stop_event is not None and stop_event.is_set():
            return {
                "processed": 0,
                "recovered": 0,
                "failures": 0,
                "remaining": 1,
                "complete": False,
            }
        with self._recovery_lock:
            if self._recovery_scan is None and not self._recovery_scan_complete:
                self._recovery_scan = self._iter_recovery_targets()
            while not self._recovery_scan_complete and processed < item_budget:
                if stop_event is not None and stop_event.is_set():
                    break
                if time.monotonic() >= deadline:
                    break
                if self._recovery_target_future is not None:
                    target = self._recovery_active_target
                    if target is None:
                        raise RuntimeError("session recovery target ownership was lost")
                elif self._recovery_pending is not None:
                    target = self._recovery_pending
                    self._recovery_pending = None
                else:
                    target = self._poll_recovery_discovery(
                        deadline=deadline,
                        stop_event=stop_event,
                    )
                    if target is _RECOVERY_DISCOVERY_PENDING:
                        break
                    if target is _RECOVERY_SCAN_END:
                        self._recovery_scan = None
                        self._recovery_scan_complete = True
                        self._finish_recovery_discovery_locked()
                        self._finish_recovery_target_locked()
                        break
                if target is _RECOVERY_SCAN_TICK:
                    continue
                outcome = self._poll_recovery_target(
                    target,
                    deadline=deadline,
                    stop_event=stop_event,
                )
                if outcome is _RECOVERY_TARGET_PENDING:
                    break
                processed += 1
                recovered += int(outcome.recovered)
                failures += int(outcome.failed)
            should_poll_discovery = not self._recovery_scan_complete
            if self._recovery_pending is not None:
                should_poll_discovery = False
            if stop_event is not None and stop_event.is_set():
                should_poll_discovery = False
            if time.monotonic() >= deadline:
                should_poll_discovery = False
            if should_poll_discovery:
                discovered = self._poll_recovery_discovery(
                    deadline=deadline,
                    stop_event=stop_event,
                )
                if discovered is _RECOVERY_SCAN_END:
                    self._recovery_scan = None
                    self._recovery_scan_complete = True
                    self._finish_recovery_discovery_locked()
                    self._finish_recovery_target_locked()
                elif discovered is not _RECOVERY_DISCOVERY_PENDING:
                    # Discovery may consume the remaining batch budget. Submit
                    # the discovered target now so startup recovery advances in
                    # the background instead of waiting for another poll.
                    self._poll_recovery_target(
                        discovered,
                        deadline=deadline,
                        stop_event=stop_event,
                    )
            complete = self._recovery_scan_complete
        return {
            "processed": processed,
            "recovered": recovered,
            "failures": failures,
            "remaining": 0 if complete else 1,
            "complete": complete,
        }

    def prune_sessions(
        self,
        user_id: str,
        workspace_id: str,
        *,
        max_sessions: int,
        batch_size: int = 25,
    ) -> list[str]:
        """Age out old chat sessions and matching execution records for one user/workspace."""
        return list(
            self.prune_sessions_batch(
                user_id,
                workspace_id,
                max_sessions=max_sessions,
                batch_size=batch_size,
            ).deleted_session_ids
        )

    def prune_sessions_batch(
        self,
        user_id: str,
        workspace_id: str,
        *,
        max_sessions: int,
        batch_size: int = 25,
    ) -> SessionPruneResult:
        """Prune one bounded batch and report whether convergence remains."""

        resolved_batch_size = max(1, int(batch_size))
        chat_store = self.get_chat_store(user_id, workspace_id)
        execution_store = self.get_execution_store(user_id, workspace_id)
        summary_index = self.get_session_summary_index(user_id, workspace_id)
        if summary_index is None:
            deleted_session_ids = chat_store.prune_old_sessions(max_sessions)
            for session_id in deleted_session_ids:
                execution_store.delete_session(session_id)
            return SessionPruneResult(
                deleted_session_ids=tuple(deleted_session_ids),
                has_more=False,
                candidates_exhausted=True,
                no_progress=False,
            )

        if not summary_index.load_persisted_for_read():
            summary_index.request_rebuild()
            return SessionPruneResult(
                deleted_session_ids=(),
                has_more=True,
                candidates_exhausted=False,
                no_progress=True,
            )

        deleted_session_ids: list[str] = []
        attempted: set[str] = set()
        index_unavailable = False
        attempt_budget = max(
            resolved_batch_size,
            min(25, resolved_batch_size * 4),
        )
        while (
            len(deleted_session_ids) < resolved_batch_size
            and len(attempted) < attempt_budget
        ):
            try:
                candidates = summary_index.prune_candidates(
                    max_sessions=max_sessions,
                    limit=min(
                        resolved_batch_size - len(deleted_session_ids),
                        attempt_budget - len(attempted),
                    ),
                    excluded_session_ids=attempted,
                )
            except SessionSummaryIndexError:
                summary_index.request_rebuild()
                index_unavailable = True
                break
            if not candidates:
                break
            new_candidates = tuple(
                candidate
                for candidate in candidates
                if getattr(candidate, "session_id", candidate) not in attempted
            )
            if not new_candidates:
                break
            for candidate in new_candidates:
                session_id = getattr(candidate, "session_id", candidate)
                expected_updated_at = getattr(
                    candidate,
                    "expected_updated_at",
                    None,
                )
                attempted.add(session_id)
                guard = getattr(execution_store, "session_prune_guard", None)
                transaction = guard(session_id) if callable(guard) else self._lock
                with transaction as prune_allowed:
                    if prune_allowed is False:
                        # A request has reserved this session between its
                        # preflight and durable turn admission.
                        continue
                    runtime_fields = execution_store.get_session_summary_runtime(
                        session_id
                    )
                    status = str(runtime_fields.get("last_run_status") or "idle")
                    runtime_protected = bool(
                        runtime_fields.get("active_run_id")
                        or runtime_fields.get("pending_interaction")
                        or status not in {
                            "idle",
                            "completed",
                            "failed",
                            "cancelled",
                            "interrupted",
                        }
                    )
                    if runtime_protected:
                        try:
                            summary_index.update_runtime(
                                session_id,
                                **runtime_fields,
                            )
                        except SessionSummaryIndexError:
                            summary_index.request_rebuild()
                            logger.warning(
                                "Session summary projection deferred during prune "
                                "workspace=%s user=%s session=%s",
                                workspace_id,
                                self._safe_user_dir(user_id),
                                session_id,
                            )
                        continue
                    metadata_reader = getattr(chat_store, "get_session_metadata", None)
                    if callable(metadata_reader):
                        if metadata_reader(session_id) is None:
                            summary_index.delete(session_id)
                            continue
                    else:
                        # Compatibility for stores that do not expose the
                        # snapshot-only metadata read yet.
                        if chat_store.get_session(session_id) is None:
                            summary_index.delete(session_id)
                            continue
                    if chat_store.delete_session_if_unprotected(
                        session_id,
                        expected_updated_at=expected_updated_at,
                    ):
                        execution_store.delete_session(session_id)
                        deleted_session_ids.append(session_id)
                    else:
                        chat_store.refresh_session_summary(session_id)
                if len(deleted_session_ids) >= resolved_batch_size:
                    break

        if index_unavailable or not summary_index.load_persisted_for_read():
            summary_index.request_rebuild()
            index_unavailable = True
            has_more = True
        else:
            try:
                has_more = bool(
                    summary_index.prune_candidates(
                        max_sessions=max_sessions,
                        limit=1,
                    )
                )
            except SessionSummaryIndexError:
                summary_index.request_rebuild()
                index_unavailable = True
                has_more = True
        return SessionPruneResult(
            deleted_session_ids=tuple(deleted_session_ids),
            has_more=has_more,
            candidates_exhausted=not has_more,
            no_progress=(
                not deleted_session_ids
                and has_more
                and (bool(attempted) or index_unavailable)
            ),
        )
