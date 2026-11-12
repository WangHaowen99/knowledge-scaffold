"""MCP 发布知识包版本（ADR-0098）：发布权限的 token 与 Hub 委托、预览 → 确认 → 后台发布、求解确认、确认令牌与队列上限。

从 develop 的 test_onto_mcp 移植；门禁改读发布检查（PB-01 同口径），不再有固定四阶段。全部为合成数据。
"""
import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from app.services import auth, hub_packages
from app.services.onto_mcp import package_publish, tokens
from app.services.onto_mcp.errors import McpError
from app.services.workspace import WorkspaceError
from tests.publish_fixtures import complete_publish_stages
from tests.test_onto_mcp import env  # noqa: F401  共用合成项目 fixture

PRINCIPAL = {"username": "operator_a", "scopes": ["package.publish"],
             "hub_issuer": "https://example.com", "hub_sub": "operator_a"}


def test_publish_scope_requires_login_and_registers_revocable_delegation(env, monkeypatch):
    manager, meta, _ = env
    calls = []
    monkeypatch.setattr(hub_packages, "register_delegation", lambda *args: calls.append(("register", args)))
    monkeypatch.setattr(hub_packages, "revoke_delegation", lambda *args: calls.append(("revoke", args)))
    with pytest.raises(McpError) as exc:
        tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]}, {"sub": "operator_a"})
    assert exc.value.code == "MCP_HUB_IDENTITY_REQUIRED"
    issued = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                          {"sub": "operator_a"}, "synthetic-access", "https://example.com", "operator_a")
    principal = tokens.verify(manager, meta["id"], issued["token"], "change")
    assert principal["hub_issuer"] == "https://example.com" and principal["hub_sub"] == "operator_a"
    assert tokens.verify_id(manager, meta["id"], principal["token_id"], "package.publish")["token_id"] == principal["token_id"]
    tokens.revoke(manager, meta["id"], issued["token_summary"]["id"], "synthetic-access")
    assert [call[0] for call in calls] == ["register", "revoke"]
    with pytest.raises(McpError) as revoked:
        tokens.verify_id(manager, meta["id"], principal["token_id"], "package.publish")
    assert revoked.value.code == "MCP_AUTH_INVALID"


def test_ordinary_token_has_no_hub_identity_and_cannot_publish(env):
    manager, meta, _ = env
    issued = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["ontology.read", "ontology.change.apply"]},
                          {"sub": "operator_a"}, "synthetic-access", "https://example.com", "operator_a")
    principal = tokens.verify(manager, meta["id"], issued["token"], "change")
    assert (principal["hub_issuer"], principal["hub_sub"]) == ("", "")
    with pytest.raises(McpError) as exc:
        tokens.verify_id(manager, meta["id"], principal["token_id"], "package.publish")
    assert exc.value.code == "MCP_SCOPE_FORBIDDEN"


def test_version_revert_scope_is_accepted_by_both_toolsets(env):
    manager, meta, _ = env
    issued = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["project.version.revert"]}, {"sub": "operator_a"})
    assert tokens.verify(manager, meta["id"], issued["token"], "read")["scopes"] == ["project.version.revert"]
    assert tokens.verify(manager, meta["id"], issued["token"], "change")["scopes"] == ["project.version.revert"]
    catalog = {item["scope"] for item in tokens.status(meta["id"], "http://127.0.0.1")["scope_catalog"]}
    assert {"project.version.revert", "package.publish"} <= catalog


def test_publish_token_is_not_exposed_when_hub_delegation_registration_fails(env, monkeypatch):
    manager, meta, _ = env

    def unavailable(*args):
        raise WorkspaceError("synthetic Hub outage", 503)
    monkeypatch.setattr(hub_packages, "register_delegation", unavailable)
    with pytest.raises(WorkspaceError):
        tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                     {"sub": "operator_a"}, "synthetic-access", "https://example.com", "operator_a")
    assert tokens.listing(manager, meta["id"])["tokens"] == []


def test_non_owner_cannot_issue_publish_token(env, monkeypatch):
    """develop 是「没有写权限的管理员」；本分支只有所有者，非所有者（含管理员）一律不签发。"""
    manager, meta, _ = env
    calls = []
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(hub_packages, "register_delegation", lambda *args: calls.append(args))
    with pytest.raises(McpError) as exc:
        tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                     {"sub": "platform_admin", "role": "admin"},
                     "synthetic-access", "https://example.com", "platform_admin")
    assert exc.value.code == "MCP_PROJECT_FORBIDDEN" and exc.value.status == 403
    assert calls == [] and tokens.listing(manager, meta["id"])["tokens"] == []


def publishable(monkeypatch, solver=None, blockers=()):
    """发布检查的结论（PB-01 同口径）由 _blockers 给出：提交号、求解契约状态与挡发布的门禁。"""
    check = {"commit": "c" * 40, "solver": solver or {"enabled": False}}
    monkeypatch.setattr(package_publish.knowledge_hub, "configured", lambda: True)
    monkeypatch.setattr(package_publish.publish_stages, "readiness", lambda manager, pid: {"ready": True, "missing_stages": []})
    monkeypatch.setattr(package_publish, "_blockers", lambda *args: (check, list(blockers)))
    monkeypatch.setattr(package_publish.knowledge_hub, "source_state", lambda *args, **kwargs: ({}, "a" * 64))
    monkeypatch.setattr(package_publish.hub_packages, "check_delegation", lambda *args: {"active": True})
    monkeypatch.setattr(package_publish.hub_packages, "association", lambda *args: None)


def wait_for_publish(manager, principal, job_id):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = package_publish.status(manager, principal, {"job_id": job_id})
        if result["status"] in {"committed", "failed"}:
            return result
        time.sleep(.01)
    raise AssertionError(result)


def test_package_publish_preview_confirm_status_and_stale_guard(env, monkeypatch):
    manager, meta, _ = env
    principal = {**PRINCIPAL, "project_id": meta["id"], "token_id": "1" * 32}
    publishable(monkeypatch)
    receipt = {"status": "committed", "version": "0.1.1", "release_id": "release_demo"}
    published = []
    monkeypatch.setattr(package_publish.hub_packages, "publish",
                        lambda *args, **kwargs: published.append(kwargs) or receipt)
    monkeypatch.setattr(package_publish.tokens, "verify_id", lambda *args: principal)
    preview = package_publish.preview(manager, principal, {"request_id": "request_demo"})
    assert preview["ready"] and preview["first_publish"] and preview["expires_at"] > time.time()
    assert preview["commit"] == "c" * 40
    started = package_publish.start(manager, principal, {
        "request_id": "request_demo", "confirmation_token": preview["confirmation_token"]})
    result = wait_for_publish(manager, principal, started["job_id"])
    assert result["status"] == "committed" and result["receipt"] == receipt
    # 后台发布的就是预览时检查过的那个提交，作者是 token 的主体
    assert published[0]["commit"] == "c" * 40 and published[0]["author"] == "operator_a"
    stale = package_publish._decode(manager, preview["confirmation_token"])
    stale["input_digest"] = "b" * 64
    with pytest.raises(McpError) as exc:
        package_publish.start(manager, principal, {
            "request_id": "request_demo", "confirmation_token": package_publish._encode(manager, stale)})
    assert exc.value.code == "PUBLISH_REQUEST_CONFLICT"


def test_package_publish_preview_reports_blocking_gates(env, monkeypatch):
    """develop 报固定四阶段未完成；插件化结构由发布检查的门禁链挡（数据绑定、求解契约、结构、流程关卡）。"""
    manager, meta, _ = env
    principal = {**PRINCIPAL, "project_id": meta["id"], "token_id": "4" * 32}
    publishable(monkeypatch, blockers=[{"code": "data_binding", "message": "1 个对象和数据表对不上"}])
    result = package_publish.preview(manager, principal, {"request_id": "blocked_demo"})
    assert not result["ready"] and result["blockers"][0]["code"] == "data_binding"
    assert "confirmation_token" not in result


def test_package_publish_preview_reports_page_required_stages(env, monkeypatch):
    manager, meta, _ = env
    principal = {"project_id": meta["id"], "token_id": "4" * 32, "username": "operator_a",
                 "scopes": ["package.publish"], "hub_issuer": "https://example.com", "hub_sub": "operator_a"}
    monkeypatch.setattr(package_publish.knowledge_hub, "configured", lambda: True)
    monkeypatch.setattr(package_publish.publish_stages, "readiness", lambda manager, pid: {
        "ready": False,
        "missing_stages": [{"key": "operation", "title": "操作提取"},
                           {"key": "validate", "title": "质量检查"}],
    })
    result = package_publish.preview(manager, principal, {"request_id": "stage_preview_demo"})
    assert not result["ready"]
    assert result["blockers"][0]["code"] == "modeling_stages_incomplete"
    assert result["blockers"][0]["missing_stages"] == [
        {"key": "operation", "title": "操作提取"},
        {"key": "validate", "title": "质量检查"},
    ]
    assert "操作提取、质量检查" in result["display"]["text"]


def test_package_publish_preview_checks_the_real_stages(env, monkeypatch):
    """不打桩：合成项目只有对象，缺资料分析、操作提取、质量检查。"""
    manager, meta, _ = env
    principal = {**PRINCIPAL, "project_id": meta["id"], "token_id": "7" * 32}
    monkeypatch.setattr(package_publish.knowledge_hub, "configured", lambda: True)
    result = package_publish.preview(manager, principal, {"request_id": "stage_real_demo"})
    assert [item["key"] for item in result["blockers"][0]["missing_stages"]] == ["research", "operation", "validate"]
    assert result["display"]["text"] == "建模未完成，请先完成：资料分析、操作提取、质量检查"


def test_package_publish_preview_uses_the_real_publish_check(env, monkeypatch):
    manager, meta, root = env
    complete_publish_stages(root)
    principal = {**PRINCIPAL, "project_id": meta["id"], "token_id": "6" * 32}
    monkeypatch.setattr(package_publish.knowledge_hub, "configured", lambda: True)
    monkeypatch.setattr(package_publish.hub_packages, "check_delegation", lambda *args: {"active": True})
    result = package_publish.preview(manager, principal, {"request_id": "real_check_demo"})
    assert result["ready"], result
    _, digest = package_publish.knowledge_hub.source_state(manager, meta["id"], False)
    assert result["source_digest"] == digest


def solver_publish_env(env, monkeypatch, verified):
    """求解契约要验证的可发布项目；hub_packages.publish 记下它收到的关键字参数。"""
    manager, meta, _ = env
    principal = {**PRINCIPAL, "project_id": meta["id"], "token_id": "5" * 32}
    revision = "sha256:" + "c" * 64
    publishable(monkeypatch, solver={"enabled": True, "revision": revision, "requires_validation_after_publish": True})
    release = {"release_id": "release_verified", "version": "0.1.1"} if verified else None
    monkeypatch.setattr(package_publish, "_verified_release", lambda *args: release)
    published = []
    receipt = {"status": "committed", "version": "0.1.2", "release_id": "release_demo"}
    monkeypatch.setattr(package_publish.hub_packages, "publish",
                        lambda *args, **kwargs: published.append(kwargs) or receipt)
    monkeypatch.setattr(package_publish.tokens, "verify_id", lambda *args: principal)
    return manager, principal, published


def test_mcp_publish_requires_the_unverified_solver_acknowledgement(env, monkeypatch):
    manager, principal, published = solver_publish_env(env, monkeypatch, verified=False)
    preview = package_publish.preview(manager, principal, {"request_id": "solver_demo"})
    assert preview["solver"]["requires_acknowledgement"] is True
    assert "solver_unverified_acknowledged" in preview["display"]["text"]
    request = {"request_id": "solver_demo", "confirmation_token": preview["confirmation_token"]}
    with pytest.raises(McpError) as exc:
        package_publish.start(manager, principal, request)
    assert exc.value.code == "SOLVER_VALIDATION_ACKNOWLEDGEMENT_REQUIRED" and exc.value.status == 409
    assert published == []
    started = package_publish.start(manager, principal, {**request, "solver_unverified_acknowledged": True})
    assert wait_for_publish(manager, principal, started["job_id"])["status"] == "committed"
    assert published[0]["acknowledged_by"] == "operator_a" and published[0]["verified_revision"] == ""
    # 重查同一个请求不再要求确认
    assert package_publish.start(manager, principal, request)["status"] == "committed"


def test_mcp_publish_of_a_verified_solver_revision_needs_no_acknowledgement(env, monkeypatch):
    manager, principal, published = solver_publish_env(env, monkeypatch, verified=True)
    preview = package_publish.preview(manager, principal, {"request_id": "solver_verified_demo"})
    assert preview["solver"]["requires_acknowledgement"] is False
    assert "0.1.1" in preview["display"]["text"]
    started = package_publish.start(manager, principal, {
        "request_id": "solver_verified_demo", "confirmation_token": preview["confirmation_token"]})
    assert wait_for_publish(manager, principal, started["job_id"])["status"] == "committed"
    assert published[0]["acknowledged_by"] == "" and published[0]["verified_revision"] == "sha256:" + "c" * 64


def test_publish_tool_accepts_the_solver_acknowledgement_argument():
    from app.services.onto_mcp.definitions import TOOLS

    tool = TOOLS["publish_package_version"]
    assert tool["inputSchema"]["properties"]["solver_unverified_acknowledged"] == {"type": "boolean"}
    assert "solver_unverified_acknowledged" not in tool["inputSchema"]["required"]
    assert {TOOLS[name]["scope"] for name in ("preview_package_publish", "publish_package_version",
                                              "get_package_publish_status")} == {"package.publish"}


def test_package_publish_confirmation_is_bound_to_token_and_expiry(env):
    manager, meta, _ = env
    payload = {"project_id": meta["id"], "token_id": "1" * 32, "request_id": "request_demo",
               "allow_unbound": False, "input_digest": "a" * 64,
               "base_release_id": None, "base_digest": None, "exp": time.time() - 1}
    expired = package_publish._encode(manager, payload)
    with pytest.raises(McpError) as exc:
        package_publish._decode(manager, expired)
    assert exc.value.code == "PUBLISH_CONFIRMATION_EXPIRED"
    payload["exp"] = time.time() + 300
    token = package_publish._encode(manager, payload)
    principal = {"project_id": meta["id"], "token_id": "2" * 32}
    with pytest.raises(McpError) as exc:
        package_publish.start(manager, principal, {"request_id": "request_demo", "confirmation_token": token})
    assert exc.value.code == "PUBLISH_CONFIRMATION_MISMATCH"


def test_package_publish_confirmation_secret_is_stable_during_first_use_concurrency(env):
    from app.governance import secrets as secret_store

    manager, _, _ = env
    secret_store.path(package_publish.CONFIRMATION_KEY, manager.workspaces_dir.parent).unlink(missing_ok=True)
    with ThreadPoolExecutor(max_workers=16) as pool:
        values = list(pool.map(lambda _: package_publish._secret(manager), range(64)))
    assert len(set(values)) == 1 and len(values[0]) == 32
    token = package_publish._encode(manager, {"exp": time.time() + 300, "marker": "synthetic"})
    assert package_publish._decode(manager, token)["marker"] == "synthetic"


def test_package_publish_queue_limits_are_checked_atomically(env, monkeypatch):
    manager, meta, _ = env
    principal = {"project_id": meta["id"], "token_id": "3" * 32}
    monkeypatch.setattr(package_publish._executor, "submit", lambda *args: None)
    monkeypatch.setattr(package_publish, "MAX_ACTIVE_JOBS_PER_TOKEN", 1)

    def token(request_id):
        return package_publish._encode(manager, {
            "project_id": meta["id"], "token_id": principal["token_id"], "request_id": request_id,
            "allow_unbound": False, "input_digest": "a" * 64, "base_release_id": None,
            "base_digest": None, "exp": time.time() + 300})
    package_publish.start(manager, principal, {"request_id": "request_one", "confirmation_token": token("request_one")})
    with pytest.raises(McpError) as exc:
        package_publish.start(manager, principal, {"request_id": "request_two", "confirmation_token": token("request_two")})
    assert exc.value.code == "PUBLISH_QUEUE_LIMIT" and exc.value.status == 429


def test_issue_route_registers_hub_delegation_for_publish_scope(env, monkeypatch):
    """M-03：选了 package.publish 时，用这次请求的登录令牌与统一账号身份在 Hub 登记委托，签发成功。"""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app

    manager, meta, _ = env
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token",
                        lambda token: {"sub": "operator_a", "iss": "https://example.com"} if token == "synthetic-access" else None)
    registered = []
    monkeypatch.setattr(hub_packages, "register_delegation", lambda *args: registered.append(args))
    client = TestClient(app, headers={"Authorization": "Bearer synthetic-access"})
    url = f"/api/v1/projects/{meta['id']}/mcp/tokens"
    created = client.post(url, json={"name": "synthetic", "scopes": ["package.publish"]})
    assert created.status_code == 201, created.text
    assert created.json()["summary"]["scopes"] == ["package.publish"]
    [(project_id, token_id, hub_token, issuer, sub, _expires)] = registered
    assert (project_id, hub_token, issuer, sub) == (meta["id"], "synthetic-access", "https://example.com", "operator_a")
    principal = tokens.verify(manager, meta["id"], created.json()["token"], "change")
    assert principal["token_id"] == token_id and principal["hub_sub"] == "operator_a"
    catalog = client.get(f"/api/v1/projects/{meta['id']}/mcp").json()
    assert "package.publish" in {item["scope"] for item in catalog["scope_catalog"]}


def test_issue_route_falls_back_to_configured_issuer(env, monkeypatch):
    """登录令牌里没有 iss 时取本服务配置的统一账号地址，与网页发布的 Hub 主体同一口径。"""
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app

    manager, meta, _ = env
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "ISSUER", "https://auth.example.com")
    monkeypatch.setattr(auth, "verify_access_token", lambda token: {"sub": "operator_a"} if token else None)
    registered = []
    monkeypatch.setattr(hub_packages, "register_delegation", lambda *args: registered.append(args))
    client = TestClient(app, headers={"Authorization": "Bearer synthetic-access"})
    response = client.post(f"/api/v1/projects/{meta['id']}/mcp/tokens", json={"name": "synthetic", "scopes": ["package.publish"]})
    assert response.status_code == 201, response.text
    assert registered[0][3] == "https://auth.example.com"
