"""拆可选包（方案 D7 后半；详设 5.3、第 14 章第 15、16 项）、第一个行业包（详设 5.5）与命令名映射。

用仓库里真实的资产：core 加 assets/packs 下的官方包。项目、条目、知识全部是合成内容。
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth
from app.services.packs import builtin, checks, lockfile
from app.services.packs.lockfile import PackLock
from app.services.workspace import WorkspaceManager
from tests.test_event_streams import buffered

ASSETS = Path(config.ASSETS_DIR)
OPTIONAL = ("trace", "knowledge", "codegen", "diagram", "evo", "websearch", "migrate")
DEFAULT_ON = ("codegen", "diagram", "knowledge", "trace")

# 前端插件页发的命令（_plan/contract-changes.md F3「页面命令的命令名与参数」）→ 后端实际的命令。
# 名字不同的以后端现有命令为准；表里有、后端没有的在对应包里补了（knowledge-review）。
COMMAND_MAP = {
    "/ontology-trace-batch": ("trace", "/ontology-trace-batch"),
    "/ontology-trace-refresh": ("trace", "/ontology-trace-batch"),
    "/ontology-trace-walk": ("trace", "/ontology-trace-interactive"),
    "/ontology-mock": ("trace", "/ontology-mock"),
    "/knowledge-review": ("knowledge", "/knowledge-review"),
    "/ontology-codegen-scan": ("codegen", "/ontology-codegen-scan"),
    "/ontology-codegen": ("codegen", "/ontology-codegen"),
    "/ontology-solver-evaluator": ("codegen", "/ontology-solver-evaluator"),
    "/ontology-update": ("core", "/ontology-update"),
    "/ontology-evolution": ("evo", "/ontology-evolution"),
    "/ontology-research": ("core", "/ontology-research"),
}


@pytest.fixture(autouse=True)
def _fresh():
    builtin.clear_cache()
    yield
    builtin.clear_cache()


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    manager = WorkspaceManager(tmp_path / "data", ASSETS)
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return manager, TestClient(app)


def _commands(root: Path) -> set[str]:
    return {path.stem for path in (root / ".claude" / "commands").glob("*.md")}


def _lock_names(root: Path) -> dict[str, bool]:
    return {entry.name: entry.enabled for entry in lockfile.read(root).packs}


# ---- 拆分本身 ----


def test_builtin_optional_packs_are_imported_and_pass_static_checks(env, tmp_path):
    manager, _ = env
    manager.packs.core_version()
    repository = manager.packs.repository
    assert set(OPTIONAL) | {"core", "industry-retail"} <= set(repository.names())
    core_names = checks.declared_names(repository.manifest("core", repository.latest("core")),
                                       repository.version_dir("core", repository.latest("core")))
    for name in (*OPTIONAL, "industry-retail"):
        version = repository.latest(name, "stable")
        manifest, items = checks.run(repository.version_dir(name, version), core_names=core_names,
                                     official=repository.is_builtin(name, version))
        failed = [item for item in items if item["result"] == "fail" and item["id"] != "names"]
        assert manifest is not None and not failed, (name, failed)
        assert repository.manifest(name, version).raw.get("split_from_core") or name == "industry-retail"


def test_core_no_longer_carries_split_out_commands_and_keeps_knowledge_loading():
    toolkit = ASSETS / "workspace-toolkit"
    commands = {path.stem for path in (toolkit / "commands").glob("*.md")}
    assert commands == {"ontology", "ontology-research", "ontology-extract-object", "ontology-extract-operation",
                        "ontology-extract-rule-metric", "ontology-validate", "ontology-update", "ontology-impact",
                        "ontology-query", "ontology-overview", "version"}
    # 知识的存放与加载留在 core（第 14 章第 15 项），沉淀这一段进知识沉淀包
    assert (toolkit / "scripts" / "knowledge_helper.py").is_file()
    assert not (toolkit / "agents" / "knowledge-conflict-agent.md").exists()
    assert (ASSETS / "packs" / "knowledge" / "KNOWLEDGE_SEDIMENTATION.md").is_file()
    assert "KNOWLEDGE_SEDIMENTATION.md" in (toolkit / "BUSINESS_KNOWLEDGE_SNIPPET.md").read_text(encoding="utf-8")
    # 增量更新留在 core，作为按需节点（第 16 项）
    flow = (toolkit / "flow.yaml").read_text(encoding="utf-8")
    assert "id: core.update" in flow and "placement: on_demand" in flow
    # B1 留下的泛化检查与 /ontology-evolution 迁进反馈演进包
    assert (ASSETS / "packs" / "evo" / "scripts" / "check_eval_generalization.py").is_file()
    assert (ASSETS / "packs" / "evo" / "commands" / "ontology-evolution.md").is_file()
    for dead in ("generate_overview.py", "split_drawio.py", "summary_to_drawio.py"):
        assert not (toolkit / "scripts" / dead).exists()


def test_new_project_enables_default_capabilities(env):
    manager, _ = env
    root = manager.project_root(manager.create_project("缺省能力")["id"])
    lock = lockfile.read(root)
    assert lock.lock_version == lockfile.LOCK_VERSION
    assert set(_lock_names(root)) == {"core", *DEFAULT_ON}
    commands = _commands(root)
    assert {"ontology-trace", "ontology-mock", "ontology-codegen", "ontology-flowchart", "knowledge-review"} <= commands
    assert "ontology-evolution" not in commands
    assert (root / ".claude" / "KNOWLEDGE_SEDIMENTATION.md").is_file()
    assert not (root / ".claude" / "skills" / "deep-research").exists()
    flow = json.loads((root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))
    assert [node["id"] for node in flow["nodes"]] == [
        "core.research", "diagram.flowchart", "core.object", "core.operation", "core.rule_metric", "core.validate",
        "trace.mock", "trace.trace", "codegen.codegen", "codegen.solver", "core.update"]
    assert flow["commands"]["trace"]["node"] == "trace.trace" and flow["commands"]["flowchart"]["node"] == "diagram.flowchart"
    assert "ontology-codegen-scan" in flow["available_commands"]


def test_core_only_project_assembles_and_filters_suggestions(env):
    manager, client = env
    root = manager.project_root(manager.create_project("只有核心", packs=[])["id"])
    assert set(_lock_names(root)) == {"core"}
    assert _commands(root) == {"ontology", "ontology-research", "ontology-extract-object", "ontology-extract-operation",
                               "ontology-extract-rule-metric", "ontology-validate", "ontology-update", "ontology-impact",
                               "ontology-query", "ontology-overview", "version"}
    flow = json.loads((root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))
    assert [node["id"] for node in flow["nodes"]] == [
        "core.research", "core.object", "core.operation", "core.rule_metric", "core.validate", "core.update"]
    suggested = {item.get("command") for items in flow["next_steps"].values() for item in items}
    assert not suggested & {"/ontology-mock", "/ontology-trace", "/ontology-codegen", "/ontology-flowchart"}
    # 「完成」之后不再推荐扫描脚本候选（代码生成与求解包没启用）
    for rel in ("research/门店补货.md", "ontology/object/门店.md", "ontology/operation/补货编制主流程.md"):
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text("# x\n", encoding="utf-8")
    (root / "reports").mkdir(exist_ok=True)
    (root / "reports" / "20260101-000000-all-rule.md").write_text("- [x] 已修\n", encoding="utf-8")
    body = client.get(f"/api/v1/projects/{root.name}/flow").json()
    assert body["suggestions"]["primary"]["action"]["command"] == "/ontology-overview"
    assert body["suggestions"]["secondary"] == []
    # 场景验证没启用：代码生成挂在质量检查之后
    enabled = client.post(f"/api/v1/projects/{root.name}/packs", json={"name": "codegen"})
    assert enabled.status_code == 201, enabled.text
    manager.packs.apply(root)
    flow = json.loads((root / ".claude" / "flow.yaml").read_text(encoding="utf-8"))
    codegen = next(node for node in flow["nodes"] if node["id"] == "codegen.codegen")
    assert codegen["after"] == "core.validate"


@pytest.mark.parametrize("name, page, command", [
    ("trace", "scenarios", "ontology-trace-batch"),
    ("knowledge", "knowledge", "knowledge-record"),
    ("codegen", "scripts", "ontology-codegen-scan"),
    ("diagram", None, "ontology-flowchart"),
    ("evo", None, "ontology-evolution"),
    ("websearch", None, None),
    ("migrate", None, None),
])
def test_each_optional_pack_enables_and_disables_per_project(env, name, page, command):
    manager, client = env
    project_id = manager.create_project("按项目启停", packs=[])["id"]
    root = manager.project_root(project_id)
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": name})
    assert response.status_code == 201, response.text
    manager.packs.apply(root)
    nav = {item["id"] for item in client.get(f"/api/v1/projects/{project_id}/extensions").json()["nav"]}
    if page:
        assert page in nav
    if command:
        assert command in _commands(root)
    skill = {"websearch": "deep-research", "migrate": "extract-migrated-objects"}.get(name)
    if skill:
        assert (root / ".claude" / "skills" / skill / "SKILL.md").is_file()

    disabled = client.post(f"/api/v1/projects/{project_id}/packs/{name}/disable")
    assert disabled.status_code == 200, disabled.text
    manager.packs.apply(root)
    view = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    if page:
        assert page not in {item["id"] for item in view["nav"]}
        assert page in {item["id"] for item in view["disabled_pages"]}
    if command:
        assert command not in _commands(root)
    assert _lock_names(root)[name] is False
    # 重新启用回到原锁定版本
    again = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": name})
    assert again.status_code == 201 and _lock_names(root)[name] is True


def test_core_cannot_be_disabled(env):
    manager, client = env
    project_id = manager.create_project("核心必装")["id"]
    response = client.post(f"/api/v1/projects/{project_id}/packs/core/disable")
    assert response.status_code == 409 and response.json()["code"] == "pack_required"


def test_legacy_lock_gets_split_packs_when_core_is_split(env):
    """存量项目（锁文件版本 1，只锁了 core）跟随 core 升到拆分后的版本时，补上拆出去的全部能力，行为不变。"""
    manager, _ = env
    root = manager.project_root(manager.create_project("存量")["id"])
    core = lockfile.read(root).get("core")
    legacy = PackLock([core], lock_version=1)
    lockfile.write(root, legacy)
    lock, wrote = manager.packs.ensure_lock(root)
    assert wrote and lock.lock_version == lockfile.LOCK_VERSION
    assert set(_lock_names(root)) == {"core", *OPTIONAL}
    assert all(_lock_names(root).values())
    # 迁移只做一次：之后用户停用的包不会再被补回来
    lock.get("evo").enabled = False
    lockfile.write(root, lock)
    again, _ = manager.packs.ensure_lock(root)
    assert again.get("evo").enabled is False


# ---- 新建项目选能力与行业 ----


def test_create_project_with_capabilities_and_industry(env):
    manager, client = env
    body = {"name": "选能力", "goal": "合成场景：门店补货单编制",
            "capabilities": {"trace": False, "evo": True, "reverse": True},
            "industry_packs": [{"name": "industry-retail"}], "system_packs": []}
    created = client.post("/api/v1/projects", json=body)
    assert created.status_code == 201, created.text
    root = manager.project_root(created.json()["id"])
    names = _lock_names(root)
    assert set(names) == {"core", "codegen", "diagram", "knowledge", "evo", "industry-retail"}
    assert (root / ".claude" / "industry" / "industry-retail" / "concepts.json").is_file()
    # 工作区规则告诉助手怎么用行业包的规范清单、写法提示与参考库
    rules = (root / ".claude" / "CLAUDE.md").read_text(encoding="utf-8")
    assert ".claude/industry/<包名>/" in rules and "只作候选提示" in rules
    missing = client.post("/api/v1/projects", json={**body, "name": "没有的行业", "industry_packs": [{"name": "industry-none"}]})
    assert missing.status_code == 404 and missing.json()["code"] == "pack_not_found"
    wrong_kind = client.post("/api/v1/projects", json={**body, "name": "类别不对", "system_packs": ["industry-retail"]})
    assert wrong_kind.status_code == 404


def test_catalog_marks_default_on_and_categories(env):
    manager, client = env
    items = {item["name"]: item for item in client.get("/api/v1/packs", params={"category": "capability,industry"}).json()["items"]}
    assert items["industry-retail"]["category"] == "industry" and items["industry-retail"]["default_on"] is True
    assert items["industry-retail"]["label"] == "零售连锁"
    assert {name for name, item in items.items() if item["category"] == "capability" and item["default_on"]} == {
        "core", *DEFAULT_ON}
    assert items["trace"]["ui"]["kind"] == "page" and items["trace"]["ui"]["pages"] == [{"id": "scenarios", "title": "场景走查"}]
    assert items["evo"]["ui"]["kind"] == "none" and items["trace"]["badges"] == ["recommended"]


def test_research_mode_web_requires_websearch(env):
    manager, client = env
    project_id = manager.create_project("联网")["id"]
    blocked = client.patch(f"/api/v1/projects/{project_id}", json={"research_mode": "web"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "pack_not_enabled"
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "websearch"}).status_code == 201
    assert client.patch(f"/api/v1/projects/{project_id}", json={"research_mode": "web"}).status_code == 200


# ---- 命令名映射 ----


def test_page_commands_resolve_to_pack_commands():
    for page_command, (pack, backend) in COMMAND_MAP.items():
        base = ASSETS / "workspace-toolkit" if pack == "core" else ASSETS / "packs" / pack
        assert (base / "commands" / f"{backend.lstrip('/')}.md").is_file(), page_command


def test_page_actions_declared_by_packs_exist():
    import yaml

    for name in ("trace", "knowledge", "codegen"):
        manifest = yaml.safe_load((ASSETS / "packs" / name / "manifest.yaml").read_text(encoding="utf-8"))
        commands = set(manifest["contributes"]["commands"])
        for page in manifest["contributes"]["pages"]:
            for action in page["actions"]:
                assert action["command"].lstrip("/") in commands, (name, action)


# ---- 第一个行业包与分层的业务知识 ----


def _industry_project(manager, *, knowledge: bool = True) -> tuple[str, Path]:
    packs = ["industry-retail"] + (["knowledge"] if knowledge else [])
    project_id = manager.create_project("零售", packs=packs)["id"]
    return project_id, manager.project_root(project_id)


def test_industry_layer_lists_and_overrides(env):
    manager, client = env
    project_id, root = _industry_project(manager)
    base = f"/api/v1/projects/{project_id}/knowledge"
    body = client.get(base, params={"limit": 200}).json()
    assert body["counts"]["project"] == 0 and body["counts"]["industry"] >= 10
    industry = next(layer for layer in body["layers"] if layer["layer"] == "industry")
    assert industry["label"] == "零售连锁" and industry["packs"] == ["industry-retail"]
    assert "零售连锁" in body["loading_rule"]
    entry = next(item for item in body["items"] if item["id"].endswith("safety-stock-formula"))
    assert entry["layer"] == "industry" and entry["readonly"] is True and entry["pack"] == "industry-retail"
    detail = client.get(f"{base}/{entry['id']}").json()
    assert detail["rule_md"] and detail["source"]["kind"] == "pack"
    readonly = client.put(f"{base}/{entry['id']}", json={"title": "改", "type": "domain_rule", "rule_md": "x",
                                                         "base_revision": detail["revision"]})
    assert readonly.status_code == 403 and readonly.json()["code"] == "artifact_readonly"

    # 项目里写一条同 slug 的条目覆盖行业条目；删掉后行业条目重新生效
    created = client.post(base, json={"title": "安全库存测算", "type": "domain_rule", "rule_md": "本项目按 5 天缓冲测算"})
    assert created.status_code == 201, created.text
    project_entry = created.json()["entry"]
    assert project_entry["id"] == "domain-rule-安全库存测算"
    slug = json.loads(json.dumps(project_entry))["id"]
    # 改成与行业条目相同的 slug 才算覆盖：直接按行业条目的文件名写一条
    helper = root / ".claude" / "scripts" / "knowledge_helper.py"
    subprocess.run([sys.executable, str(helper), "add", "--type", "domain-rule", "--slug", "safety-stock-formula",
                    "--name", "安全库存（本项目口径）", "--scope", "global", "--source", "web", "--body", "## 规则\n\n按 5 天缓冲\n",
                    "--after-conflict-check", "none-found"], cwd=root, check=True, capture_output=True)
    listed = client.get(base, params={"layer": "project"}).json()
    override = next(item for item in listed["items"] if item["id"] == "domain-rule-safety-stock-formula")
    assert override["overrides_industry"] is True
    assert next(layer for layer in listed["layers"] if layer["layer"] == "project")["overridden"] == 1
    detail = client.get(f"{base}/domain-rule-safety-stock-formula").json()
    assert detail["overrides"]["pack"] == "industry-retail" and detail["overrides"]["entry_id"] == entry["id"]
    conflicts = client.post(f"{base}/conflicts").json()
    kinds = {item["kind"] for item in conflicts["conflicts"]}
    assert "scope_conflict" in kinds and client.get(f"{base}/conflicts").json()["checked_at"]
    subprocess.run(["git", "add", "-A"], cwd=root, check=True)
    subprocess.run(["git", "commit", "-qm", "agent: 覆盖"], cwd=root, check=True)
    deleted = client.request("DELETE", f"{base}/domain-rule-safety-stock-formula", json={})
    assert deleted.status_code == 200 and deleted.json()["restored_industry_entry"] == entry["id"]
    assert [event["data"]["change"] for event in buffered(project_id, "knowledge.updated")] == ["created", "deleted"]
    assert slug


def test_knowledge_helper_loads_industry_layer_by_concept(env):
    manager, _ = env
    _, root = _industry_project(manager)
    helper = root / ".claude" / "scripts" / "knowledge_helper.py"
    out = subprocess.run([sys.executable, str(helper), "load", "--scope", "object:补货单"], cwd=root,
                         capture_output=True, text=True, check=True).stdout
    assert "来源：零售连锁（行业包" in out and "补货" in out
    unrelated = subprocess.run([sys.executable, str(helper), "load", "--scope", "object:无关对象"], cwd=root,
                               capture_output=True, text=True, check=True).stdout
    assert len(unrelated) < len(out)


def _checker_issues(root: Path) -> list[dict]:
    checker = root / ".claude" / "scripts" / "ontology_checker.py"
    run = subprocess.run([sys.executable, str(checker), "ontology", "--json"], cwd=root,
                         capture_output=True, text=True, check=True)
    return json.loads(run.stdout)


def test_checker_applies_enabled_industry_rules_as_warnings(env):
    """详设 5.5「校验」：ontology_checker 额外加载已启用行业包的声明式规则，只给警告。"""
    manager, _ = env
    _, root = _industry_project(manager, knowledge=False)
    objects = root / "ontology" / "object"
    objects.mkdir(parents=True, exist_ok=True)
    (objects / "补货单.md").write_text(
        "# 对象描述\n门店的要货单据。\n\n# 对象别名\n要货单\n\n# 对象属性清单\n"
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n|---|---|---|---|---|---|---|\n"
        "| 单号 | order_no | 单据编号 | 是 | 字符串 | - | 必填 |\n\n# 关联对象\n- 当前对象 → 收货 → [[门店]]\n",
        encoding="utf-8")
    (objects / "门店.md").write_text("# 对象描述\n经营单元。\n\n# 对象属性清单\n"
                                     "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |\n"
                                     "|---|---|---|---|---|---|---|\n| 门店编码 | store_code | 编码 | 是 | 字符串 | - | 必填 |\n",
                                     encoding="utf-8")
    issues = [item for item in _checker_issues(root) if item["category"] == "行业规则"]
    by_rule = {item["message"].split("（")[0]: item for item in issues}
    relation = by_rule["补货单要关联门店与商品"]
    assert relation["severity"] == "警告" and "零售连锁 RETAIL-R01" in relation["message"]
    assert "「商品」" in relation["message"] and "「门店」" not in relation["message"].split("：")[1].split("。")[0]
    # 别名段里的非标准称谓不算；正文里的算
    assert "「要货单」建议改称「补货单」" in by_rule["使用行业标准称谓"]["message"]
    assert all(item["severity"] in ("警告", "建议") for item in issues)

    # 没启用行业包的项目不加载这些规则
    plain = manager.project_root(manager.create_project("无行业")["id"])
    shutil.copytree(objects, plain / "ontology" / "object", dirs_exist_ok=True)
    assert not [item for item in _checker_issues(plain) if item["category"] == "行业规则"]


def test_nomination_needs_knowledge_pack_and_sanitizes(env):
    manager, client = env
    project_id, _ = _industry_project(manager, knowledge=False)
    base = f"/api/v1/projects/{project_id}/knowledge"
    entry = client.post(base, json={"title": "大单要审批", "type": "domain_rule",
                                    "rule_md": "单店单次补货超过 150 箱转区域审批，联系 13800000000，¥5000 以上另批"}).json()["entry"]
    blocked = client.post(f"{base}/{entry['id']}/nomination-preview", json={"industry_pack": "industry-retail"})
    assert blocked.status_code == 409 and blocked.json()["code"] == "pack_not_enabled"
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "knowledge"}).status_code == 201
    preview = client.post(f"{base}/{entry['id']}/nomination-preview", json={"industry_pack": "industry-retail"}).json()
    assert "13800000000" not in preview["sanitized_text"] and "5000" not in preview["sanitized_text"]
    found = {item["item"]: item["found"] for item in preview["checks"]}
    assert found["secret_or_pii"] == 1 and found["amount"] == 1 and preview["highlights"]
    assert preview["pack"] == {"name": "industry-retail", "label": "零售连锁"}
    unknown = client.post(f"{base}/{entry['id']}/nomination-preview", json={"industry_pack": "industry-none"})
    assert unknown.status_code == 409
    submitted = client.post(f"{base}/{entry['id']}/nominations", json={
        "industry_pack": "industry-retail", "sanitized_text": preview["sanitized_text"], "confirm_general": True})
    assert submitted.status_code == 201 and submitted.json()["state"] == "pending_review"
    unconfirmed = client.post(f"{base}/{entry['id']}/nominations", json={
        "industry_pack": "industry-retail", "sanitized_text": "x", "confirm_general": False})
    assert unconfirmed.status_code == 400


def test_prompt_rules_follow_active_assembly(env):
    from app.services.packs import registry

    manager, _ = env
    root = manager.project_root(manager.create_project("提示规则")["id"])
    hints = registry.prompt_hints(manager, root, "其实我们这边通常按箱补")
    assert len(hints) == 1 and "KNOWLEDGE_SEDIMENTATION" in hints[0]
    assert registry.prompt_hints(manager, root, "帮我画一张流程图") == []
    core_only = manager.project_root(manager.create_project("无提示", packs=[])["id"])
    assert registry.prompt_hints(manager, core_only, "其实我们这边通常按箱补") == []


def test_official_artifacts_on_existing_paths(env):
    manager, client = env
    project_id = manager.create_project("产物")["id"]
    root = manager.project_root(project_id)
    (root / "ontology/testcase").mkdir(parents=True, exist_ok=True)
    (root / "ontology/testcase/补货编制主流程.md").write_text(
        "# 测试场景：补货编制主流程\n\n## 场景 1：正常补货\n\n- **输入**：\n  - `qty` = `12`\n"
        "- **预期路径**：正常完成\n- **数据来源**：自定义\n", encoding="utf-8")
    (root / "ontology/data-mock/门店.md").write_text("# 门店\n\n| 门店编号 | 等级 |\n|---|---|\n| S01 | A |\n", encoding="utf-8")
    types = {item["type"] for item in client.get(f"/api/v1/projects/{project_id}/artifact-types").json()["items"]}
    assert {"trace.scenario", "trace.mock", "diagram.flowchart", "codegen.scan_report", "codegen.solver_contract",
            "core.knowledge"} <= types
    rows = client.get(f"/api/v1/projects/{project_id}/artifacts/trace.scenario").json()["items"]
    assert rows[0]["key"] == "补货编制主流程--1" and rows[0]["fields"]["input_kind"] == "custom"
    assert rows[0]["status"]["value"] == "not_run" and rows[0]["fields"]["operation"]["key"] == "补货编制主流程"
    mock = client.get(f"/api/v1/projects/{project_id}/artifacts/trace.mock").json()
    assert mock["items"][0]["fields"]["rows"] == 1 and mock["skipped"] == []
    readonly = client.put(f"/api/v1/projects/{project_id}/artifacts/core.knowledge/x", json={"base_revision": "x"})
    assert readonly.status_code in (403, 404)
