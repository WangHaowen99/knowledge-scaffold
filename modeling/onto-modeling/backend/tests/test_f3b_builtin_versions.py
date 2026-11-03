"""内置包的版本后缀把清单也算进摘要（F3b）。

F3 把 codegen、evo 清单里的说明文字改了、别的文件没动：版本后缀只按清单以外的文件算摘要，摘要不变，新镜像启动时
「codegen 0.9.2+… 已在包仓库里且内容不同」，装配一步出错。现在摘要算上清单（版本号除外），只改清单也换一个版本，
跟随 stable 的项目在下一条消息前升上去、照常装配。
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from app import config
from app.services.packs import builtin, lockfile, service
from app.services.workspace import WorkspaceManager


@pytest.fixture(autouse=True)
def _fresh_builtin_cache():
    builtin.clear_cache()
    yield
    builtin.clear_cache()


def _edit_manifest(path: Path, **fields) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")


def test_manifest_only_change_gets_a_new_version_and_same_content_keeps_it(tmp_path: Path):
    source = tmp_path / "codegen"
    shutil.copytree(Path(config.ASSETS_DIR) / "packs" / "codegen", source)
    first = builtin.stage(source, tmp_path / "a").version
    assert builtin.stage(source, tmp_path / "b").version == first  # 同一份内容同一个版本
    _edit_manifest(source / "manifest.yaml", description="合成：只改了说明")
    second = builtin.stage(source, tmp_path / "c").version
    assert second != first and second.split("+")[0] == first.split("+")[0]
    # 清单里的版本号本身不算：只改基础版本号时后缀不变
    _edit_manifest(source / "manifest.yaml", version="9.9.9")
    assert builtin.stage(source, tmp_path / "d").version.split("+")[1] == second.split("+")[1]


def test_projects_following_stable_upgrade_and_assemble_after_a_manifest_only_change(tmp_path: Path):
    assets = tmp_path / "assets"
    shutil.copytree(Path(config.ASSETS_DIR), assets, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    data = tmp_path / "data"
    manager = WorkspaceManager(data, assets)
    project_id = manager.create_project("跟随升级", owner="employee_0001")["id"]
    root = manager.project_root(project_id)
    before = {entry.name: entry.version for entry in lockfile.read(root).packs}
    assert "codegen" in before

    _edit_manifest(assets / "packs" / "codegen" / "manifest.yaml", description="合成：只改了说明")
    builtin.clear_cache()
    upgraded = WorkspaceManager(data, assets)
    upgraded.packs.core_version()  # 新镜像启动时导入官方包：入库不撞版本
    stable = upgraded.packs.repository.channel("codegen", "stable")
    assert stable != before["codegen"] and stable.split("+")[0] == before["codegen"].split("+")[0]
    assert set(upgraded.packs.repository.versions("codegen")) == {before["codegen"], stable}

    service.prepare_for_message(upgraded, project_id)
    after = {entry.name: entry.version for entry in lockfile.read(root).packs}
    assert after["codegen"] == stable
    assert {name: version for name, version in after.items() if name != "codegen"} == \
        {name: version for name, version in before.items() if name != "codegen"}
    plan = upgraded.packs.assembler.plan(lockfile.read(root))
    assert plan is not None
    assert (root / ".claude").exists() and upgraded.packs.status(root).get("state") != "error"
