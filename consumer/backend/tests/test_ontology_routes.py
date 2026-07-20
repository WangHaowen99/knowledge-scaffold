"""Tests for ontology management routes."""

import io
import json
import zipfile
from pathlib import Path

import yaml
from flask import Flask

from api.routes import ontologies as ontologies_module
from api.routes.ontologies import ontologies_bp
from api.auth.auth import AuthManager
from workspace.workspaces import create_workspace, get_workspace_paths


EXAMPLE_ONTOLOGY_ID = "example-domain-knowledge-agent"


def _build_test_client(
    tmp_path,
    username: str = "admin",
    password: str = "admin123",
    role: str = "admin",
):
    app = Flask(__name__)
    app.config["TESTING"] = True

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

    app.config["AUTH_MANAGER"] = auth_manager
    app.register_blueprint(ontologies_bp)

    client = app.test_client()
    token = auth_manager.issue_token(user)
    headers = {"Authorization": "Bearer {}".format(token)}
    return client, headers


def _create_demo_workspace(tmp_path, monkeypatch, workspace_id: str = "demo"):
    runtime_root = tmp_path / ".onto-runtime"
    monkeypatch.setenv("PROJECT_ROOT", str(runtime_root))
    create_workspace("Demo Workspace", workspace_id=workspace_id, set_as_default=True)
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
            "description: Example DOMAIN ontology for route tests\n"
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


def _build_zip_file(entries: dict[str, str]) -> io.BytesIO:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for path, content in entries.items():
            zf.writestr(path, content)
    buffer.seek(0)
    return buffer


def _build_source_ontology_zip(*, include_scripts: bool, version: str = "") -> io.BytesIO:
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
            "实体甲,实体乙",
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


def test_list_ontologies_returns_example_manifest(tmp_path, monkeypatch):
    _write_example_ontology(_create_demo_workspace(tmp_path, monkeypatch, workspace_id="example-routes"))
    client, headers = _build_test_client(tmp_path)

    response = client.get("/agentic/ontologies", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
    assert payload is not None
    example_manifest = next(item for item in payload["ontologies"] if item["id"] == EXAMPLE_ONTOLOGY_ID)
    assert example_manifest["manifest_path"].endswith("manifest.yaml")


def test_list_ontologies_uses_project_root_from_env(tmp_path, monkeypatch):
    import ontology.ontologies as ontologies_module

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

    monkeypatch.setattr(ontologies_module, "_global_registry", None)

    client, headers = _build_test_client(tmp_path)
    response = client.get("/agentic/ontologies", headers=headers)

    assert response.status_code == 200
    payload = response.get_json()
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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    response = client.put(
        "/agentic/ontologies/demo-ontology/status",
        headers=headers,
        json={"enabled": False},
    )

    assert response.status_code == 200
    payload = response.get_json()
    assert payload == {"success": True, "ontology_id": "demo-ontology", "enabled": False}

    manifest = (ontology_dir / "manifest.yaml").read_text(encoding="utf-8")
    assert "enabled: false" in manifest

    list_response = client.get("/agentic/ontologies", headers=headers)
    list_payload = list_response.get_json()
    assert list_payload["ontologies"][0]["enabled"] is False


def test_get_ontology_file_reads_object_markdown(tmp_path, monkeypatch):
    _write_example_ontology(_create_demo_workspace(tmp_path, monkeypatch, workspace_id="example-file"))
    client, headers = _build_test_client(tmp_path)

    response = client.get(
        f"/agentic/ontologies/{EXAMPLE_ONTOLOGY_ID}/file",
        query_string={"folder": "objects", "name": "TargetItem.md"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
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
        query_string={"folder": "assets"},
        headers=headers,
    )

    assert list_response.status_code == 200
    assert [item["name"] for item in list_response.get_json()["files"]] == [
        "rules/limits.ini"
    ]

    read_response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        query_string={"folder": "assets", "name": "rules/limits.ini"},
        headers=headers,
    )
    assert read_response.status_code == 200
    assert read_response.get_json()["content"] == "[limits]\nmax=10"


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
        query_string={"folder": "objects", "name": "实体A.md"},
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
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
    assert "字符" in response.get_json()["error"]


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
    assert "单实体属性数量超过上限" in response.get_json()["error"]
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
    response = client.post(
        "/agentic/ontologies/demo-ontology/upload",
        headers=headers,
        data={
            "folder": "objects",
            "file": (io.BytesIO("#### 对象描述\n实体B\n".encode("utf-8")), "实体B.md"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "单本体实体数量已达上限" in response.get_json()["error"]
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
    response = client.post(
        "/agentic/ontologies/demo-ontology/upload",
        headers=headers,
        data={
            "folder": "operations",
            "file": (io.BytesIO("#### 操作描述\n流程B\n".encode("utf-8")), "流程B.md"),
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "单本体流程数量已达上限" in response.get_json()["error"]
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
    assert response.mimetype == "application/zip"
    assert "demo-ontology-1.0.0.zip" in response.headers["Content-Disposition"]

    zip_buffer = io.BytesIO(response.data)
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
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload is not None
    assert payload["success"] is True
    assert payload["created"] is True
    assert payload["overwritten"] is False
    assert payload["import_mode"] == "converted"
    assert payload["ontology_id"] == "converted-demo"
    assert payload["ontology_name"] == "Converted Demo"
    assert payload["objects_count"] == 1
    assert payload["operations_count"] == 1
    assert payload["warnings"] == {"objects": [], "objects_index": [], "operations": []}
    assert payload["preview"]["source"]["total_files"] == 7
    assert payload["preview"]["result"]["total_files"] >= 10
    source_paths = {item["path"] for item in payload["preview"]["source"]["files"]}
    result_paths = {item["path"] for item in payload["preview"]["result"]["files"]}
    assert "manifest.yaml" in source_paths
    assert "objects/实体A.md" in result_paths
    assert "objects/index.md" in result_paths
    assert "operations/index.md" in result_paths
    assert not any(path.endswith("-list.md") for path in result_paths)
    assert "ONTOLOGY.md" not in result_paths

    ontology_dir = workspace_paths.ontologies_dir / "converted-demo"
    assert (ontology_dir / "manifest.yaml").read_text(encoding="utf-8").startswith(
        "kind: ontology-package\nschema_version: 0.1\n"
    )
    version_dir = ontology_dir / "versions" / "1.0.0"
    assert (version_dir / "objects" / "实体A.md").read_text(encoding="utf-8") == "#### 对象描述\n实体A说明\n"
    assert (version_dir / "operations" / "流程A.md").read_text(encoding="utf-8") == operation_content
    assert (version_dir / "scripts" / "demo.py").read_text(encoding="utf-8") == "print('demo')\n"


def test_import_converted_ontology_zip_rejects_operation_over_char_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "字符" in response.get_json()["error"]


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
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "本体数量已达上限" in response.get_json()["error"]
    assert not (workspace_paths.ontologies_dir / "converted-demo").exists()


def test_import_converted_ontology_zip_rejects_object_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OBJECT_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "单本体实体数量已达上限" in response.get_json()["error"]


def test_import_converted_ontology_zip_rejects_operation_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OPERATION_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "单本体流程数量已达上限" in response.get_json()["error"]


def test_import_converted_ontology_zip_rejects_entity_attribute_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ENTITY_ATTRIBUTE_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "单实体属性数量超过上限" in response.get_json()["error"]


def test_import_source_ontology_zip_rejects_generated_object_count_over_limit(tmp_path, monkeypatch):
    _create_demo_workspace(tmp_path, monkeypatch)
    monkeypatch.setenv("ONTOLOGY_OBJECT_MAX_COUNT", "1")
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "source-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 400
    assert "单本体实体数量已达上限" in response.get_json()["error"]


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
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
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

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (zip_buffer, "bound-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
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

    conflict_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_zip_file(zip_entries), "converted-demo.zip")},
        content_type="multipart/form-data",
    )

    assert conflict_response.status_code == 409
    conflict_payload = conflict_response.get_json()
    assert conflict_payload is not None
    assert conflict_payload["requires_confirmation"] is True
    assert conflict_payload["ontology_id"] == "converted-demo"

    overwrite_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_zip_file(zip_entries), "converted-demo.zip"),
            "overwrite": "true",
        },
        content_type="multipart/form-data",
    )

    assert overwrite_response.status_code == 201
    overwrite_payload = overwrite_response.get_json()
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
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=True), "source-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload is not None
    assert payload["import_mode"] == "source"
    assert payload["ontology_id"] == "source-demo"
    assert payload["preview"]["source"]["total_files"] == 5
    assert payload["preview"]["result"]["total_files"] >= 9
    source_paths = {item["path"] for item in payload["preview"]["source"]["files"]}
    result_paths = {item["path"] for item in payload["preview"]["result"]["files"]}
    assert "object/实体A.md" in source_paths
    assert "action/process_a.py" in source_paths
    assert "objects/index.md" in result_paths
    assert "objects/实体A.md" in result_paths
    assert not any(path.startswith("assets/") for path in result_paths)
    result_object = next(item for item in payload["preview"]["result"]["files"] if item["path"] == "objects/实体A.md")
    assert "| 实体A编号 | EntityACode | 实体A编号描述 | A编号 |" in result_object["content"]

    ontology_dir = workspace_paths.ontologies_dir / "source-demo"
    version_dir = ontology_dir / "versions" / "1.0.0"
    scripts_index_content = (version_dir / "scripts" / "index.md").read_text(encoding="utf-8")
    script_descriptor = (version_dir / "scripts" / "process_a.md").read_text(encoding="utf-8")
    assert "| process_a | process_a | 流程A | process_a.py |" in scripts_index_content
    assert "bound_operation: 流程A" in script_descriptor
    assert "| 实体A编号 | --entity-a-code | string | 是 | 否 | 实体A编号 | entity_a_code |" in script_descriptor
    assert (version_dir / "scripts" / "process_a.py").read_text(encoding="utf-8") == (
        "# 工具流程: 流程A\n"
        "# parameters: [{\"name\":\"实体A编号\",\"flag\":\"--entity-a-code\",\"description\":\"实体A编号\",\"required\":true,\"type\":\"string\"}]\n"
        "#查数脚本 （涉及mindsdb查数的脚本会加这个标签）\n"
        "print('process_a')\n"
    )
    assert (version_dir / "scripts" / ".env").read_text(encoding="utf-8") == "API_TOKEN=demo-token\n"
    assert ".env" not in scripts_index_content
    assert not (version_dir / "assets").exists()


def test_import_source_ontology_zip_allows_missing_optional_scripts(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=False), "source-demo.zip")},
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload is not None
    assert payload["import_mode"] == "source"

    ontology_dir = workspace_paths.ontologies_dir / "source-demo"
    version_dir = ontology_dir / "versions" / "1.0.0"
    assert not list(version_dir.glob("*-list.md"))
    assert (version_dir / "scripts" / "index.md").is_file()
    assert list((version_dir / "scripts").glob("*.md")) == [version_dir / "scripts" / "index.md"]


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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

    response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_source_ontology_zip(include_scripts=False), "source-demo.zip"),
            "overwrite": "true",
        },
        content_type="multipart/form-data",
    )

    assert response.status_code == 201
    payload = response.get_json()
    assert payload is not None
    assert payload["import_mode"] == "source"
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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    conflict_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=False), "source-demo.zip")},
        content_type="multipart/form-data",
    )

    assert conflict_response.status_code == 409
    conflict_payload = conflict_response.get_json()
    assert conflict_payload["conflict_type"] == "missing_version_existing"
    assert conflict_payload["active_version"] == "1.0.0"

    create_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_source_ontology_zip(include_scripts=False), "source-demo.zip"),
            "action": "create_version",
            "new_version": "2.0.0",
        },
        content_type="multipart/form-data",
    )

    assert create_response.status_code == 201
    payload = create_response.get_json()
    assert payload["version"] == "2.0.0"
    assert payload["active_version"] == "2.0.0"
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["active_version"] == "2.0.0"
    assert manifest["versions"] == ["1.0.0", "2.0.0"]
    assert (ontology_dir / "versions" / "1.0.0" / "objects" / "旧实体.md").exists()
    assert (ontology_dir / "versions" / "2.0.0" / "objects" / "实体A.md").exists()


def test_import_duplicate_version_conflict_can_overwrite_or_create_new_version(tmp_path, monkeypatch):
    workspace_paths = _create_demo_workspace(tmp_path, monkeypatch)
    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")

    first_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=False, version="1.0.0"), "source-demo.zip")},
        content_type="multipart/form-data",
    )
    assert first_response.status_code == 201

    conflict_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={"file": (_build_source_ontology_zip(include_scripts=False, version="1.0.0"), "source-demo.zip")},
        content_type="multipart/form-data",
    )
    assert conflict_response.status_code == 409
    assert conflict_response.get_json()["conflict_type"] == "version_exists"

    create_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_source_ontology_zip(include_scripts=False, version="1.0.0"), "source-demo.zip"),
            "action": "create_version",
            "new_version": "1.0.1",
        },
        content_type="multipart/form-data",
    )
    assert create_response.status_code == 201
    assert create_response.get_json()["version"] == "1.0.1"

    overwrite_response = client.post(
        "/agentic/ontologies/import-zip",
        headers=headers,
        data={
            "file": (_build_source_ontology_zip(include_scripts=False, version="1.0.1"), "source-demo.zip"),
            "action": "overwrite_matching",
        },
        content_type="multipart/form-data",
    )
    assert overwrite_response.status_code == 201
    assert overwrite_response.get_json()["overwritten"] is True
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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    read_response = client.get(
        "/agentic/ontologies/demo-ontology/file",
        query_string={"folder": "objects", "name": "实体A.md", "version": "2.0.0"},
        headers=headers,
    )
    assert read_response.status_code == 200
    assert read_response.get_json()["content"] == "v2\n"

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
        query_string={"version": "2.0.0"},
        headers=headers,
    )
    assert download_response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(download_response.data), "r") as zf:
        manifest = yaml.safe_load(zf.read("demo-ontology/manifest.yaml").decode("utf-8"))
        assert manifest["version"] == "2.0.0"
        assert manifest["active_version"] == "2.0.0"
        assert manifest["versions"] == ["2.0.0"]
        assert zf.read("demo-ontology/objects/实体A.md").decode("utf-8") == "v2 edited\n"


def test_set_active_version_changes_runtime_version(tmp_path, monkeypatch):
    import ontology.ontologies as ontology_registry_module
    from ontology.ontologies import get_ontology_registry

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
    (version1 / "operations" / "index.md").write_text("- v1-op\n", encoding="utf-8")
    (version2 / "operations" / "index.md").write_text("- v2-op\n", encoding="utf-8")

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    response = client.put(
        "/agentic/ontologies/demo-ontology/active-version",
        headers=headers,
        json={"version": "2.0.0"},
    )
    assert response.status_code == 200
    assert response.get_json()["active_version"] == "2.0.0"

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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    response = client.delete(
        "/agentic/ontologies/demo-ontology/versions/2.0.0",
        headers=headers,
    )

    assert response.status_code == 200
    payload = response.get_json()
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

    client, headers = _build_test_client(tmp_path, username="admin", password="admin123")
    response = client.delete(
        "/agentic/ontologies/demo-ontology/versions/1.0.0",
        headers=headers,
    )

    assert response.status_code == 409
    payload = response.get_json()
    assert payload["conflict_type"] == "active_version"
    assert payload["ontology_id"] == "demo-ontology"
    assert payload["version"] == "1.0.0"
    assert "激活版本" in payload["error"]
    assert (version1 / "operations" / "流程A.md").read_text(encoding="utf-8") == "v1\n"
    assert (version2 / "operations" / "流程A.md").read_text(encoding="utf-8") == "v2\n"
    manifest = yaml.safe_load((ontology_dir / "manifest.yaml").read_text(encoding="utf-8"))
    assert manifest["active_version"] == "1.0.0"
    assert manifest["versions"] == ["1.0.0", "2.0.0"]


def test_replace_ontology_dir_restores_existing_dir_when_switch_fails(tmp_path, monkeypatch):
    ontology_dir = tmp_path / "source-demo"
    staged_dir = tmp_path / "staged-source-demo"
    ontology_dir.mkdir(parents=True)
    staged_dir.mkdir(parents=True)
    (ontology_dir / "old.md").write_text("old\n", encoding="utf-8")
    (staged_dir / "new.md").write_text("new\n", encoding="utf-8")

    original_replace = Path.replace

    def fake_replace(self: Path, target):
        if self.resolve() == staged_dir.resolve():
            raise PermissionError("staged dir is busy")
        return original_replace(self, target)

    monkeypatch.setattr(Path, "replace", fake_replace)

    try:
        ontologies_module._replace_ontology_dir(staged_dir=staged_dir, ontology_dir=ontology_dir)
    except PermissionError as exc:
        assert str(exc) == "staged dir is busy"
    else:
        raise AssertionError("expected replace failure")

    assert (ontology_dir / "old.md").read_text(encoding="utf-8") == "old\n"
    assert not (ontology_dir / "new.md").exists()
    assert (staged_dir / "new.md").read_text(encoding="utf-8") == "new\n"
