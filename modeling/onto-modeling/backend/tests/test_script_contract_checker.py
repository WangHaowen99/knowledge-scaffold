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
