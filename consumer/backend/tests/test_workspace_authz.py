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
