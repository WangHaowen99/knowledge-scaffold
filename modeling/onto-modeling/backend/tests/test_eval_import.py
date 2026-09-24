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
from app.services.workspace import WorkspaceManager
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
- 当前对象 → 关联/包含 → [[客户]]
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


def _export(tmp_path, monkeypatch, binding: dict, graph: bool):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
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
            assert "关联/包含" not in order_doc.split("# 关系")[1]  # 非法关系名不进关系表
        else:
            assert order["resource"] == "订单表"
            assert "# 关系" not in order_doc


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


def test_mock_without_data_mock_dir_is_a_problem(tmp_path):
    _objects(tmp_path)
    plan = eval_import.collect(tmp_path, {"include_mock": True})
    assert any("示例数据种子生成失败" in p and "data-mock" in p for p in plan["problems"])


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
                "results": [{"question": "Q1", "verdict": "pass", "reason": "一致"}],
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
            return self._send(200, {"validation": {"counts": {"nodeCount": 4}, "warnings": ["提示"]}})
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
        assert state["graph_import"]["nodes"] == 4 and "提示" in state["graph_import"]["notes"]
        assert FAKE_PASSWORD not in json.dumps(state, ensure_ascii=False)
        # 上传给 modeling 的包是图模式 mapping；上传的工作簿是规范化后的那份
        assert b"eval-data.xlsx" in GraphStub.uploaded_workbook
        start = GraphStub.uploaded_package.index(b"PK")
        with zipfile.ZipFile(io.BytesIO(GraphStub.uploaded_package[start:])) as archive:
            mapping = json.loads(archive.read(f"{pid}/mappings/object-data-mappings.json"))
        assert mapping["objects"]["订单"]["resource"] == "订单"
    finally:
        server.shutdown()
