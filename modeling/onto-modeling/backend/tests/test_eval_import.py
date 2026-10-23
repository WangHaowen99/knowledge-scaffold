"""评测数据一键导入：规范化工作簿、日期处理、mapping 与导入库一致性、Neo4j 图导入编排。"""

import datetime as dt
import io
import json
import secrets
import threading
import zipfile
from http.server import HTTPServer
from pathlib import Path

import openpyxl
import pytest

from app import config
from app.services import eval_data_files, eval_data_sources, eval_import, modeling_eval, package_mapping
from app.services.workspace import WorkspaceError, WorkspaceManager
from tests.test_modeling_eval import StubModeling

# 测试密钥运行时生成，代码里不写凭据样式的字面量
FAKE_PASSWORD = secrets.token_hex(8)

CUSTOMER_DOC = """# 数据来源
客户表

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |
| --- | --- | --- | --- | --- |
| 客户编号 | CUSTOMER_ID | 合成客户编号 | 是 | 字符串 |
| 客户名称 | NAME | 合成客户名称 | 否 | 字符串 |
"""

ORDER_DOC = """# 数据来源
订单表

# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |
| --- | --- | --- | --- | --- |
| 订单号 | ORDER_ID | 合成订单号 | 是 | 字符串 |
| 客户编号 | CUSTOMER_ID | 下单客户 | 否 | 字符串 |
| 签约日 | SIGN_DATE | 签约日期 | 否 | 日期 |
| 账期 | BILL_MONTH | 结算月份 | 否 | 字符串 |

# 关联对象
- 当前对象 → 属于 → [[客户]]
"""


def _workbook(path: Path, sheets: dict[str, list[list]]) -> None:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(path)


def _upload(root: Path, name: str, sheets: dict[str, list[list]]) -> str:
    """模拟数据源表单上传一份 Excel，返回文件 ID。"""
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for sheet_name, rows in sheets.items():
        sheet = workbook.create_sheet(sheet_name)
        for row in rows:
            sheet.append(row)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return eval_data_files.save(root, name, buffer.getvalue())["id"]


def _tag(root: Path, purposes: dict[str, str]) -> None:
    path = root / "resource" / "materials.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    current = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    current.update({rel: {"purpose": purpose} for rel, purpose in purposes.items()})
    path.write_text(json.dumps(current, ensure_ascii=False), encoding="utf-8")


def _objects(root: Path) -> None:
    object_dir = root / "ontology" / "object"
    object_dir.mkdir(parents=True, exist_ok=True)
    (object_dir / "客户.md").write_text(CUSTOMER_DOC, encoding="utf-8")
    (object_dir / "订单.md").write_text(ORDER_DOC, encoding="utf-8")


def _business_data(root: Path) -> str:
    return _upload(root, "业务数据.xlsx", {
        "客户表": [["CUSTOMER_ID", " NAME "], ["C1", "合成客户甲"], ["C2", "合成客户乙"]],
        "订单表": [
            ["ORDER_ID", "CUSTOMER_ID", "SIGN_DATE", "BILL_MONTH", None, "ORDER_TIME"],
            ["O1", "C1", dt.datetime(2026, 1, 5), "2026-01", "孤立值", "2026/1/5 8:05"],
            ["O2", "C2", "2026年2月28日", "2026-02", None, "2026-02-28 23:59:59"],
        ],
    })


# ---- 规范化 ----


def test_dates_are_normalized_and_partial_dates_stay_text(tmp_path):
    _objects(tmp_path)
    file_id = _business_data(tmp_path)
    plan = eval_import.collect(tmp_path, {"files": [file_id]})
    assert not plan["problems"], plan["problems"]
    orders = next(t for t in plan["tables"] if t.name == "订单表")
    # 空表头列丢弃，表头去空白
    assert orders.columns == ["ORDER_ID", "CUSTOMER_ID", "SIGN_DATE", "BILL_MONTH", "ORDER_TIME"]
    assert [r[2] for r in orders.rows] == ["2026-01-05", "2026-02-28"]
    assert [r[4] for r in orders.rows] == ["2026-01-05 08:05:00", "2026-02-28 23:59:59"]
    assert [r[3] for r in orders.rows] == ["2026-01", "2026-02"]  # 年月保持文本
    assert orders.date_columns == ["SIGN_DATE", "ORDER_TIME"]
    assert orders.text_date_columns == ["BILL_MONTH"]
    assert any("列「BILL_MONTH」" in w and "按文本导入" in w for w in plan["warnings"])
    assert any("没有列名" in w for w in plan["warnings"])
    customers = next(t for t in plan["tables"] if t.name == "客户表")
    assert customers.columns == ["CUSTOMER_ID", "NAME"]


def test_invalid_calendar_date_keeps_column_as_text(tmp_path):
    table = eval_import.Table("t", ["D"], [["2026-02-30"], [dt.datetime(2026, 3, 1)]], "x")
    eval_import.normalize_dates(table)
    assert table.date_columns == []
    assert [r[0] for r in table.rows] == ["2026-02-30", "2026-03-01"]


def test_note_row_and_empty_sheet_follow_modeling(tmp_path):
    _objects(tmp_path)
    file_id = _upload(tmp_path, "带注释.xlsx", {
        "订单表": [
            ["ORDER_ID", "CUSTOMER_ID", "SIGN_DATE"],
            ["订单编号（主键）", "客户：外键", "签约日期（年月日）"],
            ["O1", "C1", "2026-01-05"],
            ["O2", "C2", "2026-01-06"],
        ],
        "空表": [["A", "B"]],
    })
    plan = eval_import.collect(tmp_path, {"files": [file_id]})
    orders = next(t for t in plan["tables"] if t.name == "订单表")
    assert len(orders.rows) == 2 and orders.date_columns == ["SIGN_DATE"]
    assert "空表" not in [t.name for t in plan["tables"]]
    assert any("没有数据行" in w for w in plan["warnings"])


def test_blocking_problems(tmp_path):
    _objects(tmp_path)
    first = _upload(tmp_path, "a.xlsx", {"订单表": [["ORDER_ID", "order_id"], ["O1", "O1"]]})
    second = _upload(tmp_path, "b.xlsx", {"订单表": [["ORDER_ID"], ["O2"]]})
    plan = eval_import.collect(tmp_path, {"files": [first, second, "0123456789ab", "../外部"]})
    text = "\n".join(plan["problems"])
    assert "重复列名" in text
    assert "同名表「订单表」" in text
    assert text.count("文件不存在") == 2  # 已删除的 ID + 非法 ID


def test_upload_rejects_non_excel_and_oversize(tmp_path, monkeypatch):
    with pytest.raises(eval_data_files.DataFileError, match="仅支持 Excel"):
        eval_data_files.save(tmp_path, "c.csv", b"a\n1\n")
    with pytest.raises(eval_data_files.DataFileError, match="无效的文件名"):
        eval_data_files.save(tmp_path, ".xlsx", b"x")
    monkeypatch.setattr(eval_data_files, "MAX_FILE_BYTES", 4)
    with pytest.raises(eval_data_files.DataFileError, match="不能超过"):
        eval_data_files.save(tmp_path, "big.xlsx", b"12345")
    # 路径分量被剥掉，只留文件名；文件落在工作区状态目录、不在资料区
    entry = eval_data_files.save(tmp_path, "../../逃逸.xlsx", b"PK")
    assert entry["name"] == "逃逸.xlsx"
    assert eval_data_files.path_of(tmp_path, entry["id"]).parent.parent == tmp_path / ".ontology-state" / "eval-data"


# ---- mapping 与导入库一致 ----


def test_mapping_follows_imported_data_not_schema_resource(tmp_path):
    """schema 资料比本轮数据多一列：默认绑定会把多出的列写进 mapping，导入后库里没有。"""
    _objects(tmp_path)
    _workbook(tmp_path / "resource/schema.xlsx", {
        "订单表": [["ORDER_ID", "CUSTOMER_ID", "SIGN_DATE", "BILL_MONTH"]],
    })
    _tag(tmp_path, {"resource/schema.xlsx": "schema"})
    file_id = _upload(tmp_path, "eval.xlsx", {
        "订单表": [["ORDER_ID", "CUSTOMER_ID"], ["O1", "C1"]],
    })

    default = package_mapping.resolve_object_tables(tmp_path)
    assert default["objects"]["订单"]["fields"][3]["column"] == "BILL_MONTH"  # 取到了 schema 的列

    plan = eval_import.collect(tmp_path, {"files": [file_id]})
    bound = {f["key"]: f["column"] for f in plan["binding"]["objects"]["订单"]["fields"]}
    assert bound == {"ORDER_ID": "ORDER_ID", "CUSTOMER_ID": "CUSTOMER_ID", "SIGN_DATE": "", "BILL_MONTH": ""}
    # 客户声明的表不在本轮数据里：只提示，不阻断，也不进 mapping
    assert any("客户表" in w for w in plan["warnings"])
    assert not plan["binding"]["objects"]["客户"]["table"]


def test_collect_accepts_reimported_package_fields_section(tmp_path):
    """评测导入使用重新导入知识包后的标准 # 字段章节。"""
    object_dir = tmp_path / "ontology" / "object"
    object_dir.mkdir(parents=True, exist_ok=True)
    (object_dir / "企业客户.md").write_text(
        "# 数据来源\n企业客户\n\n# 字段\n\n"
        "| 字段 key | 中文名 | 类型 | 必填 | 业务说明 |\n"
        "| --- | --- | --- | --- | --- |\n"
        "| customer_id | 客户ID | 字符串 | 是 | 主键 |\n"
        "| enterprise_name | 企业名称 | 字符串 | 否 | 名称 |\n",
        encoding="utf-8",
    )
    file_id = _upload(tmp_path, "data.xlsx", {
        "企业客户": [["customer_id", "enterprise_name"], ["C1", "合成企业"]],
    })
    plan = eval_import.collect(tmp_path, {"files": [file_id]})
    assert not plan["problems"], plan["problems"]
    assert plan["binding"]["bound"] == 1
    assert plan["binding"]["objects"]["企业客户"]["matched_columns"] == 2


def _export(tmp_path, monkeypatch, binding: dict, graph: bool):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    # 本组仅测试包与数据转换，不依赖交互式建模的资产符号链接。
    monkeypatch.setattr(manager, "_mount_assets", lambda root: None)
    meta = manager.create_project("导出核对")
    root = manager.project_root(meta["id"])
    return manager, meta["id"], root


def test_export_matches_import_workbook_nl2sql_and_graph(tmp_path, monkeypatch):
    manager, pid, root = _export(tmp_path, monkeypatch, {}, False)
    _objects(root)
    file_id = _business_data(root)
    plan = eval_import.prepare(root, {"files": [file_id]}, graph=True)

    # 工作簿回读：Sheet 与表头即 mapping 引用的表和列
    workbook = openpyxl.load_workbook(io.BytesIO(plan["workbook"]))
    assert workbook.sheetnames == ["客户表", "订单表"]
    assert [c.value for c in workbook["订单表"][1]] == [
        "ORDER_ID", "CUSTOMER_ID", "SIGN_DATE", "BILL_MONTH", "ORDER_TIME",
    ]
    assert workbook["订单表"]["C2"].value == "2026-01-05"

    for graph in (False, True):
        zip_path = tmp_path / f"pkg-{graph}.zip"
        manager.export_package_zip(pid, zip_path, binding=plan["binding"], graph=graph)
        assert eval_import.verify_mapping(zip_path, plan["tables"]) == []
        with zipfile.ZipFile(zip_path) as archive:
            mapping = json.loads(archive.read(f"{pid}/mappings/object-data-mappings.json"))
            order_doc = archive.read(f"{pid}/objects/订单.md").decode("utf-8")
        order = mapping["objects"]["订单"]
        assert order["sources"][0]["resource"] == "订单表"
        if graph:
            # 图模式：resource 是对象 key，fields 覆盖全部属性，关联对象转成 # 关系
            assert order["resource"] == "订单"
            assert set(order["fields"]) == {"ORDER_ID", "CUSTOMER_ID", "SIGN_DATE", "BILL_MONTH"}
            assert "resource: \"订单\"" in order_doc
            assert "# 关系" in order_doc and "| 属于 | 客户 |" in order_doc
        else:
            assert order["resource"] == "订单表"
            assert "# 关系" in order_doc and "| 属于 | 客户 |" in order_doc
            assert "# 关联对象" not in order_doc


def test_mock_headers_translated_and_bound_by_object(tmp_path):
    _objects(tmp_path)
    mock = tmp_path / "ontology" / "data-mock"
    mock.mkdir(parents=True)
    (mock / "订单.md").write_text(
        "# 订单 测试数据\n\n| 订单号 | 客户编号 | 签约日 | 备注 |\n|---|---|---|---|\n"
        "| O1 | C1 | 2026/1/5 | 无 |\n| O2 | C1 | 2026/1/6 | 无 |\n",
        encoding="utf-8",
    )
    plan = eval_import.collect(tmp_path, {"include_mock": True})
    assert not plan["problems"], plan["problems"]
    table = plan["tables"][0]
    assert table.name == "订单表"  # 对象声明的来源表
    assert table.columns == ["ORDER_ID", "CUSTOMER_ID", "SIGN_DATE", "备注"]
    assert table.date_columns == ["SIGN_DATE"]
    order = plan["binding"]["objects"]["订单"]
    assert order["table"] == "订单表" and order["matched_columns"] == 3
    assert any("备注" in w for w in plan["warnings"])


def test_non_graph_export_preserves_checked_relations_without_table_bindings(tmp_path, monkeypatch):
    manager, pid, root = _export(tmp_path, monkeypatch, {}, False)
    snapshot = {
        "object_a": "# 对象描述\n合成对象 A\n",
        "object_b": "# 关联对象\n- [[object_a]] → references → 当前对象\n",
    }
    # 检查后工作区被修改；打包必须继续使用已检查的对象内容。
    object_dir = root / "ontology" / "object"
    object_dir.mkdir(parents=True, exist_ok=True)
    (object_dir / "object_a.md").write_text(
        "# 关联对象\n- 当前对象 → references → [[missing_object]]\n", encoding="utf-8",
    )
    destination = io.BytesIO()

    manager.export_package_zip(
        pid, destination, binding={"objects": {}, "problems": []}, object_texts=snapshot,
    )

    with zipfile.ZipFile(destination) as archive:
        object_a = archive.read(f"{pid}/objects/object_a.md").decode("utf-8")
        object_b = archive.read(f"{pid}/objects/object_b.md").decode("utf-8")
    assert "| references | object_b |" in object_a
    assert "missing_object" not in object_a
    assert "# 关系" not in object_b
    assert "# 关联对象" not in object_b


def test_export_rewrites_standard_relation_table_once_without_losing_metadata(tmp_path, monkeypatch):
    manager, pid, root = _export(tmp_path, monkeypatch, {}, False)
    snapshot = {
        "object_a": package_mapping.render_relations_section([{
            "relation": "references", "target": "object_b", "note": "synthetic note",
            "evidence": "synthetic evidence", "sourceJoinProperty": "fk", "targetJoinProperty": "id",
        }]),
        "object_b": "# 对象描述\n合成对象 B\n",
    }
    destination = io.BytesIO()

    manager.export_package_zip(
        pid, destination, binding={"objects": {}, "problems": []}, object_texts=snapshot,
    )

    with zipfile.ZipFile(destination) as archive:
        doc = archive.read(f"{pid}/objects/object_a.md").decode("utf-8")
    assert doc.count("# 关系") == 1
    assert "| references | object_b | synthetic note | synthetic evidence | fk | id |" in doc


def test_mock_without_data_mock_dir_is_a_problem(tmp_path):
    _objects(tmp_path)
    plan = eval_import.collect(tmp_path, {"include_mock": True})
    assert any("示例数据种子生成失败" in p and "data-mock" in p for p in plan["problems"])


def test_export_preserves_checked_object_name_metadata(tmp_path, monkeypatch):
    from app.services.document_metadata import report_name

    manager, pid, root = _export(tmp_path, monkeypatch, {}, False)
    snapshot = {
        "object_a": '---\nreport_name: "synthetic_a/b"\n---\n'
        "# 关联对象\n- 当前对象 → references → [[object_b]]\n",
        "object_b": "# 对象描述\n合成对象 B\n",
    }
    destination = io.BytesIO()

    manager.export_package_zip(
        pid, destination, binding={"objects": {}, "problems": []}, object_texts=snapshot,
    )

    with zipfile.ZipFile(destination) as archive:
        doc = archive.read(f"{pid}/objects/object_a.md").decode("utf-8")
    assert report_name(doc) == "synthetic_a/b"
    assert doc.count("report_name:") == 1
    assert "| references | object_b |" in doc


def test_mock_table_without_object_doc_keeps_headers(tmp_path):
    _objects(tmp_path)
    mock = tmp_path / "ontology" / "data-mock"
    mock.mkdir(parents=True)
    (mock / "仓库.md").write_text(
        "# 仓库 测试数据\n\n| 仓库号 |\n|---|\n| W1 |\n", encoding="utf-8"
    )
    tables, _hints, warnings = eval_import._mock_tables(tmp_path)
    assert tables[0].name == "仓库" and tables[0].columns == ["仓库号"]
    assert any("找不到对应的对象文档" in w for w in warnings)


def test_no_bound_object_blocks(tmp_path):
    _objects(tmp_path)
    file_id = _upload(tmp_path, "x.xlsx", {"别的表": [["A", "B"], [1, 2]]})
    plan = eval_import.collect(tmp_path, {"files": [file_id]})
    assert any("没有任何对象绑定" in p for p in plan["problems"])


def _sentence_sources(root: Path) -> None:
    """把两个对象的「# 数据来源」首行改写成说明句，表名只在反引号里出现。"""
    object_dir = root / "ontology" / "object"
    for key, table in (("客户", "客户表"), ("订单", "订单表")):
        doc = object_dir / f"{key}.md"
        doc.write_text(
            doc.read_text(encoding="utf-8").replace(
                f"# 数据来源\n{table}\n", f"# 数据来源\n- 数据库表 `{table}` 的全部列（合成数据）。\n"
            ),
            encoding="utf-8",
        )


def test_sentence_style_sources_still_bind_and_unmatched_table_does_not_block(tmp_path):
    _objects(tmp_path)
    _sentence_sources(tmp_path)
    (tmp_path / "ontology" / "object" / "月报.md").write_text(
        "# 数据来源\n- 期初值来自 `客户表`。\n\n# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |\n| --- | --- | --- | --- | --- |\n"
        "| 月份 | MONTH | 合成月份 | 是 | 字符串 |\n",
        encoding="utf-8",
    )
    plan = eval_import.collect(tmp_path, {"files": [_business_data(tmp_path)]})
    assert not plan["problems"], plan["problems"]
    objects = plan["binding"]["objects"]
    assert objects["客户"]["table"] == "客户表" and objects["订单"]["table"] == "订单表"
    assert objects["月报"]["table"] == ""
    assert any("首行不是表名" in w for w in plan["warnings"])
    assert any("月报" in w and "没有一列能对上" in w for w in plan["warnings"])


def test_mock_sheet_named_by_quoted_table_in_sentence_source(tmp_path):
    _objects(tmp_path)
    _sentence_sources(tmp_path)
    mock = tmp_path / "ontology" / "data-mock"
    mock.mkdir(parents=True)
    (mock / "订单.md").write_text(
        "# 订单 测试数据\n\n| 订单号 | 客户编号 |\n|---|---|\n| O1 | C1 |\n", encoding="utf-8"
    )
    plan = eval_import.collect(tmp_path, {"include_mock": True})
    assert not plan["problems"], plan["problems"]
    assert plan["tables"][0].name == "订单表"
    assert plan["binding"]["objects"]["订单"]["table"] == "订单表"


def test_graph_problems_flag_invalid_property_names(tmp_path):
    _objects(tmp_path)
    doc = tmp_path / "ontology" / "object" / "客户.md"
    doc.write_text(CUSTOMER_DOC.replace("| NAME |", "| CUSTOMER-NAME |"), encoding="utf-8")
    file_id = _upload(tmp_path, "x.xlsx", {"客户表": [["CUSTOMER_ID", "CUSTOMER-NAME"], ["C1", "甲"]]})
    plan = eval_import.collect(tmp_path, {"files": [file_id]})
    assert any("CUSTOMER-NAME" in p for p in eval_import.graph_problems(plan["binding"]))
    with pytest.raises(eval_import.EvalImportError, match="Neo4j 属性名"):
        eval_import.prepare(tmp_path, {"files": [file_id]}, graph=True)


def test_formula_like_text_stays_text():
    data = eval_import.workbook_bytes([eval_import.Table("t", ["A"], [["=1+1"], ["0012"]], "x")])
    sheet = openpyxl.load_workbook(io.BytesIO(data))["t"]
    assert sheet["A2"].value == "=1+1" and sheet["A2"].data_type == "s"
    assert sheet["A3"].value == "0012"


# ---- 向导预览 ----


def test_preview_reports_entities_instances_and_relation_rows(tmp_path):
    """图导入预览给出实体数、实例数（按主键）与匹配后的关系条数。"""
    _objects(tmp_path)
    file_id = _business_data(tmp_path)

    graph = eval_import.preview(tmp_path, {"files": [file_id]}, graph=True)
    assert not graph["problems"], graph["problems"]
    # 客户表 2 行 + 订单表 2 行，主键都完整 → 4 个实例
    assert graph["entities"] == {"count": 2, "instances": 4}
    assert graph["objects"]["客户"]["instances"] == 2
    # 两张订单各按 CUSTOMER_ID 连到一个客户 → 2 条关系（不是"1 种关系"）
    assert graph["relations"] == {
        "count": 1,
        "instances": 2,
        "items": [{"source": "订单", "relation": "属于", "target": "客户", "instances": 2}],
    }

    # 非图模式落的是 MySQL 评测库，不给实体/实例/关系这套图口径的数字
    nl2sql = eval_import.preview(tmp_path, {"files": [file_id]}, graph=False)
    assert "entities" not in nl2sql and "relations" not in nl2sql
    assert "instances" not in nl2sql["objects"]["客户"]


def test_preview_relation_rows_count_every_matched_pair(tmp_path):
    """一个客户下挂多张订单：关系条数按匹配出的行对算，不是按关系种类算。"""
    _objects(tmp_path)
    file_id = _upload(tmp_path, "many.xlsx", {
        "客户表": [["CUSTOMER_ID", "NAME"], ["C1", "合成甲"], ["C2", "合成乙"]],
        "订单表": [["ORDER_ID", "CUSTOMER_ID"], ["O1", "C1"], ["O2", "C1"], ["O3", "C2"],
                 ["O4", "C9"], ["O5", None]],
    })
    result = eval_import.preview(tmp_path, {"files": [file_id]}, graph=True)
    assert result["entities"] == {"count": 2, "instances": 7}
    assert result["relations"]["instances"] == 3  # O1、O2 → C1，O3 → C2
    assert any("1 行关联值匹配不到目标实例" in w for w in result["warnings"])  # O4 → C9
    assert any("1 行关联字段为空" in w for w in result["warnings"])  # O5


def test_preview_instances_skip_blank_keys_and_block_duplicates(tmp_path):
    """实例数按 modeling 建点规则算：主键为空的行不算，主键重复直接拦下。"""
    _objects(tmp_path)
    blank = _upload(tmp_path, "blank.xlsx", {
        "客户表": [["CUSTOMER_ID", "NAME"], ["C1", "合成甲"], ["C2", "合成乙"], [None, "合成丙"]],
    })
    result = eval_import.preview(tmp_path, {"files": [blank]}, graph=True)
    assert result["entities"]["instances"] == 2
    assert any("1 行主键为空" in w for w in result["warnings"])

    duplicated = _upload(tmp_path, "dup.xlsx", {
        "客户表": [["CUSTOMER_ID", "NAME"], ["C1", "合成甲"], ["C1", "合成乙"]],
    })
    result = eval_import.preview(tmp_path, {"files": [duplicated]}, graph=True)
    assert any("主键重复" in p for p in result["problems"])


def test_preview_counts_mirrored_declarations_once(tmp_path):
    """同一条关系在两端各声明一次：算一条，并按写出关联字段的那一端补齐。"""
    _objects(tmp_path)
    customer = tmp_path / "ontology" / "object" / "客户.md"
    customer.write_text(
        CUSTOMER_DOC + "\n# 关联对象\n- [[订单]] → 属于（CUSTOMER_ID → CUSTOMER_ID） → 当前对象\n",
        encoding="utf-8",
    )
    file_id = _business_data(tmp_path)
    result = eval_import.preview(tmp_path, {"files": [file_id]}, graph=True)
    assert not result["problems"], result["problems"]
    assert result["relations"]["count"] == 1 and result["relations"]["instances"] == 2


def test_preview_separates_no_relations_from_no_matching_data(tmp_path):
    """「有节点没关系」的两种无害情形都不阻断评测，但要能分清是哪一种。"""
    # 声明正确、本轮数据一条都匹配不上：只提示，不拦
    _objects(tmp_path)
    unmatched = _upload(tmp_path, "unmatched.xlsx", {
        "客户表": [["CUSTOMER_ID", "NAME"], ["C1", "合成甲"]],
        "订单表": [["ORDER_ID", "CUSTOMER_ID"], ["O1", "C9"]],
    })
    result = eval_import.preview(tmp_path, {"files": [unmatched]}, graph=True)
    assert not result["problems"], result["problems"]
    assert result["relations"]["count"] == 1 and result["relations"]["instances"] == 0
    assert any("匹配不出任何关系" in w for w in result["warnings"])
    # 发起评测同样不被拦下
    eval_import.prepare(tmp_path, {"files": [unmatched]}, graph=True)

    # 压根没声明关系：关系数为 0，也没有「匹配不出」这类提示
    (tmp_path / "ontology" / "object" / "订单.md").write_text(
        ORDER_DOC.split("# 关联对象")[0], encoding="utf-8",
    )
    silent = eval_import.preview(tmp_path, {"files": [unmatched]}, graph=True)
    assert not silent["problems"] and silent["relations"]["count"] == 0
    assert not any("匹配不出任何关系" in w for w in silent["warnings"])


def test_preview_skips_relation_that_cannot_be_imported(tmp_path):
    """关系端点没绑数据表只影响这一条关系：预览照常可导入，并提示这条关系不导入。"""
    _objects(tmp_path)
    file_id = _upload(tmp_path, "订单.xlsx", {
        "订单表": [["ORDER_ID", "CUSTOMER_ID"], ["O1", "C1"]],
    })
    result = eval_import.preview(tmp_path, {"files": [file_id]}, graph=True)
    assert result["relations"] == {"count": 0, "instances": 0, "items": []}
    assert not any("未绑定数据表" in p for p in result["problems"])
    assert any("未绑定数据表" in w for w in result["warnings"])


# ---- 图导入关系关联 ----


def test_graph_relation_mapping_inference():
    upload = {
        "sheets": [
            {"name": "客户表", "columns": ["CUSTOMER_ID", "NAME"]},
            {"name": "订单表", "columns": ["ORDER_ID", "CUSTOMER_ID"]},
            {"name": "宽表", "columns": ["P", "Q"]},
        ],
        "schema": {
            "objects": [
                {"key": "客户", "defaultSheet": "客户表", "primaryKey": ["CUSTOMER_ID"],
                 "propertyMappings": [{"property": "CUSTOMER_ID", "sourceColumn": "CUSTOMER_ID"},
                                      {"property": "NAME", "sourceColumn": "NAME"}]},
                {"key": "订单", "defaultSheet": "订单表", "primaryKey": ["ORDER_ID"],
                 "propertyMappings": [{"property": "ORDER_ID", "sourceColumn": "ORDER_ID"},
                                      {"property": "CUSTOMER_ID", "sourceColumn": "CUSTOMER_ID"}]},
                {"key": "甲", "defaultSheet": "宽表", "primaryKey": ["P"],
                 "propertyMappings": [{"property": "P", "sourceColumn": "P"}]},
                {"key": "乙", "defaultSheet": "宽表", "primaryKey": ["Q"],
                 "propertyMappings": [{"property": "Q", "sourceColumn": "Q"}]},
            ],
            "relations": [
                {"sourceObject": "订单", "relationName": "属于", "targetObject": "客户"},
                {"sourceObject": "客户", "relationName": "拥有", "targetObject": "订单"},
                {"sourceObject": "甲", "relationName": "同行", "targetObject": "乙"},
                {"sourceObject": "甲", "relationName": "无关", "targetObject": "客户"},
            ],
        },
    }
    relations, skipped = eval_import.graph_relation_mapping(upload)
    by_name = {r["relationName"]: r for r in relations}
    assert by_name["属于"]["matchMode"] == "field_match"
    assert by_name["属于"]["sourceJoinProperty"] == by_name["属于"]["targetJoinProperty"] == "CUSTOMER_ID"
    assert by_name["拥有"]["sourceJoinProperty"] == "CUSTOMER_ID"
    assert by_name["同行"] == {
        "sourceObject": "甲", "relationName": "同行", "targetObject": "乙", "matchMode": "same_row",
    }
    assert "无关" not in by_name and "找不到关联字段" in skipped[0]


def test_graph_relation_mapping_uses_explicit_different_join_fields():
    # 人工构造的合成 Schema；不使用环境数据。
    upload = {
        "sheets": [
            {"name": "需求表", "columns": ["REQUEST_ID", "SOURCE_EVENT_ID"]},
            {"name": "事件表", "columns": ["EVENT_ID", "TITLE"]},
        ],
        "schema": {
            "objects": [
                {"key": "需求", "defaultSheet": "需求表", "primaryKey": ["REQUEST_ID"],
                 "propertyMappings": [
                     {"property": "REQUEST_ID", "sourceColumn": "REQUEST_ID"},
                     {"property": "SOURCE_EVENT_ID", "sourceColumn": "SOURCE_EVENT_ID"},
                 ]},
                {"key": "事件", "defaultSheet": "事件表", "primaryKey": ["EVENT_ID"],
                 "propertyMappings": [
                     {"property": "EVENT_ID", "sourceColumn": "EVENT_ID"},
                     {"property": "TITLE", "sourceColumn": "TITLE"},
                 ]},
            ],
            "relations": [{
                "sourceObject": "需求", "relationName": "来源事件关联", "targetObject": "事件",
                "sourceJoinProperty": "SOURCE_EVENT_ID", "targetJoinProperty": "EVENT_ID",
            }],
        },
    }
    relations, skipped = eval_import.graph_relation_mapping(upload)
    assert skipped == []
    assert relations == [{
        "sourceObject": "需求", "relationName": "来源事件关联", "targetObject": "事件",
        "matchMode": "field_match",
        "sourceJoinProperty": "SOURCE_EVENT_ID", "targetJoinProperty": "EVENT_ID",
    }]
    # 显式字段优先于同表同行推断，缺字段时也不能回退到猜测。
    upload["schema"]["objects"][1]["defaultSheet"] = "需求表"
    upload["sheets"][0]["columns"].append("EVENT_ID")
    assert eval_import.graph_relation_mapping(upload)[0] == relations
    upload["schema"]["relations"][0]["targetJoinProperty"] = "missing"
    mapped, skipped = eval_import.graph_relation_mapping(upload)
    assert mapped == [] and "关联字段不存在" in skipped[0]
    upload["schema"]["relations"][0].pop("targetJoinProperty")
    mapped, skipped = eval_import.graph_relation_mapping(upload)
    assert mapped == [] and "只声明了一个关联字段" in skipped[0]


def test_invalid_relation_is_skipped_with_full_diagnostics(tmp_path, monkeypatch):
    """写错的关系只跳过那一条：评测与导包照常进行，诊断留在提示和 relation_report 里。"""
    manager, pid, root = _export(tmp_path, monkeypatch, {}, True)
    _objects(root)
    path = root / "ontology" / "object" / "订单.md"
    path.write_text(ORDER_DOC + "- 当前对象 → 关联/包含 → [[客户]]\n", encoding="utf-8")
    file_id = _business_data(root)
    plan = eval_import.prepare(root, {"files": [file_id]}, graph=True)
    assert any("关联/包含" in w for w in plan["warnings"])
    report = {}
    dest = tmp_path / "skipped.zip"
    manager.export_package_zip(pid, dest, plan["binding"], graph=True, relation_report=report)
    assert dest.exists()
    with zipfile.ZipFile(dest) as archive:
        doc = archive.read(f"{pid}/objects/订单.md").decode("utf-8")
    assert "关联/包含" not in doc.split("# 关系", 1)[-1]
    notes = report.get("notes") or []
    assert report.get("relations_declared") == 2
    assert report.get("relations_exported") == report.get("relations_skipped") == 1
    assert notes and "第 " in notes[0] and "关联/包含" in notes[0]


def test_relations_to_concept_objects_do_not_block_graph_import(tmp_path, monkeypatch):
    manager, pid, root = _export(tmp_path, monkeypatch, {}, True)
    _objects(root)
    (root / "ontology" / "object" / "合成概念.md").write_text(
        "# 数据来源\n无\n> 合成概念对象，不落库\n\n# 关联对象\n- 当前对象 → 涉及 → [[客户]]\n",
        encoding="utf-8",
    )
    plan = eval_import.prepare(root, {"files": [_business_data(root)]}, graph=True)
    assert any("合成概念" in note and "概念对象" in note for note in plan["warnings"])
    report = {}
    manager.export_package_zip(pid, tmp_path / "concept.zip", plan["binding"], graph=True,
                               relation_report=report)
    assert report.get("relations_exported") == 1 and report.get("relations_unbound") == 1
    notes = report.get("notes") or []
    assert notes and "概念对象" in notes[0]


def test_export_preserves_explicit_join_fields(tmp_path, monkeypatch):
    manager, pid, root = _export(tmp_path, monkeypatch, {}, True)
    _objects(root)
    path = root / "ontology" / "object" / "订单.md"
    path.write_text(ORDER_DOC.replace("属于", "属于（CUSTOMER_ID → CUSTOMER_ID）"), encoding="utf-8")
    plan = eval_import.prepare(root, {"files": [_business_data(root)]}, graph=True)
    dest = tmp_path / "joined.zip"
    manager.export_package_zip(pid, dest, plan["binding"], graph=True)
    with zipfile.ZipFile(dest) as archive:
        doc = archive.read(f"{pid}/objects/订单.md").decode("utf-8")
    assert "| 关系 | 目标 | 说明 | 证据 | 源关联字段 | 目标关联字段 |" in doc
    assert "| 属于 | 客户 | CUSTOMER_ID → CUSTOMER_ID |   | CUSTOMER_ID | CUSTOMER_ID |" in doc


class _StopAtValidate(Exception):
    pass


def _upload_then_stop(calls: list[str]):
    def http(method, path, token, **kwargs):
        calls.append(path)
        if path.endswith("/validate"):
            raise _StopAtValidate()
        return {"upload": {"id": "upload_alpha", "schema": {"objects": [], "relations": [
            {"sourceObject": "object_a", "relationName": "references", "targetObject": "object_b"},
        ]}}}
    return http


def test_unmappable_relation_is_skipped_and_import_continues(tmp_path, monkeypatch):
    """映射不出的关系只跳过那一条并留诊断，导入继续往下走（这里在预检处截停）。"""
    calls: list[str] = []
    monkeypatch.setattr(modeling_eval, "_http", _upload_then_stop(calls))
    with pytest.raises(_StopAtValidate):
        modeling_eval._graph_import("project_alpha", tmp_path, lambda: "", "project_alpha", {},
                                    {"tables": [], "workbook": b""})
    assert calls[-1].endswith("/validate")
    state = modeling_eval.read_state(tmp_path)
    assert state["graph_import"]["relations_skipped"] == 1
    assert "端点不存在" in state["graph_import"]["notes"][0]


def test_relations_skipped_at_export_stay_in_graph_import_notes(tmp_path, monkeypatch):
    """导出时跳过的关系与图导入时跳过的一并留诊断，导出侧的排在前面。"""
    calls: list[str] = []
    monkeypatch.setattr(modeling_eval, "_http", _upload_then_stop(calls))
    plan = {"tables": [], "workbook": b"", "relation_report": {
        "relations_exported": 1, "relations_skipped": 1, "notes": ["合成提示：概念对象关系未导入"],
    }}
    with pytest.raises(_StopAtValidate):
        modeling_eval._graph_import("project_alpha", tmp_path, lambda: "", "project_alpha", {}, plan)
    graph_state = modeling_eval.read_state(tmp_path)["graph_import"]
    assert graph_state["relations_skipped"] == 2
    assert graph_state["notes"][0] == "合成提示：概念对象关系未导入"
    assert "端点不存在" in graph_state["notes"][1]


def test_relation_count_mismatch_stops_before_database_write(tmp_path, monkeypatch):
    """导出与图导入读到的关系数对不上是转换缺陷，在写库前停下。"""
    calls: list[str] = []
    monkeypatch.setattr(modeling_eval, "_http", _upload_then_stop(calls))
    plan = {"tables": [], "workbook": b"", "relation_report": {"relations_exported": 0, "notes": []}}
    with pytest.raises(RuntimeError, match="关系定义数不一致"):
        modeling_eval._graph_import("project_alpha", tmp_path, lambda: "", "project_alpha", {}, plan)
    assert len(calls) == 1


def _plan_with_one_object(tmp_path) -> dict:
    """一个对象绑一张表、一条关系的导入计划（只用于编排层对账测试）。"""
    binding = {"objects": {"object_a": {
        "table": "sheet_a",
        "fields": [{"key": "ID", "column": "ID", "type": "string", "primary": True}],
        "primary_key": ["ID"],
        "missing_columns": [],
    }}}
    return {"tables": [], "workbook": b"", "binding": binding}


def _stub_graph_http(validation: dict, calls: list):
    """上传 → 校验 的最小桩；执行阶段被调用说明没拦住。"""
    def http(method, path, token, **kwargs):
        calls.append(path)
        if path.endswith("/uploads"):
            return {"upload": {
                "id": "u1",
                "sheets": [{"name": "sheet_a", "columns": ["ID"]}],
                "schema": {"objects": [
                    {"key": "object_a", "defaultSheet": "sheet_a", "primaryKey": ["ID"],
                     "propertyMappings": [{"property": "ID", "sourceColumn": "ID"}]},
                ], "relations": []},
            }}
        if path.endswith("/validate"):
            return {"validation": validation}
        raise AssertionError(f"不该走到 {path}")
    return http


@pytest.mark.parametrize(("expected", "counts", "message"), [
    ({"expected_instances": 2}, {"nodeCount": 1}, "预览给出 2 个实例"),
    ({"expected_edges": 5}, {"nodeCount": 1, "relationshipCount": 2}, "预览给出 5 条关系"),
])
def test_prediction_mismatch_stops_before_database_write(
    tmp_path, monkeypatch, expected, counts, message,
):
    """预览算出的实例数/关系条数与 modeling 的导入预检不一致：写库前停下，不让数字分叉。"""
    calls = []
    monkeypatch.setattr(modeling_eval, "_http", _stub_graph_http({"counts": counts}, calls))
    plan = {**_plan_with_one_object(tmp_path), **expected}
    with pytest.raises(RuntimeError, match=message):
        modeling_eval._graph_import("project_alpha", tmp_path, lambda: "", "project_alpha", {}, plan)
    assert not any(path.endswith("/execute") for path in calls)
    assert modeling_eval.read_state(tmp_path)["graph_import"]["entities"] == 1


# ---- 编排：Neo4j 一键导入 ----


class GraphStub(StubModeling):
    posts: list = []
    uploaded_package: bytes = b""
    uploaded_workbook: bytes = b""
    executes: list = []

    def do_GET(self):
        StubModeling.calls.append(("GET", self.path))
        if self.path.endswith("/graph-imports"):
            return self._send(200, {"imports": [
                {"id": "g1", "status": "done", "nodeCount": 4, "relationshipCount": 2},
            ]})
        if "/rounds/" in self.path:
            return self._send(200, {
                "round": {"id": "r1", "status": "done", "progressDone": 1, "progressTotal": 1},
                "results": [{"question": "Q1", "verdict": "pass", "reason": "一致", "finalAnswer": "A1"}],
            })
        return StubModeling.do_GET(self)

    def do_PUT(self):
        length = int(self.headers.get("Content-Length") or 0)
        GraphStub.posts.append(("PUT", self.path, json.loads(self.rfile.read(length))))
        return self._send(200, {"dataSource": {}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length)
        GraphStub.posts.append(("POST", self.path, None))
        if self.path == "/api/package-workspaces/import":
            GraphStub.uploaded_package = raw
            return self._send(201, {"project": {"id": "mp-g"}})
        if self.path.endswith("/graph-import/uploads"):
            GraphStub.uploaded_workbook = raw
            return self._send(201, {"upload": {
                "id": "u1",
                "sheets": [{"name": "客户表", "columns": ["CUSTOMER_ID", "NAME"]},
                           {"name": "订单表", "columns": ["ORDER_ID", "CUSTOMER_ID"]}],
                "schema": {
                    "objects": [
                        {"key": "客户", "defaultSheet": "客户表", "primaryKey": ["CUSTOMER_ID"],
                         "propertyMappings": [{"property": "CUSTOMER_ID", "sourceColumn": "CUSTOMER_ID"}]},
                        {"key": "订单", "defaultSheet": "订单表", "primaryKey": ["ORDER_ID"],
                         "propertyMappings": [{"property": "CUSTOMER_ID", "sourceColumn": "CUSTOMER_ID"}]},
                    ],
                    "relations": [{"sourceObject": "订单", "relationName": "属于", "targetObject": "客户"}],
                },
            }})
        if self.path.endswith("/validate"):
            return self._send(200, {"validation": {
                "counts": {"nodeCount": 4, "relationshipCount": 2},
                "warnings": ["提示"],
                "objectDiagnostics": [{"objectKey": "示例", "code": "GRAPH_IMPORT_EMPTY_SHEET",
                                       "message": "来源 Sheet 为空"}],
            }})
        if self.path.endswith("/execute"):
            GraphStub.executes.append(json.loads(raw))
            return self._send(202, {"run": {"id": "g1", "status": "running"}})
        if self.path.endswith("/testcases/batch"):
            return self._send(200, {"added": 1})
        if self.path.endswith("/evaluate"):
            return self._send(200, {"roundId": "r1"})
        return self._send(404, {})


NEO4J = {
    "mode": "connection",
    "provider": "neo4j",
    "config": {
        "uri": "bolt://graph.example.com:7687", "username": "reader", "password": FAKE_PASSWORD,
        "workspace_property": "d2o_workspace_id", "workspace_value": "graph-eval",
        "ontology_property": "d2o_ontology_id",
    },
}


def test_neo4j_one_click_import_orchestration(tmp_path, monkeypatch):
    GraphStub.posts, GraphStub.executes = [], []
    server = HTTPServer(("127.0.0.1", 0), GraphStub)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(config, "MODELING_URL", f"http://127.0.0.1:{server.server_port}")
        monkeypatch.setattr(modeling_eval, "POLL_INTERVAL_SECONDS", 0.05)
        monkeypatch.setattr(modeling_eval, "_stop_requested", set())
        manager, pid, root = _export(tmp_path, monkeypatch, {}, True)
        _objects(root)
        file_id = _business_data(root)
        with pytest.raises(eval_data_sources.DataSourceError, match="Ontology 隔离字段"):
            eval_data_sources.normalize({
                **NEO4J, "files": [file_id],
                "config": {**NEO4J["config"], "ontology_property": ""},
            }, root=root)
        source = eval_data_sources.create_source(pid, {**NEO4J, "files": [file_id]}, root)
        assert source["name"] == "Neo4j · graph-eval（导入 1 份数据）"

        modeling_eval._run_sync(pid, root, "t", [{"question": "Q1", "groundTruth": "G1"}], source)
        state = modeling_eval.read_state(root)
        assert state["status"] == "done", state
        order = [p for _m, p, _b in GraphStub.posts]
        assert order.index("/api/projects/mp-g/data-source") \
            < order.index("/api/projects/mp-g/graph-import/uploads") \
            < order.index("/api/projects/mp-g/graph-import/uploads/u1/execute") \
            < order.index("/api/projects/mp-g/evaluate")
        execute = GraphStub.executes[0]
        assert execute["password"] == FAKE_PASSWORD and execute["replaceExisting"] is True
        assert execute["mapping"]["relations"][0]["sourceJoinProperty"] == "CUSTOMER_ID"
        graph_state = state["graph_import"]
        # 预览算的实例数/关系条数 == 导入预检 == 实际写入，三者一致才会走到这里
        assert graph_state["expected_instances"] == graph_state["nodes"] == 4
        assert graph_state["expected_edges"] == graph_state["relationships"] == 2
        assert graph_state["entities"] == 2
        assert "提示" in graph_state["notes"]
        # 预检给出的对象诊断只作提示，不拦运行（计数一致已经保证数字不会分叉）
        assert any("来源 Sheet 为空" in note for note in graph_state["notes"])
        assert FAKE_PASSWORD not in json.dumps(state, ensure_ascii=False)
        # 上传给 modeling 的包是图模式 mapping；上传的工作簿是规范化后的那份
        assert b"eval-data.xlsx" in GraphStub.uploaded_workbook
        start = GraphStub.uploaded_package.index(b"PK")
        with zipfile.ZipFile(io.BytesIO(GraphStub.uploaded_package[start:])) as archive:
            mapping = json.loads(archive.read(f"{pid}/mappings/object-data-mappings.json"))
        assert mapping["objects"]["订单"]["resource"] == "订单"
    finally:
        server.shutdown()
