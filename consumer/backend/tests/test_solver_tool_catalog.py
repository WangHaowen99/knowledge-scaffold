from __future__ import annotations

import agent.tools.registry as tool_registry
import pytest
from agent.core.jiuwen.tools import get_jiuwen_tool_infos
from agent.tools.registry import validate_tool_arguments
from agent.tools.solver import (
    SolverToolCatalogError,
    build_solver_tool_specs,
)


def _capability(
    operation_key: str = "solve_synthetic_route",
    *,
    contract_digest: str = "sha256:" + "a" * 64,
    package_revision: str = "revision-synthetic-1",
):
    return {
        "package_id": "package-synthetic",
        "package_revision": package_revision,
        "operation_key": operation_key,
        "decision_model_id": "model-synthetic",
        "problem_family": "synthetic_family.v1",
        "input_schema": {
            "type": "object",
            "properties": {
                "item_count": {"type": "integer", "minimum": 1},
                "mode": {"type": "string", "enum": ["safe", "fast"]},
            },
            "required": ["item_count", "mode"],
            "additionalProperties": False,
        },
        "result_schema": {"type": "object"},
        "contract_digest": contract_digest,
        "validator_revision": "verifier-synthetic-1",
        "enabled": True,
    }


def test_exact_solver_card_has_ascii_bounded_name_and_operation_schema():
    (spec,) = build_solver_tool_specs([_capability()], provider_name_limit=64)

    assert spec.name == "solve_solve_synthetic_route_aaaaaaaa"
    assert spec.name.isascii()
    assert len(spec.name) <= 64
    parameters = spec.schema["function"]["parameters"]
    assert parameters == _capability()["input_schema"]
    assert "operation_key" not in parameters["properties"]
    assert "endpoint" not in str(spec.schema).lower()
    assert spec.binding.package_revision == "revision-synthetic-1"
    assert spec.binding.contract_digest == "sha256:" + "a" * 64
    description = spec.schema["function"]["description"]
    assert "Only properties listed in the schema's required array" in description
    assert "omit optional properties" in description


def test_exact_card_name_truncates_slug_without_losing_digest_identity():
    operation_key = "solve_" + "very_long_operation_name_" * 8

    (spec,) = build_solver_tool_specs([_capability(operation_key)], provider_name_limit=48)

    assert len(spec.name) <= 48
    assert spec.name.startswith("solve_")
    assert spec.name.endswith("_aaaaaaaa")
    assert spec.binding.operation_key == operation_key


def test_non_ascii_operation_uses_stable_ascii_fallback_slug():
    (first,) = build_solver_tool_specs([_capability("synthetic_\u6c42\u89e3")])
    (second,) = build_solver_tool_specs([_capability("synthetic_\u6c42\u89e3")])

    assert first.name == second.name
    assert first.name.isascii()
    assert first.name.endswith("_aaaaaaaa")


def test_card_name_collision_fails_closed_even_when_short_digest_collides():
    first = _capability(contract_digest="sha256:deadbeef" + "1" * 56)
    second = _capability(
        contract_digest="sha256:deadbeef" + "2" * 56,
        package_revision="revision-synthetic-2",
    )

    with pytest.raises(SolverToolCatalogError, match="collision"):
        build_solver_tool_specs([first, second])


def test_compatibility_card_uses_strict_discriminator_oneof_not_flat_union():
    first = _capability("solve_alpha")
    second = _capability(
        "solve_beta",
        contract_digest="sha256:" + "b" * 64,
    )
    second["input_schema"] = {
        "type": "object",
        "properties": {"threshold": {"type": "number"}},
        "required": ["threshold"],
        "additionalProperties": False,
    }

    (spec,) = build_solver_tool_specs([first, second], exact_cards=False)
    parameters = spec.schema["function"]["parameters"]

    assert spec.name == "solve"
    assert len(parameters["oneOf"]) == 2
    assert parameters["additionalProperties"] is False
    assert set(parameters["properties"]) == {"operation_key", "arguments"}
    assert (
        validate_tool_arguments(
            "solve",
            {"operation_key": "solve_alpha", "arguments": {"item_count": 2, "mode": "safe"}},
            tool_schema=spec.schema,
        )
        is None
    )
    error = validate_tool_arguments(
        "solve",
        {"operation_key": "solve_alpha", "arguments": {"threshold": 2}},
        tool_schema=spec.schema,
    )
    assert error and "oneOf" in error


def test_catalog_rejects_runtime_fields_and_non_strict_input_schema():
    runtime_capability = _capability()
    runtime_capability["input_schema"]["properties"]["endpoint"] = {"type": "string"}
    with pytest.raises(SolverToolCatalogError):
        build_solver_tool_specs([runtime_capability])

    loose_capability = _capability()
    loose_capability["input_schema"]["additionalProperties"] = True
    with pytest.raises(SolverToolCatalogError):
        build_solver_tool_specs([loose_capability])


def test_missing_revision_names_the_reason_instead_of_dropping_cards(caplog):
    from agent.tools.solver import get_solver_tool_specs_for_context

    with caplog.at_level("DEBUG", logger="agent.tools.solver"):
        assert get_solver_tool_specs_for_context(
            selected_ontology_id="package-synthetic",
            selected_ontology_version=None,
            workspace_id="workspace-synthetic",
        ) == ()

    assert any(
        "resolved no revision" in record.getMessage()
        and record.levelname == "WARNING"
        for record in caplog.records
    )


def test_static_registry_and_jiuwen_add_request_scoped_solver_cards(monkeypatch):
    (solver_spec,) = build_solver_tool_specs([_capability()])
    monkeypatch.setattr(
        tool_registry,
        "get_solver_tool_specs_for_context",
        lambda **kwargs: (
            (solver_spec,)
            if kwargs
            == {
                "selected_ontology_id": "package-synthetic",
                "selected_ontology_version": "revision-synthetic-1",
                "workspace_id": "workspace-synthetic",
            }
            else ()
        ),
    )

    schemas = tool_registry.get_tools_schema(
        "main",
        selected_ontology_id="package-synthetic",
        selected_ontology_version="revision-synthetic-1",
        workspace_id="workspace-synthetic",
    )
    infos = get_jiuwen_tool_infos(
        "main",
        selected_ontology_id="package-synthetic",
        selected_ontology_version="revision-synthetic-1",
        workspace_id="workspace-synthetic",
    )

    assert solver_spec.schema in schemas
    assert any(item["name"] == solver_spec.name for item in infos)
    assert (
        tool_registry.get_tool_spec(
            solver_spec.name,
            selected_ontology_id="package-synthetic",
            selected_ontology_version="revision-synthetic-1",
            workspace_id="workspace-synthetic",
        )
        is solver_spec
    )
    assert tool_registry.get_tool_spec(solver_spec.name) is None
