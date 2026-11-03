"""V 阶段真实模型发现：助手副本里 `resource` 是平台挂的链接（指向主线工作区的资料区），
资料分析检查器按 resolve 判「越出工作区」，报「来源无法核验」，助手只好自己包一层绕过检查。"""
from __future__ import annotations

import importlib

import pytest

from app import config
from pathlib import Path

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def toolkit(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("research_checker"), importlib.import_module("table_profile")


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    main = tmp_path / "workspaces" / "p1"
    (main / "resource" / "others").mkdir(parents=True)
    (main / "resource" / "excel").mkdir(parents=True)
    (main / "resource" / "others" / "合成制度.md").write_text("# 合成制度\n\n补货阈值为安全库存的 1.2 倍。\n", encoding="utf-8")
    (main / "resource" / "excel" / "合成表.csv").write_text("store_code,sku_code\nS001,P1\n", encoding="utf-8")
    (main / "resource" / "materials.json").write_text(
        '{"resource/excel/合成表.csv": {"purpose": "schema", "reading_mode": "on_demand"}}', encoding="utf-8")
    (tmp_path / "outside.md").write_text("工作区外的文件\n", encoding="utf-8")
    copy = tmp_path / "changesets" / "p1" / "assistant"
    copy.mkdir(parents=True)
    (copy / "resource").symlink_to(Path("../../../workspaces/p1/resource"))
    return copy


def test_sources_under_platform_resource_link_are_readable(toolkit, copy_root):
    checker, _ = toolkit
    ctx = checker.CheckContext(copy_root, {})
    assert "1.2 倍" in ctx.source_text("resource/others/合成制度.md")


def test_registered_tables_under_resource_link_are_profiled(toolkit, copy_root):
    _, table_profile = toolkit
    assert table_profile.within_workspace(copy_root, copy_root / "resource" / "excel" / "合成表.csv")
    assert "合成表" in str(table_profile.table_headers(copy_root)) or table_profile.table_headers(copy_root)


def test_paths_escaping_the_link_target_are_still_rejected(toolkit, copy_root):
    checker, table_profile = toolkit
    escape = copy_root / "resource" / ".." / ".." / ".." / ".." / "outside.md"
    assert not table_profile.within_workspace(copy_root, escape)
    with pytest.raises(ValueError):
        checker.CheckContext(copy_root, {}).source_text("resource/../../../../outside.md")
    (copy_root / "notes").mkdir()
    (copy_root / "notes" / "link.md").symlink_to(copy_root.parent.parent.parent / "outside.md")
    with pytest.raises(ValueError):
        checker.CheckContext(copy_root, {}).source_text("notes/link.md")


def test_interrupted_in_copy_stays_resumable_when_mainline_moved_before_interrupt(tmp_path, monkeypatch):
    """V 阶段真实模型发现：一轮跑着时用户在资料区改了用途（主线前进），停机时续跑卡按副本算指纹说「输入没有变化」，
    重启后按主线算就成了「输入改过了」，点续跑被拒。指纹一律按主线算，两处说法一致。"""
    from app.kernel.changesets import worktree
    from app.services.flow import model
    from app.services.session_host import exec_records
    from app.services.workspace import WorkspaceManager

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    meta = manager.create_project("指纹演示", owner="operator_a")
    project_id = meta["id"]
    manager.save_resource(project_id, "others", "合成制度.md", "# 合成制度\n".encode("utf-8"), author="operator_a")
    main = manager.project_root(project_id)
    with manager._project_lock(project_id):
        copy = worktree.ensure(main)
    flow = model.load_for(main)
    record = exec_records.start(project_id, "default", root=copy, flow=flow, command="/ontology-research",
                                command_name="research")
    # 一轮跑着时用户在资料区补了一份资料：主线前进，副本还停在开始时
    manager.save_resource(project_id, "others", "补充说明.md", "# 补充\n".encode("utf-8"), author="operator_a")
    interrupted = exec_records.interrupt(project_id, record["id"], "shutdown", root=copy, flow=model.load_for(copy))
    card_view = exec_records.view(interrupted, root=copy, flow=model.load_for(copy))
    main_view = exec_records.view(interrupted, root=main, flow=model.load_for(main))
    assert card_view["resumable"] is main_view["resumable"] is True


def test_withdrawn_reason_does_not_repeat_the_prefix(tmp_path, monkeypatch):
    """界面写「这个问题已失效：<原因>」，原因里再写一遍「这个问题已失效」就重复了（V 阶段截图）。"""
    from app.services import agent_sessions

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    agent_sessions.append_history("p-v", "default", {"type": "question_asked", "payload": {"question_id": "q1"}})
    agent_sessions.repair_history("p-v", "default")
    withdrawn = [e for e in agent_sessions.load_history("p-v", "default") if e.get("type") == "question_withdrawn"]
    assert withdrawn and "失效" not in withdrawn[-1]["payload"]["reason"]


def test_newer_build_of_bundled_core_is_not_offered_as_rollback(tmp_path, monkeypatch):
    """V 阶段真实环境发现：镜像更新后 core 换了内容摘要（同一基础版本），项目锁着旧的一版时，
    PL-05 把新的一版当成「回滚到」给出来。同一基础版本只回滚到更早入库的那一版。"""
    import shutil

    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth
    from app.services.packs import builtin, lockfile

    assets = Path(config.ASSETS_DIR)
    builtin.clear_cache()
    from app.services.workspace import WorkspaceManager

    manager = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    client = TestClient(app)
    project_id = manager.create_project("新版本不算回滚")["id"]
    root = manager.project_root(project_id)
    locked = lockfile.read(root).get("core").version
    base = locked.split("+")[0]
    source = tmp_path / "toolkit-next"
    shutil.copytree(assets / "workspace-toolkit", source, ignore=shutil.ignore_patterns("__pycache__"))
    (source / "scripts" / "v_synthetic_note.py").write_text("# 合成改动\n", encoding="utf-8")
    staged = tmp_path / "core-next"
    builtin.stage(source, staged, extras_from=assets, base_version=base)
    manager.packs.repository.install(staged, origin="builtin")
    newer = builtin.mf.load(staged).version
    assert newer != locked and newer.startswith(base + "+")

    items = {item["name"]: item for item in client.get(f"/api/v1/projects/{project_id}/packs").json()["items"]}
    assert items["core"]["rollback_to"] is None and "rollback" not in items["core"]["actions"]
    builtin.clear_cache()


def test_research_report_title_is_the_topic_not_the_template_heading(tmp_path, monkeypatch):
    """资料分析报告按模板第一节是「# 分析主题」，报告列表、小结卡打开的报告页都显示成「分析主题」（V 阶段截图）。"""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth
    from app.services.workspace import WorkspaceManager
    from tests.test_changesets import main_write

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    project_id = workspaces.create_project("报告标题", owner="employee_0001")["id"]
    main_write(workspaces.project_root(project_id), "research/合成门店运营.md",
               "# 分析主题\n合成门店运营。\n\n# 领域概述\n合成。\n", "research: 合成门店运营")
    with TestClient(app) as client:
        items = client.get(f"/api/v1/projects/{project_id}/reports").json()["items"]
    names = {item["id"]: item["name"] for item in items}
    assert names.get("research:合成门店运营") == "合成门店运营"


def test_new_id_confirmation_marker_is_not_a_material_problem(tmp_path, monkeypatch):
    """「缺口与待决」里的 `- [x] 新增ID确认：…` 是给检查器的记号，资料地图问题区、报告页把它当「疑似缺失」
    列出一长串编号（V 阶段截图）。"""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth
    from app.services.workspace import WorkspaceManager
    from tests.test_changesets import main_write

    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(auth, "ENABLED", False)
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    project_id = workspaces.create_project("记号不算问题", owner="employee_0001")["id"]
    report = ("# 分析主题\n合成。\n\n# 缺口与待决\n- [x] 新增ID确认：ITEM-01、ITEM-02、RULE-01\n"
              "- [ ] 缺少退货流程的资料\n")
    main_write(workspaces.project_root(project_id), "research/合成.md", report, "research: 合成")
    with TestClient(app) as client:
        problems = client.get(f"/api/v1/projects/{project_id}/materials/map").json()["problems"]
    assert [p["text_md"] for p in problems] == ["缺少退货流程的资料"]
