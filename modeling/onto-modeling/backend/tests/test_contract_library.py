"""共享契约库按锁定版本加载（方案 A16、D5；详设 8.1 第 5 步、P1 验收「不再出现三版本并存」）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from app import config
from app.services.packs import builtin, contracts, lockfile
from app.services.publish.ontology_package import _contract_modules
from app.services.publish.spi import ReleaseSource
from app.services.workspace import WorkspaceManager

MARK = "\n# 测试用：标记这是第二个版本\nCONTRACT_TEST_MARK = 'v2'\n"


@pytest.fixture(autouse=True)
def _fresh_cache():
    builtin.clear_cache()
    yield
    builtin.clear_cache()


@pytest.fixture()
def two_versions(tmp_path: Path):
    """同一个数据目录里有两个 core 版本：镜像原样的，和 script_contract.py 改过的。"""
    data = tmp_path / "data"
    real = WorkspaceManager(data, Path(config.ASSETS_DIR))
    v1 = real.packs.core_version()
    modified = tmp_path / "assets2"
    shutil.copytree(config.ASSETS_DIR, modified, ignore=shutil.ignore_patterns("__pycache__"))
    target = modified / "workspace-toolkit" / "scripts" / "script_contract.py"
    target.write_text(target.read_text(encoding="utf-8") + MARK, encoding="utf-8")
    v2 = WorkspaceManager(data, modified).packs.core_version()
    builtin.clear_cache()
    assert v1 != v2
    return real, v1, v2


def test_default_library_uses_versioned_module_names():
    lib = contracts.default()
    module = lib.script_contract
    assert module.__name__.startswith("onto_contracts.image_")
    assert sys.modules[module.__name__] is module
    # 兄弟脚本解析到同一个版本：描述符里用的 ScriptContract 就是同版本 script_contract 的类
    assert lib.script_descriptor.ScriptContract is module.ScriptContract
    assert lib.script_argument_shape.sections is lib.operation_relations.sections


def test_projects_load_their_locked_version_side_by_side(two_versions, tmp_path: Path):
    manager, v1, v2 = two_versions
    old = manager.project_root(manager.create_project("锁定旧版")["id"])
    new = manager.project_root(manager.create_project("锁定新版")["id"])
    lock = lockfile.read(new)
    lock.upsert(manager.packs.entry_for("core", v2, follow=False))
    lockfile.write(new, lock)
    manager.packs.apply(new)

    lib_old, lib_new = contracts.for_project(old), contracts.for_project(new)
    assert lib_old is not lib_new
    assert not hasattr(lib_old.script_contract, "CONTRACT_TEST_MARK")
    assert lib_new.script_contract.CONTRACT_TEST_MARK == "v2"
    assert lib_new.script_descriptor.ScriptContract is lib_new.script_contract.ScriptContract
    assert lib_old.script_contract.ScriptContract is not lib_new.script_contract.ScriptContract
    # 发布渲染用的也是项目锁定的那一份
    src = ReleaseSource(project_id=new.name, commit="", tree=new, meta={}, version="0.1.0",
                        state_root=new / ".ontology-state")
    script_contract, script_descriptor, _shape = _contract_modules(src)
    assert script_contract is lib_new.script_contract and script_descriptor is lib_new.script_descriptor


def test_switch_takes_effect_without_restart(two_versions):
    """agent 看到的、后端渲染用的、会话按的是同一个版本：切换后下一次调用就是新版本。"""
    manager, v1, v2 = two_versions
    root = manager.project_root(manager.create_project("切换")["id"])
    assert not hasattr(contracts.module_for(root, "script_contract"), "CONTRACT_TEST_MARK")
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("core", v2))
    lockfile.write(root, lock)
    # 锁文件改了但还没切（下一条消息前才切）：后端仍用生效中的旧版本，与 agent 一致
    assert not hasattr(contracts.module_for(root, "script_contract"), "CONTRACT_TEST_MARK")
    manager.packs.apply(root)
    assert contracts.module_for(root, "script_contract").CONTRACT_TEST_MARK == "v2"
    assert "CONTRACT_TEST_MARK" in (root / ".claude" / "scripts" / "script_contract.py").read_text(encoding="utf-8")


def test_tampered_script_falls_back_to_image_version(two_versions):
    manager, v1, v2 = two_versions
    root = manager.project_root(manager.create_project("篡改")["id"])
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("core", v2))
    lockfile.write(root, lock)
    manager.packs.apply(root)
    target = (root / ".claude").resolve() / "scripts" / "script_contract.py"
    target.write_text("raise SystemExit('不该被执行')\n", encoding="utf-8")
    contracts._LIBRARIES.clear()
    with pytest.raises(ImportError):
        contracts.for_project(root).load("script_contract")
    module = contracts.module_for(root, "script_contract")
    assert module is contracts.default().script_contract


def test_workspace_without_assembly_uses_image_version(tmp_path: Path):
    (tmp_path / "ws").mkdir()
    assert contracts.for_project(tmp_path / "ws") is contracts.default()
