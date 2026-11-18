"""已发布知识包导入：格式判别、包布局→工作区布局转换与导入前门禁（Issue #359）。

回归的现象是：用「发布为知识包」导出的 zip 从「导入已有知识包」进来，预览与项目概览
都显示 0 对象 0 操作，文件整体嵌在包根目录里，任务却创建成功。
"""

import json
import zipfile
from pathlib import Path

import pytest

from app.services import publish as publish_service
from app.services import package_archive
from app.services.flow import model as flow_model
from app.services.workspace import (
    IMPORTED_PACKAGE_DIR,
    WorkspaceError,
    WorkspaceManager,
)
from tests.text_versions import write_text_versions


@pytest.fixture()
def manager(tmp_path: Path) -> WorkspaceManager:
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    return WorkspaceManager(tmp_path / "data", assets)


def _seed_project(manager: WorkspaceManager, name: str = "外卖订单") -> dict:
    """建一个带完整五元组的源项目，脚本同时覆盖 ASCII 名与中文名。"""
    meta = manager.create_project(name)
    root = manager.project_root(meta["id"])
    (root / "ontology" / "object" / "订单.md").write_text(
        "# 订单\n\n一笔外卖订单\n", encoding="utf-8"
    )
    (root / "ontology" / "object" / "商家.md").write_text(
        "# 商家\n\n出餐方\n", encoding="utf-8"
    )
    (root / "ontology" / "operation" / "创建订单.md").write_text(
        "# 创建订单\n\n生成[[订单]]\n", encoding="utf-8"
    )
    (root / "ontology" / "rule" / "限购规则.md").write_text(
        "# 限购规则\n\n约束[[订单]]数量\n", encoding="utf-8"
    )
    (root / "ontology" / "metric" / "客单价.md").write_text(
        "# 客单价\n\n基于[[订单]]金额\n", encoding="utf-8"
    )
    for key, code in {
        # 库模块：不对外调用，包内保留原文件名
        "calc_price": "def run():\n    return {}\n",
        # 对外脚本：包内按 ASCII 文件名打包，导入时按描述符还原原名
        "计算折扣": '"""\n关联operation：创建订单\n"""\ndef run():\n    return {}\n',
    }.items():
        (root / "ontology" / "scripts" / f"{key}.py").write_text(code, encoding="utf-8")
    return meta


def _export_package(manager: WorkspaceManager, project_id: str, dest: Path) -> Path:
    publish_service.export_package_zip(manager, project_id, dest)
    return dest


def _overview(manager: WorkspaceManager, project_id: str) -> dict:
    """项目概览里的对象/操作计数——Issue 里显示 0 的就是这两个数。"""
    return next(p for p in manager.list_projects() if p["id"] == project_id)


def _strip_wrapper(source: Path, dest: Path, root: str) -> Path:
    """把包根外层目录去掉重新打包：验证单层包根与无外层目录两种形态都能吃。"""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(dest, "w") as out:
        for name in src.namelist():
            out.writestr(name[len(root) :], src.read(name))
    return dest


def test_published_package_import_restores_entities(
    manager: WorkspaceManager, tmp_path: Path
):
    source = _seed_project(manager)
    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")

    imported = manager.import_package_zip("回导任务", archive)
    root = manager.project_root(imported["id"])

    # 五元组落到工作区的单数目录，而不是留在包里的复数目录
    assert (root / "ontology" / "object" / "订单.md").is_file()
    assert (root / "ontology" / "object" / "商家.md").is_file()
    assert (root / "ontology" / "operation" / "创建订单.md").is_file()
    assert (root / "ontology" / "rule" / "限购规则.md").is_file()
    assert (root / "ontology" / "metric" / "客单价.md").is_file()
    assert (root / "ontology" / "scripts" / "calc_price.py").is_file()
    # 包根目录不再作为额外目录留在工作区里
    assert not (root / source["id"]).exists()
    assert not (root / "objects").exists()

    # 概览计数：Issue 里显示 0 的两个数
    overview = _overview(manager, imported["id"])
    assert overview["objects"] == 2
    assert overview["operations"] == 1
    # 已发布知识包只有交付物、没有过程资产：research/ 等必选阶段不命中，但不截断后续，
    # 提取对象、提取操作两个必需节点各自计入；指标、代码生成是可选节点，做完也不算进度（节点模型重构 2a）
    assert overview["stages_done"] == 2
    # 只算必需节点；包里没有资料分析，提取对象已完成：资料分析算越过、不计入分母（ADR-0084）
    assert overview["stages_total"] == sum(
        1 for node in flow_model.image_flow()["nodes"] if node["need"] == "required") - 1

    # 工作区文档不带包契约 frontmatter，正文原样
    body = (root / "ontology" / "operation" / "创建订单.md").read_text(encoding="utf-8")
    assert not body.startswith("---")
    assert "生成[[订单]]" in body
    # 包里的 index.md 是派生物：不搬运，按工作区规则重建
    index = (root / "ontology" / "object" / "index.md").read_text(encoding="utf-8")
    assert "# 对象目录" in index and "订单.md" in index


def test_preview_and_import_report_the_same_counts(
    manager: WorkspaceManager, tmp_path: Path
):
    """预览与正式导入共用同一判别函数：不能再出现"预览 0、导入照建"。"""
    source = _seed_project(manager)
    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")

    summary = manager.import_summary(archive)
    assert summary["kind"] == package_archive.KIND_KNOWLEDGE_PACKAGE
    assert summary["kind_label"] == "已发布知识包"
    assert summary["package_root"] == f"{source['id']}/"
    assert summary["package_name"] == "外卖订单"
    assert summary["package_version"].startswith("0.1.")
    assert summary["blockers"] == []
    assert (summary["objects"], summary["operations"]) == (2, 1)
    assert (summary["rules"], summary["metrics"], summary["scripts"]) == (1, 1, 2)

    imported = manager.import_package_zip("回导任务", archive)
    overview = _overview(manager, imported["id"])
    assert (summary["objects"], summary["operations"]) == (
        overview["objects"],
        overview["operations"],
    )


def test_package_without_wrapper_directory_is_recognized(
    manager: WorkspaceManager, tmp_path: Path
):
    source = _seed_project(manager)
    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")
    flat = _strip_wrapper(archive, tmp_path / "flat.zip", f"{source['id']}/")

    summary = manager.import_summary(flat)
    assert summary["kind"] == package_archive.KIND_KNOWLEDGE_PACKAGE
    assert summary["package_root"] == ""
    assert summary["objects"] == 2

    imported = manager.import_package_zip("无外层目录", flat)
    root = manager.project_root(imported["id"])
    assert (root / "ontology" / "object" / "订单.md").is_file()


def test_script_filename_restored_from_descriptor(
    manager: WorkspaceManager, tmp_path: Path
):
    """中文脚本名打包时被哈希成 script_<hash>.py，导入要按描述符还原原名。"""
    source = _seed_project(manager)
    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")
    with zipfile.ZipFile(archive) as zf:
        packaged = {
            name.rsplit("/", 1)[-1]
            for name in zf.namelist()
            if "/scripts/" in name and name.endswith(".py")
        }
    assert "计算折扣.py" not in packaged  # 包内是 ASCII 文件名

    imported = manager.import_package_zip("脚本回导", archive)
    scripts = manager.project_root(imported["id"]) / "ontology" / "scripts"
    assert (scripts / "计算折扣.py").is_file()
    assert (scripts / "calc_price.py").is_file()
    assert not list(scripts.glob("script_*.py"))
    # 描述符按工作区规则重建：对外脚本有，库模块没有
    assert (scripts / "计算折扣.md").is_file()
    assert not (scripts / "calc_price.md").exists()


def test_helper_script_name_survives_round_trip(manager: WorkspaceManager, tmp_path: Path):
    """不绑定操作的辅助脚本没有描述符可还原，包内直接用原文件名。"""
    source = _seed_project(manager)
    root = manager.project_root(source["id"])
    (root / "ontology" / "scripts" / "公共工具.py").write_text(
        '"""\n关联operation：无\n"""\ndef helper():\n    return 1\n', encoding="utf-8"
    )
    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")
    imported = manager.import_package_zip("辅助脚本回导", archive)
    scripts = manager.project_root(imported["id"]) / "ontology" / "scripts"
    assert (scripts / "公共工具.py").is_file()
    assert (scripts / "计算折扣.py").is_file()


def test_original_package_is_preserved_for_fidelity(
    manager: WorkspaceManager, tmp_path: Path
):
    """原包整份留底：对象↔数据表绑定只记在包里，剥 frontmatter 后别处没有。"""
    source = _seed_project(manager)
    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")

    imported = manager.import_package_zip("留底", archive)
    preserved = manager.project_root(imported["id"]) / IMPORTED_PACKAGE_DIR
    assert (preserved / "manifest.yaml").is_file()
    assert "kind: ontology-package" in (preserved / "manifest.yaml").read_text(
        encoding="utf-8"
    )
    # 留底的是收到的原始文档（带包契约 frontmatter），工作区那份已剥掉
    assert (preserved / "objects" / "订单.md").read_text(encoding="utf-8").startswith(
        "---"
    )
    assert (preserved / "objects" / "index.md").is_file()
    # 留底区不是用户可编辑内容：不进文件树、不可写
    tree_tops = {node["name"] for node in manager.file_tree(imported["id"])}
    assert IMPORTED_PACKAGE_DIR not in tree_tops
    with pytest.raises(WorkspaceError, match="不允许编辑"):
        manager.write_file(
            imported["id"], f"{IMPORTED_PACKAGE_DIR}/manifest.yaml", "x"
        )


def test_object_data_mapping_survives_import(manager: WorkspaceManager, tmp_path: Path):
    """绑定了真实表的包：mappings 与 sources frontmatter 都要还能找回。"""
    openpyxl = pytest.importorskip("openpyxl")

    source = manager.create_project("绑定项目")
    root = manager.project_root(source["id"])
    (root / "ontology" / "object" / "订单.md").write_text(
        "# 数据来源\nORDER_TABLE\n\n"
        "# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
        "|--------|--------|-----|----|-----|--------|--------|\n"
        "| 订单号 | ORDER_ID | 编号 | 是 | 字符串 | 无 | 无 |\n",
        encoding="utf-8",
    )
    (root / "ontology" / "operation" / "创建订单.md").write_text(
        "# 创建订单\n\n生成[[订单]]\n", encoding="utf-8"
    )
    excel = root / "resource" / "excel"
    excel.mkdir(parents=True, exist_ok=True)
    workbook = openpyxl.Workbook()
    workbook.active.title = "ORDER_TABLE"
    workbook.active.append(["ORDER_ID"])
    workbook.save(excel / "数据表.xlsx")
    write_text_versions(root)  # 资料区的 Excel 只读文本版（F4c）
    (root / "resource" / "materials.json").write_text(
        json.dumps({"resource/excel/数据表.xlsx": {"purpose": "schema"}}, ensure_ascii=False),
        encoding="utf-8",
    )

    archive = _export_package(manager, source["id"], tmp_path / "pkg.zip")
    imported = manager.import_package_zip("绑定回导", archive)
    preserved = manager.project_root(imported["id"]) / IMPORTED_PACKAGE_DIR

    mapping = json.loads(
        (preserved / "mappings" / "object-data-mappings.json").read_text(encoding="utf-8")
    )
    assert mapping["objects"]["订单"]["resource"] == "ORDER_TABLE"
    assert 'resource: "ORDER_TABLE"' in (preserved / "objects" / "订单.md").read_text(
        encoding="utf-8"
    )
    assert _overview(manager, imported["id"])["objects"] == 1


def test_workspace_ontology_zip_keeps_existing_path(
    manager: WorkspaceManager, tmp_path: Path
):
    """「导出知识包（原始产物）」那条老路径不变：仍判为工作区内容、仍原样铺开。"""
    source = _seed_project(manager)
    archive = tmp_path / "ontology.zip"
    manager.export_zip(source["id"], "ontology", archive)

    summary = manager.import_summary(archive)
    assert summary["kind"] == package_archive.KIND_WORKSPACE_ONTOLOGY
    assert summary["blockers"] == []
    assert (summary["objects"], summary["operations"]) == (2, 1)

    imported = manager.import_zip("原始产物回导", archive)
    root = manager.project_root(imported["id"])
    assert (root / "ontology" / "object" / "订单.md").is_file()
    assert not (root / IMPORTED_PACKAGE_DIR).exists()
    with pytest.raises(WorkspaceError, match="这不是已发布知识包"):
        manager.import_package_zip("走错方法", archive)


def test_workspace_transfer_zip_points_to_the_other_entry(
    manager: WorkspaceManager, tmp_path: Path
):
    source = _seed_project(manager)
    archive = tmp_path / "transfer.zip"
    manager.export_zip(source["id"], "workspace", archive)

    summary = manager.import_summary(archive)
    assert summary["kind"] == package_archive.KIND_WORKSPACE_TRANSFER
    assert summary["blockers"] and "导入项目" in summary["blockers"][0]


def test_unknown_archive_lists_actual_contents(manager: WorkspaceManager, tmp_path: Path):
    archive = tmp_path / "unknown.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("随手打包/说明.txt", "x")

    summary = manager.import_summary(archive)
    assert summary["kind"] == package_archive.KIND_UNKNOWN
    assert summary["blockers"] and "随手打包" in summary["blockers"][0]


def test_empty_package_is_rejected_without_creating_a_project(
    manager: WorkspaceManager, tmp_path: Path
):
    """零对象的包必须在建项目之前拒掉——"导入成功但是空"的直接成因。"""
    archive = tmp_path / "empty.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("pkg/manifest.yaml", "kind: ontology-package\nname: 空包\n")
        zf.writestr("pkg/objects/index.md", "# 对象目录\n")
        zf.writestr("pkg/operations/index.md", "# 操作目录\n")

    summary = manager.import_summary(archive)
    assert summary["kind"] == package_archive.KIND_KNOWLEDGE_PACKAGE
    assert summary["objects"] == 0
    assert summary["blockers"] and "没有任何对象定义" in summary["blockers"][0]

    before = len(manager.list_projects())
    with pytest.raises(WorkspaceError, match="没有任何对象定义"):
        manager.import_package_zip("空包", archive)
    assert len(manager.list_projects()) == before


def test_corrupt_zip_is_rejected(manager: WorkspaceManager, tmp_path: Path):
    broken = tmp_path / "broken.zip"
    broken.write_bytes(b"not a zip at all")
    with pytest.raises(WorkspaceError, match="压缩包无法解析"):
        manager.import_summary(broken)
    before = len(manager.list_projects())
    with pytest.raises(WorkspaceError, match="压缩包无法解析"):
        manager.import_package_zip("坏包", broken)
    assert len(manager.list_projects()) == before


def test_unsafe_members_are_not_written(manager: WorkspaceManager, tmp_path: Path):
    archive = tmp_path / "evil.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("pkg/manifest.yaml", "kind: ontology-package\nname: 带穿越\n")
        zf.writestr("pkg/objects/订单.md", "---\nresource: object\n---\n\n# 订单\n")
        zf.writestr("../outside.md", "x")
        zf.writestr("pkg/.git/config", "x")

    imported = manager.import_package_zip("穿越", archive)
    root = manager.project_root(imported["id"])
    assert (root / "ontology" / "object" / "订单.md").is_file()
    assert not (root.parent / "outside.md").exists()
    assert not (root / IMPORTED_PACKAGE_DIR / ".git").exists()


def test_import_summary_counts_unchanged_for_workspace_layout(
    manager: WorkspaceManager, tmp_path: Path
):
    """原有摘要口径（含 .git 跳过与 ontology/logic 旧目录）保持不变。"""
    archive = tmp_path / "in.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        zf.writestr("ontology/object/订单.md", "# 订单")
        zf.writestr("ontology/logic/创建订单.md", "# 创建订单")
        zf.writestr("research/summary.md", "资料分析")
        zf.writestr(".git/config", "skip me")

    summary = manager.import_summary(archive)
    assert (summary["objects"], summary["operations"], summary["files"]) == (1, 1, 4)
    assert ".git" in summary["skipped"]
    assert [d["name"] for d in summary["top_dirs"]] == ["ontology", "research"]


@pytest.mark.parametrize("business_name", ["synthetic_a/b", 'synthetic_甲/乙\\"丙', "synthetic_a%2Fb"])
def test_business_name_survives_repeated_package_roundtrip(manager, tmp_path, business_name):
    """Synthetic entities keep safe IDs and original names across publication/import."""
    from urllib.parse import quote
    from app.services import studio
    from app.services.document_metadata import report_name

    source = manager.create_project("workspace_demo")
    root = manager.project_root(source["id"])
    body = "# 业务描述\n\nsynthetic description\n"
    identities = {}
    for kind in ("object", "operation", "rule", "metric"):
        original = f"{kind}_{business_name}"
        stem = quote(original, safe="")
        identities[kind] = (stem, original)
        (root / "ontology" / kind / f"{stem}.md").write_text(
            "---\nreport_name: " + json.dumps(original, ensure_ascii=False)
            + "\n---\n" + body, encoding="utf-8",
        )
    (root / "ontology/metric/synthetic_plain.md").write_text(body, encoding="utf-8")
    (root / "ontology/scripts/synthetic_script.py").write_text("def run():\n    return {}\n", encoding="utf-8")

    for cycle in range(2):
        archive = tmp_path / f"synthetic_{cycle}.zip"
        publish_service.export_package_zip(manager, source["id"], archive, allow_unbound=True)
        with zipfile.ZipFile(archive) as zf:
            for kind, (stem, original) in identities.items():
                text = zf.read(f"{source['id']}/{kind}s/{stem}.md").decode("utf-8")
                assert report_name(text) == original
                assert f"key: {json.dumps(stem)}" in text
                assert text.count("report_name:") == 1
        source = manager.import_package_zip("workspace_demo_import", archive)
        root = manager.project_root(source["id"])
        nodes = {node["id"]: node for node in studio.graph(root)["nodes"]}
        for kind, (stem, original) in identities.items():
            path = f"ontology/{kind}/{stem}.md"
            text = (root / path).read_text(encoding="utf-8")
            assert report_name(text) == original
            assert text.endswith(body)
            assert "resource:" not in text and "uses:" not in text
            assert nodes[stem]["label"] == original
            assert studio.element(root, path)["label"] == original
        assert (root / "ontology/metric/synthetic_plain.md").read_text() == body
        assert (root / "ontology/scripts/synthetic_script.py").exists()


@pytest.mark.parametrize("metadata", [
    'report_name: "synthetic_a/b"\nreport_name: "synthetic_other"',
    'report_name: invalid',
    'report_name: 123',
])
def test_invalid_business_name_blocks_export_and_import(manager, tmp_path, metadata):
    """Do not silently discard an explicit but invalid business-name mapping."""
    raw = f"---\n{metadata}\n---\n# synthetic document\n"
    with pytest.raises(WorkspaceError):
        package_archive._workspace_document(raw)
    source = manager.create_project("workspace_demo")
    path = manager.project_root(source["id"]) / "ontology/operation/synthetic_plain.md"
    path.write_text(raw, encoding="utf-8")
    with pytest.raises(WorkspaceError):
        publish_service.export_package_zip(manager, source["id"], tmp_path / "synthetic_invalid.zip", allow_unbound=True)
