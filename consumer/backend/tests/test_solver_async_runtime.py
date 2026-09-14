# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from internal_access import internal
import asyncio
from operator import getitem
from types import SimpleNamespace

from agent.core.jiuwen.tools import CurrentRuntimeToolAdapter
from agent.core.shared.solver_route_guard import (
    SolverRouteRequirement,
    solver_route_requirement_from_state,
    solver_tools_suppressed_from_state,
)
from agent.runtime.contracts import RunPreflightSnapshot, RuntimeInvocationContext
from agent.runtime.jiuwen import JiuwenAsyncAgentRuntime
from agent.runtime.jiuwen_events import JiuwenRuntimeEventBridge
from agent.runtime.service import AsyncExecutionService, OrchestratorSessionAdapter
from agent.tools.base import ToolBatchResult
from backend_synthetic_credentials import synthetic_secret
from persistence.chat_store import ChatStore
from persistence.execution_store import ExecutionStore


def _context(
    run_id: str,
    *,
    query: str = "solve synthetic route plan",
    history_messages: list[dict] | None = None,
) -> RuntimeInvocationContext:
    return RuntimeInvocationContext(
        run_id=run_id,
        session_id="session-solver",
        turn_id=1,
        workspace_id="workspace-solver",
        username="operator-solver",
        query=query,
        arguments={
            "selected_ontology_id": "ontology-solver",
            "selected_ontology_version": "2.1.0",
        },
        history_messages=history_messages or [],
    )


def _requirement() -> SolverRouteRequirement:
    return SolverRouteRequirement(
        operation_key="synthetic_route_v1",
        tool_name="solve_synthetic_route_aaaaaaaa",
    )


def test_native_runtime_binds_solver_route_from_current_request(monkeypatch):
    captured = {}

    def resolve(**kwargs):
        captured.update(kwargs)
        return _requirement()

    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        resolve,
    )
    context = _context(
        "run-route-binding",
        history_messages=[
            {
                "role": "user",
                "content": "evaluate the previously selected fixed route for overload",
            }
        ],
    )

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(context)

    assert captured == {
        "user_request": "solve synthetic route plan",
        "arguments": {
            "selected_ontology_id": "ontology-solver",
            "selected_ontology_version": "2.1.0",
        },
        "selected_ontology_id": "ontology-solver",
        "selected_ontology_version": "2.1.0",
        "workspace_id": "workspace-solver",
    }
    assert solver_route_requirement_from_state(state) == _requirement()
    assert solver_tools_suppressed_from_state(state) is False


def test_native_runtime_seeds_exact_preflight_products(monkeypatch):
    captured = {}
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        lambda **kwargs: captured.update(kwargs) or None,
    )
    snapshot = RunPreflightSnapshot(
        workspace_id="workspace-solver",
        workspace_config_generation=1,
        system_config_generation=2,
        ontology_id="ontology-solver",
        ontology_version="2.1.0",
        mutation_revision=3,
        ontology_manifest={
            "id": "ontology-solver",
            "version": "2.1.0",
            "ontology_dir": "C:/synthetic/ontology-solver",
        },
        root_payload={"operations_catalog": [], "solver_catalog": []},
        selected_ontology_context="<Selected-Ontology />",
        preload_records=(
            {
                "path": "ontologies/ontology-solver/index.md",
                "normalized_path": "ontologies/ontology-solver/index.md",
                "source": "preload",
                "turn_id": 0,
            },
        ),
        deterministic_candidate=False,
    )
    context = _context("run-preflight-products").model_copy(update={"preflight_snapshot": snapshot})

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(context)

    assert getitem(captured, "root_payload") is snapshot.root_payload
    assert state["_selected_ontology_context_cache"] == "<Selected-Ontology />"
    assert state["_preflight_selected_ontology_preload_records"] == (
        tuple(dict(record) for record in snapshot.preload_records)
    )
    assert state["_preflight_selected_ontology_preload_records"] is not snapshot.preload_records


def test_native_runtime_uses_private_frozen_preflight_model_config(monkeypatch):
    from agent.core.jiuwen_engine import _resolve_jiuwen_llm_config

    current_resolutions = []

    class CurrentRouter:
        agent_config = SimpleNamespace()

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get_config(self, purpose, profile_id=None, workspace_id=None):
            current_resolutions.append((purpose, profile_id, workspace_id))
            return SimpleNamespace(
                model="current-model",
                api_key=synthetic_secret("solver_async_runtime-credential-01"),
                max_tokens=128,
            )

    monkeypatch.setattr(
        "agent.core.jiuwen_engine.get_llm_router",
        lambda: CurrentRouter(),
    )
    observed = {}

    class Engine:
        def __init__(self, **_kwargs):
            pass

        async def arun(self, state, **_kwargs):
            resolved = _resolve_jiuwen_llm_config(
                state,
                caller="main",
                agent_config=SimpleNamespace(),
            )
            observed["model"] = resolved.model
            observed["api_key"] = resolved.api_key
            observed["state"] = state
            return {**state, "final_output": "frozen profile used"}

    snapshot = RunPreflightSnapshot(
        workspace_id="workspace-solver",
        workspace_config_generation=1,
        system_config_generation=2,
        model_profile={
            "id": "accepted-profile",
            "model_profile_ref": {"scope": "global", "id": "accepted-profile"},
            "provider": "openai_compatible",
            "base_url": "https://synthetic.example.net/v1",
            "model": "frozen-model",
            "api_key": synthetic_secret("solver_async_runtime-credential-02"),
            "timeout": 30,
            "max_tokens": 256,
            "temperature": 0.1,
        },
        deterministic_candidate=False,
    )
    context = _context("run-frozen-profile").model_copy(
        update={
            "arguments": None,
            "model_profile_id": "accepted-profile",
            "model_profile_ref": {"scope": "global", "id": "accepted-profile"},
            "preflight_snapshot": snapshot,
        }
    )

    async def exercise():
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=Engine,
        )
        return [event async for event in runtime.astream(context)]

    events = asyncio.run(exercise())

    assert events[-1].status == "completed"
    assert observed["model"] == "frozen-model"
    assert observed["api_key"] == synthetic_secret("solver_async_runtime-credential-02")
    assert current_resolutions == []
    assert "preflight_snapshot" not in observed["state"]
    assert synthetic_secret("solver_async_runtime-credential-02") not in repr(observed["state"])

    current = _resolve_jiuwen_llm_config(
        {
            "workspace_id": "workspace-solver",
            "user_context": {"model_profile_id": "accepted-profile"},
        },
        caller="main",
        agent_config=SimpleNamespace(),
    )
    assert current.model == "current-model"
    assert len(current_resolutions) == 1


def test_preflight_preload_records_skip_second_package_read(monkeypatch):
    from agent.core.shared.ontology_context import (
        hydrate_selected_ontology_read_documents,
    )

    monkeypatch.setattr(
        "agent.core.shared.ontology_context.build_selected_ontology_preload_records",
        lambda **_kwargs: (_ for _ in ()).throw(AssertionError("preflight records must be reused")),
    )
    monkeypatch.setattr(
        "agent.core.shared.ontology_context._stamp_preload_evidence_context",
        lambda *_args, **_kwargs: None,
    )
    path = "ontologies/ontology-solver/index.md"
    state = {
        "workspace_id": "workspace-solver",
        "current_turn_id": 7,
        "user_context": {
            "selected_ontology_id": "ontology-solver",
            "selected_ontology_version": "2.1.0",
        },
        "_preflight_selected_ontology_preload_records": (
            {
                "path": path,
                "normalized_path": path,
                "source": "preload",
                "turn_id": 0,
            },
        ),
    }

    hydrate_selected_ontology_read_documents(state)

    assert getitem(getitem(getitem(getitem(state, "read_documents"), "by_path"), path), "turn_id") == 7


def test_native_runtime_suppresses_solver_tools_for_current_evaluation(monkeypatch):
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        lambda **_kwargs: None,
    )
    context = _context(
        "run-route-suppression",
        query="evaluate this fixed route and check whether any unit is overloaded",
        history_messages=[
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "old-solver-call",
                        "type": "function",
                        "function": {
                            "name": "solve_synthetic_route_aaaaaaaa",
                            "arguments": "{}",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "content": "old solver result",
                "tool_call_id": "old-solver-call",
                "name": "solve_synthetic_route_aaaaaaaa",
            },
        ],
    )

    state = internal(JiuwenAsyncAgentRuntime).build_engine_state(context)

    assert solver_route_requirement_from_state(state) is None
    assert solver_tools_suppressed_from_state(state) is True


def test_native_runtime_fails_closed_without_authoritative_solver_result(monkeypatch):
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        lambda **_kwargs: _requirement(),
    )

    class Engine:
        def __init__(self, **_kwargs):
            pass

        async def arun(self, state, **_kwargs):
            return {**state, "final_output": "unverified model answer"}

    async def exercise():
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=Engine,
        )
        return [event async for event in runtime.astream(_context("run-guard-missing"))]

    events = asyncio.run(exercise())

    terminal = events[-1]
    assert terminal.event_type == "turn.finished"
    assert terminal.status == "failed"
    assert terminal.payload["error"]["error_code"] == "solver_authoritative_result_missing"


def test_native_runtime_forwards_and_accepts_authoritative_solver_result(monkeypatch):
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        lambda **_kwargs: _requirement(),
    )

    class Engine:
        def __init__(self, **_kwargs):
            pass

        async def arun(self, state, **kwargs):
            kwargs["protocol_event_observer"](
                "solver.result.authoritative",
                {
                    "tool_name": _requirement().tool_name,
                    "status": "completed",
                    "authoritative": True,
                    "summary": {"text": "verified route"},
                },
            )
            return {**state, "final_output": "verified route"}

    async def exercise():
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=Engine,
        )
        return [event async for event in runtime.astream(_context("run-guard-success"))]

    events = asyncio.run(exercise())

    authoritative = [event for event in events if event.event_type == "solver.result.authoritative"]
    assert len(authoritative) == 1
    assert authoritative[0].source["component"] == "solver"
    assert authoritative[0].payload["summary"]["text"] == "verified route"
    assert events[-1].event_type == "turn.finished"
    assert events[-1].status == "completed"


def test_native_runtime_structured_solver_arguments_bypass_model(monkeypatch):
    direct_requirement = SolverRouteRequirement(
        operation_key=_requirement().operation_key,
        tool_name=_requirement().tool_name,
        direct_arguments={"item_count": 2},
    )
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        lambda **_kwargs: direct_requirement,
    )
    executed = []

    async def execute(tool_calls, tool_context):
        executed.append((tool_calls, tool_context))
        tool_context.protocol_event_observer(
            "solver.result.authoritative",
            {
                "tool_name": direct_requirement.tool_name,
                "status": "completed",
                "authoritative": True,
                "summary": {"text": "verified direct route"},
            },
        )
        call = tool_calls[0]
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": '{"status":"completed","authoritative":true}',
                    "tool_call_id": call.id,
                    "name": call.name,
                }
            ],
            state_updates={
                "final_output": "verified direct route",
                "results": {"solver": {"authoritative": True}},
            },
            terminal=True,
        )

    monkeypatch.setattr("agent.tools.executor.aexecute_tool_batch", execute)

    def fail_engine(**_kwargs):
        raise AssertionError("structured solver input must bypass the model")

    async def exercise():
        runtime = JiuwenAsyncAgentRuntime(
            agent_config=SimpleNamespace(),
            engine_factory=fail_engine,
        )
        context = _context("run-direct-solver").model_copy(
            update={
                "arguments": {
                    "selected_ontology_id": "ontology-solver",
                    "selected_ontology_version": "2.1.0",
                    "item_count": 2,
                }
            }
        )
        return [event async for event in runtime.astream(context)]

    events = asyncio.run(exercise())

    assert len(executed) == 1
    calls, tool_context = executed[0]
    assert calls[0].name == direct_requirement.tool_name
    assert calls[0].arguments == {"item_count": 2}
    assert tool_context.caller == "main"
    assert tool_context.workspace_id == "workspace-solver"
    assert events[-1].status == "completed"
    assert events[-1].payload["content"] == "verified direct route"
    history = events[-1].payload["history_messages"]
    assert [message["role"] for message in history] == [
        "user",
        "assistant",
        "tool",
        "assistant",
    ]


def test_solver_protocol_events_keep_existing_public_event_names():
    async def exercise():
        bridge = JiuwenRuntimeEventBridge(_context("run-solver-events"))
        cases = (
            ("solver.input.awaiting_confirmation", "awaiting_input"),
            ("solver.progress", "running"),
            ("solver.verification.completed", "completed"),
            ("solver.verification.rejected", "failed"),
            ("solver.result.authoritative", "completed"),
        )
        for event_type, _expected_status in cases:
            bridge.protocol_event(
                event_type,
                {
                    "tool_name": _requirement().tool_name,
                    "phase": "verify",
                    "authoritative": event_type == "solver.result.authoritative",
                },
            )
        return cases, [await asyncio.wait_for(bridge.get(), timeout=0.1) for _event_type, _expected_status in cases]

    cases, events = asyncio.run(exercise())

    assert [event.event_type for event in events] == [item[0] for item in cases]
    assert [event.status for event in events] == [item[1] for item in cases]
    assert all(event.source["component"] == "solver" for event in events)


def test_tool_adapter_awaits_snapshot_before_solver_dispatch():
    order = []
    state = {"run_id": "run-snapshot-order", "messages": [], "results": {}}

    async def snapshot_callback(_snapshot):
        order.append("snapshot-start")
        await asyncio.sleep(0)
        order.append("snapshot-done")

    async def executor(tool_calls, _context):
        order.append("execute")
        call = tool_calls[0]
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": "done",
                    "tool_call_id": call.id,
                    "name": call.name,
                }
            ]
        )

    async def exercise():
        adapter = CurrentRuntimeToolAdapter(
            tool_name=_requirement().tool_name,
            state=state,
            context_factory=lambda: SimpleNamespace(),
            tool_batch_executor=executor,
            state_snapshot_callback=snapshot_callback,
        )
        return await adapter.invoke({"item_count": 2})

    assert asyncio.run(exercise()) == "done"
    assert order[:3] == ["snapshot-start", "snapshot-done", "execute"]


def test_direct_solver_persists_checkpoint_before_tool_submission(
    monkeypatch,
    tmp_path,
):
    requirement = SolverRouteRequirement(
        operation_key=_requirement().operation_key,
        tool_name=_requirement().tool_name,
        direct_arguments={"item_count": 2},
    )
    monkeypatch.setattr(
        "agent.core.shared.solver_route_guard.resolve_solver_route_requirement",
        lambda **_kwargs: requirement,
    )
    chat_store = ChatStore(str(tmp_path / "chat"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    context = _context("run-direct-checkpoint").model_copy(
        update={
            "arguments": {
                "selected_ontology_id": "ontology-solver",
                "selected_ontology_version": "2.1.0",
                "item_count": 2,
            }
        }
    )
    chat_store.create_session(session_id=context.session_id)
    execution_store.start_turn(
        session_id=context.session_id,
        turn_id=context.turn_id,
        run_id=context.run_id,
        user_request=context.query,
    )
    observed_checkpoints = []

    async def execute(tool_calls, tool_context):
        checkpoint = execution_store.get_waiting_solver_checkpoint(
            context.session_id,
            run_id=context.run_id,
        )
        observed_checkpoints.append(checkpoint)
        tool_context.protocol_event_observer(
            "solver.result.authoritative",
            {
                "tool_name": requirement.tool_name,
                "status": "completed",
                "authoritative": True,
                "summary": {"text": "verified after checkpoint"},
            },
        )
        call = tool_calls[0]
        return ToolBatchResult(
            messages=[
                {
                    "role": "tool",
                    "content": '{"status":"completed","authoritative":true}',
                    "tool_call_id": call.id,
                    "name": call.name,
                }
            ],
            state_updates={"final_output": "verified after checkpoint"},
            terminal=True,
        )

    monkeypatch.setattr("agent.tools.executor.aexecute_tool_batch", execute)

    class Orchestrator:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def _get_stores(self, username, workspace_id):
            assert (username, workspace_id) == (
                context.username,
                context.workspace_id,
            )
            return chat_store, execution_store

    class Sink:
        async def emit(self, _event):
            return None

    def fail_engine(**_kwargs):
        raise AssertionError("direct solver must bypass model")

    runtime = JiuwenAsyncAgentRuntime(
        agent_config=SimpleNamespace(),
        engine_factory=fail_engine,
    )
    service = AsyncExecutionService(
        runtime,
        session_adapter=OrchestratorSessionAdapter(lambda: Orchestrator()),
    )

    result = asyncio.run(service.execute(context, Sink()))

    assert result.error is None
    assert result.status == "completed"
    assert observed_checkpoints[0]["checkpoint"]["stage"] == "waiting_solver"
    assert observed_checkpoints[0]["checkpoint"]["tool_call"]["arguments"] == {"item_count": 2}
    assert (
        execution_store.get_waiting_solver_checkpoint(
            context.session_id,
            run_id=context.run_id,
        )
        is None
    )


def test_execution_service_resumes_durable_solver_result_without_model(
    tmp_path,
):
    chat_store = ChatStore(str(tmp_path / "chat"))
    execution_store = ExecutionStore(str(tmp_path / "executions"))
    context = _context("run-solver-resume")
    chat_store.create_session(session_id=context.session_id)
    execution_store.start_turn(
        session_id=context.session_id,
        turn_id=context.turn_id,
        run_id=context.run_id,
        user_request=context.query,
    )
    checkpoint = {
        "schema_version": "agent.solver-checkpoint.v1",
        "stage": "waiting_solver",
        "tool_call": {
            "id": "solver-call-resume",
            "name": _requirement().tool_name,
            "arguments": {"item_count": 2},
        },
        "state": {
            "run_id": context.run_id,
            "session_id": context.session_id,
            "current_turn_id": context.turn_id,
            "workspace_id": context.workspace_id,
            "user_id": context.username,
            "user_request": context.query,
            "user_context": {
                "selected_ontology_id": "ontology-solver",
                "selected_ontology_version": "2.1.0",
            },
            "messages": [],
            "read_documents": {},
            "results": {},
            "completed_ids": [],
            "failed_ids": [],
            "errors": [],
            "final_output": None,
        },
    }
    execution_store.save_waiting_solver_checkpoint(
        context.session_id,
        run_id=context.run_id,
        turn_id=context.turn_id,
        checkpoint=checkpoint,
    )
    execution_store.put_solver_tool_result(
        context.session_id,
        run_id=context.run_id,
        turn_id=context.turn_id,
        tool_call_id="solver-call-resume",
        result_digest="sha256:" + "a" * 64,
        result={
            "status": "completed",
            "authoritative": True,
            "summary": {"text": "verified resumed route"},
        },
    )

    class Orchestrator:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def _get_stores(self, username, workspace_id):
            assert (username, workspace_id) == (
                context.username,
                context.workspace_id,
            )
            return chat_store, execution_store

    class NoModelRuntime:
        async def astream(self, _context):
            for event in ():
                yield event
            raise AssertionError("durable solver resume must not invoke the model")

    class Sink:
        def __init__(self):
            self.events = []

        async def emit(self, event):
            self.events.append(event)

    adapter = OrchestratorSessionAdapter(lambda: Orchestrator())
    service = AsyncExecutionService(NoModelRuntime(), session_adapter=adapter)
    sink = Sink()

    result = asyncio.run(service.execute(context, sink))

    assert result.status == "completed"
    assert result.content == "verified resumed route"
    assert [event.event_type for event in sink.events] == [
        "solver.result.authoritative",
        "turn.finished",
    ]
    assert (
        execution_store.get_waiting_solver_checkpoint(
            context.session_id,
            run_id=context.run_id,
        )
        is None
    )
