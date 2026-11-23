"""旧版评测数据文件（只读）：不进工作区 git、旧配置迁移、发布包 mapping 取数。"""

import io
import json
from pathlib import Path

import openpyxl

from app import config
from app.services import eval_data_files, eval_data_sources, package_mapping
from app.services.workspace import WorkspaceManager, _git

ORDER_DOC = """# 对象属性清单
| 中文名称 | 英文名称 | 描述 | 主键 | 类型 |
| --- | --- | --- | --- | --- |
| 订单号 | ORDER_ID | 合成订单号 | 是 | 字符串 |
| 金额 | AMOUNT | 合成金额 | 否 | 数字 |
"""


def _xlsx(sheet: str = "订单表", columns=("ORDER_ID", "AMOUNT")) -> bytes:
    workbook = openpyxl.Workbook()
    worksheet = workbook.active
    worksheet.title = sheet
    worksheet.append(list(columns))
    worksheet.append(["O1", 12])
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _project(tmp_path, monkeypatch) -> tuple[str, Path]:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project("评测数据文件")
    return meta["id"], manager.project_root(meta["id"])


def _workspace_xlsx(root: Path, rel: str = "resource/excel/业务数据.xlsx") -> str:
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(_xlsx())
    return rel


def _write_sources(pid: str, sources: list[dict]) -> Path:
    store = Path(config.DATA_DIR) / "eval-data-sources" / f"{pid}.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"sources": sources}, ensure_ascii=False), encoding="utf-8")
    return store


def test_uploaded_files_stay_out_of_workspace_git(tmp_path, monkeypatch):
    """模板 .gitignore 没排除 .ontology-state；评测数据文件不能跟着进历史。"""
    pid, root = _project(tmp_path, monkeypatch)
    eval_data_files.import_workspace_file(root, _workspace_xlsx(root))
    (root / ".ontology-state" / "progress.json").write_text("{}", encoding="utf-8")
    # 复用工作区服务的 git 入口（workspace._git），测试里不另起子进程
    _git(root, "add", "-A")
    staged = _git(root, "diff", "--cached", "--name-only")
    assert ".ontology-state/progress.json" in staged
    assert "eval-data" not in staged


def test_legacy_resources_are_migrated_to_uploaded_files(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    rel = _workspace_xlsx(root, "resource/excel/示例数据.xlsx")
    store = _write_sources(pid, [{
        "id": "legacy000001", "name": "业务数据表（1 份）", "mode": "dataset",
        "resources": [rel, "resource/excel/已删除.xlsx"], "include_mock": False,
        "created_at": 1, "updated_at": 1,
    }])

    [source] = eval_data_sources.list_sources(pid, root)
    assert "resources" not in source
    assert [f["name"] for f in source["files"]] == ["示例数据.xlsx"]  # 已删除的路径丢弃
    copied = eval_data_files.path_of(root, source["files"][0]["id"])
    assert copied.read_bytes() == (root / rel).read_bytes()
    saved = json.loads(store.read_text(encoding="utf-8"))["sources"][0]
    assert saved["files"] == source["files"] and "resources" not in saved  # 迁移结果已落盘


def test_publish_mapping_reads_saved_uploads_only(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text(ORDER_DOC, encoding="utf-8")
    entry = eval_data_files.import_workspace_file(root, _workspace_xlsx(root, ".ontology-state/tmp/业务数据.xlsx"))

    unsaved = package_mapping.resolve_object_tables(root)
    assert not unsaved["objects"]["订单"]["table"]  # 没被配置引用的文件不算数据表来源

    _write_sources(pid, [{"id": "source000001", "mode": "dataset", "files": [entry]}])
    bound = package_mapping.resolve_object_tables(root)
    assert bound["objects"]["订单"]["table"] == "订单表"
