"""增量构建包（ADR-0085）：列表与启用、主线位置、发布关卡、confine 的组装校验和静态检查、用户上传。

用仓库里真实的资产。项目、条目、用户都是合成的。
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services import publish as publish_service
from app.services.autobuild import plan
from app.services.flow import assemble, view
from app.services.packs import builtin, checks, lockfile
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_pack
from tests.test_changesets import main_write, obj
from tests.test_dev_skill import pack_tool, skill_dir
from tests.test_private_packs import AS_OWNER, MINE, post_zip, project as make_project, real_flow_env  # noqa: F401

ASSETS = Path(config.ASSETS_DIR)
PACK = ASSETS / "packs" / "incremental"
SCOPE_FILE = "incremental/scope.json"
ORDER_WITH_PACK = ["core.research", "diagram.flowchart", "core.model", "incremental.scope", "incremental.apply",
                   "incremental.check", "core.metric", "core.validate", "trace.trace", "codegen.codegen",
                   "codegen.solver", "core.publish", "core.update"]
MAINLINE = ["core.research", "core.model", "incremental.scope", "incremental.apply", "incremental.check",
            "core.validate", "trace.trace", "core.publish"]


def _yaml(rel: str) -> dict:
    return yaml.safe_load((ASSETS / rel).read_text(encoding="utf-8"))


def _core() -> tuple[str, str, dict]:
    return ("core", "0.5.0", _yaml("workspace-toolkit/flow.yaml"))


def _incremental() -> tuple[str, str, dict]:
    return ("incremental", "0.1.0", _yaml("packs/incremental/flow.yaml"))


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    manager = WorkspaceManager(tmp_path / "data", ASSETS)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    yield manager, TestClient(app)
    builtin.clear_cache()


def _flow_file(root: Path) -> dict:
    return json.loads((root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))


def _gate(client, project_id: str) -> dict | None:
    gates = client.get(f"/api/v1/projects/{project_id}/publish/check").json()["gates"]
    return next((item for item in gates if item["id"] == "flow:incremental.check"), None)


def _modeled(root: Path) -> None:
    main_write(root, "research/订单.md", "# 合成资料分析\n")
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单"))
    main_write(root, "ontology/operation/下单.md", "## 逻辑描述\n\n读取[[订单]]。\n")


def _next_command(client, project_id: str) -> str:
    body = client.get(f"/api/v1/projects/{project_id}/flow").json()
    return body["suggestions"]["primary"]["action"]["command"].strip()


# ---- 列表与启用 ----


def test_pack_is_official_optional_and_passes_static_checks(env):
    manager, client = env
    manager.packs.core_version()
    repository = manager.packs.repository
    version = repository.latest("incremental", "stable")
    assert repository.is_builtin("incremental", version)
    manifest = repository.manifest("incremental", version)
    assert manifest.raw["default_on"] is False and manifest.category == "capability"
    assert manifest.raw["depends"] == {"core": ">=0.5"}
    core_version = repository.latest("core", "stable")
    assert core_version.startswith("0.5.0")
    core_names = checks.declared_names(repository.manifest("core", core_version),
                                       repository.version_dir("core", core_version))
    _, items = checks.run(repository.version_dir("incremental", version), core_names=core_names, official=True)
    failed = [item for item in items if item["result"] == "fail" and item["id"] != "names"]
    assert not failed, failed
    assert next(item for item in items if item["id"] == "confine")["result"] == "pass"
    cards = client.get("/api/v1/packs").json()["items"]
    card = next(item for item in cards if item["name"] == "incremental")
    assert card["default_on"] is False and card["category"] == "capability" and card["label"] == "增量构建"


def test_new_projects_do_not_get_it_and_behave_as_before(env):
    manager, client = env
    project_id = manager.create_project("没启用")["id"]
    root = manager.project_root(project_id)
    assert "incremental" not in {entry.name for entry in lockfile.read(root).packs}
    flow = _flow_file(root)
    assert "confine" not in flow and not [node for node in flow["nodes"] if node["id"].startswith("incremental.")]
    _modeled(root)
    assert _next_command(client, project_id) != "/incremental-scope"
    assert _gate(client, project_id) is None


# ---- 主线 ----


def test_assembly_puts_the_nodes_between_modeling_and_validation():
    flow = assemble.assemble([_core(), _incremental()])
    ids = [node["id"] for node in flow["nodes"]]
    assert ids.index("core.model") < ids.index("incremental.scope") < ids.index("incremental.apply") \
        < ids.index("incremental.check") < ids.index("core.validate") < ids.index("core.publish")
    nodes = {node["id"]: node for node in flow["nodes"]}
    for name in ("scope", "apply", "check"):
        node = nodes[f"incremental.{name}"]
        assert node["placement"] == "spine" and node["section"] == "required" and node["autobuild"] is False
    assert nodes["incremental.check"]["blocks"] == ["core.publish"]
    assert nodes["incremental.check"]["stale_on"] == ["object", "operation", "rule", "metric"]
    assert flow["commands"]["incremental-scope"]["node"] == "incremental.scope"
    assert flow["confine"] == {"scope_file": SCOPE_FILE, "pack": "incremental", "node": "incremental.scope",
                               "entry": "/incremental-scope", "lock_on_activation": True}
    assert "confine" not in assemble.assemble([_core()])


def test_enabled_project_walks_the_mainline_and_publish_waits_for_the_check(env):
    manager, client = env
    project_id = manager.create_project("增量构建")["id"]
    root = manager.project_root(project_id)
    _modeled(root)
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "incremental"})
    assert response.status_code == 201, response.text
    pack_service.prepare_for_message(manager, project_id)

    flow = _flow_file(root)
    assert [node["id"] for node in flow["nodes"]] == ORDER_WITH_PACK
    assert [node["id"] for node in flow["nodes"] if node["id"] in MAINLINE] == MAINLINE
    assert flow["confine"]["scope_file"] == SCOPE_FILE
    assert {"incremental-scope", "incremental-apply", "incremental-check"} <= {
        path.stem for path in (root / ".claude" / "commands").glob("*.md")}
    assert (root / ".claude" / "scripts" / "incremental_scope.py").is_file()

    # 没传新资料：节点不出现，不挡发布
    assert _next_command(client, project_id) != "/incremental-scope" and _gate(client, project_id) is None
    manager.save_resource(project_id, "others", "新资料.md", "# 合成资料\n".encode("utf-8"), author="tester")

    # 建了一部分又传了新资料：下一步是圈定范围；发布被传播核对挡着
    assert _next_command(client, project_id) == "/incremental-scope"
    gate = _gate(client, project_id)
    assert gate["blocking"] and gate["status"] == "not_run" and gate["source"]["pack"] == "incremental"

    main_write(root, SCOPE_FILE, json.dumps({"status": "active"}))
    assert _next_command(client, project_id) == "/incremental-apply"
    # 范围内修改改了对象：建模的操作一步因此过期，但这一轮的传播交给核对，不推整段重新提取
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单加了期望送达时间"))
    main_write(root, "incremental/applied.json", "{}\n")
    assert _next_command(client, project_id) == "/incremental-check"
    assert _gate(client, project_id)["blocking"]

    # 报告里还有 `[ ]`：还没做完
    main_write(root, "incremental/check.md", "# 传播核对\n\n- [ ] ontology/operation/下单.md\n")
    assert _next_command(client, project_id) == "/incremental-check"
    assert _gate(client, project_id)["blocking"]

    main_write(root, "incremental/check.md", "# 传播核对\n\n没有。\n")
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert nodes["incremental.check"]["state"] == "confirmed"
    gate = _gate(client, project_id)
    assert not gate["blocking"] and gate["status"] == "pass"

    # 核对之后本体又变了：过期，要重新核对才能发布
    main_write(root, "ontology/object/订单.md", obj("订单", "合成订单改过"))
    gate = _gate(client, project_id)
    assert gate["blocking"] and gate["status"] == "fail"

    # 停用：关卡和范围限定一起去掉
    assert client.post(f"/api/v1/projects/{project_id}/packs/incremental/disable").status_code == 200
    pack_service.prepare_for_message(manager, project_id)
    flow = _flow_file(root)
    assert "confine" not in flow and not [node for node in flow["nodes"] if node["id"].startswith("incremental.")]
    assert _gate(client, project_id) is None
    assert "incremental-scope" not in {path.stem for path in (root / ".claude" / "commands").glob("*.md")}


def test_imported_package_goes_straight_to_scoping(env, tmp_path):
    """导入知识包后建模已完成、资料分析算越过（ADR-0084）；启用增量构建后上传新资料，下一步就是圈定范围（ADR-0091）。"""
    manager, client = env
    source = manager.create_project("来源")["id"]
    _modeled(manager.project_root(source))
    archive = tmp_path / "package.zip"
    publish_service.export_package_zip(manager, source, archive, allow_unbound=True)
    files = {"file": ("package.zip", archive.read_bytes(), "application/zip")}
    upload_id = client.post("/api/v1/uploads", data={"purpose": "project_import"}, files=files).json()["upload_id"]
    created = client.post("/api/v1/projects/import", json={"upload_id": upload_id, "name": "导入"})
    assert created.status_code == 201, created.text
    project_id = created.json()["id"]
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "incremental"}).status_code == 201
    pack_service.prepare_for_message(manager, project_id)
    assert _next_command(client, project_id) == "/ontology-validate"
    manager.save_resource(project_id, "others", "新资料.md", "# 合成资料\n".encode("utf-8"), author="tester")
    built = view.build(manager.project_root(project_id))
    nodes = {node["id"]: node for node in built["nodes"]}
    assert nodes["core.research"]["flags"]["backfill"] is True
    assert nodes["incremental.scope"]["flags"]["backfill"] is False
    assert _next_command(client, project_id) == "/incremental-scope"


def test_autobuild_leaves_the_nodes_to_the_user(env):
    manager, _client = env
    project_id = manager.create_project("自动建模")["id"]
    root = manager.project_root(project_id)
    flow = assemble.assemble([_core(), _incremental(), ("trace", "0.1.0", _yaml("packs/trace/flow.yaml"))])
    policy = {"scope": {"until_node": None, "include_optional": False}}
    titles = {unit.get("node_id") or unit.get("node") or unit.get("id") for unit in plan._tail(root, flow, policy)}
    assert not {name for name in titles if str(name).startswith("incremental.")}
    assert "core.validate" in titles


# ---- confine 的校验 ----


def _with_confine(confine) -> tuple[str, str, dict]:
    pack, version, data = _incremental()
    return pack, version, {**data, "confine": confine}


@pytest.mark.parametrize("confine, message", [
    ("incremental/scope.json", "只能写 scope_file"),
    ({"scope_file": SCOPE_FILE, "other": 1}, "只能写 scope_file"),
    ({"scope_file": ""}, "具体文件"),
    ({"scope_file": "/etc/scope.json"}, "具体文件"),
    ({"scope_file": "incremental/../scope.json"}, "具体文件"),
    ({"scope_file": "incremental\\scope.json"}, "具体文件"),
    ({"scope_file": "incremental/*.json"}, "具体文件"),
    ({"scope_file": "ontology/object/scope.json"}, "具体文件"),
    ({"scope_file": ".onto/scope.json"}, "具体文件"),
    ({"scope_file": ".claude/scope.json"}, "具体文件"),
    ({"scope_file": "resource/scope.json"}, "具体文件"),
    ({"scope_file": "incremental/other.json"}, "produces"),
])
def test_assembly_refuses_a_bad_confine(confine, message):
    with pytest.raises(assemble.FlowAssemblyError, match=message):
        assemble.assemble([_core(), _with_confine(confine)])


def test_only_one_pack_may_declare_confine():
    second = {"confine": {"scope_file": "second/scope.json"}, "nodes": [{
        "id": "second.scope", "title": "合成范围", "kind": "agent", "placement": "step", "section": "required",
        "after": "core.model", "done": [], "produces": ["second/scope.json"], "entry": {"command": "/ontology"},
    }]}
    with pytest.raises(assemble.FlowAssemblyError, match="只能有一个包声明 confine"):
        assemble.assemble([_core(), _incremental(), ("second", "0.1.0", second)])
    # 一个包声明就没事
    assert assemble.assemble([_core(), ("second", "0.1.0", second)])["confine"]["pack"] == "second"


def _synthetic_pack(base: Path, confine) -> Path:
    flow = {"nodes": [{"id": "my-scope.scope", "title": "合成范围", "kind": "agent", "placement": "step",
                       "section": "required", "after": "core.model", "done": [],
                       "produces": ["my-scope/scope.json"], "entry": {"command": "/my-scope-run"}}]}
    if confine is not None:
        flow["confine"] = confine
    return make_pack(base, "my-scope", "0.1.0", files={
        "commands/my-scope-run.md": "运行 $ARGUMENTS\n", "flow.yaml": yaml.safe_dump(flow, allow_unicode=True),
    }, contributes={"flow": "flow.yaml", "nodes": ["my-scope.scope"]})


@pytest.mark.parametrize("confine, result", [
    ({"scope_file": "my-scope/scope.json"}, "pass"),
    ({"scope_file": "my-scope/other.json"}, "fail"),
    ({"scope_file": "ontology/scope.json"}, "fail"),
    ({"scope_file": ".onto/scope.json"}, "fail"),
    ({"scope_file": "../scope.json"}, "fail"),
    ({"scope_file": "my-scope/*.json"}, "fail"),
    ({"scope_file": "my-scope/scope.json", "extra": 1}, "fail"),
    ("my-scope/scope.json", "fail"),
])
def test_static_check_reads_confine(tmp_path, confine, result):
    _, items = checks.run(_synthetic_pack(tmp_path, confine), core_names={})
    assert next(item for item in items if item["id"] == "confine")["result"] == result


def test_static_check_has_no_confine_item_without_the_declaration(tmp_path):
    _, items = checks.run(_synthetic_pack(tmp_path, None), core_names={})
    assert "confine" not in {item["id"] for item in items}


# ---- 开发 skill 和用户上传 ----


def test_pack_tool_builds_it_and_user_upload_passes(real_flow_env, monkeypatch):  # noqa: F811
    client, _manager, tmp_path = real_flow_env
    # 内置包占着这个名字时上传会被拒（private_name）；这里查的是包本身能不能过
    monkeypatch.setattr(builtin, "PACKS_DIR", "packs-none")
    builtin.clear_cache()
    make_project(client)  # core 入库，依赖和 requires_flow 要比对它
    skill = skill_dir(client, tmp_path, "capability")
    assert "confine" in (skill / "references" / "protocol.md").read_text(encoding="utf-8")
    work = tmp_path / "work"
    source = work / "incremental"
    shutil.copytree(PACK, source, ignore=shutil.ignore_patterns("__pycache__"))
    built = pack_tool(skill, "build", source, "--json", cwd=work)
    assert built.returncode == 0, built.stdout + built.stderr
    report = json.loads(built.stdout)
    assert next(item for item in report["checks"] if item["id"] == "confine")["result"] == "pass"

    accepted = post_zip(client, MINE, Path(report["zip"]).read_bytes(), AS_OWNER)
    assert accepted.status_code == 202, accepted.text
    detail = client.get(f"{MINE}/{accepted.json()['id']}", headers=AS_OWNER).json()
    assert detail["state"] == "passed", [item for item in detail["checks"] if item["result"] == "fail"]


def test_local_check_reports_a_bad_confine(real_flow_env, tmp_path):  # noqa: F811
    client, _manager, _tmp = real_flow_env
    make_project(client)
    skill = skill_dir(client, tmp_path, "capability")
    source = tmp_path / "bad" / "my-scope-0.1.0"
    _synthetic_pack(tmp_path / "bad", {"scope_file": "ontology/scope.json"})
    done = pack_tool(skill, "check", source, "--json", cwd=tmp_path)
    assert done.returncode != 0
    assert next(item for item in json.loads(done.stdout)["checks"] if item["id"] == "confine")["result"] == "fail"
