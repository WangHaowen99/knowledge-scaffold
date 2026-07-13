"""Tests for ontology-scoped business data source storage."""

from __future__ import annotations

import json

import pytest

from flask import Flask

from api.auth.auth import AuthManager
from workspace.workspaces import create_workspace, get_workspace_paths, workspace_scope


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _create_workspace_with_ontology(tmp_path, monkeypatch, *, ontology_id: str = "demo-ontology"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    paths = get_workspace_paths("demo")
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    ontology_dir = paths.ontologies_dir / ontology_id
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology",
                f"id: {ontology_id}",
                "name: Demo Ontology",
                "description: Demo ontology for data source storage",
                "version: 1.0.0",
                "language: zh-CN",
                "",
            ]
        ),
        encoding="utf-8",
    )
    return paths


def _create_ontology(paths, ontology_id: str) -> None:
    ontology_dir = paths.ontologies_dir / ontology_id
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology",
                f"id: {ontology_id}",
                f"name: {ontology_id}",
                "description: migrated ontology",
                "version: 1.0.0",
                "language: zh-CN",
                "",
            ]
        ),
        encoding="utf-8",
    )


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


def test_ontology_data_source_encrypts_and_publicizes_secret_fields(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import (
        get_ontology_data_source,
        publicize_ontology_data_source,
        set_ontology_data_source,
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)

    with workspace_scope("demo"):
        source = set_ontology_data_source(
            ontology_id="demo-ontology",
            workspace_id="demo",
            data={
                "mode": "direct_provider",
                "provider": "dataagent",
                "config": {
                    "url": "https://dataagent.example.com/",
                    "token": "secret-token",
                    "timeout_seconds": 120,
                },
            },
        )

    raw_text = (paths.workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert raw["version"] == 1
    assert raw["sources"]["demo-ontology"]["mode"] == "direct_provider"
    assert "secret-token" not in raw_text
    assert raw["sources"]["demo-ontology"]["config"]["token"].startswith("enc:v1:")

    stored = get_ontology_data_source("demo-ontology", workspace_id="demo")
    assert stored is not None
    assert stored.mode == "direct_provider"
    assert stored.provider == "dataagent"
    assert stored.config["url"] == "https://dataagent.example.com"
    assert stored.config["token"] == "secret-token"
    assert stored.config["timeout_seconds"] == 120

    public = publicize_ontology_data_source(source)
    assert public["mode"] == "direct_provider"
    assert public["provider"] == "dataagent"
    assert public["config"]["url"] == "https://dataagent.example.com"
    assert public["config"]["has_token"] is True
    assert "token" not in public["config"]


def test_ontology_data_source_update_preserves_existing_secret_when_omitted(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import (
        get_ontology_data_source,
        set_ontology_data_source,
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    _create_workspace_with_ontology(tmp_path, monkeypatch)

    set_ontology_data_source(
        ontology_id="demo-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {
                "url": "https://old-dataagent.example.com/",
                "token": "secret-token",
                "timeout_seconds": 120,
            },
        },
    )

    updated = set_ontology_data_source(
        ontology_id="demo-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {
                "url": "https://new-dataagent.example.com/",
                "timeout_seconds": 30,
            },
        },
    )

    stored = get_ontology_data_source("demo-ontology", workspace_id="demo")
    assert stored is not None
    assert stored.config["url"] == "https://new-dataagent.example.com"
    assert stored.config["token"] == "secret-token"
    assert stored.config["timeout_seconds"] == 30
    assert updated.created_at == stored.created_at


def test_instantiated_channel_mode_is_rejected_until_supported(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import (
        OntologyDataSourceStoreError,
        set_ontology_data_source,
    )

    _create_workspace_with_ontology(tmp_path, monkeypatch)

    with pytest.raises(OntologyDataSourceStoreError, match="instantiated_channel.*尚未启用"):
        set_ontology_data_source(
            ontology_id="demo-ontology",
            workspace_id="demo",
            data={
                "mode": "instantiated_channel",
                "database": {
                    "engine": "mysql",
                    "host": "db.example.com",
                    "port": 3306,
                    "database": "business",
                    "username": "readonly",
                    "password": "plain-password",
                },
            },
        )


def test_delete_ontology_data_source_clears_summary(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import (
        build_ontology_business_data_summary,
        delete_ontology_data_source,
        get_ontology_data_source,
        set_ontology_data_source,
    )

    _create_workspace_with_ontology(tmp_path, monkeypatch)
    set_ontology_data_source(
        ontology_id="demo-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {"url": "https://dataagent.example.com"},
        },
    )

    assert build_ontology_business_data_summary("demo-ontology", workspace_id="demo") == {
        "configured": True,
        "ready": True,
        "mode": "direct_provider",
        "provider": "dataagent",
        "updated_at": get_ontology_data_source("demo-ontology", workspace_id="demo").updated_at,
    }

    assert delete_ontology_data_source("demo-ontology", workspace_id="demo") is True
    assert get_ontology_data_source("demo-ontology", workspace_id="demo") is None
    assert build_ontology_business_data_summary("demo-ontology", workspace_id="demo") == {
        "configured": False,
        "ready": False,
        "mode": "",
        "provider": "",
        "updated_at": "",
    }


def test_legacy_connection_binding_migration_copies_shared_connection_per_ontology(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import (
        get_ontology_data_source,
        migrate_legacy_ontology_data_sources,
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", SECRET_TEST_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch, ontology_id="sales-ontology")
    _create_ontology(paths, "service-ontology")
    (paths.workspace_dir / "data-connections.json").write_text(
        json.dumps(
            {
                "default_connection_id": "shared-dataagent",
                "connections": [
                    {
                        "id": "shared-dataagent",
                        "name": "Shared DataAgent",
                        "provider": "dataagent",
                        "config": {
                            "url": "https://dataagent.example.com/",
                            "token": "legacy-token",
                            "timeout_seconds": 60,
                        },
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    (paths.workspace_dir / "ontology-bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "sales-ontology": {"connection_id": "shared-dataagent"},
                    "service-ontology": {"connection_id": "shared-dataagent"},
                }
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")

    assert result["migrated"] == 2
    raw_text = (paths.workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert "legacy-token" not in raw_text
    assert sorted(raw["sources"]) == ["sales-ontology", "service-ontology"]
    assert raw["sources"]["sales-ontology"]["mode"] == "direct_provider"
    assert raw["sources"]["service-ontology"]["mode"] == "direct_provider"
    assert raw["sources"]["sales-ontology"]["config"] is not raw["sources"]["service-ontology"]["config"]

    sales = get_ontology_data_source("sales-ontology", workspace_id="demo")
    service = get_ontology_data_source("service-ontology", workspace_id="demo")
    assert sales is not None
    assert service is not None
    assert sales.config["token"] == "legacy-token"
    assert service.config["token"] == "legacy-token"
    assert (paths.workspace_dir / "data-connections.json").exists()
    assert (paths.workspace_dir / "ontology-bindings.json").exists()


def test_ontology_data_source_routes_manage_private_source(tmp_path, monkeypatch):
    client, headers = _build_test_client(tmp_path, monkeypatch)

    get_empty = client.get("/agentic/ontologies/demo-ontology/data-source", headers=headers)
    assert get_empty.status_code == 200
    assert get_empty.get_json() == {
        "ontology_id": "demo-ontology",
        "configured": False,
        "source": None,
    }

    put_response = client.put(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
        json={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {
                "url": "https://dataagent.example.com/",
                "token": "secret-token",
                "timeout_seconds": 120,
            },
        },
    )
    assert put_response.status_code == 200
    payload = put_response.get_json()
    assert payload["ontology_id"] == "demo-ontology"
    assert payload["configured"] is True
    assert payload["source"]["mode"] == "direct_provider"
    assert payload["source"]["provider"] == "dataagent"
    assert payload["source"]["config"]["url"] == "https://dataagent.example.com"
    assert payload["source"]["config"]["has_token"] is True
    assert "token" not in payload["source"]["config"]

    get_saved = client.get("/agentic/ontologies/demo-ontology/data-source", headers=headers)
    assert get_saved.status_code == 200
    assert get_saved.get_json()["source"]["config"]["has_token"] is True

    delete_response = client.delete("/agentic/ontologies/demo-ontology/data-source", headers=headers)
    assert delete_response.status_code == 200
    assert delete_response.get_json() == {
        "success": True,
        "ontology_id": "demo-ontology",
        "configured": False,
    }


def test_ontology_data_source_test_route_accepts_unsaved_candidate(tmp_path, monkeypatch):
    from agent.tools.business_data.resolver import ResolvedBusinessDataSource

    client, headers = _build_test_client(tmp_path, monkeypatch)
    captured = {}

    def fake_test_business_data_connection(connection):
        captured["connection"] = connection
        return {"success": True, "message": "候选配置可用", "tested_at": "2026-06-29T10:10:00+00:00"}

    monkeypatch.setattr(
        "api.routes.business_data.test_business_data_connection",
        fake_test_business_data_connection,
    )

    response = client.post(
        "/agentic/ontologies/demo-ontology/data-source/test",
        headers=headers,
        json={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {"url": "https://candidate-dataagent.example.com/", "token": "candidate-token"},
        },
    )

    assert response.status_code == 200
    assert response.get_json()["message"] == "候选配置可用"
    assert isinstance(captured["connection"], ResolvedBusinessDataSource)
    assert captured["connection"].ontology_id == "demo-ontology"
    assert captured["connection"].mode == "direct_provider"
    assert captured["connection"].provider == "dataagent"
    assert captured["connection"].config["token"] == "candidate-token"
