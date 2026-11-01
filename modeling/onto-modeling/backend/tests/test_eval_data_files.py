"""评测数据文件：数据源表单上传 Excel、配置引用与回收、旧配置迁移、发布包 mapping 取数。"""

import asyncio
import io
import json
import os
import time
from pathlib import Path
from types import SimpleNamespace

import openpyxl
import pytest

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


def _dataset(*file_ids: str) -> dict:
    return {"mode": "dataset", "files": list(file_ids)}


def test_saved_source_keeps_file_details(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    uploaded = eval_data_files.save(root, "业务数据.xlsx", _xlsx())
    source = eval_data_sources.create_source(pid, _dataset(uploaded["id"]), root)
    assert source["files"] == [uploaded]
    assert source["name"] == "业务数据表（1 份）"
    with pytest.raises(eval_data_sources.DataSourceError, match="不存在或已被删除"):
        eval_data_sources.create_source(pid, _dataset("0123456789ab"), root)


def test_uploaded_files_stay_out_of_workspace_git(tmp_path, monkeypatch):
    """评测结束会 git add -A；上传的业务数据不能跟着进历史。

    工作区模板已排除整个 .ontology-state；还没迁移的存量项目根 .gitignore 不排除它，
    这时靠数据目录自带的 .gitignore 兜底。
    """
    pid, root = _project(tmp_path, monkeypatch)
    eval_data_files.save(root, "业务数据.xlsx", _xlsx())
    (root / ".ontology-state" / "eval.json").write_text("{}", encoding="utf-8")
    # 复用工作区服务的 git 入口（workspace._git），测试里不另起子进程
    _git(root, "add", "-A")
    assert ".ontology-state" not in _git(root, "diff", "--cached", "--name-only")

    gitignore = root / ".gitignore"
    legacy = [line for line in gitignore.read_text(encoding="utf-8").splitlines()
              if line.strip() != ".ontology-state/"]
    gitignore.write_text("\n".join(legacy) + "\n", encoding="utf-8")
    _git(root, "add", "-A")
    staged = _git(root, "diff", "--cached", "--name-only")
    assert ".ontology-state/eval.json" in staged
    assert "eval-data" not in staged


def test_files_are_released_when_no_source_references_them(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    first = eval_data_files.save(root, "a.xlsx", _xlsx())["id"]
    second = eval_data_files.save(root, "b.xlsx", _xlsx())["id"]
    source = eval_data_sources.create_source(pid, _dataset(first, second), root)
    shared = eval_data_sources.create_source(pid, _dataset(first), root)

    eval_data_sources.update_source(pid, source["id"], _dataset(first), root)
    assert eval_data_files.path_of(root, second) is None  # 编辑时移除 → 立即回收
    eval_data_sources.delete_source(pid, source["id"], root)
    assert eval_data_files.path_of(root, first) is not None  # 另一个配置仍在用
    eval_data_sources.delete_source(pid, shared["id"], root)
    assert eval_data_files.path_of(root, first) is None


def test_unsaved_uploads_are_pruned_only_after_ttl(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    pending = eval_data_files.save(root, "表单未保存.xlsx", _xlsx())["id"]
    eval_data_sources.create_source(pid, {"mode": "none"}, root)
    assert eval_data_files.path_of(root, pending) is not None  # 可能还在别的表单里编辑

    stale = time.time() - eval_data_files.ORPHAN_TTL_SECONDS - 60
    os.utime(eval_data_files.data_dir(root) / pending, (stale, stale))
    eval_data_sources.create_source(pid, {"mode": "none"}, root)
    assert eval_data_files.path_of(root, pending) is None


def test_legacy_resources_are_migrated_to_uploaded_files(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    rel = "resource/excel/炼油数据.xlsx"
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    (root / rel).write_bytes(_xlsx())
    store = Path(config.DATA_DIR) / "eval-data-sources" / f"{pid}.json"
    store.parent.mkdir(parents=True, exist_ok=True)
    store.write_text(json.dumps({"sources": [{
        "id": "legacy000001", "name": "业务数据表（1 份）", "mode": "dataset",
        "resources": [rel, "resource/excel/已删除.xlsx"], "include_mock": False,
        "created_at": 1, "updated_at": 1,
    }]}, ensure_ascii=False), encoding="utf-8")

    source = eval_data_sources.get_source(pid, "legacy000001", root)
    assert "resources" not in source
    assert [f["name"] for f in source["files"]] == ["炼油数据.xlsx"]  # 已删除的路径丢弃
    copied = eval_data_files.path_of(root, source["files"][0]["id"])
    assert copied.read_bytes() == (root / rel).read_bytes()
    saved = json.loads(store.read_text(encoding="utf-8"))["sources"][0]
    assert saved["files"] == source["files"] and "resources" not in saved  # 迁移结果已落盘


def test_publish_mapping_reads_saved_uploads_only(tmp_path, monkeypatch):
    pid, root = _project(tmp_path, monkeypatch)
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)
    (root / "ontology" / "object" / "订单.md").write_text(ORDER_DOC, encoding="utf-8")
    file_id = eval_data_files.save(root, "业务数据.xlsx", _xlsx())["id"]

    unsaved = package_mapping.resolve_object_tables(root)
    assert not unsaved["objects"]["订单"]["table"]  # 表单里未保存的上传不算数据表来源

    eval_data_sources.create_source(pid, _dataset(file_id), root)
    bound = package_mapping.resolve_object_tables(root)
    assert bound["objects"]["订单"]["table"] == "订单表"


def test_upload_endpoint_returns_file_entry(tmp_path, monkeypatch):
    from app.routers import projects

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(projects, "workspaces", manager)
    pid = manager.create_project("上传接口")["id"]
    request = SimpleNamespace(state=SimpleNamespace(user=None), headers={})

    async def read(data):
        return data

    good = SimpleNamespace(filename="业务数据.xlsx", read=lambda: read(_xlsx()))
    data = asyncio.run(projects.upload_eval_data_file(pid, request, good))
    assert data["file"]["name"] == "业务数据.xlsx"
    assert eval_data_files.path_of(manager.project_root(pid), data["file"]["id"]) is not None

    bad = SimpleNamespace(filename="数据.csv", read=lambda: read(b"a\n1\n"))
    response = asyncio.run(projects.upload_eval_data_file(pid, request, bad))
    assert response.status_code == 400
