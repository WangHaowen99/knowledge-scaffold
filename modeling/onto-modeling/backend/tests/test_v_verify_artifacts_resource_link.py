"""V 阶段真实模型发现：助手副本里 `resource` 是平台挂的链接，轻量核验（verify_artifacts）算输入指纹时
把 resource/materials.json 判成「核验路径越出工作区」直接抛错，提交 hook 拦下校验这一轮的修改，助手转去问用户。"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from app import config

SCRIPTS = Path(config.ASSETS_DIR) / "workspace-toolkit" / "scripts"


@pytest.fixture
def verify_artifacts(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    return importlib.import_module("verify_artifacts")


@pytest.fixture
def copy_root(tmp_path: Path) -> Path:
    main = tmp_path / "workspaces" / "p1"
    (main / "resource").mkdir(parents=True)
    (main / "resource" / "materials.json").write_text("{}", encoding="utf-8")
    (tmp_path / "outside.md").write_text("工作区外的文件\n", encoding="utf-8")
    copy = tmp_path / "changesets" / "p1" / "assistant"
    (copy / "ontology" / "object").mkdir(parents=True)
    (copy / "ontology" / "object" / "门店.md").write_text("# 对象描述\n合成门店。\n", encoding="utf-8")
    (copy / "resource").symlink_to(Path("../../../workspaces/p1/resource"))
    return copy


def test_input_digest_accepts_platform_resource_link(verify_artifacts, copy_root):
    root = copy_root.resolve()
    assert verify_artifacts.input_digest(root, "ontology")


def test_links_out_of_workspace_are_still_rejected(verify_artifacts, copy_root):
    root = copy_root.resolve()
    (root / "ontology" / "object" / "外部.md").symlink_to(root.parent.parent.parent / "outside.md")
    with pytest.raises(ValueError):
        verify_artifacts.input_digest(root, "ontology")
