"""Synthetic project-local MCP contract, permission and persistence regressions."""
import json
from concurrent.futures import ThreadPoolExecutor
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.services.workspace import WorkspaceError, WorkspaceManager, _git
from app.services.onto_mcp import tokens
from app.services.onto_mcp import workspace as mcp
from app.services.onto_mcp import documents as doc
from app.services.onto_mcp.errors import McpError
from app.services.onto_mcp import package_publish
from app.routers import onto_mcp as routes


@pytest.fixture
def env(tmp_path, monkeypatch):
    assets = tmp_path / "assets"
    (assets / "workspace-toolkit/templates").mkdir(parents=True)
    (assets / "workspace-rules.md").write_text("# synthetic rules")
    manager = WorkspaceManager(tmp_path / "data", assets)
    meta = manager.create_project("workspace_demo", owner="operator_a")
    root = manager.project_root(meta["id"])
    monkeypatch.setattr(mcp, "editing_issue", lambda *args: None)
    for name in ("synthetic_a", "synthetic_b"):
        (root / "ontology/object" / (name + ".md")).write_text(
            "# 对象描述\n\nsynthetic description\n\n# 对象属性清单\n\n"
            "| 中文名称 | 英文名称 | 类型 | 描述 | 主键 | 属性别名 | 属性规则 |\n"
            "| --- | --- | --- | --- | --- | --- | --- |\n"
            "| synthetic field | field_a | string | synthetic note | 否 | 无 | 无 |\n\n"
            "# 关联对象\n\n无\n\n# 自定义章节\n\nkeep this text\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "test: synthetic fixture")
    return manager, meta, root


def call(env, name, **args):
    manager, meta, _ = env
    return mcp.execute(manager, meta["id"], name, args, "operator_a")


def test_all_read_tools_and_local_revision(env):
    result = call(env, "list_ontology_objects", limit=1)
    assert result["total"] == 2 and result["next_cursor"]
    assert result["items"][0]["title"] == "synthetic_a"
    second = call(env, "list_ontology_objects", limit=1, cursor=result["next_cursor"])
    assert second["items"][0]["key"] == "synthetic_b"
    value = call(env, "get_ontology_object", object_id="object:synthetic_a")["object"]
    assert value["fields"][0]["key"] == "field_a"
    assert value["description"] == "synthetic description"
    assert call(env, "list_ontology_operations")["total"] == 0
    assert call(env, "list_ontology_rules")["total"] == 0
    with pytest.raises(McpError, match="查询条件"):
        call(env, "list_ontology_objects", cursor=result["next_cursor"], query="changed")
    (env[2] / "ontology/object/synthetic_a.md").write_text("# 对象描述\nchanged")
    with pytest.raises(McpError) as exc:
        call(env, "get_ontology_object", object_id="object:synthetic_a", revision=result["revision"])
    assert exc.value.code == "REVISION_CONFLICT"


def test_object_relation_table_preserves_join_fields(env):
    manager, meta, root = env
    path = root / "ontology/object/synthetic_a.md"
    path.write_text(path.read_text() + "\n# 关系\n\n| 关系 | 目标对象 | 说明 | 证据 | 源关联字段 | 目标关联字段 |\n| --- | --- | --- | --- | --- | --- |\n| belongs_to | synthetic_b | synthetic note | synthetic evidence | field_a | field_a |\n", encoding="utf-8")
    value = call(env, "get_ontology_object", object_id="object:synthetic_a")["object"]
    relation = value["relations"][0]
    assert relation["kind"] == "object_relation" and relation["sourceJoinProperty"] == "field_a"
    rev = mcp.revision(root)
    result = call(env, "update_ontology_object", object_id="object:synthetic_a", expected_revision=rev,
                  relation_ops={"update": [{"relationId": relation["id"], "changes": {"description": "changed note"}}]})
    text = path.read_text()
    assert "changed note" in text and "synthetic evidence" in text and result["object"]["relations"][0]["target_key"] == "synthetic_b"
    assert result["object"]["relations"][0]["sourceJoinProperty"] == "field_a"
    assert result["object"]["relations"][0]["targetJoinProperty"] == "field_a"


def test_object_preview_fields_relations_and_atomic_commit(env):
    rev = call(env, "list_ontology_objects")["revision"]
    args = {"object_id": "object:synthetic_a", "expected_revision": rev,
            "metadata": {"title": "synthetic/a", "description": "new description"},
            "field_ops": {"add": [{"key": "field_b", "cnName": "synthetic_b", "type": "string"}],
                          "update": [{"key": "field_a", "changes": {"description": "updated"}}]},
            "relation_ops": {"add": [{"kind": "association", "targetId": "object:synthetic_b", "label": "synthetic relation"}]}}
    preview = call(env, "validate_ontology_object_change", **args)
    assert preview["ok"] and mcp.revision(env[2]) == rev
    result = call(env, "update_ontology_object", **args)
    assert result["revision"] != rev
    assert result["object"]["title"] == "synthetic/a"
    assert len(result["object"]["fields"]) == 2
    path = env[2] / "ontology/object/synthetic_a.md"
    assert "keep this text" in path.read_text()
    assert "report_name:" in path.read_text()
    with pytest.raises(McpError) as exc:
        call(env, "update_ontology_object", **args)
    assert exc.value.code == "REVISION_CONFLICT"
    rid = result["object"]["relations"][0]["id"]
    updated = call(env, "update_ontology_object", object_id="object:synthetic_a", expected_revision=result["revision"],
                   relation_ops={"remove": [rid]}, field_ops={"remove": ["field_b"]})
    assert updated["object"]["relations"] == []
    assert len(updated["object"]["fields"]) == 1


def test_operation_rule_create_update_delete_and_reference_gate(env):
    rev = mcp.revision(env[2])
    args = dict(expected_revision=rev, key="synthetic_rule", title="synthetic rule", content="synthetic condition")
    preview = call(env, "validate_ontology_operation_rule_change", expected_revision=rev, resource_type="rule",
                   action="create", resource_id="rule:synthetic_rule", payload={"title": args["title"], "content": args["content"]})
    assert preview["ok"]
    rule = call(env, "create_ontology_rule", **args)
    assert call(env, "get_ontology_rule", rule_id="rule:synthetic_rule")["rule"]["content"] == "synthetic condition"
    operation = call(env, "create_ontology_operation", expected_revision=rule["revision"], key="synthetic_operation",
                     title="synthetic operation", description="synthetic description", inputs=[], outputs=[],
                     flow=["synthetic step"], related_objects=["synthetic_a"], uses_rules=["synthetic_rule"])
    value = call(env, "get_ontology_operation", operation_id="operation:synthetic_operation")["operation"]
    assert value["flow"] == ["synthetic step"] and value["related_objects"] == ["synthetic_a"]
    with pytest.raises(McpError) as exc:
        call(env, "delete_ontology_rule", expected_revision=operation["revision"], rule_id="rule:synthetic_rule")
    assert exc.value.code == "SUBMISSION_BLOCKED"
    changed = call(env, "update_ontology_operation", expected_revision=operation["revision"], operation_id="operation:synthetic_operation",
                   changes={"uses_rules": [], "flow": ["changed step"], "inputs": [{"param": "input_a", "required": True, "type": "string", "note": "note"}]})
    assert changed["operation"]["inputs"][0]["param"] == "input_a"
    changed = call(env, "update_ontology_rule", expected_revision=changed["revision"], rule_id="rule:synthetic_rule", changes={"content": "changed condition"})
    removed = call(env, "delete_ontology_rule", expected_revision=changed["revision"], rule_id="rule:synthetic_rule")
    call(env, "delete_ontology_operation", expected_revision=removed["revision"], operation_id="operation:synthetic_operation")
    assert call(env, "list_ontology_rules")["total"] == 0


def test_invalid_preview_and_write_do_not_mutate(env):
    rev = mcp.revision(env[2])
    args = dict(object_id="object:synthetic_a", expected_revision=rev, field_ops={"remove": ["unknown"]})
    assert not call(env, "validate_ontology_object_change", **args)["ok"]
    with pytest.raises(McpError):
        call(env, "update_ontology_object", **args)
    assert mcp.revision(env[2]) == rev
    with pytest.raises(McpError):
        call(env, "create_ontology_rule", expected_revision=rev, key="../escape", title="synthetic", content="text")


def test_management_status_is_onto_local_without_modeling(env, monkeypatch):
    manager, meta, _ = env
    monkeypatch.setattr(routes, "manager", lambda: manager)
    monkeypatch.setattr(routes, "access", lambda project_id, request, write=False: manager.get_project(project_id))
    monkeypatch.setattr(routes.config, "MODELING_URL", "https://example.invalid")
    app = FastAPI()
    app.include_router(routes.router)
    request = TestClient(app, base_url="https://testserver")
    response = request.get(f"/api/projects/{meta['id']}/modeling-mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["workspace"]["source"] == "onto" and body["runtime"]["project_id"] == meta["id"]
    assert body["runtime"]["base_url"] == "https://testserver"


@pytest.mark.parametrize("body", [
    {"name": ""}, {"name": "x" * 121}, {"scopes": None}, {"scopes": "ontology.read"},
    {"scopes": []}, {"scopes": ["unknown"]}, {"scopes": [None]},
    {"expires_in_seconds": True}, {"expires_in_seconds": "300"},
    {"expires_in_seconds": 300.0}, {"expires_in_seconds": 299}, {"expires_in_seconds": 2592001},
])
def test_invalid_token_options_do_not_issue_credentials(env, body):
    manager, meta, _ = env
    with pytest.raises(McpError) as exc:
        tokens.issue(manager, meta, {"name": "synthetic", **body}, {"sub": "operator_a"})
    assert exc.value.code == "INVALID_INPUT"
    assert tokens.listing(manager, meta["id"])["tokens"] == []


@pytest.mark.parametrize("ttl", [300, 2592000, None])
def test_token_expiry_boundaries(env, ttl):
    manager, meta, _ = env
    result = tokens.issue(manager, meta, {"name": "synthetic", "expires_in_seconds": ttl},
                          {"sub": "operator_a"})
    assert (result["token_summary"]["expiresAt"] is None) == (ttl is None)
    assert tokens.verify(manager, meta["id"], result["token"], "read")["username"] == "operator_a"


def test_publish_scope_requires_login_and_registers_revocable_delegation(env, monkeypatch):
    manager, meta, _ = env
    calls = []
    from app.services import hub_packages
    monkeypatch.setattr(hub_packages, "register_delegation", lambda *args: calls.append(("register", args)))
    monkeypatch.setattr(hub_packages, "revoke_delegation", lambda *args: calls.append(("revoke", args)))
    with pytest.raises(McpError) as exc:
        tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                     {"sub": "operator_a"})
    assert exc.value.code == "MCP_HUB_IDENTITY_REQUIRED"
    issued = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                          {"sub": "operator_a"}, "synthetic-access", "https://example.com", "operator_a")
    principal = tokens.verify(manager, meta["id"], issued["token"], "change")
    assert principal["hub_issuer"] == "https://example.com" and principal["hub_sub"] == "operator_a"
    tokens.revoke(manager, meta["id"], issued["token_summary"]["id"], "synthetic-access")
    assert [call[0] for call in calls] == ["register", "revoke"]


def test_publish_token_is_not_exposed_when_hub_delegation_registration_fails(env, monkeypatch):
    manager, meta, _ = env
    from app.services import hub_packages
    def unavailable(*args):
        raise WorkspaceError("synthetic Hub outage", 503)
    monkeypatch.setattr(hub_packages, "register_delegation", unavailable)
    with pytest.raises(WorkspaceError):
        tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                     {"sub": "operator_a"}, "synthetic-access", "https://example.com", "operator_a")
    assert tokens.listing(manager, meta["id"])["tokens"] == []


def test_admin_cannot_issue_publish_token_without_project_write_membership(env, monkeypatch):
    manager, meta, _ = env
    from app.services import hub_packages
    calls = []
    monkeypatch.setattr(hub_packages, "register_delegation", lambda *args: calls.append(args))
    with pytest.raises(McpError) as exc:
        tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["package.publish"]},
                     {"sub": "platform_admin", "role": "admin"},
                     "synthetic-access", "https://example.com", "platform_admin")
    assert exc.value.code == "MCP_PROJECT_FORBIDDEN" and exc.value.status == 403
    assert calls == [] and tokens.listing(manager, meta["id"])["tokens"] == []


def test_package_publish_preview_confirm_status_and_stale_guard(env, monkeypatch):
    manager, meta, _ = env
    principal = {"project_id": meta["id"], "token_id": "1" * 32, "username": "operator_a",
                 "scopes": ["package.publish"], "hub_issuer": "https://example.com", "hub_sub": "operator_a"}
    monkeypatch.setattr(package_publish.knowledge_hub, "configured", lambda: True)
    monkeypatch.setattr(manager, "_ensure_solver_publishable", lambda pid: None)
    monkeypatch.setattr(package_publish.knowledge_hub, "source_state",
                        lambda *args: ({}, "a" * 64))
    monkeypatch.setattr(package_publish.hub_packages, "check_delegation", lambda *args: {"active": True})
    monkeypatch.setattr(package_publish.hub_packages, "association", lambda *args: None)
    receipt = {"status": "committed", "version": "0.1.1", "release_id": "release_demo"}
    monkeypatch.setattr(package_publish.hub_packages, "publish", lambda *args, **kwargs: receipt)
    monkeypatch.setattr(package_publish.tokens, "verify_id", lambda *args: principal)
    preview = package_publish.preview(manager, principal, {"request_id": "request_demo"})
    assert preview["ready"] and preview["first_publish"] and preview["expires_at"] > time.time()
    started = package_publish.start(manager, principal, {
        "request_id": "request_demo", "confirmation_token": preview["confirmation_token"]})
    for _ in range(100):
        result = package_publish.status(manager, principal, {"job_id": started["job_id"]})
        if result["status"] != "queued":
            break
        time.sleep(.01)
    assert result["status"] == "committed" and result["receipt"] == receipt
    stale = package_publish._decode(manager, preview["confirmation_token"])
    stale["input_digest"] = "b" * 64
    with pytest.raises(McpError) as exc:
        package_publish.start(manager, principal, {
            "request_id": "request_demo", "confirmation_token": package_publish._encode(manager, stale)})
    assert exc.value.code == "PUBLISH_REQUEST_CONFLICT"


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
    manager, _, _ = env
    path = manager.workspaces_dir.parent / "onto-mcp-confirmation.key"
    path.unlink(missing_ok=True)
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


@pytest.mark.parametrize("value,schema,valid", [
    (True, {"type": "integer"}, False), (1, {"type": "integer"}, True),
    (1.0, {"type": "integer"}, False), (None, {"type": "string"}, False),
    ("x" * 100000, {"type": "string"}, True), ("x" * 100001, {"type": "string"}, False),
    ("synthetic", {"type": "string", "pattern": "synthetic"}, True),
    ("other", {"type": "string", "pattern": "synthetic"}, False),
])
def test_input_validation_boundaries(value, schema, valid):
    from app.services.onto_mcp.definitions import validate_input
    if valid:
        validate_input(value, schema)
    else:
        with pytest.raises(McpError) as exc:
            validate_input(value, schema)
        assert exc.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("value", [None, 1, "", " ", " synthetic", "synthetic ", "a\nb"])
def test_invalid_document_titles(value):
    with pytest.raises(McpError) as exc:
        doc.patch_title("# synthetic\n", value)
    assert exc.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("value", [None, "", " synthetic", "synthetic ", "x" * 81,
                                  "a..b", "a/b", "a\\b", "a#b", "a\x00b"])
def test_invalid_document_keys(value):
    with pytest.raises(McpError) as exc:
        doc.key(value)
    assert exc.value.code == "INVALID_INPUT"


@pytest.mark.parametrize("payload", [
    None, [], "synthetic", {}, {"jsonrpc": "1.0", "method": "ping"},
    {"jsonrpc": "2.0", "method": 1}, {"jsonrpc": "2.0", "method": "ping", "params": []},
    {"jsonrpc": "2.0", "method": "ping", "id": True},
    {"jsonrpc": "2.0", "method": "ping", "id": []},
    {"jsonrpc": "2.0", "method": "ping", "id": {}},
])
def test_invalid_rpc_envelopes(payload):
    import asyncio
    from starlette.requests import Request

    async def receive():
        return {"type": "http.request", "body": json.dumps(payload).encode()}

    with pytest.raises(McpError) as exc:
        asyncio.run(routes.parse_rpc(Request({"type": "http"}, receive)))
    assert exc.value.code == "INVALID_REQUEST"


@pytest.mark.parametrize("cursor,code", [
    ({"revision": "previous"}, "CURSOR_REVISION_MISMATCH"),
    ({"revision": "current", "context": {}}, "CURSOR_CONTEXT_MISMATCH"),
    ({}, "INVALID_CURSOR"), ([], "INVALID_CURSOR"),
])
def test_cursor_error_codes_are_preserved(cursor, code):
    import base64
    encoded = base64.urlsafe_b64encode(json.dumps(cursor).encode()).decode().rstrip("=")
    with pytest.raises(McpError) as exc:
        mcp.list_resources({}, "object", {"cursor": encoded}, "current")
    assert exc.value.code == code


def test_tokens_scope_revocation_acl_and_project_isolation(env):
    manager, meta, root = env
    issued = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["ontology.change.validate"], "expires_in_seconds": None}, {"sub": "operator_a"})
    assert tokens.verify(manager, meta["id"], issued["token"], "change")["scopes"] == ["ontology.change.validate"]
    assert issued["token"] not in json.dumps(tokens.listing(manager, meta["id"]))
    with pytest.raises(McpError):
        tokens.verify(manager, "another-project", issued["token"], "change")
    tokens.revoke(manager, meta["id"], issued["token_summary"]["id"])
    with pytest.raises(McpError):
        tokens.verify(manager, meta["id"], issued["token"], "change")
    issued = tokens.issue(manager, meta, {"name": "synthetic"}, {"sub": "operator_a"})
    manager._update_meta(meta["id"], owner="operator_b", members={})
    with pytest.raises(McpError) as exc:
        tokens.verify(manager, meta["id"], issued["token"], "read")
    assert exc.value.status == 403


def test_http_protocol_and_scope_enforcement(env, monkeypatch):
    manager, meta, root = env
    monkeypatch.setattr(routes, "manager", lambda: manager)
    app = FastAPI()
    app.include_router(routes.transport_router)
    client = TestClient(app)
    raw = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["ontology.read"]}, {"sub": "operator_a"})["token"]
    headers = {"Authorization": "Bearer " + raw}
    url = f"/api/onto-mcp/{meta['id']}/read/mcp"
    def rpc(method, params=None, **kwargs):
        return client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kwargs)
    assert rpc("initialize").json()["result"]["serverInfo"]["name"] == tokens.SERVER["name"]
    assert rpc("server/discover").status_code == 200
    assert len(rpc("tools/list").json()["result"]["tools"]) == 6
    result = rpc("tools/call", {"name": "list_ontology_objects"}).json()["result"]
    assert result["structuredContent"]["total"] == 2 and not result["isError"]
    assert rpc("tools/call", {"name": "delete_ontology_rule"}).status_code == 404
    assert client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"}).status_code == 401



def test_validation_only_token_cannot_apply(env, monkeypatch):
    manager, meta, _ = env
    monkeypatch.setattr(routes, "manager", lambda: manager)
    app = FastAPI()
    app.include_router(routes.transport_router)
    raw = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["ontology.change.validate"]}, {"sub": "operator_a"})["token"]
    with TestClient(app) as client:
        response = client.post(f"/api/onto-mcp/{meta['id']}/change/mcp", headers={"Authorization": "Bearer " + raw},
                               json={"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {
                                   "name": "update_ontology_object", "arguments": {}}})
        assert response.status_code == 403
    assert len(tokens.listing(manager, meta["id"])["tokens"]) == 1


def test_persist_failure_rolls_back_and_keeps_unrelated_changes(env, monkeypatch):
    manager, meta, root = env
    other = root / "ontology/object/synthetic_b.md"
    other.write_text(other.read_text() + "\nsynthetic uncommitted text\n")
    target = root / "ontology/object/synthetic_a.md"
    before = target.read_bytes()
    rev = mcp.revision(root)
    original = mcp._git
    def fail_commit(root, *args):
        if args[0] == "commit":
            raise RuntimeError("synthetic commit failure")
        return original(root, *args)
    monkeypatch.setattr(mcp, "_git", fail_commit)
    with pytest.raises(RuntimeError):
        call(env, "update_ontology_object", object_id="object:synthetic_a", expected_revision=rev,
             metadata={"description": "synthetic changed description"})
    assert target.read_bytes() == before
    assert "synthetic uncommitted text" in other.read_text()
    assert not _git(root, "diff", "--cached", "--name-only").strip()


def test_busy_project_and_symlink_are_blocked(env, monkeypatch, tmp_path):
    manager, meta, root = env
    rev = mcp.revision(root)
    monkeypatch.setattr(mcp, "editing_issue", lambda *args: mcp.issue("PROJECT_EDITING_BUSY", "synthetic busy"))
    preview = call(env, "validate_ontology_object_change", object_id="object:synthetic_a", expected_revision=rev,
                   metadata={"description": "synthetic new"})
    assert not preview["ok"]
    assert preview["issues"][0]["code"] == "PROJECT_EDITING_BUSY"
    external = tmp_path / "synthetic_outside.md"
    external.write_text("synthetic data")
    (root / "ontology/object/synthetic_link.md").symlink_to(external)
    with pytest.raises(McpError) as exc:
        call(env, "list_ontology_objects")
    assert exc.value.code == "UNSAFE_PATH"


def test_real_sse_transport_delivers_rpc_and_rejects_wrong_principal(env, monkeypatch):
    import threading
    import time
    import socket
    import http.client
    from urllib.parse import urlsplit
    import uvicorn

    manager, meta, _ = env
    monkeypatch.setattr(routes, "manager", lambda: manager)
    app = FastAPI()
    app.include_router(routes.transport_router)
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(app, log_level="error", lifespan="off")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True)
    thread.start()
    first = tokens.issue(manager, meta, {"name": "synthetic_first"}, {"sub": "operator_a"})["token"]
    second = tokens.issue(manager, meta, {"name": "synthetic_second"}, {"sub": "operator_a"})["token"]
    try:
        for _ in range(100):
            if server.started:
                break
            time.sleep(.02)
        class Client:
            def __init__(self):
                self.connections = []

            def request(self, method, endpoint, headers, payload=None):
                target = urlsplit(endpoint)
                connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                self.connections.append(connection)
                path = target.path + ("?" + target.query if target.query else "")
                connection.request(method, path, body=json.dumps(payload) if payload else None,
                                   headers={**headers, "Content-Type": "application/json"})
                return connection.getresponse()

            def close(self):
                for connection in self.connections:
                    connection.close()

        client = Client()
        try:
            headers = {"Authorization": "Bearer " + first}
            with client.request("GET", f"/api/onto-mcp/{meta['id']}/read/sse", headers) as response:
                assert response.status == 200
                lines = (line.decode().rstrip("\r\n") for line in iter(response.readline, b""))
                assert next(lines) == "event: endpoint"
                endpoint = next(lines).removeprefix("data: ")
                payload = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "list_ontology_objects"}}
                wrong = client.request("POST", endpoint, {"Authorization": "Bearer " + second}, payload)
                assert wrong.status == 403
                assert client.request("POST", endpoint, headers, payload).status == 202
                for line in lines:
                    if line.startswith("data: "):
                        result = json.loads(line[6:])
                        assert result["id"] == 7
                        assert result["result"]["structuredContent"]["total"] == 2
                        break
        finally:
            client.close()
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()
