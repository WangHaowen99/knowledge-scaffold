"""Synthetic project-local MCP contract, permission and persistence regressions."""
import json
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.services import auth
from app.services.workspace import WorkspaceManager, _git
from app.services.onto_mcp import tokens
from app.services.onto_mcp import workspace as mcp
from app.services.onto_mcp import documents as doc
from app.services.onto_mcp.errors import McpError
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


def _management_app(manager, monkeypatch):
    from app.api import errors

    monkeypatch.setattr(routes, "manager", lambda: manager)
    monkeypatch.setattr(routes, "access", lambda project_id, request: manager.get_project(project_id))
    app = FastAPI()
    app.include_router(routes.router)
    errors.install(app)
    return TestClient(app, base_url="https://testserver")


def test_management_status_is_onto_local_without_modeling(env, monkeypatch):
    manager, meta, _ = env
    request = _management_app(manager, monkeypatch)
    response = request.get(f"/api/v1/projects/{meta['id']}/mcp")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "running" and body["base_url"] == "https://testserver"
    assert body["local_only"] is False and body["pack_tools"] == []
    assert {item["url"] for item in body["endpoints"]} >= {
        f"https://testserver/api/v1/onto-mcp/{meta['id']}/read/mcp",
        f"https://testserver/api/v1/onto-mcp/{meta['id']}/change/sse",
    }
    assert body["token_policy"] == {"min_ttl_s": 300, "default_ttl_s": 86400, "max_ttl_s": 2592000, "supports_non_expiring": True}
    assert [toolset["name"] for toolset in body["toolsets"]] == ["read", "change"]
    # 读工具集：6 个本体读工具 + 插件产物的 artifact_list、artifact_get（契约 M-08）
    assert len(body["toolsets"][0]["tools"]) == 8
    assert {"artifact_list", "artifact_get"} <= set(body["toolsets"][0]["tools"])
    templates = body["client_config_templates"]
    assert "<READ_TOKEN>" in templates["read"] and "<CHANGE_TOKEN>" in templates["change"]
    assert json.loads(templates["full"])["mcpServers"].keys() == {"modeling-read", "modeling-change"}
    # 连接顺序与协议细节不再下发（UI 文案口径 ⑤，契约补充 UI 文案第 15 条）
    assert "recommended_order" not in body and "limits" not in body


def test_management_token_lifecycle_shapes(env, monkeypatch):
    manager, meta, _ = env
    client = _management_app(manager, monkeypatch)
    base = f"/api/v1/projects/{meta['id']}/mcp/tokens"
    created = client.post(base, json={"name": "synthetic", "scopes": ["ontology.read"], "expires_in_s": 30 * 86400})
    assert created.status_code == 201
    body = created.json()
    summary = body["summary"]
    assert body["token"] and summary["subject"] == "operator_a"
    assert set(summary) == {"id", "name", "scopes", "subject", "issued_by", "created_at", "expires_at", "last_used_at", "remaining_text"}
    assert summary["created_at"].endswith("Z") and summary["remaining_text"] == "30 天 · 剩 30 天"
    assert body["token"] in body["client_config"]["read"] and body["client_config"]["change"] == ""

    forever = client.post(base, json={"name": "synthetic_forever", "scopes": ["ontology.change.validate"], "expires_in_s": None}).json()
    assert forever["summary"]["expires_at"] is None and forever["summary"]["remaining_text"] == "永不过期"
    assert forever["client_config"]["read"] == "" and forever["token"] in forever["client_config"]["change"]

    invalid = client.post(base, json={"name": "synthetic", "expires_in_s": 10})
    assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_argument"
    assert "error" not in invalid.json()

    listed = client.get(base).json()["items"]
    assert [item["name"] for item in listed] == ["synthetic_forever", "synthetic"]
    assert body["token"] not in json.dumps(listed)
    assert client.delete(f"{base}/{summary['id']}").status_code == 204
    again = client.delete(f"{base}/{summary['id']}")
    assert again.status_code == 404 and again.json()["code"] == "not_found"
    assert [item["name"] for item in client.get(base).json()["items"]] == ["synthetic_forever"]


def test_remaining_text_formats():
    assert routes.remaining_text(0, None, now=10) == "永不过期"
    assert routes.remaining_text(0, 5, now=10) == "已过期"
    assert routes.remaining_text(0, 30 * 86400, now=9 * 86400) == "30 天 · 剩 21 天"
    assert routes.remaining_text(0, 3600, now=0) == "1 小时 · 剩 1 小时"


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


def test_tokens_scope_revocation_acl_and_project_isolation(env, monkeypatch):
    manager, meta, root = env
    monkeypatch.setattr(auth, "ENABLED", True)
    issued = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["ontology.change.validate"], "expires_in_seconds": None}, {"sub": "operator_a"})
    assert tokens.verify(manager, meta["id"], issued["token"], "change")["scopes"] == ["ontology.change.validate"]
    assert issued["token"] not in json.dumps(tokens.listing(manager, meta["id"]))
    with pytest.raises(McpError):
        tokens.verify(manager, "another-project", issued["token"], "change")
    tokens.revoke(manager, meta["id"], issued["token_summary"]["id"])
    with pytest.raises(McpError):
        tokens.verify(manager, meta["id"], issued["token"], "change")
    issued = tokens.issue(manager, meta, {"name": "synthetic"}, {"sub": "operator_a"})
    manager._update_meta(meta["id"], owner="operator_b")
    with pytest.raises(McpError) as exc:
        tokens.verify(manager, meta["id"], issued["token"], "read")
    assert exc.value.status == 403


def test_http_protocol_and_scope_enforcement(env, monkeypatch):
    manager, meta, root = env
    monkeypatch.setattr(routes, "manager", lambda: manager)
    app = FastAPI()
    app.include_router(routes.transport_router)
    app.include_router(routes.legacy_transport_router)
    client = TestClient(app)
    raw = tokens.issue(manager, meta, {"name": "synthetic", "scopes": ["ontology.read"]}, {"sub": "operator_a"})["token"]
    headers = {"Authorization": "Bearer " + raw}
    # v1 路径与永久保留的旧路径行为完全相同（契约 M-05）
    for url in (f"/api/v1/onto-mcp/{meta['id']}/read/mcp", f"/api/onto-mcp/{meta['id']}/read/mcp"):
        def rpc(method, params=None, **kwargs):
            return client.post(url, headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}}, **kwargs)
        assert rpc("initialize").json()["result"]["serverInfo"]["name"] == tokens.SERVER["name"]
        assert rpc("server/discover").status_code == 200
        assert len(rpc("tools/list").json()["result"]["tools"]) == 8
        result = rpc("tools/call", {"name": "list_ontology_objects"}).json()["result"]
        assert result["structuredContent"]["total"] == 2 and not result["isError"]
        missing = rpc("tools/call", {"name": "delete_ontology_rule"})
        assert missing.status_code == 404 and missing.json()["error"]["data"]["code"] == "MCP_TOOL_NOT_FOUND"
        unauthenticated = client.post(url, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
        assert unauthenticated.status_code == 401
        assert unauthenticated.json()["error"]["data"]["code"] == "MCP_AUTH_REQUIRED"



def test_validation_only_token_cannot_apply(env, monkeypatch):
    manager, meta, _ = env
    monkeypatch.setattr(routes, "manager", lambda: manager)
    app = FastAPI()
    app.include_router(routes.legacy_transport_router)
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
    # 提交经写入事务（kernel/writepath）在临时索引上生成：让生成提交那一步失败
    from app.kernel.writepath import tx as writepath_tx

    original = writepath_tx._git
    def fail_commit(root, *args, **kwargs):
        if args[0] == "commit-tree":
            raise RuntimeError("synthetic commit failure")
        return original(root, *args, **kwargs)
    monkeypatch.setattr(writepath_tx, "_git", fail_commit)
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
    app.include_router(routes.legacy_transport_router)
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
