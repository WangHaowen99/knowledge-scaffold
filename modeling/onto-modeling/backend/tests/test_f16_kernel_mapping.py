"""F16 内核：对象文档「# 数据来源」写数据库锚点 `db:<源>/<库>.<表>`，按最新完整快照绑定真实表名、列名（package_mapping）。"""

from __future__ import annotations

import json
from pathlib import Path

from app.services import package_mapping
from tests.f13_support import DB_SOURCE, db_anchor, db_records
from tests.test_package_mapping import make_workspace, write_object


def _snapshot(root: Path, snapshot_id: str, records: list[dict], *, complete: bool = True,
              operation: str = "snapshot", source: str = DB_SOURCE) -> None:
    folder = root / "resource" / "systems" / source / snapshot_id
    (folder / "records").mkdir(parents=True)
    (folder / "manifest.json").write_text(json.dumps({"format": "evidence-snapshot/1", "complete": complete,
                                                      "operation": operation, "source": {"id": source}}),
                                          encoding="utf-8")
    by_type: dict[str, list[str]] = {}
    for record in records:
        by_type.setdefault(record["type"], []).append(json.dumps(record, ensure_ascii=False))
    for kind, lines in by_type.items():
        (folder / "records" / f"{kind}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def _bare(tmp_path: Path) -> Path:
    """没有任何表格资料的工作区。"""
    root = tmp_path / "ws"
    (root / "ontology" / "object").mkdir(parents=True)
    return root


def _workspace(tmp_path: Path, sheets: dict[str, list[str]] | None = None) -> Path:
    root = make_workspace(tmp_path, sheets) if sheets else _bare(tmp_path)
    _snapshot(root, "20261105-080000-0001", db_records())
    return root


def test_anchor_binds_real_table_and_columns(tmp_path):
    root = _workspace(tmp_path)
    write_object(root, "客户", [("客户号", "id"), ("名称", "NAME"), ("电话", "phone"), ("邮箱", "email")],
                 source=db_anchor("crm", "customer"))
    write_object(root, "订单", [("订单号", "id"), ("金额", "amount")], source=f"`{db_anchor('crm', 'orders')}`")
    result = package_mapping.resolve_object_tables(root)
    customer = result["objects"]["客户"]
    assert customer["table"] == "customer" and customer["matched_by"] == "declared"
    assert [field["column"] for field in customer["fields"]] == ["id", "name", "phone", ""]
    assert customer["missing_columns"] == ["email"] and customer["source_anchor"] == db_anchor("crm", "customer")
    assert result["objects"]["订单"]["table"] == "orders" and result["problems"] == []
    assert not any("数据表「customer」" in warning for warning in result["warnings"])

    mappings = package_mapping.object_data_mappings(result)["objects"]
    assert mappings["客户"]["resource"] == "customer"
    assert mappings["客户"]["sources"][0] == {"role": "primary", "resource": "customer", "fields": {
        "id": {"source_column": "id"}, "NAME": {"source_column": "name"}, "phone": {"source_column": "phone"}}}
    graph = package_mapping.object_data_mappings(result, graph=True)["objects"]
    assert graph["客户"]["resource"] == "客户" and graph["客户"]["sources"][0]["resource"] == "customer"


def test_anchor_missing_in_snapshot_is_declared_missing(tmp_path):
    root = _workspace(tmp_path)
    write_object(root, "客户", [("客户号", "id"), ("电话", "phone")], source=db_anchor("crm", "client"))
    write_object(root, "货品", [("别的", "nothing")], source=db_anchor("stock", "item"))
    write_object(root, "列", [("客户号", "id")], source=db_anchor("crm", "customer", "id"))
    result = package_mapping.resolve_object_tables(root)
    issues = {issue["object"]: issue for issue in result["issues"]}
    assert issues["客户"]["kind"] == "declared_missing" and issues["客户"]["blocking"]
    assert issues["客户"]["candidates"][0]["table"] == "customer"
    assert issues["客户"]["declared"] == db_anchor("crm", "client")
    assert issues["货品"]["kind"] == "no_columns" and issues["货品"]["table"] == "item"
    assert issues["列"]["kind"] == "declared_missing"  # 指到列不算表
    assert len(result["problems"]) == 3
    assert all(item["table"] == "" for item in result["objects"].values())
    # 降级发布：只提示不阻断
    relaxed = package_mapping.resolve_object_tables(root, strict_declared=False)
    assert relaxed["problems"] == [] and len(relaxed["issues"]) == 3


def test_latest_complete_snapshot_wins(tmp_path):
    root = _workspace(tmp_path)
    newer = [record for record in db_records() if not record["id"].startswith(db_anchor("crm", "customer"))]
    _snapshot(root, "20261105-090000-0001", newer)
    # 更新的补充样本不完整，不算
    _snapshot(root, "20261105-100000-0001", db_records(), complete=False, operation="sample")
    write_object(root, "客户", [("客户号", "id")], source=db_anchor("crm", "customer"))
    issue = package_mapping.resolve_object_tables(root)["issues"][0]
    assert issue["kind"] == "declared_missing"


def test_without_snapshot_behaviour_is_unchanged(tmp_path):
    root = _bare(tmp_path)
    write_object(root, "客户", [("客户号", "id")], source=db_anchor("crm", "customer"))
    result = package_mapping.resolve_object_tables(root)
    assert result["issues"][0]["kind"] == "declared_unverifiable" and result["problems"] == []

    with_sheets = make_workspace(tmp_path / "b", {"T_CUSTOMER": ["id", "name"]})
    write_object(with_sheets, "客户", [("客户号", "id")], source=db_anchor("crm", "customer"))
    write_object(with_sheets, "顾客", [("客户号", "id"), ("名称", "name")], source="T_CUSTOMER")
    sheet = package_mapping.resolve_object_tables(with_sheets)
    assert sheet["issues"][0]["kind"] == "declared_missing" and "现有表：T_CUSTOMER" in sheet["issues"][0]["message"]
    assert sheet["objects"]["顾客"]["table"] == "T_CUSTOMER"
    # 别的源有快照不影响这个源
    _snapshot(with_sheets, "20261105-080000-0001", db_records(source="other-db"), source="other-db")
    assert package_mapping.resolve_object_tables(with_sheets)["issues"][0]["message"] == sheet["issues"][0]["message"]


def test_anchor_and_sheet_objects_coexist_and_relation_prefix(tmp_path):
    root = _workspace(tmp_path, {"T_SUPPLIER": ["SUPPLIER_ID", "TITLE"]})
    write_object(root, "客户", [("客户号", "id"), ("名称", "name")], source=db_anchor("crm", "customer"))
    write_object(root, "供应商", [("编号", "SUPPLIER_ID"), ("名称", "TITLE")], source="T_SUPPLIER")
    result = package_mapping.resolve_object_tables(root)
    assert result["objects"]["客户"]["table"] == "customer" and result["objects"]["供应商"]["table"] == "T_SUPPLIER"
    assert result["bound"] == 2 and result["problems"] == []
    doc = package_mapping.parse_object_doc((root / "ontology/object/客户.md").read_text(encoding="utf-8"))
    assert "customer" in package_mapping._relation_prefixes("客户", doc)
