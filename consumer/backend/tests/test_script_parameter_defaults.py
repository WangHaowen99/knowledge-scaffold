from agent.tools.execute_script import _build_cli_args
from ontology.ontology_catalog import (
    _format_script_catalog,
    _parse_script_parameters_from_markdown,
    public_script_catalog,
)
from ontology.legacy_package_converter import (
    _normalize_legacy_parameters,
    _write_script_descriptor,
)


def test_script_parameter_default_round_trips_into_execution_catalog():
    markdown = """
| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 |
| --- | --- | --- | --- | --- | --- |
| records | --records | array | 是 |  | 待处理记录 |
| mode | --mode | string | 否 | standard | 处理模式 |
"""

    parameters = _parse_script_parameters_from_markdown(markdown)
    assert parameters[1]["default"] == "standard"

    public = public_script_catalog([{
        "operation_key": "process_records",
        "script_key": "process_records",
        "title": "处理记录",
        "parameters": parameters,
        "_is_bound_operation": True,
    }])
    assert public[0]["parameters"][1]["default"] == "standard"
    assert "default=standard" in "\n".join(_format_script_catalog(public))

    args, missing, invalid = _build_cli_args(
        script_args=[],
        script_parameters=parameters,
        arguments={"records": [{"id": "record-a"}]},
    )
    assert missing == []
    assert invalid == []
    assert args[-2:] == ["--mode", "standard"]


def test_legacy_package_conversion_preserves_parameter_defaults(tmp_path):
    parameters = _normalize_legacy_parameters([
        {
            "name": "strict",
            "flag": "--strict",
            "type": "boolean",
            "required": False,
            "default": False,
        },
    ])

    descriptor = tmp_path / "scripts" / "validate.md"
    _write_script_descriptor(
        descriptor,
        script_key="validate",
        operation_key="validate_records",
        entrypoint="scripts/validate.py",
        parameters=parameters,
    )

    reparsed = _parse_script_parameters_from_markdown(descriptor.read_text(encoding="utf-8"))
    assert reparsed[0]["required"] is False
    assert reparsed[0]["default"] is False
