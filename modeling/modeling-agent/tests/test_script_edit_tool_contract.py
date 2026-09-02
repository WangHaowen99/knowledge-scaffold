from modeling_agent.tools import CORE_TOOL_SPECS


def test_read_file_redirects_managed_python_to_symbol_context() -> None:
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "read_file")

    assert "scripts/*.py" in spec.description
    assert "get_script_edit_context" in spec.description


def test_domain_patch_schema_distinguishes_evidence_token_from_value() -> None:
    spec = next(item for item in CORE_TOOL_SPECS if item.name == "patch_rule")
    action = spec.properties["actions"]["items"]

    assert "attach_evidence" in action["properties"]["token"]["description"]
    assert "attach_evidence/detach_evidence 禁止" in action["properties"]["value"]["description"]
