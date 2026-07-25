"""Tests for spawn_sub_agent orchestration behavior."""

from __future__ import annotations

import json
import threading
import time
from types import SimpleNamespace

import pytest

from agent.exceptions import WorkflowCancelledError
from agent.tools.base import ToolContext
from agent.tools.spawn_sub_agent import SpawnSubAgentTool
from config import AgentConfig
from workspace.workspaces import create_workspace, get_workspace_paths


def _agent_config(**overrides):
    config = {
        "llm_connection_max_retries": 3,
        "main_agent_max_iterations": 50,
        "sub_agent_max_iterations": 5,
        "sub_agent_max_concurrency": 4,
        "sub_agent_max_depth": 2,
        "parallel_default_max_workers": 10,
        "context_max_rounds": 10,
    }
    config.update(overrides)
    return AgentConfig(**config)


def _make_context(**overrides) -> ToolContext:
    context = ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-subagents",
        session_id="sess-subagents",
        turn_id=1,
        agent_config=_agent_config(),
        caller="main",
        project_root=None,
        event_emitter=None,
        parent_tool_call_id=None,
        subagent_manager=None,
        subagent_lineage=None,
        protocol_event_observer=None,
    )
    for key, value in overrides.items():
        setattr(context, key, value)
    return context


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "spawn-subagent"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Spawn SubAgent Demo", workspace_id=workspace_id, set_as_default=True)
    return get_workspace_paths(workspace_id)


class _FakeSubAgent:
    def __init__(self, handler):
        self._handler = handler

    def execute(self, task, context=None, **kwargs):
        return self._handler(task, context or {}, **kwargs)


def _patch_subagent(monkeypatch, handler):
    from agent.sub_agents import manager as manager_module

    def factory():
        return _FakeSubAgent(handler)

    monkeypatch.setattr(manager_module, "SubAgent", factory)


def test_spawn_sub_agent_batch_returns_group_payload(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()

    def handler(task, context, **kwargs):
        del context, kwargs
        return {
            "output": "done: {}".format(task),
            "_success": True,
            "_key_steps": ["dispatch started", "produced final result"],
        }

    _patch_subagent(monkeypatch, handler)

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={
            "tasks": [
                {"id": "t1", "task": "collect facts", "context": {"scope": "demo"}},
            ]
        },
        context=_make_context(),
        tool_call_id="call_spawn_1",
    )

    payload = json.loads(outcome.content)
    assert payload["status"] == "completed"
    assert payload["summary"]
    assert len(payload["tasks"]) == 1
    assert payload["tasks"][0]["id"] == "t1"
    assert payload["tasks"][0]["status"] == "completed"
    assert payload["tasks"][0]["result"]["output"] == "done: collect facts"
    assert payload["tasks"][0]["key_steps"] == ["dispatch started", "produced final result"]


def test_spawn_sub_agent_forwards_parent_agent_mode(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()
    captured_kwargs = {}

    def handler(task, context, **kwargs):
        del task, context
        captured_kwargs.update(kwargs)
        return {
            "output": "ok",
            "_success": True,
            "_key_steps": ["dispatch started", "produced final result"],
        }

    _patch_subagent(monkeypatch, handler)

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={
            "tasks": [
                {"id": "t1", "task": "delegate nested fan-out"},
            ]
        },
        context=_make_context(
            agent_mode="multi_agent",
            model_profile_id="accurate",
        ),
        tool_call_id="call_spawn_mode",
    )

    payload = json.loads(outcome.content)
    assert payload["status"] == "completed"
    assert captured_kwargs["parent_agent_mode"] == "multi_agent"
    assert captured_kwargs["parent_model_profile_id"] == "accurate"


def test_spawn_sub_agent_forwards_parent_selected_ontology(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()
    captured_kwargs = {}

    def handler(task, context, **kwargs):
        del task, context
        captured_kwargs.update(kwargs)
        return {
            "output": "ok",
            "_success": True,
            "_key_steps": ["dispatch started", "produced final result"],
        }

    _patch_subagent(monkeypatch, handler)

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={"tasks": [{"id": "t1", "task": "use selected ontology"}]},
        context=_make_context(selected_ontology_id="parent-ontology"),
        tool_call_id="call_spawn_selected_ontology",
    )

    payload = json.loads(outcome.content)
    assert payload["status"] == "completed"
    assert captured_kwargs["parent_selected_ontology_id"] == "parent-ontology"


def test_sub_agent_uses_stable_loop_engine_facade(monkeypatch):
    from agent.sub_agents.base import SubAgent

    agent_config = _agent_config()
    calls = {}

    class FakeLoopEngine:
        def __init__(self, **kwargs):
            calls["engine_kwargs"] = kwargs

        def run(self, state, **kwargs):
            calls["state"] = state
            calls["run_kwargs"] = kwargs
            return {
                **state,
                "final_output": "child result",
                "messages": [{"role": "assistant", "content": "child result"}],
            }

    monkeypatch.setattr("agent.core.engine.AgentLoopEngine", FakeLoopEngine)
    monkeypatch.setattr(
        "agent.sub_agents.base.get_llm_router",
        lambda: SimpleNamespace(agent_config=agent_config),
    )
    monkeypatch.setattr("agent.sub_agents.base.build_ontologies_section", lambda **_kwargs: "")
    monkeypatch.setattr("agent.sub_agents.base.build_skills_section", lambda **_kwargs: "")

    result = SubAgent().execute(
        "child task",
        {"ontology_id": "demo"},
        run_id="run-child",
        session_id="sess-child",
        parent_agent_mode="thinking",
        parent_model_profile_id="accurate",
    )

    assert result["_success"] is True
    assert result["output"] == "child result"
    assert calls["engine_kwargs"] == {"agent_config": agent_config}
    assert calls["run_kwargs"]["caller"] == "subagent"
    assert calls["state"]["subagent_id"].startswith("sub_")
    assert calls["state"]["user_context"] == {
        "agent_mode": "thinking",
        "model_profile_id": "accurate",
    }


def test_sub_agent_inherits_parent_selected_ontology_for_context_prompt_and_schema(monkeypatch):
    from agent.core.shared.tool_context import build_tool_context
    from agent.sub_agents.base import SubAgent
    from agent.tools.registry import get_tools_schema

    calls = {}

    def fake_build_ontologies_section(*, selected_ontology_id=None, workspace_id=None):
        calls["prompt_scope"] = (selected_ontology_id, workspace_id)
        return "selected ontology prompt"

    def fake_execute_script_schema(
        selected_ontology_id=None,
        *,
        selected_ontology_version=None,
        workspace_id=None,
    ):
        calls["schema_scope"] = (selected_ontology_id, selected_ontology_version, workspace_id)
        return {
            "type": "function",
            "function": {
                "name": "execute_script",
                "description": "test",
                "parameters": {"type": "object", "properties": {}},
            },
        }

    class FakeLoopEngine:
        def __init__(self, **kwargs):
            del kwargs

        def run(self, state, **kwargs):
            del kwargs
            calls["state"] = state
            calls["user_prompt"] = state["user_request"]
            tool_context = build_tool_context(
                state,
                memory_manager=None,
                agent_config=_agent_config(),
                caller="subagent",
                agent_iteration=1,
                workspace_id=state["workspace_id"],
                project_root=None,
            )
            calls["tool_context"] = tool_context
            get_tools_schema(
                "subagent",
                selected_ontology_id=tool_context.selected_ontology_id,
                workspace_id=tool_context.workspace_id,
            )
            return {**state, "final_output": "done", "messages": []}

    monkeypatch.setattr("agent.core.engine.AgentLoopEngine", FakeLoopEngine)
    monkeypatch.setattr(
        "agent.sub_agents.base.get_llm_router",
        lambda: SimpleNamespace(agent_config=_agent_config()),
    )
    monkeypatch.setattr(
        "agent.sub_agents.base.build_ontologies_section",
        fake_build_ontologies_section,
    )
    monkeypatch.setattr("agent.sub_agents.base.build_skills_section", lambda **_kwargs: "")
    monkeypatch.setattr(
        "agent.tools.registry.build_execute_script_tool_schema",
        fake_execute_script_schema,
    )

    result = SubAgent().execute(
        "child task",
        {
            "workspace_id": "task-workspace",
            "selected_ontology_id": "task-ontology",
            "ontology_id": "operation-ontology",
        },
        run_id="run-child",
        session_id="sess-child",
        parent_workspace_id="parent-workspace",
        parent_selected_ontology_id="parent-ontology",
    )

    assert result["_success"] is True
    assert calls["state"]["workspace_id"] == "parent-workspace"
    assert calls["state"]["user_context"]["selected_ontology_id"] == "parent-ontology"
    assert calls["tool_context"].selected_ontology_id == "parent-ontology"
    assert calls["prompt_scope"] == ("parent-ontology", "parent-workspace")
    assert calls["schema_scope"] == ("parent-ontology", None, "parent-workspace")
    assert "parent-ontology" in calls["user_prompt"]
    assert "task-ontology" not in calls["user_prompt"]
    assert "operation-ontology" not in calls["user_prompt"]


def test_sub_agent_task_context_cannot_define_selected_ontology_scope(monkeypatch):
    from agent.sub_agents.base import SubAgent

    calls = {}

    class FakeLoopEngine:
        def __init__(self, **kwargs):
            del kwargs

        def run(self, state, **kwargs):
            del kwargs
            calls["state"] = state
            return {**state, "final_output": "done", "messages": []}

    monkeypatch.setattr("agent.core.engine.AgentLoopEngine", FakeLoopEngine)
    monkeypatch.setattr(
        "agent.sub_agents.base.get_llm_router",
        lambda: SimpleNamespace(agent_config=_agent_config()),
    )
    monkeypatch.setattr(
        "agent.sub_agents.base.build_ontologies_section",
        lambda **kwargs: calls.setdefault("prompt_scope", kwargs) and "",
    )
    monkeypatch.setattr("agent.sub_agents.base.build_skills_section", lambda **_kwargs: "")

    result = SubAgent().execute(
        "child task",
        {"selected_ontology_id": "task-controlled-ontology"},
        parent_workspace_id="parent-workspace",
        parent_selected_ontology_id=None,
    )

    assert result["_success"] is True
    assert calls["state"]["user_context"].get("selected_ontology_id") is None
    assert calls["prompt_scope"]["selected_ontology_id"] is None
    assert "task-controlled-ontology" not in calls["state"]["user_request"]


def test_sub_agent_parent_workspace_locks_scope_with_task_context_fallback(monkeypatch):
    from agent.sub_agents.base import SubAgent

    observed_workspaces = []
    states = []

    class FakeLoopEngine:
        def __init__(self, **kwargs):
            del kwargs

        def run(self, state, **kwargs):
            del kwargs
            states.append(state)
            return {**state, "final_output": "done", "messages": []}

    monkeypatch.setattr("agent.core.engine.AgentLoopEngine", FakeLoopEngine)
    monkeypatch.setattr(
        "agent.sub_agents.base.get_llm_router",
        lambda: SimpleNamespace(agent_config=_agent_config()),
    )
    monkeypatch.setattr(
        "agent.sub_agents.base.build_ontologies_section",
        lambda *, workspace_id=None, selected_ontology_id=None: observed_workspaces.append(workspace_id) or "",
    )
    monkeypatch.setattr("agent.sub_agents.base.build_skills_section", lambda **_kwargs: "")

    SubAgent().execute(
        "parent lock",
        {"workspace_id": "untrusted-task-workspace"},
        parent_workspace_id="parent-workspace",
    )
    SubAgent().execute(
        "task fallback",
        {"workspace_id": "task-workspace"},
    )

    assert observed_workspaces == ["parent-workspace", "task-workspace"]
    assert [state["workspace_id"] for state in states] == [
        "parent-workspace",
        "task-workspace",
    ]


def test_spawn_sub_agent_preserves_semantic_ontology_context(monkeypatch, tmp_path):
    from agent.sub_agents import manager as manager_module
    from ontology import ontologies as ontologies_module

    manager_module.clear_subagent_manager_registry()
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, workspace_id="spawnctx")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "scripts").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for spawn-sub-agent tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "index.md").write_text("- demo-tool\n- child-tool\n", encoding="utf-8")
    (ontology_dir / "objects" / "index.md").write_text("- demo-object\n", encoding="utf-8")
    (ontology_dir / "scripts" / "index.md").write_text("# scripts\n", encoding="utf-8")
    for key, operation_key, argument in (
        ("demo_tool", "demo-tool", "Demo编号"),
        ("child_tool", "child-tool", "Child编号"),
        ("shared_helper", "shared-helper", "Helper编号"),
    ):
        (ontology_dir / "scripts" / "{}.md".format(key)).write_text(
            "---\nresource: script\nkey: {}\nbound_operation: {}\nentrypoint: {}.py\n---\n\n"
            "| 参数 key | CLI 参数 | 类型 | 必填 | 说明 |\n"
            "| --- | --- | --- | --- | --- |\n"
            "| {} | --{}-id | string | 是 | {} |\n".format(
                key,
                operation_key,
                key,
                argument,
                key.split("_")[0],
                argument,
            ),
            encoding="utf-8",
        )
    (ontology_dir / "operations" / "demo-tool.md").write_text(
        "# 管理标签\n#工具流程\n",
        encoding="utf-8",
    )
    (ontology_dir / "operations" / "child-tool.md").write_text(
        "# 管理标签\n#工具流程\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    captured_context = {}

    def handler(task, context, **kwargs):
        del task, kwargs
        captured_context.update(context)
        return {
            "output": "ok",
            "_success": True,
            "_key_steps": ["dispatch started", "produced final result"],
        }

    _patch_subagent(monkeypatch, handler)

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={
            "tasks": [
                {
                    "id": "t1",
                    "task": "run demo tool",
                    "context": {
                        "ontology_id": "demo-ontology",
                        "operation_key": "demo-tool",
                        "sub_operation_keys": ["child-tool"],
                    },
                }
            ]
        },
        context=_make_context(project_root=str(workspace_paths.workspace_dir)),
        tool_call_id="call_spawn_enrich",
    )

    payload = json.loads(outcome.content)
    assert payload["status"] == "completed"
    assert captured_context == {
        "ontology_id": "demo-ontology",
        "operation_key": "demo-tool",
        "sub_operation_keys": ["child-tool"],
    }


def test_spawn_sub_agent_rejects_missing_tasks(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()
    _patch_subagent(monkeypatch, lambda task, context, **kwargs: {})

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={},
        context=_make_context(),
        tool_call_id="call_missing",
    )
    payload = json.loads(outcome.content)
    assert payload["status"] == "error"
    assert "tasks" in payload["summary"].lower()


def test_spawn_sub_agent_batch_respects_global_concurrency_cap(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()

    lock = threading.Lock()
    current_running = 0
    max_running = 0

    def handler(task, context, **kwargs):
        del context, kwargs
        nonlocal current_running, max_running
        with lock:
            current_running += 1
            max_running = max(max_running, current_running)
        time.sleep(0.03)
        with lock:
            current_running -= 1
        return {
            "output": task,
            "_success": True,
            "_key_steps": ["dispatch started", "produced final result"],
        }

    _patch_subagent(monkeypatch, handler)

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={
            "tasks": [
                {"id": "t1", "task": "one"},
                {"id": "t2", "task": "two"},
                {"id": "t3", "task": "three"},
                {"id": "t4", "task": "four"},
            ]
        },
        context=_make_context(agent_config=_agent_config(sub_agent_max_concurrency=2)),
        tool_call_id="call_spawn_2",
    )

    payload = json.loads(outcome.content)
    assert payload["status"] == "completed"
    assert len(payload["tasks"]) == 4
    assert max_running <= 2


def test_spawn_sub_agent_depth_limit_is_enforced(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()
    _patch_subagent(monkeypatch, lambda task, context, **kwargs: {})

    tool = SpawnSubAgentTool()
    outcome = tool.execute(
        args={"tasks": [{"id": "c1", "task": "child task"}]},
        context=_make_context(
            caller="subagent",
            agent_config=_agent_config(sub_agent_max_depth=2),
            subagent_lineage={
                "subagent_id": "sub_parent",
                "depth": 2,
                "root_tool_call_id": "call_root",
            },
        ),
        tool_call_id="call_spawn_nested",
    )

    payload = json.loads(outcome.content)
    assert payload["status"] == "error"
    assert "max depth" in payload["summary"].lower()


def test_spawn_sub_agent_nested_batch_reuses_parent_slot_without_deadlock(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()
    nested_config = _agent_config(sub_agent_max_concurrency=1)
    manager = manager_module.get_subagent_manager("run-subagents", nested_config)
    manager._semaphore.acquire()

    lock = threading.Lock()
    current_running = 0
    max_running = 0

    def handler(task, context, **kwargs):
        del task, context, kwargs
        nonlocal current_running, max_running
        with lock:
            current_running += 1
            max_running = max(max_running, current_running)
        time.sleep(0.02)
        with lock:
            current_running -= 1
        return {
            "output": "ok",
            "_success": True,
            "_key_steps": ["dispatch started", "produced final result"],
        }

    _patch_subagent(monkeypatch, handler)

    tool = SpawnSubAgentTool()
    try:
        outcome = tool.execute(
            args={
                "tasks": [
                    {"id": "nested-1", "task": "first"},
                    {"id": "nested-2", "task": "second"},
                ]
            },
            context=_make_context(
                caller="subagent",
                agent_config=nested_config,
                subagent_manager=manager,
                subagent_lineage={
                    "subagent_id": "sub_parent",
                    "depth": 1,
                    "root_tool_call_id": "call_root",
                },
            ),
            tool_call_id="call_spawn_nested_batch",
        )
    finally:
        manager._semaphore.release()

    payload = json.loads(outcome.content)
    assert payload["status"] == "completed"
    assert [item["id"] for item in payload["tasks"]] == ["nested-1", "nested-2"]
    assert max_running == 1


def test_spawn_sub_agent_propagates_workflow_cancellation(monkeypatch):
    from agent.sub_agents import manager as manager_module

    manager_module.clear_subagent_manager_registry()

    def handler(task, context, **kwargs):
        del task, context, kwargs
        raise WorkflowCancelledError(
            "Workflow cancelled by user",
            details={"run_id": "run-subagents"},
        )

    _patch_subagent(monkeypatch, handler)

    events = []
    tool = SpawnSubAgentTool()

    with pytest.raises(WorkflowCancelledError, match="Workflow cancelled by user"):
        tool.execute(
            args={"tasks": [{"id": "cancelled-task", "task": "stop now"}]},
            context=_make_context(
                event_emitter=lambda event_type, payload: events.append(
                    (event_type, payload)
                )
            ),
            tool_call_id="call_spawn_cancelled",
        )

    assert events[-1][0] == "subagent_group.finished"
    assert events[-1][1]["status"] == "cancelled"
