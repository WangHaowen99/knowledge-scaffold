"""Synthetic CLI results cover conservative static checks and actual JSON type validation."""
import pytest

from app.services.toolkit import script_contract, script_output

HEADER = '"""\n输入参数：无\n输出：\n    result    integer    合成结果\n"""\n'


@pytest.mark.parametrize("expression", ['{"other": 1}', 'calculate()'])
def test_known_wrong_cli_keys_are_rejected(expression):
    code = HEADER + 'import json\ndef calculate():\n    return {"other": 1}\n'
    code += f'def _cli():\n    result = {expression}\n    print(json.dumps(result))\n'
    with pytest.raises(script_contract.ScriptContractError, match="输出字段与声明不一致"):
        script_contract.parse_script(code)


def test_branches_check_every_possible_return_and_ignore_error_envelope():
    code = HEADER + '''import json
def calculate(condition):
    if condition:
        return {"result": 1}
    return {"other": 1}
def _cli():
    print(json.dumps(calculate(True)))
'''
    with pytest.raises(script_contract.ScriptContractError, match="other"):
        script_contract.parse_script(code)
    code = code.replace('return {"other": 1}', 'return {"ok": False, "error": {"code": "INVALID_INPUT"}}')
    assert script_contract.parse_script(code).output_verified


@pytest.mark.parametrize("body", [
    'result = {"temporary": 1}\n    result["result"] = result.pop("temporary")\n    return result',
    'result = {"temporary": 1}\n    result.update(result=1)\n    return result',
    'return dict(result=1)',
    'return {**external_result}',
    'for item in values:\n        return {"result": 1}',
])
def test_unknown_result_shapes_warn_without_false_rejection(body):
    code = HEADER + f'import json\ndef calculate():\n    {body}\ndef _cli():\n    print(json.dumps(calculate()))\n'
    contract = script_contract.parse_script(code)
    assert not contract.output_verified
    assert any("尚未验证" in warning for warning in contract.warnings)


def test_unused_and_non_stdout_helpers_do_not_define_cli_output():
    code = HEADER + '''import json
def helper():
    print(json.dumps({"internal": 1}))
def _cli():
    encoded = json.dumps({"internal": 1})
    print(json.dumps({"result": 1}))
'''
    assert script_contract.parse_script(code).output_verified


def test_static_path_limit_requires_verification_instead_of_exponential_work():
    body = "\n".join("    if condition:\n        pass" for _ in range(40))
    code = HEADER + f'import json\ndef calculate():\n{body}\n    return {{"result": 1}}\ndef _cli():\n    print(json.dumps(calculate()))\n'
    assert not script_contract.parse_script(code).output_verified


@pytest.mark.parametrize("payload, kind, expected", [
    ({"result": 2}, "integer", ""),
    ({"result": True}, "integer", "类型"),
    ({"result": 2}, "number", ""),
    ({"result": "2"}, "number", "类型"),
    ({"other": 2}, "integer", "缺少"),
    ([2], "integer", "JSON 对象"),
    ({"result": {}}, "object", ""),
    ({"result": []}, "array", ""),
    ({"result": False}, "boolean", ""),
    ({"result": "synthetic"}, "string", ""),
])
def test_runtime_output_fields_and_types(payload, kind, expected):
    error = script_output.output_error(payload, ({"name": "result", "type": kind},))
    assert expected in error if expected else error == ""


def test_nested_output_field_is_checked():
    schema = ({"name": "result.count", "type": "integer"},)
    assert script_output.output_error({"result": {"count": 1}}, schema) == ""
    assert "result.count" in script_output.output_error({"result": {}}, schema)


def test_function_parameter_shadowing_requires_runtime_verification():
    code = HEADER + '''import json
def helper():
    return {"internal": 1}
def calculate(helper):
    return helper()
def _cli():
    print(json.dumps(calculate(callback)))
'''
    assert not script_contract.parse_script(code).output_verified


@pytest.mark.parametrize("imports", ["import json as encoder", "import math, json as encoder"])
def test_json_import_alias_preserves_output_validation(imports):
    code = HEADER + f'{imports}\nprint(encoder.dumps({{"result": 1}}))\n'
    assert script_contract.parse_script(code).output_verified
    with pytest.raises(script_contract.ScriptContractError, match="输出字段与声明不一致"):
        script_contract.parse_script(code.replace('{"result": 1}', '{"other": 1}'))


def test_reachable_call_chain_checks_outputs_and_ignores_unused_helpers():
    code = HEADER + '''import json
def unused():
    print(json.dumps({"internal": 1}))
def emit_result():
    print(json.dumps({"result": 1}))
def middle():
    emit_result()
def main():
    middle()
if __name__ == "__main__":
    main()
'''
    assert script_contract.parse_script(code).output_verified
    with pytest.raises(script_contract.ScriptContractError, match="输出字段与声明不一致"):
        script_contract.parse_script(code.replace('{"result": 1}', '{"other": 1}'))


@pytest.mark.parametrize("functions", [
    'def calculate():\n    return calculate()\n',
    '@decorator\ndef calculate():\n    return {"other": 1}\n',
    "".join(f'def calculate_{index}():\n    return calculate_{index + 1}()\n' for index in range(9))
    + 'def calculate_9():\n    return {"other": 1}\ndef calculate():\n    return calculate_0()\n',
])
def test_recursive_decorated_or_deep_calls_require_runtime_verification(functions):
    code = HEADER + 'import json\n' + functions + 'def _cli():\n    print(json.dumps(calculate()))\n'
    assert not script_contract.parse_script(code).output_verified
