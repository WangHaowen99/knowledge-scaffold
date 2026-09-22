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
        json.dumps({"resource/excel/评测数据.xlsx": {"purpose": "eval_data"}}, ensure_ascii=False),
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
