"""ontology_checker 对「# 数据来源」的检查：首行格式提示、存储属性英文名与真实表头逐列对账。

与 package_mapping 的解析、评测导入的绑定同一口径。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"

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


def run_checker(ontology: Path, category: str) -> list[dict]:
    # 用当前解释器跑，保证与测试环境同一套依赖（列名对账读 Excel 需要 openpyxl）
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "ontology_checker.py"), str(ontology), "--json"],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return [i for i in json.loads(result.stdout) if i["category"] == category]


def data_source_issues(tmp_path: Path, source: str) -> list[dict]:
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    (ontology / "object" / "订单.md").write_text(OBJECT_DOC.format(source=source), encoding="utf-8")
    return run_checker(ontology, "数据来源格式")


@pytest.mark.parametrize("source", [
    "- 数据库表 `T_ORDER` 的 `ORDER_ID` 列（按月汇总）。",
    "期初余额由上月末结转。",
])
def test_sentence_first_line_is_flagged(tmp_path: Path, source: str):
    issues = data_source_issues(tmp_path, source)
    assert len(issues) == 1 and issues[0]["severity"] == "警告"
    assert "首行应只写表名" in issues[0]["message"]


@pytest.mark.parametrize("source", [
    "T_ORDER\n\n> 数据库表 `T_ORDER` 的 `ORDER_ID` 列。",
    "`T_ORDER`",
    "订单 明细(2024)",
    "无\n\n> 由上游流程写入，不落库。",
    "无（派生对象，不落库）",
])
def test_bare_name_or_none_passes(tmp_path: Path, source: str):
    assert data_source_issues(tmp_path, source) == []


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


def columns_workspace(tmp_path: Path, uploaded: bool = True) -> Path:
    """一张合成数据表 T_ORDER(ORDER_ID, name, AMOUNT)：评测数据源上传的文件，或 schema 资料。"""
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
    (tmp_path / "ontology" / "object").mkdir(parents=True)
    return tmp_path / "ontology"


def column_issues(ontology: Path, rows: list[str], source: str = "T_ORDER") -> list[dict]:
    (ontology / "object" / "订单.md").write_text(
        COLUMNS_DOC.format(source=source, rows="\n".join(rows)), encoding="utf-8"
    )
    return run_checker(ontology, "列名对账")


KEY_ROW = "| 订单号 | ORDER_ID | 合成订单号 | 是 | 字符串 | 无 | 无 |"


@pytest.mark.parametrize("uploaded", [True, False])
def test_real_column_hidden_in_alias_is_an_error(tmp_path: Path, uploaded: bool):
    ontology = columns_workspace(tmp_path, uploaded=uploaded)
    issues = column_issues(ontology, [KEY_ROW, "| 订单名称 | order_name | 合成名称 | 否 | 字符串 | name | 无 |"])
    assert [i["severity"] for i in issues] == ["错误"]
    assert "order_name" in issues[0]["message"] and "英文名称改为 name" in issues[0]["message"]


def test_missing_column_warns_unless_marked_derived_or_non_stored(tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    issues = column_issues(ontology, [
        KEY_ROW,
        "| 评分 | SCORE | 合成评分 | 否 | 数值 | 无 | 无 |",
        "| 折扣 | DISCOUNT | 合成折扣 | 否 | 数值 | 无 | 派生：由 [[计算折扣]] 写入 |",
        "| 税率 | TAX_RATE | 合成税率 | 否 | 数值 | 无 | 非库字段；取自规程 |",
    ])
    assert len(issues) == 1 and issues[0]["severity"] == "警告"
    assert "SCORE" in issues[0]["message"]
    assert "DISCOUNT" not in issues[0]["message"] and "TAX_RATE" not in issues[0]["message"]


def test_columns_match_case_insensitively_and_undeclared_objects_are_skipped(tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    assert column_issues(ontology, [KEY_ROW, "| 名称 | NAME | 合成名称 | 否 | 字符串 | 无 | 无 |"]) == []
    assert column_issues(ontology, [KEY_ROW, "| 评分 | SCORE | 合成评分 | 否 | 数值 | 无 | 无 |"], source="无") == []


def test_declared_table_not_in_headers_warns(tmp_path: Path):
    ontology = columns_workspace(tmp_path)
    issues = column_issues(ontology, [KEY_ROW], source="T_ARCHIVE")
    assert len(issues) == 1 and "T_ARCHIVE" in issues[0]["message"] and "T_ORDER" in issues[0]["message"]


def test_no_table_headers_means_no_column_check(tmp_path: Path):
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    assert column_issues(ontology, [KEY_ROW, "| 评分 | SCORE | 合成评分 | 否 | 数值 | 无 | 无 |"]) == []


def test_headers_flag_lists_uploaded_and_schema_tables(tmp_path: Path):
    columns_workspace(tmp_path)
    result = subprocess.run(
        [sys.executable, str(SCRIPTS / "check_eval_data.py"), "--headers"],
        capture_output=True, text=True, timeout=60, cwd=tmp_path,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout) == {"T_ORDER": ["ORDER_ID", "name", "AMOUNT"]}
