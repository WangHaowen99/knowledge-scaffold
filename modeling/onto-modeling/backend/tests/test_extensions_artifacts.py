"""扩展项注册表与左栏、插件产物登记与通用视图（契约 PL-12、AR-01～AR-09、M-08；方案 D17、C13）。

用一个测试专用的包（检查清单 `checklist`）验证 P2 的两条验收：
- 停用带页面的包后，左栏页面消失、产物保留；
- 新登记一类插件产物，不改前端就能列表、看详情（前端只认通用接口与登记）。
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.kernel import event_port
from app.kernel.artifacts import index as artifact_index
from app.kernel.artifacts import parsers, registry as artifact_registry
from app.main import app
from app.services import auth
from app.services import project_events as project_stream
from app.services.onto_mcp import workspace as mcp_workspace
from app.services.packs import builtin
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack
from tests.test_event_streams import buffered
from tests.test_packs_api import upload, zipped

ITEM_TYPE = {
    "type": "checklist.item",
    "title": "检查项",
    "path": "ext/checklist/items/{key}.md",
    "format": "md-frontmatter",
    "key": "filename",
    "schema": {"version": "1", "fields": [
        {"name": "title", "type": "text", "label": "检查项", "required": True},
        {"name": "level", "type": "enum", "label": "级别", "required": True,
         "enum": [{"value": "high", "label": "高", "tone": "red"}, {"value": "low", "label": "低", "tone": "gray"}]},
        {"name": "operation", "type": "ref", "label": "关联操作", "required": False},
        {"name": "result", "type": "enum", "label": "结论", "required": False, "enum": ["pass", "fail"]},
    ]},
    "sections": ["说明", "处理"],
    "refs": [{"field": "operation", "kind": "operation"}],
    "status": {"field": "level", "map": {"high": "高", "low": "低"}},
    "index": "ext/checklist/index/items.jsonl",
}
ITEM_VIEW = {
    "artifact": "checklist.item",
    "list": {"columns": ["title", "level", "operation", "result"], "filters": ["level"]},
    "detail": {"layout": "sections", "side": ["level", "operation"]},
    "card": {"title": "title", "chips": ["level"]},
}


def _checklist_files() -> dict[str, str]:
    import yaml

    return {
        "commands/checklist-run.md": "按检查清单逐项检查 $ARGUMENTS\n",
        "artifacts/item.yaml": yaml.safe_dump(ITEM_TYPE, allow_unicode=True),
        "views/item.yaml": yaml.safe_dump(ITEM_VIEW, allow_unicode=True),
    }


CHECKLIST_CONTRIBUTES = {
    "artifacts": ["artifacts/item.yaml"],
    "views": ["views/item.yaml"],
    "pages": [{"id": "scenarios", "page_api": ">=1.0 <2.0", "nav": {"title": "检查清单", "icon": "check", "order": 1},
               "badge": "inbox", "data": {"artifacts": ["checklist.item"]},
               "actions": [{"id": "run", "label": "跑一遍", "command": "/checklist-run"}]}],
    "gates": [{"id": "title-short", "label": "检查项标题不超过 20 字", "artifact": "checklist.item",
               "rules": [{"field": "title", "max_length": 20}]}],
}


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    event_port.clear()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "检查", "goal": "合成场景：门店补货"}).json()["id"]
    source = make_pack(tmp_path / "src", "checklist", "1.0.0", files=_checklist_files(),
                       contributes=CHECKLIST_CONTRIBUTES)
    accepted = upload(client, zipped(source)).json()
    detail = client.get(f"/api/v1/admin/packs/uploads/{accepted['id']}").json()
    assert detail["state"] == "passed", detail["checks"]
    assert client.post(f"/api/v1/admin/packs/uploads/{accepted['id']}/publish", json={"channel": "stable"}).status_code == 200
    yield client, manager, project_id
    builtin.clear_cache()


def _enable(client, project_id):
    response = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "checklist"})
    assert response.status_code == 201, response.text
    return response.json()


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit_all(root: Path, message: str = "agent: 生成") -> None:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", message], cwd=root, check=True, capture_output=True)


# ---- 扩展项注册表与左栏 ----


def test_extensions_nav_lists_kernel_pages_then_enabled_plugin_pages(env):
    client, _, project_id = env
    before = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    assert [item["id"] for item in before["nav"]] == ["wb-l2", "studio", "resources", "publish"]
    assert all(item["kind"] == "kernel" for item in before["nav"]) and before["page_api"] == "1.0"
    assert before["artifact_types"] == [] and before["disabled_pages"] == []

    _enable(client, project_id)
    view = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    plugin = [item for item in view["nav"] if item["kind"] == "plugin"]
    assert plugin == [{"id": "scenarios", "title": "检查清单", "icon": "check", "kind": "plugin", "pack": "checklist",
                       "pack_label": "checklist 包", "order": 10, "overflow": False,
                       "badge": {"source": "inbox", "count": 0}}]
    page = view["pages"][0]
    assert page["pack"] == "checklist" and page["data"]["artifacts"] == ["checklist.item"]
    assert page["actions"] == [{"id": "run", "label": "跑一遍", "command": "/checklist-run", "hint": ""}]
    # 能力包的按钮下一条消息才可用；左栏、产物类型马上出现
    assert page["pending"] is True and view["pending_hash"]
    assert view["artifact_types"] == ["checklist.item"] and view["views"] == ["checklist.item"]
    assert view["gates"] == [{"id": "title-short", "label": "检查项标题不超过 20 字", "pack": "checklist", "on": ["save"]}]
    events = buffered(project_id, "extensions.updated")
    assert events and {"nav", "pages", "artifact_types"} <= set(events[-1]["data"]["changed"])
    assert buffered(project_id, "assembly.updated")[-1]["data"]["state"] == "pending"


def test_page_not_in_builtin_pages_never_reaches_nav(env, tmp_path):
    client, manager, project_id = env
    from app.services.packs import registry

    _enable(client, project_id)
    view = registry.PackView("checklist", "1.0.0", manager.packs.repository.manifest("checklist", "1.0.0"),
                             manager.packs.repository.version_dir("checklist", "1.0.0"))
    assert registry._page_ok({"id": "scenarios"}, view.manifest)
    assert not registry._page_ok({"id": "my-own-page"}, view.manifest)
    assert not registry._page_ok({"id": "scenarios", "page_api": ">=2.0"}, view.manifest)


def test_disabling_page_pack_hides_page_and_keeps_artifacts(env):
    client, manager, project_id = env
    _enable(client, project_id)
    created = client.post(f"/api/v1/projects/{project_id}/artifacts/checklist.item",
                          json={"key": "shelf-limit", "fields": {"title": "陈列上限", "level": "high"},
                                "sections": {"说明": "超出陈列上限的补货要审批"}})
    assert created.status_code == 201, created.text
    root = manager.project_root(project_id)
    assert (root / "ext/checklist/items/shelf-limit.md").is_file()

    assert client.post(f"/api/v1/projects/{project_id}/packs/checklist/disable").status_code == 200
    view = client.get(f"/api/v1/projects/{project_id}/extensions").json()
    assert [item["id"] for item in view["nav"]] == ["wb-l2", "studio", "resources", "publish"]
    assert view["disabled_pages"] == [{"id": "scenarios", "pack": "checklist", "pack_label": "checklist 包",
                                       "title": "检查清单"}]
    assert view["artifact_types"] == []
    # 产物留在工作区与版本里；类型随包停用而不可读
    assert (root / "ext/checklist/items/shelf-limit.md").is_file()
    gone = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item")
    assert gone.status_code == 404 and gone.json()["code"] == "artifact_type_unknown"
    assert "nav" in buffered(project_id, "extensions.updated")[-1]["data"]["changed"]

    # 重新启用：页面回来，已有产物照常列出
    assert client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "checklist"}).status_code == 201
    rows = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item").json()
    assert [item["key"] for item in rows["items"]] == ["shelf-limit"]


def test_page_action_resolves_to_command_while_pack_enabled(env):
    """S-20 的 action {pack, page, action_id} 由注册表换成命令；停用后认不出。"""
    client, manager, project_id = env
    from app.services.packs import registry

    action = {"pack": "checklist", "page": "scenarios", "action_id": "run"}
    assert registry.resolve_action(manager, project_id, action) is None
    _enable(client, project_id)
    assert registry.resolve_action(manager, project_id, action) == {
        "command": "/checklist-run", "pack": "checklist", "page": "scenarios", "label": "跑一遍"}
    assert registry.resolve_action(manager, project_id, {**action, "action_id": "nope"}) is None
    assert registry.resolve_action(manager, project_id, {**action, "pack": "trace"}) is None
    assert registry.resolve_action(manager, project_id, "run") is None
    assert client.post(f"/api/v1/projects/{project_id}/packs/checklist/disable").status_code == 200
    assert registry.resolve_action(manager, project_id, action) is None


def test_extension_differ_compares_with_last_computed_registry(env, monkeypatch):
    """项目流 extensions.updated 的比较函数（会话宿主登记）：和本项目上一次算出的注册表比。"""
    client, manager, project_id = env
    from app.services.packs import registry

    # 只看这一个比较函数：启动时登记的那份在锁文件提交时也会换掉比较基准
    monkeypatch.setattr(project_stream, "_extension_differs", [])
    differ = registry.extension_differ(manager)
    registry._LAST_VIEWS.pop(project_id, None)
    assert differ(project_id, None, "h1") is None  # 这个进程里还没有基准：整份重拉
    assert differ(project_id, "h1", "h1") == []
    _enable(client, project_id)
    changed = differ(project_id, "h1", "h2")
    assert {"nav", "pages", "artifact_types", "views", "gates"} <= set(changed)
    client.get(f"/api/v1/projects/{project_id}/extensions")  # PL-12 读不动比较基准
    assert differ(project_id, "h2", "h2") == []
    # 进程里没有基准时，改锁文件之前的注册表就是基准
    registry._LAST_VIEWS.pop(project_id, None)
    assert client.post(f"/api/v1/projects/{project_id}/packs/checklist/disable").status_code == 200
    assert "nav" in differ(project_id, "h2", "h3")


# ---- 通用视图：新类型不改前端就能列表、看详情 ----


def test_new_artifact_type_lists_and_details_through_generic_api(env):
    client, manager, project_id = env
    _enable(client, project_id)
    root = manager.project_root(project_id)
    types = client.get(f"/api/v1/projects/{project_id}/artifact-types").json()["items"]
    item_type = next(item for item in types if item["type"] == "checklist.item")
    assert item_type["path"] == "ext/checklist/items/{key}.md" and item_type["format"] == "md-frontmatter"
    assert [field["name"] for field in item_type["schema"]["fields"]] == ["title", "level", "operation", "result"]
    assert item_type["schema"]["fields"][1]["enum"][0] == {"value": "high", "label": "高", "tone": "red"}
    assert item_type["schema"]["fields"][2]["kind"] == "operation"
    assert item_type["view"]["list"]["filters"] == ["level"] and item_type["view"]["card"]["chips"] == ["level"]

    # 助手按格式写进工作区的产物（不经接口），登记了就能读到
    _write(root, "ext/checklist/items/expiry.md",
           "---\ntitle: 临期商品下架\nlevel: low\noperation: '[[安全库存测算]]'\n---\n\n## 说明\n\n到期前 3 天下架\n\n## 处理\n\n转出清处理\n")
    _write(root, "ext/checklist/items/region-quota.md",
           "---\ntitle: 区域配额\nlevel: high\n---\n\n## 说明\n\n单店不超过 150 箱\n")
    _commit_all(root)
    rows = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item").json()
    assert rows["total"] == 2 and [item["key"] for item in rows["items"]] == ["expiry", "region-quota"]
    first = rows["items"][0]
    assert first["title"] == "临期商品下架" and first["status"] == {"value": "low", "label": "低", "tone": "gray"}
    assert first["fields"]["operation"] == {"kind": "operation", "id": "operation:安全库存测算", "key": "安全库存测算",
                                            "name": "安全库存测算"}
    assert rows["facets"]["level"] == [{"value": "high", "label": "高", "count": 1}, {"value": "low", "label": "低", "count": 1}]
    filtered = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item?filter[level]=high").json()
    assert [item["key"] for item in filtered["items"]] == ["region-quota"]
    searched = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item", params={"q": "出清"}).json()
    assert [item["key"] for item in searched["items"]] == ["expiry"]
    bad = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item?filter[title]=x")
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_argument"

    detail = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item/expiry").json()
    assert detail["sections"] == {"说明": "到期前 3 天下架", "处理": "转出清处理"}
    assert detail["refs"] == [{"kind": "operation", "id": "operation:安全库存测算", "key": "安全库存测算",
                               "name": "安全库存测算", "field": "operation"}]
    assert detail["format_version"] == "1" and detail["path"] == "ext/checklist/items/expiry.md"
    assert detail["revision"] and detail["lock"] is None
    missing = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item/none")
    assert missing.status_code == 404 and missing.json()["code"] == "artifact_not_found"


def test_save_versions_history_schema_and_gates(env):
    client, manager, project_id = env
    _enable(client, project_id)
    base = f"/api/v1/projects/{project_id}/artifacts/checklist.item"
    created = client.post(base, json={"key": "shelf-limit", "fields": {"title": "陈列上限", "level": "high"},
                                      "sections": {"说明": "超出要审批"}}).json()
    revision = created["artifact"]["revision"]
    assert created["version_label"] and created["commit"]

    saved = client.put(f"{base}/shelf-limit", json={"base_revision": revision, "fields": {"level": "low"},
                                                     "sections": {"处理": "区域审批"}, "summary": "降级"})
    assert saved.status_code == 200, saved.text
    artifact = saved.json()["artifact"]
    assert artifact["fields"]["level"] == "low" and artifact["sections"]["处理"] == "区域审批"
    assert artifact["sections"]["说明"] == "超出要审批"
    root = manager.project_root(project_id)
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout
    assert "Onto-Source: web" in log and "降级" in log

    stale_write = client.put(f"{base}/shelf-limit", json={"base_revision": revision, "fields": {"level": "high"}})
    assert stale_write.status_code == 409 and stale_write.json()["code"] == "revision_conflict"
    current = artifact["revision"]
    invalid = client.put(f"{base}/shelf-limit", json={"base_revision": current, "fields": {"level": "medium"}})
    assert invalid.status_code == 422 and invalid.json()["code"] == "artifact_schema_invalid"
    assert invalid.json()["details"]["errors"][0]["field"] == "level"
    assert saved.json()["warnings"] == []
    check = client.post(f"{base}/shelf-limit/validate", json={}).json()
    assert check == {"ok": True, "errors": [], "warnings": []}
    # 包声明的保存门禁只提示（F3b）：照常保存，没过的规则在 warnings 里；校验也只给警告
    gated = client.put(f"{base}/shelf-limit", json={"base_revision": current, "fields": {"title": "很" * 30}})
    assert gated.status_code == 200, gated.text
    assert [(item["code"], item["gate"], item["level"]) for item in gated.json()["warnings"]] == [
        ("pack_gate", "title-short", "warning")]
    assert gated.json()["artifact"]["fields"]["title"] == "很" * 30
    check = client.post(f"{base}/shelf-limit/validate", json={}).json()
    assert check["ok"] is True and check["errors"] == [] and check["warnings"][0]["gate"] == "title-short"

    history = client.get(f"{base}/shelf-limit/history").json()["items"]
    assert [item["source"] for item in history] == ["web", "web", "web"]
    assert history[0]["version_label"] and history[0]["summary"].startswith("web: 保存检查项")

    deleted = client.request("DELETE", f"{base}/shelf-limit",
                             json={"base_revision": gated.json()["artifact"]["revision"]})
    assert deleted.status_code == 200 and not (root / "ext/checklist/items/shelf-limit.md").exists()
    assert [event["data"]["change"] for event in event_port.recent(project_id, "artifacts.updated")] == [
        "created", "updated", "updated", "deleted"]


def test_new_artifact_must_stay_in_pack_directory_and_types_in_registration():
    ok = artifact_registry.parse_registration(dict(ITEM_TYPE), pack="checklist", pack_version="1.0.0", official=False)
    assert ok.path == "ext/checklist/items/*.md" and ok.path_template == "ext/checklist/items/{key}.md"
    for bad in ({"path": "ontology/object/*.md"}, {"path": "ext/other/*.md"}, {"type": "object"},
                {"type": "other.item"}, {"format": "xlsx"}, {"path": "ext/checklist/*/*.md"}):
        with pytest.raises(artifact_registry.RegistrationError):
            artifact_registry.parse_registration({**ITEM_TYPE, **bad}, pack="checklist", pack_version="1.0.0", official=False)
    # 官方包拆包时可以把已有产物登记在原目录（旧目录只读别名，详设 5.9.4）
    legacy = artifact_registry.parse_registration({**ITEM_TYPE, "path": "ontology/testcase/*.md"}, pack="checklist",
                                                  pack_version="1.0.0", official=True)
    assert legacy.key_from_path("ontology/testcase/下单.md") == "下单"
    assert legacy.key_from_path("ontology/testcase/sub/下单.md") is None


def test_static_check_rejects_artifact_outside_pack_dir(env, tmp_path):
    client, _, _ = env
    import yaml

    files = _checklist_files()
    files["artifacts/item.yaml"] = yaml.safe_dump({**ITEM_TYPE, "path": "ontology/rule/*.md"}, allow_unicode=True)
    source = make_pack(tmp_path / "bad", "checklist", "1.1.0", files=files, tenant=True,
                       contributes=CHECKLIST_CONTRIBUTES)
    accepted = upload(client, zipped(source), tenant="租户 A").json()
    detail = client.get(f"/api/v1/admin/packs/uploads/{accepted['id']}").json()
    check = next(item for item in detail["checks"] if item["id"] == "entity_types")
    assert check["result"] == "fail" and "ext/checklist/" in check["detail"]


# ---- 引用与过期、包脚本生成的索引 ----


def test_stale_when_referenced_entry_changes_or_disappears(env):
    client, manager, project_id = env
    _enable(client, project_id)
    root = manager.project_root(project_id)
    _write(root, "ontology/operation/安全库存测算.md", "# 业务描述\n测算安全库存\n")
    _write(root, "ext/checklist/items/expiry.md", "---\ntitle: 临期\nlevel: low\noperation: 安全库存测算\n---\n")
    _write(root, "ext/checklist/items/ghost.md", "---\ntitle: 无主\nlevel: low\noperation: 不存在的操作\n---\n")
    _commit_all(root)
    base = f"/api/v1/projects/{project_id}/artifacts/checklist.item"
    rows = {item["key"]: item for item in client.get(base).json()["items"]}
    assert rows["expiry"]["stale"] is False and rows["ghost"]["stale"] is True

    _write(root, "ontology/operation/安全库存测算.md", "# 业务描述\n测算安全库存（改过口径）\n")
    rows = {item["key"]: item for item in client.get(base).json()["items"]}
    assert rows["expiry"]["stale"] is True
    detail = client.get(f"{base}/expiry").json()
    assert "安全库存测算" in detail["stale_reason"]
    assert [item["key"] for item in client.get(base, params={"stale": "false"}).json()["items"]] == []
    # 产物重新生成（内容变了）后基线重置
    _write(root, "ext/checklist/items/expiry.md", "---\ntitle: 临期（重算）\nlevel: low\noperation: 安全库存测算\n---\n")
    assert {item["key"]: item for item in client.get(base).json()["items"]}["expiry"]["stale"] is False
    summary = client.get(f"{base}/summary").json()
    assert summary["total"] == 2 and summary["stale"] == 1
    assert summary["by"] == [{"value": "low", "label": "低", "count": 2}]


def test_after_commit_reports_artifacts_and_stale_transitions(env):
    """会话宿主每轮结束调用：给 workspace.changed 的 artifacts，并发 artifacts.updated（updated / stale / fresh）。"""
    from app.kernel.artifacts import service as artifact_service

    client, manager, project_id = env
    _enable(client, project_id)
    root = manager.project_root(project_id)
    _write(root, "ontology/operation/安全库存测算.md", "# 业务描述\n测算安全库存\n")
    _write(root, "ext/checklist/items/expiry.md", "---\ntitle: 临期\nlevel: low\noperation: 安全库存测算\n---\n")
    _commit_all(root)
    event_port.clear()
    refs = artifact_service.after_commit(manager, project_id, ["ext/checklist/items/expiry.md", "ontology/x.md"])
    assert refs == [{"type": "checklist.item", "key": "expiry", "title": "临期"}]
    assert [event["data"] for event in event_port.recent(project_id, "artifacts.updated")] == [
        {"type": "checklist.item", "change": "updated", "keys": ["expiry"]}]

    event_port.clear()
    _write(root, "ontology/operation/安全库存测算.md", "# 业务描述\n测算安全库存（改过口径）\n")
    assert artifact_service.after_commit(manager, project_id, ["ontology/operation/安全库存测算.md"]) == []
    assert [event["data"] for event in event_port.recent(project_id, "artifacts.updated")] == [
        {"type": "checklist.item", "change": "stale", "keys": ["expiry"]}]

    event_port.clear()
    _write(root, "ontology/operation/安全库存测算.md", "# 业务描述\n测算安全库存\n")
    artifact_service.after_commit(manager, project_id, ["ontology/operation/安全库存测算.md"])
    assert [event["data"]["change"] for event in event_port.recent(project_id, "artifacts.updated")] == ["fresh"]


def test_pack_script_index_fills_computed_fields_and_flags_outdated(env):
    client, manager, project_id = env
    _enable(client, project_id)
    root = manager.project_root(project_id)
    text = "---\ntitle: 陈列上限\nlevel: high\n---\n"
    _write(root, "ext/checklist/items/shelf.md", text)
    digest = artifact_index.sha(text.encode())
    _write(root, "ext/checklist/index/items.jsonl",
           json.dumps({"key": "shelf", "fields": {"result": "pass"}, "sections": {"处理": "已推演"},
                       "source_sha256": digest}, ensure_ascii=False) + "\n")
    _commit_all(root)
    base = f"/api/v1/projects/{project_id}/artifacts/checklist.item"
    row = client.get(base).json()["items"][0]
    assert row["fields"]["result"] == "pass" and row["stale"] is False
    assert client.get(f"{base}/shelf").json()["sections"]["处理"] == "已推演"
    _write(root, "ext/checklist/items/shelf.md", text + "\n## 说明\n\n改了\n")
    detail = client.get(f"{base}/shelf").json()
    assert detail["stale"] is True and "索引" in detail["stale_reason"]


def test_summary_trend_follows_mainline_versions(env):
    client, manager, project_id = env
    _enable(client, project_id)
    root = manager.project_root(project_id)
    _write(root, "ext/checklist/items/a.md", "---\ntitle: A\nlevel: high\n---\n")
    _write(root, "ext/checklist/items/b.md", "---\ntitle: B\nlevel: low\n---\n")
    _commit_all(root, "agent: 第一版")
    _write(root, "ext/checklist/items/b.md", "---\ntitle: B\nlevel: high\n---\n")
    _commit_all(root, "agent: 第二版")
    body = client.get(f"/api/v1/projects/{project_id}/artifacts/checklist.item/summary", params={"trend": "true"}).json()
    assert [point["value_pct"] for point in body["trend"]] == [50.0, 100.0]
    assert body["delta_vs_previous_pct"] == 50.0


# ---- MCP 只读工具 ----


def test_mcp_artifact_tools_are_capped_read_only(env):
    client, manager, project_id = env
    _enable(client, project_id)
    root = manager.project_root(project_id)
    _write(root, "ext/checklist/items/long.md", "---\ntitle: 长说明\nlevel: low\n---\n\n## 说明\n\n" + "长" * 6000 + "\n")
    _commit_all(root)
    listed = mcp_workspace.execute(manager, project_id, "artifact_list", {"type": "checklist.item", "limit": 5}, "local")
    assert listed["total"] == 1 and listed["items"][0]["key"] == "long"
    got = mcp_workspace.execute(manager, project_id, "artifact_get", {"type": "checklist.item", "key": "long"}, "local")
    assert got["truncated"] is True and len(got["artifact"]["sections"]["说明"]) == 4000
    from app.services.onto_mcp.errors import McpError

    with pytest.raises(McpError) as error:
        mcp_workspace.execute(manager, project_id, "artifact_get", {"type": "checklist.missing", "key": "x"}, "local")
    assert error.value.code == "ARTIFACT_TYPE_UNKNOWN"


# ---- 内置解析器 ----


SECTIONS_TYPE = artifact_registry.parse_registration({
    "type": "trace.scenario", "title": "场景", "path": "ontology/testcase/*.md", "format": "md-sections",
    "schema": {"fields": [{"name": "title", "type": "text", "required": True},
                          {"name": "operation", "type": "ref"}, {"name": "expected", "type": "text"}]},
    "sections": ["输入", "预期结论", "推演记录"],
    "section_labels": {"预期结论": "预期路径"},
    "field_labels": {"expected": "预期路径", "source_label": "数据来源"},
    "derive": {"operation": "h1_suffix"},
    "refs": [{"field": "operation", "kind": "operation"}],
}, pack="trace", pack_version="1.0.0", official=True)

TESTCASE = """# 测试场景：补货编制主流程

> 由 /ontology-trace 生成

## 场景 1：正常补货

- **输入**：
  - `store_id` = `"S01"`
  - `qty` = `12`
- **预期路径**：正常完成
- **数据来源**：正常数据第1行

## 场景 2：超出陈列上限

- **输入**：
  - `qty` = `400`
- **预期路径**：异常终止于步骤3
- **数据来源**：自定义

<!--
## 场景 9：注释里的不算
-->
"""


def test_md_sections_parse_and_rewrite_one_item():
    items = parsers.parse(SECTIONS_TYPE, "ontology/testcase/补货编制主流程.md", TESTCASE.encode(),
                          file_key="补货编制主流程")
    assert [item.key for item in items] == ["补货编制主流程--1", "补货编制主流程--2"]
    first = items[0]
    assert first.title == "正常补货" and first.fields["operation"] == "补货编制主流程"
    assert first.fields["expected"] == "正常完成" and first.fields["source_label"] == "正常数据第1行"
    assert first.sections["输入"].startswith("\n") is False and "`qty` = `12`" in first.sections["输入"]
    assert first.sections["预期结论"] == "正常完成"
    text = parsers.render(SECTIONS_TYPE, TESTCASE, fields={"title": "超量需审批"},
                          sections={"预期结论": "转区域审批"}, item_key="补货编制主流程--2")
    again = parsers.parse(SECTIONS_TYPE, "x.md", text.encode(), file_key="补货编制主流程")
    assert again[0].sections == first.sections and again[0].title == "正常补货"
    assert again[1].title == "超量需审批" and again[1].sections["预期结论"] == "转区域审批"
    assert "## 场景 2：超量需审批" in text and "<!--" in text


def test_frontmatter_and_table_parsers():
    table_type = artifact_registry.parse_registration({
        "type": "trace.mock", "path": "ontology/data-mock/*.md", "format": "md-frontmatter",
        "derive": {"object": "stem", "rows": "table_rows"}, "schema": {"fields": [{"name": "object", "type": "ref"},
                                                                                {"name": "rows", "type": "number"}]},
    }, pack="trace", pack_version="1", official=True)
    text = "# 门店 测试数据\n\n| 门店编号 | 等级 |\n|---|---|\n| S01 | A |\n| S02 | B |\n"
    item = parsers.parse(table_type, "ontology/data-mock/门店.md", text.encode(), file_key="门店")[0]
    assert item.fields == {"object": "门店", "rows": 2}
    assert item.table == {"columns": ["门店编号", "等级"], "rows": [["S01", "A"], ["S02", "B"]], "total_rows": 2}
    rendered = parsers.render(ITEM_REG, "---\ntitle: 旧\nlevel: low\n---\n\n## 说明\n\n旧说明\n",
                              fields={"title": "新"}, sections={"处理": "补上"})
    meta, body = parsers.split_frontmatter(rendered)
    assert meta == {"title": "新", "level": "low"}
    assert parsers.sections_of(body) == {"说明": "旧说明", "处理": "补上"}


ITEM_REG = artifact_registry.parse_registration(dict(ITEM_TYPE), pack="checklist", pack_version="1.0.0", official=False)
