"""Tests for ontology management routes."""

import io
import json
import threading
import zipfile
from pathlib import Path

import yaml
from fastapi.testclient import TestClient

from api.testing import TestApplication
from api.routes import ontologies as ontologies_module
from api.routes.ontologies import ontologies_router
from auth_test_backend import AuthManager
from workspace.workspaces import create_workspace, get_workspace_paths, update_workspace


EXAMPLE_ONTOLOGY_ID = "example-domain-knowledge-agent"


def _build_test_client(
    tmp_path,
    username: str = "admin",
    password: str = "TestBootstrap#2026",
    role: str = "admin",
):
    app = TestApplication()
    app.state.services["TESTING"] = True

    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    if username != "admin":
        temporary_password = "UserStrong123!"
        auth_manager.create_user(username, temporary_password, role=role)
        user = auth_manager.change_password(username, temporary_password, "ChangedStrong123!")
    else:
        user = auth_manager.authenticate(username, password)
        assert user is not None
        if user.password_change_required:
            user = auth_manager.change_password(username, password, "RootStrong123!")

    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(ontologies_router)

    client = TestClient(app)
    token = auth_manager.issue_token(user)
    headers = {"Authorization": "Bearer {}".format(token)}
    return client, headers


def _post(client: TestClient, path: str, *, data=None, **kwargs):
    """Send ordinary or multipart POST requests with HTTPX semantics."""
    if not isinstance(data, dict):
        return client.post(path, data=data, **kwargs)

    fields = {}
    files = {}
    for key, value in data.items():
        if (
            isinstance(value, tuple)
            and len(value) == 2
            and hasattr(value[0], "read")
        ):
            stream, filename = value
            files[key] = (filename, stream)
        else:
            fields[key] = value
    if files:
        return client.post(
            path,
            data=fields or None,
            files=files,
            **kwargs,
        )
    return client.post(path, data=data, **kwargs)


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "demo", *, owner=None):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True, owner=owner)
    return get_workspace_paths(workspace_id)


def _write_example_ontology(workspace_paths):
    ontology_dir = workspace_paths.ontologies_dir / EXAMPLE_ONTOLOGY_ID
    objects_dir = ontology_dir / "versions" / "1.0.0" / "objects"
    objects_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            f"id: {EXAMPLE_ONTOLOGY_ID}\n"
            f"name: {EXAMPLE_ONTOLOGY_ID}\n"
            "description: Example domain ontology for route tests\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (objects_dir / "TargetItem.md").write_text(
        "# 实体属性清单\n\n| 字段 | 说明 |\n| --- | --- |\n| TargetItem_ID | TargetItem identifier |\n",
        encoding="utf-8",
    )
    return ontology_dir


def _build_zip_file(entries: dict[str, str | bytes]) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    buffer.seek(0)
    return buffer


def test_zip_upload_read_is_bounded_to_configured_limit(monkeypatch):
    class RecordingStream(io.BytesIO):
        requested_size = None

        def read(self, size=-1):
            self.requested_size = size
            return super().read(size)

    stream = RecordingStream(b"12345")
    uploaded_file = type("UploadedFile", (), {"file": stream})()
    monkeypatch.setattr(ontologies_module, "ONTOLOGY_MAX_ZIP_BYTES", 4)

    uploaded_bytes, error = ontologies_module._read_limited_zip_upload(uploaded_file)

    assert uploaded_bytes is None
    assert error == "Zip file too large"
    assert stream.requested_size == 5


def test_server_request_limit_exceeds_ontology_zip_limit():
    from server_config import MAX_CONTENT_LENGTH

    assert MAX_CONTENT_LENGTH > ontologies_module.ONTOLOGY_MAX_ZIP_BYTES


def _build_source_ontology_zip(
    *,
    include_scripts: bool,
    version: str = "",
    object_aliases: str = "实体甲,实体乙",
) -> io.BytesIO:
    version_line = [f"version: {version}"] if version else []
    entries = {
        "source-demo/SKILL.md": "\n".join([
            "---",
            "id: source-demo",
            "name: Source Demo",
            "description: Imported from source zip",
            *version_line,
            "---",
            "",
            "# Source Demo",
            "",
            "source ontology package",
            "",
        ]),
        "source-demo/object/实体A.md": "\n".join([
            "# 对象描述",
            "实体A对象描述",
            "",
            "# 对象别名",
            object_aliases,
            "",
            "# 英文名称",
            "EntityA",
            "",
            "# 对象属性清单",
            "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |",
            "| --- | --- | --- | --- | --- | --- | --- |",
            "| 实体A编号 | EntityACode | 实体A编号描述 | 是 | 字符串 | A编号 | 非空 |",
            "",
        ]),
        "source-demo/logic/流程A.md": "\n".join([
            "# 业务描述",
            "流程A描述",
            "",
            "# 与logic的关联",
            "- 被流程使用 > 无",
            "- 使用流程 > 无",
            "",
            "# 与object的关联",
            "- 输入数据的对象：[[实体A]]",
            "- 输出数据的对象：无",
            "- 支撑数据的对象：无",
            "",
            "# 输入",
            "| 参数名称 | 描述 |",
            "| --- | --- |",
            "| 实体A编号 | 实体A编号描述 |",
            "",
            "# 输出",
            "| 参数名称 | 描述 |",
            "| --- | --- |",
            "| 结果 | 处理结果 |",
            "",
            "# 逻辑描述",
            "执行 process_a 脚本",
            "",
        ]),
    }
    if include_scripts:
        entries.update({
            "source-demo/action/process_a.py": (
                "# 工具流程: 流程A\n"
                "# parameters: [{\"name\":\"实体A编号\",\"flag\":\"--entity-a-code\",\"description\":\"实体A编号\",\"required\":true,\"type\":\"string\"}]\n"
                "#查数脚本 （涉及mindsdb查数的脚本会加这个标签）\n"
                "print('process_a')\n"
            ),
            "source-demo/action/.env": "API_TOKEN=demo-token\n",
        })
    return _build_zip_file(entries)


def _build_v01_ontology_zip(
    *,
    ontology_id: str = "source-demo",
    version: str = "",
) -> io.BytesIO:
    version_line = "version: {}\n".format(version or "1.0.0")
    entries = {
        "{}/manifest.yaml".format(ontology_id): (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: {}\n".format(ontology_id)
            + "name: {}\n".format(ontology_id)
            + "description: standard test package\n"
            + version_line
            + "language: zh-CN\n"
        ),
        "{}/index.md".format(ontology_id): (
            "---\nresource: root\nkey: root\ntitle: Test\n---\n\n# Test\n"
        ),
        "{}/objects/index.md".format(ontology_id): (
            "---\nresource: object_index\nkey: objects\ntitle: Objects\n---\n"
        ),
        "{}/objects/实体A.md".format(ontology_id): (
            "---\nresource: object\nkey: 实体A\ntitle: 实体A\n---\n"
        ),
        "{}/operations/index.md".format(ontology_id): (
            "---\nresource: operation_index\nkey: operations\ntitle: Operations\n---\n"
        ),
        "{}/operations/流程A.md".format(ontology_id): (
            "---\nresource: operation\nkey: 流程A\ntitle: 流程A\n"
            "uses:\n  objects: []\n---\n"
        ),
        "{}/scripts/index.md".format(ontology_id): (
            "---\nresource: script_index\nkey: scripts\ntitle: Scripts\n---\n"
        ),
    }
    return _build_zip_file(entries)


def test_list_ontologies_returns_example_manifest(tmp_path, monkeypatch):
    _write_example_ontology(_create_demo_workspace(tmp_path, monkeypatch, workspace_id="example-routes"))
    client, headers = _build_test_client(tmp_path)

    response = client.get("/agentic/ontologies", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload is not None
    example_manifest = next(item for item in payload["ontologies"] if item["id"] == EXAMPLE_ONTOLOGY_ID)
    assert example_manifest["manifest_path"].endswith("manifest.yaml")


def test_list_ontologies_uses_project_root_from_env(tmp_path, monkeypatch):
    import ontology.ontologies as ontology_registry_module

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for routes\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )

    monkeypatch.setattr(ontology_registry_module, "_global_registry", None)

    client, headers = _build_test_client(tmp_path)
    response = client.get("/agentic/ontologies", headers=headers)

    assert response.status_code == 200
    payload = response.json()
    assert payload is not None
    assert payload["ontologies"] == [
        {
            "active_version": "1.0.0",
            "business_data": {
                "configured": False,
                "mode": "",
                "provider": "",
                "ready": False,
                "updated_at": "",
            },
            "description": "Demo ontology for routes",
            "enabled": True,
            "id": "demo-ontology",
            "language": "zh-CN",
            "manifest_path": str(ontology_dir / "manifest.yaml"),
            "name": "Demo Ontology",
            "index_path": None,
            "version": "1.0.0",
            "versions": ["1.0.0"],
        }
    ]


def test_update_ontology_status_persists_enabled_flag(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    ontology_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for routes\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    response = client.put(
        "/agentic/ontologies/demo-ontology/status",
        headers=headers,
        json={"enabled": False},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload == {"success": True, "ontology_id": "demo-ontology", "enabled": False}

    manifest = (ontology_dir / "manifest.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in manifest

    list_response = client.get("/agentic/ontologies", headers=headers)
    list_payload = list_response.json()
    assert list_payload["ontologies"][0]["enabled"] is False


def test_workspace_owner_can_manage_ontology_package(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, owner="modeler")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "objects").mkdir(parents=True)
    (version2 / "objects").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Owner managed ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "objects" / "实体A.md").write_text("原始内容\n", encoding="utf-8")
    (version2 / "objects" / "实体A.md").write_text("待删除版本\n", encoding="utf-8")
    client, headers = _build_test_client(tmp_path, username="modeler", role="user")

    status_response = client.put(
        "/agentic/ontologies/demo-ontology/status",
        headers=headers,
        json={"enabled": False},
    )
    assert status_response.status_code == 200

    edit_response = client.put(
        "/agentic/ontologies/demo-ontology/file",
        headers=headers,
        json={
            "folder": "objects",
            "name": "实体A.md",
            "version": "1.0.0",
            "content": "所有者编辑\n",
        },
    )
    assert edit_response.status_code == 200
    assert (version1 / "objects" / "实体A.md").read_text(encoding="utf-8") == "所有者编辑\n"

    upload_response = _post(client,
        "/agentic/ontologies/demo-ontology/upload",
        headers=headers,
        data={
            "folder": "objects",
            "version": "1.0.0",
            "file": (io.BytesIO("新增内容\n".encode("utf-8")), "新增实体.md"),
        },
    )
    assert upload_response.status_code == 201
    assert (version1 / "objects" / "新增实体.md").read_text(encoding="utf-8") == "新增内容\n"

    version_response = client.delete(
        "/agentic/ontologies/demo-ontology/versions/2.0.0",
        headers=headers,
    )
    assert version_response.status_code == 200

    delete_response = client.delete("/agentic/ontologies/demo-ontology", headers=headers)
    assert delete_response.status_code == 200
    assert not ontology_dir.exists()


def test_workspace_member_cannot_manage_ontology_package(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, owner="alice")
    update_workspace("demo", members=["modeler"])
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version_dir = ontology_dir / "versions" / "1.0.0" / "objects"
    version_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Member read-only ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version_dir / "实体A.md").write_text("原始内容\n", encoding="utf-8")
    client, headers = _build_test_client(tmp_path, username="modeler", role="user")

    responses = [
        _post(client, "/agentic/ontologies/import-zip", headers=headers),
        client.put(
            "/agentic/ontologies/demo-ontology/active-version",
            headers=headers,
            json={"version": "1.0.0"},
        ),
        client.put(
            "/agentic/ontologies/demo-ontology/status",
            headers=headers,
            json={"enabled": False},
        ),
        client.put(
            "/agentic/ontologies/demo-ontology/file",
            headers=headers,
            json={"folder": "objects", "name": "实体A.md", "content": "越权编辑\n"},
        ),
        _post(client,
            "/agentic/ontologies/demo-ontology/upload",
            headers=headers,
            data={"folder": "objects", "file": (io.BytesIO(b"unauthorized\n"), "new.md")},
        ),
        client.delete(
            "/agentic/ontologies/demo-ontology/versions/1.0.0",
            headers=headers,
        ),
        client.delete("/agentic/ontologies/demo-ontology", headers=headers),
    ]
    for response in responses:
        assert response.status_code == 403
        assert response.json() == {
            "error": "Forbidden",
            "code": "WORKSPACE_ACCESS_DENIED",
        }


def test_get_ontology_file_reads_object_markdown(tmp_path, monkeypatch):
    _write_example_ontology(_create_demo_workspace(tmp_path, monkeypatch, workspace_id="example-file"))
    client, headers = _build_test_client(tmp_path)

    response = client.get(
        f"/agentic/ontologies/{EXAMPLE_ONTOLOGY_ID}/file",
        params={"folder": "objects", "name": "TargetItem.md"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload is not None
    assert payload["folder"] == "objects"
    assert "# 实体属性清单" in payload["content"]
    assert "TargetItem_ID" in payload["content"]


def test_ontology_file_routes_list_and_read_assets_folder(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    assets_dir = ontology_dir / "versions" / "1.0.0" / "assets"
    (assets_dir / "rules").mkdir(parents=True)
    (assets_dir / "rules" / "limits.ini").write_text("[limits]\nmax=10\n", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for folder route tests\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )

    client, headers = _build_test_client(tmp_path)
    list_response = client.get(
        "/agentic/ontologies/demo-ontology/files",
        params={"folder": "assets"},
        headers=headers,
    )

    assert list_response.status_code == 200
    assert [item["name"] for item in list_response.json()["files"]] == [
        "rules/limits.ini"
    ]

    read_response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        params={"folder": "assets", "name": "rules/limits.ini"},
        headers=headers,
    )
    assert read_response.status_code == 200
    assert read_response.json()["content"] == "[limits]\nmax=10"


def test_get_ontology_file_uses_onto_runtime_root_from_env(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    object_dir = workspace_paths.ontologies_dir / "demo-ontology" / "objects"
    object_dir.mkdir(parents=True)
    (workspace_paths.ontologies_dir / "demo-ontology" / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for routes\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (object_dir / "实体A.md").write_text("#### 对象描述\n实体A来自workspace目录", encoding="utf-8")

    client, headers = _build_test_client(tmp_path)
    response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        params={"folder": "objects", "name": "实体A.md"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload is not None
    assert payload["content"] == "#### 对象描述\n实体A来自workspace目录"


def test_update_operation_file_allows_content_at_default_char_limit(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    operation_dir = ontology_dir / "operations"
    operation_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for operation length route tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (operation_dir / "流程A.md").write_text("旧内容\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path)
    content = "x" * 15000
    response = client.put(
        "/agentic/ontologies/demo-ontology/file",
        headers=headers,
        json={
            "folder": "operations",
            "name": "流程A.md",
            "content": content,
        },
    )

    assert response.status_code == 200
    assert (
        ontology_dir / "versions" / "1.0.0" / "operations" / "流程A.md"
    ).read_text(encoding="utf-8") == content


def test_update_operation_file_rejects_content_over_char_limit(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    operation_dir = ontology_dir / "operations"
    operation_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for operation length route tests\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (operation_dir / "流程A.md").write_text("旧内容\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path)
    response = client.put(
        "/agentic/ontologies/demo-ontology/file",
        headers=headers,
        json={
            "folder": "operations",
            "name": "流程A.md",
            "content": "x" * 15001,
        },
    )

    assert response.status_code == 400
    assert "字符" in response.json()["error"]


def test_file_editors_reject_deterministic_declarations_without_a_lock(
    tmp_path,
    monkeypatch,
):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    operation_dir = ontology_dir / "operations"
    operation_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Legacy ontology for deterministic edit rejection\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (operation_dir / "query.md").write_text("legacy operation\n", encoding="utf-8")
    deterministic_content = (
        "---\n"
        "resource: operation\n"
        "key: query\n"
        "execution:\n"
        "  type: deterministic\n"
        "  input_schema: schemas/query.input.json\n"
        "  output_schema: schemas/query.output.json\n"
        "---\n"
    )
    client, headers = _build_test_client(tmp_path)

    update_response = client.put(
        "/agentic/ontologies/demo-ontology/file",
        headers=headers,
        json={
            "folder": "operations",
            "name": "query.md",
            "content": deterministic_content,
        },
    )
    upload_response = _post(
        client,
        "/agentic/ontologies/demo-ontology/upload",
        headers=headers,
        data={
            "folder": "operations",
            "file": (
                io.BytesIO(
                    deterministic_content.replace("key: query", "key: new").encode(
                        "utf-8"
                    )
                ),
                "new.md",
            ),
        },
    )

    assert update_response.status_code == 409
    assert update_response.json()["error_code"] == "deterministic_operation_immutable"
    assert upload_response.status_code == 409
    assert upload_response.json()["error_code"] == "deterministic_operation_immutable"
    version_dir = ontology_dir / "versions" / "1.0.0"
    assert (version_dir / "operations" / "query.md").read_text(encoding="utf-8") == (
        "legacy operation\n"
    )
    assert not (version_dir / "operations" / "new.md").exists()
    assert not (version_dir / ".deterministic-lock.json").exists()


def test_update_object_file_rejects_attribute_count_over_limit(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ENTITY_ATTRIBUTE_MAX_COUNT", "1")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    object_dir = ontology_dir / "objects"
    object_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for object attribute limits\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (object_dir / "实体A.md").write_text("#### 对象描述\n旧内容\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path)
    response = client.put(
        "/agentic/ontologies/demo-ontology/file",
        headers=headers,
        json={
            "folder": "objects",
            "name": "实体A.md",
            "content": "\n".join([
                "#### 对象描述",
                "实体A说明",
                "",
                "#### 实体属性清单",
                "| 中文名称 | 英文名称 | 描述 |",
                "| --- | --- | --- |",
                "| 属性A | attr_a | A说明 |",
                "| 属性B | attr_b | B说明 |",
            ]),
        },
    )

    assert response.status_code == 400
    assert "单实体属性数量超过上限" in response.json()["error"]
    assert (
        ontology_dir / "versions" / "1.0.0" / "objects" / "实体A.md"
    ).read_text(encoding="utf-8") == "#### 对象描述\n旧内容\n"


def test_upload_object_file_rejects_new_file_when_object_count_reaches_limit(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OBJECT_MAX_COUNT", "1")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    object_dir = ontology_dir / "objects"
    object_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for object count limits\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (object_dir / "实体A.md").write_text("#### 对象描述\n实体A\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path)
    response = _post(client,
        "/agentic/ontologies/demo-ontology/upload",
        headers=headers,
        data={
            "folder": "objects",
            "file": (io.BytesIO("#### 对象描述\n实体B\n".encode("utf-8")), "实体B.md"),
        },
    )

    assert response.status_code == 400
    assert "单本体实体数量已达上限" in response.json()["error"]
    assert not (ontology_dir / "versions" / "1.0.0" / "objects" / "实体B.md").exists()


def test_upload_operation_file_rejects_new_file_when_operation_count_reaches_limit(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OPERATION_MAX_COUNT", "1")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    operation_dir = ontology_dir / "operations"
    operation_dir.mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for operation count limits\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (operation_dir / "流程A.md").write_text("#### 操作描述\n流程A\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path)
    response = _post(client,
        "/agentic/ontologies/demo-ontology/upload",
        headers=headers,
        data={
            "folder": "operations",
            "file": (io.BytesIO("#### 操作描述\n流程B\n".encode("utf-8")), "流程B.md"),
        },
    )

    assert response.status_code == 400
    assert "单本体流程数量已达上限" in response.json()["error"]
    assert not (ontology_dir / "versions" / "1.0.0" / "operations" / "流程B.md").exists()


def test_download_ontology_zip_returns_full_package_for_regular_user(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "rules").mkdir(parents=True)
    (ontology_dir / "metrics").mkdir(parents=True)
    (ontology_dir / "scripts" / "nested").mkdir(parents=True)
    (ontology_dir / "mappings").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology for download\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "index.md").write_text("# Demo Ontology\n", encoding="utf-8")
    (ontology_dir / "objects" / "index.md").write_text("- 实体A\n", encoding="utf-8")
    (ontology_dir / "operations" / "index.md").write_text("- 流程A\n", encoding="utf-8")
    (ontology_dir / "rules" / "index.md").write_text("# Rules\n", encoding="utf-8")
    (ontology_dir / "metrics" / "index.md").write_text("# Metrics\n", encoding="utf-8")
    (ontology_dir / "scripts" / "index.md").write_text("# Scripts\n", encoding="utf-8")
    (ontology_dir / "scripts" / "demo.md").write_text(
        "---\nresource: script\nkey: demo\nentrypoint: nested/demo.py\n---\n",
        encoding="utf-8",
    )
    (ontology_dir / "objects" / "实体A.md").write_text("实体A内容\n", encoding="utf-8")
    (ontology_dir / "operations" / "流程A.md").write_text("流程A内容\n", encoding="utf-8")
    (ontology_dir / "scripts" / "nested" / "demo.py").write_text("print('demo')\n", encoding="utf-8")
    (ontology_dir / "mappings" / "object-data-mappings.json").write_text(
        '{"schema_version":"0.1","objects":{}}\n',
        encoding="utf-8",
    )

    client, headers = _build_test_client(
        tmp_path,
        username="viewer",
        password="viewer123",
        role="user",
    )
    response = client.get(
        "/agentic/ontologies/demo-ontology/download",
        headers=headers,
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "application/zip"
    assert "demo-ontology-1.0.0.zip" in response.headers["Content-Disposition"]

    zip_buffer = io.BytesIO(response.content)
    with zipfile.ZipFile(zip_buffer, "r") as zf:
        assert sorted(zf.namelist()) == [
            "demo-ontology/index.md",
            "demo-ontology/manifest.yaml",
            "demo-ontology/mappings/object-data-mappings.json",
            "demo-ontology/metrics/index.md",
            "demo-ontology/objects/index.md",
            "demo-ontology/objects/实体A.md",
            "demo-ontology/operations/index.md",
            "demo-ontology/operations/流程A.md",
            "demo-ontology/rules/index.md",
            "demo-ontology/scripts/demo.md",
            "demo-ontology/scripts/index.md",
            "demo-ontology/scripts/nested/demo.py",
        ]
        assert zf.read("demo-ontology/objects/实体A.md").decode("utf-8") == "实体A内容\n"
        assert zf.read("demo-ontology/scripts/nested/demo.py").decode("utf-8") == "print('demo')\n"


def test_import_ontology_zip_converts_legacy_package_once(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, owner="modeler")
    client, headers = _build_test_client(tmp_path, username="modeler", role="user")
    operation_content = "x" * 15000

    zip_buffer = _build_zip_file({
        "converted-demo/manifest.yaml": (
            "kind: ontology\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Imported from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "scope: []\n"
        ),
        "converted-demo/ONTOLOGY.md": "# Converted Demo\n",
        "converted-demo/objects-list.md": "- 实体A\n",
        "converted-demo/operations-list.md": "- 流程A\n",
        "converted-demo/objects/实体A.md": "#### 对象描述\n实体A说明\n",
        "converted-demo/operations/流程A.md": operation_content,
        "converted-demo/scripts/demo.py": "print('demo')\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
    )

    assert response.status_code == 201
    assert response.json()["import_mode"] == "converted"
    assert (workspace_paths.ontologies_dir / "converted-demo").exists()


def test_import_converted_ontology_zip_reports_invalid_frontmatter_as_bad_request(
    tmp_path,
    monkeypatch,
):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "invalid-frontmatter/manifest.yaml": (
            "kind: ontology\n"
            "id: invalid-frontmatter\n"
            "name: Invalid Frontmatter\n"
            "description: Invalid legacy object frontmatter\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "invalid-frontmatter/ONTOLOGY.md": "# Invalid Frontmatter\n",
        "invalid-frontmatter/objects-list.md": (
            "| key | title |\n"
            "| --- | --- |\n"
            "| Part | Part |\n"
        ),
        "invalid-frontmatter/objects/Part.md": (
            "---\n"
            "key: [Part\n"
            "---\n\n"
            "# Part\n"
        ),
        "invalid-frontmatter/operations-list.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "invalid-frontmatter.zip")},
    )

    assert response.status_code == 400
    assert "Invalid frontmatter" in response.json()["error"]
    assert not (workspace_paths.ontologies_dir / "invalid-frontmatter").exists()


def test_import_ontology_zip_rejects_object_alias_mismatch(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "alias-warning/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: alias-warning\n"
            "name: Alias Warning\n"
            "description: Alias validation warning\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "alias-warning/index.md": "# Alias Warning\n",
        "alias-warning/objects/index.md": (
            "| object key | alias |\n"
            "| --- | --- |\n"
            "| ObjectA | CatalogAlias |\n"
        ),
        "alias-warning/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\ntitle: Display Object A\naliases:\n"
            "  - DetailAlias\n---\n\n# Object A\n"
        ),
        "alias-warning/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "alias-warning.zip")},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload is not None
    assert payload["error"] == (
        "对象 key='ObjectA'、title='Display Object A' 别名不一致："
        "objects/index.md = [CatalogAlias]；"
        "object_a.md = [DetailAlias]"
    )
    assert not (workspace_paths.ontologies_dir / "alias-warning").exists()


def test_import_ontology_zip_rejects_field_alias_conflict(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "field-alias-error/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: field-alias-error\n"
            "name: Field Alias Error\n"
            "description: Field alias validation error\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "field-alias-error/index.md": "# Field Alias Error\n",
        "field-alias-error/objects/index.md": (
            "| object key | title |\n"
            "| --- | --- |\n"
            "| ObjectA | 对象甲 |\n"
        ),
        "field-alias-error/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\ntitle: 对象甲\n---\n\n"
            "# 字段\n\n"
            "| 字段 key | 中文名 | 别名 |\n"
            "| --- | --- | --- |\n"
            "| FieldA | 字段甲 | 编号 |\n"
            "| FieldB | 字段乙 | 编号 |\n"
        ),
        "field-alias-error/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "field-alias-error.zip")},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "对象“对象甲”的字段名称“编号”同时指向 [FieldA, FieldB]"
    }
    assert not (workspace_paths.ontologies_dir / "field-alias-error").exists()


def test_import_ontology_zip_returns_field_alias_warnings(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "field-alias-warning/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: field-alias-warning\n"
            "name: Field Alias Warning\n"
            "description: Field alias validation warning\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "field-alias-warning/index.md": "# Field Alias Warning\n",
        "field-alias-warning/objects/index.md": (
            "| object key | title |\n"
            "| --- | --- |\n"
            "| ObjectA | 对象甲 |\n"
        ),
        "field-alias-warning/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\ntitle: 对象甲\n---\n\n"
            "# 字段\n\n"
            "| 字段 key | 中文名 | 别名 |\n"
            "| --- | --- | --- |\n"
            "| FieldA | 字段甲 | 编号、编号 |\n"
        ),
        "field-alias-warning/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "field-alias-warning.zip")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload is not None
    assert payload["warnings"]["field_aliases"] == [
        "对象“对象甲”的字段“FieldA”存在重复别名“编号”"
    ]


def test_import_converted_zip_supports_outerless_catalog_and_short_field_row(
    tmp_path,
    monkeypatch,
):
    from ontology.ontology_catalog import (
        find_field_alias_matches,
        resolve_object_reference,
    )

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "outerless-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: outerless-demo\n"
            "name: Outerless Demo\n"
            "description: Optional outer pipes\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "outerless-demo/index.md": "# Outerless Demo\n",
        "outerless-demo/objects/index.md": (
            "object key | title | aliases\n"
            "--- | --- | ---\n"
            "ObjectA | 对象甲 | 目标对象\n"
        ),
        "outerless-demo/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\ntitle: 对象甲\n"
            "aliases:\n  - 目标对象\n---\n\n"
            "# 字段\n\n"
            "字段 key | 中文名 | 别名\n"
            "--- | --- | ---\n"
            "ActualCode | 业务编号\n"
        ),
        "outerless-demo/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "outerless-demo.zip")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload is not None
    assert payload["import_mode"] == "converted"
    version_dir = (
        workspace_paths.ontologies_dir
        / "outerless-demo"
        / "versions"
        / "1.0.0"
    )
    resolution, error = resolve_object_reference(version_dir, "目标对象")
    assert error is None
    assert resolution is not None
    assert resolution.object_key == "ObjectA"
    assert find_field_alias_matches(
        version_dir / "objects" / "object_a.md",
        "查询业务编号",
    ) == (("业务编号", "ActualCode"),)


def test_import_converted_ontology_zip_handles_unreadable_object_catalog(
    tmp_path,
    monkeypatch,
):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    from ontology import ontology_catalog as catalog_module

    original_read_optional_text = catalog_module.read_optional_text

    def fail_to_read_object_catalog(path):
        if path.name == "index.md" and path.parent.name == "objects":
            raise OSError("permission denied")
        return original_read_optional_text(path)

    monkeypatch.setattr(
        catalog_module,
        "read_optional_text",
        fail_to_read_object_catalog,
    )
    zip_buffer = _build_zip_file({
        "invalid-catalog/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: invalid-catalog\n"
            "name: Invalid Catalog\n"
            "description: Invalid UTF-8 object catalog\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "invalid-catalog/index.md": "# Invalid Catalog\n",
        "invalid-catalog/objects/index.md": (
            "| object key |\n"
            "| --- |\n"
            "| ObjectA |\n"
        ),
        "invalid-catalog/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\n---\n\n# Object A\n"
        ),
        "invalid-catalog/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "invalid-catalog.zip")},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": "Failed to read objects/index.md."
    }
    assert not (workspace_paths.ontologies_dir / "invalid-catalog").exists()


def test_import_converted_ontology_zip_rejects_invalid_utf8_object_mapping(
    tmp_path,
    monkeypatch,
):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "invalid-mapping/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: invalid-mapping\n"
            "name: Invalid Mapping\n"
            "description: Invalid UTF-8 object mapping\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "invalid-mapping/index.md": "# Invalid Mapping\n",
        "invalid-mapping/objects/index.md": (
            "| object key |\n"
            "| --- |\n"
            "| ObjectA |\n"
        ),
        "invalid-mapping/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\n---\n\n# Object A\n"
        ),
        "invalid-mapping/operations/index.md": "# Operations\n",
        "invalid-mapping/mappings/object-data-mappings.json": b"\xff\xfe",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "invalid-mapping.zip")},
    )

    assert response.status_code == 400
    assert response.json() == {
        "error": (
            "Failed to decode mappings/object-data-mappings.json as UTF-8."
        )
    }
    assert not (workspace_paths.ontologies_dir / "invalid-mapping").exists()


def test_import_converted_ontology_zip_accepts_aligned_object_alias_table_and_reports_empty_warning(
    tmp_path,
    monkeypatch,
):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "alias-warning/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: alias-warning\n"
            "name: Alias Warning\n"
            "description: Alias validation warning\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "alias-warning/index.md": "# Alias Warning\n",
        "alias-warning/objects/index.md": (
            "| object key | alias |\n"
            "| :--- | ---: |\n"
            "| ObjectA | First//Second |\n"
        ),
        "alias-warning/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\naliases:\n"
            "  - First\n  - Second\n---\n\n# Object A\n"
        ),
        "alias-warning/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "alias-warning.zip")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload is not None
    assert payload["import_mode"] == "converted"
    assert payload["warnings"]["object_aliases"] == [
        "Object 'ObjectA' catalog aliases contain an empty value."
    ]
    assert (workspace_paths.ontologies_dir / "alias-warning").is_dir()


def test_import_ontology_zip_aggregates_object_alias_errors(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path)
    zip_buffer = _build_zip_file({
        "alias-error/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: alias-error\n"
            "name: Alias Error\n"
            "description: Alias validation errors\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "alias-error/index.md": "# Alias Error\n",
        "alias-error/objects/index.md": (
            "| object key | alias |\n"
            "| --- | --- |\n"
            "| ObjectA | Shared |\n"
            "| ObjectB | Shared |\n"
            "| Missing | Ghost |\n"
        ),
        "alias-error/objects/object_a.md": (
            "---\nresource: object\nkey: ObjectA\naliases:\n  - Shared\n---\n"
        ),
        "alias-error/objects/object_b.md": (
            "---\nresource: object\nkey: ObjectB\naliases:\n  - Shared\n---\n"
        ),
        "alias-error/operations/index.md": "# Operations\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "alias-error.zip")},
    )

    assert response.status_code == 400
    payload = response.json()
    assert payload is not None
    assert "Object catalog key 'Missing' has no matching detail." in payload["error"]
    assert "Object alias 'shared' points to multiple objects: ObjectA, ObjectB." in payload["error"]
    assert not (workspace_paths.ontologies_dir / "alias-error").exists()


def test_regular_user_ontology_writes_still_enforce_workspace_access(tmp_path, monkeypatch):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Shared Workspace", workspace_id="shared", set_as_default=True, owner=None)
    create_workspace("Private Workspace", workspace_id="private", owner="alice")
    client, base_headers = _build_test_client(tmp_path, username="modeler", role="user")
    headers = {**base_headers, "X-Workspace-Id": "private"}

    responses = [
        _post(client, "/agentic/ontologies/import-zip", headers=headers),
        client.put(
            "/agentic/ontologies/demo-ontology/active-version",
            headers=headers,
            json={"version": "1.0.0"},
        ),
    ]
    for response in responses:
        assert response.status_code == 403
        assert response.json() == {"error": "Forbidden", "code": "WORKSPACE_ACCESS_DENIED"}


def test_shadow_workspace_owner_import_converts_legacy_evaluation_package(
    tmp_path,
    monkeypatch,
):
    from api.routes.workspaces import workspaces_router

    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    monkeypatch.setenv("MODELING_SERVICE_TOKEN", "s" * 32)

    app = TestApplication()
    app.state.services["TESTING"] = True
    auth_manager = AuthManager(str(tmp_path / "users.json"), "test-secret")
    auth_manager.create_user("alice", "UserStrong123!", role="user")
    alice = auth_manager.change_password("alice", "UserStrong123!", "ChangedStrong123!")
    app.state.services["AUTH_MANAGER"] = auth_manager
    app.include_router(workspaces_router)
    app.include_router(ontologies_router)
    client = TestClient(app)

    provisioned = _post(client,
        "/agentic/internal/modeling/shadow-workspaces/ensure",
        json={"project_id": "pj_demo-123", "owner": "alice"},
        headers={"Authorization": "Bearer {}".format("s" * 32)},
    )
    assert provisioned.status_code == 201

    zip_buffer = _build_zip_file({
        "evaluation-demo/manifest.yaml": (
            "kind: ontology\n"
            "id: evaluation-demo\n"
            "name: Evaluation Demo\n"
            "description: Shadow workspace package\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "scope: []\n"
        ),
        "evaluation-demo/ONTOLOGY.md": "# Evaluation Demo\n",
        "evaluation-demo/objects-list.md": "- 实体A\n",
        "evaluation-demo/operations-list.md": "- 流程A\n",
        "evaluation-demo/objects/实体A.md": "#### 对象描述\n实体A说明\n",
        "evaluation-demo/operations/流程A.md": "流程说明\n",
    })
    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers={
            "Authorization": "Bearer {}".format(auth_manager.issue_token(alice)),
            "X-Workspace-Id": "mw-shadow-pj-demo-123",
        },
        data={"file": (zip_buffer, "evaluation-demo.zip")},
    )

    assert response.status_code == 201
    assert response.json()["import_mode"] == "converted"
    assert (
        get_workspace_paths("mw-shadow-pj-demo-123").ontologies_dir
        / "evaluation-demo"
    ).exists()


def test_import_converted_ontology_zip_rejects_operation_over_char_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    zip_buffer = _build_zip_file({
        "converted-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Imported from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "converted-demo/objects/index.md": "- 实体A\n",
        "converted-demo/operations/index.md": "- 流程A\n",
        "converted-demo/objects/实体A.md": "#### 对象描述\n实体A说明\n",
        "converted-demo/operations/流程A.md": "x" * 15001,
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
    )

    assert response.status_code == 400
    assert "字符" in response.json()["error"]


def test_import_new_ontology_rejects_when_workspace_ontology_count_reaches_limit(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_MAX_COUNT_PER_WORKSPACE", "1")
    existing_dir = workspace_paths.ontologies_dir / "existing-demo"
    existing_dir.mkdir(parents=True)
    (existing_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: existing-demo\n"
            "name: Existing Demo\n"
            "description: Existing ontology\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    zip_buffer = _build_zip_file({
        "converted-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Imported from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "converted-demo/objects/index.md": "- 实体A\n",
        "converted-demo/operations/index.md": "- 流程A\n",
        "converted-demo/objects/实体A.md": "#### 对象描述\n实体A说明\n",
        "converted-demo/operations/流程A.md": "#### 操作描述\n流程A说明\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
    )

    assert response.status_code == 400
    assert "本体数量已达上限" in response.json()["error"]
    assert not (workspace_paths.ontologies_dir / "converted-demo").exists()


def test_import_converted_ontology_zip_rejects_object_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OBJECT_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    zip_buffer = _build_zip_file({
        "converted-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Imported from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "converted-demo/objects/index.md": "- 实体A\n- 实体B\n",
        "converted-demo/operations/index.md": "- 流程A\n",
        "converted-demo/objects/实体A.md": "#### 对象描述\n实体A说明\n",
        "converted-demo/objects/实体B.md": "#### 对象描述\n实体B说明\n",
        "converted-demo/operations/流程A.md": "#### 操作描述\n流程A说明\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
    )

    assert response.status_code == 400
    assert "单本体实体数量已达上限" in response.json()["error"]


def test_import_converted_ontology_zip_rejects_operation_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OPERATION_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    zip_buffer = _build_zip_file({
        "converted-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Imported from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "converted-demo/objects/index.md": "- 实体A\n",
        "converted-demo/operations/index.md": "- 流程A\n- 流程B\n",
        "converted-demo/objects/实体A.md": "#### 对象描述\n实体A说明\n",
        "converted-demo/operations/流程A.md": "#### 操作描述\n流程A说明\n",
        "converted-demo/operations/流程B.md": "#### 操作描述\n流程B说明\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
    )

    assert response.status_code == 400
    assert "单本体流程数量已达上限" in response.json()["error"]


def test_import_converted_ontology_zip_rejects_entity_attribute_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ENTITY_ATTRIBUTE_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    zip_buffer = _build_zip_file({
        "converted-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Imported from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "converted-demo/objects/index.md": "- 实体A\n",
        "converted-demo/operations/index.md": "- 流程A\n",
        "converted-demo/objects/实体A.md": "\n".join([
            "#### 对象描述",
            "实体A说明",
            "",
            "#### 实体属性清单",
            "| 中文名称 | 英文名称 | 描述 |",
            "| --- | --- | --- |",
            "| 属性A | attr_a | A说明 |",
            "| 属性B | attr_b | B说明 |",
            "",
        ]),
        "converted-demo/operations/流程A.md": "#### 操作描述\n流程A说明\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
    )

    assert response.status_code == 400
    assert "单实体属性数量超过上限" in response.json()["error"]


def test_import_source_ontology_zip_rejects_generated_object_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OBJECT_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    object_content = "\n".join([
        "# 对象描述",
        "实体对象描述",
        "",
        "# 对象别名",
        "",
        "# 英文名称",
        "Entity",
        "",
        "# 对象属性清单",
        "| 中文名称 | 英文名称 | 描述 | 主键 | 类型 | 属性别名 | 属性规则 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
        "| 编号 | code | 编号描述 | 是 | 字符串 |  | 非空 |",
        "",
    ])
    zip_buffer = _build_zip_file({
        "source-demo/SKILL.md": "\n".join([
            "---",
            "id: source-demo",
            "name: Source Demo",
            "description: Imported from source zip",
            "---",
            "",
            "# Source Demo",
            "",
        ]),
        "source-demo/object/实体A.md": object_content.replace("Entity", "EntityA"),
        "source-demo/object/实体B.md": object_content.replace("Entity", "EntityB"),
        "source-demo/logic/流程A.md": "\n".join([
            "# 业务描述",
            "流程A描述",
            "",
            "# 与logic的关联",
            "- 被流程使用 > 无",
            "- 使用流程 > 无",
            "",
            "# 与object的关联",
            "- 输入数据的对象：[[实体A]]",
            "- 输出数据的对象：无",
            "- 支撑数据的对象：无",
            "",
            "# 输入",
            "| 参数名称 | 描述 |",
            "| --- | --- |",
            "| 编号 | 编号描述 |",
            "",
            "# 输出",
            "| 参数名称 | 描述 |",
            "| --- | --- |",
            "| 结果 | 处理结果 |",
            "",
            "# 逻辑描述",
            "处理流程A",
            "",
        ]),
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "source-demo.zip")},
    )

    assert response.status_code == 400
    assert "v0.1" in response.json()["error"]


def test_import_ontology_zip_does_not_inherit_default_legacy_connection(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    from agent.tools.business_data.ontology_sources_store import get_ontology_data_source

    (workspace_paths.workspace_dir / "data-connections.json").write_text(
        json.dumps({
            "default_connection_id": "default-nl2sql",
            "connections": [{
                "id": "default-nl2sql",
                "name": "Default NL2SQL",
                "provider": "nl2sql_http",
                "config": {"url": "https://nl2sql.example.com"},
            }],
        }),
        encoding="utf-8",
    )
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    zip_buffer = _build_zip_file({
        "bound-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: bound-demo\n"
            "name: Bound Demo\n"
            "description: Imported without implicit data source\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        "bound-demo/objects/index.md": "- 实体A\n",
        "bound-demo/operations/index.md": "- 流程A\n",
    })

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "bound-demo.zip")},
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["business_data_configured"] is False
    assert "business_data_binding" not in payload
    assert get_ontology_data_source("bound-demo", workspace_id="demo") is None


def test_import_ontology_zip_overwrite_replaces_existing_converted_package(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "converted-demo"
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Old Converted Demo\n"
            "description: Old version\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "scope: []\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "objects" / "旧实体.md").write_text("旧内容\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    zip_entries = {
        "converted-demo/manifest.yaml": (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: converted-demo\n"
            "name: Converted Demo\n"
            "description: Replaced from converted zip\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "scope: []\n"
        ),
        "converted-demo/objects/index.md": "- 新实体\n",
        "converted-demo/operations/index.md": "- 新流程\n",
        "converted-demo/objects/新实体.md": "新内容\n",
        "converted-demo/operations/新流程.md": "新流程内容\n",
    }

    conflict_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_zip_file(zip_entries), "converted-demo.zip")},
    )

    assert conflict_response.status_code == 409
    conflict_payload = conflict_response.json()
    assert conflict_payload is not None
    assert conflict_payload["requires_confirmation"] is True
    assert conflict_payload["ontology_id"] == "converted-demo"

    overwrite_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_zip_file(zip_entries), "converted-demo.zip"),
            "overwrite": "true",
        },
    )

    assert overwrite_response.status_code == 201
    overwrite_payload = overwrite_response.json()
    assert overwrite_payload is not None
    assert overwrite_payload["created"] is False
    assert overwrite_payload["overwritten"] is True
    assert overwrite_payload["import_mode"] == "converted"

    version_dir = ontology_dir / "versions" / "1.0.0"
    assert not (version_dir / "objects" / "旧实体.md").exists()
    assert (version_dir / "objects" / "新实体.md").read_text(encoding="utf-8") == "新内容\n"
    assert (version_dir / "operations" / "新流程.md").read_text(encoding="utf-8") == "新流程内容\n"


def test_import_source_ontology_zip_preserves_optional_scripts(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=True), "source-demo.zip")},
    )

    assert response.status_code == 400
    assert "v0.1" in response.json()["error"]


def test_import_source_ontology_zip_allows_missing_optional_scripts(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=False), "source-demo.zip")},
    )

    assert response.status_code == 400
    assert "v0.1" in response.json()["error"]


def test_import_source_ontology_zip_splits_slash_aliases_and_reports_empty_warning(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (
                _build_source_ontology_zip(
                    include_scripts=False,
                    object_aliases="实体甲//实体乙",
                ),
                "source-demo.zip",
            )
        },
    )

    assert response.status_code == 400
    assert "v0.1" in response.json()["error"]


def test_import_source_ontology_zip_overwrite_removes_stale_files(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "source-demo"
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "scripts").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: source-demo\n"
            "name: Source Demo\n"
            "description: Existing source ontology\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
            "scope: []\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "index.md").write_text("# Old Source Demo\n", encoding="utf-8")
    (ontology_dir / "objects" / "index.md").write_text("# objects\n", encoding="utf-8")
    (ontology_dir / "operations" / "index.md").write_text("# operations\n", encoding="utf-8")
    (ontology_dir / "objects" / "旧实体.md").write_text("旧实体\n", encoding="utf-8")
    (ontology_dir / "operations" / "旧流程.md").write_text("旧流程\n", encoding="utf-8")
    (ontology_dir / "scripts" / "index.md").write_text("# scripts\n", encoding="utf-8")
    (ontology_dir / "scripts" / "old.md").write_text(
        "---\nresource: script\nkey: old\nbound_operation: 旧流程\nentrypoint: old.py\n---\n",
        encoding="utf-8",
    )
    (ontology_dir / "scripts" / "old.py").write_text("print('old')\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_v01_ontology_zip(), "source-demo.zip"),
            "overwrite": "true",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload is not None
    assert payload["import_mode"] == "converted"
    assert payload["overwritten"] is True

    version_dir = ontology_dir / "versions" / "1.0.0"
    assert not (version_dir / "objects" / "旧实体.md").exists()
    assert not (version_dir / "operations" / "旧流程.md").exists()
    assert not list(version_dir.glob("*-list.md"))
    assert not (version_dir / "scripts" / "old.md").exists()
    assert not (version_dir / "scripts" / "old.py").exists()
    assert (version_dir / "scripts" / "index.md").is_file()
    assert (version_dir / "objects" / "实体A.md").exists()
    assert (version_dir / "operations" / "流程A.md").exists()


def test_import_zip_cannot_overwrite_locked_deterministic_version(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "source-demo"
    version_dir = ontology_dir / "versions" / "1.0.0"
    version_dir.mkdir(parents=True)
    (version_dir / ".deterministic-lock.json").write_text("{}", encoding="utf-8")
    (version_dir / "original.md").write_text("original\n", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: source-demo\n"
            "name: Source Demo\n"
            "description: Locked ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    client, headers = _build_test_client(tmp_path)

    response = _post(
        client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (
                _build_v01_ontology_zip(version="1.0.0"),
                "source-demo.zip",
            ),
            "overwrite": "true",
        },
    )

    assert response.status_code == 409
    assert response.json()["error_code"] == "deterministic_operation_immutable"
    assert (version_dir / "original.md").read_text(encoding="utf-8") == "original\n"


def test_import_source_zip_missing_version_conflict_can_create_new_version(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "source-demo"
    (ontology_dir / "objects").mkdir(parents=True)
    (ontology_dir / "operations").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: source-demo\n"
            "name: Source Demo\n"
            "description: Existing source ontology\n"
            "version: 1.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (ontology_dir / "objects" / "旧实体.md").write_text("旧实体\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    conflict_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_v01_ontology_zip(), "source-demo.zip")},
    )

    assert conflict_response.status_code == 409
    conflict_payload = conflict_response.json()
    assert conflict_payload["conflict_type"] == "version_exists"
    assert conflict_payload["active_version"] == "1.0.0"

    create_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_v01_ontology_zip(), "source-demo.zip"),
            "action": "create_version",
            "new_version": "2.0.0",
        },
    )

    assert create_response.status_code == 201
    payload = create_response.json()
    assert payload["version"] == "2.0.0"
    assert payload["active_version"] == "2.0.0"
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["active_version"] == "2.0.0"
    assert manifest["versions"] == ["1.0.0", "2.0.0"]
    assert (ontology_dir / "versions" / "1.0.0" / "objects" / "旧实体.md").exists()
    assert (ontology_dir / "versions" / "2.0.0" / "objects" / "实体A.md").exists()


def test_import_duplicate_version_conflict_can_overwrite_or_create_new_version(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")

    first_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_v01_ontology_zip(version="1.0.0"), "source-demo.zip")},
    )
    assert first_response.status_code == 201

    conflict_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_v01_ontology_zip(version="1.0.0"), "source-demo.zip")},
    )
    assert conflict_response.status_code == 409
    assert conflict_response.json()["conflict_type"] == "version_exists"

    create_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_v01_ontology_zip(version="1.0.0"), "source-demo.zip"),
            "action": "create_version",
            "new_version": "1.0.1",
        },
    )
    assert create_response.status_code == 201
    assert create_response.json()["version"] == "1.0.1"

    overwrite_response = _post(client,
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_v01_ontology_zip(version="1.0.1"), "source-demo.zip"),
            "action": "overwrite_matching",
        },
    )
    assert overwrite_response.status_code == 201
    assert overwrite_response.json()["overwritten"] is True
    manifest = yaml.safe_load((workspace_paths.ontologies_dir / "source-demo" / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["active_version"] == "1.0.1"
    assert manifest["versions"] == ["1.0.0", "1.0.1"]


def test_file_routes_and_download_operate_on_selected_version(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "objects").mkdir(parents=True)
    (version2 / "objects").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "objects" / "实体A.md").write_text("v1\n", encoding="utf-8")
    (version2 / "objects" / "实体A.md").write_text("v2\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    read_response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        params={"folder": "objects", "name": "实体A.md", "version": "2.0.0"},
        headers=headers,
    )
    assert read_response.status_code == 200
    assert read_response.json()["content"] == "v2\n"

    save_response = client.put(
        "/agentic/ontologies/demo-ontology/file",
        headers=headers,
        json={"folder": "objects", "name": "实体A.md", "version": "2.0.0", "content": "v2 edited\n"},
    )
    assert save_response.status_code == 200
    assert (version1 / "objects" / "实体A.md").read_text(encoding="utf-8") == "v1\n"
    assert (version2 / "objects" / "实体A.md").read_text(encoding="utf-8") == "v2 edited\n"

    download_response = client.get(
        "/agentic/ontologies/demo-ontology/download",
        params={"version": "2.0.0"},
        headers=headers,
    )
    assert download_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download_response.content), "r") as zf:
        manifest = yaml.safe_load(zf.read("demo-ontology/manifest.yaml").decode("utf-8"))
        assert manifest["version"] == "2.0.0"
        assert manifest["active_version"] == "2.0.0"
        assert manifest["versions"] == ["2.0.0"]
        assert zf.read("demo-ontology/objects/实体A.md").decode("utf-8") == "v2 edited\n"


def test_set_active_version_changes_runtime_version(tmp_path, monkeypatch):
    import ontology.ontologies as ontology_registry_module
    from ontology.ontologies import get_ontology_registry

    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch, owner="modeler")
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "operations").mkdir(parents=True)
    (version2 / "operations").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "operations" / "index.md").write_text("- v1-op\n", encoding="utf-8")
    (version2 / "operations" / "index.md").write_text("- v2-op\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="modeler", role="user")
    response = client.put(
        "/agentic/ontologies/demo-ontology/active-version",
        headers=headers,
        json={"version": "2.0.0"},
    )
    assert response.status_code == 200
    assert response.json()["active_version"] == "2.0.0"

    monkeypatch.setattr(ontology_registry_module, "_global_registry", None)
    ontology = get_ontology_registry().get_ontology("demo-ontology")
    assert ontology.version == "2.0.0"
    assert Path(ontology.ontology_dir) == version2


def test_delete_ontology_version_removes_only_target_version(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "operations").mkdir(parents=True)
    (version2 / "operations").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "operations" / "流程A.md").write_text("v1\n", encoding="utf-8")
    (version2 / "operations" / "流程A.md").write_text("v2\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    response = client.delete(
        "/agentic/ontologies/demo-ontology/versions/2.0.0",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["success"] is True
    assert payload["ontology_id"] == "demo-ontology"
    assert payload["deleted_version"] == "2.0.0"
    assert payload["active_version"] == "1.0.0"
    assert payload["versions"] == ["1.0.0"]
    assert (version1 / "operations" / "流程A.md").read_text(encoding="utf-8") == "v1\n"
    assert not version2.exists()
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["active_version"] == "1.0.0"
    assert manifest["version"] == "1.0.0"
    assert manifest["versions"] == ["1.0.0"]


def test_delete_active_ontology_version_returns_409(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    ontology_dir = workspace_paths.ontologies_dir / "demo-ontology"
    version1 = ontology_dir / "versions" / "1.0.0"
    version2 = ontology_dir / "versions" / "2.0.0"
    (version1 / "operations").mkdir(parents=True)
    (version2 / "operations").mkdir(parents=True)
    (ontology_dir / "manifest.yaml").write_text(
        (
            "kind: ontology-package\n"
            "schema_version: 0.1\n"
            "id: demo-ontology\n"
            "name: Demo Ontology\n"
            "description: Demo ontology\n"
            "version: 1.0.0\n"
            "active_version: 1.0.0\n"
            "versions:\n"
            "  - 1.0.0\n"
            "  - 2.0.0\n"
            "language: zh-CN\n"
        ),
        encoding="utf-8",
    )
    (version1 / "operations" / "流程A.md").write_text("v1\n", encoding="utf-8")
    (version2 / "operations" / "流程A.md").write_text("v2\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="TestBootstrap#2026")
    response = client.delete(
        "/agentic/ontologies/demo-ontology/versions/1.0.0",
        headers=headers,
    )

    assert response.status_code == 409
    payload = response.json()
    assert payload["conflict_type"] == "active_version"
    assert payload["ontology_id"] == "demo-ontology"
    assert payload["version"] == "1.0.0"
    assert "激活版本" in payload["error"]
    assert (version1 / "operations" / "流程A.md").read_text(encoding="utf-8") == "v1\n"
    assert (version2 / "operations" / "流程A.md").read_text(encoding="utf-8") == "v2\n"
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["active_version"] == "1.0.0"
    assert manifest["versions"] == ["1.0.0", "2.0.0"]


def test_replace_ontology_dir_keeps_existing_dir_when_atomic_exchange_fails(
    tmp_path,
    monkeypatch,
):
    ontology_dir = tmp_path / "source-demo"
    staged_dir = tmp_path / "staged-source-demo"
    ontology_dir.mkdir(parents=True)
    staged_dir.mkdir(parents=True)
    (ontology_dir / "old.md").write_text("old\n", encoding="utf-8")
    (staged_dir / "new.md").write_text("new\n", encoding="utf-8")

    monkeypatch.setattr(
        ontologies_module,
        "portable_replace_directory",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            PermissionError("staged dir is busy")
        ),
    )

    try:
        ontologies_module._replace_ontology_dir(staged_dir=staged_dir, ontology_dir=ontology_dir)
    except PermissionError as exc:
        assert str(exc) == "staged dir is busy"
    else:
        raise AssertionError("expected replace failure")

    assert (ontology_dir / "old.md").read_text(encoding="utf-8") == "old\n"
    assert not (ontology_dir / "new.md").exists()
    assert (staged_dir / "new.md").read_text(encoding="utf-8") == "new\n"


def test_replace_ontology_dir_atomically_exchanges_existing_tree(tmp_path):
    ontology_dir = tmp_path / "source-demo"
    staged_dir = tmp_path / "staged-source-demo"
    ontology_dir.mkdir(parents=True)
    staged_dir.mkdir(parents=True)
    (ontology_dir / "old.md").write_text("old\n", encoding="utf-8")
    (staged_dir / "new.md").write_text("new\n", encoding="utf-8")

    ontologies_module._replace_ontology_dir(
        staged_dir=staged_dir,
        ontology_dir=ontology_dir,
    )

    assert (ontology_dir / "new.md").read_text(encoding="utf-8") == "new\n"
    assert not (ontology_dir / "old.md").exists()
    assert not staged_dir.exists()


def test_replace_ontology_dir_uses_portable_backup_when_exchange_is_unavailable(
    tmp_path,
    monkeypatch,
):
    ontology_dir = tmp_path / "knowledge-package-demo"
    staged_dir = tmp_path / "staged-knowledge-package-demo"
    ontology_dir.mkdir()
    staged_dir.mkdir()
    (ontology_dir / "old.md").write_text("old\n", encoding="utf-8")
    (staged_dir / "new.md").write_text("new\n", encoding="utf-8")
    ontologies_module._replace_ontology_dir(
        staged_dir=staged_dir,
        ontology_dir=ontology_dir,
    )

    assert (ontology_dir / "new.md").read_text(encoding="utf-8") == "new\n"
    assert not staged_dir.exists()
    assert not list(tmp_path.glob(".knowledge-package-demo.replace-backup-*"))


def test_ontology_id_keeps_legacy_dot_compatibility():
    assert ontologies_module._is_valid_ontology_id("factory.v2") is True


def test_copy_versioned_package_keeps_existing_dir_when_copy_fails(tmp_path, monkeypatch):
    source_dir = tmp_path / "source"
    version_dir = tmp_path / "ontology" / "versions" / "1.0.0"
    source_dir.mkdir()
    version_dir.mkdir(parents=True)
    (source_dir / "first.md").write_text("first\n", encoding="utf-8")
    (source_dir / "second.md").write_text("second\n", encoding="utf-8")
    (version_dir / "old.md").write_text("old\n", encoding="utf-8")
    original_copy2 = ontologies_module.shutil.copy2

    def fail_second_copy(source, target, *args, **kwargs):
        if Path(source).name == "second.md":
            raise OSError("disk full")
        return original_copy2(source, target, *args, **kwargs)

    monkeypatch.setattr(ontologies_module.shutil, "copy2", fail_second_copy)

    try:
        ontologies_module._copy_versioned_package_contents(source_dir, version_dir)
    except OSError as exc:
        assert str(exc) == "disk full"
    else:
        raise AssertionError("expected copy failure")

    assert (version_dir / "old.md").read_text(encoding="utf-8") == "old\n"
    assert not (version_dir / "first.md").exists()
    assert not (version_dir / "second.md").exists()


def test_concurrent_version_installs_are_serialized_without_lost_updates(
    tmp_path,
    monkeypatch,
):
    ontology_dir = tmp_path / "ontologies" / "demo"
    version1 = ontology_dir / "versions" / "1.0.0"
    version1.mkdir(parents=True)
    (version1 / "base.txt").write_text("base", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        "kind: ontology-package\nschema_version: 0.1\nid: demo\n"
        "version: 1.0.0\nactive_version: 1.0.0\nversions:\n  - 1.0.0\n",
        encoding="utf-8",
    )
    sources = []
    for version in ("2.0.0", "3.0.0"):
        source = tmp_path / "sources" / version
        source.mkdir(parents=True)
        (source / "value.txt").write_text(version, encoding="utf-8")
        sources.append((version, source))

    original_copytree = ontologies_module.shutil.copytree
    first_copy_entered = threading.Event()
    allow_first_copy = threading.Event()
    active_copies = 0
    maximum_active_copies = 0
    counter_lock = threading.Lock()

    def guarded_copytree(source, target, *args, **kwargs):
        nonlocal active_copies, maximum_active_copies
        is_ontology_snapshot = Path(source).resolve() == ontology_dir.resolve()
        if is_ontology_snapshot:
            with counter_lock:
                active_copies += 1
                maximum_active_copies = max(maximum_active_copies, active_copies)
                first = not first_copy_entered.is_set()
                first_copy_entered.set()
            if first:
                assert allow_first_copy.wait(5)
        try:
            return original_copytree(source, target, *args, **kwargs)
        finally:
            if is_ontology_snapshot:
                with counter_lock:
                    active_copies -= 1

    monkeypatch.setattr(ontologies_module.shutil, "copytree", guarded_copytree)
    errors = []

    def install(version, source):
        try:
            ontologies_module._install_ontology_version_package(
                source_root=source,
                ontology_dir=ontology_dir,
                ontology_id="demo",
                ontology_name="Demo",
                ontology_description="",
                target_version=version,
                existed=True,
            )
        except Exception as exc:  # surfaced in the assertion below
            errors.append(exc)

    first = threading.Thread(target=install, args=sources[0])
    second = threading.Thread(target=install, args=sources[1])
    first.start()
    assert first_copy_entered.wait(5)
    second.start()
    allow_first_copy.set()
    first.join(5)
    second.join(5)

    assert not first.is_alive()
    assert not second.is_alive()
    assert errors == []
    assert maximum_active_copies == 1
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["versions"] == ["1.0.0", "2.0.0", "3.0.0"]
    assert (ontology_dir / "versions" / "2.0.0" / "value.txt").read_text() == "2.0.0"
    assert (ontology_dir / "versions" / "3.0.0" / "value.txt").read_text() == "3.0.0"


def test_install_and_status_mutation_share_one_ontology_lock(tmp_path, monkeypatch):
    ontology_dir = tmp_path / "ontologies" / "demo"
    version1 = ontology_dir / "versions" / "1.0.0"
    version1.mkdir(parents=True)
    (version1 / "base.txt").write_text("base", encoding="utf-8")
    (ontology_dir / "manifest.yaml").write_text(
        "kind: ontology-package\nschema_version: 0.1\nid: demo\n"
        "name: Demo\ndescription: Demo ontology\nlanguage: zh-CN\n"
        "enabled: true\nversion: 1.0.0\nactive_version: 1.0.0\n"
        "versions:\n  - 1.0.0\n",
        encoding="utf-8",
    )
    source = tmp_path / "source"
    source.mkdir()
    (source / "value.txt").write_text("2.0.0", encoding="utf-8")

    original_copytree = ontologies_module.shutil.copytree
    snapshot_started = threading.Event()
    allow_snapshot = threading.Event()
    mutation_started = threading.Event()
    mutation_finished = threading.Event()
    errors = []

    def guarded_copytree(source_path, target, *args, **kwargs):
        if Path(source_path).resolve() == ontology_dir.resolve():
            snapshot_started.set()
            assert allow_snapshot.wait(5)
        return original_copytree(source_path, target, *args, **kwargs)

    monkeypatch.setattr(ontologies_module.shutil, "copytree", guarded_copytree)

    def install():
        try:
            ontologies_module._install_ontology_version_package(
                source_root=source,
                ontology_dir=ontology_dir,
                ontology_id="demo",
                ontology_name="Demo",
                ontology_description="",
                target_version="2.0.0",
                existed=True,
            )
        except Exception as exc:
            errors.append(exc)

    def disable():
        mutation_started.set()
        try:
            ontologies_module._set_ontology_enabled(ontology_dir, False)
        except Exception as exc:
            errors.append(exc)
        finally:
            mutation_finished.set()

    install_thread = threading.Thread(target=install)
    mutation_thread = threading.Thread(target=disable)
    install_thread.start()
    assert snapshot_started.wait(5)
    mutation_thread.start()
    assert mutation_started.wait(5)
    assert not mutation_finished.wait(0.1)
    allow_snapshot.set()
    install_thread.join(5)
    mutation_thread.join(5)

    assert not install_thread.is_alive()
    assert not mutation_thread.is_alive()
    assert errors == []
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["enabled"] is False
    assert manifest["versions"] == ["1.0.0", "2.0.0"]
    assert (ontology_dir / "versions" / "2.0.0" / "value.txt").read_text() == "2.0.0"
