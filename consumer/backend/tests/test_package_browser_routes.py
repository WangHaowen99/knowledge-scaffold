"""Route integration tests for the shared ontology package browser."""

from __future__ import annotations

import io
import warnings
import zipfile

import pytest
import yaml
from flask import Flask

from api.auth.auth import AuthManager
from api.routes import ontologies as ontologies_module
from api.routes.ontologies import ontologies_bp
from workspace.workspaces import create_workspace, get_workspace_paths


def _build_client(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)
    paths = get_workspace_paths("demo")
    ontology_dir = paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "operations").mkdir(parents=True)
    (version1 / "assets" / "nested").mkdir(parents=True)
    (version2 / "operations").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        "\n".join(
            [
                "kind: ontology-package",
                "schema_version: 0.1",
                "id: demo-ontology",
                "name: Demo Ontology",
                "description: Package browser route tests",
                "version: 1.0.0",
                "active_version: 1.0.0",
                "versions:",
                "  - 1.0.0",
                "  - 2.0.0",
                "language: zh-CN",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (version1 / "operations" / "流程.md").write_text("v1 flow\n", encoding="utf-8")
    (version2 / "operations" / "流程.md").write_text("v2 flow\n", encoding="utf-8")
    (version1 / "assets" / "nested" / "guide.txt").write_text("guide\n", encoding="utf-8")
    (version1 / "assets" / ".env").write_text("KEY=value\n", encoding="utf-8")
    (version1 / "dataset").mkdir()
    (version1 / "dataset" / "secret.txt").write_text("secret\n", encoding="utf-8")
    (version1 / ".hidden").mkdir()
    (version1 / ".hidden" / "secret.md").write_text("secret\n", encoding="utf-8")

    app = Flask(__name__)
    app.config["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    user = auth_manager.authenticate("admin", "TestBootstrap#2026")
    assert user is not None
    user = auth_manager.change_password("admin", "TestBootstrap#2026", "RootStrong123!")
    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(ontologies_bp)
    client = app.test_client()
    headers = {"Authorization": "Bearer {}".format(auth_manager.issue_token(user))}
    return client, headers, version1


def _flatten_paths(entries):
    paths = []
    for entry in entries:
        paths.append(entry["path"])
        paths.extend(_flatten_paths(entry.get("children") or []))
    return paths


def _zip_from_entries(entries):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for name, content in entries:
                archive.writestr(name, content)
    buffer.seek(0)
    return buffer


def test_tree_and_raw_file_share_safe_versioned_browser(tmp_path, monkeypatch):
    client, headers, version1 = _build_client(tmp_path, monkeypatch)
    (version1 / "file-link.md").symlink_to(version1 / "operations" / "流程.md")

    tree_response = client.get(
        "/agentic/ontologies/demo-ontology/tree",
        headers=headers,
    )
    raw_response = client.get(
        "/agentic/ontologies/demo-ontology/raw-file",
        query_string={"path": "operations/流程.md", "version": "2.0.0"},
        headers=headers,
    )

    assert tree_response.status_code == 200
    tree_paths = _flatten_paths(tree_response.get_json()["entries"])
    assert "operations/流程.md" in tree_paths
    assert "dataset" not in tree_paths
    assert ".hidden" not in tree_paths
    assert "file-link.md" not in tree_paths
    assert raw_response.status_code == 200
    assert raw_response.get_json()["content"] == "v2 flow\n"


def test_raw_file_rejects_traversal_symlink_binary_and_oversize(tmp_path, monkeypatch):
    client, headers, version1 = _build_client(tmp_path, monkeypatch)
    (version1 / "linked.md").symlink_to(version1 / "operations" / "流程.md")
    (version1 / "binary.txt").write_bytes(b"text\x00binary")
    (version1 / "large.txt").write_bytes(b"x" * (2 * 1024 * 1024 + 1))

    cases = {
        "..\\manifest.yaml": (400, "Invalid file path"),
        "linked.md": (400, "Invalid file path"),
        "binary.txt": (400, "Binary file is not viewable"),
        "large.txt": (400, "File too large to view online"),
    }
    for path, (status_code, error) in cases.items():
        response = client.get(
            "/agentic/ontologies/demo-ontology/raw-file",
            query_string={"path": path},
            headers=headers,
        )
        assert response.status_code == status_code
        assert response.get_json()["error"] == error


def test_all_package_read_routes_reject_symlinked_version_directory(tmp_path, monkeypatch):
    client, headers, version1 = _build_client(tmp_path, monkeypatch)
    outside_version = tmp_path / "outside-version"
    version1.rename(outside_version)
    version1.symlink_to(outside_version, target_is_directory=True)

    responses = [
        client.get(
            "/agentic/ontologies/demo-ontology/tree",
            headers=headers,
        ),
        client.get(
            "/agentic/ontologies/demo-ontology/raw-file",
            query_string={"path": "operations/流程.md"},
            headers=headers,
        ),
        client.get(
            "/agentic/ontologies/demo-ontology/files",
            query_string={"folder": "operations"},
            headers=headers,
        ),
        client.get(
            "/agentic/ontologies/demo-ontology/file",
            query_string={"folder": "operations", "name": "流程.md"},
            headers=headers,
        ),
    ]

    assert [response.status_code for response in responses] == [400, 400, 400, 400]
    assert all(response.get_json()["error"] == "Invalid file path" for response in responses)


def test_editor_file_listing_uses_browser_filters_and_keeps_env_asset(tmp_path, monkeypatch):
    client, headers, version1 = _build_client(tmp_path, monkeypatch)
    (version1 / "assets" / "linked.txt").symlink_to(
        version1 / "assets" / "nested" / "guide.txt"
    )

    response = client.get(
        "/agentic/ontologies/demo-ontology/files",
        query_string={"folder": "assets"},
        headers=headers,
    )

    assert response.status_code == 200
    assert [item["name"] for item in response.get_json()["files"]] == [
        ".env",
        "nested/guide.txt",
    ]


def test_editor_file_read_rejects_symlink(tmp_path, monkeypatch):
    client, headers, version1 = _build_client(tmp_path, monkeypatch)
    (version1 / "operations" / "linked.md").symlink_to(
        version1 / "operations" / "流程.md"
    )

    response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        query_string={"folder": "operations", "name": "linked.md"},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Invalid file path"


def test_editor_asset_read_keeps_env_and_rejects_symlink(tmp_path, monkeypatch):
    client, headers, version1 = _build_client(tmp_path, monkeypatch)
    (version1 / "assets" / "linked.txt").symlink_to(
        version1 / "assets" / "nested" / "guide.txt"
    )

    env_response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        query_string={"folder": "assets", "name": ".env"},
        headers=headers,
    )
    linked_response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        query_string={"folder": "assets", "name": "linked.txt"},
        headers=headers,
    )

    assert env_response.status_code == 200
    assert env_response.get_json()["content"] == "KEY=value"
    assert linked_response.status_code == 400
    assert linked_response.get_json()["error"] == "Error: Invalid asset key."


def test_import_v01_package_normalizes_manifest_catalogs_and_script_mapping(tmp_path, monkeypatch):
    client, headers, _version1 = _build_client(tmp_path, monkeypatch)
    zip_buffer = io.BytesIO()
    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(
            "legacy/manifest.yaml",
            """\
kind: ontology-package
schema_version: 0.1
id: legacy-example
name: Legacy Example
description: Legacy example package
version: 0.1.0
language: zh-CN
status: draft
""",
        )
        archive.writestr(
            "legacy/index.md",
            "---\nresource: root\n---\n\n# Legacy Example\n",
        )
        archive.writestr(
            "legacy/objects/index.md",
            "| object key | title |\n| --- | --- |\n| Part | Part |\n",
        )
        archive.writestr("legacy/objects/Part.md", "# Part\n")
        archive.writestr(
            "legacy/operations/index.md",
            "| operation key | title |\n| --- | --- |\n| QueryPart | Query part |\n",
        )
        archive.writestr("legacy/operations/QueryPart.md", "# Query part\n")
        archive.writestr("legacy/scripts/query_part.py", "print('ok')\n")
        archive.writestr(
            "legacy/scripts/query_part.md",
            """\
---
resource: script
entrypoint: query_part.py
bound_operation: QueryPart
---

| 参数 key | CLI 参数 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- | --- |
| part_code | --part-code | string | 是 | Part code |
""",
        )
    zip_buffer.seek(0)

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "legacy.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201

    ontology_dir = get_workspace_paths("demo").ontologies_dir / "legacy-example"
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["kind"] == "ontology-package"
    assert str(manifest["schema_version"]) == "0.1"
    assert manifest["active_version"] == "0.1.0"
    assert manifest["language"] == "zh-CN"
    assert "status" not in manifest
    version_dir = ontology_dir / "versions" / "0.1.0"
    assert "# Legacy Example" in (version_dir / "index.md").read_text(encoding="utf-8")
    assert (version_dir / "objects" / "index.md").is_file()
    assert (version_dir / "operations" / "index.md").is_file()
    assert "bound_operation: QueryPart" in (
        version_dir / "scripts" / "query_part.md"
    ).read_text(encoding="utf-8")
    assert not list(version_dir.glob("*-list.md"))
    assert not (version_dir / "ONTOLOGY.md").exists()

    raw_response = client.get(
        "/agentic/ontologies/legacy-example/raw-file",
        query_string={"path": "operations/QueryPart.md"},
        headers=headers,
    )
    assert raw_response.status_code == 200
    assert raw_response.get_json()["content"] == "# Query part\n"


@pytest.mark.parametrize(
    "unsafe_name",
    (
        "/legacy/manifest.yaml",
        "C:\\legacy\\manifest.yaml",
        "legacy/./manifest.yaml",
        "legacy//manifest.yaml",
    ),
)
def test_import_zip_rejects_absolute_and_noncanonical_member_paths(
    tmp_path,
    monkeypatch,
    unsafe_name,
):
    client, headers, _version1 = _build_client(tmp_path, monkeypatch)
    archive = _zip_from_entries([(unsafe_name, "kind: ontology-package\n"
            "schema_version: 0.1\n")])

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (archive, "unsafe.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == "Zip contains unsafe path"


@pytest.mark.parametrize(
    "entries, expected_error",
    (
        (
            (("legacy/manifest.yaml", "first"), ("legacy/manifest.yaml", "second")),
            "Zip contains duplicate paths",
        ),
        (
            (("legacy/File.md", "first"), ("legacy/file.md", "second")),
            "Zip contains duplicate paths",
        ),
        (
            (("legacy", "file"), ("legacy/manifest.yaml", "child")),
            "Zip contains colliding paths",
        ),
    ),
)
def test_import_zip_rejects_duplicate_and_colliding_targets(
    tmp_path,
    monkeypatch,
    entries,
    expected_error,
):
    client, headers, _version1 = _build_client(tmp_path, monkeypatch)
    archive = _zip_from_entries(entries)

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (archive, "collision.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert response.get_json()["error"] == expected_error


def test_import_zip_rejects_member_count_and_path_depth_limits(tmp_path, monkeypatch):
    client, headers, _version1 = _build_client(tmp_path, monkeypatch)
    monkeypatch.setattr(ontologies_module, "ONTOLOGY_MAX_ZIP_MEMBERS", 2)
    too_many = _zip_from_entries((("a", ""), ("b", ""), ("c", "")))

    count_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (too_many, "many.zip")},
        content_type="multipart/form-data",
    )

    assert count_response.status_code == 400
    assert count_response.get_json()["error"] == "Zip contains too many entries"

    monkeypatch.setattr(ontologies_module, "ONTOLOGY_MAX_ZIP_MEMBERS", 10)
    monkeypatch.setattr(ontologies_module, "ONTOLOGY_MAX_ZIP_PATH_DEPTH", 2)
    too_deep = _zip_from_entries((("a/b/c.txt", "deep"),))
    depth_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (too_deep, "deep.zip")},
        content_type="multipart/form-data",
    )

    assert depth_response.status_code == 400
    assert depth_response.get_json()["error"] == "Zip contains unsafe path"


def test_import_zip_rejects_unsupported_v01_schema_and_ambiguous_roots(tmp_path, monkeypatch):
    client, headers, _version1 = _build_client(tmp_path, monkeypatch)
    invalid_v01 = _zip_from_entries(
        (
            (
                "legacy/manifest.yaml",
                "kind: ontology-package\nschema_version: 9.9\nid: legacy\nname: Legacy\n"
                "description: Legacy\nversion: 1.0.0\n",
            ),
            ("legacy/objects/index.md", "# Objects\n"),
        )
    )
    invalid_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (invalid_v01, "invalid-v01.zip")},
        content_type="multipart/form-data",
    )
    assert invalid_response.status_code == 400
    assert invalid_response.get_json()["error"] == "Unsupported or invalid ontology package manifest"

    current_manifest = (
        "kind: ontology-package\nschema_version: 0.1\nid: current-demo\nname: Current Demo\n"
        "description: Current\nversion: 1.0.0\nlanguage: zh-CN\n"
    )
    legacy_manifest = (
        "kind: ontology-package\nschema_version: 0.1\nid: legacy-demo\nname: Legacy Demo\n"
        "description: Legacy\nversion: 0.1.0\nlanguage: zh-CN\n"
    )
    ambiguous = _zip_from_entries(
        (
            ("current/manifest.yaml", current_manifest),
            ("current/index.md", "# Current\n"),
            ("current/objects/index.md", "# Objects\n"),
            ("current/operations/index.md", "# Operations\n"),
            ("legacy/manifest.yaml", legacy_manifest),
            ("legacy/index.md", "# Legacy\n"),
            ("legacy/objects/index.md", "# Objects\n"),
            ("legacy/operations/index.md", "# Operations\n"),
        )
    )
    ambiguous_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (ambiguous, "ambiguous.zip")},
        content_type="multipart/form-data",
    )
    assert ambiguous_response.status_code == 400
    assert ambiguous_response.get_json()["error"] == "Zip contains multiple ontology package roots"


def test_import_v01_defaults_recommended_language(tmp_path, monkeypatch):
    client, headers, _version1 = _build_client(tmp_path, monkeypatch)
    archive = _zip_from_entries(
        (
            (
                "legacy/manifest.yaml",
                "kind: ontology-package\nschema_version: 0.1\nid: language-default\n"
                "name: Language Default\ndescription: Missing recommended language\nversion: 0.1.0\n",
            ),
            ("legacy/index.md", "# Language Default\n"),
            ("legacy/objects/index.md", "# Objects\n"),
            ("legacy/operations/index.md", "# Operations\n"),
        )
    )

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (archive, "language-default.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    ontology_dir = get_workspace_paths("demo").ontologies_dir / "language-default"
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["kind"] == "ontology-package"
    assert str(manifest["schema_version"]) == "0.1"
    assert manifest["language"] == "zh-CN"
