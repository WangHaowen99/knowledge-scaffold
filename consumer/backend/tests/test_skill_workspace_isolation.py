# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Regression tests for workspace-scoped skill discovery and execution."""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from agent.tools.base import ToolContext
from agent.tools.skill import SkillTool
from utils.skills import SkillLoader, get_skill_registry
from workspace.workspaces import (
    create_workspace,
    delete_workspace,
    get_workspace_paths,
)
from internal_access import internal


def _write_skill(skill_root, name: str, description: str) -> None:
    skill_dir = skill_root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: {}\ndescription: {}\n---\n\n# {}\n".format(
            name,
            description,
            name,
        ),
        encoding="utf-8",
    )


def _tool_context(workspace_id: str) -> ToolContext:
    return ToolContext(
        memory_manager=None,
        messages=[],
        run_id="run-skills",
        session_id="session-skills",
        turn_id=1,
        # SkillTool does not inspect the agent config; a sentinel keeps this
        # unit test independent from the application's full config defaults.
        agent_config=None,
        caller="main",
        workspace_id=workspace_id,
        project_root=None,
        event_emitter=None,
        parent_tool_call_id=None,
        subagent_manager=None,
        subagent_lineage=None,
        protocol_event_observer=None,
    )


def test_skill_registry_isolated_by_workspace_with_system_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a", set_as_default=True)
    create_workspace("Workspace B", workspace_id="workspace-b")

    paths_a = get_workspace_paths("workspace-a")
    paths_b = get_workspace_paths("workspace-b")
    system_root = tmp_path / ".onto-runtime" / "system" / "skills"
    _write_skill(paths_a.skills_dir, "only-a", "Skill from A")
    _write_skill(paths_b.skills_dir, "only-b", "Skill from B")
    _write_skill(system_root, "system-skill", "Shared system skill")

    registry_a = get_skill_registry(workspace_id="workspace-a")
    registry_b = get_skill_registry(workspace_id="workspace-b")

    assert set(registry_a.list_skills()) == {"only-a", "system-skill"}
    assert set(registry_b.list_skills()) == {"only-b", "system-skill"}
    assert registry_a is not registry_b


def test_skill_registry_refreshes_when_workspace_skills_change(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a", set_as_default=True)
    paths = get_workspace_paths("workspace-a")

    registry = get_skill_registry(workspace_id="workspace-a")
    assert registry.list_skills() == []

    _write_skill(paths.skills_dir, "added-later", "Added after registry creation")

    refreshed = get_skill_registry(workspace_id="workspace-a")
    assert refreshed is registry
    assert refreshed.get_skill("added-later") is not None


def test_deleted_and_recreated_workspace_does_not_reuse_cached_skill_snapshot(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Keeper", workspace_id="keeper", set_as_default=True)
    create_workspace("Workspace A", workspace_id="workspace-a")
    paths = get_workspace_paths("workspace-a")
    _write_skill(paths.skills_dir, "old-secret", "Secret from deleted workspace")

    registry = get_skill_registry(workspace_id="workspace-a")
    assert registry.get_skill("old-secret") is not None

    delete_workspace("workspace-a")
    create_workspace("Workspace A Recreated", workspace_id="workspace-a")

    recreated = get_skill_registry(workspace_id="workspace-a")
    assert recreated is not registry
    assert recreated.get_skill("old-secret") is None


def test_workspace_skill_overrides_same_named_system_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a", set_as_default=True)
    paths = get_workspace_paths("workspace-a")
    system_root = tmp_path / ".onto-runtime" / "system" / "skills"
    _write_skill(paths.skills_dir, "shared", "Workspace-specific skill")
    _write_skill(system_root, "shared", "System fallback skill")

    skill = get_skill_registry(workspace_id="workspace-a").get_skill("shared")

    assert skill is not None
    assert skill.description == "Workspace-specific skill"


def test_skill_registry_refresh_publishes_one_complete_snapshot(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a", set_as_default=True)
    paths = get_workspace_paths("workspace-a")
    _write_skill(paths.skills_dir, "old-skill", "Old skill")
    registry = get_skill_registry(workspace_id="workspace-a")
    assert registry.list_skills() == ["old-skill"]

    _write_skill(paths.skills_dir, "new-skill", "New skill")
    started = threading.Event()
    release = threading.Event()
    original_load_skill = SkillLoader.load_skill

    def blocking_load_skill(skill_path):
        if skill_path.name == "new-skill":
            started.set()
            assert release.wait(timeout=5)
        return original_load_skill(skill_path)

    monkeypatch.setattr(SkillLoader, "load_skill", staticmethod(blocking_load_skill))

    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(registry.refresh)
        assert started.wait(timeout=5)
        assert registry.list_skills() == ["old-skill"]
        release.set()
        future.result(timeout=5)

    assert set(registry.list_skills()) == {"old-skill", "new-skill"}


def test_skill_tool_uses_context_workspace_and_rejects_other_workspace_skill(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a")
    create_workspace("Workspace B", workspace_id="workspace-b", set_as_default=True)

    paths_a = get_workspace_paths("workspace-a")
    paths_b = get_workspace_paths("workspace-b")
    _write_skill(paths_a.skills_dir, "only-a", "Skill from A")
    _write_skill(paths_b.skills_dir, "only-b", "Skill from B")

    outcome = SkillTool().execute(
        args={"skill_name": "only-b", "resource": "root"},
        context=_tool_context("workspace-a"),
    )

    assert outcome.content is not None
    assert outcome.content.startswith("Error: Skill 'only-b' not found")
    assert (
        json.loads(
            SkillTool()
            .execute(
                args={"skill_name": "only-a", "resource": "root"},
                context=_tool_context("workspace-a"),
            )
            .content
        )["skill"]["name"]
        == "only-a"
    )


def test_subagent_prompt_uses_explicit_workspace_skill_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a")
    create_workspace("Workspace B", workspace_id="workspace-b", set_as_default=True)

    _write_skill(get_workspace_paths("workspace-a").skills_dir, "only-a", "Skill from A")
    _write_skill(get_workspace_paths("workspace-b").skills_dir, "only-b", "Skill from B")

    from agent.sub_agents.base import SubAgent

    prompt = internal(SubAgent()).build_system_prompt(workspace_id="workspace-a")

    assert "only-a" in prompt
    assert "only-b" not in prompt


def test_tool_schema_uses_explicit_workspace_skill_registry(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Workspace A", workspace_id="workspace-a")
    create_workspace("Workspace B", workspace_id="workspace-b", set_as_default=True)

    # Only the default workspace has a skill.  A schema request for A must not
    # enable the skill tool merely because B happens to be the process default.
    _write_skill(get_workspace_paths("workspace-b").skills_dir, "only-b", "Skill from B")

    from agent.tools.registry import get_tools_schema

    schema_names = {schema["function"]["name"] for schema in get_tools_schema("main", workspace_id="workspace-a")}

    assert "skill" not in schema_names
