"""发布按提交打包（A8、A9）：只读树导出、版本号单调递增、发布标签、未提交改动不进包、
预检与发布同口径、版本列表编号，以及网页上传资料即提交。fixture 全部为合成数据。
"""

import io
import json
import zipfile
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app.services import publish as publish_service
from app.services import solver as solver_service
from app.main import app
from app.services import auth
from app.services.publish.tree import release_tree
from app.services.workspace import WorkspaceError, WorkspaceManager, _git
from tests.test_publish_binding_gate import object_doc, write_object, write_schema
from tests.text_versions import write_text_versions

openpyxl = pytest.importorskip("openpyxl")


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def git_paths(root: Path, *args: str) -> str:
    """git 输出里的中文路径不转义，便于和期望路径直接比较。"""
    return _git(root, "-c", "core.quotePath=false", *args)


def commit_all(root: Path, message: str = "test: synthetic stage") -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "--allow-empty", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


SCRIPT = (
    '"""\n关联operation：创建订单\n\n功能描述：计算客单价\n\n输入参数：\n'
    "    orders    json    订单列表，必填\n"
    "    factor    float   加权因子，默认 1.0\n\n输出：\n    return 客单价数值\n\"\"\"\n\n"
    "import argparse\n"
    "def run(orders, factor):\n    return 0\n"
    "def _cli():\n    parser = argparse.ArgumentParser()\n"
    "    parser.add_argument('--orders', required=True, type=json.loads)\n"
    "    parser.add_argument('--factor', default=1.0, type=float)\n"
    "    run(**vars(parser.parse_args()))\n"
)


def rich_project(manager: WorkspaceManager, name: str = "发布回归") -> tuple[str, Path]:
    """对象（绑定 schema 表）、旧目录操作、规则、指标与对外脚本各一份，全部已提交。"""
    pid = manager.create_project(name)["id"]
    root = manager.project_root(pid)
    write_schema(root, {"T_ORDER": ["ORDER_ID", "AMOUNT"]})
    write_object(root, "订单", "T_ORDER", ["ORDER_ID", "AMOUNT"])
    (root / "ontology" / "logic").mkdir(exist_ok=True)
    (root / "ontology" / "logic" / "创建订单.md").write_text("# 创建订单\n\n生成[[订单]]\n", encoding="utf-8")
    (root / "ontology" / "rule" / "限购规则.md").write_text("# 限购规则\n\n约束[[订单]]数量\n", encoding="utf-8")
    (root / "ontology" / "metric" / "客单价.md").write_text("# 客单价\n\n基于[[订单]]金额\n", encoding="utf-8")
    (root / "ontology" / "scripts" / "calc_price.py").write_text(SCRIPT, encoding="utf-8")
    commit_all(root)
    return pid, root


def package_files(path: Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as zf:
        return {name: zf.read(name) for name in zf.namelist()}


def manifest(path: Path, pid: str) -> dict:
    return yaml.safe_load(package_files(path)[f"{pid}/manifest.yaml"])


def release_tags(root: Path) -> list[str]:
    return _git(root, "tag", "--list", "release/*").split()


# ---- 版本号、标签与发布记录 ----


def test_publish_packs_head_and_tags_release(manager, tmp_path):
    pid, root = rich_project(manager)
    head = _git(root, "rev-parse", "HEAD").strip()
    count = int(_git(root, "rev-list", "--count", "--first-parent", "HEAD"))

    record = publish_service.publish_package(manager, pid, tmp_path / "pkg.zip", author="operator_a")

    version = f"0.{count}.0"  # 契约 3.20：0.{n}.0，n 是主线版本号
    assert (record["version"], record["commit"], record["tag"]) == (version, head, f"release/{version}")
    assert record["uncommitted"] == [] and record["uncommitted_count"] == 0
    data = manifest(tmp_path / "pkg.zip", pid)
    assert data["version"] == version and data["source_commit"] == head
    assert _git(root, "cat-file", "-t", f"release/{version}").strip() == "tag"  # 注释标签
    assert _git(root, "rev-parse", f"release/{version}^{{commit}}").strip() == head
    message = _git(root, "tag", "--list", "--format=%(contents)", f"release/{version}")
    assert f"发布 {version}" in message and "发布人：operator_a" in message and "降级发布：否" in message
    assert manager.get_project(pid)["last_package_publish"] == record
    assert publish_service.package_binding_check(manager, pid)["last_publish"]["version"] == version


def test_degraded_publish_is_marked_in_tag(manager, tmp_path):
    pid, root = rich_project(manager)
    write_object(root, "客户", "T_CLIENT", ["CUSTOMER_ID"])
    commit_all(root)
    record = publish_service.publish_package(manager, pid, tmp_path / "pkg.zip", allow_unbound=True, author="operator_a")
    assert record["degraded"] is True
    message = _git(root, "tag", "--list", "--format=%(contents)", record["tag"])
    assert "降级发布：是（1 个对象未绑定数据表）" in message


def test_same_commit_republish_keeps_version_and_bytes(manager, tmp_path):
    pid, root = rich_project(manager)
    first = publish_service.publish_package(manager, pid, tmp_path / "a.zip")
    second = publish_service.publish_package(manager, pid, tmp_path / "b.zip")
    assert second["version"] == first["version"] and second["tag"] == first["tag"]
    assert (tmp_path / "a.zip").read_bytes() == (tmp_path / "b.zip").read_bytes()
    assert release_tags(root) == [first["tag"]]


def test_version_keeps_growing_past_fifty_commits(manager, tmp_path):
    pid, root = rich_project(manager)
    for index in range(60):
        commit_all(root, f"test: 第 {index} 次保存")
    count = int(_git(root, "rev-list", "--count", "--first-parent", "HEAD"))
    assert count > 60

    first = publish_service.publish_package(manager, pid, tmp_path / "a.zip")
    assert first["version"] == f"0.{count}.0"

    versions = manager.versions(pid)
    assert len(versions) == count  # 不再截断为 50 条，分页在接口层（V-01）
    assert versions[0]["n"] == count and versions[-1]["n"] == 1
    assert versions[0]["release"] == first["version"]
    assert all(entry["release"] is None for entry in versions[1:])

    manager.write_file(pid, "knowledge/note.md", "合成说明")
    second = publish_service.publish_package(manager, pid, tmp_path / "b.zip")
    assert second["version"] == f"0.{count + 1}.0"
    assert manager.versions(pid)[0]["n"] == count + 1


def test_version_never_falls_below_existing_releases(manager, tmp_path):
    """历史里已有更大的发布号（旧版的 0.1.N，或迁移来的项目）：0.{n}.0 不够大时取最大版本的补丁号 + 1，
    不倒退（contract-changes 2026-11-01 主控条）。"""
    pid, root = rich_project(manager)
    base = _git(root, "rev-list", "--max-parents=0", "HEAD").strip()
    _git(root, "tag", "-a", "release/0.1.90", "-m", "发布 0.1.90", base)
    record = publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    count = int(_git(root, "rev-list", "--count", "--first-parent", "HEAD"))
    assert record["version"] == f"0.{count}.0"  # 0.{n}.0 已经大于 0.1.90

    _git(root, "tag", "-a", "release/0.40.3", "-m", "发布 0.40.3", base)
    commit_all(root, "test: 再保存一次")
    again = publish_service.publish_package(manager, pid, tmp_path / "again.zip")
    assert again["version"] == "0.40.4"  # n 远小于已有最大 minor：沿用最大版本，补丁号 + 1


def test_release_tags_cannot_be_deleted(manager, tmp_path):
    pid, root = rich_project(manager)
    record = publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    with pytest.raises(WorkspaceError):
        manager.delete_tag(pid, record["tag"])
    assert release_tags(root) == [record["tag"]]


# ---- 未提交改动与预检 ----


def test_uncommitted_changes_stay_out_and_are_listed(manager, tmp_path):
    pid, root = rich_project(manager)
    head = _git(root, "rev-parse", "HEAD").strip()
    order = root / "ontology" / "object" / "订单.md"
    order.write_text(order.read_text(encoding="utf-8") + "\n未提交的合成说明\n", encoding="utf-8")
    # 未提交的新对象声明了不存在的表：按工作区算会阻断，按 HEAD 算不在包里
    write_object(root, "客户", "T_MISSING", ["CUSTOMER_ID"])
    (root / "ontology" / "rule" / "index.md").write_text("# 规则目录\n", encoding="utf-8")
    (root / "research" / "草稿.md").write_text("不是包相关目录\n", encoding="utf-8")

    check = publish_service.package_binding_check(manager, pid)
    assert check["commit"] == head and check["blocking"] == 0 and check["objects"] == 1
    expected = ["ontology/object/客户.md", "ontology/object/订单.md"]
    assert check["uncommitted"] == expected and check["uncommitted_count"] == 2

    record = publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    files = package_files(tmp_path / "pkg.zip")
    assert f"{pid}/objects/客户.md" not in files
    assert "未提交的合成说明" not in files[f"{pid}/objects/订单.md"].decode("utf-8")
    assert record["commit"] == head and record["uncommitted"] == expected
    # 发布不替用户提交：改动仍留在工作区
    assert "未提交的合成说明" in order.read_text(encoding="utf-8")
    assert "ontology/object/客户.md" in git_paths(root, "status", "--porcelain", "-uall")


def test_check_and_publish_agree_on_head_tree(manager, tmp_path):
    """预检看的是 HEAD：提交里的阻断项即使工作区已改好也照样阻断，与发布一致。"""
    pid, root = rich_project(manager)
    write_object(root, "客户", "T_MISSING", ["CUSTOMER_ID"])
    commit_all(root)
    write_object(root, "客户", "T_ORDER", ["ORDER_ID"])  # 工作区修好但没提交

    assert publish_service.package_binding_check(manager, pid)["blocking"] == 1
    with pytest.raises(WorkspaceError) as blocked:
        publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    assert blocked.value.code == "binding_problems"
    assert release_tags(root) == [] and manager.get_project(pid).get("last_package_publish") is None


# ---- 回归：只读树导出与干净工作区导出逐字节一致 ----


def test_archive_export_matches_clean_workspace_export(manager, tmp_path):
    pid, root = rich_project(manager)
    record = publish_service.publish_package(manager, pid, tmp_path / "release.zip")
    publish_service.export_package_zip(manager, 
        pid, tmp_path / "workspace.zip", version=record["version"], source_commit=record["commit"]
    )
    assert (tmp_path / "release.zip").read_bytes() == (tmp_path / "workspace.zip").read_bytes()


def test_solver_archive_export_matches_clean_workspace_export(manager, tmp_path):
    from tests.test_solver_contract_check import _accept, _with_contract

    pid, root = _with_contract(manager)
    _accept(root, solver_service.solver_release_status(manager, pid)["revision"])  # 回执在工作区状态目录，按树的修订号找
    record = publish_service.publish_package(manager, pid, tmp_path / "release.zip", allow_unbound=True)
    publish_service.export_package_zip(manager, 
        pid, tmp_path / "workspace.zip", allow_unbound=True,
        version=record["version"], source_commit=record["commit"],
    )
    assert manifest(tmp_path / "release.zip", pid)["schema_version"] == "0.2"
    assert (tmp_path / "release.zip").read_bytes() == (tmp_path / "workspace.zip").read_bytes()


def test_preview_export_numbers_by_mainline(manager, tmp_path):
    """不经发布直接渲染（预览、测试）：版本号按主线版本号取 0.{n}.0，不写来源提交。"""
    pid, root = rich_project(manager)
    for index in range(55):
        commit_all(root, f"test: {index}")
    publish_service.export_package_zip(manager, pid, tmp_path / "legacy.zip")
    data = manifest(tmp_path / "legacy.zip", pid)
    assert data["version"] == f"0.{_git(root, 'rev-list', '--count', '--first-parent', 'HEAD').strip()}.0"
    assert "source_commit" not in data


def test_graph_variant_maps_objects_by_key(manager, tmp_path):
    """图模式留作发布适配器的变体（方案第 6 章第 1 项）：resource 为对象 key，Sheet 与列在 sources 里。"""
    pid, _root = rich_project(manager)
    publish_service.export_package_zip(manager, pid, tmp_path / "plain.zip")
    publish_service.export_package_zip(manager, pid, tmp_path / "graph.zip", graph=True)
    plain = json.loads(package_files(tmp_path / "plain.zip")[f"{pid}/mappings/object-data-mappings.json"])
    graph = json.loads(package_files(tmp_path / "graph.zip")[f"{pid}/mappings/object-data-mappings.json"])
    assert plain["objects"]["订单"]["resource"] == "T_ORDER"
    assert graph["objects"]["订单"]["resource"] == "订单"
    assert graph["objects"]["订单"]["sources"][0]["resource"] == "T_ORDER"


# ---- 只读树的安全与完整性 ----


def test_symlink_escaping_the_tree_blocks_release(manager, tmp_path):
    pid, root = rich_project(manager)
    (root / "ontology" / "object" / "外部.md").symlink_to("/etc/hosts")
    commit_all(root)
    with pytest.raises(WorkspaceError) as blocked:
        publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    assert blocked.value.code == "release_tree_unsafe"
    assert release_tags(root) == []


def test_export_ignore_attribute_cannot_drop_package_files(manager, tmp_path):
    pid, root = rich_project(manager)
    (root / ".gitattributes").write_text("ontology/rule/** export-ignore\n", encoding="utf-8")
    commit_all(root)
    with pytest.raises(WorkspaceError) as blocked:
        publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    assert blocked.value.code == "release_tree_mismatch"


# ---- 网页上传资料即提交 ----


def _xlsx(columns: list[str], sheet: str = "T_CUSTOMER") -> bytes:
    workbook = openpyxl.Workbook()
    workbook.active.title = sheet
    workbook.active.append(columns)
    buffer = io.BytesIO()
    workbook.save(buffer)
    return buffer.getvalue()


def _head_files(root: Path) -> set[str]:
    return set(git_paths(root, "show", "--name-only", "--format=", "HEAD").split())


def test_upload_commits_only_its_own_paths(manager, tmp_path):
    pid, root = rich_project(manager)
    # agent 的改动：一处已暂存、一处未暂存，都不能被网页上传带进提交
    (root / "research" / "分析.md").write_text("agent 暂存\n", encoding="utf-8")
    _git(root, "add", "research/分析.md")
    order = root / "ontology" / "object" / "订单.md"
    order.write_text(order.read_text(encoding="utf-8") + "\nagent 未暂存\n", encoding="utf-8")

    saved = manager.save_resource(
        pid, "excel", "客户[1].xlsx", _xlsx(["CUSTOMER_ID", "NAME"]), purpose="schema", author="operator_a"
    )
    assert saved["commit"] == _git(root, "rev-parse", "HEAD").strip()
    assert _head_files(root) == {"resource/excel/客户[1].xlsx", "resource/materials.json"}
    subject = _git(root, "log", "-1", "--format=%s").strip()
    assert subject == "web: 上传资料 resource/excel/客户[1].xlsx by operator_a"
    assert git_paths(root, "diff", "--cached", "--name-only").split() == ["research/分析.md"]
    assert "ontology/object/订单.md" in git_paths(root, "diff", "--name-only")

    # 多源连接器把它转成文本版（这里直接调转换器）后，发布就能读到：对象绑上它
    write_text_versions(root, commit=True)
    manager.write_file(pid, "ontology/object/客户.md", object_doc("T_CUSTOMER", ["CUSTOMER_ID", "NAME"]))
    publish_service.publish_package(manager, pid, tmp_path / "pkg.zip")
    mappings = json.loads(package_files(tmp_path / "pkg.zip")[f"{pid}/mappings/object-data-mappings.json"])
    assert "客户" in mappings["objects"]

    manager.set_resource_directive(pid, "resource/excel/客户[1].xlsx", "evidence", author="operator_a")
    assert _head_files(root) == {"resource/materials.json"}
    manager.delete_resource(pid, "resource/excel/客户[1].xlsx", author="operator_a")
    # 删资料连同它的文本版一起删（ADR-0073）
    assert _head_files(root) == {
        "resource/excel/客户[1].xlsx", "resource/materials.json", "resource/text/excel/客户[1].xlsx.md",
    }
    assert _git(root, "log", "-1", "--format=%s").strip().startswith("web: 删除资料 resource/excel/客户[1].xlsx")
    assert git_paths(root, "diff", "--cached", "--name-only").split() == ["research/分析.md"]


def test_marking_legacy_untracked_upload_commits_the_file(manager):
    """旧版上传没进 git 的表格：标成 schema 时连文件一起提交，发布才读得到。"""
    pid, root = rich_project(manager)
    (root / "resource" / "excel" / "旧表.xlsx").write_bytes(_xlsx(["CUSTOMER_ID"]))
    manager.set_resource_directive(pid, "resource/excel/旧表.xlsx", "schema")
    assert _head_files(root) == {"resource/excel/旧表.xlsx", "resource/materials.json"}


def test_unchanged_upload_does_not_create_empty_commit(manager):
    pid, root = rich_project(manager)
    data = _xlsx(["CUSTOMER_ID"])
    manager.save_resource(pid, "excel", "客户.xlsx", data, purpose="schema")
    head = _git(root, "rev-parse", "HEAD").strip()
    again = manager.save_resource(pid, "excel", "客户.xlsx", data, purpose="schema")
    assert again["commit"] == "" and again["replaced"] is True
    assert _git(root, "rev-parse", "HEAD").strip() == head


# ---- 路由：响应头带回版本号与来源提交 ----


def test_publish_route_creates_release_record(manager, monkeypatch):
    """PB-03：POST 发布，响应是发布记录；GET 不再有发布副作用（旧导出包路径 410）。"""
    from app.api import deps

    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    pid, root = rich_project(manager)
    (root / "ontology" / "rule" / "新规则.md").write_text("# 新规则\n", encoding="utf-8")
    client = TestClient(app)
    assert client.get(f"/api/projects/{pid}/export", params={"scope": "package"}).status_code == 410
    response = client.post(f"/api/v1/projects/{pid}/releases", json={})
    assert response.status_code == 201
    record = manager.get_project(pid)["last_package_publish"]
    body = response.json()
    assert body["version"] == record["version"] and body["commit"] == record["commit"]
    assert body["tag"] == f"release/{record['version']}" and body["format"] == "ontology"
    assert body["version_label"] == f"v0.{record['version'].split('.')[1]}"
    assert record["uncommitted"] == ["ontology/rule/新规则.md"]
    check = client.get(f"/api/v1/projects/{pid}/publish/check").json()
    assert check["uncommitted"] == {"count": 1, "paths": ["ontology/rule/新规则.md"]}
    assert check["last_release"]["version"] == record["version"]
    # 同一提交再发布：沿用原版本，记录不变
    again = client.post(f"/api/v1/projects/{pid}/releases", json={})
    assert again.status_code == 201 and again.json()["version"] == record["version"]


def test_publish_route_blocks_on_gates_and_allows_degrade(manager, monkeypatch):
    from app.api import deps

    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    pid, root = rich_project(manager)
    write_object(root, "客户", "T_CLIENT", ["CUSTOMER_ID"])
    commit_all(root)
    client = TestClient(app)
    check = client.get(f"/api/v1/projects/{pid}/publish/check").json()
    assert check["blocking_count"] == 1 and check["degradable"] is True
    assert check["binding"]["issues"][0]["object"]["id"] == "object:客户"
    blocked = client.post(f"/api/v1/projects/{pid}/releases", json={})
    assert blocked.status_code == 422 and blocked.json()["code"] == "gate_blocked"
    assert [gate["id"] for gate in blocked.json()["details"]["gates"]] == ["data_binding"]
    degraded = client.post(f"/api/v1/projects/{pid}/releases", json={"degrade": True})
    assert degraded.status_code == 201
    assert degraded.json()["gates"] == {"all_passed": False, "degraded": True, "unbound_objects": ["客户"]}


def test_release_tree_helper_is_read_only_copy(manager):
    """只读树来自提交：工作区里后来的改动、未跟踪文件都不在树里。"""
    pid, root = rich_project(manager)
    head = _git(root, "rev-parse", "HEAD").strip()
    (root / "ontology" / "object" / "临时.md").write_text("# 临时\n", encoding="utf-8")
    with release_tree(root, head) as tree:
        assert (tree / "ontology" / "object" / "订单.md").is_file()
        assert not (tree / "ontology" / "object" / "临时.md").exists()
        assert not (tree / "research").exists()  # 只导出包相关目录
    assert not tree.exists()


def test_release_reads_only_tables_from_resource(manager):
    """资料区只有用途清单和表格会被发布读取：PDF 不复制进只读树，改了也不算未提交。"""
    pid, root = rich_project(manager)
    (root / "resource" / "others").mkdir(parents=True, exist_ok=True)
    (root / "resource" / "others" / "说明.pdf").write_bytes(b"%PDF-1.4 synthetic\n")
    head = commit_all(root)
    with release_tree(root, head) as tree:
        assert (tree / "resource" / "materials.json").is_file()
        assert any((tree / "resource").rglob("*.xlsx"))
        assert not (tree / "resource" / "others" / "说明.pdf").exists()

    (root / "resource" / "others" / "说明.pdf").write_bytes(b"%PDF-1.4 changed\n")
    (root / "resource" / "others" / "新图.png").write_bytes(b"synthetic")
    (root / "resource" / "excel" / "新表.csv").write_text("ID\n1\n", encoding="utf-8")
    check = publish_service.package_binding_check(manager, pid)
    assert check["uncommitted"] == ["resource/excel/新表.csv"]


def test_startup_commits_pending_uploads_only(manager):
    """启动补提交：资料区里没进 git 的上传提交一次，agent 的改动留在工作区；再跑不产生提交。"""
    pid, root = rich_project(manager)
    (root / "resource" / "excel" / "旧表.xlsx").write_bytes(_xlsx(["CUSTOMER_ID"]))
    order = root / "ontology" / "object" / "订单.md"
    order.write_text(order.read_text(encoding="utf-8") + "\nagent 未提交\n", encoding="utf-8")

    assert manager.commit_pending_uploads() == [pid]
    assert _head_files(root) == {"resource/excel/旧表.xlsx"}
    assert "ontology/object/订单.md" in git_paths(root, "diff", "--name-only")
    head = _git(root, "rev-parse", "HEAD").strip()
    assert manager.commit_pending_uploads() == []
    assert _git(root, "rev-parse", "HEAD").strip() == head


def test_knowledge_delete_commits_only_its_file(manager):
    pid, root = rich_project(manager)
    (root / "knowledge").mkdir(exist_ok=True)
    (root / "knowledge" / "术语.md").write_text("# 术语\n", encoding="utf-8")
    commit_all(root)
    (root / "research" / "分析.md").write_text("agent 暂存\n", encoding="utf-8")
    _git(root, "add", "research/分析.md")

    result = manager.delete_file(pid, "knowledge/术语.md", author="operator_a")
    assert result["commit"] == _git(root, "rev-parse", "HEAD").strip()
    assert _head_files(root) == {"knowledge/术语.md"}
    assert git_paths(root, "diff", "--cached", "--name-only").split() == ["research/分析.md"]
