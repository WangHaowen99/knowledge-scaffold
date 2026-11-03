"""ontology_checker 对「# 数据来源」的检查：首行格式提示、存储属性英文名与真实表头逐列对账。

与 package_mapping 的解析、发布包的数据绑定同一口径。
"""

import dataclasses
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import config
from tests.text_versions import write_text_versions

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def toolkit(monkeypatch):
    """工具箱脚本按同目录互相 import（ontology_checker → table_profile），在进程内直接调用。"""
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("ontology_checker"), importlib.import_module("table_profile")

OBJECT_DOC = """# 对象描述
合成订单。

# 对象别名
无

# 英文名称
Order

# 数据来源
{source}

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 订单号 | ORDER_ID | 合成订单号 | 是 | 字符串 | 无 | 无 |
"""


def run_checker(toolkit, ontology: Path, category: str) -> list[dict]:
    checker, _ = toolkit
    issues = [dataclasses.asdict(issue) for issue in checker.run_checks(ontology)]
    return [i for i in issues if i["category"] == category]


def data_source_issues(toolkit, tmp_path: Path, source: str) -> list[dict]:
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    (ontology / "object" / "订单.md").write_text(OBJECT_DOC.format(source=source), encoding="utf-8")
    return run_checker(toolkit, ontology, "数据来源格式")


@pytest.mark.parametrize("source", [
    "- 数据库表 `T_ORDER` 的 `ORDER_ID` 列（按月汇总）。",
    "期初余额由上月末结转。",
])
def test_sentence_first_line_is_flagged(toolkit, tmp_path: Path, source: str):
    issues = data_source_issues(toolkit, tmp_path, source)
    assert len(issues) == 1 and issues[0]["severity"] == "警告"
    assert "首行应只写表名" in issues[0]["message"]


@pytest.mark.parametrize("source", [
    "T_ORDER\n\n> 数据库表 `T_ORDER` 的 `ORDER_ID` 列。",
    "`T_ORDER`",
    "订单 明细(2024)",
    "无\n\n> 由上游流程写入，不落库。",
    "无（派生对象，不落库）",
])
def test_bare_name_or_none_passes(toolkit, tmp_path: Path, source: str):
    assert data_source_issues(toolkit, tmp_path, source) == []


# ---- 存储属性英文名 ↔ 真实表头逐列对账 ----

openpyxl = pytest.importorskip("openpyxl")

COLUMNS_DOC = """# 对象描述
合成订单。

# 对象别名
无

# 英文名称
Order

# 数据来源
{source}

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}
"""


def columns_workspace(tmp_path: Path, uploaded: bool = False) -> Path:
    """一张合成数据表 T_ORDER(ORDER_ID, name, AMOUNT)：schema 资料；uploaded=True 时放在旧版
    上传目录（.ontology-state 下），评测移出后（ADR-0020）它不再是表来源。"""
    workbook = openpyxl.Workbook()
    workbook.active.title = "T_ORDER"
    workbook.active.append(["ORDER_ID", "name", "AMOUNT"])
    if uploaded:
        path = tmp_path / ".ontology-state" / "eval-data" / "0123456789ab" / "业务数据.xlsx"
    else:
        path = tmp_path / "resource" / "excel" / "结构.xlsx"
        (tmp_path / "resource").mkdir(parents=True, exist_ok=True)
        (tmp_path / "resource" / "materials.json").write_text(
            json.dumps({"resource/excel/结构.xlsx": {"purpose": "schema"}}, ensure_ascii=False), encoding="utf-8"
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)
    write_text_versions(tmp_path)  # 资料区的 Excel 只读文本版（F4c）
    (tmp_path / "ontology" / "object").mkdir(parents=True)
    return tmp_path / "ontology"


def column_issues(toolkit, ontology: Path, rows: list[str], source: str = "T_ORDER") -> list[dict]:
    (ontology / "object" / "订单.md").write_text(
        COLUMNS_DOC.format(source=source, rows="\n".join(rows)), encoding="utf-8"
    )
    return run_checker(toolkit, ontology, "列名对账")


KEY_ROW = "| 订单号 | ORDER_ID | 合成订单号 | 是 | 字符串 | 无 | 无 |"


def test_real_column_hidden_in_alias_is_an_error(toolkit, tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    issues = column_issues(toolkit, ontology, [KEY_ROW, "| 订单名称 | order_name | 合成名称 | 否 | 字符串 | name | 无 |"])
    assert [i["severity"] for i in issues] == ["错误"]
    assert "order_name" in issues[0]["message"] and "英文名称改为 name" in issues[0]["message"]


def test_leftover_upload_is_not_a_table_source(toolkit, tmp_path: Path):
    """旧版上传目录里残留的表不参与列名对账：只剩「无可核对的数据表」提示。"""
    ontology = columns_workspace(tmp_path, uploaded=True)
    issues = column_issues(toolkit, ontology, [KEY_ROW, "| 订单名称 | order_name | 合成名称 | 否 | 字符串 | name | 无 |"])
    assert [i["severity"] for i in issues] == ["警告"]
    assert "还没有可核对的数据表" in issues[0]["message"]


def test_missing_column_warns_unless_marked_derived_or_non_stored(toolkit, tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    issues = column_issues(toolkit, ontology, [
        KEY_ROW,
        "| 评分 | SCORE | 合成评分 | 否 | 数值 | 无 | 无 |",
        "| 折扣 | DISCOUNT | 合成折扣 | 否 | 数值 | 无 | 派生：由 [[计算折扣]] 写入 |",
        "| 税率 | TAX_RATE | 合成税率 | 否 | 数值 | 无 | 非库字段；取自规程 |",
    ])
    assert len(issues) == 1 and issues[0]["severity"] == "警告"
    assert "SCORE" in issues[0]["message"]
    assert "DISCOUNT" not in issues[0]["message"] and "TAX_RATE" not in issues[0]["message"]


def test_columns_match_case_insensitively_and_undeclared_objects_are_skipped(toolkit, tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    assert column_issues(toolkit, ontology, [KEY_ROW, "| 名称 | NAME | 合成名称 | 否 | 字符串 | 无 | 无 |"]) == []
    assert column_issues(toolkit, ontology, [KEY_ROW, "| 评分 | SCORE | 合成评分 | 否 | 数值 | 无 | 无 |"], source="无") == []


def test_declared_table_not_in_headers_is_an_error(toolkit, tmp_path: Path):
    """与发布门禁同口径：有数据表可核对、声明却对不上，发布时会被阻断。"""
    ontology = columns_workspace(tmp_path)
    issues = column_issues(toolkit, ontology, [KEY_ROW], source="T_ARCHIVE")
    assert [i["severity"] for i in issues] == ["错误"]
    assert "T_ARCHIVE" in issues[0]["message"]
    assert "T_ORDER" in issues[0]["message"]


def test_sentence_first_line_binds_existing_quoted_table_like_backend(toolkit, tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    assert column_issues(toolkit, ontology, [KEY_ROW], source="- 取自 `T_ORDER` 的明细（按月）。") == []
    issues = column_issues(toolkit, ontology, [KEY_ROW], source="- 取自 `T_ARCHIVE` 的明细（按月）。")
    assert [i["severity"] for i in issues] == ["错误"]


def test_declared_table_without_any_matching_column_is_an_error(toolkit, tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    issues = column_issues(toolkit, ontology, ["| 月份 | MONTH | 合成月份 | 是 | 字符串 | 无 | 无 |"])
    assert "错误" in [i["severity"] for i in issues]
    assert any("没有一列能对上" in i["message"] for i in issues)


def test_no_table_headers_means_no_column_check(toolkit, tmp_path: Path):
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    rows = [KEY_ROW, "| 评分 | SCORE | 合成评分 | 否 | 数值 | 无 | 无 |"]
    assert column_issues(toolkit, ontology, rows, source="无") == []


def test_declaration_without_any_table_only_warns(toolkit, tmp_path: Path):
    """没有数据表时声明无从核对：提示不要编造表名，但不阻断提交。"""
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    issues = column_issues(toolkit, ontology, [KEY_ROW], source="T_ORDER")
    assert [i["severity"] for i in issues] == ["警告"]
    assert "T_ORDER" in issues[0]["message"] and "schema" in issues[0]["message"]


def test_field_alias_conflict_matches_evaluation_rule(toolkit, tmp_path: Path):
    ontology = tmp_path / "ontology"
    object_dir = ontology / "object"
    object_dir.mkdir(parents=True)
    (object_dir / "订单.md").write_text(
        """# 对象描述
合成订单。

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 订单状态 | order_status | 订单状态 | 否 | 字符串 | 状态 | 无 |
| 支付状态 | payment_status | 支付状态 | 否 | 字符串 | 状态 | 无 |
""",
        encoding="utf-8",
    )

    issues = run_checker(toolkit, ontology, "字段名称冲突")

    assert len(issues) == 1
    assert issues[0]["severity"] == "错误"
    assert "order_status" in issues[0]["message"]
    assert "payment_status" in issues[0]["message"]
    assert "禁止删除别名" in issues[0]["message"]


def test_headers_flag_lists_schema_tables(toolkit, tmp_path: Path, monkeypatch, capsys):
    _, table_profile = toolkit
    columns_workspace(tmp_path)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("sys.argv", ["table_profile.py", "--headers"])
    assert table_profile.main() == 0
    assert json.loads(capsys.readouterr().out) == {"T_ORDER": ["ORDER_ID", "name", "AMOUNT"]}


def test_old_script_name_forwards_for_one_release(tmp_path: Path):
    """旧名 check_eval_data.py 保留一版转发：同一份输出，stderr 提示改名。"""
    columns_workspace(tmp_path)
    old = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_eval_data.py"), "--headers"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    new = subprocess.run(
        [sys.executable, str(SCRIPTS / "table_profile.py"), "--headers"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    assert old.returncode == new.returncode == 0
    assert old.stdout == new.stdout and json.loads(old.stdout) == {"T_ORDER": ["ORDER_ID", "name", "AMOUNT"]}
    assert "table_profile.py" in old.stderr


def test_reconcile_given_tables(tmp_path: Path):
    """三方对账只对命令行给出的数据表；不给时提示用法，不报差异。"""
    ontology = columns_workspace(tmp_path)
    (ontology / "object" / "订单.md").write_text(
        COLUMNS_DOC.format(source="T_ORDER", rows=KEY_ROW), encoding="utf-8"
    )
    script = str(SCRIPTS / "table_profile.py")
    idle = subprocess.run([sys.executable, script, "--json"], cwd=tmp_path, capture_output=True, text=True, timeout=60)
    assert idle.returncode == 0 and json.loads(idle.stdout)["status"] == "no_files"
    checked = subprocess.run(
        [sys.executable, script, "--json", "resource/excel/结构.xlsx"],
        cwd=tmp_path, capture_output=True, text=True, timeout=60,
    )
    result = json.loads(checked.stdout)
    assert checked.returncode == 1 and result["status"] == "warn"
    assert result["files"][0]["sheets"][0]["attr_unmatched"] == ["AMOUNT", "name"]
