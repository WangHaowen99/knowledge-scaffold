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
