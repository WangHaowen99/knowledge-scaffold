"""Tests for A2A task service safeguards and status projection."""

from __future__ import annotations

from contextlib import contextmanager
from types import SimpleNamespace

from flask import Flask, current_app

from api.protocols.a2a.models import A2ATaskState
from api.protocols.a2a.service import A2ATaskService
from persistence.run_store import RunStore, RunStatus


class _FakeChatStore:
    def __init__(self, sessions=None):
        self.sessions = dict(sessions or {})

    def get_session(self, session_id):
        return self.sessions.get(session_id)

    def create_session(self, session_id):
        session = SimpleNamespace(session_id=session_id)
        self.sessions[session_id] = session
        return session


class _FakeExecutionStore:
    def __init__(self, history=None):
        self.history = history
        self.failed_turns = []

    def get_session_history(self, session_id):
        del session_id
        return self.history

    def fail_turn(self, session_id, turn_id, error):
        self.failed_turns.append(
            {
                "session_id": session_id,
                "turn_id": turn_id,
                "error": error,
            }
        )


class _FakeOrchestrator:
    def __init__(self, *, run_impl, run_store=None):
        self._run_impl = run_impl
        self.run_store = run_store or RunStore()

    def run(self, **kwargs):
        return self._run_impl(**kwargs)


def test_get_task_ignores_other_run_awaiting_input():
    run_store = RunStore()
    run_store.create_run("task-a", "hello", metadata={"session_id": "session-1"})
    run_store.complete_run("task-a", {"ok": True})

    execution_store = _FakeExecutionStore(
        {
            "turns": [
                {"turn_id": 1, "run_id": "task-a", "status": "success"},
                {"turn_id": 2, "run_id": "task-b", "status": "awaiting_input"},
            ]
        }
    )
    service = A2ATaskService(
        orchestrator=SimpleNamespace(run_store=run_store),
        chat_store=_FakeChatStore(),
        execution_store=execution_store,
        user_id="user-1",
        workspace_id="ws-1",
    )

    snapshot = service.get_task("task-a")

    assert snapshot.state == A2ATaskState.COMPLETED


def test_get_task_ignores_stale_matching_awaiting_input_run():
    run_store = RunStore()
    run_store.create_run("task-a", "hello", metadata={"session_id": "session-1"})
    run_store.complete_run("task-a", {"ok": True})

    execution_store = _FakeExecutionStore(
        {
            "turns": [
                {"turn_id": 1, "run_id": "task-a", "status": "awaiting_input"},
            ]
        }
    )
    service = A2ATaskService(
        orchestrator=SimpleNamespace(run_store=run_store),
        chat_store=_FakeChatStore(),
        execution_store=execution_store,
        user_id="user-1",
        workspace_id="ws-1",
    )

    snapshot = service.get_task("task-a")

    assert snapshot.state == A2ATaskState.COMPLETED


def test_background_task_enters_workspace_and_app_context(monkeypatch):
    entered = {"workspace": None, "app_name": None}

    @contextmanager
    def fake_workspace_scope(workspace_id):
        entered["workspace"] = workspace_id
        yield workspace_id

    monkeypatch.setattr("api.protocols.a2a.service.workspace_scope", fake_workspace_scope)

    app = Flask("a2a-service-test")

    def run_impl(**kwargs):
        assert kwargs["workspace_id"] == "ws-1"
        assert entered["workspace"] == "ws-1"
        assert kwargs["user_context"]["supports_user_interaction"] is False
        entered["app_name"] = current_app.name
        yield {"event": "agent.v2", "data": {"run_id": kwargs["requested_run_id"]}}

    service = A2ATaskService(
        orchestrator=_FakeOrchestrator(run_impl=run_impl),
        chat_store=_FakeChatStore(),
        execution_store=_FakeExecutionStore(),
        user_id="user-1",
        workspace_id="ws-1",
        flask_app=app,
    )

    service._run_task_in_background(
        task_id="task-1",
        session_id="session-1",
        message="hello",
        context={},
    )

    assert entered["workspace"] == "ws-1"
    assert entered["app_name"] == "a2a-service-test"


def test_background_task_marks_failed_when_bootstrap_raises(monkeypatch):
    @contextmanager
    def fake_workspace_scope(workspace_id):
        yield workspace_id

    monkeypatch.setattr("api.protocols.a2a.service.workspace_scope", fake_workspace_scope)

    run_store = RunStore()

    def run_impl(**kwargs):
        del kwargs
        raise RuntimeError("boom")

    service = A2ATaskService(
        orchestrator=_FakeOrchestrator(run_impl=run_impl, run_store=run_store),
        chat_store=_FakeChatStore(),
        execution_store=_FakeExecutionStore(),
        user_id="user-1",
        workspace_id="ws-1",
    )

    service._run_task_in_background(
        task_id="task-boot-fail",
        session_id="session-1",
        message="hello",
        context={},
    )

    run = run_store.get_run("task-boot-fail")
    assert run is not None
    assert run.status == RunStatus.FAILED
    assert run.error == "Workflow execution failed"
    assert run.metadata["session_id"] == "session-1"
