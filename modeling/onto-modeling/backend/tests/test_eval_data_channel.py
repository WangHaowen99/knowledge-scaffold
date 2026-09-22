"""评测数据通道测试（方案 eval-optimization-integration P1）：
三方对账脚本、mock→评测种子转换器、后端对账端点。

脚本走真实 subprocess 执行（与 agent/平台同一路径）。
"""

import json
import subprocess
from pathlib import Path

import openpyxl
import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


def make_workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    (ws / "resource" / "excel").mkdir(parents=True)
    (ws / "ontology" / "object").mkdir(parents=True)
    (ws / ".claude").symlink_to(Path(config.ASSETS_DIR) / "workspace-toolkit")

    # schema 资料（csv：文件名即表名，首行即字段）
    (ws / "resource" / "excel" / "订单表.csv").write_text(
        "order_id,customer_id,total_amount\nx,y,1\n", encoding="utf-8"
    )
    # 对象属性清单
    (ws / "ontology" / "object" / "订单.md").write_text(
        "# 对象描述\n订单。\n\n# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 |\n| --- | --- | --- |\n"
        "| 订单ID | order_id | 编号 |\n| 金额 | total_amount | 元 |\n",
        encoding="utf-8",
    )
    # 评测数据表（xlsx：一个匹配列 + 一个未知列）
    wb = openpyxl.Workbook()
    ws_sheet = wb.active
    ws_sheet.title = "订单"
    ws_sheet.append(["order_id", "total_amount", "unknown_col"])
    ws_sheet.append(["o1", 12, "x"])
    wb.save(ws / "resource" / "excel" / "评测数据.xlsx")

    (ws / "resource" / "materials.json").write_text(
        json.dumps(
            {
                "resource/excel/订单表.csv": {"purpose": "schema", "reading_mode": "on_demand"},
                "resource/excel/评测数据.xlsx": {"purpose": "eval_data", "reading_mode": "on_demand"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return ws


def run_script(ws: Path, name: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["python3", str(SCRIPTS / name), *args],
        cwd=ws, capture_output=True, text=True, timeout=60,
    )


def test_check_eval_data_reports_three_way_alignment(tmp_path):
    ws = make_workspace(tmp_path)
    proc = run_script(ws, "check_eval_data.py", "--json")
    data = json.loads(proc.stdout)
    assert data["status"] == "warn"
    assert data["schema_columns"] == 3
    assert data["object_attributes"] == 2
    entry = data["files"][0]
    assert entry["path"] == "resource/excel/评测数据.xlsx"
    sheet = entry["sheets"][0]
    assert sheet["schema_matched"] == ["order_id", "total_amount"]
    assert sheet["data_only"] == ["unknown_col"]  # schema 中不存在
    assert "unknown_col" in sheet["attr_unmatched"]  # 未映射到对象属性
    assert proc.returncode == 1  # 有差异

    # 修掉未知列 → 通过
    wb = openpyxl.Workbook()
    sh = wb.active
    sh.title = "订单"
    sh.append(["order_id", "total_amount"])
    sh.append(["o1", 12])
    wb.save(ws / "resource" / "excel" / "评测数据.xlsx")
    proc = run_script(ws, "check_eval_data.py", "--json")
    data = json.loads(proc.stdout)
    assert data["status"] == "ok" and proc.returncode == 0


def test_check_eval_data_without_schema(tmp_path):
    ws = make_workspace(tmp_path)
    (ws / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/评测数据.xlsx": {"purpose": "eval_data"}}, ensure_ascii=False),
        encoding="utf-8",
    )
    import shutil

    shutil.rmtree(ws / "ontology" / "object")
    proc = run_script(ws, "check_eval_data.py", "--json")
    data = json.loads(proc.stdout)
    assert data["status"] == "no_schema"
    assert proc.returncode == 0  # 无可对账不算失败


def test_mock_to_eval_seed_builds_workbook(tmp_path):
    ws = make_workspace(tmp_path)
    mock = ws / "ontology" / "data-mock"
    mock.mkdir(parents=True)
    (mock / "订单.md").write_text(
        "# 订单 测试数据\n\n| order_id | total_amount |\n|---|---|\n| o1 | 20 |\n| o2 | 35 |\n",
        encoding="utf-8",
    )
    (mock / "index.md").write_text("# 目录\n", encoding="utf-8")
    proc = run_script(ws, "mock_to_eval_seed.py")
    assert proc.returncode == 0, proc.stderr
    out = ws / "tmp" / "eval-seed.xlsx"
    assert out.exists()
    wb = openpyxl.load_workbook(out)
    assert wb.sheetnames == ["订单"]
    rows = list(wb["订单"].iter_rows(values_only=True))
    assert rows[0] == ("order_id", "total_amount")
    assert len(rows) == 3


def test_eval_data_purpose_accepted_by_workspace(tmp_path, monkeypatch):
    from app.services.workspace import WorkspaceManager

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project("评测数据用途")
    saved = manager.save_resource(
        meta["id"], "excel", "数据.csv", b"a,b\n1,2\n",
        purpose="eval_data", reading_mode="on_demand",
    )
    directives = manager.resource_directives(meta["id"])
    assert directives[saved["path"]]["purpose"] == "eval_data"
    with pytest.raises(Exception):
        manager.set_resource_directive(meta["id"], saved["path"], "not-a-purpose")
