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


# ---- 1. 流程图标准保存不丢模型以外的内容 ----

DRAWIO = """<mxfile host="drawio" compressed="false">
  <diagram id="p1" name="主流程">
    <mxGraphModel><root>
      <mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="lane" value="门店" style="swimlane;" vertex="1" parent="1"><mxGeometry x="100" y="50" width="400" height="300" as="geometry"/></mxCell>
      <mxCell id="s" value="开始" style="ellipse;" vertex="1" parent="lane"><mxGeometry x="20" y="40" width="80" height="40" as="geometry"/></mxCell>
      <mxCell id="a" value="编制补货单" style="rounded=1;fillColor=#ffe6cc;" vertex="1" parent="lane"><mxGeometry x="20" y="120" width="120" height="60" as="geometry"/></mxCell>
      <mxCell id="note" value="专家批注：这里要区分促销期" style="text;" vertex="1" parent="1"><mxGeometry x="550" y="20" width="200" height="40" as="geometry"/></mxCell>
      <mxCell id="e1" style="endArrow=classic;" edge="1" parent="lane" source="s" target="a"><mxGeometry relative="1" as="geometry"/></mxCell>
    </root></mxGraphModel>
  </diagram>
  <diagram id="p2" name="异常处理">
    <mxGraphModel><root><mxCell id="0"/><mxCell id="1" parent="0"/>
      <mxCell id="x" value="第二页的异常分支" style="rounded=1;" vertex="1" parent="1"><mxGeometry x="0" y="0" width="120" height="60" as="geometry"/></mxCell>
    </root></mxGraphModel>
  </diagram>
</mxfile>
"""


def test_flowchart_standard_save_keeps_lanes_notes_and_other_pages(env):
    manager, client = env
    project_id = manager.create_project("流程图")["id"]
    root = manager.project_root(project_id)
    _write(root, "ontology/diagrams/补货.drawio", DRAWIO)
    _commit_all(root)
    base = f"/api/v1/projects/{project_id}/artifacts/diagram.flowchart/补货"
    detail = client.get(base).json()
    model = json.loads(json.dumps(detail["content"]))
    step = next(node for node in model["nodes"] if node["id"] == "a")
    assert (step["x"], step["y"]) == (180, 200)  # 泳道里的节点按父级累加成绝对坐标
    step["label"] = "编制并提交补货单"
    step["x"] += 40
    model["nodes"].append({"id": "n-end", "type": "end", "label": "结束", "description": "", "x": 300, "y": 420,
                           "refs": [], "pending": False, "pending_note": None})
    model["edges"].append({"id": "e2", "type": "seq", "from": "a", "to": "n-end", "label": "", "condition": ""})
    saved = client.put(base, json={"base_revision": detail["revision"], "content": model})
    assert saved.status_code == 200, saved.text

    text = (root / "ontology/diagrams/补货.drawio").read_text(encoding="utf-8")
    pages = ET.fromstring(text).findall("diagram")
    assert [page.get("name") for page in pages] == ["主流程", "异常处理"]
    assert "第二页的异常分支" in text and "专家批注：这里要区分促销期" in text
    cells = {el.get("id"): el for el in pages[0].find("mxGraphModel/root")}
    assert "swimlane" in cells["lane"].get("style")
    # 改过的节点留在泳道里、样式不动，坐标换算回相对泳道
    moved = cells["a"].find("mxCell")
    assert cells["a"].get("label") == "编制并提交补货单" and moved.get("parent") == "lane"
    assert moved.get("style") == "rounded=1;fillColor=#ffe6cc;"
    assert (moved.find("mxGeometry").get("x"), moved.find("mxGeometry").get("y")) == ("60", "120")
    # 没改的节点不动；新节点、新连线追加
    start_geometry = cells["s"].find("mxCell/mxGeometry")
    assert (start_geometry.get("x"), start_geometry.get("y"), start_geometry.get("width")) == ("20", "40", "80")
    assert cells["n-end"].get("onto_type") == "end" and cells["e2"].find("mxCell").get("target") == "n-end"
    back = client.get(base).json()["content"]
    assert {node["id"] for node in back["nodes"]} == {"s", "a", "n-end"}
    assert next(node for node in back["nodes"] if node["id"] == "a")["x"] == 220
    assert back["process"]["roles"] == ["门店"]


def test_drawio_source_merges_unsaved_model_into_file(env):
    """AR-10（develop !547）：有未保存修改时切到高级编辑，交给编辑器的是合进原文的 XML——尺寸、样式、泳道、批注、其他页都在；不落盘。"""
    manager, client = env
    project_id = manager.create_project("流程图")["id"]
    root = manager.project_root(project_id)
    _write(root, "ontology/diagrams/补货.drawio", DRAWIO)
    _commit_all(root)
    base = f"/api/v1/projects/{project_id}/artifacts/diagram.flowchart/补货"
    detail = client.get(base).json()

    plain = client.post(f"{base}/drawio", json={})
    assert plain.status_code == 200, plain.text
    assert plain.json() == {"xml": DRAWIO, "revision": detail["revision"]}

    model = json.loads(json.dumps(detail["content"]))
    step = next(node for node in model["nodes"] if node["id"] == "a")
    step["label"] = "编制并提交补货单"
    step["description"] = "第一行\n第二行"
    model["edges"][0]["condition"] = "收到申请后"
    merged = client.post(f"{base}/drawio", json={"content": model})
    assert merged.status_code == 200, merged.text
    xml = merged.json()["xml"]
    pages = ET.fromstring(xml).findall("diagram")
    assert [page.get("name") for page in pages] == ["主流程", "异常处理"]
    assert "第二页的异常分支" in xml and "专家批注：这里要区分促销期" in xml
    cells = {el.get("id"): el for el in pages[0].find("mxGraphModel/root")}
    assert "swimlane" in cells["lane"].get("style")
    cell = cells["a"].find("mxCell")
    assert (cells["a"].get("label"), cells["a"].get("description")) == ("编制并提交补货单", "第一行\n第二行")
    assert cell.get("style") == "rounded=1;fillColor=#ffe6cc;" and cell.find("mxGeometry").get("width") == "120"
    assert cells["e1"].get("condition") == "收到申请后" and cells["e1"].find("mxCell").get("style") == "endArrow=classic;"
    # 不落盘
    assert (root / "ontology/diagrams/补货.drawio").read_text(encoding="utf-8") == DRAWIO
    # 交回来的就是高级编辑保存时的 XML：解析出来和提交的模型一致
    from app.kernel.artifacts import flowchart

    again = flowchart.from_drawio_edit(xml, detail["content"])
    assert next(node for node in again["nodes"] if node["id"] == "a")["description"] == "第一行\n第二行"
    assert next(edge for edge in again["edges"] if edge["id"] == "e1")["condition"] == "收到申请后"

    bad = client.post(f"{base}/drawio", json={"content": {"nodes": [{"id": "0"}]}})
    assert bad.status_code == 422 and bad.json()["code"] == "artifact_schema_invalid"


def test_flowchart_save_drops_cells_hanging_on_removed_nodes():
    from app.kernel.artifacts import flowchart

    model = flowchart.parse(DRAWIO)
    model["nodes"] = [node for node in model["nodes"] if node["id"] != "s"]
    model["edges"] = []
    text = flowchart.render(model, previous=DRAWIO)
    cells = {el.get("id") for el in ET.fromstring(text).find("diagram/mxGraphModel/root")}
    assert "s" not in cells and "e1" not in cells  # 删掉的节点和连在它上面的连线一起删
    assert {"lane", "note", "a"} <= cells


# ---- 2. 场景保存不删节内的自由内容 ----

SCENARIO = ("# 测试场景：补货编制主流程\n\n## 场景 1：正常补货\n\n- **输入**：\n  - `qty` = `12`\n"
            "- **预期路径**：正常完成\n- **数据来源**：自定义\n\n备注：这一段是专家手写的说明，不是列表项。\n\n"
            "| 参数 | 取值 |\n|---|---|\n| qty | 12 |\n\n### 补充\n\n人工补充的细节\n\n"
            "## 场景 2：缺货补货\n\n- **输入**：\n  - `qty` = `0`\n- **预期路径**：异常终止于步骤2\n- **数据来源**：自定义\n")


def test_scenario_save_keeps_free_text_tables_and_subsections(env):
    manager, client = env
    project_id = manager.create_project("场景")["id"]
    root = manager.project_root(project_id)
    _write(root, SCENARIO_REL, SCENARIO)
    _commit_all(root)
    base = f"/api/v1/projects/{project_id}/artifacts/trace.scenario"
    row = next(item for item in client.get(base).json()["items"] if item["key"] == "补货编制主流程--1")
    saved = client.put(f"{base}/补货编制主流程--1", json={
        "base_revision": row["revision"], "fields": {"title": "正常补货（改名）", "expected": "走审批分支"}})
    assert saved.status_code == 200, saved.text
    text = (root / SCENARIO_REL).read_text(encoding="utf-8")
    assert "## 场景 1：正常补货（改名）" in text and "- **预期路径**：走审批分支" in text
    assert "备注：这一段是专家手写的说明，不是列表项。" in text
    assert "| qty | 12 |" in text and "### 补充\n\n人工补充的细节" in text
    assert "- **输入**：\n  - `qty` = `12`" in text
    assert text.endswith("- **数据来源**：自定义\n") and "异常终止于步骤2" in text  # 别的场景不动


# ---- 3. 网页新建的场景符合模板 ----


def test_new_scenario_file_follows_testcase_template(env):
    manager, client = env
    project_id = manager.create_project("新场景")["id"]
    root = manager.project_root(project_id)
    created = client.post(f"/api/v1/projects/{project_id}/artifacts/trace.scenario", json={
        "key": "补货编制主流程",
        "fields": {"title": "缺货补货", "source_label": "自定义", "expected": "正常完成"},
        "sections": {"输入": "- `qty` = `3`"}})
    assert created.status_code == 201, created.text
    text = (root / SCENARIO_REL).read_text(encoding="utf-8")
    assert text.startswith("# 测试场景：补货编制主流程\n")
    assert "## 场景 1：缺货补货\n\n- **输入**：\n  - `qty` = `3`\n- **预期路径**：正常完成\n- **数据来源**：自定义\n" in text
    # 场景模拟包自己的解析脚本认得它
    script = root / ".claude" / "scripts" / "trace_inputs.py"
    import importlib.util

    spec = importlib.util.spec_from_file_location("trace_inputs_under_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    parsed = module.parse_source(root / SCENARIO_REL)
    assert parsed["operation"] == "补货编制主流程"
    assert parsed["scenarios"] == [{"id": "1", "name": "缺货补货", "inputs": {"qty": 3}, "input_notes": {},
                                    "expected_path": "正常完成", "data_source": "自定义"}]


# ---- 4、5、12. 配额：删项目前先入账；所有发起一轮的入口都查；并发上限的检查和占位在同一把锁里 ----


@pytest.fixture()
def quotas(env, monkeypatch):
    from app.governance import quota
    from app.services import agent_sessions
    from app.services.agent_sessions import SessionManager
    from tests.test_session_host import FakeEngine

    monkeypatch.setattr(quota, "_service", None)
    service = quota.service()
    service.sync_interval = 0
    pool = SessionManager()
    monkeypatch.setattr(pool, "_engine_class_for", lambda project_id: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", pool)
    FakeEngine.scripts, FakeEngine.sent = [], []
    return service, pool


def test_deleting_project_books_unsynced_usage_first(env, quotas):
    from app.services import agent_sessions, conversation_store

    manager, _ = env
    service, _ = quotas
    project_id = manager.create_project("用量")["id"]
    service.sync()  # 发消息前闸门同步过一次（当时用量 0）
    with open(conversation_store.history_path(project_id, conversation_store.DEFAULT_ID), "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"type": "model_usage", "ts": time.time(),
                                 "usage": {"input_tokens": 1_500_000, "output_tokens": 0}}) + "\n")
    agent_sessions.purge_history(project_id)  # 删项目（SessionManager.discard）时清历史
    service.sync()
    assert service.used_today("local") == 1_500_000


def test_studio_review_handoff_is_quota_gated(env, quotas, monkeypatch):
    from app.routers import studio as studio_routes

    manager, client = env
    service, _ = quotas
    project_id = manager.create_project("复核")["id"]
    service.set_limits({"daily_tokens": 1000}, {})
    service.record(project_id, "local", 5000)
    monkeypatch.setattr(studio_routes.studio_service, "get_draft", lambda pid, rel: {"content": "# 订单\n", "base_revision": ""})
    monkeypatch.setattr(studio_routes.studio_service, "deterministic_check",
                        lambda root, rel, content: {"errors": [], "review": ["改了对象的主键"]})
    monkeypatch.setattr(studio_routes.studio_service, "stage_for_review", lambda root, rel, content: ".onto/run/review.md")
    response = client.post(f"/api/v1/projects/{project_id}/studio/entries/object:订单/submit", json={})
    assert response.status_code == 429 and response.json()["code"] == "quota_exceeded"
    assert response.json()["details"]["quota"] == "daily_tokens"


def test_concurrent_session_limit_holds_under_simultaneous_sends(env, quotas, monkeypatch):
    from app.governance import quota
    from tests.test_session_host import FakeEngine

    manager, _ = env
    service, pool = quotas
    service.set_limits({"concurrent_sessions": 1}, {})
    first, second = (manager.create_project(name)["id"] for name in ("并发一", "并发二"))

    class SlowEngine(FakeEngine):
        async def connect(self) -> bool:
            await asyncio.sleep(0.3)  # 连上之前另一条发送已经开始
            return False

    monkeypatch.setattr(pool, "_engine_class_for", lambda project_id: SlowEngine)

    async def scenario():
        results = await asyncio.gather(
            pool.send(first, manager.project_root(first), "开始建模"),
            pool.send(second, manager.project_root(second), "开始建模"),
            return_exceptions=True)
        await pool.shutdown()
        return results

    results = asyncio.run(scenario())
    refused = [item for item in results if isinstance(item, quota.QuotaExceeded)]
    assert len(refused) == 1 and refused[0].details["quota"] == "concurrent_sessions"
    assert len([item for item in results if not isinstance(item, BaseException)]) == 1


# ---- 6、7. 知识：提名服务端重新脱敏；同名条目覆盖行业条目 ----


def _industry_project(client) -> str:
    created = client.post("/api/v1/projects", json={"name": "零售", "goal": "合成场景：门店补货",
                                                    "industry_packs": ["industry-retail"]})
    assert created.status_code == 201, created.text
    return created.json()["id"]


def test_nomination_rejects_text_that_still_needs_sanitizing(env):
    _, client = env
    project_id = _industry_project(client)
    base = f"/api/v1/projects/{project_id}/knowledge"
    entry = client.post(base, json={"title": "补货周期口径", "type": "convention", "rule_md": "按日均销量乘以补货周期"})
    assert entry.status_code == 201, entry.text
    entry_id = entry.json()["entry"]["id"]
    raw = "联系 13800000000 或 someone@example.com，示例有限公司 合同 HT-20231234 金额 50000元"
    refused = client.post(f"{base}/{entry_id}/nominations", json={
        "industry_pack": "industry-retail", "confirm_general": True, "sanitized_text": raw})
    assert refused.status_code == 422 and refused.json()["code"] == "nomination_not_sanitized"
    found = {item["item"] for item in refused.json()["details"]["checks"]}
    assert found == {"secret_or_pii", "customer_name", "internal_id", "amount"}
    assert "13800000000" not in refused.text  # 不回显命中的原文
    preview = client.post(f"{base}/{entry_id}/nomination-preview", json={"industry_pack": "industry-retail"}).json()
    accepted = client.post(f"{base}/{entry_id}/nominations", json={
        "industry_pack": "industry-retail", "confirm_general": True, "sanitized_text": preview["sanitized_text"]})
    assert accepted.status_code == 201, accepted.text


def test_same_title_project_entry_overrides_industry_entry(env):
    _, client = env
    project_id = _industry_project(client)
    base = f"/api/v1/projects/{project_id}/knowledge"
    industry = client.get(base, params={"layer": "industry", "limit": 200}).json()["items"]
    target = industry[0]
    created = client.post(base, json={"title": target["title"], "type": target["type"],
                                      "rule_md": "本项目按近 6 个月的口径"})
    assert created.status_code == 201, created.text
    entry = created.json()["entry"]
    assert entry["overrides_industry"] is True
    layers = client.get(base).json()["layers"]
    assert next(layer for layer in layers if layer["layer"] == "project")["overridden"] == 1
    detail = client.get(f"{base}/{entry['id']}").json()
    assert detail["overrides"]["entry_id"] == target["id"]


# ---- 8. 目录符号链接读不到工作区外 ----


def test_artifacts_do_not_follow_directory_symlinks_out_of_project(env):
    manager, client = env
    first, second = (manager.create_project(name)["id"] for name in ("甲项目", "乙项目"))
    root_a, root_b = manager.project_root(first), manager.project_root(second)
    _write(root_b, "ontology/testcase/机密流程.md",
           "# 测试场景：机密流程\n\n## 场景 1：乙项目的场景\n\n- **输入**：\n  - `x` = `1`\n- **预期路径**：乙项目内部口径\n")
    target = root_a / "ontology" / "testcase"
    if target.exists():
        import shutil

        shutil.rmtree(target)
    os.symlink(os.path.relpath(root_b / "ontology" / "testcase", root_a / "ontology"), target)
    rows = client.get(f"/api/v1/projects/{first}/artifacts/trace.scenario").json()
    assert rows["items"] == []
    # 项目里正常的文件照常读
    rows_b = client.get(f"/api/v1/projects/{second}/artifacts/trace.scenario").json()
    assert [item["key"] for item in rows_b["items"]] == ["机密流程--1"]


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


# ---- 11. 产物保存：版本校验和写入在同一把锁里 ----


def test_artifact_saves_racing_on_same_revision_do_not_lose_updates(env, monkeypatch):
    from app.kernel.artifacts import service as artifact_service

    manager, client = env
    project_id = manager.create_project("并发保存")["id"]
    root = manager.project_root(project_id)
    _write(root, SCENARIO_REL, SCENARIO)
    _commit_all(root)
    row = next(item for item in client.get(f"/api/v1/projects/{project_id}/artifacts/trace.scenario").json()["items"]
               if item["key"] == "补货编制主流程--1")
    original = artifact_service._write_text
    inside, release = threading.Event(), threading.Event()

    def slow_first(*args, **kwargs):
        if not inside.is_set():
            inside.set()
            release.wait(5)  # 第一个保存校验过版本、正在算内容时，第二个保存进来
        return original(*args, **kwargs)

    monkeypatch.setattr(artifact_service, "_write_text", slow_first)
    outcomes: dict[str, object] = {}

    def save(name: str, title: str) -> None:
        try:
            artifact_service.save(manager, project_id, "trace.scenario", "补货编制主流程--1",
                                  {"base_revision": row["revision"], "fields": {"title": title}},
                                  actor="local", iso=deps.iso)
            outcomes[name] = "saved"
        except artifact_service.ArtifactError as exc:
            outcomes[name] = exc.code

    first = threading.Thread(target=save, args=("first", "甲改的标题"))
    first.start()
    assert inside.wait(5)
    second = threading.Thread(target=save, args=("second", "乙改的标题"))
    second.start()
    time.sleep(0.3)
    release.set()
    first.join(10)
    second.join(10)
    assert sorted(outcomes.values()) == ["revision_conflict", "saved"]
    assert "## 场景 1：甲改的标题" in (root / SCENARIO_REL).read_text(encoding="utf-8")
