"""对象 ↔ 数据表绑定解析（package_mapping）：声明优先、重合度推断、列裁剪与 mapping 产出。"""

import json
from pathlib import Path

import pytest

from app.services import package_mapping

openpyxl = pytest.importorskip("openpyxl")


def make_workspace(tmp_path: Path, sheets: dict[str, list[str]]) -> Path:
    root = tmp_path / "ws"
    (root / "resource" / "excel").mkdir(parents=True)
    (root / "ontology" / "object").mkdir(parents=True)
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, columns in sheets.items():
        workbook.create_sheet(name).append(columns)
    workbook.save(root / "resource" / "excel" / "评测数据.xlsx")
    (root / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/评测数据.xlsx": {"purpose": "schema"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    return root


def write_object(root: Path, key: str, rows: list[tuple[str, str]], source: str | None = None) -> None:
    table = "\n".join(f"| {cn} | {en} | 说明 | 否 | 字符串 | 无 | 无 |" for cn, en in rows)
    (root / "ontology" / "object" / f"{key}.md").write_text(
        (f"# 数据来源\n{source}\n\n" if source is not None else "")
        + "# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        f"{table}\n",
        encoding="utf-8",
    )


def test_declared_source_wins_over_column_overlap(tmp_path):
    root = make_workspace(tmp_path, {
        "T_ORDER": ["ORDER_ID", "AMOUNT"],
        "T_ORDER_ARCHIVE": ["ORDER_ID", "AMOUNT", "ARCHIVED_AT"],
    })
    # 两张表列头重合度相同，靠推断会并列第一 → 必须显式声明
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")])
    assert package_mapping.resolve_object_tables(root)["objects"]["订单"]["table"] == ""

    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source="T_ORDER_ARCHIVE")
    item = package_mapping.resolve_object_tables(root)["objects"]["订单"]
    assert item["table"] == "T_ORDER_ARCHIVE" and item["matched_by"] == "declared"


def test_unique_best_overlap_is_inferred(tmp_path):
    root = make_workspace(tmp_path, {
        "T_ORDER": ["ORDER_ID", "AMOUNT", "STATUS"],
        "T_USER": ["USER_ID", "USER_NAME"],
    })
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT"), ("状态", "STATUS")])
    item = package_mapping.resolve_object_tables(root)["objects"]["订单"]
    assert item["table"] == "T_ORDER" and item["matched_by"] == "inferred"
    assert item["matched_columns"] == 3


def test_single_column_overlap_is_not_enough(tmp_path):
    """只对上一列多半是巧合（如 ID 列同名），宁可不绑也不错绑。"""
    root = make_workspace(tmp_path, {"T_ORDER": ["ID", "AMOUNT"], "T_USER": ["USER_NAME"]})
    write_object(root, "用户", [("编号", "ID"), ("姓名", "NAME")])
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["用户"]["table"] == ""
    assert result["bound"] == 0
    assert any("没有绑定真实数据表" in w for w in result["warnings"])


def test_declared_table_missing_is_a_problem(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "订单", [("订单号", "ORDER_ID")], source="T_NOT_THERE")
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["table"] == ""
    assert any("T_NOT_THERE" in p for p in result["problems"])


def test_template_placeholder_and_none_are_not_table_names(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(
        root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")],
        source="{待填写：该对象对应的真实数据表名，即 eval_data/schema 表格资料里的 Sheet 名，\n逐字相同}",
    )
    # 占位符不当成声明 → 回落到推断，且不报"声明的表不存在"
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["table"] == "T_ORDER"
    assert not result["problems"]

    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source="无")
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["table"] == "T_ORDER"
    assert not result["problems"]


def test_sentence_first_line_binds_first_existing_quoted_table(tmp_path):
    """首行写成说明句：整句不是表名，按句中反引号标出且真实存在的第一个表绑定并提示改写。"""
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"], "T_USER": ["USER_ID"]})
    write_object(
        root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")],
        source="- 数据库表 `T_ORDER` 的 `ORDER_ID` / `AMOUNT` 列（按月汇总）。\n- 客户信息见 `T_USER`。",
    )
    result = package_mapping.resolve_object_tables(root)
    item = result["objects"]["订单"]
    assert item["table"] == "T_ORDER" and item["matched_by"] == "declared"
    assert not result["problems"]
    assert any("首行不是表名" in w for w in result["warnings"])


def test_backticked_bare_name_is_a_declaration(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source="- `T_ORDER`")
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["matched_by"] == "declared"
    assert not any("首行不是表名" in w for w in result["warnings"])


@pytest.mark.parametrize("source", [
    "无\n- 由上游流程写入，不落库。",
    "无（派生对象，不落库）\n\n> 见 `T_ARCHIVE`",
])
def test_none_with_notes_declares_nothing(tmp_path, source):
    """首行「无」即不声明：后面的说明行不能被当成表名。"""
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source=source)
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["declared_table"] == ""
    assert result["objects"]["订单"]["matched_by"] == "inferred"
    assert not result["problems"]


def test_bare_missing_name_does_not_fall_back_to_notes(tmp_path):
    """首行是规范表名但本轮没有这张表时，不从说明行里另找一张表顶替。"""
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(
        root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")],
        source="T_ORDER_ARCHIVE\n\n> 历史明细另见 `T_ORDER`",
    )
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["table"] == ""
    assert any("T_ORDER_ARCHIVE" in p and "首行只写表名" in p for p in result["problems"])


def test_declared_table_without_matching_columns_blocks_only_when_strict(tmp_path):
    root = make_workspace(tmp_path, {"T_STOCK": ["STOCK_ID", "QTY"], "T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "月报", [("月份", "MONTH"), ("产量", "OUTPUT")], source="T_STOCK")
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source="T_ORDER")

    strict = package_mapping.resolve_object_tables(root)
    assert any("月报" in p and "没有一列能对上" in p for p in strict["problems"])

    lenient = package_mapping.resolve_object_tables(root, strict_declared=False)
    assert not lenient["problems"]
    assert any("月报" in w and "没有一列能对上" in w for w in lenient["warnings"])
    assert lenient["objects"]["月报"]["table"] == "" and lenient["bound"] == 1


def test_derived_and_non_stored_attributes_are_not_missing_columns(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    doc = root / "ontology" / "object" / "订单.md"
    doc.write_text(
        "# 数据来源\nT_ORDER\n\n# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 订单号 | ORDER_ID | 说明 | 是 | 字符串 | 无 | 无 |\n"
        "| 评分 | SCORE | 说明 | 否 | 数值 | 无 | 派生：由 [[计算评分]] 写入 |\n"
        "| 税率 | TAX_RATE | 说明 | 否 | 数值 | 无 | 非库字段 |\n"
        "| 备注 | REMARK | 说明 | 否 | 字符串 | 无 | 无 |\n",
        encoding="utf-8",
    )
    item = package_mapping.resolve_object_tables(root)["objects"]["订单"]
    assert item["missing_columns"] == ["REMARK"]
    assert [f["key"] for f in item["fields"]] == ["ORDER_ID", "SCORE", "TAX_RATE", "REMARK"]


def test_unmatched_attributes_stay_in_fields_but_not_in_mapping(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(
        root, "订单",
        [("订单号", "ORDER_ID"), ("金额", "AMOUNT"), ("派生评分", "SCORE")],
        source="T_ORDER",
    )
    result = package_mapping.resolve_object_tables(root)
    item = result["objects"]["订单"]
    assert item["missing_columns"] == ["SCORE"]
    assert [f["key"] for f in item["fields"]] == ["ORDER_ID", "AMOUNT", "SCORE"]
    assert any("SCORE" in w for w in result["warnings"])

    mapping = package_mapping.object_data_mappings(result)
    assert set(mapping["objects"]["订单"]["fields"]) == {"ORDER_ID", "AMOUNT"}


def test_unused_table_is_warned(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"], "T_USER": ["USER_ID", "NAME"]})
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source="T_ORDER")
    result = package_mapping.resolve_object_tables(root)
    assert any("T_USER" in w and "没有任何对象绑定" in w for w in result["warnings"])


def test_rewrite_object_body_replaces_section_and_drops_guidance(tmp_path):
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID"]})
    body = (
        "# 对象描述\n订单。\n\n"
        "# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 订单号 | ORDER_ID | 编号 | 是 | 字符串 | 单号 | 唯一 |\n\n"
        "> **「属性规则」填写指引** —— 作者指引不应进发布包\n\n"
        "# 关联对象\n- 当前对象 → 属于 → [[客户]]\n"
    )
    fields = package_mapping.parse_object_doc(body)["fields"]
    out = package_mapping.rewrite_object_body(body, fields)
    assert "# 字段" in out and "# 对象属性清单" not in out
    assert "填写指引" not in out
    assert "| ORDER_ID | 订单号 | 字符串 | 是 | 编号 | 单号 | 唯一 |" in out
    # 其他段落原样保留
    assert "# 对象描述" in out and "[[客户]]" in out


def test_no_source_tables_binds_nothing(tmp_path):
    """没有 eval_data/schema 资料时不报错、不瞎绑——纯概念建模阶段的正常状态。"""
    root = tmp_path / "ws"
    (root / "ontology" / "object").mkdir(parents=True)
    write_object(root, "订单", [("订单号", "ORDER_ID")])
    result = package_mapping.resolve_object_tables(root)
    assert result["tables"] == {} and result["bound"] == 0
    assert not result["problems"]
    assert package_mapping.object_data_mappings(result)["objects"] == {}
