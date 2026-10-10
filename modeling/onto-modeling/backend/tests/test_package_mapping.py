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
    assert relations == {}
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


def test_unbound_endpoints_block_unless_publish_was_degraded():
    """降级发布已确认「这些对象不查库」：端点因此未绑定的关系只记原因，不该再拦住导包。"""
    texts = {
        "object_a": "# 关联对象\n- 当前对象 → references → [[object_b]]",
        "object_b": "# 对象描述\n合成对象",
    }
    # object_b 存在但没绑数据表
    report = {}
    _, skipped = package_mapping.graph_relations(texts, {"object_a"}, report)
    assert len(skipped) == 1 and "未绑定数据表" in skipped[0]
    assert report.get("relations_skipped") == 1 and report.get("relations_unbound") == 0

    degraded = {}
    relations, skipped = package_mapping.graph_relations(
        texts, {"object_a"}, degraded, allow_unbound=True,
    )
    notes = degraded.get("notes") or []
    assert relations == {} and skipped == []
    assert degraded.get("relations_skipped") == 1 and degraded.get("relations_unbound") == 1
    assert notes and "未绑定数据表" in notes[0]


def test_absent_endpoint_blocks_even_when_degraded():
    """端点根本不存在是文档要改，降级确认与它无关，仍然阻断。"""
    texts = {"object_a": "# 关联对象\n- 当前对象 → references → [[missing]]"}
    relations, skipped = package_mapping.graph_relations(
        texts, {"object_a"}, allow_unbound=True,
    )
    assert relations == {}
    assert len(skipped) == 1 and "不存在" in skipped[0]


@pytest.mark.parametrize("line", [
    "- 当前对象 → broken → [[object_b]] → extra",
    "- 当前对象 → broken（a → b → c） → [[object_b]]",
    "- 当前对象 → broken（a → b → [[object_b]]",
    "- 当前对象 → bad/name → [[object_b]]",
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


def test_incoming_relations_keep_direction_and_conflicts_are_visible():
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
    # 两端都写了关联字段且不是同一对：无从判断该连哪一对，整条不导出并指出另一处声明
    assert relations == {} and report.get("relations_exported") == 0
    assert len(skipped) == 1
    assert "object_b" in skipped[0] and "第 2 行" in skipped[0]
    assert "对象「object_a」第 2 行" in skipped[0]
    assert "fk → id" in skipped[0] and "other_fk → id" in skipped[0]


@pytest.mark.parametrize("declaring", ["object_a", "object_b"])
def test_relation_declared_on_one_end_only_keeps_its_join_fields(declaring):
    """一端写出关联字段、另一端只写关系名：按已声明的补齐，不是冲突。"""
    def name(key: str) -> str:
        return "references（fk → id）" if key == declaring else "references"

    texts = {
        "object_a": f"# 关联对象\n- 当前对象 → {name('object_a')} → [[object_b]]",
        "object_b": f"# 关联对象\n- [[object_a]] → {name('object_b')} → 当前对象",
    }
    report = {}
    relations, skipped = package_mapping.graph_relations(texts, set(texts), report)
    assert skipped == []
    assert report.get("relations_exported") == 1 and report.get("relations_duplicates") == 1
    edge = relations["object_a"][0]
    assert (edge["sourceJoinProperty"], edge["targetJoinProperty"]) == ("fk", "id")


def test_empty_relation_sections_are_valid():
    for value in ("", "无", "无（没有关联）", "> 填写指导"):
        assert package_mapping.graph_relations({"object_a": "# 关联对象\n" + value}, {"object_a"}) == ({}, [])


def test_studio_uses_the_same_relation_label_with_nested_arrow():
    from app.services import studio

    line = "- 当前对象 → references（fk → id） → [[object_b]]"
    assert studio._object_relation_label(line, "object_a", "object_b") == "references"
