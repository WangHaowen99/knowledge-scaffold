"""ontology_checker --include-mock 的主键唯一性检查。

对象属性清单里多列标 主键=是 时是联合主键，重复判定必须用全部主键列组成的元组，
只看第一列会把「同一商品在不同区域的价格」误报成主键重复。
"""

import dataclasses
import importlib
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("ontology_checker")


OBJECT_DOC = """# 对象描述
商品区域价格。

# 对象别名
无

# 英文名称
region_price

# 数据来源
region_price

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
| 商品编码 | sku_code | 商品编码 | 是 | 字符串 | 无 | 无 |
| 区域编码 | region_code | 销售区域编码 | 是 | 字符串 | 无 | 无 |
| 价格 | price | 区域售价 | 否 | 数值 | 无 | 无 |

# 关联对象
无
"""

MOCK_DOC = """# 商品区域价格

## 正常数据

| 商品编码 | 区域编码 | 价格 |
| --- | --- | --- |
{rows}
"""


def pk_issues(checker, tmp_path: Path, rows: str) -> list[str]:
    ontology = tmp_path / "ontology"
    (ontology / "object").mkdir(parents=True)
    (ontology / "data-mock").mkdir()
    (ontology / "object" / "商品区域价格.md").write_text(OBJECT_DOC, encoding="utf-8")
    (ontology / "data-mock" / "商品区域价格.md").write_text(MOCK_DOC.format(rows=rows), encoding="utf-8")
    issues = [dataclasses.asdict(i) for i in checker.run_checks(ontology, include_mock=True)]
    return [i["message"] for i in issues if i["category"] == "主键重复"]


def test_composite_key_rows_unique_as_tuple_pass(checker, tmp_path: Path):
    rows = (
        "| SKU-01 | R1 | 12.5 |\n"
        "| SKU-01 | R2 | 13.0 |\n"
        "| SKU-02 | R1 | 8.0 |"
    )
    assert pk_issues(checker, tmp_path, rows) == []


def test_composite_key_duplicate_tuple_is_flagged(checker, tmp_path: Path):
    rows = (
        "| SKU-01 | R1 | 12.5 |\n"
        "| SKU-01 | R2 | 13.0 |\n"
        "| SKU-01 | R1 | 12.8 |"
    )
    messages = pk_issues(checker, tmp_path, rows)
    assert len(messages) == 1
    assert "商品编码" in messages[0] and "区域编码" in messages[0]
    assert "SKU-01" in messages[0] and "R1" in messages[0]
