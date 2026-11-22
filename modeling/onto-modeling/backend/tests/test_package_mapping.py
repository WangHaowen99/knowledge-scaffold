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
    workbook.save(root / "resource" / "excel" / "业务数据.xlsx")
    # 资料区的 Excel 由多源连接器转成文本版（F4a），表头从文本版读、不解析原件（F4c）
    from app.evidence.convert import md_table, sheet_marker

    text = root / "resource" / "text" / "excel" / "业务数据.xlsx.md"
    text.parent.mkdir(parents=True)
    text.write_text("\n\n".join(f"{sheet_marker(name)}\n## {name}\n\n{md_table([columns])}"
                                for name, columns in sheets.items()) + "\n", encoding="utf-8")
    (root / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/业务数据.xlsx": {"purpose": "schema"}}, ensure_ascii=False),
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


def test_imported_package_fields_section_binds_columns(tmp_path):
    """导出的知识包使用 # 字段，重新导入后仍应按英文 key 绑定表头。"""
    root = make_workspace(tmp_path, {"企业客户": ["customer_id", "enterprise_name", "industry"]})
    (root / "ontology" / "object" / "企业客户.md").write_text(
        "# 数据来源\n企业客户\n\n# 字段\n\n"
        "| 字段 key | 中文名 | 类型 | 必填 | 业务说明 | 别名 |\n"
        "| --- | --- | --- | --- | --- | --- |\n"
        "| customer_id | 客户ID | 字符串 | 是 | 主键 | 客户编号 |\n"
        "| enterprise_name | 企业名称 | 字符串 | 否 | 名称 | 公司名称 |\n"
        "| industry | 行业 | 枚举 | 否 | 行业 | 行业类型 |\n",
        encoding="utf-8",
    )
    result = package_mapping.resolve_object_tables(root)
    item = result["objects"]["企业客户"]
    assert item["table"] == "企业客户"
    assert item["matched_columns"] == 3
    assert not result["problems"]


def test_new_fields_section_wins_over_legacy_section(tmp_path):
    root = make_workspace(tmp_path, {"T": ["new_id"]})
    (root / "ontology" / "object" / "对象.md").write_text(
        "# 数据来源\nT\n\n"
        "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n| --- | --- | --- | --- | --- |\n"
        "| 旧 | old_id | 旧 | 是 | 字符串 |\n\n"
        "# 字段\n| 字段 key | 中文名 | 类型 | 必填 | 业务说明 |\n| --- | --- | --- | --- | --- |\n"
        "| new_id | 新 | 字符串 | 是 | 新 |\n",
        encoding="utf-8",
    )
    parsed = package_mapping.parse_object_doc((root / "ontology" / "object" / "对象.md").read_text(encoding="utf-8"))
    assert [field["key"] for field in parsed["fields"]] == ["new_id"]


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


def test_inference_skips_tables_without_declared_primary_key(tmp_path):
    """主键列不在表里的表不作推断候选：未声明来源的对象撞上别的表两三列同名属性时不能被错绑。"""
    root = make_workspace(tmp_path, {"T_STOCK": ["TANK_ID", "MONTH", "OPENING"]})
    (root / "ontology" / "object" / "月度汇总.md").write_text(
        "# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
        "| --- | --- | --- | --- | --- | --- | --- |\n"
        "| 记录号 | RECORD_ID | 说明 | 是 | 字符串 | 无 | 无 |\n"
        "| 罐号 | TANK_ID | 说明 | 否 | 字符串 | 无 | 无 |\n"
        "| 月份 | MONTH | 说明 | 否 | 字符串 | 无 | 无 |\n",
        encoding="utf-8",
    )
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["月度汇总"]["table"] == ""
    assert package_mapping.object_data_mappings(result)["objects"] == {}

    # 含主键列的表即使重合列数与别的表并列，也是唯一候选
    tables = {"T_STOCK": ["TANK_ID", "MONTH", "OPENING"], "T_RECORD": ["RECORD_ID", "TANK_ID"]}
    item = package_mapping.resolve_object_tables(root, tables=tables)["objects"]["月度汇总"]
    assert item["table"] == "T_RECORD" and item["matched_by"] == "inferred"
    assert item["primary_key"] == ["RECORD_ID"]


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
        source="{待填写：该对象对应的真实数据表名，即 schema 表格资料里的 Sheet 名，\n逐字相同}",
    )
    # 占位符不当成声明 → 回落到推断，且不报"声明的表不存在"
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["table"] == "T_ORDER"
    assert not result["problems"]

    # 「无」是显式声明不落库：不当表名，也不再按重合度推断
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source="无")
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["table"] == ""
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
    """首行「无」即不绑定：后面的说明行不能被当成表名。"""
    root = make_workspace(tmp_path, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "订单", [("订单号", "ORDER_ID"), ("金额", "AMOUNT")], source=source)
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["订单"]["declared_table"] == ""
    assert result["objects"]["订单"]["table"] == ""
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
    """没有 schema 资料时不报错、不瞎绑——纯概念建模阶段的正常状态。"""
    root = tmp_path / "ws"
    (root / "ontology" / "object").mkdir(parents=True)
    write_object(root, "订单", [("订单号", "ORDER_ID")])
    result = package_mapping.resolve_object_tables(root)
    assert result["tables"] == {} and result["bound"] == 0
    assert not result["problems"]
    assert package_mapping.object_data_mappings(result)["objects"] == {}


def test_relation_parser_ignores_join_arrow_inside_parentheses():
    # 人工合成的关系定义。
    text = (
        "# 关联对象\n"
        "- 当前对象 → 来源事件关联（source_event_id → event_id）→ [[线索事件]]\n"
    )
    assert package_mapping.parse_relation_definitions(text) == [{
        "line": 2, "source": "当前对象", "relation": "来源事件关联", "target": "线索事件",
        "note": "source_event_id → event_id",
    }]
    relations, skipped = package_mapping.graph_relations(
        {"需求": text}, {"需求", "线索事件"}
    )
    assert skipped == []
    assert relations["需求"][0] == {
        "relation": "来源事件关联",
        "target": "线索事件",
        "sourceJoinProperty": "source_event_id",
        "targetJoinProperty": "event_id",
        "note": "source_event_id → event_id",
    }


def test_package_relations_preflight_checks_arrow_and_table_formats_without_name_rules():
    texts = {
        "object_a": (
            "# 关联对象\n"
            "- 当前对象 → 1:N → [[object_b]]\n"
            "- 当前对象 → references → [[missing_object]]\n"
        ),
        "object_b": (
            "# 关系\n\n"
            "| 关系 | 目标 | 说明 |\n"
            "| --- | --- | --- |\n"
            "| reports_to | object_b | 自关联 |\n"
        ),
    }

    relations, problems = package_mapping.package_relations(texts, allow_self_relations=False)

    assert set(relations) == {"object_a"}
    assert relations["object_a"][0]["relation"] == "1:N"
    assert [problem for problem in problems if "不存在" in problem] == [
        "ontology/object/object_a.md 第 3 行：关系端点「missing_object」不存在",
    ]
    assert any("object_b" in problem and "不允许自关联" in problem for problem in problems)


def test_package_relations_merges_only_matching_fields_across_formats():
    texts = {
        "object_a": "# 关联对象\n- 当前对象 → references → [[object_b]]\n"
        + package_mapping.render_relations_section([{
            "relation": "references", "target": "object_b", "sourceJoinProperty": "fk", "targetJoinProperty": "id",
        }]),
        "object_b": "# 关联对象\n- [[object_a]] → references → 当前对象\n",
    }

    relations, problems = package_mapping.package_relations(texts, allow_self_relations=False)

    assert problems == []
    assert list(relations) == ["object_a"]
    assert len(relations["object_a"]) == 2
    assert relations["object_a"][0]["target"] == "object_b"
    assert relations["object_a"][1]["sourceJoinProperty"] == "fk"


@pytest.mark.parametrize(("source_text", "target_text", "duplicate_location"), [
    (
        "# 关联对象\n- 当前对象 → references → [[object_b]]\n"
        "- 当前对象名 ->  references  -> [[object_b|合成别名]]\n",
        "",
        "ontology/object/object_a.md 第 3 行",
    ),
    (
        "# 关系\n| 关系 | 目标 |\n| --- | --- |\n"
        "| references | object_b |\n| references | [[object_b#合成段落]] |\n",
        "",
        "ontology/object/object_a.md 第 5 行",
    ),
    (
        "# 关联对象\n- 当前对象 → references → [[object_b]]\n"
        "# 关系\n| 关系 | 目标 |\n| --- | --- |\n| references | object_b |\n",
        "",
        "ontology/object/object_a.md 第 6 行",
    ),
    (
        "# 关联对象\n- 当前对象 → caf\u00e9 → [[object_b]]\n"
        "- 当前对象 → cafe\u0301 → [[object_b]]\n",
        "",
        "ontology/object/object_a.md 第 3 行",
    ),
])
def test_evaluation_rejects_normalized_duplicates_with_both_locations(
    source_text, target_text, duplicate_location,
):
    # 全合成声明：别名、当前对象占位符、箭头/表格格式及 Unicode 等价写法不应绕过检查。
    _, problems = package_mapping.package_relations(
        {"object_a": source_text, "object_b": target_text},
        allow_self_relations=False, reject_duplicate_relations=True,
    )

    first_line = 4 if source_text.startswith("# 关系") else 2
    assert len(problems) == 1
    assert "重复关系" in problems[0]
    assert duplicate_location in problems[0]
    assert f"首次声明于 ontology/object/object_a.md 第 {first_line} 行" in problems[0]


def test_evaluation_keeps_distinct_directed_relations_and_legacy_names():
    relations, problems = package_mapping.package_relations({
        "object_a": "# 关联对象\n"
        "- 当前对象 → 属于申请 → [[object_b]]\n"
        "- 当前对象 → references → [[object_b]]\n"
        "- 当前对象 → references → [[object_c]]\n",
        "object_b": "# 关联对象\n- 当前对象 → references → [[object_a]]\n",
        "object_c": "",
    }, allow_self_relations=False, reject_duplicate_relations=True)

    assert problems == []
    assert sum(map(len, relations.values())) == 4
    assert relations["object_a"][0]["relation"] == "属于申请"
    assert relations["object_b"][0]["target"] == "object_a"


def test_duplicate_check_normalizes_endpoint_unicode_before_validating_references():
    _, problems = package_mapping.package_relations({
        "object_a": "# 关联对象\n"
        "- 当前对象 → references → [[object_\u00e9]]\n"
        "- 当前对象 → references → [[object_e\u0301|合成别名]]\n",
        "object_\u00e9": "",
    }, allow_self_relations=False, reject_duplicate_relations=True)

    assert len(problems) == 1
    assert "重复关系" in problems[0]
    assert "不存在" not in problems[0]
    assert "ontology/object/object_a.md 第 3 行" in problems[0]
    assert "首次声明于 ontology/object/object_a.md 第 2 行" in problems[0]


def test_each_duplicate_reports_the_first_declaration_not_the_previous_duplicate():
    _, problems = package_mapping.package_relations({
        "object_a": "# 关联对象\n"
        "- 当前对象 → references（fk → id） → [[object_b]]\n"
        "- 当前对象 → references（fk → id） → [[object_b]]\n"
        "- 当前对象 → references（fk -> id） → [[object_b]]\n",
        "object_b": "",
    }, allow_self_relations=False, reject_duplicate_relations=True)

    assert len(problems) == 2
    assert all("重复关系" in problem for problem in problems)
    assert all("首次声明于 ontology/object/object_a.md 第 2 行" in problem for problem in problems)
    assert "object_a.md 第 3 行" in problems[0]
    assert "object_a.md 第 4 行" in problems[1]


@pytest.mark.parametrize("target_first", [False, True])
@pytest.mark.parametrize("note", ["", "（fk → id）"])
def test_relation_declared_once_in_each_endpoint_file_is_one_relation(target_first, note):
    """Issue 419：源、目标两端文件各写一次是同一关系的两端描述，评测和导出都不算重复。"""
    texts = _related_pair(note, ["fk"], ["id"])
    texts["object_b"] = texts["object_b"].replace("→ references →", f"→ references{note} →")
    # 名称不同的关系（如买方、卖方）同样两端各写一次，各自保留
    texts["object_a"] += f"\n- 当前对象 → awarded{note} → [[object_b]]\n"
    texts["object_b"] += f"\n- [[object_a]] → awarded{note} → 当前对象\n"
    if target_first:
        texts = dict(reversed(texts.items()))

    relations, problems = package_mapping.package_relations(
        texts, reject_duplicate_relations=True, strict_fields=True,
    )

    assert problems == []
    assert list(relations) == ["object_a"]
    assert [(item["relation"], item["target"]) for item in relations["object_a"]] == [
        ("references", "object_b"), ("awarded", "object_b"),
    ]
    expected = ("fk", "id") if note else (None, None)
    assert all((item.get("sourceJoinProperty"), item.get("targetJoinProperty")) == expected
               for item in relations["object_a"])


@pytest.mark.parametrize("repeated", ["object_a", "object_b"])
def test_repeat_in_one_endpoint_file_is_still_rejected_at_its_own_lines(repeated):
    # 目标端文件排在前面，确认首次声明取的是同一文件里的那一行
    declarations = (
        ("object_b", "- [[object_a]] → references → 当前对象\n"),
        ("object_a", "- 当前对象 → references → [[object_b]]\n"),
    )
    texts = {}
    for key, line in declarations:
        repeat = 2 if key == repeated else 1
        texts[key] = "# 关联对象\n" + line * repeat

    relations, problems = package_mapping.package_relations(texts, reject_duplicate_relations=True)

    assert sum(map(len, relations.values())) == 1
    assert len(problems) == 1
    assert problems[0].startswith(f"ontology/object/{repeated}.md 第 3 行：重复关系")
    assert problems[0].endswith(f"首次声明于 ontology/object/{repeated}.md 第 2 行")


@pytest.mark.parametrize("graph", [False, True])
def test_same_named_relations_keep_distinct_join_field_pairs(graph):
    texts = _related_pair("（fk → id）", ["fk", "other_fk", "id"], ["id", "other_id"])
    texts["object_a"] += "\n- 当前对象 → references（other_fk → id） → [[object_b]]\n"
    texts["object_b"] = texts["object_b"].replace("→ references →", "→ references（fk → id） →")
    if graph:
        relations, problems = package_mapping.graph_relations(texts, set(texts))
    else:
        relations, problems = package_mapping.package_relations(texts)

    assert problems == []
    assert len(relations["object_a"]) == 2
    assert {(item["sourceJoinProperty"], item["targetJoinProperty"]) for item in relations["object_a"]} == {
        ("fk", "id"), ("other_fk", "id"),
    }


def test_evaluation_rejects_only_matching_join_field_pairs_across_formats():
    texts = _related_pair("（object_a.fk → object_b.id）", ["fk", "other_fk"], ["id"])
    texts["object_b"] = ""
    texts["object_a"] += "\n" + package_mapping.render_relations_section([
        {"relation": "references", "target": "object_b", "sourceJoinProperty": "other_fk", "targetJoinProperty": "id"},
        {"relation": "references", "target": "object_b", "sourceJoinProperty": "fk", "targetJoinProperty": "id"},
    ])

    _, problems = package_mapping.package_relations(texts, reject_duplicate_relations=True)

    assert len(problems) == 1
    assert "重复关系" in problems[0]
    assert "fk → id" in problems[0]
    assert "首次声明于" in problems[0]


def test_strict_relations_accept_self_relations_with_different_join_fields(tmp_path):
    texts = _self_object(
        "- 当前对象 → references（manager_id → employee_id） → 当前对象\n"
        "- 当前对象 → references（employee_id → manager_id） → 当前对象\n"
    )
    directory = tmp_path / "ontology" / "object"
    directory.mkdir(parents=True)
    for key, text in texts.items():
        (directory / f"{key}.md").write_text(text, encoding="utf-8")

    assert package_mapping.read_object_texts(tmp_path, strict=True) == texts
    _, problems = package_mapping.package_relations(texts, reject_duplicate_relations=True)
    assert problems == []


def test_standard_relation_table_keeps_graph_join_fields_and_evidence():
    texts = {
        "object_a": package_mapping.render_relations_section([{
            "relation": "references", "target": "object_b", "sourceJoinProperty": "fk", "targetJoinProperty": "id",
            "note": "synthetic note", "evidence": "synthetic evidence",
        }]),
        "object_b": "# 对象描述\n合成对象 B\n",
    }

    relations, problems = package_mapping.graph_relations(texts, set(texts))

    assert problems == []
    assert relations["object_a"][0] == {
        "relation": "references", "target": "object_b", "sourceJoinProperty": "fk", "targetJoinProperty": "id",
        "note": "synthetic note", "evidence": "synthetic evidence",
    }


@pytest.mark.parametrize("table", [
    "| 说明 | 引用 |\n| --- | --- |\n| synthetic | object_b |\n",
    "| 关系 | 目标 |\n| --- | --- |\n| references | |\n",
])
def test_malformed_relation_table_is_not_treated_as_no_relations(table):
    _, problems = package_mapping.package_relations({"object_a": "# 关系\n" + table})

    assert len(problems) == 1
    assert "ontology/object/object_a.md" in problems[0]
    assert "缺少" in problems[0]


def test_standard_same_named_relations_keep_different_join_fields():
    relation = {"relation": "references", "target": "object_b", "sourceJoinProperty": "fk", "targetJoinProperty": "id"}
    text = package_mapping.render_relations_section([
        relation, {**relation, "sourceJoinProperty": "other_fk"},
    ])

    relations, problems = package_mapping.package_relations(
        {"object_a": text, "object_b": ""}, reject_duplicate_relations=True,
    )

    assert problems == []
    assert len(relations["object_a"]) == 2


def _related_pair(note: str, source_fields: list[str], target_fields: list[str]) -> dict:
    """两个互指的合成对象：括注写法由参数给出，属性清单决定能否认出关联字段。"""
    def doc(fields: list[str], line: str) -> str:
        rows = "".join(f"| {key} | {key} | 合成 | 否 | 字符串 |\n" for key in fields)
        return (
            "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n"
            "| --- | --- | --- | --- | --- |\n" + rows
            + "\n# 关联对象\n" + line
        )

    return {
        "object_a": doc(source_fields, f"- 当前对象 → references{note} → [[object_b]]"),
        "object_b": doc(target_fields, "- [[object_a]] → references → 当前对象"),
    }


@pytest.mark.parametrize(("note", "expected"), [
    # 带箭头：按方向取两侧字段
    ("（fk → id）", ("fk", "id")),
    # 裸字段名、夹在自由文本里的字段名：都靠属性清单认出来，不依赖措辞
    ("（shared）", ("shared", "shared")),
    ("（shared 贯通）", ("shared", "shared")),
    ("（通过 shared 关联）", ("shared", "shared")),
    # 括注里没有一个词能在两端属性清单里找到：就是一句纯说明
    ("（内部编号）", (None, None)),
    ("（1:N）", (None, None)),
    ("", (None, None)),
])
def test_join_fields_are_resolved_by_property_lists(note, expected):
    texts = _related_pair(note, ["fk", "shared", "内部编号"], ["id", "shared"])
    relations, skipped = package_mapping.graph_relations(texts, set(texts))
    assert skipped == []
    edge = relations["object_a"][0]
    assert (edge["sourceJoinProperty"], edge["targetJoinProperty"]) == expected


@pytest.mark.parametrize(("note", "message"), [
    # 两端属性清单都有 shared 和 both，无从判断该连哪一对
    ("（shared 或 both 皆可）", "关联字段不明确"),
    # 箭头两侧写了字段，但目标对象属性清单里没有
    ("（fk → missing）", "不在两端对象的属性清单里"),
])
def test_ambiguous_or_absent_join_fields_are_reported(note, message):
    texts = _related_pair(note, ["fk", "shared", "both"], ["id", "shared", "both"])
    relations, skipped = package_mapping.graph_relations(texts, set(texts))
    assert len(relations["object_a"]) == 1
    assert relations["object_a"][0]["sourceJoinProperty"] is None
    assert len(skipped) == 1 and message in skipped[0]


def test_unfilled_template_relation_lines_are_not_diagnostics():
    """照模板新建但还没填写的对象：占位行是"未声明"，不该当成被丢掉的声明报错。"""
    text = (
        "# 关联对象\n"
        "- 当前对象 → {待填写：关系名} → [[{待填写：对象A}]]\n"
        "- [[{待填写：对象B}]] → {待填写：关系名} → 当前对象\n"
    )
    assert package_mapping.parse_relation_definitions(text) == []
    assert package_mapping.graph_relations({"object_a": text}, {"object_a"}) == ({}, [])


@pytest.mark.parametrize("name", ["references", "关联", "关联（补充说明）", "关联（id 贯通）"])
def test_relation_names_and_notes_are_separated(name):
    relations, skipped = package_mapping.graph_relations(
        {"object_a": f"# 关联对象\n- 当前对象 → {name} → [[object_b]]"},
        {"object_a", "object_b"},
    )
    assert not skipped
    assert len(relations["object_a"]) == 1
    assert "（" not in relations["object_a"][0]["relation"]


def test_unbound_endpoints_are_skipped_with_reason():
    """概念对象数据来源写「无」是正常结论：连到它的关系只跳过并记原因，不需要降级确认。"""
    texts = {
        "object_a": "# 关联对象\n- 当前对象 → references → [[object_b]]",
        "object_b": "# 对象描述\n合成对象",
    }
    # object_b 存在但没绑数据表
    report = {}
    relations, skipped = package_mapping.graph_relations(texts, {"object_a"}, report)
    assert relations == {}
    assert len(skipped) == 1 and "未绑定数据表，本条关系不导入" in skipped[0]
    assert report.get("relations_skipped") == 1 and report.get("relations_unbound") == 1
    assert report.get("notes") == skipped


@pytest.mark.parametrize("source", ["无", "无（合成概念对象，不落库）"])
def test_relations_to_concept_objects_are_noted_as_concepts(source):
    """「# 数据来源」显式写「无」的概念对象图里本来就没有节点：跳过原因点明是概念对象，不当成绑定出错。"""
    texts = {
        "object_a": "# 关联对象\n- 当前对象 → references（{待确认：合成说明}）→ [[concept_b]]",
        "concept_b": f"# 数据来源\n{source}\n> 合成概念对象\n\n# 关联对象\n- 当前对象 → triggers → [[concept_c]]",
        "concept_c": "# 数据来源\n无",
    }
    report = {}
    relations, skipped = package_mapping.graph_relations(texts, {"object_a"}, report)
    assert relations == {}
    assert len(skipped) == 2 and all("概念对象" in note for note in skipped)
    assert report.get("relations_skipped") == report.get("relations_unbound") == 2
    assert report.get("notes") == skipped


def test_unbound_endpoint_next_to_a_concept_object_is_not_called_concept():
    """没写数据来源的端点说明的是绑定出了问题；同一条关系里另一端是概念对象也不能按概念对象提示。"""
    texts = {
        "concept_a": "# 数据来源\n无\n\n# 关联对象\n- 当前对象 → references → [[object_b]]",
        "object_b": "# 对象描述\n合成对象",
    }
    _, skipped = package_mapping.graph_relations(texts, set())
    assert len(skipped) == 1 and "未绑定数据表" in skipped[0]
    assert "概念对象" not in skipped[0]


def test_absent_endpoint_is_reported():
    texts = {"object_a": "# 关联对象\n- 当前对象 → references → [[missing]]"}
    report = {}
    relations, skipped = package_mapping.graph_relations(texts, {"object_a"}, report)
    assert relations == {}
    assert len(skipped) == 1 and "不存在" in skipped[0]
    assert report.get("relations_unbound") == 0


def _self_object(line: str) -> dict:
    return {"employee": (
        "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| 员工编号 | employee_id | 合成 | 是 | 字符串 |\n"
        "| 上级编号 | manager_id | 合成 | 否 | 字符串 |\n"
        "\n# 关联对象\n" + line
    )}


def test_self_relation_is_preserved_with_or_without_join_fields():
    """自关联本身合法；显式字段对和同一行关联都可以导出。"""
    relations, skipped = package_mapping.graph_relations(
        _self_object("- 当前对象 → 上级（manager_id → employee_id） → 当前对象"), {"employee"},
    )
    assert skipped == []
    assert relations["employee"][0]["target"] == "employee"
    assert (relations["employee"][0]["sourceJoinProperty"],
            relations["employee"][0]["targetJoinProperty"]) == ("manager_id", "employee_id")

    relations, skipped = package_mapping.graph_relations(
        _self_object("- 当前对象 → 上级 → 当前对象"), {"employee"},
    )
    assert skipped == []
    assert relations["employee"][0]["target"] == "employee"
    assert relations["employee"][0]["sourceJoinProperty"] is None


@pytest.mark.parametrize("line", [
    "- 当前对象 → broken → [[object_b]] → extra",
    "- 当前对象 → broken（a → b → c） → [[object_b]]",
    "- 当前对象 → broken（a → b → [[object_b]]",
    "- 当前对象 → bad\\name → [[object_b]]",
    "- 当前对象 → references → [[missing]]",
])
def test_bad_relation_declarations_have_line_diagnostics(line):
    report = {}
    relations, skipped = package_mapping.graph_relations(
        {"object_a": "# 对象描述\n合成对象\n# 关联对象\n" + line},
        {"object_a", "object_b"}, report,
    )
    assert relations == {}
    assert len(skipped) == 1 and "第 4 行" in skipped[0]
    assert report.get("relations_declared") == report.get("relations_skipped") == 1


def test_incoming_relations_keep_direction_and_distinct_join_fields():
    report = {}
    texts = {
        "object_a": "# 关联对象\n- 当前对象 → references（fk -> id） → [[object_b]]",
        "object_b": "# 关联对象\n- [[object_a]] → references（fk -> id） → 当前对象",
    }
    relations, skipped = package_mapping.graph_relations(texts, set(texts), report)
    assert not skipped and set(relations) == {"object_a"}
    assert len(relations["object_a"]) == 1
    assert report.get("relations_duplicates") == 1
    texts["object_b"] = texts["object_b"].replace("fk -> id", "other_fk -> id")
    relations, skipped = package_mapping.graph_relations(texts, set(texts), report)
    assert skipped == [] and report.get("relations_exported") == 2
    assert {(edge["sourceJoinProperty"], edge["targetJoinProperty"]) for edge in relations["object_a"]} == {
        ("fk", "id"), ("other_fk", "id"),
    }


@pytest.mark.parametrize("declaring", ["object_a", "object_b"])
def test_implicit_and_explicit_join_declarations_remain_distinct(declaring):
    """空字段对与显式字段对不是同一关系，不猜字段或合并声明。"""
    def name(key: str) -> str:
        return "references（fk → id）" if key == declaring else "references"

    texts = {
        "object_a": f"# 关联对象\n- 当前对象 → {name('object_a')} → [[object_b]]",
        "object_b": f"# 关联对象\n- [[object_a]] → {name('object_b')} → 当前对象",
    }
    report = {}
    relations, skipped = package_mapping.graph_relations(texts, set(texts), report)
    assert skipped == []
    assert report.get("relations_exported") == 2 and report.get("relations_duplicates") == 0
    edge = next(item for item in relations["object_a"] if item["sourceJoinProperty"])
    assert (edge["sourceJoinProperty"], edge["targetJoinProperty"]) == ("fk", "id")


def test_empty_relation_sections_are_valid():
    for value in ("", "无", "无（没有关联）", "> 填写指导"):
        assert package_mapping.graph_relations({"object_a": "# 关联对象\n" + value}, {"object_a"}) == ({}, [])


def test_studio_uses_the_same_relation_label_with_nested_arrow():
    from app.services import studio

    line = "- 当前对象 → references（fk → id） → [[object_b]]"
    assert studio._object_relation_label(line, "object_a", "object_b") == "references"


@pytest.mark.parametrize("placeholder", ["当前对象名", "[[当前对象名]]"])
def test_literal_current_object_name_placeholder_means_current_object(placeholder):
    """旧版抽取模板把「当前对象」写成了「当前对象名」，照抄进文档的关系不能在发布时静默丢掉。"""
    texts = _related_pair("（fk → id）", ["fk"], ["id"])
    texts["object_b"] = texts["object_b"].replace("→ references →", "→ references（fk → id） →")
    texts = {key: text.replace("当前对象", placeholder) for key, text in texts.items()}
    report = {}
    relations, skipped = package_mapping.graph_relations(texts, set(texts), report)
    assert skipped == []
    assert report.get("relations_exported") == 1 and report.get("relations_duplicates") == 1
    edge = relations["object_a"][0]
    assert (edge["target"], edge["sourceJoinProperty"], edge["targetJoinProperty"]) == ("object_b", "fk", "id")


def test_studio_treats_current_object_name_placeholder_as_current_object():
    from app.services import studio

    line = "- 当前对象名 → references（fk → id） → [[object_b]]"
    assert studio._object_relation_label(line, "object_a", "object_b") == "references"
    incoming = "- [[object_b]] → references → 当前对象名"
    assert studio._object_relation_label(incoming, "object_a", "object_b") == "references"
    assert studio._target_points_to_current(incoming, "object_a", "object_b")


def _table_pair(note: str) -> dict:
    """两端各自声明了数据表的合成对象：括注里的字段可带表名前缀。"""
    def doc(table: str, fields: list[str], line: str) -> str:
        rows = "".join(f"| {key} | {key} | 合成 | 否 | 字符串 |\n" for key in fields)
        return (
            f"# 数据来源\n{table}\n\n"
            "# 对象属性清单\n| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n"
            "| --- | --- | --- | --- | --- |\n" + rows
            + "\n# 关联对象\n" + line
        )

    return {
        "订单明细": doc("order_line", ["line_id", "product_code"], f"- 当前对象 → 包含{note} → [[商品]]"),
        "商品": doc("product", ["product_code", "product_name"], "- [[订单明细]] → 包含 → 当前对象"),
    }


@pytest.mark.parametrize("note", [
    "（order_line.product_code → product.product_code）",
    "（`order_line`.`product_code` → product . product_code）",
    # 前缀写对象名同样认得
    "（订单明细.product_code → 商品.product_code）",
    # 只有一侧带前缀
    "（product_code → product.product_code）",
])
def test_join_fields_with_table_prefix_are_accepted(note):
    texts = _table_pair(note)
    relations, skipped = package_mapping.graph_relations(texts, set(texts))
    assert skipped == []
    edge = relations["订单明细"][0]
    assert (edge["sourceJoinProperty"], edge["targetJoinProperty"]) == ("product_code", "product_code")


@pytest.mark.parametrize("note", [
    # 前缀是另一端的表：方向写反了，按前缀剥掉会连错方向
    "（product.product_code → order_line.product_code）",
    # 多级前缀、一侧多个字段：仍然取不出唯一的一对
    "（db.order_line.product_code → product.product_code）",
    "（order_line.product_code line_id → product_code）",
])
def test_ambiguous_prefixed_join_fields_are_still_rejected(note):
    texts = _table_pair(note)
    relations, skipped = package_mapping.graph_relations(texts, set(texts))
    assert len(relations["订单明细"]) == 1
    assert relations["订单明细"][0]["sourceJoinProperty"] is None
    assert len(skipped) == 1 and "关联字段" in skipped[0]


@pytest.mark.parametrize("source", ["无", "无（输出实体，不落库）", "—", "N/A"])
def test_explicit_none_source_skips_overlap_inference(tmp_path, source):
    """「# 数据来源」写「无」是显式声明不落库：列名再像某张表也不能按重合度绑上去。"""
    root = make_workspace(tmp_path, {"summary_ledger": ["summary_no", "customer_name", "summary_date"]})
    write_object(
        root, "汇总单",
        [("单据编号", "summary_no"), ("客户名称", "customer_name"), ("汇总日期", "summary_date")],
        source=source,
    )
    result = package_mapping.resolve_object_tables(root)
    item = result["objects"]["汇总单"]
    assert item["table"] == "" and item["matched_by"] == ""
    assert not result["problems"]
    # 没写「# 数据来源」的对象照旧按重合度推断
    write_object(
        root, "汇总单",
        [("单据编号", "summary_no"), ("客户名称", "customer_name"), ("汇总日期", "summary_date")],
    )
    assert package_mapping.resolve_object_tables(root)["objects"]["汇总单"]["matched_by"] == "inferred"
