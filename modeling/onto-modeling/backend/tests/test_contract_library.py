"""共享契约库按锁定版本加载（方案 A16、D5；详设 8.1 第 5 步、P1 验收「不再出现三版本并存」）。"""

from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

from app import config
from app.kernel import metamodel
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


def test_write_path_syncs_descriptors_with_locked_version(two_versions, monkeypatch):
    """写入事务的派生步骤（脚本描述符）用项目锁定版本的契约库，和 agent 在工作区里跑的同一份。"""
    from tests.test_script_descriptor import REVIEW_SCRIPT

    manager, _v1, v2 = two_versions
    # 契约库按版本缓存：别的用例在它自己的数据目录里装过（并篡改过）同一个版本，这里从空缓存开始
    monkeypatch.setattr(contracts, "_LIBRARIES", {})
    project_id = manager.create_project("描述符")["id"]
    root = manager.project_root(project_id)
    lock = lockfile.read(root)
    lock.upsert(manager.packs.entry_for("core", v2, follow=False))
    lockfile.write(root, lock)
    manager.packs.apply(root)
    locked = contracts.for_project(root).script_descriptor
    image = contracts.default().script_descriptor
    assert locked is not image

    calls = []
    original = locked.sync_workspace_descriptors

    def recording(ontology):
        calls.append(Path(ontology))
        return original(ontology)

    def image_used(_ontology):
        raise AssertionError("写入路径不该用镜像版本的描述符实现")

    monkeypatch.setattr(locked, "sync_workspace_descriptors", recording)
    monkeypatch.setattr(image, "sync_workspace_descriptors", image_used)
    result = manager.write_file(project_id, "ontology/scripts/order_review.py", REVIEW_SCRIPT, author="operator_a")
    assert result["commit"] and calls == [root / "ontology"]
    # 目录规范化（存量项目补描述符）也用同一份
    manager.ensure_package_layout(project_id)
    assert calls == [root / "ontology"] * 2


# ---- 元模型由契约库按版本加载（方案 C3 与 A16 合流）----

METAMODEL_MARK = "\n# 测试用：标记这是第二个版本\nMETAMODEL_TEST_MARK = 'v2'\n"


@pytest.fixture()
def metamodel_versions(tmp_path: Path, monkeypatch):
    """同一个数据目录里两个 core 版本：镜像原样的，和 onto_metamodel.py 改过的。"""
    monkeypatch.setattr(contracts, "_LIBRARIES", {})
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    real = WorkspaceManager(data, Path(config.ASSETS_DIR))
    v1 = real.packs.core_version()
    modified = tmp_path / "assets2"
    shutil.copytree(config.ASSETS_DIR, modified, ignore=shutil.ignore_patterns("__pycache__"))
    target = modified / "workspace-toolkit" / "scripts" / "onto_metamodel.py"
    target.write_text(target.read_text(encoding="utf-8") + METAMODEL_MARK, encoding="utf-8")
    v2 = WorkspaceManager(data, modified).packs.core_version()
    builtin.clear_cache()
    assert v1 != v2
    return real, v1, v2


def test_core_manifest_declares_metamodel_script():
    from app.services.packs import checks

    with_extras = Path(config.ASSETS_DIR) / "workspace-toolkit"
    _manifest, items = checks.run(with_extras)
    declared = next(item for item in items if item["id"] == "declared")
    assert declared["result"] == "pass", declared


def test_contract_scripts_share_their_versions_metamodel():
    lib = contracts.default()
    assert lib.onto_metamodel.__name__ == f"{lib.prefix}.onto_metamodel"
    # 兄弟脚本 `import onto_metamodel` 解析到同一版本，不再往 sys.path 里塞镜像目录
    assert lib.operation_relations.mm is lib.onto_metamodel
    assert lib.script_argument_shape.mm is lib.onto_metamodel
    assert metamodel.mm.__name__.startswith("onto_contracts.image_")  # 导入期常量用镜像版本


def test_metamodel_follows_project_lock_and_stable_channel(metamodel_versions):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import auth

    manager, v1, v2 = metamodel_versions
    old = manager.project_root(manager.create_project("锁定旧版")["id"])
    new = manager.project_root(manager.create_project("锁定新版")["id"])
    lock = lockfile.read(new)
    lock.upsert(manager.packs.entry_for("core", v2, follow=False))
    lockfile.write(new, lock)
    manager.packs.apply(new)
    assert not hasattr(metamodel.for_project(old), "METAMODEL_TEST_MARK")
    assert metamodel.for_project(new).METAMODEL_TEST_MARK == "v2"
    # 项目里的解析（Studio 的图谱、条目详情）用的是项目锁定的那一份
    assert metamodel.for_project(new) is contracts.for_project(new).script_argument_shape.mm

    # 没有项目上下文：跟 stable 通道走（运维推广 v2、再回滚到 v1）
    manager.packs.repository.set_channel("core", "stable", v2)
    assert metamodel.stable().METAMODEL_TEST_MARK == "v2"
    manager.packs.repository.set_channel("core", "stable", v1)
    assert not hasattr(metamodel.stable(), "METAMODEL_TEST_MARK")
    assert metamodel.stable() is not metamodel.mm  # 包仓库里的 v1，不是镜像目录那份
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(auth, "ENABLED", False)
        body = TestClient(app).get("/api/v1/metamodel").json()
    assert body["version"] == metamodel.stable().METAMODEL_VERSION


def test_stable_metamodel_falls_back_to_image_without_repository(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "empty")
    assert metamodel.stable() is contracts.default().onto_metamodel
