"""ontology_checker --include-scripts 与知识包导出共用脚本契约校验。

脚本走真实 subprocess 执行，与 /ontology-codegen Step 4.4 的调用方式一致。
"""

import json
import subprocess
from pathlib import Path

import pytest

from app import config
from app.services.workspace import ScriptContractError, _script_header_meta

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"

BAD_SCRIPT = '''"""
关联operation：无

功能描述：示例

输入参数：
    device_id        str           装置代码；必填
    yield_records    list[dict]    收率记录；默认空列表

输出：
    return dict
"""
import argparse
import json


def select(device_id, yield_records=None):
    return {}


def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--device-id", required=True)
    print(json.dumps(select(**vars(parser.parse_args()))))


if __name__ == "__main__":
    _cli()
'''


def run_checker(ontology: Path, *args: str) -> list[dict]:
    result = subprocess.run(
        ["python3", str(SCRIPTS / "ontology_checker.py"), str(ontology), "--json", *args],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def make_ontology(tmp_path: Path, code: str) -> Path:
    ontology = tmp_path / "ontology"
    (ontology / "scripts").mkdir(parents=True)
    (ontology / "scripts" / "select_yield.py").write_text(code, encoding="utf-8")
    return ontology


def test_include_scripts_reports_same_contract_error_as_export(tmp_path: Path):
    ontology = make_ontology(tmp_path, BAD_SCRIPT)
    issues = [i for i in run_checker(ontology, "--include-scripts") if i["category"] == "脚本契约"]
    with pytest.raises(ScriptContractError) as exc:
        _script_header_meta(BAD_SCRIPT)
    assert [i["message"] for i in issues] == [str(exc.value)]
    assert issues[0]["severity"] == "错误"
    assert issues[0]["file"] == "scripts/select_yield.py"


def test_include_scripts_passes_consistent_script(tmp_path: Path):
    good = BAD_SCRIPT.replace(
        "    yield_records    list[dict]    收率记录；默认空列表\n", ""
    )
    ontology = make_ontology(tmp_path, good)
    issues = run_checker(ontology, "--include-scripts")
    assert [i for i in issues if i["category"] == "脚本契约"] == []


def test_scripts_are_not_checked_without_flag(tmp_path: Path):
    ontology = make_ontology(tmp_path, BAD_SCRIPT)
    assert [i for i in run_checker(ontology) if i["category"] == "脚本契约"] == []


INT_SCRIPT = '''"""
关联operation：无

功能描述：示例

输入参数：
    year      {doc_type}    统计年份；必填
    ratio     float         折算系数；默认值：1.0

输出：
    return dict
"""
import argparse


def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--ratio", type=float, default=1.0)
    parser.parse_args()


if __name__ == "__main__":
    _cli()
'''


@pytest.mark.parametrize("doc_type", ["int", "integer"])
def test_int_params_export_as_integer_not_number(doc_type: str):
    """type=int 的参数必须导出为 integer：导出 number 时运行端按浮点拼成 2026.0，argparse int 直接退出。"""
    _, params = _script_header_meta(INT_SCRIPT.format(doc_type=doc_type))
    types = {p["name"]: p["type"] for p in params}
    assert types == {"year": "integer", "ratio": "number"}


def test_header_number_for_argparse_int_is_still_accepted():
    """头部写 number、argparse 写 type=int 的旧脚本仍可导出，类型以 argparse 为准。"""
    _, params = _script_header_meta(INT_SCRIPT.format(doc_type="number"))
    assert {p["name"]: p["type"] for p in params}["year"] == "integer"
