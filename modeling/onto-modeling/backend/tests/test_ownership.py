"""单人所有权（A11、A12）：所有者 / 管理员 / 其他用户 / 无主项目 / 鉴权关闭 × 列表、读、写、
管理操作、MCP token 签发与使用、指定所有者与审计，以及启动迁移删成员表。

用户名、项目名均为合成数据。
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import config
from app.main import app
from app.routers import admin, projects
from app.services import audit, auth, ownership
from app.services.workspace import META_FILE, WorkspaceManager

OWNER, OTHER, ADMIN = "operator_a", "operator_b", "operator_admin"
TOKENS = {
    "owner-token": {"sub": OWNER, "role": "user"},
    "other-token": {"sub": OTHER, "role": "user"},
    "admin-token": {"sub": ADMIN, "role": "admin"},
}


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "MODELING_URL", "")
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit" / "templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# rules", encoding="utf-8")
    (assets / "resource-template" / "others").mkdir(parents=True)
    instance = WorkspaceManager(tmp_path / "data", assets)
    monkeypatch.setattr(projects, "workspaces", instance)
    monkeypatch.setattr(admin, "workspaces", instance)
    return instance


@pytest.fixture()
def client(manager, monkeypatch) -> TestClient:
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: TOKENS.get(token))
    return TestClient(app)


@pytest.fixture()
def ids(manager) -> dict:
    owned = manager.create_project("project_alpha", owner=OWNER, background="合成背景说明")
    unowned = manager.create_project("project_legacy")
    return {"owned": owned["id"], "unowned": unowned["id"]}


def _h(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


# ---- 列表 ----


def test_list_shows_own_projects_and_metadata_only_to_admin(client, ids):
    mine = client.get("/api/projects", headers=_h("owner-token")).json()["projects"]
    assert [p["id"] for p in mine] == [ids["owned"]]
    assert mine[0]["background"] == "合成背景说明"  # 本人项目给完整信息

    assert client.get("/api/projects", headers=_h("other-token")).json()["projects"] == []

    listed = {p["id"]: p for p in client.get("/api/projects", headers=_h("admin-token")).json()["projects"]}
    assert set(listed) == {ids["owned"], ids["unowned"]}
    for item in listed.values():
        assert set(item) <= set(ownership.META_VIEW_FIELDS)
        assert "background" not in item and "engine" not in item
    assert listed[ids["owned"]]["owner"] == OWNER
    assert listed[ids["unowned"]]["owner"] == ""  # 前端据此标「无主」


def test_project_meta_endpoint(client, ids):
    owned = ids["owned"]
    assert client.get(f"/api/projects/{owned}", headers=_h("owner-token")).json()["background"]
    meta = client.get(f"/api/projects/{owned}", headers=_h("admin-token")).json()
    assert set(meta) <= set(ownership.META_VIEW_FIELDS) and meta["owner"] == OWNER
    denied = client.get(f"/api/projects/{owned}", headers=_h("other-token"))
    assert denied.status_code == 403 and denied.json()["code"] == ownership.CODE_OWNER_ONLY


# ---- 项目内容：读、写、会话、导出、管理操作 ----

CONTENT_REQUESTS = [
    ("get", "/files", None),
    ("get", "/file?path=ontology/object/index.md", None),
    ("get", "/versions", None),
    ("get", "/package/check", None),
    ("get", "/export?scope=workspace", None),
    ("get", "/session/conversations", None),
    ("get", "/modeling-mcp", None),
    ("put", "/file", {"path": "knowledge/note.md", "content": "合成"}),
    ("post", "/clone", {"name": ""}),
    ("patch", "", {"name": "改名后的项目"}),
    ("post", "/archive", {"archived": True}),
    ("delete", "", {"confirmed": True}),
]


def _call(client, method, project_id, suffix, body, token):
    kwargs = {"headers": _h(token)}
    if body is not None:
        kwargs["json"] = body
    return client.request(method.upper(), f"/api/projects/{project_id}{suffix}", **kwargs)


@pytest.mark.parametrize("method, suffix, body", CONTENT_REQUESTS)
@pytest.mark.parametrize("token", ["other-token", "admin-token"])
def test_content_is_owner_only(client, ids, manager, method, suffix, body, token):
    response = _call(client, method, ids["owned"], suffix, body, token)
    assert response.status_code == 403
    assert response.json()["code"] == ownership.CODE_OWNER_ONLY
    meta = manager.get_project(ids["owned"])
    assert (meta["name"], meta.get("archived")) == ("project_alpha", None)


@pytest.mark.parametrize("method, suffix, body", CONTENT_REQUESTS)
@pytest.mark.parametrize("token", ["owner-token", "other-token", "admin-token"])
def test_unowned_project_is_closed_until_owner_assigned(client, ids, method, suffix, body, token):
    response = _call(client, method, ids["unowned"], suffix, body, token)
    assert response.status_code == 403
    assert response.json() == {
        "error": "该项目尚未指定所有者，请联系管理员",
        "code": ownership.CODE_UNOWNED,
    }


def test_owner_reads_writes_and_manages(client, ids, manager):
    owned = ids["owned"]
    assert client.get(f"/api/projects/{owned}/files", headers=_h("owner-token")).status_code == 200
    saved = client.put(
        f"/api/projects/{owned}/file", headers=_h("owner-token"),
        json={"path": "knowledge/note.md", "content": "合成"},
    )
    assert saved.status_code == 200 and saved.json()["commit"]
    assert client.get(f"/api/projects/{owned}/session/conversations", headers=_h("owner-token")).status_code == 200
    renamed = client.patch(f"/api/projects/{owned}", headers=_h("owner-token"), json={"name": "project_beta"})
    assert renamed.status_code == 200 and renamed.json()["name"] == "project_beta"
    assert client.post(
        f"/api/projects/{owned}/archive", headers=_h("owner-token"), json={"archived": True}
    ).json()["archived"] is True


def test_member_routes_are_gone(client, ids):
    owned = ids["owned"]
    assert client.get(f"/api/projects/{owned}/members", headers=_h("owner-token")).status_code == 404
    assert client.put(
        f"/api/projects/{owned}/members", headers=_h("owner-token"), json={"members": {OTHER: "editor"}}
    ).status_code in (404, 405)


# ---- MCP token：签发与使用 ----


def _issue(client, project_id, token):
    return client.post(
        f"/api/projects/{project_id}/modeling-mcp/tokens", headers=_h(token),
        json={"name": "synthetic", "scopes": ["ontology.read"]},
    )


def _mcp_ping(client, project_id, raw):
    return client.post(
        f"/api/onto-mcp/{project_id}/read/mcp", headers={"Authorization": f"Bearer {raw}"},
        json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
    )


def test_only_owner_issues_tokens_and_owner_change_revokes_them(client, ids):
    owned = ids["owned"]
    for token in ("admin-token", "other-token"):  # 管理员不再代发
        assert _issue(client, owned, token).status_code == 403
    issued = _issue(client, owned, "owner-token")
    assert issued.status_code == 201
    assert issued.json()["token_summary"]["subjectUsername"] == OWNER
    raw = issued.json()["token"]
    assert _mcp_ping(client, owned, raw).status_code == 200

    moved = client.put(
        f"/api/admin/projects/{owned}/owner", headers=_h("admin-token"), json={"owner": OTHER}
    )
    assert moved.status_code == 200
    assert _mcp_ping(client, owned, raw).status_code == 403  # 旧所有者的 token 随换人失效
    assert _issue(client, owned, "owner-token").status_code == 403
    assert _issue(client, owned, "other-token").status_code == 201


# ---- 指定所有者与审计 ----


def test_admin_assigns_owner_with_audit(client, ids):
    unowned = ids["unowned"]
    path = f"/api/admin/projects/{unowned}/owner"
    assert client.put(path, headers=_h("owner-token"), json={"owner": OWNER}).status_code == 403
    for bad in ("", "  ", "has space", "x" * 65, "a/b"):
        response = client.put(path, headers=_h("admin-token"), json={"owner": bad})
        assert response.status_code == 400 and response.json()["code"] == "invalid_owner"

    assigned = client.put(path, headers=_h("admin-token"), json={"owner": f" {OTHER} "})
    assert assigned.status_code == 200
    assert assigned.json()["owner"] == OTHER and set(assigned.json()) <= set(ownership.META_VIEW_FIELDS)
    assert client.get(f"/api/projects/{unowned}/files", headers=_h("other-token")).status_code == 200
    assert [p["id"] for p in client.get("/api/projects", headers=_h("other-token")).json()["projects"]] == [unowned]

    # 管理员把所有者改成自己就能读内容：允许，但必须留审计
    client.put(path, headers=_h("admin-token"), json={"owner": ADMIN})
    assert client.get(f"/api/projects/{unowned}/files", headers=_h("admin-token")).status_code == 200
    assert client.get(f"/api/projects/{unowned}/files", headers=_h("other-token")).status_code == 403

    details = [entry["detail"] for entry in audit.read(200) if entry.get("detail", {}).get("action") == "set_owner"]
    assert details == [  # 新的在前
        {"action": "set_owner", "project": unowned, "from": OTHER, "to": ADMIN},
        {"action": "set_owner", "project": unowned, "from": "", "to": OTHER},
    ]
    operators = [entry["user"] for entry in audit.read(200) if entry.get("detail")]
    assert operators == [ADMIN, ADMIN]


def test_set_owner_on_missing_project_is_404(client):
    response = client.put(
        "/api/admin/projects/no-such-project/owner", headers=_h("admin-token"), json={"owner": OWNER}
    )
    assert response.status_code == 404


def test_admin_overview_lists_metadata_only(client, ids):
    overview = client.get("/api/admin/overview", headers=_h("admin-token")).json()
    for item in overview["projects"]:
        assert "background" not in item
        assert {"usage", "size_bytes", "owner", "name", "stages_done"} <= set(item)


# ---- 鉴权关闭：全部放开 ----


def test_auth_disabled_keeps_everything_open(manager, ids, monkeypatch):
    monkeypatch.setattr(auth, "ENABLED", False)
    client = TestClient(app)
    listed = client.get("/api/projects").json()["projects"]
    assert {p["id"] for p in listed} == {ids["owned"], ids["unowned"]}
    assert all("background" in p for p in listed)
    for project_id in ids.values():
        assert client.get(f"/api/projects/{project_id}/files").status_code == 200
        issued = _issue(client, project_id, "")
        assert issued.status_code == 201
        assert _mcp_ping(client, project_id, issued.json()["token"]).status_code == 200


# ---- 启动迁移：删成员表，报告无主项目 ----


def test_startup_migration_drops_members_once_and_reports_unowned(manager, ids, caplog):
    owned_root = manager.project_root(ids["owned"])
    meta = json.loads((owned_root / META_FILE).read_text(encoding="utf-8"))
    meta["members"] = {OTHER: "editor", "operator_c": "viewer"}
    (owned_root / META_FILE).write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    with caplog.at_level("WARNING"):
        assert manager.migrate_single_owner() == [ids["unowned"]]
    assert "members" not in manager.get_project(ids["owned"])
    assert manager.get_project(ids["owned"])["owner"] == OWNER
    [entry] = [e for e in audit.read(200) if e.get("detail", {}).get("action") == "remove_members"]
    assert entry["user"] == "system"
    assert entry["detail"]["members"] == {OTHER: "editor", "operator_c": "viewer"}
    assert "无主项目 1 个" in caplog.text and ids["unowned"] in caplog.text

    # 幂等：再跑不改元数据、不再记审计，仍报告无主项目
    assert manager.migrate_single_owner() == [ids["unowned"]]
    assert len([e for e in audit.read(200) if e.get("detail", {}).get("action") == "remove_members"]) == 1


def test_trash_restore_drops_legacy_members(manager):
    trash = manager.trash_dir() / "legacy-project-1700000000"
    trash.mkdir(parents=True)
    (trash / META_FILE).write_text(
        json.dumps({"id": "legacy-project", "name": "旧项目", "owner": OWNER, "members": {OTHER: "viewer"}}),
        encoding="utf-8",
    )
    restored = manager.restore_trash(trash.name)
    assert "members" not in restored and "members" not in manager.get_project(restored["id"])
