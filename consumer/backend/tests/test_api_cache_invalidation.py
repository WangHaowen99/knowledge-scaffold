"""Real API read-after-write coverage for metadata view caches."""

from __future__ import annotations

import io

import yaml
from api.routes import ontologies as ontologies_routes
from ontology.view_cache import OntologyViewCache
from test_ontology_routes import _build_test_client, _create_demo_workspace, _post
from test_workspace_authz import _build_env, _create_workspace_api


def _write_versioned_ontology(workspace_paths, ontology_id: str = "cache-demo"):
    ontology_dir = workspace_paths.ontologies_dir / ontology_id
    version_one = ontology_dir / "versions" / "1.0.0"
    version_two = ontology_dir / "versions" / "2.0.0"
    (version_one / "objects").mkdir(parents=True)
    (version_two / "objects").mkdir(parents=True)
    (version_one / "objects" / "VersionOne.md").write_text(
        "# Version one\n",
        encoding="utf-8",
    )
    (version_two / "objects" / "VersionTwo.md").write_text(
        "# Version two\n",
        encoding="utf-8",
    )
    (ontology_dir / "manifest.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ontology-package",
                "schema_version": 0.1,
                "id": ontology_id,
                "name": "Cache Demo",
                "description": "Synthetic cache invalidation package",
                "version": "1.0.0",
                "active_version": "1.0.0",
                "versions": ["1.0.0", "2.0.0"],
                "language": "en-US",
                "enabled": True,
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return ontology_dir


def _tree_names(payload: dict) -> set[str]:
    names: set[str] = set()

    def visit(entries):
        for entry in entries:
            names.add(entry["name"])
            visit(entry.get("children") or [])

    visit(payload["entries"])
    return names


def _ontology_by_id(payload: dict, ontology_id: str) -> dict:
    return next(item for item in payload["ontologies"] if item["id"] == ontology_id)


def test_ontology_file_apis_publish_immediately_after_warm_tree_cache(
    tmp_path,
    monkeypatch,
):
    import ontology.view_cache as view_cache_module

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    _write_versioned_ontology(workspace_paths)
    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8, check_interval_seconds=3600),
    )
    builds = 0
    original_build = ontologies_routes.build_package_tree

    def counted_build(*args, **kwargs):
        nonlocal builds
        builds += 1
        return original_build(*args, **kwargs)

    monkeypatch.setattr(ontologies_routes, "build_package_tree", counted_build)
    client, headers = _build_test_client(tmp_path)
    tree_path = "/agentic/ontologies/cache-demo/tree"

    assert client.get(tree_path, headers=headers).status_code == 200
    assert client.get(tree_path, headers=headers).status_code == 200
    assert builds == 1

    edited = client.put(
        "/agentic/ontologies/cache-demo/file",
        headers=headers,
        json={
            "folder": "objects",
            "name": "VersionOne.md",
            "content": "# Edited through API\n",
        },
    )
    assert edited.status_code == 200, edited.json()
    edit_readback = client.get(
        "/agentic/ontologies/cache-demo/file",
        params={"folder": "objects", "name": "VersionOne.md"},
        headers=headers,
    )
    assert edit_readback.status_code == 200
    assert edit_readback.json()["content"] == "# Edited through API\n"
    assert client.get(tree_path, headers=headers).status_code == 200
    assert builds == 2

    uploaded = _post(
        client,
        "/agentic/ontologies/cache-demo/upload",
        headers=headers,
        data={
            "folder": "objects",
            "file": (io.BytesIO(b"# Uploaded through API\n"), "Uploaded.md"),
        },
    )
    assert uploaded.status_code == 201, uploaded.json()
    uploaded_tree = client.get(tree_path, headers=headers)
    assert uploaded_tree.status_code == 200
    assert "Uploaded.md" in _tree_names(uploaded_tree.json())
    assert builds == 3


def test_ontology_manifest_apis_publish_immediately_after_warm_views(
    tmp_path,
    monkeypatch,
):
    import ontology.view_cache as view_cache_module

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    _write_versioned_ontology(workspace_paths)
    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8, check_interval_seconds=3600),
    )
    client, headers = _build_test_client(tmp_path)
    list_path = "/agentic/ontologies"
    tree_path = "/agentic/ontologies/cache-demo/tree"

    for _ in range(2):
        assert client.get(list_path, headers=headers).status_code == 200
        assert client.get(tree_path, headers=headers).status_code == 200

    disabled = client.put(
        "/agentic/ontologies/cache-demo/status",
        headers=headers,
        json={"enabled": False},
    )
    assert disabled.status_code == 200, disabled.json()
    assert _ontology_by_id(client.get(list_path, headers=headers).json(), "cache-demo")["enabled"] is False

    enabled = client.put(
        "/agentic/ontologies/cache-demo/status",
        headers=headers,
        json={"enabled": True},
    )
    assert enabled.status_code == 200, enabled.json()
    assert _ontology_by_id(client.get(list_path, headers=headers).json(), "cache-demo")["enabled"] is True

    activated = client.put(
        "/agentic/ontologies/cache-demo/active-version",
        headers=headers,
        json={"version": "2.0.0"},
    )
    assert activated.status_code == 200, activated.json()
    active_readback = _ontology_by_id(
        client.get(list_path, headers=headers).json(),
        "cache-demo",
    )
    assert active_readback["active_version"] == "2.0.0"
    active_tree = client.get(tree_path, headers=headers)
    assert active_tree.status_code == 200
    assert active_tree.json()["version"] == "2.0.0"
    assert "VersionTwo.md" in _tree_names(active_tree.json())
    assert "VersionOne.md" not in _tree_names(active_tree.json())

    deleted = client.delete(
        "/agentic/ontologies/cache-demo",
        headers=headers,
    )
    assert deleted.status_code == 200, deleted.json()
    deleted_list = client.get(list_path, headers=headers)
    assert deleted_list.status_code == 200
    assert all(item["id"] != "cache-demo" for item in deleted_list.json()["ontologies"])
    assert client.get(tree_path, headers=headers).status_code == 404


def test_workspace_apis_publish_immediately_after_each_warm_catalog_read(
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("PERF_WORKSPACE_SNAPSHOT_ENABLED", "true")
    client, headers = _build_env(tmp_path, monkeypatch)
    _create_workspace_api(
        client,
        headers["admin"],
        name="Shared",
        workspace_id="shared",
        owner=None,
    )

    for _ in range(2):
        warm = client.get("/agentic/workspaces", headers=headers["admin"])
        assert warm.status_code == 200
        assert [item["id"] for item in warm.json()["workspaces"]] == ["shared"]

    created = client.post(
        "/agentic/workspaces",
        json={"name": "Cache Target", "id": "cache-target", "owner": "alice"},
        headers=headers["admin"],
    )
    assert created.status_code == 201, created.json()
    after_create = client.get("/agentic/workspaces", headers=headers["admin"]).json()
    assert {item["id"] for item in after_create["workspaces"]} == {
        "shared",
        "cache-target",
    }

    updated = client.patch(
        "/agentic/workspaces/cache-target",
        json={"name": "Cache Target Updated"},
        headers=headers["alice"],
    )
    assert updated.status_code == 200, updated.json()
    updated_get = client.get(
        "/agentic/workspaces/cache-target",
        headers=headers["alice"],
    )
    assert updated_get.status_code == 200
    assert updated_get.json()["name"] == "Cache Target Updated"

    members = client.put(
        "/agentic/workspaces/cache-target/members",
        json={"members": ["bob"]},
        headers=headers["alice"],
    )
    assert members.status_code == 200, members.json()
    member_list = client.get("/agentic/workspaces", headers=headers["bob"]).json()
    member_item = next(item for item in member_list["workspaces"] if item["id"] == "cache-target")
    assert member_item["members"] == ["bob"]
    assert member_item["role"] == "member"

    made_default = client.post(
        "/agentic/workspaces/cache-target/default",
        headers=headers["admin"],
    )
    assert made_default.status_code == 200, made_default.json()
    default_list = client.get("/agentic/workspaces", headers=headers["admin"]).json()
    assert default_list["default_workspace_id"] == "cache-target"
    assert next(item for item in default_list["workspaces"] if item["id"] == "cache-target")["is_default"] is True

    restored_default = client.post(
        "/agentic/workspaces/shared/default",
        headers=headers["admin"],
    )
    assert restored_default.status_code == 200, restored_default.json()
    assert client.get("/agentic/workspaces", headers=headers["admin"]).json()["default_workspace_id"] == "shared"

    deleted = client.delete(
        "/agentic/workspaces/cache-target",
        headers=headers["admin"],
    )
    assert deleted.status_code == 200, deleted.json()
    after_delete = client.get("/agentic/workspaces", headers=headers["admin"]).json()
    remaining_ids = [item["id"] for item in after_delete["workspaces"]]
    assert remaining_ids[0] == "shared"
    assert len(remaining_ids) == 2
    assert remaining_ids[1].startswith("home-")
    assert "cache-target" not in remaining_ids
    assert (
        client.get(
            "/agentic/workspaces/cache-target",
            headers=headers["admin"],
        ).status_code
        == 404
    )
