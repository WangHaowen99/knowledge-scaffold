"""B4c 审查确认的缺陷（合并进交付分支后修）：每条一个回归用例，修复前失败、修复后通过。

用仓库里真实的官方包；项目、场景、流程图、知识与用量全部是合成内容。
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import threading
import time
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services.packs import builtin
from app.services.workspace import WorkspaceManager

ASSETS = Path(config.ASSETS_DIR)
SCENARIO_REL = "ontology/testcase/补货编制主流程.md"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", ASSETS)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    yield manager, TestClient(app)
    builtin.clear_cache()


def _commit_all(root: Path, message: str = "agent: 生成") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True, capture_output=True)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


# ---- 9. core 的 stable 回滚到拆分前：跟随的项目不跟，回滚选项不给 ----


def test_core_rolled_back_before_split_is_not_followed_or_offered(env, tmp_path):
    from app.services.packs import lockfile, service as packs_service

    manager, client = env
    project_id = manager.create_project("回滚")["id"]
    root = manager.project_root(project_id)
    repository = manager.packs.repository
    split_core = lockfile.read(root).get("core").version
    staged = tmp_path / "old-core"
    builtin.stage(ASSETS / "workspace-toolkit", staged, extras_from=ASSETS, base_version="0.1.0")
    repository.install(staged, origin="builtin")
    old_core = builtin.mf.load(staged).version
    assert old_core.startswith("0.1.0+")

    items = {item["name"]: item for item in client.get(f"/api/v1/projects/{project_id}/packs").json()["items"]}
    assert items["core"]["rollback_to"] is None and "rollback" not in items["core"]["actions"]

    repository.set_channel("core", "stable", old_core, by="ops")  # 运维把 stable 指回拆分前
    status = packs_service.prepare_for_message(manager, project_id)
    assert lockfile.read(root).get("core").version == split_core
    assert status["state"] == "applied" and not status.get("last_error")


# ---- 10. 官方身份只认镜像内置；清单里的路径不能出包 ----


def test_uploaded_pack_claiming_official_gets_no_legacy_dirs(tmp_path):
    import yaml

    from app.services.packs import checks
    from app.services.packs import manifest as mf

    directory = tmp_path / "evil"
    (directory / "artifacts").mkdir(parents=True)
    (directory / "commands").mkdir()
    (directory / "commands" / "run.md").write_text("合成命令\n", encoding="utf-8")
    (directory / "artifacts" / "k.yaml").write_text(yaml.safe_dump(
        {"type": "evil.k", "path": "ontology/solver/*/binding.yaml", "format": "yaml"}), encoding="utf-8")
    (directory / "manifest.yaml").write_text(yaml.safe_dump({
        "name": "evil", "version": "1.0.0", "kind": "capability", "category": "capability", "label": "合成",
        "kernel_api": ">=1.0 <2.0", "engines": ["claude"], "publisher": {"kind": "official", "label": "平台"},
        "contributes": {"commands": ["run"], "artifacts": ["artifacts/k.yaml"]}}), encoding="utf-8")
    _, items = checks.run(directory, None, tenant=False)
    by_id = {item["id"]: item for item in items}
    assert by_id["entity_types"]["result"] == "fail" and "ext/evil/" in by_id["entity_types"]["detail"]
    assert by_id["names"]["result"] == "fail"

    for field, value in (("knowledge_dir", "../../outside"), ("knowledge_dir", "/etc"), ("readme", "..\\x.md")):
        (directory / "manifest.yaml").write_text(yaml.safe_dump({
            "name": "evil", "version": "1.0.0", "kind": "knowledge", "category": "industry", "label": "合成",
            "kernel_api": ">=1.0 <2.0", "contributes": {field: value}}), encoding="utf-8")
        with pytest.raises(mf.ManifestError):
            mf.load(directory)
