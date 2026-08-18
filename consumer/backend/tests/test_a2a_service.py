"""Tests for the A2A projection over the canonical async run manager."""

from __future__ import annotations

import asyncio
from dataclasses import replace
from types import SimpleNamespace

import pytest

from agent.runtime.contracts import (
    RunSnapshot,
    RunStatus,
    RuntimeInvocationContext,
    RuntimeResult,
)
from agent.runtime.errors import RunNotFoundError
from api.protocols.a2a.models import A2ATaskCreateRequest, A2ATaskState
from api.protocols.a2a.service import A2ATaskService


class _FakeChatStore:
    def __init__(self):
        self.sessions = {}

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def create_session(self, session_id):
        session = SimpleNamespace(session_id=session_id)
        self.sessions[session_id] = session
        return session


class _FakeAsyncManager:
    def __init__(self):
        self.snapshots = {}
        self.created_contexts = []
        self.cancelled = []

    async def create_run(self, context, scope):
        self.created_contexts.append(context)
        snapshot = _snapshot(context, RunStatus.QUEUED, scope=scope)
        self.snapshots[context.run_id] = snapshot
        return snapshot, True

    async def get_run(self, run_id, scope):
        snapshot = self.snapshots.get(run_id)
        if snapshot is None or not scope.owns(snapshot):
            return None
        return snapshot

    async def cancel_run(self, run_id, scope, reason):
        snapshot = await self.get_run(run_id, scope)
        if snapshot is None:
            raise RunNotFoundError(run_id)
        self.cancelled.append((run_id, reason))
        snapshot = replace(
            snapshot,
            status=RunStatus.CANCELLED,
            result=RuntimeResult(status="cancelled"),
        )
        self.snapshots[run_id] = snapshot
        return snapshot


def _snapshot(context, status, *, scope, result=None):
    return RunSnapshot(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        status=status,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
        last_seq=1,
        scope=scope,
        context=context,
        result=result,
    )


def _call(coroutine):
    return asyncio.run(coroutine)


def _service(manager=None):
    return A2ATaskService(
        chat_store=_FakeChatStore(),
        user_id="operator_a",
        workspace_id="workspace_demo",
        async_manager=manager or _FakeAsyncManager(),
        async_call=_call,
    )


def test_service_requires_canonical_async_dependencies():
    with pytest.raises(ValueError, match="canonical async run manager"):
        A2ATaskService(
            chat_store=_FakeChatStore(),
            user_id="operator_a",
            workspace_id="workspace_demo",
            async_manager=None,
            async_call=None,
        )


def test_create_task_submits_runtime_context_and_projects_pending_state():
    manager = _FakeAsyncManager()
    service = _service(manager)

    task = service.create_task(
        A2ATaskCreateRequest(
            message="synthetic question",
            session_id="session_demo",
            task_id="task_demo",
            context={
                "model_profile_id": "profile_demo",
                "arguments": {"selected_ontology_id": "ontology_demo"},
            },
        )
    )

    assert task.state == A2ATaskState.PENDING
    assert task.metadata == {"mode": "async_run_manager"}
    context = manager.created_contexts[0]
    assert context.run_id == "task_demo"
    assert context.session_id == "session_demo"
    assert context.username == "operator_a"
    assert context.workspace_id == "workspace_demo"
    assert context.supports_user_interaction is False
    assert context.model_profile_id == "profile_demo"


def test_get_task_projects_completed_runtime_result():
    manager = _FakeAsyncManager()
    service = _service(manager)
    service.create_task(
        A2ATaskCreateRequest(
            message="synthetic question",
            session_id="session_demo",
            task_id="task_demo",
        )
    )
    queued = manager.snapshots["task_demo"]
    manager.snapshots["task_demo"] = replace(
        queued,
        status=RunStatus.COMPLETED,
        result=RuntimeResult(status="completed", content="synthetic answer"),
    )

    task = service.get_task("task_demo")

    assert task.state == A2ATaskState.COMPLETED
    assert task.result == {"status": "completed", "content": "synthetic answer"}
    assert task.completed_at == task.updated_at


def test_duplicate_task_id_is_rejected_by_runtime_repository_view():
    service = _service()
    request = A2ATaskCreateRequest(
        message="synthetic question",
        session_id="session_demo",
        task_id="task_demo",
    )
    service.create_task(request)

    with pytest.raises(ValueError, match="task_id already exists"):
        service.create_task(request)


def test_cancel_task_uses_async_manager_and_preserves_reason():
    manager = _FakeAsyncManager()
    service = _service(manager)
    service.create_task(
        A2ATaskCreateRequest(
            message="synthetic question",
            session_id="session_demo",
            task_id="task_demo",
        )
    )

    assert service.cancel_task("task_demo") is True
    assert manager.cancelled == [("task_demo", "a2a_cancel")]
    assert service.get_task("task_demo").state == A2ATaskState.CANCELED
