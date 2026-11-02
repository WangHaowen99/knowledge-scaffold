"""包自带界面（ADR-0055）：静态检查对 ui/ 的放行与拦截、上传包界面静态资源的隔离响应头与票据、
视图里编辑器与卡片声明的解析、对话里通用产物卡的形状。上传包样例是测试夹具里合成的 sticky-notes。"""

from __future__ import annotations

import io
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from app.kernel.artifacts import cards, registry as artifact_registry
from app.main import app
from app.services import auth
from app.services.packs import builtin, checks, lockfile, ui as pack_ui
from app.services.packs.repository import write_checksums
from app.services.workspace import WorkspaceManager
from tests.pack_helpers import make_assets, make_pack

FIXTURE = Path(__file__).parent / "fixtures" / "packs" / "sticky-notes"
NOTE = "sticky-notes.note"


def sample_pack(base: Path, *, extra: dict[str, str] | None = None, drop: tuple[str, ...] = ()) -> Path:
    """把夹具复制一份、按需加减文件，补上校验和。"""
    target = Path(base) / "sticky-notes"
    shutil.copytree(FIXTURE, target)
    for rel in drop:
        (target / rel).unlink()
    for rel, content in (extra or {}).items():
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        (target / rel).write_text(content, encoding="utf-8")
    write_checksums(target)
    return target


def zipped(directory: Path) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        for path in sorted(directory.rglob("*")):
            if path.is_file():
                zf.write(path, path.relative_to(directory).as_posix())
    return buffer.getvalue()


def _result(items: list[dict], check_id: str) -> tuple[str, str]:
    item = next(item for item in items if item["id"] == check_id)
    return item["result"], item["detail"]


# ---- 静态检查 ----


def test_upload_pack_with_built_ui_passes(tmp_path):
    manifest, items = checks.run(sample_pack(tmp_path), tenant=True)
    assert manifest is not None and checks.summary(items)["fail"] == 0, items
    assert _result(items, "ui")[0] == "pass" and _result(items, "frontend")[0] == "pass"
    assert manifest.summary()["contributes"]["editors"] == ["note"]


@pytest.mark.parametrize("rel", [
    "ui/note/server.py", "ui/note/run.sh", "ui/note/page.php", "ui/note/App.jsx", "ui/note/main.ts",
    "ui/note/package.json", "ui/note/node_modules/lib/index.js", "ui/note/.htaccess", "ui/note/module.wasm",
])
def test_upload_ui_only_allows_built_static_assets(tmp_path, rel):
    _, items = checks.run(sample_pack(tmp_path, extra={rel: "x\n"}), tenant=True)
    result, detail = _result(items, "ui")
    assert result == "fail" and rel in detail


def test_frontend_code_outside_ui_still_fails(tmp_path):
    _, items = checks.run(sample_pack(tmp_path, extra={"web/app.js": "x\n"}), tenant=True)
    assert _result(items, "frontend")[0] == "fail" and _result(items, "ui")[0] == "pass"


def test_editor_entry_and_view_reference_are_checked(tmp_path):
    _, items = checks.run(sample_pack(tmp_path / "a", drop=("ui/note/index.html",)), tenant=True)
    assert _result(items, "ui")[0] == "fail" and "ui/note/index.html" in _result(items, "ui")[1]
    view = "artifact: sticky-notes.note\ndetail: {editor: missing}\n"
    _, items = checks.run(sample_pack(tmp_path / "b", extra={"views/note.yaml": view}), tenant=True)
    assert _result(items, "ui")[0] == "fail" and "missing" in _result(items, "ui")[1]


def test_symlink_and_traversal_in_ui_are_rejected(tmp_path):
    source = sample_pack(tmp_path)
    (source / "ui" / "note" / "link.js").symlink_to(source / "manifest.yaml")
    _, items = checks.run(source, tenant=True)
    assert _result(items, "symlinks")[0] == "fail"


def test_upload_zip_with_traversal_is_refused(env):
    client, *_ = env
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        zf.writestr("manifest.yaml", "name: x\n")
        zf.writestr("ui/../../evil.js", "x")
    files = {"file": ("pack.zip", buffer.getvalue(), "application/zip")}
    response = client.post("/api/v1/admin/packs/uploads", files=files)
    assert response.status_code == 400 and response.json()["details"]["reason"] == "path_traversal"


def test_official_pack_ui_is_platform_source(tmp_path):
    """官方包（镜像内置）的 ui/ 是随平台编译的源码，入口 index.jsx；上传包同样内容判失败。"""
    files = {"commands/viz-run.md": "运行\n", "ui/board/index.jsx": "export default function B() { return null; }\n",
             "ui/board/model.js": "export const x = 1;\n"}
    contributes = {"editors": [{"id": "board", "title": "看板"}]}
    official = make_pack(tmp_path / "o", "viz", "0.1.0", files=files, contributes=contributes)
    _, items = checks.run(official, official=True)
    assert _result(items, "ui")[0] == "pass" and _result(items, "frontend")[0] == "pass"
    _, items = checks.run(make_pack(tmp_path / "t", "viz", "0.1.0", files=files, contributes=contributes, tenant=True),
                          tenant=True)
    assert _result(items, "ui")[0] == "fail"


# ---- 上传、启用、打开 ----


@pytest.fixture()
def env(tmp_path: Path, monkeypatch):
    builtin.clear_cache()
    pack_ui.revoke_all()
    manager = WorkspaceManager(tmp_path / "data", make_assets(tmp_path))
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    project_id = client.post("/api/v1/projects", json={"name": "便签", "goal": "合成场景：图书借阅建模"}).json()["id"]
    yield client, manager, project_id, tmp_path
    builtin.clear_cache()
    pack_ui.revoke_all()


def _publish_and_enable(client, tmp_path, project_id) -> None:
    accepted = client.post("/api/v1/admin/packs/uploads",
                           files={"file": ("pack.zip", zipped(sample_pack(tmp_path / "src")), "application/zip")})
    assert accepted.status_code == 202, accepted.text
    upload_id = accepted.json()["id"]
    detail = client.get(f"/api/v1/admin/packs/uploads/{upload_id}").json()
    assert detail["state"] == "passed", detail["checks"]
    assert client.post(f"/api/v1/admin/packs/uploads/{upload_id}/publish", json={"channel": "stable"}).status_code == 200
    enabled = client.post(f"/api/v1/projects/{project_id}/packs", json={"name": "sticky-notes"})
    assert enabled.status_code == 201, enabled.text


def _write_note(manager, project_id: str, key: str, body: str, *, color: str = "yellow") -> Path:
    root = manager.project_root(project_id)
    path = root / "ext" / "sticky-notes" / "notes" / f"{key}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"---\ntitle: {key} 便签\ncolor: {color}\ntopic: 借阅规则\n---\n\n## 正文\n\n{body}\n", encoding="utf-8")
    return path


def _commit(root: Path, message: str) -> str:
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-q", "-m", message],
                   cwd=root, check=True, capture_output=True)
    return subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()


def test_artifact_types_carry_editor_and_card_declaration(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    types = {item["type"]: item for item in client.get(f"/api/v1/projects/{project_id}/artifact-types").json()["items"]}
    view = types[NOTE]["view"]
    assert view["card"] == {"title": "title", "subtitle": "topic", "chips": ["color"]}
    editor = view["detail"]["editor"]
    assert editor["mode"] == "iframe" and editor["id"] == "note" and editor["pack"] == "sticky-notes"
    assert editor["entry"] == "ui/note/index.html" and editor["ui_api"] == ">=1.0 <2.0"


def test_ui_is_not_assembled_for_the_agent(env):
    client, manager, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    plan = manager.packs.assembler.plan(lockfile.read(manager.project_root(project_id)))
    target = manager.packs.assembler.build(plan)
    assert (target / "commands" / "sticky-notes-tidy.md").is_file()
    assert not (target / "ui").exists()


def test_ui_session_serves_static_files_with_isolation_headers(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    opened = client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/ui-sessions", json={"editor": "note"})
    assert opened.status_code == 201, opened.text
    body = opened.json()
    assert body["ui_api"] == "1.0" and body["url"].startswith("/api/v1/pack-ui/") and body["url"].endswith("/note/index.html")
    page = client.get(body["url"])
    assert page.status_code == 200 and page.headers["content-type"].startswith("text/html")
    csp = page.headers["content-security-policy"]
    assert csp.startswith("sandbox allow-scripts") and "allow-same-origin" not in csp
    assert "connect-src 'none'" in csp and "frame-ancestors 'self'" in csp and "default-src 'none'" in csp
    assert page.headers["x-content-type-options"] == "nosniff" and page.headers["referrer-policy"] == "no-referrer"
    base = body["url"].rsplit("/", 1)[0]
    script = client.get(f"{base}/editor.js")
    assert script.status_code == 200 and script.headers["content-type"].startswith("text/javascript")
    assert "sandbox allow-scripts" in script.headers["content-security-policy"]
    # 票据不是登录态：拿它调别的接口没用；路径越界、票据不对都当不存在
    ticket = body["url"].split("/")[4]
    assert client.get(f"/api/v1/pack-ui/{ticket}/../manifest.yaml").status_code == 404
    assert client.get(f"/api/v1/pack-ui/{ticket}/note/%2e%2e/%2e%2e/manifest.yaml").status_code == 404
    assert client.get(f"/api/v1/pack-ui/not-a-ticket/note/index.html").status_code == 404


def test_ui_static_files_need_no_login_but_sessions_do(env, monkeypatch):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    url = client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/ui-sessions", json={"editor": "note"}).json()["url"]
    monkeypatch.setattr(auth, "ENABLED", True)
    assert client.get(url).status_code == 200
    refused = client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/ui-sessions", json={"editor": "note"})
    assert refused.status_code == 401


def test_disabling_the_pack_closes_the_editor(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    url = client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/ui-sessions", json={"editor": "note"}).json()["url"]
    assert client.get(url).status_code == 200
    assert client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/disable").status_code == 200
    gone = client.get(url)
    assert gone.status_code == 404 and gone.json()["code"] == "pack_not_enabled"
    again = client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/ui-sessions", json={"editor": "note"})
    assert again.status_code == 409 and again.json()["code"] == "pack_not_enabled"
    types = client.get(f"/api/v1/projects/{project_id}/artifact-types").json()["items"]
    assert NOTE not in {item["type"] for item in types}


def test_ui_session_rejects_unknown_editor_and_unknown_pack(env):
    client, _, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    missing = client.post(f"/api/v1/projects/{project_id}/packs/sticky-notes/ui-sessions", json={"editor": "nope"})
    assert missing.status_code == 404
    other = client.post(f"/api/v1/projects/{project_id}/packs/nope/ui-sessions", json={"editor": "note"})
    assert other.status_code == 409 and other.json()["code"] == "pack_not_enabled"


def test_save_from_editor_goes_through_write_path(env):
    """窄接口的保存就是 AR-05：走写入事务（提交带 web 来源），命中写入范围时如实 423。"""
    client, manager, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    root = manager.project_root(project_id)
    _write_note(manager, project_id, "n1", "第一版")
    _commit(root, "合成：便签")
    detail = client.get(f"/api/v1/projects/{project_id}/artifacts/{NOTE}/n1").json()
    saved = client.put(f"/api/v1/projects/{project_id}/artifacts/{NOTE}/n1",
                       json={"base_revision": detail["revision"], "sections": {"正文": "第二版"}, "summary": "在便签编辑器里修改"})
    assert saved.status_code == 200, saved.text
    log = subprocess.run(["git", "log", "-1", "--format=%B"], cwd=root, capture_output=True, text=True).stdout
    assert "Onto-Source: web" in log
    from app.kernel.artifacts import service as artifact_service

    lock = {"state": "assistant_running", "artifacts": [{"type": NOTE, "key": "n1"}], "paths": []}
    artifact_service.register_lock_provider(lambda _root: lock)
    try:
        locked = client.put(f"/api/v1/projects/{project_id}/artifacts/{NOTE}/n1",
                            json={"base_revision": saved.json()["artifact"]["revision"], "sections": {"正文": "第三版"}})
    finally:
        artifact_service.register_lock_provider(None)
    assert locked.status_code == 423 and locked.json()["code"] == "write_scope_locked"


# ---- 通用产物卡 ----


def test_round_cards_follow_card_declaration(env):
    client, manager, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    root = manager.project_root(project_id)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    _write_note(manager, project_id, "n1", "第一版", color="blue")
    head = _commit(root, "合成：一轮新增便签")
    result = cards.round_cards(manager, project_id, {"added": ["ext/sticky-notes/notes/n1.md"], "modified": [], "deleted": []},
                               base=base, seed=head)
    assert len(result) == 1
    card = result[0]
    assert card["card_type"] == "artifact" and card["card_id"].startswith("ac-")
    assert card["eyebrow"] == "便签 · 本轮新增" and card["title"] == "n1 便签" and card["subtitle"] == "借阅规则"
    assert card["artifact"] == {"type": NOTE, "key": "n1", "title": "n1 便签"}
    assert card["open"] == {"kind": "dialog", "target": "artifact", "ref": card["artifact"]}
    assert card["plugin"] == {"name": "sticky-notes", "label": "便签"}
    assert card["chips"] == [{"field": "color", "label": "颜色", "value": "blue", "text": "蓝色", "tone": "blue"}]
    assert card["fields"] == [] and card["component"] is None and "locked" not in card and card["change"] == "add"
    assert card["detail"]["kind"] == "editor" and card["detail"]["editor"]["mode"] == "iframe"
    # 同一轮同一项产物的卡片 ID 稳定（卡片更新按 ID 整体替换）
    again = cards.round_cards(manager, project_id, {"modified": ["ext/sticky-notes/notes/n1.md"]}, seed=head)
    assert again[0]["card_id"] == card["card_id"] and again[0]["change"] == "mod"
    assert cards.round_cards(manager, project_id, {"deleted": ["ext/sticky-notes/notes/n1.md"]}, seed=head) == []


def test_card_without_declaration_and_flowchart_component(tmp_path):
    """没写 card 的类型按缺省（标题 + 状态字段）；card.component 认得的平台组件带上它要的数据。"""
    data = {"type": "viz.board", "path": "ext/viz/boards/*.json", "format": "json",
            "schema": {"fields": [{"name": "title", "type": "text"},
                                  {"name": "state", "type": "enum", "enum": [{"value": "draft", "label": "草稿", "tone": "gray"}]}]},
            "status": {"field": "state", "map": {"draft": "草稿"}}}
    atype = artifact_registry.parse_registration(data, pack="viz", pack_version="0.1.0", official=False)
    assert atype.view["card"] == {"title": "title", "chips": ["state"]}
    assert atype.view["detail"]["layout"] == "sections" and "editor" not in atype.view["detail"]
    card = cards.card_for(atype, tmp_path, {"key": "b1", "title": "看板一", "rel": "ext/viz/boards/b1.json",
                                            "fields": {"title": "看板一", "state": "draft"}}, change="mod")
    assert card["detail"] == {"kind": "generic", "layout": "sections"}
    assert card["chips"][0]["text"] == "草稿"
    declared = {"artifact": "viz.board", "card": {"component": "flowchart", "title": "title", "chips": ["nope"]},
                "detail": {"component": "flowchart", "editor": "board"}}
    atype = artifact_registry.parse_registration(data, pack="viz", pack_version="0.1.0", official=True, view=declared,
                                                 editors={"board": {"id": "board", "title": "看板", "ui_api": ""}})
    assert atype.view["card"] == {"title": "title", "chips": [], "component": "flowchart"}
    assert atype.view["detail"]["editor"]["mode"] == "bundled"
    assert atype.view["detail"]["editor"]["entry"] == "ui/board/index.jsx"


def test_md_sections_round_cards_only_changed_items(env):
    """一个文件多项（场景文件）：只给这一轮真变了的那几项出卡。"""
    client, manager, project_id, tmp_path = env
    root = manager.project_root(project_id)
    data = {"type": "core.memo", "path": "ext/core/memos/*.md", "format": "md-sections",
            "schema": {"fields": [{"name": "title", "type": "text"}]}}
    atype = artifact_registry.parse_registration(data, pack="core", pack_version="0.1.0", official=False)
    path = root / "ext" / "core" / "memos" / "m.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("# 备忘\n\n## 一\n\n- **说明**：甲\n\n## 二\n\n- **说明**：乙\n", encoding="utf-8")
    base = _commit(root, "合成：备忘")
    path.write_text("# 备忘\n\n## 一\n\n- **说明**：甲\n\n## 二\n\n- **说明**：乙改\n\n## 三\n\n- **说明**：丙\n", encoding="utf-8")
    head = _commit(root, "合成：改备忘")
    import app.kernel.artifacts.cards as cards_module

    original = cards_module.types_for_project
    cards_module.types_for_project = lambda _manager, _root: [atype]
    try:
        result = cards.round_cards(manager, project_id, {"modified": ["ext/core/memos/m.md"]}, base=base, seed=head)
    finally:
        cards_module.types_for_project = original
    assert [(card["change"], card["artifact"]["key"]) for card in result] == [("add", "m--3"), ("mod", "m--2")]


def test_session_host_broadcasts_artifact_cards_after_a_turn(env):
    """一轮结束：「这一轮改了什么」之后，每个改到的插件产物各一张卡（登记在 app/wiring.py）。"""
    import asyncio
    import types

    from app.services import agent_sessions

    client, manager, project_id, tmp_path = env
    _publish_and_enable(client, tmp_path, project_id)
    root = manager.project_root(project_id)
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=root, capture_output=True, text=True).stdout.strip()
    _write_note(manager, project_id, "n2", "助手写的")
    _commit(root, "合成：助手这一轮")
    sent: list[dict] = []
    session = types.SimpleNamespace(cwd=str(root), round_base_commit=base, project_id=project_id,
                                    broadcast=lambda record, **_: sent.append(record))
    asyncio.run(agent_sessions.manager._emit_workspace_changes(session))
    assert [record["type"] for record in sent] == ["workspace_changes", "card"]
    card = sent[1]["payload"]
    assert card["card_type"] == "artifact" and card["artifact"]["key"] == "n2" and card["change"] == "add"


def test_builtin_diagram_pack_ships_its_flowchart_editor(tmp_path):
    """第一个样例：流程图编辑器迁进图示包 ui/flowchart/，随平台编译；视图选平台的流程图卡组件。"""
    from app import config
    from app.services.packs import registry
    from app.services.packs.repository import PackRepository

    builtin.clear_cache()
    repository = PackRepository(tmp_path / "data")
    versions = builtin.import_packs(repository, Path(config.ASSETS_DIR))
    version = versions["diagram"]
    directory = repository.version_dir("diagram", version)
    assert (directory / "ui" / "flowchart" / "index.jsx").is_file()
    view = registry.PackView("diagram", version, repository.manifest("diagram", version), directory,
                             official=repository.is_builtin("diagram", version))
    atype = next(item for item in artifact_registry.types_for_pack(view) if item.type == "diagram.flowchart")
    assert atype.view["card"]["component"] == "flowchart"
    editor = atype.view["detail"]["editor"]
    assert editor["mode"] == "bundled" and editor["entry"] == "ui/flowchart/index.jsx" and editor["pack"] == "diagram"
    _, items = checks.run(directory, official=True)
    assert _result(items, "ui")[0] == "pass" and _result(items, "frontend")[0] == "pass"
    builtin.clear_cache()
