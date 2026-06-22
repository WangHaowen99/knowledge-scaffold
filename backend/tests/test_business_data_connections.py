"""Tests for workspace-scoped business data connections and ontology bindings."""

from __future__ import annotations

import json

from flask import Flask

from agentic.services.auth import AuthManager
from agentic.workspaces import create_workspace, get_workspace_paths, workspace_scope


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _create_workspace_with_ontology(tmp_path, monkeypatch, workspace_id: str = "demo"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    paths = get_workspace_paths(workspace_id)
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)
    ontology_dir = paths.ontologies_dir / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology",
                "id: demo-ontology",
                "name: Demo Ontology",
                "description: Demo ontology for business data binding",
                "version: 1.0.0",
                "language: zh-CN",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return paths


def _build_test_client(tmp_path, monkeypatch):
    from agentic.routes.business_data import business_data_bp
    from agentic.routes.ontologies import ontologies_bp

    _create_workspace_with_ontology(tmp_path, monkeypatch)

    app = Flask(__name__)
    app.config["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "admin123")
    assert user is not None
    user = auth_manager.change_password("admin", "admin123", "RootStrong123!")

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(business_data_bp)
    app.register_blueprint(ontologies_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    return client, {"Authorization": "Bearer {}".format(token)}


def test_resolver_requires_explicit_ontology_binding(tmp_path, monkeypatch):
    from agentic.business_data.bindings_store import set_ontology_data_binding
    from agentic.business_data.connections_store import create_data_connection
    from agentic.business_data.resolver import (
        BusinessDataConnectionError,
        resolve_business_data_connection,
    )

    _create_workspace_with_ontology(tmp_path, monkeypatch)

    with workspace_scope("demo"):
        create_data_connection(
            {
                "id": "example-nl2sql",
                "name": "示例制造 NL2SQL",
                "provider": "nl2sql_http",
                "config": {
                    "url": "https://nl2sql.equipment.local/",
                    "token": "secret-token",
                    "scene": "example",
                    "timeout_seconds": 120,
                },
            }
        )

        try:
            resolve_business_data_connection(
                workspace_id="demo",
                ontology_id="demo-ontology",
            )
        except BusinessDataConnectionError as exc:
            assert "未绑定业务数据源" in str(exc)
        else:
            raise AssertionError("resolver should fail before ontology binding is configured")

        set_ontology_data_binding(
            ontology_id="demo-ontology",
            connection_id="example-nl2sql",
        )
        resolved = resolve_business_data_connection(
            workspace_id="demo",
            ontology_id="demo-ontology",
        )

    assert resolved.id == "example-nl2sql"
    assert resolved.provider == "nl2sql_http"
    assert resolved.config["url"] == "https://nl2sql.equipment.local"
    assert resolved.config["token"] == "secret-token"
    assert resolved.config["scene"] == "example"


def test_data_connection_routes_and_ontology_summary(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "example-nl2sql",
            "name": "示例制造 NL2SQL",
            "provider": "nl2sql_http",
            "config": {
                "url": "https://nl2sql.equipment.local/",
                "token": "secret-token",
                "scene": "example",
            },
            "set_as_default": True,
        },
    )
    assert create_response.status_code == 201
    created = create_response.get_json()
    assert created["connection"]["id"] == "example-nl2sql"
    assert created["connection"]["config"]["url"] == "https://nl2sql.equipment.local"
    assert created["connection"]["config"]["has_token"] is True
    assert "token" not in created["connection"]["config"]
    assert created["default_connection_id"] == "example-nl2sql"

    bind_response = client.put(
        "/agentic/ontologies/demo-ontology/data-binding",
        headers=headers,
        json={"connection_id": "example-nl2sql"},
    )
    assert bind_response.status_code == 200
    assert bind_response.get_json()["binding"]["connection_id"] == "example-nl2sql"

    ontologies_response = client.get("/agentic/ontologies", headers=headers)
    assert ontologies_response.status_code == 200
    ontology = ontologies_response.get_json()["ontologies"][0]
    assert ontology["business_data"] == {
        "connection_id": "example-nl2sql",
        "connection_name": "示例制造 NL2SQL",
        "provider": "nl2sql_http",
        "ready": True,
    }

    delete_response = client.delete(
        "/agentic/data-connections/example-nl2sql",
        headers=headers,
    )
    assert delete_response.status_code == 409
    assert delete_response.get_json()["bound_ontologies"] == ["demo-ontology"]


def test_data_connection_secret_fields_are_encrypted_at_rest(tmp_path, monkeypatch):
    from agentic.business_data.connections_store import (
        create_data_connection,
        get_data_connection,
        list_data_connections,
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)

    with workspace_scope("demo"):
        create_data_connection(
            {
                "id": "secure-nl2sql",
                "name": "Secure NL2SQL",
                "provider": "nl2sql_http",
                "config": {
                    "url": "https://nl2sql.example.local/",
                    "token": "secret-token",
                    "scene": "demo",
                },
            },
            workspace_id="demo",
        )

        raw_text = (paths.workspace_dir / "data-connections.json").read_text(encoding="utf-8")
        raw = json.loads(raw_text)

        assert "secret-token" not in raw_text
        assert raw["connections"][0]["config"]["token"].startswith("enc:v1:")

        stored = get_data_connection("secure-nl2sql", workspace_id="demo")
        assert stored.config["token"] == "secret-token"

        public = list_data_connections("demo")
        assert public["connections"][0]["config"]["has_token"] is True
        assert "token" not in public["connections"][0]["config"]


def test_legacy_plaintext_data_connection_secret_is_rewritten_on_read(tmp_path, monkeypatch):
    from agentic.business_data.connections_store import get_data_connection

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    connections_path = paths.workspace_dir / "data-connections.json"
    connections_path.write_text(
        json.dumps(
            {
                "default_connection_id": "legacy-nl2sql",
                "connections": [
                    {
                        "id": "legacy-nl2sql",
                        "name": "Legacy NL2SQL",
                        "provider": "nl2sql_http",
                        "config": {
                            "url": "https://nl2sql.example.local",
                            "token": "legacy-token",
                            "scene": "demo",
                            "timeout_seconds": 600,
                            "max_retries": 3,
                        },
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    with workspace_scope("demo"):
        connection = get_data_connection("legacy-nl2sql", workspace_id="demo")

    assert connection.config["token"] == "legacy-token"
    raw_text = connections_path.read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert "legacy-token" not in raw_text
    assert raw["connections"][0]["config"]["token"].startswith("enc:v1:")


def test_data_connection_routes_generate_id_when_id_is_omitted(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "name": "Warehouse NL2SQL",
            "provider": "nl2sql_http",
            "config": {
                "url": "https://nl2sql.example.com/",
                "scene": "warehouse",
            },
        },
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload["connection"]["id"] == "warehouse-nl2sql"
    assert payload["connection"]["name"] == "Warehouse NL2SQL"


def test_data_connection_routes_disambiguate_auto_generated_ids(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    first_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "name": "aa",
            "provider": "nl2sql_http",
            "config": {"url": "https://nl2sql-aa.example.com/"},
        },
    )
    assert first_response.status_code == 201
    assert first_response.get_json()["connection"]["id"] == "aa"

    second_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "name": "aa_测试",
            "provider": "nl2sql_http",
            "config": {"url": "https://nl2sql-aa-test.example.com/"},
        },
    )
    assert second_response.status_code == 201
    assert second_response.get_json()["connection"]["id"] == "aa-2"


def test_data_connection_name_must_be_unique_within_workspace(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    first_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "warehouse-a",
            "name": "Warehouse NL2SQL",
            "provider": "nl2sql_http",
            "config": {"url": "https://nl2sql-a.example.com/"},
        },
    )
    assert first_response.status_code == 201

    duplicate_create = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "warehouse-b",
            "name": "  warehouse nl2sql  ",
            "provider": "nl2sql_http",
            "config": {"url": "https://nl2sql-b.example.com/"},
        },
    )
    assert duplicate_create.status_code == 400
    assert "名称已存在" in duplicate_create.get_json()["detail"]

    second_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "warehouse-c",
            "name": "Warehouse Backup",
            "provider": "nl2sql_http",
            "config": {"url": "https://nl2sql-c.example.com/"},
        },
    )
    assert second_response.status_code == 201

    duplicate_update = client.patch(
        "/agentic/data-connections/warehouse-c",
        headers=headers,
        json={
            "name": "Warehouse NL2SQL",
            "provider": "nl2sql_http",
            "config": {"url": "https://nl2sql-c.example.com/"},
        },
    )
    assert duplicate_update.status_code == 400
    assert "名称已存在" in duplicate_update.get_json()["detail"]


def test_data_connection_create_rejects_eleventh_connection(tmp_path, monkeypatch):
    from agentic.business_data.connections_store import (
        BusinessDataConnectionStoreError,
        create_data_connection,
    )

    _create_workspace_with_ontology(tmp_path, monkeypatch)

    with workspace_scope("demo"):
        for index in range(10):
            create_data_connection(
                {
                    "id": "conn-{}".format(index),
                    "name": "Connection {}".format(index),
                    "provider": "nl2sql_http",
                    "config": {"url": "https://nl2sql-{}.example.com/".format(index)},
                }
            )

        try:
            create_data_connection(
                {
                    "id": "conn-10",
                    "name": "Connection 10",
                    "provider": "nl2sql_http",
                    "config": {"url": "https://nl2sql-10.example.com/"},
                }
        )
        except BusinessDataConnectionStoreError as exc:
            assert "数据连接配置数量已达上限" in str(exc)
        else:
            raise AssertionError("eleventh data connection should be rejected")

def test_data_connection_test_route_probes_nl2sql_http_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "json": json,
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-nl2sql",
            "name": "Test NL2SQL",
            "provider": "nl2sql_http",
            "config": {
                "url": "https://nl2sql.example.com/",
                "token": "secret-token",
                "scene": "example",
                "timeout_seconds": 120,
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-nl2sql/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "连接正常" in payload["message"]
    assert calls == [
        {
            "url": "https://nl2sql.example.com/api/v1/query",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
            "json": {"question": "连接测试", "scene": "example"},
            "timeout": 10,
        }
    ]


def test_data_connection_test_route_probes_mindsdb_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"data": []}

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "json": json,
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr("requests.post", fake_post)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-mindsdb",
            "name": "Test MindsDB",
            "provider": "mindsdb",
            "config": {
                "url": "https://mindsdb.example.com/",
                "default_database": "mindsdb",
                "requires_auth": False,
                "timeout_seconds": 120,
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-mindsdb/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "匿名访问可用" in payload["message"]
    assert calls == [
        {
            "url": "https://mindsdb.example.com/api/sql/query",
            "headers": {"Content-Type": "application/json"},
            "json": {
                "query": "SELECT 1",
                "context": {"db": "mindsdb", "show_secrets": False},
            },
            "timeout": 15,
        }
    ]


def test_data_connection_test_route_probes_mindsdb_auth_and_query(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(url, headers=None, json=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "json": json,
            "timeout": timeout,
        })
        if url.endswith("/api/login"):
            return FakeResponse({"token": "jwt-token"})
        return FakeResponse({"data": []})

    monkeypatch.setattr("requests.post", fake_post)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-mindsdb-auth",
            "name": "Test MindsDB Auth",
            "provider": "mindsdb",
            "config": {
                "url": "https://mindsdb.example.com/",
                "login": "mindsdb",
                "password": "secret-password",
                "default_database": "mindsdb",
                "requires_auth": True,
                "timeout_seconds": 120,
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-mindsdb-auth/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "鉴权与查询接口可用" in payload["message"]
    assert calls == [
        {
            "url": "https://mindsdb.example.com/api/login",
            "headers": {},
            "json": {"username": "mindsdb", "password": "secret-password"},
            "timeout": 15,
        },
        {
            "url": "https://mindsdb.example.com/api/sql/query",
            "headers": {"Content-Type": "application/json", "Authorization": "Bearer jwt-token"},
            "json": {
                "query": "SELECT 1",
                "context": {"db": "mindsdb", "show_secrets": False},
            },
            "timeout": 15,
        },
    ]
