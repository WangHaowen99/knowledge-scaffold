from modeling_agent.tools import core_tool_specs_for_phase


def test_validation_phase_can_read_operation_snapshot_without_domain_write_tools() -> None:
    validation = {spec.name for spec in core_tool_specs_for_phase("validation")}

    assert {"list_domain_resources", "get_domain_resource"} <= validation
    assert {
        "get_script_edit_context", "stage_script_patch", "run_script_test",
    } <= validation
    assert not {
        "stage_change_set", "create_operation", "patch_operation",
        "delete_domain_resource", "stage_tabular_dataset",
    } & validation
