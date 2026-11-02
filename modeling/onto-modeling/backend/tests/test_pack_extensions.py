"""5.6 扩展项里 B5c 补齐的几类（详设 5.6、6.4；ADR-0068）。

- 界面入口：快捷项、附加操作进 PL-12，命令要是本包的；数据源连接向导随证据源留空，不出现；
- 声明式门禁：on 含 commit 的在写入事务提交（含合入）时查，on 含 publish 的进发布门禁链；
- 视图选择：内核内置的视图里选，依赖证据源的本期不出现；
- 配置项进配置注册表，指标说明进 /metrics，告警规则进 O-08（包声明的只能停用）；
- 只读 MCP 工具：按本包的产物类型列表、取一项；
- 节点钩子：挂在已有节点前后，进节点动作与两轮之间的提示，不新增节点。

用合成的检查清单包（tests/test_extensions_artifacts 的产物登记）与合成节点包。
"""

from __future__ import annotations

import asyncio
import copy
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.governance import config_registry, telemetry
from app.kernel import event_port
from app.kernel.writepath import ChangeTx, Origin
from app.main import app
from app.ops_app import ops_app
from app.routers import onto_mcp
from app.services import auth
from app.services.flow import model as flow_model
from app.services.flow import view
from app.services.packs import builtin, checks, extensions, registry
from app.services.packs import service as pack_service
from app.services.workspace import WorkspaceManager
from app.services.workspace.errors import WorkspaceError
from tests.pack_helpers import NODE_PACK_FLOW, make_assets, make_node_pack, make_pack
from tests.test_extensions_artifacts import CHECKLIST_CONTRIBUTES, _checklist_files

TOKENS = {"ops-token": {"sub": "operator_ops", "role": "ops"}}
OPS = {"Authorization": "Bearer ops-token"}

EXTRA = {
    "entries": [
        {"id": "quick-run", "kind": "quick_action", "label": "跑一遍检查", "command": "/checklist-run", "where": "wb-l2"},
        {"id": "extra-run", "kind": "extra_action", "label": "重跑", "command": "/checklist-run", "where": "scenarios"},
        {"id": "connect", "kind": "source_wizard", "label": "连接业务系统"},
    ],
    "gates": [
        *CHECKLIST_CONTRIBUTES["gates"],
        {"id": "level-set", "label": "检查项要有级别", "artifact": "checklist.item", "on": ["commit"],
         "rules": [{"field": "level", "required": True}]},
        {"id": "enough", "label": "检查项通过率", "artifact": "checklist.item", "on": ["publish"],
         "min_count": 1, "ratio": {"field": "result", "in": ["pass"], "at_least": 0.5}},
    ],
    "view_choices": ["er_view", "evidence_sidebar"],
    "config": [{"key": "max_items", "label": "每次最多检查几项", "type": "int", "default": 6, "min": 1, "max": 50}],
    "metrics": [{"name": "items_checked", "help": "检查过的检查项数", "type": "counter"}],
    "alerts": [{"id": "fail-rate", "name": "检查失败率", "condition": "checklist.fail_rate > 20%", "window": "1h",
                "level": "warning", "runbook": "合成处置手册"}],
    "mcp_tools": [{"name": "items", "title": "列出检查项", "artifact": "checklist.item", "mode": "list"},
                  {"name": "item", "title": "读取检查项", "artifact": "checklist.item", "mode": "get"}],
}


def item_md(title: str, level: str | None, result: str = "pass") -> str:
    front = {"title": title, **({"level": level} if level else {}), "result": result}
    return "---\n" + yaml.safe_dump(front, allow_unicode=True) + "---\n\n## 说明\n\n合成\n"


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    event_port.clear()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "扩展项", "goal": "合成场景：图书借阅建模"}).json()["id"]
    contributes = {**CHECKLIST_CONTRIBUTES, **EXTRA}
    source = make_pack(tmp_path / "src", "checklist", "1.0.0", files=_checklist_files(), contributes=contributes)
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("checklist", "stable", "1.0.0", by="admin")
    pack_service.enable(manager, project_id, "checklist")
    pack_service.prepare_for_message(manager, project_id)
    yield client, manager, project_id, tmp_path
    builtin.clear_cache()


def test_static_check_of_declarations(tmp_path, env):
    _client, manager, _pid, _tmp = env
    good = make_pack(tmp_path / "good", "checklist", "1.0.1", files=_checklist_files(),
                     contributes={**CHECKLIST_CONTRIBUTES, **EXTRA})
    _manifest, items = checks.run(good, None)
    assert {item["id"]: item["result"] for item in items}["extensions"] == "pass"
    bad = copy.deepcopy(EXTRA)
    bad["entries"][0]["command"] = "/other-run"
    bad["gates"][1]["artifact"] = "other.item"
    bad["view_choices"] = ["kanban"]
    bad["config"][0]["key"] = "Max Items"
    bad["metrics"][0]["name"] = "items-checked"
    bad["alerts"][0].pop("condition")
    bad["mcp_tools"][0]["artifact"] = "other.item"
    bad["mcp_tools"][1]["mode"] = "write"
    broken = make_pack(tmp_path / "bad", "checklist", "1.0.2", files=_checklist_files(),
                       contributes={**CHECKLIST_CONTRIBUTES, **bad})
    _manifest, items = checks.run(broken, None)
    result = {item["id"]: item for item in items}["extensions"]
    assert result["result"] == "fail"
    for word in ("/other-run", "门禁 level-set", "kanban", "Max Items", "items-checked", "condition", "MCP 工具 items", "只读"):
        assert word in result["detail"], word


def test_entries_view_choices_config_in_extensions(env):
    client, _manager, project_id, _tmp = env
    ext = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    assert [(item["id"], item["kind"]) for item in ext["entries"]] == [("quick-run", "quick_action"), ("extra-run", "extra_action")]
    assert ext["entries"][0]["command"] == "/checklist-run"
    assert ext["view_choices"] == [{"id": "er_view", "pack": "checklist"}]  # 证据侧栏随证据源留空
    assert {"key": "checklist.max_items", "pack": "checklist"} in ext["config"]
    gates = {gate["id"]: gate["on"] for gate in ext["gates"]}
    assert gates == {"title-short": ["save"], "level-set": ["commit"], "enough": ["publish"]}


def test_config_items_land_in_the_registry(env):
    _client, manager, _pid, _tmp = env
    extensions.sync_config(manager)
    item = next(row for row in config_registry.items() if row["key"] == "checklist.max_items")
    assert item["source"] == "pack" and item["pack"] == "checklist" and item["value"] == "6"
    assert item["effect"] == "next_message"


def test_commit_gate_blocks_writes_but_not_reverts(env):
    _client, manager, project_id, _tmp = env
    rel = "ext/checklist/items/借阅检查.md"
    with pytest.raises(WorkspaceError) as exc:
        with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
            tx.write(rel, item_md("借阅检查", None))
            tx.commit("web: 保存检查项")
    assert exc.value.code == "gate_failed" and exc.value.status == 422
    assert exc.value.details["findings"][0]["gate"] == "level-set"
    root = manager.project_root(project_id)
    assert not (root / rel).exists()  # 门禁不过，写入回滚
    with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
        tx.write(rel, item_md("借阅检查", "high"))
        assert tx.commit("web: 保存检查项").commit
    with ChangeTx(manager, project_id, Origin("revert", "local")) as tx:
        tx.write(rel, item_md("借阅检查", None))
        assert tx.commit("revert: 回到以前").commit


def test_publish_gates_join_the_chain(env):
    client, manager, project_id, _tmp = env
    root = manager.project_root(project_id)

    def check() -> dict:
        body = client.get(f"/api/v1/projects/{project_id}/publish/check").json()
        return next(gate for gate in body["gates"] if gate["id"] == "checklist.enough")

    gate = check()
    assert gate["status"] == "fail" and gate["blocking"] and gate["source"]["kind"] == "pack"
    assert "至少要 1 个" in gate["summary"]
    for name, result in (("甲", "pass"), ("乙", "fail"), ("丙", "fail")):
        with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
            tx.write(f"ext/checklist/items/{name}.md", item_md(name, "low", result))
            tx.commit(f"web: {name}")
    gate = check()
    assert gate["status"] == "fail" and "1/3" in gate["findings"][0]["message"]
    with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
        tx.write("ext/checklist/items/乙.md", item_md("乙", "low", "pass"))
        tx.commit("web: 乙")
    assert check()["status"] == "pass"
    assert (root / "ext" / "checklist" / "items" / "乙.md").is_file()


def test_metrics_and_alert_rules(env, monkeypatch):
    _client, manager, _pid, _tmp = env
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (TOKENS.get(token), "" if token in TOKENS else "invalid"))
    ops = TestClient(ops_app)
    assert "# HELP onto_pack_checklist__items_checked 检查过的检查项数" in telemetry.prometheus_text()
    rules = ops.get("/api/v1/ops/alerts/rules", headers=OPS).json()
    pack_rule = next(rule for rule in rules["items"] if rule["id"] == "checklist.fail-rate")
    assert pack_rule["source"] == "checklist@1.0.0" and pack_rule["enabled"] and pack_rule["state"] == "ok"
    assert rules["firing"] == 0
    assert ops.put("/api/v1/ops/alerts/rules/checklist.fail-rate", headers=OPS, json={"name": "改名"}).status_code == 403
    assert ops.delete("/api/v1/ops/alerts/rules/checklist.fail-rate", headers=OPS).status_code == 403
    off = ops.put("/api/v1/ops/alerts/rules/checklist.fail-rate", headers=OPS, json={"enabled": False})
    assert off.status_code == 200 and off.json()["enabled"] is False
    created = ops.post("/api/v1/ops/alerts/rules", headers=OPS,
                       json={"name": "错误率", "condition": "error_rate > 5%", "window": "5m", "level": "critical",
                             "runbook_md": "合成"})
    assert created.status_code == 201 and created.json()["source"] == "kernel"
    rule_id = created.json()["id"]
    renamed = ops.put(f"/api/v1/ops/alerts/rules/{rule_id}", headers=OPS, json={"name": "错误率太高"})
    assert renamed.json()["name"] == "错误率太高" and renamed.json()["level"] == "critical"
    assert ops.delete(f"/api/v1/ops/alerts/rules/{rule_id}", headers=OPS).status_code == 204
    assert ops.post("/api/v1/ops/alerts/rules", headers=OPS, json={"name": "x", "condition": "y", "level": "loud"}).status_code == 400


def test_pack_mcp_tools_are_read_only_views_of_own_artifacts(env):
    _client, manager, project_id, _tmp = env
    with ChangeTx(manager, project_id, Origin("web", "local")) as tx:
        tx.write("ext/checklist/items/借阅检查.md", item_md("借阅检查", "high"))
        tx.commit("web: 保存检查项")
    principal = {"project_id": project_id, "scopes": ["ontology.read"], "username": "local"}
    listed = asyncio.run(onto_mcp.dispatch({"method": "tools/list"}, "read", principal, "2025-06-18"))["tools"]
    names = {tool["name"]: tool for tool in listed}
    assert {"checklist__items", "checklist__item"} <= set(names) and names["checklist__items"]["annotations"]["readOnlyHint"]
    change = asyncio.run(onto_mcp.dispatch({"method": "tools/list"}, "change", {**principal, "scopes": ["ontology.change.apply"]},
                                           "2025-06-18"))["tools"]
    assert "checklist__items" not in {tool["name"] for tool in change}
    called = asyncio.run(onto_mcp.dispatch({"method": "tools/call", "params": {"name": "checklist__items", "arguments": {}}},
                                           "read", principal, "2025-06-18"))
    assert not called["isError"] and [row["key"] for row in called["structuredContent"]["items"]] == ["借阅检查"]
    got = asyncio.run(onto_mcp.dispatch({"method": "tools/call", "params": {"name": "checklist__item",
                                                                            "arguments": {"key": "借阅检查"}}},
                                        "read", principal, "2025-06-18"))
    assert not got["isError"] and got["structuredContent"]["artifact"]["key"] == "借阅检查"


# ---- 节点钩子（用仓库里真实的 core 组装流程）----


@pytest.fixture()
def real(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    project_id = manager.create_project("钩子", owner="employee_0001")["id"]
    yield manager, project_id, tmp_path
    builtin.clear_cache()


def hook_flow(**hook) -> dict:
    flow = copy.deepcopy(NODE_PACK_FLOW)
    flow["hooks"] = [{"id": "naming.normalize", "title": "术语归一", "node": "core.object", "when": "after",
                      "command": "/naming-check 归一", **hook}]
    return flow


def test_node_hook_extends_a_node_without_adding_one(real):
    manager, project_id, tmp_path = real
    root = manager.project_root(project_id)
    source = make_node_pack(tmp_path / "src", flow=hook_flow())
    _manifest, items = checks.run(source, manager.packs.repository)
    assert all(item["result"] != "fail" for item in items), items
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("naming", "stable", "0.1.0", by="admin")
    pack_service.enable(manager, project_id, "naming")
    pack_service.prepare_for_message(manager, project_id)

    flow = flow_model.load_for(root)
    assert [hook["id"] for hook in flow["hooks"]] == ["naming.normalize"]
    built = {node["id"]: node for node in view.build(root)["nodes"]}
    assert "naming.normalize" not in built
    host = built["core.object"]
    assert host["hooks"] == [{"id": "naming.normalize", "title": "术语归一", "when": "after",
                              "command": "/naming-check 归一", "pack": "naming"}]
    assert host["actions"][-1] == {"kind": "start", "label": "术语归一", "target": "/naming-check 归一",
                                   "hook": "naming.normalize"}
    hints = registry.prompt_hints(manager, root, "/ontology-extract-object 从资料里提取")
    assert any("术语归一" in hint and "/naming-check 归一" in hint for hint in hints)
    assert not registry.prompt_hints(manager, root, "/ontology-extract-operation")


@pytest.mark.parametrize("hook,word", [({"node": "core.nothing"}, "不存在"), ({"command": "/other-run"}, "不在本次装配里"),
                                       ({"id": "other.normalize"}, "编号")])
def test_node_hook_assembly_checks(real, hook, word):
    manager, project_id, tmp_path = real
    source = make_node_pack(tmp_path / "src", flow=hook_flow(**hook))
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("naming", "stable", "0.1.0", by="admin")
    with pytest.raises(pack_service.PackError) as exc:
        pack_service.enable(manager, project_id, "naming")
    assert exc.value.code == "flow_assembly_failed" and word in str(exc.value)
