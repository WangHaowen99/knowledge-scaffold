"""ontology_checker 对「# 关联对象」关联字段的检查。

与后端 package_mapping._relation_join、评测图导入 eval_import.graph_relation_mapping
同一口径：同表按同一数据行关联不需要关联字段，跨表没声明又推断不出就是问题。
"""

import dataclasses
import importlib
import re
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def checker(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("ontology_checker")


OBJECT_DOC = """# 对象描述
{name}。

# 对象别名
无

# 英文名称
{name}

# 数据来源
{table}

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |
| --- | --- | --- | --- | --- | --- | --- |
{rows}

# 关联对象
{relations}
"""


def write(ontology: Path, name: str, table: str, rows: str, relations: str = "") -> None:
    ontology.joinpath("object").mkdir(parents=True, exist_ok=True)
    (ontology / "object" / f"{name}.md").write_text(
        OBJECT_DOC.format(name=name, table=table, rows=rows, relations=relations),
        encoding="utf-8",
    )


# 两端关联字段不同名：订单侧的外键列与客户侧的主键列拼写不同
ORDER_ROWS = (
    "| 订单号 | ORDER_ID | 订单主键 | 是 | 字符串 | 无 | 无 |\n"
    "| 客户编号 | BUYER_NO | 指向客户 | 否 | 字符串 | 无 | 无 |"
)
CUSTOMER_ROWS = "| 客户编号 | CUST_NO | 客户主键 | 是 | 字符串 | 无 | 无 |"


def join_issues(checker, ontology: Path) -> list[str]:
    issues = [dataclasses.asdict(i) for i in checker.run_checks(ontology)]
    return [i["message"] for i in issues if i["category"] == "关联字段"]


def duplicate_issues(checker, ontology: Path) -> list[dict]:
    issues = [dataclasses.asdict(i) for i in checker.run_checks(ontology)]
    return [i for i in issues if i["category"] == "重复关系"]


def test_relation_written_once_in_each_endpoint_file_is_not_a_duplicate(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS, "- 当前对象 → 属于（BUYER_NO → CUST_NO） → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS, "- [[订单]] → 属于（BUYER_NO → CUST_NO） → 当前对象")
    assert duplicate_issues(checker, ontology) == []


def test_relation_repeated_in_one_file_is_flagged_with_both_lines(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS,
          "- 当前对象 → 属于（BUYER_NO → CUST_NO） → [[客户]]\n"
          "- 当前对象 -> 属于（订单表.BUYER_NO -> CUST_NO） -> [[客户]]\n"
          "- 当前对象 → 属于 → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS)
    issues = duplicate_issues(checker, ontology)
    assert len(issues) == 1 and issues[0]["severity"] == "错误" and "订单" in issues[0]["file"]
    first, second = map(int, re.search(r"第 (\d+) 行和第 (\d+) 行", issues[0]["message"]).groups())
    assert second == first + 1


def test_cross_table_relation_without_join_is_flagged(checker, tmp_path: Path):
    """跨表、无括注、两端没有同名主键：评测会被拦，建模阶段就该报出来。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS, "- 当前对象 → 属于 → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS, "- [[订单]] → 属于 → 当前对象")

    messages = join_issues(checker, ontology)
    assert len(messages) == 2  # 两端各报自己那一行
    assert "未声明关联字段" in messages[0]
    assert "属于（源对象字段 → 目标对象字段）" in messages[0]


def test_declared_join_fields_pass(checker, tmp_path: Path):
    """括注声明不同名字段即可，不要求两端同名。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS,
          "- 当前对象 → 属于（BUYER_NO → CUST_NO） → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS,
          "- [[订单]] → 属于（BUYER_NO → CUST_NO） → 当前对象")

    assert join_issues(checker, ontology) == []


def test_declared_join_field_must_exist(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS,
          "- 当前对象 → 属于（BUYER_NO → 不存在的列） → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS)

    messages = join_issues(checker, ontology)
    assert len(messages) == 1
    assert "不存在的列" in messages[0] and "英文名称" in messages[0]


def test_same_table_relation_needs_no_join(checker, tmp_path: Path):
    """两端同一张表：图导入按同一数据行关联，不该要求括注。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "宽表", ORDER_ROWS, "- 当前对象 → 属于 → [[客户]]")
    write(ontology, "客户", "宽表", CUSTOMER_ROWS, "- [[订单]] → 属于 → 当前对象")

    assert join_issues(checker, ontology) == []


def test_same_name_primary_key_still_inferable(checker, tmp_path: Path):
    """跨表但目标主键在源端有同名属性：后端能推断，不报，免得噪音淹没真问题。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表",
          "| 订单号 | ORDER_ID | 订单主键 | 是 | 字符串 | 无 | 无 |\n"
          "| 客户编号 | CUST_NO | 指向客户 | 否 | 字符串 | 无 | 无 |",
          "- 当前对象 → 属于 → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS, "- [[订单]] → 属于 → 当前对象")

    assert join_issues(checker, ontology) == []


def test_ambiguous_note_is_flagged(checker, tmp_path: Path):
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS,
          "- 当前对象 → 属于（BUYER_NO 与 ORDER_ID → CUST_NO） → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS)

    messages = join_issues(checker, ontology)
    assert len(messages) == 1
    assert "括注不明确" in messages[0]


def test_plain_note_is_not_mistaken_for_join(checker, tmp_path: Path):
    """括注当纯说明用的存量文档不该被判成关联字段声明（两端清单都没有这些词）。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "宽表", ORDER_ROWS, "- 当前对象 → 属于（一对多） → [[客户]]")
    write(ontology, "客户", "宽表", CUSTOMER_ROWS)

    assert join_issues(checker, ontology) == []


@pytest.mark.parametrize("note", [
    "（订单表.BUYER_NO → 客户表.CUST_NO）",
    "（订单.BUYER_NO → `客户`.`CUST_NO`）",
])
def test_table_prefixed_join_fields_pass(checker, tmp_path: Path, note):
    """括注字段带上本端的表名或对象名前缀，与发布侧同口径照认。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS, f"- 当前对象 → 属于{note} → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS)

    assert join_issues(checker, ontology) == []


def test_prefix_of_the_other_end_is_flagged(checker, tmp_path: Path):
    """前缀写成另一端的表：方向写反了，不能剥掉前缀了事。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS,
          "- 当前对象 → 属于（客户表.BUYER_NO → 订单表.CUST_NO） → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS)

    messages = join_issues(checker, ontology)
    assert len(messages) == 1 and "括注不明确" in messages[0]


def test_literal_current_object_name_placeholder_is_checked(checker, tmp_path: Path):
    """旧版抽取模板的字面占位「当前对象名」按当前对象处理，关联字段照样核对。"""
    ontology = tmp_path / "ontology"
    write(ontology, "订单", "订单表", ORDER_ROWS, "- 当前对象名 → 属于 → [[客户]]")
    write(ontology, "客户", "客户表", CUSTOMER_ROWS)

    messages = join_issues(checker, ontology)
    assert len(messages) == 1 and "未声明关联字段" in messages[0]
