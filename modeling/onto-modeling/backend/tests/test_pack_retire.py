"""启动时下架镜像里已移除的官方包（builtin.retire_missing）。

规则：全部版本都由镜像入库、没有 owner、不是 core、这次的 `assets/packs` 里没有它 → 下架；`assets/packs` 整个
不存在时什么都不下架。下架后目录、详情、缺省启用、插件运维都看不到它，启动核对把它从项目锁文件里去掉，项目照常
装配、能发消息；换回带这个包的镜像取消下架，已经去掉的锁条目不加回来。

包全部合成（tests/pack_helpers）。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.ops_app import ops_app
from app.services import auth
from app.services.packs import builtin, catalog, governance, lockfile
from app.services.packs import service as pack_service
from app.services.packs.service import PackError
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack
from tests.proc import run as run_cmd
from tests.test_packs_api import publish_pack

TOKENS = {"ops-token": {"sub": "operator_ops", "role": "ops"}}
OPS = {"Authorization": "Bearer ops-token"}
GONE, KEPT = "extra-notes", "extra-keep"
OWNER = "employee_0001"


def add_official(assets: Path, name: str) -> None:
    """镜像里放一个缺省启用的官方能力包（和真实资产一样不带校验和，入库时生成）。"""
    source = make_pack(assets.parent / "src" / name, name, "0.1.0", checksums=False, default_on=True)
    shutil.copytree(source, assets / "packs" / name)


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    governance.record_reconcile({})
    data = tmp_path / "data"
    assets = make_assets(tmp_path)
    for name in (GONE, KEPT):
        add_official(assets, name)
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (TOKENS.get(token), "" if token in TOKENS else "invalid"))

    def restart() -> WorkspaceManager:
        """换镜像重启：清掉进程里的入库缓存，按现在的资产起服务并做启动核对。"""
        builtin.clear_cache()
        manager = WorkspaceManager(data, assets)
        monkeypatch.setattr(deps, "workspaces", manager)
        pack_service.reconcile_all(manager)
        return manager

    yield assets, restart, tmp_path
    builtin.clear_cache()
    governance.record_reconcile({})


def remove_from_image(assets: Path, tmp_path: Path, name: str = GONE) -> Path:
    away = tmp_path / "removed" / name
    away.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(assets / "packs" / name), str(away))
    return away


def locked(root: Path) -> set[str]:
    return {entry.name for entry in lockfile.read(root).packs}


def commands(root: Path) -> set[str]:
    return {path.stem for path in (root / ".claude" / "commands").glob("*.md")}


def last_commit(root: Path) -> str:
    return run_cmd(["git", "log", "-1", "--format=%s"], cwd=root, capture_output=True, text=True).stdout.strip()


def new_project(manager, name: str) -> tuple[str, Path]:
    project_id = manager.create_project(name, owner=OWNER)["id"]
    return project_id, manager.project_root(project_id)


def test_pack_removed_from_image_is_retired_and_projects_still_assemble(env):
    assets, restart, tmp_path = env
    manager = restart()
    project_id, root = new_project(manager, "下架前")
    pack_service.prepare_for_message(manager, project_id)
    assert {GONE, KEPT} <= locked(root) and f"{GONE}-run" in commands(root)
    version = manager.packs.repository.channel(GONE, "stable")

    remove_from_image(assets, tmp_path)
    manager = restart()
    repository = manager.packs.repository
    assert repository.retired(GONE)["reason"] and repository.retired(KEPT) is None and repository.retired("core") is None
    assert repository.versions(GONE) == [version]  # 版本目录留着，只是哪里都不列
    assert GONE not in repository.names() and GONE not in repository.names(private=True)
    assert repository.channel(GONE, "stable") is None and repository.latest(GONE, "stable") is None
    assert GONE not in {item["name"] for item in catalog.catalog(manager)["items"]}
    assert GONE not in manager.packs.default_capabilities() and KEPT in manager.packs.default_capabilities()
    with pytest.raises(PackError) as missing:
        catalog.detail(manager, GONE)
    assert missing.value.status == 404

    # 启动核对已经把它从老项目的锁文件里去掉并提交；项目照常装配、能发消息
    assert GONE not in locked(root) and KEPT in locked(root)
    assert last_commit(root) == "chore: 写入能力包锁文件"
    status = pack_service.prepare_for_message(manager, project_id)
    assert status["state"] == "applied", status
    assert f"{GONE}-run" not in commands(root) and f"{KEPT}-run" in commands(root)
    with pytest.raises(PackError) as refused:
        pack_service.enable(manager, project_id, GONE)
    assert refused.value.status == 404

    _, fresh = new_project(manager, "下架后")
    assert GONE not in locked(fresh) and KEPT in locked(fresh)


def test_putting_the_pack_back_restores_it_without_re_adding_lock_entries(env):
    assets, restart, tmp_path = env
    manager = restart()
    project_id, root = new_project(manager, "回滚")
    version = manager.packs.repository.channel(GONE, "stable")
    away = remove_from_image(assets, tmp_path)
    restart()
    assert GONE not in locked(root)

    shutil.move(str(away), str(assets / "packs" / GONE))  # 换回带这个包的旧镜像
    manager = restart()
    repository = manager.packs.repository
    assert repository.retired(GONE) is None and repository.channel(GONE, "stable") == version
    assert GONE in repository.names() and GONE in manager.packs.default_capabilities()
    # 已经从锁文件里去掉的不加回来；要用的项目重新启用，新项目按 default_on 照常带上
    assert GONE not in locked(root)
    pack_service.enable(manager, project_id, GONE)
    assert lockfile.read(root).get(GONE).enabled
    _, fresh = new_project(manager, "回滚后")
    assert GONE in locked(fresh)


def test_assets_without_packs_directory_retire_nothing(env):
    assets, restart, tmp_path = env
    manager = restart()
    _, root = new_project(manager, "最小资产")
    shutil.move(str(assets / "packs"), str(tmp_path / "packs-away"))
    manager = restart()
    repository = manager.packs.repository
    assert repository.retired(GONE) is None and repository.retired(KEPT) is None
    assert {GONE, KEPT} <= set(repository.names()) and {GONE, KEPT} <= locked(root)


def test_uploaded_private_and_mixed_packs_are_never_retired(env):
    assets, restart, tmp_path = env
    manager = restart()
    repository = manager.packs.repository
    repository.install(make_pack(tmp_path / "up", "extra-upload", "0.1.0"), origin="upload", by="admin")
    repository.set_channel("extra-upload", "stable", "0.1.0", by="admin")
    repository.claim("extra-private", "employee_0002")
    repository.install(make_pack(tmp_path / "up", "extra-private", "0.1.0"), origin="private", by="employee_0002")
    # 镜像自带的包又上传过一个版本：有一个版本不是镜像入库的就不归镜像管
    repository.install(make_pack(tmp_path / "up", KEPT, "0.2.0"), origin="upload", by="admin")
    for name in (GONE, KEPT):
        remove_from_image(assets, tmp_path, name)

    repository = restart().packs.repository
    assert repository.retired(GONE) is not None  # 对照：只由镜像入库的照常下架
    for name in ("extra-upload", "extra-private", KEPT):
        assert repository.retired(name) is None, name
    assert {"extra-upload", KEPT} <= set(repository.names())
    assert repository.private_names("employee_0002") == ["extra-private"]


def test_publishing_an_upload_under_the_retired_name_brings_it_back(env, monkeypatch):
    assets, restart, tmp_path = env
    restart()
    remove_from_image(assets, tmp_path)
    manager = restart()
    assert manager.packs.repository.retired(GONE) is not None

    monkeypatch.setattr(auth, "ENABLED", False)  # 本地模式：同时是管理员
    publish_pack(TestClient(app), tmp_path / "upload", GONE, "0.2.0")
    repository = manager.packs.repository
    assert repository.retired(GONE) is None and repository.latest(GONE, "stable") == "0.2.0"
    # 有了上传的版本，再启动也不会被下架
    assert restart().packs.repository.retired(GONE) is None


def test_ops_does_not_list_or_act_on_a_retired_pack(env):
    assets, restart, tmp_path = env
    version = restart().packs.repository.channel(GONE, "stable")
    remove_from_image(assets, tmp_path)
    restart()
    client = TestClient(ops_app)
    listed = client.get("/api/v1/ops/packs", headers=OPS)
    assert listed.status_code == 200, listed.text
    names = {item["name"] for item in listed.json()["items"]}
    assert GONE not in names and KEPT in names
    rolled = client.post(f"/api/v1/ops/packs/{GONE}/rollback", headers=OPS, json={"channel": "stable", "to_version": version})
    assert rolled.status_code == 404, rolled.text
