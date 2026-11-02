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
