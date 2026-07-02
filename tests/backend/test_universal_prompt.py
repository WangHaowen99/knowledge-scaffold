"""Tests for consolidated universal prompt policy."""

import inspect
from pathlib import Path

import pytest

from agentic.prompts.universal import build_universal_system_prompt


def test_universal_prompt_contains_top_level_sections_and_language_policy():
    prompt = build_universal_system_prompt()
    # Orthogonal top-level sections (no priority/conflict-resolution clause).
    assert "## Domain Routing" in prompt
    assert "## Ontology Execution Flow" in prompt
    assert "## Tool Usage Rules" in prompt
    assert "## Response Style" in prompt
    assert "Instruction Priority" not in prompt
    assert "## Prohibitions" not in prompt
    assert "Respond in the same language as the user's latest request." in prompt


def test_universal_prompt_has_no_router_artifacts():
    prompt = build_universal_system_prompt()

    assert "<routing_reason>" not in prompt
    assert "direct or simple or complex" not in prompt.lower()
    assert "route\": \"direct" not in prompt.lower()


def test_universal_prompt_omits_planning_tool_usage_guidance():
    prompt = build_universal_system_prompt()
    assert "### planning_tool" not in prompt
    assert "planning_tool(action=\"create\")" not in prompt
    assert "active plan task ID" not in prompt

    prompt_repeat = build_universal_system_prompt()
    assert prompt_repeat == prompt


def test_subagent_prompt_omits_planning_tool_guidance():
    subagent_source = (
        Path(__file__).resolve().parents[3]
        / "runtime"
        / "agent"
        / "agentic"
        / "sub_agents"
        / "base.py"
    ).read_text(encoding="utf-8")

    assert "You CANNOT call" not in subagent_source


def test_subagent_prompt_inherits_main_parallel_batching_rules_without_tool_rule_duplication():
    subagent_source = (
        Path(__file__).resolve().parents[3]
        / "runtime"
        / "agent"
        / "agentic"
        / "sub_agents"
        / "base.py"
    ).read_text(encoding="utf-8")

    assert "All tool calls in one response run in parallel" not in subagent_source
    assert "Batching rules:" in subagent_source
    assert "what is logically one fan-out" in subagent_source
    assert "If task B needs task A's output" in subagent_source
    assert "## Tool Usage Rules" not in subagent_source
    assert "### business_data_search" not in subagent_source
    assert "### execute_script" not in subagent_source


def test_universal_prompt_prefers_spawn_sub_agent_for_independent_subtasks():
    prompt = build_universal_system_prompt()
    assert "spawn_sub_agent" in prompt
    assert "(b) **Parallel fan-out**" in prompt
    assert "ALWAYS batch all parallel tasks into ONE `spawn_sub_agent` call" in prompt
    assert "If task B needs task A's output" in prompt
    assert "Context payload requirements" not in prompt
    assert "### spawn_sub_agent" not in prompt


def test_universal_prompt_keeps_tool_usage_rules_general():
    prompt = build_universal_system_prompt()
    assert "Follow each tool's schema description" in prompt
    assert "When multiple tool calls have known inputs" in prompt
    assert "batch them in the same response" in prompt
    assert "dependency phases" not in prompt
    assert "If a required input for the chosen tool or execution path is missing" in prompt
    assert "call `memory` with the content to preserve" in prompt
    assert "entity keys" not in prompt
    assert "### ontology_context" not in prompt
    assert "### business_data_search" not in prompt
    assert "### execute_script" not in prompt


def test_universal_prompt_does_not_include_web_search_tool_specific_rules():
    signature = inspect.signature(build_universal_system_prompt)
    prompt = build_universal_system_prompt()

    assert "include_web_search" not in signature.parameters
    assert "`web_search`" not in prompt
    assert "public internet information" not in prompt


def test_universal_prompt_includes_skill_rules():
    prompt = build_universal_system_prompt(
        skills_section=(
            "## Skills\n"
            "Available skills are listed in the \"Available-Skills\" section below.\n"
            "Skills are reusable external helpers, not enterprise domain ontologies.\n"
            "Use skill(skill_name='<name>', resource='root') when needed."
        ),
    )

    assert "## Skills" in prompt
    assert "Available skills are listed in the \"Available-Skills\" section below" in prompt
    assert "reusable external helpers, not enterprise domain ontologies" in prompt
    assert "skill(skill_name='<name>', resource='root')" in prompt


def test_universal_prompt_prefers_script_backed_execution():
    prompt = build_universal_system_prompt()
    # Script-backed routing is stated exactly once, in Step 4(a).
    assert "Script-backed (工具流程)" in prompt
    assert "script_catalog" in prompt
    assert "invoke `execute_script`" in prompt
    # Old duplicated phrasings are gone.
    assert "If `scripts-list.md` exists" not in prompt
    assert "execute_python_script" not in prompt


def test_universal_prompt_appends_memory_context_without_memory_tool_rules():
    prompt = build_universal_system_prompt(
        memory_context="## Relevant Long-Term Memory\n- [preference] 默认使用中文回答。",
    )

    assert "## Memory Tool Rules" not in prompt
    assert "you MUST call `store_memory`" not in prompt
    assert "Use `recall_memory`" not in prompt
    assert "## Relevant Long-Term Memory" in prompt


def test_universal_prompt_appends_ontologies_section():
    prompt = build_universal_system_prompt(
        ontologies_section="## Available Ontologies\n- test_ontology",
    )
    assert "## Available Ontologies\n- test_ontology" in prompt


def test_universal_prompt_uses_selected_ontology_override():
    prompt = build_universal_system_prompt()

    assert "<Selected-Ontology>" in prompt
    assert "If the user has explicitly selected an ontology, use that ontology." in prompt
    assert "matching the user's intent against each ontology id and description" in prompt
    assert "Use the injected `<Selected-Ontology>` block" in prompt
    assert "call `ontology_context(resource=\"root\")` once" in prompt
    assert "Preloaded Selected Ontology Context" not in prompt


def test_universal_prompt_keeps_ontology_ids_out_of_skill_tool():
    prompt = build_universal_system_prompt()

    assert "Use `skill` only for names listed in `<Available-Skills>`" in prompt
    assert "use ontology ids only with ontology tools" in prompt
    assert "Ontology ids from `<Available-Ontologies>` or `<Selected-Ontology>`" not in prompt


def test_universal_prompt_matches_operation_by_result_scope():
    prompt = build_universal_system_prompt()

    assert "choose by result scope" in prompt
    assert "某批次上" in prompt
    assert "F000 上" in prompt
    assert "global inventory operation" in prompt
    assert "instance-level cost question" in prompt


def test_universal_prompt_uses_exact_operation_attributes_for_calculation():
    prompt = build_universal_system_prompt()

    assert "use exactly that attribute" in prompt
    assert "单位成本（物料成本）" in prompt
    assert "not `物料成本 + 制造成本`" in prompt


def test_universal_prompt_requires_complete_collection_for_range_calculation():
    prompt = build_universal_system_prompt()

    assert "query the complete collection before calculating" in prompt
    assert "从起始批次到当前切换批次" in prompt
    assert "累计" in prompt
    assert "only the start item" in prompt


def test_universal_prompt_no_longer_mentions_removed_hitl_tool():
    prompt = build_universal_system_prompt()
    removed_tool = "ask" + "_user"

    assert removed_tool not in prompt


def test_ontology_optional_doc_no_longer_mentions_removed_hitl_tool():
    ontology_doc_path = (
        Path(__file__).resolve().parents[3]
        / ".onto-runtime"
        / "ontologies"
        / "example-domain-operations-manual"
        / "ONTOLOGY.md"
    )

    if not ontology_doc_path.exists():
        pytest.skip("Optional ontology doc fixture is not present")

    content = ontology_doc_path.read_text(encoding="utf-8")
    removed_tool = "ask" + "_user"
    assert removed_tool not in content


def test_ontology_doc_and_scripts_list_document_script_first_tool_flows():
    ontology_doc_path = (
        Path(__file__).resolve().parents[3]
        / ".onto-runtime"
        / "ontologies"
        / "example-domain-operations-manual"
        / "ONTOLOGY.md"
    )
    scripts_list_path = (
        Path(__file__).resolve().parents[3]
        / ".onto-runtime"
        / "ontologies"
        / "example-domain-operations-manual"
        / "scripts-list.md"
    )

    if not ontology_doc_path.exists() or not scripts_list_path.exists():
        pytest.skip("Optional ontology doc fixture is not present")

    ontology_doc_content = ontology_doc_path.read_text(encoding="utf-8")
    scripts_list_content = scripts_list_path.read_text(encoding="utf-8")
    assert "脚本优先规则" in ontology_doc_content
    assert "scripts-list.md" in ontology_doc_content
    assert "优先调用 `execute_script`" in ontology_doc_content
    assert "ontologies/example-domain-operations-manual/scripts/get_in_progress_flights.py" not in ontology_doc_content
    assert "scripts/get_in_progress_flights.py" in scripts_list_content
