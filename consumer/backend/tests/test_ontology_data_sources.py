# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for ontology-scoped business data source storage."""

from __future__ import annotations

import json
import stat
from types import SimpleNamespace

import pytest
from api.auth.auth import AuthManager
from api.testing import TestApplication
from backend_synthetic_credentials import (
    ACCESS_KEY_SECRET,
    API_KEY,
    AUTH_SIGNING_SECRET,
    BOOTSTRAP_ADMIN_PASSWORD,
    ENCRYPTION_KEY,
    STRONG_PASSWORD,
    STRONG_PASSWORD_2,
    STRONG_PASSWORD_3,
)
from fastapi.testclient import TestClient
from workspace.workspaces import create_workspace, get_workspace_paths, update_workspace, workspace_scope
from internal_access import internal


def _create_workspace_with_ontology(
    tmp_path,
    monkeypatch,
    *,
    ontology_id: str = "demo-ontology",
    owner=None,
    members=None,
):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    paths = get_workspace_paths("demo")
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True, owner=owner)
    if members is not None:
        update_workspace("demo", members=members)
    _create_ontology(paths, ontology_id)
    return paths


def _create_ontology(paths, ontology_id: str) -> None:
    ontology_dir = paths.ontologies_dir / ontology_id
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                f"id: {ontology_id}",
                f"name: {ontology_id}",
                "description: ontology for data source storage",
                "version: 1.0.0",
                "language: zh-CN",
                "",
            ]
        ),
        encoding="utf-8",
    )


def _build_test_client(
    tmp_path,
    monkeypatch,
    *,
    username="admin",
    role="admin",
    workspace_owner=None,
    workspace_members=None,
):
    from api.routes.business_data import business_data_router
    from api.routes.ontologies import ontologies_router

    _create_workspace_with_ontology(
        tmp_path,
        monkeypatch,
        owner=workspace_owner,
        members=workspace_members,
    )
    app = TestApplication()
    app.state.services["TESTING"] = True

    class _DirectIO:
        async def run(self, function, *args, **kwargs):
            return function(*args, **kwargs)

    app.state.agent_execution_runtime = SimpleNamespace(
        short_io=_DirectIO(),
        http_client=object(),
    )
    auth_manager = AuthManager(str(tmp_path / "users.json"), AUTH_SIGNING_SECRET)
    if username == "admin":
        user = auth_manager.authenticate("admin", BOOTSTRAP_ADMIN_PASSWORD)
        assert user is not None
        user = auth_manager.change_password("admin", BOOTSTRAP_ADMIN_PASSWORD, STRONG_PASSWORD)
    else:
        auth_manager.create_user(username, STRONG_PASSWORD_2, role=role)
        user = auth_manager.change_password(username, STRONG_PASSWORD_2, STRONG_PASSWORD_3)
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(business_data_router)
    app.include_router(ontologies_router)
    return TestClient(app), {"Authorization": "Bearer {}".format(auth_manager.issue_token(user))}


def _legacy_connection(connection_id: str, *, token: str = API_KEY) -> dict:
    return {
        "id": connection_id,
        "name": connection_id,
        "provider": "dataagent",
        "config": {
            "url": "https://dataagent.example.com/",
            "token": token,
            "timeout_seconds": 60,
        },
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-02T00:00:00+00:00",
    }


def _write_legacy_connections(paths, *connections: dict) -> None:
    (paths.workspace_dir / "data-connections.json").write_text(
        json.dumps(
            {"default_connection_id": connections[0]["id"] if connections else "", "connections": list(connections)},
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def _manual_backup_path(source_path):
    return source_path.with_name("{}.pre-direct-v2.bak".format(source_path.name))


def _assert_private_manual_backup(source_path, expected: bytes):
    backup_path = _manual_backup_path(source_path)
    assert backup_path.read_bytes() == expected
    assert stat.S_IMODE(backup_path.stat().st_mode) == 0o600
    return backup_path


def test_migration_removes_legacy_deterministic_fields_from_neo4j_source(
    tmp_path,
    monkeypatch,
):
    from agent.tools.business_data.ontology_sources_store import get_ontology_data_source
    from agent.tools.business_data.provider_config_codec import encrypt_provider_config
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    config = {
        "uri": "neo4j://neo4j.example.net:7687",
        "database": "neo4j",
        "username": "reader",
        "password": STRONG_PASSWORD,
        "workspace_property": "d2o_workspace_id",
        "workspace_value": "demo",
        "ontology_property": "d2o_ontology_id",
        "ontology_value": "demo-ontology",
        "data_revision": "legacy-revision",
        "snapshot_selector": "neo4j",
        "access_scope_revision": "legacy-scope",
        "deterministic_read_only": True,
        "deterministic_snapshot_immutable": True,
        "deterministic_scope_isolated": True,
    }
    source_path = paths.workspace_dir / "ontology-data-sources.json"
    source_path.write_text(
        json.dumps(
            {
                "version": 2,
                "sources": {
                    "demo-ontology": {
                        "mode": "direct_provider",
                        "provider": "neo4j",
                        "config": encrypt_provider_config("neo4j", config),
                    }
                },
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")

    persisted = json.loads(source_path.read_text(encoding="utf-8"))
    persisted_config = persisted["sources"]["demo-ontology"]["config"]
    assert result["errors"] == []
    assert set(persisted_config).isdisjoint(
        config.keys()
        & {
            "data_revision",
            "snapshot_selector",
            "access_scope_revision",
            "deterministic_read_only",
            "deterministic_snapshot_immutable",
            "deterministic_scope_isolated",
        }
    )
    assert persisted_config["uri"] == "neo4j://neo4j.example.net:7687"
    assert persisted_config["password"].startswith("enc:v1:")
    stored = get_ontology_data_source("demo-ontology", workspace_id="demo")
    assert stored is not None
    assert stored.config["password"] == STRONG_PASSWORD


def test_migration_rechecks_sources_when_old_marker_predates_field_cleanup(
    tmp_path,
    monkeypatch,
):
    from agent.tools.business_data.provider_config_codec import encrypt_provider_config
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    config = {
        "uri": "neo4j://neo4j.example.net:7687",
        "database": "neo4j",
        "username": "reader",
        "password": STRONG_PASSWORD,
        "workspace_property": "d2o_workspace_id",
        "workspace_value": "demo",
        "ontology_property": "d2o_ontology_id",
        "ontology_value": "demo-ontology",
        "data_revision": "legacy-revision",
        "snapshot_selector": "neo4j",
    }
    source_path = paths.workspace_dir / "ontology-data-sources.json"
    source_path.write_text(
        json.dumps(
            {
                "version": 2,
                "sources": {
                    "demo-ontology": {
                        "mode": "direct_provider",
                        "provider": "neo4j",
                        "config": encrypt_provider_config("neo4j", config),
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").write_text(
        json.dumps(
            {
                "migration_id": "ontology-data-sources-direct-v2",
                "version": 2,
                "errors": [],
            }
        ),
        encoding="utf-8",
    )

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")

    assert result["errors"] == []
    persisted = json.loads(source_path.read_text(encoding="utf-8"))
    assert "data_revision" not in persisted["sources"]["demo-ontology"]["config"]
    assert "snapshot_selector" not in persisted["sources"]["demo-ontology"]["config"]


def test_ontology_data_source_encrypts_and_publicizes_secret_fields(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import (
        get_ontology_data_source,
        publicize_ontology_data_source,
        set_ontology_data_source,
    )

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
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
                    "token": API_KEY,
                    "timeout_seconds": 120,
                },
            },
        )

    raw_text = (paths.workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert raw["version"] == 2
    assert raw["sources"]["demo-ontology"]["mode"] == "direct_provider"
    assert API_KEY not in raw_text
    assert raw["sources"]["demo-ontology"]["config"]["token"].startswith("enc:v1:")

    stored = get_ontology_data_source("demo-ontology", workspace_id="demo")
    assert stored is not None
    assert stored.provider == "dataagent"
    assert stored.config["url"] == "https://dataagent.example.com"
    assert stored.config["token"] == API_KEY
    assert stored.config["timeout_seconds"] == 120

    public = publicize_ontology_data_source(source)
    assert public["mode"] == "direct_provider"
    assert public["provider"] == "dataagent"
    assert public["config"]["has_token"] is True
    assert "token" not in public["config"]

    (paths.workspace_dir / "ontology-bindings.json").write_text("{damaged-after-marker", encoding="utf-8")
    assert get_ontology_data_source("demo-ontology", workspace_id="demo").provider == "dataagent"


def test_ontology_data_source_update_preserves_existing_secret_when_omitted(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import get_ontology_data_source, set_ontology_data_source

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    _create_workspace_with_ontology(tmp_path, monkeypatch)
    set_ontology_data_source(
        ontology_id="demo-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {
                "url": "https://old-dataagent.example.com/",
                "token": API_KEY,
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
            "config": {"url": "https://new-dataagent.example.com/", "timeout_seconds": 30},
        },
    )

    stored = get_ontology_data_source("demo-ontology", workspace_id="demo")
    assert stored is not None
    assert stored.config["url"] == "https://new-dataagent.example.com"
    assert stored.config["token"] == API_KEY
    assert stored.config["timeout_seconds"] == 30
    assert updated.created_at == stored.created_at


def test_connection_ref_writes_are_rejected(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import OntologyDataSourceStoreError, set_ontology_data_source

    _create_workspace_with_ontology(tmp_path, monkeypatch)
    with pytest.raises(OntologyDataSourceStoreError, match="不支持.*connection_ref"):
        set_ontology_data_source(
            ontology_id="demo-ontology",
            workspace_id="demo",
            data={"mode": "connection_ref", "connection_id": "shared"},
        )


@pytest.mark.parametrize(
    ("document", "error_pattern"),
    [
        ({"version": 1, "sources": {}}, "版本不受支持"),
        (
            {
                "version": 2,
                "sources": {
                    "demo-ontology": {
                        "mode": "connection_ref",
                        "connection_id": "shared",
                    }
                },
            },
            "格式无效.*mode 必须为 direct_provider",
        ),
    ],
)
def test_runtime_read_rejects_noncanonical_documents(
    tmp_path,
    monkeypatch,
    document,
    error_pattern,
):
    from agent.tools.business_data.ontology_sources_store import (
        OntologyDataSourceStoreError,
        get_ontology_data_source,
    )

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    (paths.workspace_dir / "ontology-data-sources.json").write_text(
        json.dumps(document),
        encoding="utf-8",
    )

    with pytest.raises(OntologyDataSourceStoreError, match=error_pattern):
        get_ontology_data_source("demo-ontology", workspace_id="demo")


def test_instantiated_channel_mode_is_rejected_until_supported(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import OntologyDataSourceStoreError, set_ontology_data_source

    _create_workspace_with_ontology(tmp_path, monkeypatch)
    with pytest.raises(OntologyDataSourceStoreError, match="instantiated_channel.*尚未启用"):
        set_ontology_data_source(
            ontology_id="demo-ontology",
            workspace_id="demo",
            data={"mode": "instantiated_channel", "database": {"engine": "mysql"}},
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
    assert build_ontology_business_data_summary("demo-ontology", workspace_id="demo") == {
        "configured": False,
        "ready": False,
        "mode": "",
        "provider": "",
        "updated_at": "",
    }


def test_legacy_bindings_migrate_to_independent_direct_sources(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import get_ontology_data_source, set_ontology_data_source
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch, ontology_id="sales-ontology")
    _create_ontology(paths, "service-ontology")
    _write_legacy_connections(paths, _legacy_connection("shared-dataagent"))
    connection_path = paths.workspace_dir / "data-connections.json"
    bindings_path = paths.workspace_dir / "ontology-bindings.json"
    connection_bytes = connection_path.read_bytes()
    bindings_path.write_text(
        json.dumps(
            {
                "bindings": {
                    "sales-ontology": {"connection_id": "shared-dataagent"},
                    "service-ontology": {"connection_id": "shared-dataagent"},
                }
            }
        ),
        encoding="utf-8",
    )
    bindings_bytes = bindings_path.read_bytes()
    expected_backups = [_manual_backup_path(connection_path), _manual_backup_path(bindings_path)]

    preview = migrate_legacy_ontology_data_sources(workspace_id="demo", dry_run=True)
    assert preview["migrated"] == 2
    assert preview["removed_legacy_files"] == 2
    assert preview["backup_paths"] == [str(path) for path in expected_backups]
    assert not (paths.workspace_dir / "ontology-data-sources.json").exists()
    assert connection_path.read_bytes() == connection_bytes
    assert not any(path.exists() for path in expected_backups)

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert result["migrated"] == 2
    raw_text = (paths.workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")
    raw = json.loads(raw_text)
    assert raw["version"] == 2
    assert API_KEY not in raw_text
    for ontology_id in ("sales-ontology", "service-ontology"):
        assert raw["sources"][ontology_id]["mode"] == "direct_provider"
        assert raw["sources"][ontology_id]["provider"] == "dataagent"
        assert "connection_id" not in raw["sources"][ontology_id]
        assert raw["sources"][ontology_id]["config"]["token"].startswith("enc:v1:")

    assert result["removed_legacy_files"] == 2
    assert result["backup_paths"] == [str(path) for path in expected_backups]
    _assert_private_manual_backup(connection_path, connection_bytes)
    _assert_private_manual_backup(bindings_path, bindings_bytes)
    assert not connection_path.exists()
    assert not bindings_path.exists()
    marker_path = paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json"
    assert marker_path.is_file()
    assert json.loads(marker_path.read_text(encoding="utf-8"))["manual_recovery_backups"] == [
        path.name for path in expected_backups
    ]
    assert not list(paths.workspace_dir.glob("*.tmp"))

    set_ontology_data_source(
        ontology_id="sales-ontology",
        workspace_id="demo",
        data={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {"url": "https://sales.example.com"},
        },
    )
    assert get_ontology_data_source("sales-ontology", workspace_id="demo").config["url"] == "https://sales.example.com"
    assert (
        get_ontology_data_source("service-ontology", workspace_id="demo").config["url"]
        == "https://dataagent.example.com"
    )

    repeated = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert repeated["migrated"] == 0
    assert repeated["skipped"] is True
    assert repeated["backup_paths"] == [str(path) for path in expected_backups]


def test_existing_connection_ref_source_is_migrated_in_place(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import get_ontology_data_source
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(paths, _legacy_connection("shared-dataagent", token=ACCESS_KEY_SECRET))
    connection_path = paths.workspace_dir / "data-connections.json"
    connection_bytes = connection_path.read_bytes()
    source_path = paths.workspace_dir / "ontology-data-sources.json"
    source_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "demo-ontology": {
                        "mode": "connection_ref",
                        "connection_id": "shared-dataagent",
                        "created_at": "2026-01-03T00:00:00+00:00",
                        "updated_at": "2026-01-03T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    source_bytes = source_path.read_bytes()

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert result["migrated"] == 1
    raw_text = source_path.read_text(encoding="utf-8")
    raw_source = json.loads(raw_text)["sources"]["demo-ontology"]
    assert raw_source["mode"] == "direct_provider"
    assert raw_source["provider"] == "dataagent"
    assert "connection_id" not in raw_source
    assert ACCESS_KEY_SECRET not in raw_text
    assert result["removed_legacy_files"] == 1
    assert result["backup_paths"] == [
        str(_manual_backup_path(source_path)),
        str(_manual_backup_path(connection_path)),
    ]
    _assert_private_manual_backup(source_path, source_bytes)
    _assert_private_manual_backup(connection_path, connection_bytes)
    assert not connection_path.exists()
    assert get_ontology_data_source("demo-ontology", workspace_id="demo").config["token"] == ACCESS_KEY_SECRET


def test_forward_only_migration_discards_unbound_legacy_connections(tmp_path, monkeypatch):
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(paths, _legacy_connection("unused-connection"))
    connection_path = paths.workspace_dir / "data-connections.json"
    connection_bytes = connection_path.read_bytes()
    backup_path = _manual_backup_path(connection_path)

    preview = migrate_legacy_ontology_data_sources(workspace_id="demo", dry_run=True)
    assert preview["migrated"] == 0
    assert preview["removed_legacy_files"] == 1
    assert preview["backup_paths"] == [str(backup_path)]
    assert connection_path.is_file()
    assert not backup_path.exists()

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert result["migrated"] == 0
    assert result["skipped"] is False
    assert result["removed_legacy_files"] == 1
    assert result["backup_paths"] == [str(backup_path)]
    _assert_private_manual_backup(connection_path, connection_bytes)
    assert not connection_path.exists()
    assert json.loads((paths.workspace_dir / "ontology-data-sources.json").read_text(encoding="utf-8")) == {
        "version": 2,
        "sources": {},
    }


def test_completed_migration_no_longer_depends_on_legacy_files(tmp_path, monkeypatch):
    from agent.tools.business_data.ontology_sources_store import get_ontology_data_source
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(paths, _legacy_connection("shared-dataagent"))
    legacy_bindings_path = paths.workspace_dir / "ontology-bindings.json"
    legacy_bindings_path.write_text(
        json.dumps({"bindings": {"demo-ontology": {"connection_id": "shared-dataagent"}}}),
        encoding="utf-8",
    )

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert result["migrated"] == 1
    legacy_bindings_path.write_text("{damaged-after-migration", encoding="utf-8")

    source = get_ontology_data_source("demo-ontology", workspace_id="demo")
    assert source is not None
    assert source.mode == "direct_provider"
    assert source.provider == "dataagent"


def test_validation_failure_preserves_legacy_files_and_is_retryable(tmp_path, monkeypatch):
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch, ontology_id="sales-ontology")
    _create_ontology(paths, "service-ontology")
    _write_legacy_connections(paths, _legacy_connection("available"))
    (paths.workspace_dir / "ontology-bindings.json").write_text(
        json.dumps(
            {
                "bindings": {
                    "sales-ontology": {"connection_id": "available"},
                    "service-ontology": {"connection_id": "missing"},
                }
            }
        ),
        encoding="utf-8",
    )

    first = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert first["migrated"] == 0
    assert first["candidate_migrated"] == 1
    assert len(first["errors"]) == 1
    assert not (paths.workspace_dir / "ontology-data-sources.json").exists()
    assert (paths.workspace_dir / "data-connections.json").is_file()
    assert (paths.workspace_dir / "ontology-bindings.json").is_file()
    assert not (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()

    _write_legacy_connections(paths, _legacy_connection("available"), _legacy_connection("missing"))
    second = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert second["migrated"] == 2
    assert second["errors"] == []


def test_migration_preserves_invalid_json_for_recovery(tmp_path, monkeypatch):
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    legacy_path = paths.workspace_dir / "ontology-bindings.json"
    legacy_path.write_text("{invalid-json", encoding="utf-8")

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")
    assert result["errors"] == ["旧本体绑定文件无法解析"]
    assert legacy_path.read_text(encoding="utf-8") == "{invalid-json"
    assert result["removed_legacy_files"] == 0
    assert not (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()


def test_migration_encrypts_preserved_direct_provider_secret(tmp_path, monkeypatch):
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    monkeypatch.setenv("SECRET_ENCRYPTION_KEY", ENCRYPTION_KEY)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    target_path = paths.workspace_dir / "ontology-data-sources.json"
    target_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "demo-ontology": {
                        "mode": "direct_provider",
                        "provider": "dataagent",
                        "config": {"url": "https://dataagent.example.com", "token": API_KEY},
                        "created_at": "2026-01-01T00:00:00+00:00",
                        "updated_at": "2026-01-01T00:00:00+00:00",
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    result = migrate_legacy_ontology_data_sources(workspace_id="demo")
    raw_text = target_path.read_text(encoding="utf-8")
    assert result["errors"] == []
    assert result["preserved"] == 1
    assert API_KEY not in raw_text
    assert json.loads(raw_text)["version"] == 2
    assert json.loads(raw_text)["sources"]["demo-ontology"]["config"]["token"].startswith("enc:v1:")


def test_migration_dry_run_does_not_create_encryption_key_or_marker(tmp_path, monkeypatch):
    from migrations.ontology_data_sources_direct_v2 import migrate_legacy_ontology_data_sources

    monkeypatch.delenv("SECRET_ENCRYPTION_KEY", raising=False)
    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    target_path = paths.workspace_dir / "ontology-data-sources.json"
    target_path.write_text(
        json.dumps(
            {
                "version": 1,
                "sources": {
                    "demo-ontology": {
                        "mode": "direct_provider",
                        "provider": "dataagent",
                        "config": {
                            "url": "https://dataagent.example.com",
                            "token": ACCESS_KEY_SECRET,
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    before = target_path.read_bytes()

    result = migrate_legacy_ontology_data_sources(workspace_id="demo", dry_run=True)

    assert result["errors"] == []
    assert result["dry_run"] is True
    assert result["backup_paths"] == [str(_manual_backup_path(target_path))]
    assert target_path.read_bytes() == before
    assert not _manual_backup_path(target_path).exists()
    assert not (tmp_path / ".onto-runtime" / "config" / "secret.key").exists()
    assert not (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()


def test_explicit_data_source_migration_apply_requires_stopped_writes(tmp_path):
    from migrations.ontology_data_sources_direct_v2 import execute_migration

    code, report = execute_migration(
        tmp_path / ".onto-runtime",
        dry_run=False,
        writes_stopped=False,
        from_version="old",
        to_version="new",
    )

    assert code == 2
    assert report["ok"] is False
    assert report["error"]["code"] == "WRITES_NOT_STOPPED"


def test_apply_precheck_prevents_partial_migration_across_workspaces(tmp_path, monkeypatch):
    from migrations.ontology_data_sources_direct_v2 import execute_migration

    demo_paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(demo_paths, _legacy_connection("available"))
    (demo_paths.workspace_dir / "ontology-bindings.json").write_text(
        json.dumps({"bindings": {"demo-ontology": {"connection_id": "available"}}}),
        encoding="utf-8",
    )

    create_workspace("Broken Workspace", workspace_id="broken")
    broken_paths = get_workspace_paths("broken")
    _create_ontology(broken_paths, "broken-ontology")
    (broken_paths.workspace_dir / "ontology-bindings.json").write_text(
        json.dumps({"bindings": {"broken-ontology": {"connection_id": "missing"}}}),
        encoding="utf-8",
    )

    code, report = execute_migration(
        tmp_path / ".onto-runtime",
        dry_run=False,
        writes_stopped=True,
        from_version="old",
        to_version="new",
    )

    assert code == 2
    assert report["phase"] == "apply_precheck"
    assert not (demo_paths.workspace_dir / "ontology-data-sources.json").exists()
    assert not (demo_paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()
    assert (demo_paths.workspace_dir / "data-connections.json").is_file()
    assert (demo_paths.workspace_dir / "ontology-bindings.json").is_file()


def test_manual_backup_failure_blocks_source_mutation(tmp_path, monkeypatch):
    import migrations.ontology_data_sources_direct_v2 as migration_module

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(paths, _legacy_connection("available"))
    connection_path = paths.workspace_dir / "data-connections.json"
    bindings_path = paths.workspace_dir / "ontology-bindings.json"
    bindings_path.write_text(
        json.dumps({"bindings": {"demo-ontology": {"connection_id": "available"}}}),
        encoding="utf-8",
    )
    connection_bytes = connection_path.read_bytes()
    bindings_bytes = bindings_path.read_bytes()
    original_write = internal(migration_module).write_bytes_atomic

    def fail_backup(path, payload, *, mode=None):
        if path.name.endswith(migration_module.MANUAL_BACKUP_SUFFIX):
            raise OSError("backup denied")
        return original_write(path, payload, mode=mode)

    monkeypatch.setattr(migration_module, "_write_bytes_atomic", fail_backup)
    code, report = migration_module.execute_migration(
        tmp_path / ".onto-runtime",
        dry_run=False,
        writes_stopped=True,
    )

    assert code == 2
    assert report["phase"] == "apply"
    assert any("创建人工恢复备份失败" in error for error in report["errors"])
    assert connection_path.read_bytes() == connection_bytes
    assert bindings_path.read_bytes() == bindings_bytes
    assert not (paths.workspace_dir / "ontology-data-sources.json").exists()
    assert not list(paths.workspace_dir.glob("*.pre-direct-v2.bak"))
    assert not (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()


def test_forward_only_cleanup_failure_blocks_completion_and_is_retryable(tmp_path, monkeypatch):
    import migrations.ontology_data_sources_direct_v2 as migration_module

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(paths, _legacy_connection("available"))
    connection_path = paths.workspace_dir / "data-connections.json"
    bindings_path = paths.workspace_dir / "ontology-bindings.json"
    connection_bytes = connection_path.read_bytes()
    bindings_path.write_text(
        json.dumps({"bindings": {"demo-ontology": {"connection_id": "available"}}}),
        encoding="utf-8",
    )
    bindings_bytes = bindings_path.read_bytes()
    original_remove = internal(migration_module).remove_legacy_files

    def fail_cleanup(_paths):
        raise OSError("cleanup denied")

    monkeypatch.setattr(migration_module, "_remove_legacy_files", fail_cleanup)
    code, report = migration_module.execute_migration(
        tmp_path / ".onto-runtime",
        dry_run=False,
        writes_stopped=True,
    )

    assert code == 2
    assert report["phase"] == "apply"
    assert any("cleanup denied" in error for error in report["errors"])
    assert (paths.workspace_dir / "ontology-data-sources.json").is_file()
    assert connection_path.is_file()
    assert bindings_path.is_file()
    connection_backup = _assert_private_manual_backup(connection_path, connection_bytes)
    bindings_backup = _assert_private_manual_backup(bindings_path, bindings_bytes)
    assert report["workspaces"]["demo"]["backup_paths"] == [
        str(connection_backup),
        str(bindings_backup),
    ]
    assert not (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()

    monkeypatch.setattr(migration_module, "_remove_legacy_files", original_remove)
    retry_code, retry_report = migration_module.execute_migration(
        tmp_path / ".onto-runtime",
        dry_run=False,
        writes_stopped=True,
    )

    assert retry_code == 0
    assert retry_report["ok"] is True
    assert connection_backup.read_bytes() == connection_bytes
    assert bindings_backup.read_bytes() == bindings_bytes
    assert not connection_path.exists()
    assert not bindings_path.exists()
    assert (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").is_file()


def test_forward_only_verification_failure_keeps_legacy_files(tmp_path, monkeypatch):
    import migrations.ontology_data_sources_direct_v2 as migration_module

    paths = _create_workspace_with_ontology(tmp_path, monkeypatch)
    _write_legacy_connections(paths, _legacy_connection("available"))
    (paths.workspace_dir / "ontology-bindings.json").write_text(
        json.dumps({"bindings": {"demo-ontology": {"connection_id": "available"}}}),
        encoding="utf-8",
    )

    def fail_verification(_paths, _expected):
        raise migration_module.OntologyDataSourceMigrationError("verification failed")

    monkeypatch.setattr(migration_module, "_verify_canonical_document", fail_verification)
    code, report = migration_module.execute_migration(
        tmp_path / ".onto-runtime",
        dry_run=False,
        writes_stopped=True,
    )

    assert code == 2
    assert any("verification failed" in error for error in report["errors"])
    assert (paths.workspace_dir / "data-connections.json").is_file()
    assert (paths.workspace_dir / "ontology-bindings.json").is_file()
    assert not (paths.workspace_dir / "ontology-data-sources.direct-v2.migration-complete.json").exists()


def test_ontology_data_source_routes_manage_private_source(tmp_path, monkeypatch):
    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        username="modeler",
        role="user",
        workspace_owner="modeler",
    )
    get_empty = client.get("/agentic/ontologies/demo-ontology/data-source", headers=headers)
    assert get_empty.status_code == 200
    assert get_empty.json() == {"ontology_id": "demo-ontology", "configured": False, "source": None}

    put_response = client.put(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
        json={
            "mode": "direct_provider",
            "provider": "dataagent",
            "config": {
                "url": "https://dataagent.example.com/",
                "token": API_KEY,
                "timeout_seconds": 120,
            },
        },
    )
    assert put_response.status_code == 200
    payload = put_response.json()
    assert payload["configured"] is True
    assert payload["source"]["provider"] == "dataagent"
    assert payload["source"]["config"]["has_token"] is True
    assert "token" not in payload["source"]["config"]

    delete_response = client.delete("/agentic/ontologies/demo-ontology/data-source", headers=headers)
    assert delete_response.status_code == 200
    assert delete_response.json()["configured"] is False


@pytest.mark.parametrize(
    ("username", "role", "workspace_owner"),
    [
        ("admin", "admin", None),
        ("modeler", "user", "modeler"),
    ],
)
def test_ontology_data_source_test_route_accepts_unsaved_candidate(
    tmp_path,
    monkeypatch,
    username,
    role,
    workspace_owner,
):
    from agent.tools.business_data.resolver import ResolvedBusinessDataSource

    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        username=username,
        role=role,
        workspace_owner=workspace_owner,
    )
    captured = {}

    def fake_test_business_data_connection(connection):
        captured["connection"] = connection
        return {
            "success": True,
            "message": "候选配置可用",
            "tested_at": "2026-06-29T10:10:00+00:00",
        }

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
            "config": {"url": "https://candidate.example.com/", "token": API_KEY},
        },
    )
    assert response.status_code == 200
    assert response.json()["message"] == "候选配置可用"
    assert isinstance(captured["connection"], ResolvedBusinessDataSource)
    assert captured["connection"].ontology_id == "demo-ontology"
    assert captured["connection"].provider == "dataagent"
    assert captured["connection"].config["token"] == API_KEY


def test_ontology_data_source_write_routes_reject_workspace_member(tmp_path, monkeypatch):
    client, headers = _build_test_client(
        tmp_path,
        monkeypatch,
        username="modeler",
        role="user",
        workspace_owner="alice",
        workspace_members=["modeler"],
    )
    read_response = client.get(
        "/agentic/ontologies/demo-ontology/data-source",
        headers=headers,
    )
    assert read_response.status_code == 200

    responses = [
        client.put(
            "/agentic/ontologies/demo-ontology/data-source",
            headers=headers,
            json={
                "mode": "direct_provider",
                "provider": "dataagent",
                "config": {"url": "https://candidate.example.com/"},
            },
        ),
        client.delete(
            "/agentic/ontologies/demo-ontology/data-source",
            headers=headers,
        ),
        client.post(
            "/agentic/ontologies/demo-ontology/data-source/test",
            headers=headers,
            json={
                "mode": "direct_provider",
                "provider": "dataagent",
                "config": {"url": "https://candidate.example.com/"},
            },
        ),
    ]
    for response in responses:
        assert response.status_code == 403
        assert response.json() == {
            "error": "Forbidden",
            "code": "WORKSPACE_ACCESS_DENIED",
        }
