"""Workspace 水平越权（horizontal authorization）测试。

覆盖：resolve_workspace_id 的访问强制、列表可见性过滤、
owner/members 管理端点权限、以及无 owner 字段旧数据的兼容性。
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

from flask import Flask
import pytest

from api.auth.auth import AuthManager, AuthUser
from api.routes.skills import skills_bp
from api.routes.workspaces import workspaces_bp


FORBIDDEN_BODY = {"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}


def _build_env(tmp_path, monkeypatch):
    """构造带 admin/alice/bob 三个用户的测试 app（workspaces + skills 蓝图）。"""
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))

    from utils import skills as skills_module

    monkeypatch.setattr(skills_module, "_global_registry", None)

    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    admin = auth_manager.authenticate("admin", "admin123")
    assert admin is not None
    admin = auth_manager.change_password("admin", "admin123", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(workspaces_bp)
    app.register_blueprint(skills_bp)

    headers = {
        "admin": {"Authorization": "Bearer {}".format(auth_manager.issue_token(admin))},
    }
    for username in ("alice", "bob"):
        temporary_password = "UserStrong123!"
        auth_manager.create_user(username, temporary_password, role="user")
        user = auth_manager.change_password(username, temporary_password, "ChangedStrong123!")
        headers[username] = {"Authorization": "Bearer {}".format(auth_manager.issue_token(user))}

    return app.test_client(), headers


def _ws_headers(base_headers, workspace_id):
    merged = dict(base_headers)
    merged["X-Workspace-Id"] = workspace_id
    return merged


_UNSET = object()


def _create_workspace_api(client, admin_headers, *, name, workspace_id, owner=_UNSET):
    payload = {"name": name, "id": workspace_id}
    if owner is not _UNSET:
        payload["owner"] = owner
    response = client.post("/agentic/workspaces", json=payload, headers=admin_headers)
    assert response.status_code == 201, response.get_json()
    return response.get_json()


def _setup_shared_and_owned(client, headers):
    """shared：无 owner 的公共空间，先创建自动成为默认；alice-ws：alice 私有空间。"""
    shared = _create_workspace_api(
        client, headers["admin"], name="Shared WS", workspace_id="shared", owner=None
    )
    owned = _create_workspace_api(
        client, headers["admin"], name="Alice WS", workspace_id="alice-ws", owner="alice"
    )
    return shared, owned


def _get_skills(client, base_headers, workspace_id=None):
    headers = _ws_headers(base_headers, workspace_id) if workspace_id else base_headers
    return client.get("/agentic/skills", headers=headers)


# ---------------------------------------------------------------------------
# 越权核心：业务路由被 resolve_workspace_id 拦截
# ---------------------------------------------------------------------------


def test_cross_user_access_denied_on_business_route(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = _get_skills(client, headers["bob"], "alice-ws")

    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY


def test_owner_member_admin_can_access_owned_workspace(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    assert _get_skills(client, headers["alice"], "alice-ws").status_code == 200
    assert _get_skills(client, headers["admin"], "alice-ws").status_code == 200

    response = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob"]},
        headers=headers["admin"],
    )
    assert response.status_code == 200

    assert _get_skills(client, headers["bob"], "alice-ws").status_code == 200


def test_ownerless_workspace_accessible_to_any_user(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    assert _get_skills(client, headers["bob"], "shared").status_code == 200


def test_missing_header_falls_back_to_default_workspace(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    assert _get_skills(client, headers["bob"]).status_code == 200


def test_owned_workspace_becomes_default_stays_accessible(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.post("/agentic/workspaces/alice-ws/default", headers=headers["admin"])
    assert response.status_code == 200

    assert _get_skills(client, headers["bob"], "alice-ws").status_code == 200


# ---------------------------------------------------------------------------
# 列表可见性过滤与字段
# ---------------------------------------------------------------------------


def test_list_workspaces_hides_other_users_private_workspaces(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.get("/agentic/workspaces", headers=headers["bob"])
    assert response.status_code == 200
    body = response.get_json()
    ids = [item["id"] for item in body["workspaces"]]
    assert ids == ["shared"]
    assert body["total"] == 1
    assert body["default_workspace_id"] == "shared"

    shared = body["workspaces"][0]
    assert shared["owner"] is None
    assert shared["members"] == []
    assert shared["role"] is None


def test_list_workspaces_owner_and_admin_visibility(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    alice_body = client.get("/agentic/workspaces", headers=headers["alice"]).get_json()
    alice_ids = {item["id"] for item in alice_body["workspaces"]}
    assert alice_ids == {"shared", "alice-ws"}
    owned = next(item for item in alice_body["workspaces"] if item["id"] == "alice-ws")
    assert owned["owner"] == "alice"
    assert owned["role"] == "admin"

    admin_body = client.get("/agentic/workspaces", headers=headers["admin"]).get_json()
    admin_ids = {item["id"] for item in admin_body["workspaces"]}
    assert admin_ids == {"shared", "alice-ws"}
    admin_owned = next(item for item in admin_body["workspaces"] if item["id"] == "alice-ws")
    assert admin_owned["role"] is None


def test_list_workspaces_member_sees_workspace_with_member_role(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob"]},
        headers=headers["admin"],
    )

    body = client.get("/agentic/workspaces", headers=headers["bob"]).get_json()
    ids = {item["id"] for item in body["workspaces"]}
    assert ids == {"shared", "alice-ws"}
    member_item = next(item for item in body["workspaces"] if item["id"] == "alice-ws")
    assert member_item["role"] == "member"
    assert member_item["members"] == ["bob"]


# ---------------------------------------------------------------------------
# 管理端点：PATCH / PUT members / POST owner 默认值
# ---------------------------------------------------------------------------


def test_owner_can_rename_workspace(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"name": "Alice 新空间"},
        headers=headers["alice"],
    )
    assert response.status_code == 200
    assert response.get_json()["name"] == "Alice 新空间"


def test_non_owner_user_cannot_patch_workspace(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"name": "Bob 改名"},
        headers=headers["bob"],
    )
    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY


def test_owner_cannot_change_owner_field(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"owner": "bob"},
        headers=headers["alice"],
    )
    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY


def test_admin_can_change_owner_field(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"owner": "bob"},
        headers=headers["admin"],
    )
    assert response.status_code == 200
    assert response.get_json()["owner"] == "bob"

    # 换 owner 后原 owner 失去访问权
    assert _get_skills(client, headers["alice"], "alice-ws").status_code == 403


def test_workspace_owner_reference_must_exist_on_create_and_transfer(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    created = client.post(
        "/agentic/workspaces",
        json={"name": "Orphan", "id": "orphan", "owner": "missing-owner"},
        headers=headers["admin"],
    )
    assert created.status_code == 400
    assert created.get_json() == {
        "detail": "工作空间负责人 missing-owner 不存在",
        "code": "WORKSPACE_OWNER_NOT_FOUND",
        "username": "missing-owner",
    }

    transferred = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"owner": "missing-owner"},
        headers=headers["admin"],
    )
    assert transferred.status_code == 400
    assert transferred.get_json()["code"] == "WORKSPACE_OWNER_NOT_FOUND"
    assert client.get(
        "/agentic/workspaces/alice-ws",
        headers=headers["admin"],
    ).get_json()["owner"] == "alice"


def test_put_members_owner_allowed_and_validated(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    # owner 可改，重复与空白会被规整
    response = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob", "bob", " bob "]},
        headers=headers["alice"],
    )
    assert response.status_code == 200
    assert response.get_json()["members"] == ["bob"]

    # 非 owner 普通用户被拒
    response = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": []},
        headers=headers["bob"],
    )
    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY

    # 格式校验：非数组 / 空字符串项 / 超过 64 项
    for bad_payload in (
        {"members": "bob"},
        {"members": [""]},
        {"members": [42]},
        {"members": ["user{}".format(i) for i in range(65)]},
        {},
    ):
        response = client.put(
            "/agentic/workspaces/alice-ws/members",
            json=bad_payload,
            headers=headers["alice"],
        )
        assert response.status_code == 400, bad_payload


def test_workspace_member_identity_must_exist_before_validation_or_save(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    checked = client.post(
        "/agentic/workspaces/alice-ws/members/validate",
        json={"username": "missing-user"},
        headers=headers["alice"],
    )
    assert checked.status_code == 400
    assert checked.get_json() == {
        "detail": "成员用户 missing-user 不存在",
        "code": "WORKSPACE_MEMBER_NOT_FOUND",
        "username": "missing-user",
        "usernames": ["missing-user"],
    }

    saved = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob", "missing-user"]},
        headers=headers["alice"],
    )
    assert saved.status_code == 400
    assert saved.get_json()["code"] == "WORKSPACE_MEMBER_NOT_FOUND"
    assert client.get(
        "/agentic/workspaces/alice-ws",
        headers=headers["admin"],
    ).get_json()["members"] == []

    multiple = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["missing-b", "bob", "missing-a"]},
        headers=headers["alice"],
    )
    assert multiple.status_code == 400
    assert multiple.get_json()["code"] == "WORKSPACE_MEMBERS_NOT_FOUND"
    assert multiple.get_json()["usernames"] == ["missing-b", "missing-a"]


def test_member_save_revalidates_after_successful_add_check(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    checked = client.post(
        "/agentic/workspaces/alice-ws/members/validate",
        json={"username": "bob"},
        headers=headers["alice"],
    )
    assert checked.status_code == 200
    assert checked.get_json() == {"valid": True, "username": "bob"}

    auth_manager = client.application.config["AUTH_MANAGER"]
    auth_manager.delete_user("bob")
    saved = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob"]},
        headers=headers["alice"],
    )

    assert saved.status_code == 400
    assert saved.get_json()["code"] == "WORKSPACE_MEMBER_NOT_FOUND"
    assert client.get(
        "/agentic/workspaces/alice-ws",
        headers=headers["admin"],
    ).get_json()["members"] == []


def test_pending_user_deletion_blocks_member_validation_and_save(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    reconciliation_id = "b" * 32
    reconciliation_path = (
        tmp_path
        / ".onto-runtime"
        / "system"
        / "user-deletion-reconciliation.json"
    )
    reconciliation_path.parent.mkdir(parents=True, exist_ok=True)
    reconciliation_path.write_text(
        json.dumps({
            "version": 1,
            "records": [{
                "id": reconciliation_id,
                "username": "bob",
                "status": "cleanup_pending",
                "identity_deleted": True,
                "created_at": "2026-08-04T00:00:00+00:00",
                "updated_at": "2026-08-04T00:00:00+00:00",
                "attempts": 1,
                "completed_steps": [],
                "errors": {"workspace_members": "pending"},
            }],
        }),
        encoding="utf-8",
    )

    for method, path, payload in (
        (client.post, "/agentic/workspaces/alice-ws/members/validate", {"username": "bob"}),
        (client.put, "/agentic/workspaces/alice-ws/members", {"members": ["bob"]}),
    ):
        response = method(path, json=payload, headers=headers["alice"])
        assert response.status_code == 409
        assert response.get_json()["code"] == "USER_DELETION_RECONCILIATION_PENDING"
        assert response.get_json()["reconciliation_id"] == reconciliation_id

    assert client.get(
        "/agentic/workspaces/alice-ws",
        headers=headers["admin"],
    ).get_json()["members"] == []


@pytest.mark.parametrize(
    ("method_name", "path", "payload"),
    [
        ("put", "/agentic/workspaces/alice-ws/members", []),
        ("put", "/agentic/workspaces/alice-ws/members", "alice"),
        ("put", "/agentic/workspaces/alice-ws/members", 1),
        ("post", "/agentic/workspaces/alice-ws/members/validate", []),
        ("post", "/agentic/workspaces/alice-ws/members/validate", "alice"),
        ("post", "/agentic/workspaces/alice-ws/members/validate", 1),
    ],
)
def test_member_endpoints_reject_non_object_json(
    tmp_path,
    monkeypatch,
    method_name,
    path,
    payload,
):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = getattr(client, method_name)(
        path,
        json=payload,
        headers=headers["alice"],
    )

    assert response.status_code == 400
    assert response.get_json() == {"detail": "请求体必须是 JSON object"}


@pytest.mark.parametrize("payload", [[], ["workspace"], "workspace", 1])
def test_workspace_create_and_patch_reject_non_object_json(tmp_path, monkeypatch, payload):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    responses = [
        client.post(
            "/agentic/workspaces",
            json=payload,
            headers=headers["admin"],
        ),
        client.patch(
            "/agentic/workspaces/alice-ws",
            json=payload,
            headers=headers["admin"],
        ),
    ]

    for response in responses:
        assert response.status_code == 400
        assert response.get_json() == {"detail": "请求体必须是 JSON object"}


@pytest.mark.parametrize("payload", [[], ["workspace"], "workspace", 1])
def test_modeling_shadow_workspace_rejects_non_object_json(tmp_path, monkeypatch, payload):
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)
    client, _ = _build_env(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json=payload,
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )

    assert response.status_code == 400
    assert response.get_json() == {"detail": "请求体必须是 JSON object"}


def test_corrupt_user_deletion_state_returns_503_for_acl_writes(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    reconciliation_path = (
        tmp_path
        / ".onto-runtime"
        / "system"
        / "user-deletion-reconciliation.json"
    )
    reconciliation_path.parent.mkdir(parents=True, exist_ok=True)
    reconciliation_path.write_text("{broken", encoding="utf-8")

    responses = [
        client.post(
            "/agentic/workspaces/alice-ws/members/validate",
            json={"username": "bob"},
            headers=headers["alice"],
        ),
        client.put(
            "/agentic/workspaces/alice-ws/members",
            json={"members": ["bob"]},
            headers=headers["alice"],
        ),
        client.patch(
            "/agentic/workspaces/alice-ws",
            json={"owner": "bob"},
            headers=headers["admin"],
        ),
    ]

    for response in responses:
        assert response.status_code == 503
        assert response.get_json() == {
            "error": "用户删除状态暂不可用，请稍后再试",
            "code": "USER_DELETION_STATE_UNAVAILABLE",
        }
    workspace = client.get(
        "/agentic/workspaces/alice-ws",
        headers=headers["admin"],
    ).get_json()
    assert workspace["owner"] == "alice"
    assert workspace["members"] == []


def test_member_validation_requires_workspace_administration(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.post(
        "/agentic/workspaces/alice-ws/members/validate",
        json={"username": "alice"},
        headers=headers["bob"],
    )

    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY


def test_member_save_fails_closed_when_identity_directory_is_unavailable(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    auth_manager = client.application.config["AUTH_MANAGER"]

    def fail_directory():
        raise ValueError("认证服务暂不可用")

    monkeypatch.setattr(auth_manager, "list_users", fail_directory)
    response = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob"]},
        headers=headers["alice"],
    )

    assert response.status_code == 503
    assert response.get_json()["code"] == "AUTH_DIRECTORY_UNAVAILABLE"
    assert client.get(
        "/agentic/workspaces/alice-ws",
        headers=headers["admin"],
    ).get_json()["members"] == []

    # Removing all references remains possible even during an auth outage.
    cleared = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": []},
        headers=headers["alice"],
    )
    assert cleared.status_code == 200


def test_create_workspace_defaults_owner_to_creator(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/workspaces",
        json={"name": "Created WS", "id": "created-ws"},
        headers=headers["admin"],
    )
    assert response.status_code == 201
    body = response.get_json()
    assert body["owner"] == "admin"
    assert body["members"] == []


def test_regular_user_can_create_only_self_owned_non_default_workspace(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _create_workspace_api(
        client,
        headers["admin"],
        name="Shared WS",
        workspace_id="shared",
        owner=None,
    )

    response = client.post(
        "/agentic/workspaces",
        json={
            "name": "Alice Created WS",
            "id": "alice-created",
            "owner": "bob",
            "set_as_default": True,
        },
        headers=headers["alice"],
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["owner"] == "alice"
    assert body["members"] == []
    assert body["is_default"] is False


def test_regular_user_first_workspace_does_not_become_global_default(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/workspaces",
        json={"name": "Alice First WS", "id": "alice-first"},
        headers=headers["alice"],
    )

    assert response.status_code == 201
    assert response.get_json()["owner"] == "alice"
    assert response.get_json()["is_default"] is False
    listing = client.get("/agentic/workspaces", headers=headers["alice"]).get_json()
    assert listing["default_workspace_id"] is None
    assert listing["workspaces"][0]["is_default"] is False
    bob_listing = client.get("/agentic/workspaces", headers=headers["bob"]).get_json()
    assert bob_listing["workspaces"] == []


def test_workspace_owner_can_delete_owned_workspace_but_member_cannot(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    member_response = client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob"]},
        headers=headers["alice"],
    )
    assert member_response.status_code == 200

    denied = client.delete("/agentic/workspaces/alice-ws", headers=headers["bob"])
    assert denied.status_code == 403
    assert denied.get_json() == FORBIDDEN_BODY

    deleted = client.delete("/agentic/workspaces/alice-ws", headers=headers["alice"])
    assert deleted.status_code == 200
    assert deleted.get_json() == {"success": True, "id": "alice-ws"}
    assert client.get("/agentic/workspaces/alice-ws", headers=headers["admin"]).status_code == 404


def test_workspace_owner_can_delete_owned_default_without_promoting_another_workspace(
    tmp_path,
    monkeypatch,
):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    set_default = client.post(
        "/agentic/workspaces/alice-ws/default",
        headers=headers["admin"],
    )
    assert set_default.status_code == 200

    deleted = client.delete("/agentic/workspaces/alice-ws", headers=headers["alice"])

    assert deleted.status_code == 200
    listing = client.get("/agentic/workspaces", headers=headers["admin"]).get_json()
    assert listing["default_workspace_id"] is None
    assert [workspace["id"] for workspace in listing["workspaces"]] == ["shared"]
    assert listing["workspaces"][0]["is_default"] is False


def test_global_admin_still_cannot_delete_default_workspace_directly(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    response = client.delete("/agentic/workspaces/shared", headers=headers["admin"])

    assert response.status_code == 400
    assert response.get_json() == {"detail": "默认工作空间不可删除，请先切换默认工作空间"}


# ---------------------------------------------------------------------------
# 旧数据兼容：workspace.json 无 owner/members 字段
# ---------------------------------------------------------------------------


def _write_legacy_workspace(tmp_path, workspace_id="legacy-ws"):
    legacy_dir = tmp_path / ".onto-runtime" / "workspaces" / workspace_id
    legacy_dir.mkdir(parents=True)
    (legacy_dir / "workspace.json").write_text(
        json.dumps(
            {
                "id": workspace_id,
                "name": "Legacy WS",
                "description": "旧版无 owner 字段",
                "created_at": "2026-01-01T00:00:00+00:00",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def test_modeling_service_can_ensure_owned_shadow_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)
    client, headers = _build_env(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )

    assert response.status_code == 201
    body = response.get_json()
    assert body["created"] is True
    assert body["workspace"]["id"] == "mw-shadow-pj-demo-123"
    assert body["workspace"]["owner"] == "alice"

    visible = client.get(
        "/agentic/workspaces/mw-shadow-pj-demo-123",
        headers=headers["alice"],
    )
    assert visible.status_code == 200
    assert visible.get_json()["role"] == "admin"


def test_modeling_shadow_workspace_is_not_blocked_by_user_workspace_limit(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)
    monkeypatch.setenv("WORKSPACE_MAX_COUNT", "1")
    client, headers = _build_env(tmp_path, monkeypatch)

    ordinary = client.post(
        "/agentic/workspaces",
        json={"name": "Ordinary", "id": "ordinary"},
        headers=headers["admin"],
    )
    assert ordinary.status_code == 201

    response = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )

    assert response.status_code == 201
    assert response.get_json()["workspace"]["id"] == "mw-shadow-pj-demo-123"

    over_limit = client.post(
        "/agentic/workspaces",
        json={"name": "Second ordinary", "id": "second-ordinary"},
        headers=headers["admin"],
    )
    assert over_limit.status_code == 400
    assert "工作空间数量已达上限" in over_limit.get_json()["detail"]


def test_modeling_shadow_workspace_provisioning_rejects_bad_token_and_owner_takeover(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)
    client, _ = _build_env(tmp_path, monkeypatch)

    rejected = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("x" * 32)},
    )
    assert rejected.status_code == 403

    created = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )
    assert created.status_code == 201

    repeated = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )
    assert repeated.status_code == 200
    assert repeated.get_json()["created"] is False

    takeover = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "bob"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )
    assert takeover.status_code == 409


def test_legacy_workspace_without_owner_fields_still_works(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    _write_legacy_workspace(tmp_path)

    # list：普通用户可见，owner/members 以默认值补齐
    body = client.get("/agentic/workspaces", headers=headers["bob"]).get_json()
    legacy = next(item for item in body["workspaces"] if item["id"] == "legacy-ws")
    assert legacy["owner"] is None
    assert legacy["members"] == []
    assert legacy["role"] is None

    # get：单查正常
    response = client.get("/agentic/workspaces/legacy-ws", headers=headers["bob"])
    assert response.status_code == 200
    assert response.get_json()["owner"] is None

    # 访问：业务路由放行
    assert _get_skills(client, headers["bob"], "legacy-ws").status_code == 200
    assert _get_skills(client, headers["alice"], "legacy-ws").status_code == 200


def test_get_single_workspace_visibility(tmp_path, monkeypatch):
    """单查与列表同一可见性规则：私有空间对非成员连元数据也不暴露。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    # 非成员：403 且不泄漏名称/成员
    response = client.get("/agentic/workspaces/alice-ws", headers=headers["bob"])
    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY

    # owner：200 且 role='admin'
    response = client.get("/agentic/workspaces/alice-ws", headers=headers["alice"])
    assert response.status_code == 200
    assert response.get_json()["role"] == "admin"

    # 全局 admin：200（非 owner 关系 role=None）
    response = client.get("/agentic/workspaces/alice-ws", headers=headers["admin"])
    assert response.status_code == 200
    assert response.get_json()["role"] is None

    # 公共空间：任何登录用户 200
    assert client.get("/agentic/workspaces/shared", headers=headers["bob"]).status_code == 200


def test_member_can_access_but_cannot_patch(tmp_path, monkeypatch):
    """member 能访问业务路由，但改名/描述仍限 owner/admin。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    # alice 把 bob 加为成员
    client.put(
        "/agentic/workspaces/alice-ws/members",
        json={"members": ["bob"]},
        headers=headers["alice"],
    )
    # member bob 能访问业务路由
    assert _get_skills(client, headers["bob"], "alice-ws").status_code == 200
    # 但 member 不是 owner，PATCH 被拒
    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"name": "Bob 改名"},
        headers=headers["bob"],
    )
    assert response.status_code == 403
    assert response.get_json() == FORBIDDEN_BODY


def test_patch_ownerless_workspace_requires_admin(tmp_path, monkeypatch):
    """无 owner 的公共空间 PATCH 仍等效 admin-only：普通用户非 owner → 403。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    # shared 无 owner，普通用户 bob 改名被拒
    response = client.patch(
        "/agentic/workspaces/shared",
        json={"name": "Bob 改公共空间"},
        headers=headers["bob"],
    )
    assert response.status_code == 403
    # admin 可改
    response = client.patch(
        "/agentic/workspaces/shared",
        json={"name": "Admin 改公共空间"},
        headers=headers["admin"],
    )
    assert response.status_code == 200


def test_admin_can_clear_owner_back_to_public(tmp_path, monkeypatch):
    """admin PATCH owner:null 把私有空间清回公共，之后任何用户可访问。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    # 清空前 bob 无权
    assert _get_skills(client, headers["bob"], "alice-ws").status_code == 403
    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"owner": None},
        headers=headers["admin"],
    )
    assert response.status_code == 200
    assert response.get_json()["owner"] is None
    # 清空后 bob 可访问
    assert _get_skills(client, headers["bob"], "alice-ws").status_code == 200


def test_patch_without_owner_key_keeps_owner(tmp_path, monkeypatch):
    """PATCH body 不含 owner 键时不动 owner（不会被误清空）。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    response = client.patch(
        "/agentic/workspaces/alice-ws",
        json={"description": "只改描述"},
        headers=headers["alice"],
    )
    assert response.status_code == 200
    assert response.get_json()["owner"] == "alice"


def test_put_members_on_missing_workspace_404(tmp_path, monkeypatch):
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    response = client.put(
        "/agentic/workspaces/does-not-exist/members",
        json={"members": ["bob"]},
        headers=headers["admin"],
    )
    assert response.status_code == 404


def test_owner_transfer_and_former_owner_update_are_one_serializable_transaction(
    tmp_path,
    monkeypatch,
):
    """Owner transfer wins the lock first; the former owner must re-read and fail."""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)

    import workspace.workspaces as workspace_module

    entered_transfer = threading.Event()
    release_transfer = threading.Event()
    original_update_locked = workspace_module._update_workspace_locked

    def blocking_update_locked(workspace_id, **fields):
        if fields.get("owner") == "carol":
            entered_transfer.set()
            assert release_transfer.wait(timeout=5)
        return original_update_locked(workspace_id, **fields)

    monkeypatch.setattr(
        workspace_module,
        "_update_workspace_locked",
        blocking_update_locked,
    )
    admin = AuthUser(username="admin", role="admin")
    alice = AuthUser(username="alice", role="user")

    with ThreadPoolExecutor(max_workers=2) as executor:
        transfer_future = executor.submit(
            workspace_module.update_workspace_authorized,
            "alice-ws",
            admin,
            owner="carol",
        )
        assert entered_transfer.wait(timeout=5)
        former_owner_future = executor.submit(
            workspace_module.update_workspace_authorized,
            "alice-ws",
            alice,
            name="stale-owner-write",
        )
        assert not former_owner_future.done()
        release_transfer.set()
        assert transfer_future.result(timeout=5)["owner"] == "carol"
        with pytest.raises(workspace_module.WorkspaceAuthorizationError):
            former_owner_future.result(timeout=5)

    workspace = workspace_module.get_workspace("alice-ws")
    assert workspace["owner"] == "carol"
    assert workspace["name"] == "Alice WS"


def test_runtime_config_get_enforces_access(tmp_path, monkeypatch):
    """GET runtime-config 与业务路由同一可见性：非成员读私有空间配置 → 403。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    # admin 给 alice-ws 写入运行配置
    resp = client.patch(
        "/agentic/workspaces/alice-ws/runtime-config",
        json={"acp_suppress_script_patterns": "secret-keyword"},
        headers=headers["admin"],
    )
    assert resp.status_code == 200

    # 非成员 bob 读取被拒（不泄漏配置）
    resp = client.get("/agentic/workspaces/alice-ws/runtime-config", headers=headers["bob"])
    assert resp.status_code == 403
    assert resp.get_json() == FORBIDDEN_BODY

    # owner alice 可读
    resp = client.get("/agentic/workspaces/alice-ws/runtime-config", headers=headers["alice"])
    assert resp.status_code == 200
    assert resp.get_json()["acp_suppress_script_patterns"] == "secret-keyword"

    # 公共空间 shared 任何登录用户可读
    resp = client.get("/agentic/workspaces/shared/runtime-config", headers=headers["bob"])
    assert resp.status_code == 200


def test_runtime_config_patch_is_admin_only(tmp_path, monkeypatch):
    """runtime-config 写侧仍 admin-only：owner 也不能 PATCH（前端 ws.config.write 未开 owner 通道）。"""
    client, headers = _build_env(tmp_path, monkeypatch)
    _setup_shared_and_owned(client, headers)
    # owner alice 尝试写 runtime-config → 403（require_role admin）
    resp = client.patch(
        "/agentic/workspaces/alice-ws/runtime-config",
        json={"acp_suppress_script_patterns": "x"},
        headers=headers["alice"],
    )
    assert resp.status_code == 403
    # admin 可写
    resp = client.patch(
        "/agentic/workspaces/alice-ws/runtime-config",
        json={"acp_suppress_script_patterns": "x"},
        headers=headers["admin"],
    )
    assert resp.status_code == 200
