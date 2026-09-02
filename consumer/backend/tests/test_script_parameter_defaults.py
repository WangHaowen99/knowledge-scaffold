from agent.tools.execute_script import _build_cli_args
from ontology.ontology_catalog import (
    _format_script_catalog,
    _parse_script_parameters_from_markdown,
    public_script_catalog,
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


def test_modeling_markdown_boolean_actions_drive_consumer_cli_semantics():
    markdown = """
| 参数 key | CLI 参数 | 类型 | 必填 | 默认值 | 说明 | 是否多值 | Action |
| --- | --- | --- | --- | --- | --- | --- | --- |
| verbose | --verbose | boolean | 否 | false | 详细输出 | 否 | store_true |
| cache | --cache | boolean | 否 | true | 使用缓存 | 否 | store_false |
| enabled | --enabled | boolean | 否 | false | 普通布尔值 | 否 |  |
"""

    parameters = _parse_script_parameters_from_markdown(markdown)
    assert parameters[0]["action"] == "store_true"
    assert parameters[1]["action"] == "store_false"
    assert "action" not in parameters[2]

    args, missing, invalid = _build_cli_args(
        script_args=[],
        script_parameters=parameters,
        arguments={"verbose": True, "cache": False, "enabled": False},
    )

    assert (args, missing, invalid) == (
        ["--verbose", "--cache", "--enabled", "false"],
        [],
        [],
    )
