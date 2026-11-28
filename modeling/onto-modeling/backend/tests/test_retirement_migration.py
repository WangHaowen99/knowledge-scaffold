"""评测移出的启动迁移（ADR-0020）：被对象引用的上传表迁进资料区并提交，评测状态与数据源配置删净，幂等。

上传表、对象、数据源配置（含"密码"）全部为合成数据。
"""

import io
import json
from pathlib import Path

import pytest

from app import config
from app.services import error_log, eval_retirement, package_mapping
from app.services.workspace import META_FILE, WorkspaceManager, _git
from tests.test_publish_binding_gate import write_object, write_schema
from tests.text_versions import write_text_versions

openpyxl = pytest.importorskip("openpyxl")

FILE_A = "a" * 12
FILE_B = "b" * 12


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def xlsx(sheets: dict[str, list[str]]) -> bytes:
    workbook = openpyxl.Workbook()
    workbook.remove(workbook.active)
    for name, columns in sheets.items():
        workbook.create_sheet(name).append(columns)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def upload(root: Path, file_id: str, name: str, sheets: dict[str, list[str]]) -> bytes:
    folder = root / ".ontology-state" / "eval-data" / file_id
    folder.mkdir(parents=True)
    data = xlsx(sheets)
    (folder / name).write_bytes(data)
    return data


def legacy_config(manager: WorkspaceManager, pid: str, file_ids: list[str]) -> Path:
    path = manager.data_dir / "eval-data-sources" / f"{pid}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"sources": [{
        "id": "s1", "mode": "connection", "provider": "neo4j",
        "config": {"password": "synthetic-password"},
        "files": [{"id": file_id} for file_id in file_ids],
    }]}), encoding="utf-8")
    return path


def committed_project(manager: WorkspaceManager, name: str = "迁移") -> tuple[str, Path]:
    pid = manager.create_project(name)["id"]
    root = manager.project_root(pid)
    write_object(root, "订单", "T_ORDER", ["ORDER_ID", "AMOUNT"])
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: synthetic objects")
    return pid, root


def head_files(root: Path) -> list[str]:
    return _git(root, "-c", "core.quotePath=false", "show", "--name-only", "--pretty=format:", "HEAD").split()


def test_referenced_upload_moves_into_resource_and_state_is_purged(manager):
    pid, root = committed_project(manager)
    data = upload(root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    upload(root, FILE_B, "无关表.xlsx", {"T_OTHER": ["X", "Y"]})
    (root / ".ontology-state" / "eval.json").write_text('{"status": "done"}', encoding="utf-8")
    (root / ".ontology-state" / "solver-validation-cases.json").write_text("[]", encoding="utf-8")
    (root / ".ontology-state" / "solver-validations.json").write_text('{"runs": []}', encoding="utf-8")
    config_file = legacy_config(manager, pid, [FILE_A])
    (manager.data_dir / "modeling-cleanup-tasks.json").write_text("[]", encoding="utf-8")
    meta = json.loads((root / META_FILE).read_text(encoding="utf-8"))
    meta.update(modeling_project_id="mp-1", modeling_package_digest="d", modeling_package_revision="r")
    (root / META_FILE).write_text(json.dumps(meta), encoding="utf-8")
    (root / "ontology" / "rule" / "未提交.md").write_text("# 未提交\n", encoding="utf-8")

    report = eval_retirement.migrate(manager)

    assert report == {"migrated": {pid: ["resource/excel/业务表.xlsx"]}, "failed": []}
    assert (root / "resource" / "excel" / "业务表.xlsx").read_bytes() == data
    directives = json.loads((root / "resource" / "materials.json").read_text(encoding="utf-8"))
    assert directives["resource/excel/业务表.xlsx"]["purpose"] == "schema"
    assert sorted(head_files(root)) == ["resource/excel/业务表.xlsx", "resource/materials.json"]
    assert "ontology/rule/未提交.md" in _git(root, "-c", "core.quotePath=false", "status", "--porcelain")
    assert not (root / ".ontology-state" / "eval-data").exists()
    # 最近一轮的状态留给「旧版评测记录」只读显示（ADR-0092）
    assert (root / ".ontology-state" / "eval.json").read_text(encoding="utf-8") == '{"status": "done"}'
    # 旧验证用例保留，求解页只读导出（SV-05，同 develop）
    assert (root / ".ontology-state" / "solver-validation-cases.json").read_text(encoding="utf-8") == "[]"
    assert (root / ".ontology-state" / "solver-validations.json").exists()  # 回执门禁记录保留
    assert not config_file.parent.exists() and not (manager.data_dir / "modeling-cleanup-tasks.json").exists()
    assert not set(eval_retirement.LEGACY_META_FIELDS) & set(manager.get_project(pid))
    write_text_versions(root)  # 迁进资料区后由多源连接器转出文本版，表头从文本版读（ADR-0076）
    binding = package_mapping.resolve_object_tables(root)
    assert binding["objects"]["订单"]["table"] == "T_ORDER" and not binding["problems"]


def test_migration_is_idempotent(manager):
    pid, root = committed_project(manager)
    upload(root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    eval_retirement.migrate(manager)
    head = _git(root, "rev-parse", "HEAD")
    assert eval_retirement.migrate(manager) == {"migrated": {}, "failed": []}
    assert _git(root, "rev-parse", "HEAD") == head


def test_half_finished_migration_resumes_without_duplicate_commit(manager):
    """复制并提交后、删评测数据前停机：再跑一遍时表已在 schema 资料里，只删数据，不重复提交。"""
    pid, root = committed_project(manager)
    upload(root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    eval_retirement.migrate(manager)
    write_text_versions(root, commit=True)  # 多源连接器已转出文本版
    head = _git(root, "rev-parse", "HEAD")
    upload(root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})  # 模拟没删掉

    assert eval_retirement.migrate(manager) == {"migrated": {}, "failed": []}
    assert _git(root, "rev-parse", "HEAD") == head
    assert not (root / ".ontology-state" / "eval-data").exists()


def test_inferred_binding_counts_as_reference(manager):
    pid = manager.create_project("推断")["id"]
    root = manager.project_root(pid)
    write_object(root, "订单", None, ["ORDER_ID", "AMOUNT", "STATUS"])
    upload(root, FILE_A, "推断表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT", "STATUS"]})

    assert eval_retirement.migrate(manager)["migrated"] == {pid: ["resource/excel/推断表.xlsx"]}


def test_unreferenced_or_shadowed_uploads_are_dropped(manager):
    pid, root = committed_project(manager)
    write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: schema")
    head = _git(root, "rev-parse", "HEAD")
    upload(root, FILE_A, "同名表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})  # schema 资料里已有同名表
    upload(root, FILE_B, "无关表.xlsx", {"T_OTHER": ["X"]})

    assert eval_retirement.migrate(manager)["migrated"] == {}
    assert _git(root, "rev-parse", "HEAD") == head
    assert not (root / "resource" / "excel" / "同名表.xlsx").exists()
    assert not (root / ".ontology-state" / "eval-data").exists()


def test_name_clash_keeps_existing_file(manager):
    pid, root = committed_project(manager)
    existing = root / "resource" / "excel" / "业务表.xlsx"
    existing.parent.mkdir(parents=True, exist_ok=True)
    existing.write_bytes(b"synthetic-other-content")
    upload(root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})

    moved = eval_retirement.migrate(manager)["migrated"][pid]

    assert moved == [f"resource/excel/业务表-{FILE_A}.xlsx"]
    assert existing.read_bytes() == b"synthetic-other-content"


def test_configured_upload_wins_same_sheet_name(manager):
    pid, root = committed_project(manager)
    upload(root, FILE_A, "未保存.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    upload(root, FILE_B, "已保存.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    legacy_config(manager, pid, [FILE_B])

    assert eval_retirement.migrate(manager)["migrated"] == {pid: ["resource/excel/已保存.xlsx"]}


def test_one_failing_project_keeps_its_data_but_secrets_are_deleted(manager):
    broken, broken_root = committed_project(manager, "坏项目")
    good, good_root = committed_project(manager, "好项目")
    upload(broken_root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    upload(good_root, FILE_A, "业务表.xlsx", {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    (broken_root / "resource" / "materials.json").write_text("{broken", encoding="utf-8")
    config_file = legacy_config(manager, broken, [FILE_A])

    report = eval_retirement.migrate(manager)

    assert report["failed"] == [broken] and list(report["migrated"]) == [good]
    assert (broken_root / ".ontology-state" / "eval-data" / FILE_A).is_dir()  # 留到下次启动重试
    assert not config_file.exists()  # 密码不等迁移
    [entry] = error_log.read()
    assert entry["project_id"] == broken and entry["source"] == "eval_retirement"

    (broken_root / "resource" / "materials.json").write_text("{}", encoding="utf-8")
    assert eval_retirement.migrate(manager)["migrated"] == {broken: ["resource/excel/业务表.xlsx"]}


def test_projects_without_legacy_data_are_untouched(manager):
    pid, root = committed_project(manager)
    head = _git(root, "rev-parse", "HEAD")
    assert eval_retirement.migrate(manager) == {"migrated": {}, "failed": []}
    assert _git(root, "rev-parse", "HEAD") == head
