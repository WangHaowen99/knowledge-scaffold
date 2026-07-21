"""Tests for ontology-scoped business data provider APIs and probes."""

from __future__ import annotations

import json
import sys
from types import SimpleNamespace

import pytest
import requests
from flask import Flask

from api.auth.auth import AuthManager
from workspace.workspaces import create_workspace, get_workspace_paths


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _create_workspace_with_ontology(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    paths = get_workspace_paths("demo")
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    ontology_dir = paths.ontologies_dir / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: demo-ontology",
                "name: Demo Ontology",
                "description: Demo ontology for provider tests",
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
    return app.test_client(), {"Authorization": "Bearer {}".format(auth_manager.issue_token(user))}


def _test_candidate(client, headers, provider: str, config: dict):
    return client.post(
        "/agentic/ontologies/demo-ontology/data-source/test",
        headers=headers,
        json={"mode": "direct_provider", "provider": provider, "config": config},
    )


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/agentic/business-data/provider-schemas"),
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
    monkeypatch.setattr("api.routes.business_data.resolve_workspace_id", lambda _request: abort(403))
    with client.open(path, method=method, headers=headers, json={}) as response:
        assert response.status_code == 403


def test_resolver_requires_explicit_ontology_data_source(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import set_ontology_data_source
    from agent.tools.business_data.resolver import BusinessDataConnectionError, resolve_business_data_source

    _create_workspace_with_ontology(tmp_path, monkeypatch)
    with pytest.raises(BusinessDataConnectionError, match="未配置业务数据源"):
        resolve_business_data_source(workspace_id="demo", ontology_id="demo-ontology")

    set_ontology_data_source(
        ontology_id="demo-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "nl2sql_http",
            "config": {
                "url": "https://nl2sql.equipment.local/",
                "token": "secret-token",
                "scene": "example",
                "timeout_seconds": 120,
            },
        },
    )
    resolved = resolve_business_data_source(workspace_id="demo", ontology_id="demo-ontology")
    assert resolved.id == "demo-ontology"
    assert resolved.name == "demo-ontology"
    assert resolved.provider == "nl2sql_http"
    assert resolved.config["url"] == "https://nl2sql.equipment.local"
    assert resolved.config["token"] == "secret-token"


def test_graph_provider_schemas_and_secrets_are_publicized(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    response = client.get("/agentic/business-data/provider-schemas", headers=headers)
    assert response.status_code == 200
    schemas = {item["name"]: item for item in response.get_json()["providers"]}
    assert {"neo4j", "hugegraph"}.issubset(schemas)
    neo4j_fields = {field["name"]: field for field in schemas["neo4j"]["fields"]}
    hugegraph_fields = {field["name"]: field for field in schemas["hugegraph"]["fields"]}
    assert neo4j_fields["password"]["secret"] is True
    assert neo4j_fields["workspace_property"]["default"] == "d2o_workspace_id"
    assert hugegraph_fields["token"]["secret"] is True
    assert hugegraph_fields["ontology_property"]["default"] == "d2o_ontology_id"


def test_neo4j_source_secret_is_encrypted_at_rest(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import publicize_ontology_data_source, set_ontology_data_source

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    source = set_ontology_data_source(
        ontology_id="demo-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "neo4j",
            "config": {
                "uri": "bolt://neo4j.example.local:7687",
                "database": "neo4j",
                "username": "neo4j",
                "password": "secret-password",
                "workspace_property": "d2o_workspace_id",
            },
        },
    )
    raw_text = (paths.workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert "secret-password" not in raw_text
    assert raw["sources"]["demo-ontology"]["config"]["password"].startswith("enc:v1:")
    public = publicize_ontology_data_source(source)
    assert public["config"]["has_password"] is True
    assert "password" not in public["config"]


def test_data_source_test_route_probes_nl2sql_http_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return {"success": True}

    def fake_post(_session, url, headers=None, timeout=None, **request_kwargs):
        calls.append({
            "url": url,
            "headers": headers or {},
            "json": request_kwargs.get("json"),
            "timeout": timeout,
        })
        return FakeResponse()

    monkeypatch.setattr(requests.Session, "post", fake_post)
    response = _test_candidate(
        client,
        headers,
        "nl2sql_http",
        {
            "url": "https://nl2sql.example.com/",
            "token": "secret-token",
            "scene": "example",
            "timeout_seconds": 120,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls == [{
        "url": "https://nl2sql.example.com/api/v1/query",
        "headers": {"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
        "json": {"question": "连接测试", "scene": "example"},
        "timeout": 10,
    }]


def test_data_source_test_route_probes_mindsdb_anonymous_service(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return [{"name": "my_agent", "project_id": 1}]

    def fake_get(_session, url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr(requests.Session, "get", fake_get)
    response = _test_candidate(
        client,
        headers,
        "mindsdb",
        {
            "url": "https://mindsdb.example.com/",
            "default_database": "mindsdb",
            "requires_auth": False,
            "timeout_seconds": 120,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls[0]["url"] == "https://mindsdb.example.com/api/projects/mindsdb/agents"
    assert calls[0]["headers"] == {"Content-Type": "application/json"}
    assert calls[0]["timeout"] == 15


def test_data_source_test_route_probes_mindsdb_auth_and_query(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    post_calls = []
    get_calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

        def __init__(self, payload):
            self._payload = payload

        def raise_for_status(self):
            return None

        def json(self):
            return self._payload

    def fake_post(_session, url, headers=None, timeout=None, **request_kwargs):
        post_calls.append({
            "url": url,
            "headers": headers or {},
            "json": request_kwargs.get("json"),
            "timeout": timeout,
        })
        return FakeResponse({"token": "jwt-token"})

    def fake_get(_session, url, headers=None, timeout=None):
        get_calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        return FakeResponse([{"name": "my_agent", "project_id": 1}])

    monkeypatch.setattr(requests.Session, "post", fake_post)
    monkeypatch.setattr(requests.Session, "get", fake_get)
    response = _test_candidate(
        client,
        headers,
        "mindsdb",
        {
            "url": "https://mindsdb.example.com/",
            "login": "mindsdb",
            "password": "secret-password",
            "default_database": "mindsdb",
            "requires_auth": True,
            "timeout_seconds": 120,
        },
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert [call["url"] for call in post_calls] == ["https://mindsdb.example.com/api/login"]
    assert [call["url"] for call in get_calls] == [
        "https://mindsdb.example.com/api/projects/mindsdb/agents",
    ]
    assert get_calls[0]["headers"]["Authorization"] == "Bearer jwt-token"


def test_data_source_test_route_rejects_missing_mindsdb_agent(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    class FakeResponse:
        status_code = 200
        text = "{}"

        def raise_for_status(self):
            return None

        def json(self):
            return [{"name": "available_agent", "project_id": 1}]

    monkeypatch.setattr(requests.Session, "get", lambda *args, **kwargs: FakeResponse())
    response = _test_candidate(
        client,
        headers,
        "mindsdb",
        {
            "url": "https://mindsdb.example.com/",
            "default_agent_name": "missing_agent",
            "project_name": "mindsdb",
            "requires_auth": False,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert "missing_agent" in payload["message"]
    assert "mindsdb" in payload["message"]


def test_data_source_test_route_rejects_missing_mindsdb_project(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    class FakeResponse:
        status_code = 404
        text = "{}"

        def raise_for_status(self):
            raise AssertionError("404 should be handled before raise_for_status")

        def json(self):
            return {}

    monkeypatch.setattr(requests.Session, "get", lambda *args, **kwargs: FakeResponse())
    response = _test_candidate(
        client,
        headers,
        "mindsdb",
        {
            "url": "https://mindsdb.example.com/",
            "default_agent_name": "my_agent",
            "project_name": "missing_project",
            "requires_auth": False,
        },
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is False
    assert "missing_project" in payload["message"]


def test_data_source_test_route_probes_dataagent_and_legacy_agent_card(tmp_path, monkeypatch):
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

    def fake_get(_session, url, headers=None, timeout=None):
        calls.append({"url": url, "headers": headers or {}, "timeout": timeout})
        if url.endswith("/.well-known/agent-card.json"):
            return FakeResponse(404, {"detail": "not found"})
        return FakeResponse(200, {"name": "DataAgent"})

    monkeypatch.setattr(requests.Session, "get", fake_get)
    response = _test_candidate(
        client,
        headers,
        "dataagent",
        {"url": "https://dataagent.example.com/", "token": "secret-token", "timeout_seconds": 120},
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert [call["url"] for call in calls] == [
        "https://dataagent.example.com/.well-known/agent-card.json",
        "https://dataagent.example.com/.well-known/agent.json",
    ]
    assert calls[0]["headers"] == {"Authorization": "Bearer secret-token"}


def test_data_source_test_route_probes_neo4j_service(tmp_path, monkeypatch):
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
    response = _test_candidate(
        client,
        headers,
        "neo4j",
        {
            "uri": "bolt://neo4j.example.com:7687",
            "database": "domain",
            "username": "neo4j",
            "password": "secret-password",
            "workspace_property": "d2o_workspace_id",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls == [
        ("driver", "bolt://neo4j.example.com:7687", ("neo4j", "secret-password"), 15),
        ("verify", None),
        ("session", "domain"),
        ("run", "RETURN 1 AS ok"),
        ("close", None),
    ]


def test_data_source_test_route_probes_hugegraph_service(tmp_path, monkeypatch):
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
        calls.append({"url": url, "headers": headers or {}, "auth": auth, "timeout": timeout})
        return FakeResponse()

    monkeypatch.setattr("requests.get", fake_get)
    response = _test_candidate(
        client,
        headers,
        "hugegraph",
        {
            "url": "https://hugegraph.example.com",
            "graph": "demo",
            "username": "graph-user",
            "password": "secret-password",
            "token": "secret-token",
            "workspace_property": "d2o_workspace_id",
        },
    )
    assert response.status_code == 200
    assert response.get_json()["success"] is True
    assert calls == [{
        "url": "https://hugegraph.example.com/graphs/demo/schema",
        "headers": {"Authorization": "Bearer secret-token"},
        "auth": ("graph-user", "secret-password"),
        "timeout": 15,
    }]
