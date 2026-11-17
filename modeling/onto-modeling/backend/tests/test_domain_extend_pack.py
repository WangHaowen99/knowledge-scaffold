"""领域包扩展（ADR-0099）：列表与启用、主线位置、和其他限定范围的包互斥、导入项目走完锁定 → 新增 → 核对、
发布关卡、助手副本里底座只读、自主构建不做「新增功能」。

用仓库里真实的资产和真实的导入流程；另一个限定范围的包是合成的（tests/pack_helpers）。项目、条目、用户都是合成的。
"""

from __future__ import annotations

import copy
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.kernel.changesets import service as changesets
from app.main import app
from app.services import auth
from app.services import publish as publish_service
from app.services.autobuild import plan
from app.services.flow import assemble, view
from app.services.packs import builtin, checks, lockfile
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import ROUND_PACK, ROUND_PACK_FLOW, install_round_pack
from tests.proc import git
from tests.test_changesets import main_write, obj
from tests.test_guard_bypass import denied, run_guard, write

ASSETS = Path(config.ASSETS_DIR)
PACK = "domain-extend"
SCRIPT = ASSETS / "packs" / PACK / "scripts" / "domain_extend.py"
SCOPE_FILE = "ext/domain-extend/scope.json"
ORDER = "ontology/object/订单.md"
PLACE = "ontology/operation/下单.md"
EXTENSION = "ontology/object/订单配送扩展.md"


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ASSETS / rel).read_text(encoding="utf-8"))


def _core() -> tuple[str, str, dict]:
    return ("core", "0.5.0", _yaml("workspace-toolkit/flow.yaml"))


def _pack(name: str = PACK) -> tuple[str, str, dict]:
    return (name, "0.1.0", _yaml(f"packs/{name}/flow.yaml"))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", ASSETS)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    monkeypatch.setattr(changesets, "_activity_provider", None)
    yield manager, TestClient(app)
    builtin.clear_cache()


def _imported(manager, client, tmp_path: Path, name: str = "领域扩展") -> tuple[str, Path]:
    """合成一个领域项目，导出成已发布知识包，再按导入流程建成新项目。"""
    source = manager.create_project("合成领域")["id"]
    root = manager.project_root(source)
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, ORDER, obj("订单", "合成订单"))
    main_write(root, PLACE, "## 逻辑描述\n\n读取[[订单]]。\n")
    archive = tmp_path / f"{name}.zip"
    publish_service.export_package_zip(manager, source, archive, allow_unbound=True)
    files = {"file": ("package.zip", archive.read_bytes(), "application/zip")}
    upload_id = client.post("/api/v1/uploads", data={"purpose": "project_import"}, files=files).json()["upload_id"]
    created = client.post("/api/v1/projects/import", json={"upload_id": upload_id, "name": name})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    return project_id, manager.project_root(project_id)


def _enable(manager, client, project_id: str, name: str = PACK):
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": name})
    if response.status_code == 201:
        pack_service.prepare_for_message(manager, project_id)
    return response


def _script(root: Path, action: str, command: str | None = None) -> str:
    """按命令拿锁后跑包里的脚本，结果合入主线（和一轮对话结束时一样）。"""
    if command:
        lock = root / ".ontology-state" / f"lock-{command}.json"
        lock.parent.mkdir(parents=True, exist_ok=True)
        lock.write_text(json.dumps({"phase": "start", "owner_pid": os.getpid()}), encoding="utf-8")
    done = subprocess.run([sys.executable, str(SCRIPT), action], cwd=root, capture_output=True, text=True, timeout=60)
    if command:
        lock.unlink()  # 命令收尾释放锁
    assert done.returncode == 0, done.stdout + done.stderr
    git(root, "add", "-A", "--", "ext", "ontology")
    if git(root, "status", "--porcelain", "--", "ext", "ontology").strip():
        git(root, "-c", "core.hooksPath=/dev/null", "commit", "-qm", f"assistant: 合成 {action}")
    return done.stdout


def _next_command(client, project_id: str) -> str:
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    return body["suggestions"]["primary"]["action"]["command"].strip()


def _gate(client, project_id: str) -> dict | None:
    gates = client.get(f"/api/v1/projects/{project_id}/publish/check").json()["gates"]
    return next((item for item in gates if item["id"] == "flow:domain-extend.check"), None)


def _flow_file(root: Path) -> dict:
    return json.loads((root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))


# ---- 列表与启用 ----


def test_pack_is_official_optional_and_passes_static_checks(env):
    manager, client = env
    manager.packs.core_version()
    repository = manager.packs.repository
    version = repository.latest(PACK, "stable")
    assert repository.is_builtin(PACK, version)
    manifest = repository.manifest(PACK, version)
    assert manifest.raw["default_on"] is False and manifest.category == "capability"
    core_names = checks.declared_names(repository.manifest("core", repository.latest("core", "stable")),
                                       repository.version_dir("core", repository.latest("core", "stable")))
    _, items = checks.run(repository.version_dir(PACK, version), core_names=core_names, official=True)
    failed = [item for item in items if item["result"] == "fail" and item["id"] != "names"]
    assert not failed, failed
    assert next(item for item in items if item["id"] == "confine")["result"] == "pass"
    card = next(item for item in client.get("/api/v1/packs").json()["items"] if item["name"] == PACK)
    assert card["default_on"] is False and card["label"] == "领域包扩展"


def test_new_projects_do_not_get_it(env):
    manager, client = env
    project_id = manager.create_project("没启用")["id"]
    root = manager.project_root(project_id)
    assert PACK not in {entry.name for entry in lockfile.read(root).packs}
    flow = _flow_file(root)
    assert "confine" not in flow and not [node for node in flow["nodes"] if node["id"].startswith("domain-extend.")]
    assert _gate(client, project_id) is None


# ---- 主线 ----


def test_assembly_puts_the_nodes_after_modeling_and_the_check_blocks_publish():
    flow = assemble.assemble([_core(), _pack()])
    ids = [node["id"] for node in flow["nodes"]]
    assert ids.index("core.operation") < ids.index("domain-extend.bind") < ids.index("domain-extend.add") \
        < ids.index("domain-extend.check") < ids.index("core.publish")
    nodes = {node["id"]: node for node in flow["nodes"]}
    assert nodes["domain-extend.add"]["autobuild"] is False
    assert nodes["domain-extend.check"]["blocks"] == ["core.publish"]
    assert nodes["domain-extend.check"]["done"] == ["domain_extend_checked", "!domain_extend_pending",
                                                    "domain_extend_bound"]
    assert flow["confine"] == {"scope_file": SCOPE_FILE, "pack": PACK, "node": "domain-extend.bind",
                               "entry": "/domain-extend-bind"}
    assert flow["commands"]["domain-extend-add"]["node"] == "domain-extend.add"


def test_it_cannot_be_enabled_together_with_another_confining_pack(env, tmp_path):
    with pytest.raises(assemble.FlowAssemblyError, match="只能有一个包声明 confine"):
        assemble.assemble([_core(), (ROUND_PACK, "0.1.0", copy.deepcopy(ROUND_PACK_FLOW)), _pack()])
    manager, client = env
    project_id, _root = _imported(manager, client, tmp_path)
    install_round_pack(manager, tmp_path)
    assert _enable(manager, client, project_id, ROUND_PACK).status_code == 201
    response = _enable(manager, client, project_id)
    assert response.status_code == 422 and "confine" in response.text


def test_autobuild_leaves_adding_features_to_the_user(env):
    manager, _client = env
    project_id = manager.create_project("自动建模")["id"]
    root = manager.project_root(project_id)
    flow = assemble.assemble([_core(), _pack()])
    policy = {"scope": {"until_node": None, "include_optional": False}}
    units = {unit.get("node_id") or unit.get("node") or unit.get("id") for unit in plan._tail(root, flow, policy)}
    assert "domain-extend.bind" in units and "domain-extend.check" in units
    assert "domain-extend.add" not in units


def test_imported_project_walks_bind_add_check_and_publish_waits_for_the_check(env, tmp_path):
    manager, client = env
    project_id, root = _imported(manager, client, tmp_path)
    assert _enable(manager, client, project_id).status_code == 201
    assert {"domain-extend-bind", "domain-extend-add", "domain-extend-check"} <= {
        path.stem for path in (root / ".claude" / "commands").glob("*.md")}
    assert "领域包扩展（已启用）" in (root / "CLAUDE.md").read_text(encoding="utf-8")

    # 导入后建模已完成：下一步锁定底座，发布被底座核对挡着
    assert _next_command(client, project_id) == "/domain-extend-bind"
    gate = _gate(client, project_id)
    assert gate["blocking"] and gate["source"]["pack"] == PACK

    _script(root, "bind", "domain-extend-bind")
    assert json.loads((root / SCOPE_FILE).read_text(encoding="utf-8"))["editable"] == []
    assert _next_command(client, project_id) == "/domain-extend-add"

    main_write(root, EXTENSION, obj("订单配送扩展", "合成扩展，关联[[订单]]"))
    _script(root, "added", "domain-extend-add")
    assert _next_command(client, project_id) == "/domain-extend-check"
    assert _gate(client, project_id)["blocking"]

    _script(root, "check", "domain-extend-check")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert nodes["domain-extend.check"]["state"] == "confirmed"
    gate = _gate(client, project_id)
    assert not gate["blocking"] and gate["status"] == "pass"

    # 网页上改了底座：核对过期，重跑核对列成待处理，撤回之前一直挡发布
    main_write(root, ORDER, obj("订单", "人工改了底座"))
    assert _gate(client, project_id)["blocking"]
    _script(root, "check", "domain-extend-check")
    assert "- [ ] 底座对象 ontology/object/订单.md 被修改" in (root / "ext/domain-extend/check.md").read_text(
        encoding="utf-8")
    assert _gate(client, project_id)["blocking"]
    _script(root, "restore", "domain-extend-check")
    _script(root, "check", "domain-extend-check")
    assert not _gate(client, project_id)["blocking"]

    # 发布之后锁定自动解除：锁定底座变回待做，重新锁定、核对之前不能再发布
    git(root, "tag", "-a", "release/v0.1.0", "-m", "发布 0.1.0")
    main_write(root, "ontology/rule/超时配送.md", "# 超时配送\n\n引用[[订单]]。\n")
    assert _next_command(client, project_id) == "/domain-extend-bind"
    assert _gate(client, project_id)["blocking"]
    _script(root, "bind", "domain-extend-bind")
    scope = json.loads((root / SCOPE_FILE).read_text(encoding="utf-8"))
    assert EXTENSION in scope["editable"] and ORDER not in scope["editable"]

    # 停用：关卡和底座锁定一起去掉
    assert client.post(f"/api/v1/projects/{project_id}/packs/{PACK}/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    assert "confine" not in _flow_file(root) and _gate(client, project_id) is None


def test_the_assistant_copy_cannot_touch_the_base_but_can_add(env, tmp_path):
    manager, client = env
    project_id, root = _imported(manager, client, tmp_path)
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert not denied(run_guard(copy, "scope", write(ORDER), cwd=copy))  # 没启用插件的项目和以前一样

    assert _enable(manager, client, project_id).status_code == 201
    _script(root, "bind", "domain-extend-bind")
    copy = changesets.copy_for_turn(manager, project_id, root)
    for rel in (ORDER, PLACE):
        reason = run_guard(copy, "scope", write(rel), cwd=copy)
        assert denied(reason) and rel in reason
    assert not denied(run_guard(copy, "scope", write(EXTENSION), cwd=copy))
    assert denied(run_guard(copy, "scope", write(SCOPE_FILE), cwd=copy))

    assert client.post(f"/api/v1/projects/{project_id}/packs/{PACK}/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    copy = changesets.copy_for_turn(manager, project_id, root)
    assert not denied(run_guard(copy, "scope", write(ORDER), cwd=copy))
