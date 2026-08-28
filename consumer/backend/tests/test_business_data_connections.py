"""Tests for ontology-scoped business data provider APIs and probes."""

from __future__ import annotations

from fastapi.testclient import TestClient

from api.testing import TestApplication

import json
import sys
from types import SimpleNamespace

import pytest
import requests

from auth_test_backend import AuthManager
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
    from api.routes.business_data import business_data_router
    from api.routes.ontologies import ontologies_router

    _create_workspace_with_ontology(tmp_path, monkeypatch)
    app = TestApplication()
    app.state.services["TESTING"] = True

    class _DirectIO:
        async def run(self, function, *args, **kwargs):
            return function(*args, **kwargs)

    class _RequestsAsyncClient:
        async def get(self, url, **kwargs):
            return requests.Session().get(url, **kwargs)

        async def post(self, url, **kwargs):
            return requests.Session().post(url, **kwargs)

    app.state.agent_execution_runtime = SimpleNamespace(
        short_io=_DirectIO(),
        http_client=_RequestsAsyncClient(),
    )
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(business_data_router)
    app.include_router(ontologies_router)
    return TestClient(app), {"Authorization": "Bearer {}".format(auth_manager.issue_token(user))}


def _test_candidate(client, headers, provider: str, config: dict):
    return client.post(
        "/agentic/ontologies/demo-ontology/data-source/test",
        headers=headers,
        json={"mode": "direct_provider", "provider": provider, "config": config},
    )


def test_modeling_internal_runtime_search_uses_workspace_source(
    tmp_path, monkeypatch
):
    client, _headers = _build_test_client(tmp_path, monkeypatch)
    service_token = "s" * 32
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", service_token)
    captured = {}

    async def fake_search(**kwargs):
        captured.update(kwargs)
        return '{"success":true,"result_count":205}'

    monkeypatch.setattr(
        "api.routes.business_data.handle_business_data_search_async",
        fake_search,
    )
    response = client.post(
        "/agentic/internal/modeling/business-data/search",
        headers={
            "Authorization": f"Bearer {service_token}",
            "X-Workspace-Id": "demo",
        },
        json={
            "ontology_id": "demo-ontology",
            "ontology_version": "1.0.0",
            "entity": "synthetic_record",
            "query": "查找完整运行时记录",
        },
    )

    assert response.status_code == 200, response.text
    assert response.json()["result"] == '{"success":true,"result_count":205}'
    assert captured == {
        "ontology_id": "demo-ontology",
        "ontology_version": "1.0.0",
        "entity": "synthetic_record",
        "query": "查找完整运行时记录",
        "workspace_id": "demo",
    }


def test_modeling_internal_runtime_search_requires_service_token(
    tmp_path, monkeypatch
):
    client, _headers = _build_test_client(tmp_path, monkeypatch)
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)

    response = client.post(
        "/agentic/internal/modeling/business-data/search",
        headers={"Authorization": "Bearer wrong-service-token"},
        json={
            "ontology_id": "demo-ontology",
            "entity": "synthetic_record",
            "query": "查找完整运行时记录",
        },
    )

    assert response.status_code == 403


@pytest.mark.parametrize(
    ("method", "path", "resolver_name"),
    [
        ("GET", "/agentic/business-data/provider-schemas", "resolve_workspace_id"),
        ("GET", "/agentic/ontologies/demo-ontology/data-source", "resolve_workspace_id"),
        (
            "PUT",
            "/agentic/ontologies/demo-ontology/data-source",
            "resolve_workspace_owner_or_admin_id",
        ),
        (
            "DELETE",
            "/agentic/ontologies/demo-ontology/data-source",
            "resolve_workspace_owner_or_admin_id",
        ),
        (
            "POST",
            "/agentic/ontologies/demo-ontology/data-source/test",
            "resolve_workspace_owner_or_admin_id",
        ),
    ],
)
def test_business_data_routes_enforce_workspace_authz_before_access(
    tmp_path,
    monkeypatch,
    method,
    path,
    resolver_name,
):
    from fastapi import HTTPException

    def deny_access(_request):
        raise HTTPException(status_code=403)

    client, headers = _build_test_client(tmp_path, monkeypatch)
    monkeypatch.setattr(
        "api.dependencies.{}".format(resolver_name),
        deny_access,
    )
    response = client.request(method, path, headers=headers, json={})
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


def test_shadow_workspace_owner_can_manage_ontology_data_source(tmp_path, monkeypatch):
    from api.routes.business_data import business_data_router
    from api.routes.workspaces import workspaces_router

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)
    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)

    app = TestApplication()
    app.state.services["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    auth_manager.create_user("alice", "UserStrong123!", role="user")
    alice = auth_manager.change_password("alice", "UserStrong123!", "ChangedStrong123!")
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(workspaces_router)
    app.include_router(business_data_router)
    client = TestClient(app)

    provisioned = client.post(
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )
    assert provisioned.status_code == 201

    ontology_dir = (
        get_workspace_paths("mw-shadow-pj-demo-123").ontologies_dir
        / "evaluation-demo"
    )
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: evaluation-demo\n"
            "name: Evaluation Demo\n"
            "description: Evaluation package\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )

    response = client.put(
        "/agentic/ontologies/evaluation-demo/data-source",
        headers={
            "Authorization": "Bearer {}".format(auth_manager.issue_token(alice)),
            "X-Workspace-Id": "mw-shadow-pj-demo-123",
        },
        json={
            "mode": "direct_provider",
            "provider": "nl2sql_http",
            "config": {
                "url": "http://nl2sql:5600",
                "token": "secret-token",
                "scene": "mw_proj_demo",
            },
        },
    )

    assert response.status_code == 200
    assert response.json()["source"]["provider"] == "nl2sql_http"


def test_graph_provider_schemas_and_secrets_are_publicized(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    response = client.get("/agentic/business-data/provider-schemas", headers=headers)
    assert response.status_code == 200
    schemas = {item["name"]: item for item in response.json()["providers"]}
    assert {"neo4j", "hugegraph"}.issubset(schemas)
    neo4j_fields = {field["name"]: field for field in schemas["neo4j"]["fields"]}
    hugegraph_fields = {field["name"]: field for field in schemas["hugegraph"]["fields"]}
    assert neo4j_fields["password"]["secret"] is True
    assert neo4j_fields["workspace_property"]["default"] == "d2o_workspace_id"
    assert neo4j_fields["workspace_value"]["default"] == ""
    assert neo4j_fields["ontology_value"]["default"] == ""
    assert neo4j_fields["graph_connect_timeout_seconds"]["default"] == 10
    assert neo4j_fields["graph_query_timeout_seconds"]["default"] == 30
    assert neo4j_fields["graph_agent_timeout_seconds"]["default"] == 120
    assert hugegraph_fields["token"]["secret"] is True
    assert neo4j_fields["ontology_property"]["default"] == "d2o_ontology_id"
    assert neo4j_fields["deterministic_snapshot_immutable"]["default"] is False
    assert neo4j_fields["deterministic_scope_isolated"]["default"] is False
    assert hugegraph_fields["ontology_property"]["default"] == "d2o_ontology_id"
    assert hugegraph_fields["graph_connect_timeout_seconds"]["default"] == 10
    assert hugegraph_fields["graph_query_timeout_seconds"]["default"] == 30
    assert hugegraph_fields["graph_agent_timeout_seconds"]["default"] == 120
    assert hugegraph_fields["max_retries"]["default"] == 1


def test_graph_provider_defaults_and_explicitly_disables_ontology_scope():
    from agent.tools.business_data.provider_registry import normalize_provider_config

    base_config = {
        "uri": "bolt://neo4j.example.com:7687",
        "username": "neo4j",
        "password": "secret-password",
    }

    defaulted = normalize_provider_config("neo4j", base_config)
    disabled = normalize_provider_config(
        "neo4j",
        {**base_config, "ontology_property": ""},
    )

    assert defaulted["ontology_property"] == "d2o_ontology_id"
    assert disabled["ontology_property"] == ""


@pytest.mark.parametrize(
    ("provider", "config"),
    [
        (
            "neo4j",
            {
                "uri": "bolt://embedded:uri-secret@neo4j.example.com:7687",
                "username": "neo4j",
                "password": "dedicated-secret",
            },
        ),
        (
            "neo4j",
            {
                "uri": "neo4j:uri-secret@neo4j.example.com:7687",
                "username": "neo4j",
                "password": "dedicated-secret",
            },
        ),
        (
            "hugegraph",
            {"url": "https://embedded:uri-secret@hugegraph.example.com"},
        ),
        (
            "hugegraph",
            {"url": "https://hugegraph.example.com?access_token=query-secret"},
        ),
        (
            "hugegraph",
            {"url": "https://hugegraph.example.com#fragment-secret"},
        ),
    ],
)
def test_graph_provider_config_rejects_endpoint_userinfo(provider, config):
    from agent.tools.business_data.provider_registry import (
        BusinessDataProviderError,
        normalize_provider_config,
    )

    with pytest.raises(BusinessDataProviderError) as exc:
        normalize_provider_config(provider, config)

    assert "uri-secret" not in str(exc.value)


def test_legacy_endpoint_userinfo_is_removed_from_public_provider_config():
    from agent.tools.business_data.provider_registry import publicize_provider_config

    public = publicize_provider_config(
        "hugegraph",
        {"url": "https://embedded:uri-secret@hugegraph.example.com"},
    )

    assert public["url"] == "https://hugegraph.example.com"
    assert "uri-secret" not in str(public)


def test_legacy_schemeless_endpoint_userinfo_is_fully_redacted():
    from agent.tools.business_data.endpoint_security import endpoint_secret_values
    from agent.tools.business_data.provider_registry import publicize_provider_config

    endpoint = "neo4j:uri-secret@neo4j.example.com:7687"
    public = publicize_provider_config("neo4j", {"uri": endpoint})

    assert public["uri"] == "[redacted-endpoint]"
    assert "uri-secret" in endpoint_secret_values(endpoint)
    assert endpoint in endpoint_secret_values(endpoint)


def test_legacy_endpoint_query_credentials_and_fragment_are_removed_from_public_config():
    from agent.tools.business_data.provider_registry import publicize_provider_config

    public = publicize_provider_config(
        "hugegraph",
        {
            "url": (
                "https://hugegraph.example.com?region=east&access_token=query-secret"
                "#fragment-secret"
            )
        },
    )

    assert public["url"] == "https://hugegraph.example.com?region=east"
    assert "query-secret" not in str(public)
    assert "fragment-secret" not in str(public)


def test_legacy_graph_timeout_is_publicized_without_widening_stricter_limits():
    from agent.tools.business_data.provider_registry import publicize_provider_config

    public = publicize_provider_config("neo4j", {"timeout_seconds": 5})

    assert public["graph_connect_timeout_seconds"] == 5
    assert public["graph_query_timeout_seconds"] == 5
    assert public["graph_agent_timeout_seconds"] == 120


@pytest.mark.parametrize(
    ("provider", "config"),
    [
        (
            "neo4j",
            {
                "uri": "bolt://neo4j.example.com:7687",
                "username": "neo4j",
                "password": "secret-password",
                "timeout_seconds": 5,
            },
        ),
        (
            "hugegraph",
            {
                "url": "https://hugegraph.example.com",
                "timeout_seconds": 5,
            },
        ),
    ],
)
def test_legacy_graph_timeout_is_normalized_without_widening_stricter_limits(provider, config):
    from agent.tools.business_data.provider_registry import normalize_provider_config

    normalized = normalize_provider_config(provider, config)

    assert normalized["graph_connect_timeout_seconds"] == 5
    assert normalized["graph_query_timeout_seconds"] == 5
    assert normalized["graph_agent_timeout_seconds"] == 120


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
                "uri": "bolt://neo4j.example.com:7687",
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
    assert response.json()["success"] is True
    assert calls == [{
        "url": "https://nl2sql.example.com/api/v1/sql",
        "headers": {"Content-Type": "application/json", "Authorization": "Bearer secret-token"},
        "json": {"sql": "SELECT 1", "scene": "example"},
        "timeout": 10,
    }]


def test_nl2sql_schemas_require_token_and_scene(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    provider_schemas = client.get(
        "/agentic/business-data/provider-schemas",
        headers=headers,
    ).json()
    schemas = {item["name"]: item for item in provider_schemas["providers"]}

    for provider_name in ("nl2sql_http", "nl2sql_template"):
        fields = {
            field["name"]: field
            for field in schemas[provider_name]["fields"]
        }
        assert fields["token"]["required"] is True
        assert fields["token"]["secret"] is True
        assert fields["scene"]["required"] is True


@pytest.mark.parametrize("provider", ["nl2sql_http", "nl2sql_template"])
@pytest.mark.parametrize(
    ("missing_field", "expected_detail"),
    [
        ("token", "访问 Token 不能为空"),
        ("scene", "不能为空"),
    ],
)
def test_nl2sql_new_config_rejects_missing_required_fields_for_save_and_probe(
    tmp_path,
    monkeypatch,
    provider,
    missing_field,
    expected_detail,
):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    config = {
        "url": "https://nl2sql.example.com/",
        "token": "secret-token",
        "scene": "example",
        "timeout_seconds": 120,
    }
    config.pop(missing_field)
    payload = {
        "mode": "direct_provider",
        "provider": provider,
        "config": config,
    }

    saved = client.put(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
        json=payload,
    )
    tested = client.post(
        "/agentic/ontologies/demo-ontology/data-source/test",
        headers=headers,
        json=payload,
    )

    assert saved.status_code == 400
    assert expected_detail in saved.json()["detail"]
    assert tested.status_code == 400
    assert expected_detail in tested.json()["detail"]


@pytest.mark.parametrize("provider", ["nl2sql_http", "nl2sql_template"])
def test_nl2sql_edit_preserves_existing_token_and_probes_sql(
    tmp_path,
    monkeypatch,
    provider,
):
    client, headers = _build_test_client(tmp_path, monkeypatch)
    calls = []

    class FakeResponse:
        status_code = 200
        text = "{}"

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
    initial = {
        "url": "https://nl2sql.example.com/",
        "token": "secret-token",
        "scene": "example",
        "timeout_seconds": 120,
    }
    saved = client.put(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
        json={
            "mode": "direct_provider",
            "provider": provider,
            "config": initial,
        },
    )
    assert saved.status_code == 200

    edited = {key: value for key, value in initial.items() if key != "token"}
    updated = client.put(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
        json={
            "mode": "direct_provider",
            "provider": provider,
            "config": edited,
        },
    )
    tested = _test_candidate(client, headers, provider, edited)

    assert updated.status_code == 200
    assert updated.json()["source"]["config"]["has_token"] is True
    assert tested.status_code == 200
    assert tested.json()["success"] is True
    assert calls == [{
        "url": "https://nl2sql.example.com/api/v1/sql",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret-token",
        },
        "json": {"sql": "SELECT 1", "scene": "example"},
        "timeout": 10,
    }]


@pytest.mark.parametrize("provider", ["nl2sql_http", "nl2sql_template"])
@pytest.mark.parametrize(
    ("missing_field", "expected_detail"),
    [
        ("token", "访问 Token 不能为空"),
        ("scene", "不能为空"),
    ],
)
def test_legacy_nl2sql_config_is_rejected_before_saved_probe_and_runtime(
    tmp_path,
    monkeypatch,
    provider,
    missing_field,
    expected_detail,
):
    from agent.tools.business_data.resolver import (
        BusinessDataConnectionError,
        resolve_business_data_source,
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    client, headers = _build_test_client(tmp_path, monkeypatch)
    valid_config = {
        "url": "https://nl2sql.example.com/",
        "token": "secret-token",
        "scene": "example",
        "timeout_seconds": 120,
    }
    saved = client.put(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
        json={
            "mode": "direct_provider",
            "provider": provider,
            "config": valid_config,
        },
    )
    assert saved.status_code == 200

    source_path = get_workspace_paths("demo").workspace_dir / "ontology-data-sources.json"
    document = json.loads(source_path.read_text(encoding="utf-8"))
    document["sources"]["demo-ontology"]["config"].pop(missing_field)
    source_path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    calls = []
    monkeypatch.setattr(
        requests.Session,
        "post",
        lambda *_args, **_kwargs: calls.append(True),
    )

    tested = client.post(
        "/agentic/ontologies/demo-ontology/data-source/test",
        headers=headers,
        json={},
    )

    assert tested.status_code == 400
    assert expected_detail in tested.json()["detail"]
    assert calls == []
    with pytest.raises(BusinessDataConnectionError, match=expected_detail):
        resolve_business_data_source(
            workspace_id="demo",
            ontology_id="demo-ontology",
        )


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
    assert response.json()["success"] is True
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
    assert response.json()["success"] is True
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
    payload = response.json()
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
    payload = response.json()
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
    assert response.json()["success"] is True
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

    monkeypatch.setitem(
        sys.modules,
        "neo4j",
        SimpleNamespace(GraphDatabase=FakeGraphDatabase),
    )
    response = _test_candidate(
        client,
        headers,
        "neo4j",
        {
            "uri": "bolt://neo4j.example.com:7687",
            "database": "domain",
            "username": "neo4j",
            "password": "secret-password",
            "graph_connect_timeout_seconds": 10,
            "workspace_property": "d2o_workspace_id",
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == [
        ("driver", "bolt://neo4j.example.com:7687", ("neo4j", "secret-password"), 10),
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

    monkeypatch.setattr(requests, "get", fake_get)
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
            "graph_connect_timeout_seconds": 10,
            "workspace_property": "d2o_workspace_id",
        },
    )
    assert response.status_code == 200
    assert response.json()["success"] is True
    assert calls == [{
        "url": "https://hugegraph.example.com/graphs/demo/schema",
        "headers": {"Authorization": "Bearer secret-token"},
        "auth": ("graph-user", "secret-password"),
        "timeout": 10,
    }]
