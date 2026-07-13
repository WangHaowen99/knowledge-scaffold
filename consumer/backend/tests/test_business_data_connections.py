"""Tests for workspace-scoped business data connections and ontology bindings."""

from __future__ import annotations

import json

import pytest
from flask import Flask

from api.auth.auth import AuthManager
from workspace.workspaces import create_workspace, get_workspace_paths, workspace_scope


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
    from api.routes.business_data import business_data_bp
    from api.routes.ontologies import ontologies_bp

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


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/agentic/business-data/provider-schemas"),
        ("GET", "/agentic/data-connections"),
        ("POST", "/agentic/data-connections"),
        ("PATCH", "/agentic/data-connections/example"),
        ("DELETE", "/agentic/data-connections/example"),
        ("POST", "/agentic/data-connections/example/default"),
        ("POST", "/agentic/data-connections/example/test"),
        ("GET", "/agentic/ontologies/demo-ontology/data-binding"),
        ("PUT", "/agentic/ontologies/demo-ontology/data-binding"),
        ("DELETE", "/agentic/ontologies/demo-ontology/data-binding"),
        ("GET", "/agentic/ontologies/demo-ontology/data-source"),
        ("PUT", "/agentic/ontologies/demo-ontology/data-source"),
        ("DELETE", "/agentic/ontologies/demo-ontology/data-source"),
        ("POST", "/agentic/ontologies/demo-ontology/data-source/test"),
    ],
)
def test_business_data_routes_enforce_workspace_authz_before_access(
    tmp_path,
    monkeypatch,
    method,
    path,
):
    from flask import abort

    client, headers = _build_test_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "api.routes.business_data.resolve_workspace_id",
        lambda _request: abort(403),
    )

    response = client.open(path, method=method, headers=headers, json={})

    assert response.status_code == 403


def test_resolver_requires_explicit_ontology_binding(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import set_ontology_data_binding
    from agent.tools.business_data.connections_store import create_data_connection
    from agent.tools.business_data.resolver import (
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

    binding_response = client.get(
        "/agentic/ontologies/demo-ontology/data-binding",
        headers=headers,
    )
    assert binding_response.status_code == 200
    assert binding_response.get_json()["binding"]["connection_id"] == "example-nl2sql"
    source_document = json.loads(
        (get_workspace_paths("demo").workspace_dir / "ontology-data-sources.json").read_text(
            encoding="utf-8",
        )
    )
    source_payload = source_document["sources"]["demo-ontology"]
    assert source_payload["mode"] == "connection_ref"
    assert source_payload["connection_id"] == "example-nl2sql"
    assert "provider" not in source_payload
    assert "config" not in source_payload

    ontologies_response = client.get("/agentic/ontologies", headers=headers)
    assert ontologies_response.status_code == 200
    ontology = ontologies_response.get_json()["ontologies"][0]
    assert ontology["business_data"]["configured"] is True
    assert ontology["business_data"]["ready"] is True
    assert ontology["business_data"]["mode"] == "connection_ref"
    assert ontology["business_data"]["connection_id"] == "example-nl2sql"
    assert ontology["business_data"]["connection_name"] == "示例制造 NL2SQL"
    assert ontology["business_data"]["provider"] == "nl2sql_http"

    delete_response = client.delete(
        "/agentic/data-connections/example-nl2sql",
        headers=headers,
    )
    assert delete_response.status_code == 409
    assert delete_response.get_json()["bound_ontologies"] == ["demo-ontology"]


def test_data_connection_secret_fields_are_encrypted_at_rest(tmp_path, monkeypatch):
    from agent.tools.business_data.connections_store import (
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


def test_graph_provider_schemas_and_secrets_are_publicized():
    from agent.tools.business_data.provider_registry import list_provider_schemas, publicize_provider_config

    schemas = {item["name"]: item for item in list_provider_schemas()}

    assert "dataagent" in schemas
    assert "neo4j" in schemas
    assert "hugegraph" in schemas
    dataagent_fields = {field["name"]: field for field in schemas["dataagent"]["fields"]}
    neo4j_fields = {field["name"]: field for field in schemas["neo4j"]["fields"]}
    hugegraph_fields = {field["name"]: field for field in schemas["hugegraph"]["fields"]}
    assert dataagent_fields["url"]["required"] is True
    assert dataagent_fields["token"]["secret"] is True
    assert dataagent_fields["timeout_seconds"]["default"] == 600
    assert dataagent_fields["max_retries"]["default"] == 3
    assert neo4j_fields["password"]["secret"] is True
    assert neo4j_fields["max_result_rows"]["default"] == 100
    assert "enable_graph_agent" not in neo4j_fields
    assert neo4j_fields["max_iterations"]["default"] == 4
    assert "graph_agent_runtime" not in neo4j_fields
    assert "enable_jiuwen_graph_agent" not in neo4j_fields
    assert neo4j_fields["workspace_property"]["default"] == "d2o_workspace_id"
    assert hugegraph_fields["token"]["secret"] is True
    assert hugegraph_fields["graph"]["default"] == "hugegraph"
    assert "graph_space" in hugegraph_fields
    assert hugegraph_fields["max_result_rows"]["default"] == 100
    assert "enable_graph_agent" not in hugegraph_fields
    assert hugegraph_fields["max_iterations"]["default"] == 4
    assert "graph_agent_runtime" not in hugegraph_fields
    assert "enable_jiuwen_graph_agent" not in hugegraph_fields
    assert hugegraph_fields["workspace_property"]["default"] == "d2o_workspace_id"

    public = publicize_provider_config(
        "neo4j",
        {
            "uri": "bolt://neo4j.test:7687",
            "database": "neo4j",
            "username": "neo4j",
            "password": "secret-password",
            "workspace_property": "d2o_workspace_id",
        },
    )
    assert public["has_password"] is True
    assert "password" not in public
    assert public["uri"] == "bolt://neo4j.test:7687"

    public_dataagent = publicize_provider_config(
        "dataagent",
        {
            "url": "https://dataagent.example.com",
            "token": "secret-token",
            "timeout_seconds": 120,
            "max_retries": 2,
        },
    )
    assert public_dataagent["has_token"] is True
    assert "token" not in public_dataagent
    assert public_dataagent["url"] == "https://dataagent.example.com"


def test_neo4j_data_connection_secret_fields_are_encrypted_at_rest(tmp_path, monkeypatch):
    from agent.tools.business_data.connections_store import create_data_connection, list_data_connections

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)

    with workspace_scope("demo"):
        create_data_connection(
            {
                "id": "secure-neo4j",
                "name": "Secure Neo4J",
                "provider": "neo4j",
                "config": {
                    "uri": "bolt://neo4j.example.local:7687",
                    "database": "neo4j",
                    "username": "neo4j",
                    "password": "secret-password",
                    "workspace_property": "d2o_workspace_id",
                },
            },
            workspace_id="demo",
        )

        raw_text = (paths.workspace_dir / "data-connections.json").read_text(encoding="utf-8")
        raw = json.loads(raw_text)

        assert "secret-password" not in raw_text
        assert raw["connections"][0]["config"]["password"].startswith("enc:v1:")
        public = list_data_connections("demo")
        assert public["connections"][0]["config"]["has_password"] is True
        assert "password" not in public["connections"][0]["config"]


def test_legacy_plaintext_data_connection_secret_is_rewritten_on_read(tmp_path, monkeypatch):
    from agent.tools.business_data.connections_store import get_data_connection

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
    from agent.tools.business_data.connections_store import (
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


def test_data_connection_test_route_probes_dataagent_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"name": "DataAgent", "version": "1.0.0"}

    def fake_get(url, headers=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-dataagent",
            "name": "Test DataAgent",
            "provider": "dataagent",
            "config": {
                "url": "https://dataagent.example.com/",
                "token": "secret-token",
                "timeout_seconds": 120,
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-dataagent/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "DataAgent REST 服务连接正常" in payload["message"]
    assert calls == [
        {
            "url": "https://dataagent.example.com/.well-known/agent-card.json",
            "headers": {"Authorization": "Bearer secret-token"},
            "timeout": 10,
        }
    ]


def test_data_connection_test_route_probes_dataagent_legacy_agent_card(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        text = "{}"

        def __init__(self, status_code, payload):
            self.status_code = status_code
            self._payload = payload

        def raise_for_status(self):
            if self.status_code >= 400:
                raise AssertionError("unexpected status")

        def json(self):
            return self._payload

    def fake_get(url, headers=None, timeout=None):
        calls.append(url)
        if url.endswith("/.well-known/agent-card.json"):
            return FakeResponse(404, {"detail": "not found"})
        return FakeResponse(200, {"name": "DataAgent"})

    monkeypatch.setattr("requests.get", fake_get)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-dataagent-legacy",
            "name": "Test DataAgent Legacy",
            "provider": "dataagent",
            "config": {"url": "https://dataagent.example.com/"},
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-dataagent-legacy/test", headers=headers)

    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls == [
        "https://dataagent.example.com/.well-known/agent-card.json",
        "https://dataagent.example.com/.well-known/agent.json",
    ]


def test_data_connection_create_and_desensitize_dataagent(tmp_path, monkeypatch):
    from agent.tools.business_data.connections_store import create_data_connection, get_data_connection, list_data_connections

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)

    with workspace_scope("demo"):
        create_data_connection(
            {
                "id": "demo-dataagent",
                "name": "Demo DataAgent",
                "provider": "dataagent",
                "config": {
                    "url": "https://dataagent.example.com/",
                    "token": "secret-token",
                    "timeout_seconds": 120,
                },
            },
            workspace_id="demo",
        )

        raw_text = (paths.workspace_dir / "data-connections.json").read_text(encoding="utf-8")
        raw = json.loads(raw_text)

        assert "secret-token" not in raw_text
        assert raw["connections"][0]["config"]["token"].startswith("enc:v1:")
        stored = get_data_connection("demo-dataagent", workspace_id="demo")
        assert stored.provider == "dataagent"
        assert stored.config["url"] == "https://dataagent.example.com"
        assert stored.config["token"] == "secret-token"

        public = list_data_connections("demo")
        conn = public["connections"][0]
        assert conn["config"]["url"] == "https://dataagent.example.com"
        assert conn["config"]["has_token"] is True
        assert "token" not in conn["config"]


def test_data_connection_test_route_probes_neo4j_service(tmp_path, monkeypatch):
    import sys
    from types import SimpleNamespace

    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResult:
        def single(self):
            return {"ok": 1}

    class FakeSession:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def run(self, query):
            calls.append(("run", query))
            return FakeResult()

    class FakeDriver:
        def verify_connectivity(self):
            calls.append(("verify", None))

        def session(self, database=None):
            calls.append(("session", database))
            return FakeSession()

        def close(self):
            calls.append(("close", None))

    class FakeGraphDatabase:
        @staticmethod
        def driver(uri, auth=None, connection_timeout=None):
            calls.append(("driver", uri, auth, connection_timeout))
            return FakeDriver()

    monkeypatch.setitem(sys.modules, "neo4j", SimpleNamespace(GraphDatabase=FakeGraphDatabase))
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-neo4j",
            "name": "Test Neo4J",
            "provider": "neo4j",
            "config": {
                "uri": "bolt://neo4j.example.com:7687",
                "database": "domain",
                "username": "neo4j",
                "password": "secret-password",
                "workspace_property": "d2o_workspace_id",
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-neo4j/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "Neo4J 连接成功" in payload["message"]
    assert calls == [
        ("driver", "bolt://neo4j.example.com:7687", ("neo4j", "secret-password"), 15),
        ("verify", None),
        ("session", "domain"),
        ("run", "RETURN 1 AS ok"),
        ("close", None),
    ]


def test_data_connection_test_route_probes_hugegraph_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"name": "demo"}

    def fake_get(url, headers=None, auth=None, timeout=None):
        calls.append({
            "url": url,
            "headers": headers or {},
            "auth": auth,
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)
    create_response = client.post(
        "/agentic/data-connections",
        headers=headers,
        json={
            "id": "test-hugegraph",
            "name": "Test HugeGraph",
            "provider": "hugegraph",
            "config": {
                "url": "https://hugegraph.example.com",
                "graph": "demo",
                "username": "graph-user",
                "password": "secret-password",
                "token": "secret-token",
                "workspace_property": "d2o_workspace_id",
            },
        },
    )
    assert create_response.status_code == 201

    response = client.post("/agentic/data-connections/test-hugegraph/test", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert "HugeGraph 连接成功" in payload["message"]
    assert calls == [
        {
            "url": "https://hugegraph.example.com/graphs/demo/schema",
            "headers": {"Authorization": "Bearer secret-token"},
            "auth": ("graph-user", "secret-password"),
            "timeout": 15,
        }
    ]
