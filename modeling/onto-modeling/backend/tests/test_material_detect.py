"""表格资料的数据表识别：Sheet 结构判定、上传预填 schema、资料区疑似数据表候选。"""

import io
from pathlib import Path

import openpyxl
import pytest

from app.services import material_detect
from app.services.material_detect import classify_sheet
from app.services.workspace import WorkspaceManager


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _xlsx(sheets: dict[str, list[list]], merge: str = "") -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, rows in sheets.items():
        sheet = workbook.create_sheet(name)
        for row in rows:
            sheet.append(row)
        if merge:
            sheet.merge_cells(merge)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


ORDER_ROWS = [
    ["order_id", "customer_id", "amount", "created_at"],
    ["订单编号", "客户编号", "金额", "创建时间"],
    ["O001", "C001", 120.5, "2026-01-01"],
]
TERM_ROWS = [["术语", "别名", "释义"], ["订单", "单据", "客户提交的购买请求"]]


@pytest.mark.parametrize(
    ("name", "rows", "merged", "expected"),
    [
        ("orders", ORDER_ROWS, False, "table"),
        ("sensor", [["unit_id", "41_M11", "41_M12"], ["U1", 1, 2]], False, "table"),
        ("机场", [["code", "region_code", "国家", "名称"], ["AAA", "X", "甲国", "甲机场"]], False, "maybe"),
        ("字段表", [["字段名", "类型", "说明"], ["order_id", "string", "订单编号"]], False, "dictionary"),
        ("术语表", TERM_ROWS, False, "term"),
        ("glossary", [["term", "definition"], ["order", "a purchase request"]], False, "term"),
        # 只有「名称」「说明」的中文表头是普通表格，不是术语表
        ("商品", [["商品名称", "规格", "说明"], ["甲商品", "500g", "常规款"]], False, "document"),
        ("issues", [["issue_id", "description", "type"], ["I1", "描述", "缺陷"]], False, "table"),
        ("清单", [["api", "service", "param"], ["", "", "name"]], True, "document"),
        ("dup", [["id", "id", "name"]], False, "document"),
        ("__说明__", [["本文件说明"]], False, "meta"),
        ("总览", ORDER_ROWS, False, "table"),
        ("blank", [[None, None]], False, "empty"),
    ],
)
def test_classify_sheet(name, rows, merged, expected):
    assert classify_sheet(name, rows, merged) == expected


def test_detect_data_table_whole_file(tmp_path: Path):
    mixed = tmp_path / "mixed.xlsx"
    mixed.write_bytes(_xlsx({"__说明__": [["说明"]], "orders": ORDER_ROWS, "术语表": TERM_ROWS}))
    tables = tmp_path / "tables.xlsx"
    tables.write_bytes(
        _xlsx({"__说明__": [["说明"]], "orders": ORDER_ROWS, "items": [["item_id", "order_id"]]})
    )
    merged = tmp_path / "merged.xlsx"
    merged.write_bytes(_xlsx({"orders": ORDER_ROWS}, merge="A1:B1"))
    dictionary = tmp_path / "fields.csv"
    dictionary.write_text("字段名,类型,说明\norder_id,string,订单编号\n", encoding="utf-8")
    plain = tmp_path / "orders.csv"
    plain.write_text("order_id,amount\nO001,1\n", encoding="utf-8")

    assert material_detect.detect_data_table(tables) == {"tables": ["orders", "items"]}
    assert material_detect.detect_data_table(plain) == {"tables": ["orders"]}
    assert material_detect.detect_data_table(mixed) is None
    assert material_detect.detect_data_table(merged) is None
    assert material_detect.detect_data_table(dictionary) is None
    broken = tmp_path / "broken.xlsx"
    broken.write_bytes(b"not a workbook")
    assert material_detect.detect_data_table(broken) is None
    assert material_detect.detect_data_table(tmp_path / "doc.md") is None


def test_upload_prefills_schema_for_data_table(manager: WorkspaceManager):
    pid = manager.create_project("上传识别")["id"]
    saved = manager.save_resource(pid, "excel", "订单.xlsx", _xlsx({"orders": ORDER_ROWS}))
    assert saved["detected"] == {"purpose": "schema", "tables": ["orders"]}
    assert manager.resource_directives(pid)[saved["path"]] == {
        "purpose": "schema",
        "reading_mode": "on_demand",
        "auto_detected": True,
        "schema_selection": material_detect.selection_metadata(material_detect.resolve_selection(
            manager.project_root(pid) / saved["path"], {"auto_detected": True})),
    }

    term = manager.save_resource(pid, "excel", "术语.xlsx", _xlsx({"术语表": TERM_ROWS}))
    assert term["detected"] == {"purpose": "term", "tables": ["术语表"]}
    assert manager.resource_directives(pid)[term["path"]] == {
        "purpose": "term",
        "reading_mode": "on_demand",
        "auto_detected": True,
    }

    # 术语表与数据表混在一份文件里：两种用途都不预填
    mixed = manager.save_resource(
        pid, "excel", "混合.xlsx", _xlsx({"orders": ORDER_ROWS, "术语表": TERM_ROWS})
    )
    assert "detected" not in mixed
    assert mixed["path"] not in manager.resource_directives(pid)


def test_upload_respects_explicit_purpose_and_manual_tags(manager: WorkspaceManager):
    pid = manager.create_project("人工优先")["id"]
    data = _xlsx({"orders": ORDER_ROWS})
    explicit = manager.save_resource(pid, "excel", "a.xlsx", data, purpose="evidence")
    assert "detected" not in explicit
    assert manager.resource_directives(pid)[explicit["path"]]["purpose"] == "evidence"

    # 人工改过用途后再替换同名文件：不重判、不覆盖
    manager.set_resource_directive(pid, explicit["path"], "term")
    manager.save_resource(pid, "excel", "a.xlsx", data)
    assert manager.resource_directives(pid)[explicit["path"]] == {
        "purpose": "term",
        "reading_mode": "on_demand",
    }

    # 只勾了全文精读也照样识别
    full = manager.save_resource(pid, "excel", "b.xlsx", data, reading_mode="full")
    directive = manager.resource_directives(pid)[full["path"]]
    assert directive["purpose"] == "schema"
    assert directive["reading_mode"] == "full"
    assert directive["auto_detected"] is True
    assert directive["schema_selection"]["included_sheets"] == ["orders"]


def test_replacing_auto_detected_file_redetects(manager: WorkspaceManager):
    pid = manager.create_project("替换重判")["id"]
    saved = manager.save_resource(pid, "excel", "x.xlsx", _xlsx({"orders": ORDER_ROWS}))
    assert manager.resource_directives(pid)[saved["path"]]["auto_detected"] is True

    manager.save_resource(pid, "excel", "x.xlsx", _xlsx({"术语表": TERM_ROWS}))
    assert manager.resource_directives(pid)[saved["path"]]["purpose"] == "term"

    plain = [["商品名称", "规格", "说明"], ["甲商品", "500g", "常规款"]]
    manager.save_resource(pid, "excel", "x.xlsx", _xlsx({"商品": plain}))
    assert saved["path"] not in manager.resource_directives(pid)


def test_schema_candidates_lists_untagged_data_tables(manager: WorkspaceManager):
    pid = manager.create_project("疑似数据表")["id"]
    root = manager.project_root(pid)
    excel = root / "resource" / "excel"
    excel.mkdir(parents=True, exist_ok=True)
    # 直接落盘模拟升级前已上传、未打标签的资料
    (excel / "orders.xlsx").write_bytes(_xlsx({"orders": ORDER_ROWS}))
    (excel / "terms.xlsx").write_bytes(_xlsx({"术语表": TERM_ROWS}))
    (excel / "tagged.xlsx").write_bytes(_xlsx({"orders": ORDER_ROWS}))
    manager.set_resource_directive(pid, "resource/excel/tagged.xlsx", "evidence")

    assert manager.schema_candidates(pid) == {"resource/excel/orders.xlsx": {"tables": ["orders"]}}

    manager.set_resource_directive(pid, "resource/excel/orders.xlsx", "schema")
    assert manager.schema_candidates(pid) == {}
