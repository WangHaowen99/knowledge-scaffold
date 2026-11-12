"""I1 审查修复第 2 条：资料区里有坏的 xlsx / docx 时，上传（R-02）、预览（R-06）、出处（EV-29）、资料地图与核验（R-07、R-09）都不 500，按读不了 / 不可核验处理。"""

import io
import zipfile
from pathlib import Path

import openpyxl
import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, streams
from app.services.workspace import WorkspaceManager
from tests import proc


def _good_xlsx() -> bytes:
    wb = openpyxl.Workbook()
    wb.active["A1"] = "编号"
    wb.active["B1"] = "名称"
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _rewrite(data: bytes, name: str, content: bytes | None) -> bytes:
    src = zipfile.ZipFile(io.BytesIO(data))
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w") as dst:
        for info in src.infolist():
            if info.filename == name:
                if content is not None:
                    dst.writestr(name, content)
            else:
                dst.writestr(info, src.read(info))
    return out.getvalue()


CASES = {
    "bad-workbook-xml.xlsx": lambda: _rewrite(_good_xlsx(), "xl/workbook.xml", b"<workbook><sheets><sheet"),
    "bad-sheet-xml.xlsx": lambda: _rewrite(_good_xlsx(), "xl/worksheets/sheet1.xml", b"<worksheet><sheetData><row r='1'><c r='A1' t='s'><v>99</v></c>"),
    "bad-styles.xlsx": lambda: _rewrite(_good_xlsx(), "xl/styles.xml", b"<styleSheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><cellXfs><xf numFmtId='abc'/></cellXfs></styleSheet>"),
    "bad-sharedstrings.xlsx": lambda: _rewrite(_good_xlsx(), "xl/worksheets/sheet1.xml",
                                                b"<worksheet xmlns='http://schemas.openxmlformats.org/spreadsheetml/2006/main'><sheetData><row r='1'><c r='A1' t='s'><v>7</v></c></row></sheetData></worksheet>"),
    "no-body.docx": lambda: _rewrite(_rewrite(_good_xlsx(), "xl/workbook.xml", None), "[Content_Types].xml", b"<Types/>"),
}


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("坏资料", owner="employee_0001")["id"]
    streams.reset_all()
    with TestClient(app, raise_server_exceptions=False) as client:
        yield client, workspaces, project_id, workspaces.project_root(project_id)
    streams.reset_all()


def test_upload_and_read_corrupt_materials(api):
    client, _ws, pid, _root = api
    base = f"/api/v1/projects/{pid}"
    for name, make in CASES.items():
        up = client.post(f"{base}/resources", files={"file": (name, make())})
        assert up.status_code == 201, (name, up.status_code, up.text[:200])
        path = up.json()["material"]["path"]
        assert client.get(f"{base}/resources/preview", params={"path": path}).status_code == 200, name
        an = client.get(f"{base}/anchors/resolve", params={"anchor": f"file:{path}#sheet:Sheet!A1"})
        assert an.status_code == 200 and an.json()["items"][0]["resolved"] is False, (name, an.text[:200])
    for path in ("materials/map", "materials/verification", "resources"):
        assert client.get(f"{base}/{path}").status_code == 200, path


def test_corrupt_xlsx_already_in_resource(api):

    client, _ws, pid, root = api
    base = f"/api/v1/projects/{pid}"
    (root / "resource" / "excel").mkdir(parents=True, exist_ok=True)
    for name, make in CASES.items():
        if name.endswith(".xlsx"):
            (root / "resource" / "excel" / name).write_bytes(make())
    report = ("# 分析主题\n合成。\n\n# 业务项清单\n| 业务项ID | 业务项名称 | 分类 | 规则ID | 引用字段 | 来源位置 |\n"
              "| --- | --- | --- | --- | --- | --- |\n| ITEM-01 | 门店 | 对象 | 无 | 无 | resource/excel/bad-workbook-xml.xlsx#sheet:Sheet!A1 |\n")
    (root / "research").mkdir(exist_ok=True)
    (root / "research" / "20261102-合成.md").write_text(report, encoding="utf-8")
    proc.run(["git", "add", "-A"], cwd=root, check=True)
    proc.run(["git", "-c", "core.hooksPath=/dev/null", "commit", "-qm", "web: 合成坏表"], cwd=root, check=True)
    for name in CASES:
        if name.endswith(".xlsx"):
            path = f"resource/excel/{name}"
            assert client.get(f"{base}/resources/preview", params={"path": path}).status_code == 200, name
            assert client.get(f"{base}/anchors/resolve", params={"anchor": f"file:{path}#sheet:Sheet!A1"}).status_code == 200
    for path in ("materials/map", "materials/verification", "materials/candidates", "resources"):
        assert client.get(f"{base}/{path}").status_code == 200, path
