"""I1：界面文案的后端部分（UI 文案口径 ③ ⑤、证据源本期不下发）与前端新认的字段。

说明槽位不再下发；证据源相关的数据不下发；项目内显示的文字不用「合入」「主线」「提交」「L4」；
FL-01 脚本节点带最近一次结果（刷新后仍在）。
"""

from __future__ import annotations

from app.services.flow import view
from tests import proc
from tests.test_node_pack_lifecycle import (  # noqa: F401  （夹具按名字注入）
    LINT_SCRIPT, SCRIPT_NODE, enable_and_apply, env, fake_sandbox, install, kinds_flow)
from tests.test_changesets import main_write, obj, project  # noqa: F401
from tests.test_release_publish import manager  # noqa: F401


def _script_pack(manager, tmp_path, *, fail: bool = False) -> None:
    node = {**SCRIPT_NODE, "script": {**SCRIPT_NODE["script"], "args": ["--fail"]}} if fail else SCRIPT_NODE
    install(manager, tmp_path, flow=kinds_flow(node, probes={"naming_lint": {"files": ["reports"], "glob": "lint-naming.md"}}),
            files={"commands/naming-check.md": "检查 $ARGUMENTS\n", "scripts/naming_lint.py": LINT_SCRIPT})


def test_script_node_last_run_survives_refresh(env, fake_sandbox):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    main_write(root, "ontology/object/读者.md", obj("读者", "合成读者"))
    _script_pack(manager, tmp_path)
    enable_and_apply(client, manager, project_id)
    nodes = {node["id"]: node for node in view.build(root)["nodes"]}
    assert nodes["naming.lint"]["last_run"] is None  # 还没跑过
    assert "last_run" not in nodes["core.model"]  # 只有脚本节点带
    assert all("note" not in node for node in nodes.values())  # 节点说明不再下发

    started = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert started.status_code == 200, started.text
    flow = client.get(f"/api/v1/projects/{project_id}/flow").json()
    run = next(node for node in flow["nodes"] if node["id"] == "naming.lint")["last_run"]
    assert run["state"] == "passed" and run["summary"] == "# 命名格式检查" and run["at"].endswith("Z")
    assert (root / ".onto" / "run" / "script-runs.json").is_file()
    assert "script-runs.json" not in subprocess_ls(root)  # 不进版本


def test_script_node_failure_is_recorded_but_unavailable_sandbox_is_not(env, fake_sandbox, monkeypatch):
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    _script_pack(manager, tmp_path, fail=True)
    enable_and_apply(client, manager, project_id)
    failed = client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={})
    assert failed.status_code == 422
    run = next(node for node in view.build(root)["nodes"] if node["id"] == "naming.lint")["last_run"]
    assert run["state"] == "failed" and run["summary"] == "退出码 3"
    monkeypatch.setattr(fake_sandbox[0], "gateway_url", lambda: "")
    assert client.post(f"/api/v1/projects/{project_id}/flow/nodes/naming.lint/start", json={}).status_code == 503
    again = next(node for node in view.build(root)["nodes"] if node["id"] == "naming.lint")["last_run"]
    assert again == run  # 沙箱不可用时没跑起来，不覆盖上一次的结果


def subprocess_ls(root) -> str:

    return proc.run(["git", "ls-files"], cwd=root, capture_output=True, text=True, check=True).stdout


# ---- 知识：加载规则说明、冲突说明不下发，冲突的另一方给数据 ----


def test_knowledge_conflicts_carry_data_not_sentences(tmp_path):
    import json

    from app.services import knowledge_layers

    root = tmp_path
    (root / "ontology" / "object").mkdir(parents=True)
    entry = {"id": "domain-rule-a", "title": "陈列上限", "_slug": "a", "_rule": "合成口径一", "source": {"kind": "manual"},
             "scope": {"entries": [{"kind": "object", "key": "不存在的对象"}]}}
    twin = {**entry, "id": "domain-rule-b", "_slug": "b", "scope": {}}
    industry = [{"id": "industry-retail:a", "title": "陈列上限（行业）", "_slug": "a", "_rule": "合成口径二",
                 "_pack_label": "零售连锁"}]
    result = knowledge_layers.check_conflicts(root, [entry, twin], industry)
    by_kind = {item["kind"]: item for item in result["conflicts"]}
    assert all("note" not in item for item in result["conflicts"])
    assert by_kind["duplicate"]["duplicate_of"] == {"id": "domain-rule-a", "title": "陈列上限"}
    assert by_kind["broken_ref"]["missing"] == ["不存在的对象"]
    assert [knowledge_layers.conflict_with(by_kind[k]) for k in ("scope_conflict", "duplicate", "broken_ref")] == [
        "陈列上限（行业）", "陈列上限", "不存在的对象"]
    # 旧版存下的结果带 note：读出来时去掉
    old = {**result, "conflicts": [{**item, "note": "旧说明"} for item in result["conflicts"]]}
    knowledge_layers.save_conflicts(root, old)
    assert all("note" not in item for item in knowledge_layers.last_conflicts(root)["conflicts"])
    assert json.loads((root / knowledge_layers.CONFLICTS_REL).read_text(encoding="utf-8"))


# ---- 运维可观测：合入卡有计数来源、证据采集卡不返回、没有数据的列不返回 ----


def test_merge_counts_for_ops_merges_card(project):
    from app.governance import telemetry
    from app.kernel.changesets import service
    from tests.test_changesets import _pending

    workspaces, project_id, root = project
    telemetry.reset_metrics()
    _copy, record = _pending(workspaces, project_id, root)
    service.merge(workspaces, project_id, record["id"], actor="employee_0001")
    assert 'onto_merges_total{source="agent"} 1' in telemetry.prometheus_text()


def test_attribution_omits_columns_without_a_source(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from app.governance import telemetry

    from app.ops_app import ops_app
    from app.services import auth

    monkeypatch.setattr(auth, "ENABLED", False)
    telemetry.reset_metrics()
    telemetry.observe("onto_turn_seconds", 2.0, pack="naming@0.1.0", node="naming.lint")
    rows = TestClient(ops_app).get("/api/v1/ops/attribution", params={"by": "pack"}).json()["items"]
    assert rows and rows[0]["key"] == "naming@0.1.0"
    assert "reruns" not in rows[0] and "first_accept_pct" not in rows[0]


# ---- 插件目录、发布 ----


def test_release_manifest_gives_source_version_label(manager, monkeypatch, hub_login):
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth
    from tests.test_release_publish import rich_project

    monkeypatch.setattr(deps, "workspaces", manager)
    pid, _root = rich_project(manager)
    manager._update_meta(pid, owner="operator_a")
    client = TestClient(app, headers=hub_login)
    record = client.post(f"/api/v1/projects/{pid}/releases", json={}).json()
    body = client.get(f"/api/v1/projects/{pid}/releases/{record['version']}/manifest").json()
    assert body["source_version_label"] == record["version_label"] and body["source_commit"] == record["commit"]


def test_pack_catalog_has_no_meta_line_and_data_access_only_converters(env):
    """多源连接器分类里只有转换器（F4a，ADR-0073）；数据库采集这类证据源仍不出现（ADR-0056，见下一条）。"""
    client, _manager, project_id, _tmp = env
    body = client.get("/api/v1/packs", params={"project_id": project_id}).json()
    assert body["items"] and all("meta" not in item for item in body["items"])
    providers = [item for item in body["items"] if item["category"] == "provider"]
    assert providers and all(item["converter"] for item in providers)
    assert body["counts"]["provider"] == len(providers)


def test_editor_reference_carries_pack_label(env):
    client, _manager, project_id, _tmp = env
    types = client.get(f"/api/v1/projects/{project_id}/artifact-types").json()["items"]
    flowchart = next(item for item in types if item["type"] == "diagram.flowchart")
    editor = flowchart["view"]["detail"]["editor"]
    assert editor["pack"] == "diagram" and editor["pack_label"] == "图示"


# ---- 证据源本期不下发 ----


def test_external_nodes_are_not_listed_in_flow(env):
    from tests.test_node_pack_lifecycle import EXTERNAL_NODE

    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    # 没写提供方的外部节点能装进流程（启用检查只认声明了的提供方），FL-01 照样不列
    install(manager, tmp_path, flow=kinds_flow({key: value for key, value in EXTERNAL_NODE.items() if key != "provider"}))
    enable_and_apply(client, manager, project_id)
    from app.services.flow import model

    assert "naming.fetch" in {node["id"] for node in model.load_for(root)["nodes"]}  # 确实装进了流程
    flow = view.build(root)
    assert "naming.fetch" not in {node["id"] for node in flow["nodes"]}
    assert all("naming.fetch" not in (edge["from"], edge["to"]) for edge in flow["edges"])
    assert all("naming.fetch" not in section["nodes"] for section in flow["sections"])


def test_provider_packs_are_not_listed_anywhere(env, monkeypatch):
    from fastapi.testclient import TestClient

    from app.ops_app import ops_app
    from app.services import auth
    from tests.pack_helpers import make_pack

    client, manager, project_id, tmp_path = env
    source = make_pack(tmp_path / "src", "db-generic", "0.1.0", kind="provider")
    manager.packs.repository.install(source, origin="upload", by="admin")
    manager.packs.repository.set_channel("db-generic", "stable", "0.1.0", by="admin")
    assert manager.packs.repository.manifest("db-generic", "0.1.0").category == "provider"
    assert "db-generic" not in {item["name"] for item in client.get("/api/v1/packs").json()["items"]}
    monkeypatch.setattr(auth, "ENABLED", False)
    ops = TestClient(ops_app)
    assert "db-generic" not in {item.get("name") for item in ops.get("/api/v1/ops/packs").json()["items"]}
    assert "db-generic" not in {item["component"] for item in ops.get("/api/v1/ops/versions").json()["items"]}


# ---- 口径 ③：项目内显示的错误提示、通知、待处理标签不用「合入」「主线」 ----

PROJECT_FACING = ("app/routers/changesets.py", "app/routers/flow.py", "app/routers/studio.py", "app/routers/versions.py",
                  "app/kernel/writepath/tx.py", "app/kernel/changesets/service.py", "app/services/flow/inbox.py",
                  "app/services/publish/service.py", "app/services/publish/tree.py")
ERRORS = {"ApiError", "WorkspaceError", "ArtifactError", "after_close"}


def _messages(path):
    import ast
    from pathlib import Path

    tree = ast.parse(Path(path).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and getattr(node.func, "id", getattr(node.func, "attr", "")) in ERRORS:
            for arg in node.args:
                for part in ast.walk(arg):
                    if isinstance(part, ast.Constant) and isinstance(part.value, str):
                        yield part.value


def test_project_facing_texts_avoid_merge_and_mainline():
    from app.services.flow import inbox

    words = ("合入", "主线")
    found = [(path, text) for path in PROJECT_FACING for text in _messages(path) if any(w in text for w in words)]
    assert found == []
    assert not [label for label in inbox.KIND_LABELS.values() if any(w in label for w in words)]
