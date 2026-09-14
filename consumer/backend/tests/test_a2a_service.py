# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""A2A adapter contract tests over canonical Runs and durable Turns."""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace

import pytest
from agent.runtime.contracts import (
    USER_WORKSPACE_RUN_CONCURRENCY_LIMIT,
    RunPreflightSnapshot,
    RunSnapshot,
    RunStatus,
    RuntimeResult,
)
from agent.runtime.errors import RunConflictError
from api.protocols.a2a.models import A2ATaskCreateRequest, A2ATaskState
from api.protocols.a2a.service import A2ATaskService
from persistence.execution_store import ExecutionStore


class _InlinePool:
    async def run(self, function, *args, **kwargs):
        return function(*args, **kwargs)


class _Manager:
    def __init__(self):
        self.snapshots = {}
        self.contexts = []
        self.cancelled = []

    @asynccontextmanager
    async def session_guard(self, _scope, _session_id):
        yield

    async def assert_session_available(self, _scope, _session_id):
        return None

    async def create_run(self, context, scope, *, session_guarded=False):
        assert session_guarded is True
        self.contexts.append(context)
        snapshot = RunSnapshot(
            run_id=context.run_id,
            session_id=context.session_id,
            turn_id=context.turn_id,
            status=RunStatus.QUEUED,
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
            last_seq=0,
            scope=scope,
            context=context,
        )
        self.snapshots[context.run_id] = snapshot
        return snapshot, True

    async def get_run(self, run_id, scope):
        snapshot = self.snapshots.get(run_id)
        return snapshot if snapshot is not None and scope.owns(snapshot) else None

    async def cancel_run(self, run_id, scope, reason):
        snapshot = await self.get_run(run_id, scope)
        assert snapshot is not None
        self.cancelled.append((run_id, reason))
        snapshot = replace(
            snapshot,
            status=RunStatus.CANCELLED,
            result=RuntimeResult(status="cancelled"),
        )
        self.snapshots[run_id] = snapshot
        return snapshot


class _Runtime:
    def __init__(self, manager, *, preflight_builder=None):
        self.manager = manager
        self.short_io = _InlinePool()
        if preflight_builder is not None:
            self.protocol_preflight_builder = preflight_builder


def _service(tmp_path, manager=None, *, preflight_builder=None):
    manager = manager or _Manager()
    return (
        A2ATaskService(
            runtime=_Runtime(manager, preflight_builder=preflight_builder),
            execution_store=ExecutionStore(str(tmp_path / "executions")),
            username="operator_a",
            workspace_id="workspace_demo",
        ),
        manager,
    )


def test_create_task_keeps_external_id_out_of_canonical_identity(tmp_path):
    service, manager = _service(tmp_path)

    view, snapshot = asyncio.run(
        service.create_task(
            A2ATaskCreateRequest(
                message="synthetic question",
                session_id="session_demo",
                task_id="external_task_demo",
            )
        )
    )

    assert view.id == "external_task_demo"
    assert snapshot.run_id.startswith("agentic-")
    assert snapshot.run_id != view.id
    assert view.metadata["canonical_run_id"] == snapshot.run_id
    assert manager.contexts[0].supports_user_interaction is False
    assert manager.contexts[0].concurrency_limit == USER_WORKSPACE_RUN_CONCURRENCY_LIMIT
    _, turn = service.execution_store.find_turn_by_protocol_id("a2a", "external_task_demo")
    assert turn["run_id"] == snapshot.run_id


def test_create_task_builds_one_dynamic_preflight_before_each_durable_turn(
    tmp_path,
):
    events = []
    calls = []
    expected = RunPreflightSnapshot(
        workspace_id="workspace_demo",
        workspace_config_generation=1,
        system_config_generation=1,
    )

    def build_preflight(**kwargs):
        events.append("preflight")
        calls.append(kwargs)
        return expected

    service, manager = _service(
        tmp_path,
        preflight_builder=build_preflight,
    )
    create_turn = service.execution_store.create_turn_for_run

    def tracked_create_turn(*args, **kwargs):
        events.append("durable_turn")
        return create_turn(*args, **kwargs)

    service.execution_store.create_turn_for_run = tracked_create_turn
    for index in range(2):
        asyncio.run(
            service.create_task(
                A2ATaskCreateRequest(
                    message=f"synthetic question {index}",
                    session_id=f"session_dynamic_preflight_{index}",
                    task_id=f"external_task_{index}",
                    context={"model_profile_id": "global:synthetic"},
                )
            )
        )

    assert len(calls) == 2
    assert [call["session_id"] for call in calls] == [
        "session_dynamic_preflight_0",
        "session_dynamic_preflight_1",
    ]
    assert events == [
        "preflight",
        "durable_turn",
        "preflight",
        "durable_turn",
    ]
    assert [context.preflight_snapshot for context in manager.contexts] == [
        expected,
        expected,
    ]


def test_create_task_releases_protocol_id_when_preflight_fails(tmp_path):
    calls = []

    def build_preflight(**_kwargs):
        calls.append("preflight")
        if len(calls) == 1:
            raise ValueError("synthetic preflight failure")
        return RunPreflightSnapshot(
            workspace_id="workspace_demo",
            workspace_config_generation=1,
            system_config_generation=1,
        )

    service, manager = _service(tmp_path, preflight_builder=build_preflight)
    request = A2ATaskCreateRequest(
        message="synthetic question",
        session_id="session_preflight_retry",
        task_id="external_task_preflight_retry",
    )

    with pytest.raises(ValueError, match="synthetic preflight failure"):
        asyncio.run(service.create_task(request))
    assert service.execution_store.find_turn_by_protocol_id(
        "a2a",
        "external_task_preflight_retry",
    ) == (None, None)

    view, _snapshot = asyncio.run(service.create_task(request))

    assert view.id == "external_task_preflight_retry"
    assert len(manager.contexts) == 1
    assert calls == ["preflight", "preflight"]


def test_create_task_normalizes_operation_pin_for_preflight_and_execution(tmp_path):
    calls = []

    def build_preflight(**kwargs):
        calls.append(kwargs)
        return RunPreflightSnapshot(
            workspace_id="workspace_demo",
            workspace_config_generation=1,
            system_config_generation=1,
            deterministic_candidate=True,
        )

    service, manager = _service(tmp_path, preflight_builder=build_preflight)

    asyncio.run(
        service.create_task(
            A2ATaskCreateRequest(
                message="synthetic question",
                session_id="session_operation_pin",
                task_id="external_task_operation_pin",
                context={"requested_operation_key": "  synthetic_operation  "},
            )
        )
    )

    assert calls[0]["user_context"]["requested_operation_key"] == ("synthetic_operation")
    assert manager.contexts[0].requested_operation_key == "synthetic_operation"


def test_duplicate_external_task_id_is_rejected(tmp_path):
    service, _manager = _service(tmp_path)
    request = A2ATaskCreateRequest(
        message="synthetic question",
        session_id="session_demo",
        task_id="external_task_demo",
    )
    asyncio.run(service.create_task(request))

    with pytest.raises(RunConflictError, match="task_id already exists"):
        asyncio.run(service.create_task(request))


def test_unexpected_awaiting_is_projected_as_interaction_not_supported(tmp_path):
    service, manager = _service(tmp_path)
    scope = service.scope
    context = manager.contexts[0] if manager.contexts else None
    if context is None:
        from agent.runtime.contracts import RuntimeInvocationContext

        context = RuntimeInvocationContext(
            run_id="agentic-synthetic",
            session_id="session_demo",
            turn_id=1,
            workspace_id="workspace_demo",
            username="operator_a",
            query="synthetic question",
            supports_user_interaction=False,
        )
    snapshot = RunSnapshot(
        run_id=context.run_id,
        session_id=context.session_id,
        turn_id=context.turn_id,
        status=RunStatus.AWAITING_INPUT,
        created_at="2026-01-01T00:00:00Z",
        updated_at="2026-01-01T00:00:01Z",
        last_seq=1,
        scope=scope,
        context=context,
        result=RuntimeResult(status="awaiting_input"),
        pending_interaction={"id": "interaction_demo", "type": "input"},
    )

    view = service.project(snapshot, external_task_id="external_task_demo")

    assert view.state is A2ATaskState.FAILED
    assert view.metadata["error_code"] == "INTERACTION_NOT_SUPPORTED"


def test_cancel_uses_canonical_run_id(tmp_path):
    service, manager = _service(tmp_path)
    _view, snapshot = asyncio.run(
        service.create_task(
            A2ATaskCreateRequest(
                message="synthetic question",
                session_id="session_demo",
                task_id="external_task_demo",
            )
        )
    )

    cancelled = asyncio.run(service.cancel_task("external_task_demo"))

    assert cancelled.state is A2ATaskState.CANCELED
    assert manager.cancelled == [(snapshot.run_id, "a2a_cancel")]
