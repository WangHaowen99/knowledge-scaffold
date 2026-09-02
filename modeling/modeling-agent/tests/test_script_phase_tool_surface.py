from modeling_agent.tools import (
    DOMAIN_PHASE_TOOLS,
    DOMAIN_READ_TOOLS,
    core_tool_specs_for_phase,
)


def test_validation_phase_can_read_operation_snapshot_without_domain_write_tools() -> None:
    validation = {spec.name for spec in core_tool_specs_for_phase("validation")}

    assert {
        "list_domain_resources", "get_domain_resource", "define_script_contract",
        "get_script_edit_context", "stage_script_patch", "run_script_test",
    } <= validation
    assert DOMAIN_READ_TOOLS <= validation
    assert not (DOMAIN_PHASE_TOOLS - DOMAIN_READ_TOOLS) & validation
