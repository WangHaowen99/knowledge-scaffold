"""知识包版本对比（API 契约 3.20 PC 组，ADR-0095）：上传与发布版本两种来源、ZIP 安全校验、异步任务、配额与清理。

自 develop 的同名测试移植：发布版本的来源从 Hub release 改为本项目版本号（publish.release_package）。数据全部合成。
"""

import io
import random
import struct
import threading
import time
import unicodedata
import zipfile
from concurrent.futures import Future, ThreadPoolExecutor, wait
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app import config
from app.api import deps
from app.main import app
from app.routers.package_compare import PackageCompareBody
from app.services import auth, package_compare, publish
from app.services.workspace import WorkspaceError, WorkspaceManager, _git


def archive(root="", package_id="synthetic", schema_version="0.1", **files):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as value:
        prefix = f"{root.strip('/')}/" if root else ""
        manifest = (
            "kind: ontology-package\n"
            f"schema_version: '{schema_version}'\n"
            f"id: {package_id}\n"
            "version: 0.1.1\nname: Synthetic\ndescription: Synthetic package\n"
        )
        if schema_version == "0.2":
            manifest += "solver_layout: operation-centric.v1\n"
        value.writestr(prefix + "manifest.yaml", manifest)
        for name, content in files.items():
            value.writestr(prefix + name, content)
    output.seek(0)
    return output


@pytest.fixture
def comparison_root(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(package_compare, "_SERVICE_OWNER", True)
    # 别的测试跑过应用生命周期会把服务标成已停（shutdown）
    monkeypatch.setattr(package_compare, "_SERVICE_AVAILABLE", True)
    monkeypatch.setattr(package_compare, "_STORAGE_RESERVATIONS", {})
    monkeypatch.setattr(package_compare, "_RESULT_RESERVATIONS", set())
    if package_compare._EXECUTOR._shutdown:
        # 同一进程里别的测试跑过应用生命周期，shutdown 停掉了线程池
        monkeypatch.setattr(package_compare, "_EXECUTOR", ThreadPoolExecutor(max_workers=2))
    yield "synthetic-subject"
    drain_tasks()


def drain_tasks():
    """等本测试提交的后台比较跑完：任务结束才释放预留、写结果，留着会写进下一个测试的数据目录。"""
    if not isinstance(package_compare._EXECUTOR, ThreadPoolExecutor):
        return  # 测试换上的假执行器，任务永远不会结束
    with package_compare._TASK_LOCK:
        pending = [future for future in package_compare._RUNNING.values() if future is not None]
    wait(pending, timeout=10)


# 等待上限（join、wait、轮询次数）只是兜底：每次解析都起一个子进程，机器忙时 develop 原来的 2 秒不够；断言不变

def fake_release(monkeypatch, tmp_path, payload: bytes, calls=None, before=None):
    """publish.release_package 的替身：按版本返回一份留存包。"""
    def release_package(manager, project_id, version, *rest, token=""):
        if calls is not None:
            calls.append((manager, project_id, version))
        tokens.append(token)
        if before:
            before()
        stored = tmp_path / "release-store" / f"{project_id}-{version}.zip"
        stored.parent.mkdir(parents=True, exist_ok=True)
        stored.write_bytes(payload)
        return stored, {"version": version, "sha256": "0" * 64}

    monkeypatch.setattr(publish, "release_package", release_package)
    tokens = []
    return tokens


def test_modeling_export_is_accepted_as_comparison_input(
    comparison_root, tmp_path,
):
    manager = WorkspaceManager(tmp_path / "workspace-data", Path(config.ASSETS_DIR))
    project = manager.create_project("Comparison export contract")
    project_root = manager.project_root(project["id"])
    (project_root / "ontology/object/order.md").write_text(
        "# Order\n\nA synthetic order object.\n",
        encoding="utf-8",
    )
    exported = publish.export_package_zip(
        manager,
        project["id"],
        tmp_path / "exported-package.zip",
        allow_unbound=True,
    )

    with exported.open("rb") as upload:
        uploaded = package_compare.create_input(
            comparison_root,
            upload,
            exported.name,
            project["id"],
        )

    _archive_path, metadata = package_compare._load_input(
        comparison_root,
        uploaded["input_id"],
        project["id"],
    )
    assert metadata["package_id"] == project["id"]
    # 本分支未发布导出的版本号按主线计（develop 固定 0.1.1），以导出包自己的 manifest 为准
    with zipfile.ZipFile(exported) as package:
        manifest_name = next(name for name in package.namelist() if name.endswith("/manifest.yaml") or name == "manifest.yaml")
        exported_version = yaml.safe_load(package.read(manifest_name))["version"]
    assert metadata["package_version"] == exported_version
    assert metadata["file_count"] == uploaded["file_count"]
    assert {"manifest.yaml", "index.md", "objects/index.md", "objects/order.md"} <= set(
        metadata["files"]
    )


def test_upload_compare_reports_four_file_states_and_categories(comparison_root):
    left = package_compare.create_input(comparison_root, archive(**{
        "objects/a.md": "a\n", "objects/changed.md": "old\n", "rules/removed.md": "gone\n",
    }), "left.zip")
    right = package_compare.create_input(comparison_root, archive(**{
        "objects/a.md": "a\n", "objects/changed.md": "new\n", "objects/added.md": "new\n",
    }), "right.zip")
    result = package_compare.create_comparison("project_alpha", comparison_root, {
        "kind": "upload", "input_id": left["input_id"]
    }, {"kind": "upload", "input_id": right["input_id"]})
    assert result["status"] == "ready"
    assert result["summary"]["files"] == {"added": 1, "deleted": 1, "modified": 1, "unchanged": 2}
    rows = package_compare.list_files(comparison_root, "project_alpha", result["comparison_id"], status="modified")
    assert rows["total"] == 1 and rows["items"][0]["path"] == "objects/changed.md"
    diff = package_compare.file_diff(
        "project_alpha", comparison_root, result["comparison_id"], "objects/changed.md",
    )
    assert diff["left"]["text"] == "old\n" and diff["right"]["text"] == "new\n"


def test_compare_source_models_reject_invalid_field_types():
    with pytest.raises(ValidationError):
        PackageCompareBody.model_validate({
            "left": {
                "kind": "release",
                "version": ["0.1.0"],
            },
            "right": {"kind": "upload", "input_id": "cmpin_demo"},
        })
    with pytest.raises(ValidationError):
        PackageCompareBody.model_validate({
            "left": {"kind": "frozen", "side": "left"},
            "right": {"kind": "upload", "input_id": "cmpin_demo"},
        })


def test_service_rejects_invalid_source_field_types(comparison_root):
    with pytest.raises(WorkspaceError) as error:
        package_compare.start_comparison(
            None, "project_alpha",
            comparison_root,
            {
                "kind": "release",
                "version": ["0.1.0"],
            },
            {"kind": "upload", "input_id": "cmpin_demo"},
        )
    assert error.value.status == 400


def test_upload_rejects_bad_zip_and_path_traversal(comparison_root):
    with pytest.raises(WorkspaceError, match="有效的 ZIP"):
        package_compare.create_input(comparison_root, io.BytesIO(b"not zip"))
    bad = io.BytesIO()
    with zipfile.ZipFile(bad, "w") as value:
        value.writestr("../escape.md", "bad")
    bad.seek(0)
    with pytest.raises(WorkspaceError, match="不安全"):
        package_compare.create_input(comparison_root, bad)


@pytest.mark.parametrize("name, message", [
    (".git/config", r"\.git"),
    ("objects/bad\x01name.md", "控制字符"),
])
def test_upload_rejects_workspace_metadata_and_control_characters(comparison_root, name, message):
    with pytest.raises(WorkspaceError, match=message):
        package_compare.create_input(comparison_root, archive(**{name: "synthetic"}))


def test_upload_rejects_backslash_paths(comparison_root):
    with pytest.raises(WorkspaceError, match="反斜杠"):
        package_compare.create_input(
            comparison_root, archive(**{"objects\\legacy.md": "synthetic"}),
        )


@pytest.mark.parametrize("prefix", [b"", b"self-extracting-prefix"])
def test_upload_rejects_nul_in_original_zip_member_name(comparison_root, prefix):
    data = archive(**{"objects/ab.md": "synthetic"}).getvalue()
    unsafe_name = b"objects/a\x00.md"
    assert len(unsafe_name) == len(b"objects/ab.md")
    data = prefix + data.replace(b"objects/ab.md", unsafe_name)
    with pytest.raises(WorkspaceError, match="NUL"):
        package_compare.create_input(comparison_root, io.BytesIO(data))


def test_upload_rejects_excessive_path_entries_and_manifest_depth(
    comparison_root, monkeypatch,
):
    monkeypatch.setattr(package_compare, "MAX_PATH_BYTES", 32)
    with pytest.raises(WorkspaceError, match="路径超过长度限制"):
        package_compare.create_input(
            comparison_root,
            archive(**{f"objects/{'x' * 40}.md": "synthetic"}),
        )

    monkeypatch.setattr(package_compare, "MAX_PATH_BYTES", 1024)
    monkeypatch.setattr(package_compare, "MAX_ENTRIES", 1)
    with pytest.raises(WorkspaceError, match="条目数量超过限制"):
        package_compare.create_input(
            comparison_root, archive(**{"objects/a.md": "synthetic"}),
        )

    monkeypatch.setattr(package_compare, "MAX_ENTRIES", 4000)
    nested = "value"
    for _ in range(17):
        nested = {"nested": nested}
    value = archive(**{"objects/a.md": "synthetic"})
    rewritten = io.BytesIO()
    with zipfile.ZipFile(value) as source, zipfile.ZipFile(rewritten, "w") as target:
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "manifest.yaml":
                import yaml
                manifest = yaml.safe_load(content)
                manifest["extra"] = nested
                content = yaml.safe_dump(manifest).encode()
            target.writestr(info.filename, content)
    rewritten.seek(0)
    with pytest.raises(WorkspaceError, match="嵌套层级超过限制"):
        package_compare.create_input(comparison_root, rewritten)


def test_archive_categories_ignore_package_root_folder(comparison_root):
    item = package_compare.create_input(comparison_root, archive(root="package_demo", **{
        "objects/a.md": "object",
        "rules/a.md": "rule",
        "metadata/detail.json": "{}",
    }))
    _path, meta = package_compare._load_input(comparison_root, item["input_id"])
    categories = {path: value["category"] for path, value in meta["files"].items()}
    assert categories["objects/a.md"] == "objects"
    assert categories["rules/a.md"] == "rules"
    assert categories["metadata/detail.json"] == "metadata"
    assert categories["manifest.yaml"] == "metadata"


def test_wrapper_directory_does_not_change_comparison_paths(comparison_root):
    files = {"objects/a.md": "same"}
    left = package_compare.create_input(comparison_root, archive(**files), project_id="project_alpha")
    right = package_compare.create_input(
        comparison_root, archive(root="exported-package", **files), project_id="project_alpha",
    )
    result = package_compare.create_comparison(
        "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    assert result["summary"]["files"] == {
        "added": 0, "deleted": 0, "modified": 0, "unchanged": 2,
    }


@pytest.mark.parametrize("builder, message", [
    (lambda value: (
        value.writestr(
            "manifest.yaml",
            "kind: ontology-package\nschema_version: '0.1'\nid: synthetic\n"
            "version: 1\nname: Synthetic\ndescription: x\n",
        ),
        value.writestr("objects/a.md", "file"),
        value.writestr("objects/a.md/", ""),
    ), "文件与目录冲突"),
    (lambda value: value.writestr("/manifest.yaml", "bad"), "绝对路径"),
])
def test_archive_rejects_unsafe_member_layout(comparison_root, builder, message):
    value = io.BytesIO()
    with zipfile.ZipFile(value, "w") as archive_file:
        builder(archive_file)
    value.seek(0)
    with pytest.raises(WorkspaceError, match=message):
        package_compare.create_input(comparison_root, value)


def test_archive_rejects_manifest_only_and_files_outside_wrapper(comparison_root):
    with pytest.raises(WorkspaceError, match="没有可比较内容"):
        package_compare.create_input(comparison_root, archive())
    value = archive(root="wrapper", **{"objects/a.md": "a"})
    mixed = io.BytesIO()
    with zipfile.ZipFile(value) as source, zipfile.ZipFile(mixed, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info))
        target.writestr("outside.txt", "not part of the package")
    mixed.seek(0)
    with pytest.raises(WorkspaceError, match="根目录外"):
        package_compare.create_input(comparison_root, mixed)


@pytest.mark.parametrize("extra, message", [
    ("name: Duplicate\n", "重复或无效键"),
    ("extra: {key: first, key: second}\n", "重复或无效键"),
    ('extra: [{key: first, "key": second}]\n', "重复或无效键"),
    ("extra: {1: value}\n", "重复或无效键"),
    ("extra: {true: value}\n", "重复或无效键"),
    ("extra: {? [a, b]: value}\n", "重复或无效键"),
    ("extra: &shared {key: value}\ncopy: *shared\n", "别名或标签"),
    ("extra: !!python/object:builtins.object {}\n", "别名或标签"),
    ("extra: [unterminated\n", "YAML 无效"),
    ("---\nextra: second-document\n", "YAML 无效"),
])
def test_manifest_safe_load_preserves_strict_validation(comparison_root, extra, message):
    original = archive(**{"objects/a.md": "synthetic"})
    value = io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(value, "w") as target:
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "manifest.yaml":
                content += extra.encode("utf-8")
            target.writestr(info.filename, content)
    value.seek(0)
    with pytest.raises(WorkspaceError, match=message) as error:
        package_compare.create_input(comparison_root, value)
    assert error.value.status == 400


def test_manifest_safe_load_accepts_nested_string_keys(comparison_root):
    original = archive(**{"objects/a.md": "synthetic"})
    value = io.BytesIO()
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(value, "w") as target:
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "manifest.yaml":
                content += b'extra: [{"1": one, "true": yes}, {key: value}]\n'
            target.writestr(info.filename, content)
    value.seek(0)
    result = package_compare.create_input(comparison_root, value)
    assert result["file_count"] == 2


def test_archive_worker_passes_paths_as_arguments_without_shell(tmp_path, monkeypatch):
    folder = tmp_path / "synthetic path;$(echo unexpected)"
    folder.mkdir()
    path = folder / "archive & input.zip"
    path.write_bytes(archive(**{"objects/a.md": "synthetic"}).getvalue())
    original = package_compare.subprocess.Popen
    calls = []

    def recording_popen(args, **kwargs):
        calls.append((args, kwargs))
        return original(args, **kwargs)

    monkeypatch.setattr(package_compare.subprocess, "Popen", recording_popen)
    index = package_compare._archive_index(path)
    assert index["file_count"] == 2
    args, options = calls[0]
    assert isinstance(args, list)
    assert Path(args[0]).is_absolute()
    assert args[1:3] == ["-m", "app.services.package_compare_worker"]
    assert args[3] == str(path)
    assert options["shell"] is False
    assert not list(folder.glob("onto-package-index-*.json"))


def test_schema_version_02_requires_supported_solver_layout(comparison_root):
    item = package_compare.create_input(
        comparison_root, archive(schema_version="0.2", **{"objects/a.md": "a"}),
    )
    assert item["package_id"] == "synthetic"
    value = archive(**{"objects/a.md": "a"})
    bad = io.BytesIO()
    with zipfile.ZipFile(value) as source, zipfile.ZipFile(bad, "w") as target:
        for info in source.infolist():
            content = source.read(info)
            if info.filename == "manifest.yaml":
                content = content.replace(b"schema_version: '0.1'", b"schema_version: '0.2'")
            target.writestr(info.filename, content)
    bad.seek(0)
    with pytest.raises(WorkspaceError, match="solver_layout"):
        package_compare.create_input(comparison_root, bad)


def test_compare_inputs_are_private_and_expire(comparison_root, monkeypatch):
    item = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}))
    with pytest.raises(WorkspaceError, match="无权"):
        package_compare._load_input("another-subject", item["input_id"])
    monkeypatch.setattr(package_compare, "INPUT_TTL", 0)
    with pytest.raises(WorkspaceError, match="过期"):
        package_compare._load_input(comparison_root, item["input_id"])


def test_start_comparison_is_pollable(comparison_root):
    left = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha")
    right = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha")
    job = package_compare.start_comparison(None, "project_alpha", comparison_root, {
        "kind": "upload", "input_id": left["input_id"]
    }, {"kind": "upload", "input_id": right["input_id"]})
    assert job["status"] == "queued"
    for _ in range(3000):
        result = package_compare.get_comparison(comparison_root, "project_alpha", job["comparison_id"])
        if result["status"] == "ready":
            assert result["summary"]["files"]["modified"] == 1
            break
        time.sleep(0.01)
    else:
        pytest.fail("comparison worker did not finish")


def test_start_comparison_is_idempotent_for_the_same_request(comparison_root):
    left = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha",
    )
    refs = ({"kind": "upload", "input_id": left["input_id"]},
            {"kind": "upload", "input_id": right["input_id"]})
    first = package_compare.start_comparison(
        None, "project_alpha", comparison_root, *refs, request_id="request_demo",
    )
    second = package_compare.start_comparison(
        None, "project_alpha", comparison_root, *refs, request_id="request_demo",
    )
    assert second["comparison_id"] == first["comparison_id"]


def test_request_id_rejects_different_sources(comparison_root):
    first = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha",
    )
    second = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha",
    )
    third = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "c"}), project_id="project_alpha",
    )
    package_compare.start_comparison(
        None, "project_alpha", comparison_root,
        {"kind": "upload", "input_id": first["input_id"]},
        {"kind": "upload", "input_id": second["input_id"]},
        request_id="request_conflict",
    )
    with pytest.raises(WorkspaceError) as error:
        package_compare.start_comparison(
            None, "project_alpha", comparison_root,
            {"kind": "upload", "input_id": first["input_id"]},
            {"kind": "upload", "input_id": third["input_id"]},
            request_id="request_conflict",
        )
    assert error.value.status == 409


def test_cancel_reaches_an_upload_even_when_the_cancel_request_arrives_first(comparison_root):
    package_compare.cancel_upload(comparison_root, "upload_demo")
    with pytest.raises(WorkspaceError, match="已取消"):
        package_compare.create_input(
            comparison_root, archive(**{"objects/a.md": "a"}),
            project_id="project_alpha", request_id="upload_demo",
        )


def test_file_search_includes_business_display_name(comparison_root):
    left = package_compare.create_input(comparison_root, archive(**{
        "objects/a.md": "---\nreport_name: 旧名称\n---\nold\n",
    }))
    right = package_compare.create_input(comparison_root, archive(**{
        "objects/a.md": "---\nreport_name: 业务订单\n---\nnew\n",
    }))
    result = package_compare.create_comparison(
        "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    rows = package_compare.list_files(
        comparison_root, "project_alpha", result["comparison_id"], query="业务订单",
    )
    assert rows["total"] == 1
    assert rows["items"][0]["display_name"] == "业务订单"


def test_file_diff_degrades_text_with_excessive_line_length(
    comparison_root, monkeypatch,
):
    monkeypatch.setattr(package_compare, "MAX_LINE_CHARS", 4)
    left = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "short"}),
        project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "longer"}),
        project_id="project_alpha",
    )
    result = package_compare.create_comparison(
        "project_alpha",
        comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    diff = package_compare.file_diff(
        "project_alpha",
        comparison_root,
        result["comparison_id"],
        "objects/a.md",
    )
    assert diff["left"]["encoding"] == "complex_text"
    assert diff["right"]["encoding"] == "complex_text"


def test_start_comparison_limits_user_and_global_pending_tasks(comparison_root, monkeypatch):
    class HoldingExecutor:
        def submit(self, *_args, **_kwargs):
            return Future()

    monkeypatch.setattr(package_compare, "_EXECUTOR", HoldingExecutor())
    monkeypatch.setattr(package_compare, "_RUNNING", {})
    monkeypatch.setattr(package_compare, "_CANCELLED", set())
    monkeypatch.setattr(package_compare, "MAX_WORKERS", 1)
    monkeypatch.setattr(package_compare, "MAX_QUEUED_TASKS", 1)
    monkeypatch.setattr(package_compare, "MAX_USER_PENDING_TASKS", 1)

    def refs(owner):
        left = package_compare.create_input(
            owner, archive(**{"objects/a.md": "left"}), project_id="project_alpha",
        )
        right = package_compare.create_input(
            owner, archive(**{"objects/a.md": "right"}), project_id="project_alpha",
        )
        return (
            {"kind": "upload", "input_id": left["input_id"]},
            {"kind": "upload", "input_id": right["input_id"]},
        )

    first = package_compare.start_comparison(None, "project_alpha", "user-a", *refs("user-a"))
    with pytest.raises(WorkspaceError) as user_error:
        package_compare.start_comparison(None, "project_alpha", "user-a", *refs("user-a"))
    assert user_error.value.status == 429 and user_error.value.code == "compare_user_busy"

    second = package_compare.start_comparison(None, "project_alpha", "user-b", *refs("user-b"))
    with pytest.raises(WorkspaceError) as global_error:
        package_compare.start_comparison(None, "project_alpha", "user-c", *refs("user-c"))
    assert global_error.value.status == 429 and global_error.value.code == "compare_busy"

    package_compare.delete("user-a", first["comparison_id"])
    replacement = package_compare.start_comparison(None, "project_alpha", "user-c", *refs("user-c"))
    package_compare.delete("user-b", second["comparison_id"])
    package_compare.delete("user-c", replacement["comparison_id"])


def test_file_diff_rejects_unfinished_comparison(comparison_root):
    comparison_id = "cmp_pending"
    folder = package_compare._comparison_folder(comparison_root, comparison_id)
    folder.mkdir()
    package_compare._write_json(folder / "job.json", {
        "comparison_id": comparison_id,
        "project_id": "project_alpha",
        "owner": comparison_root,
        "status": "queued",
        "phase": "queued",
        "created_at": time.time(),
    })

    with pytest.raises(WorkspaceError, match="尚未完成") as error:
        package_compare.file_diff("project_alpha", comparison_root, comparison_id, "objects/a.md")
    assert error.value.status == 409


def test_repeated_async_comparison_keeps_new_job_result(comparison_root):
    left = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha")
    right = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha")
    refs = ({"kind": "upload", "input_id": left["input_id"]},
            {"kind": "upload", "input_id": right["input_id"]})

    jobs = []
    for _ in range(2):
        job = package_compare.start_comparison(None, "project_alpha", comparison_root, *refs)
        jobs.append(job)
        for _ in range(3000):
            result = package_compare.get_comparison(comparison_root, "project_alpha", job["comparison_id"])
            if result["status"] == "ready":
                assert result["comparison_id"] == job["comparison_id"]
                break
            time.sleep(0.01)
        else:
            pytest.fail("comparison worker did not finish")
    assert jobs[0]["comparison_id"] != jobs[1]["comparison_id"]


def test_cancelled_worker_cannot_recreate_ready_result(comparison_root, monkeypatch):
    left = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha",
    )
    entered = threading.Event()
    resume = threading.Event()
    original = package_compare._summary

    def delayed_summary(*args):
        entered.set()
        assert resume.wait(30)
        return original(*args)

    monkeypatch.setattr(package_compare, "_summary", delayed_summary)
    job = package_compare.start_comparison(
        None, "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    assert entered.wait(30)
    package_compare.delete(comparison_root, job["comparison_id"])
    resume.set()
    future = package_compare._RUNNING.get((comparison_root, job["comparison_id"]))
    if future:
        future.result(timeout=2)
    with pytest.raises(WorkspaceError) as error:
        package_compare.get_comparison(comparison_root, "project_alpha", job["comparison_id"])
    assert error.value.status == 410


def test_same_release_is_rejected_before_freezing(comparison_root, monkeypatch):
    calls = []
    monkeypatch.setattr(publish, "release_package", lambda *args: calls.append(args))
    ref = {"kind": "release", "version": "0.1.0"}
    with pytest.raises(WorkspaceError, match="不能相同"):
        package_compare.start_comparison(None, "project_alpha", comparison_root, ref, dict(ref))
    assert calls == []


def test_failed_job_is_removed_after_result_ttl(comparison_root, monkeypatch):
    comparison_id = "cmp_failed"
    folder = package_compare._comparison_folder(comparison_root, comparison_id)
    folder.mkdir()
    package_compare._write_json(folder / "job.json", {
        "comparison_id": comparison_id, "project_id": "project_alpha", "owner": comparison_root,
        "status": "failed", "phase": "failed", "created_at": 0,
    })
    monkeypatch.setattr(package_compare, "COMPARISON_TTL", 1)
    package_compare.maintain()
    assert not folder.exists()


def test_large_text_uses_preview_fallback(comparison_root, monkeypatch):
    monkeypatch.setattr(package_compare, "MAX_TEXT_BYTES", 3)
    left = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "large"}), project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "other"}), project_id="project_alpha",
    )
    result = package_compare.create_comparison(
        "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    diff = package_compare.file_diff(
        "project_alpha", comparison_root, result["comparison_id"], "objects/a.md",
    )
    assert diff["left"]["encoding"] == "too_large" and diff["left"]["text"] is None


def test_input_cannot_be_deleted_until_its_comparison_is_deleted(comparison_root):
    left = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha",
    )
    result = package_compare.create_comparison(
        "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    with pytest.raises(WorkspaceError) as error:
        package_compare.delete_input(comparison_root, "project_alpha", left["input_id"])
    assert error.value.status == 409
    package_compare.delete(comparison_root, result["comparison_id"])
    package_compare.delete_input(comparison_root, "project_alpha", left["input_id"])
    assert not (package_compare.root(comparison_root) / left["input_id"]).exists()


def test_input_delete_and_task_creation_are_atomic(comparison_root, monkeypatch):
    left = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "left"}),
        project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "right"}),
        project_id="project_alpha",
    )
    delete_checked = threading.Event()
    delete_resume = threading.Event()
    original_glob = package_compare.Path.glob
    input_folder = package_compare.root(comparison_root) / left["input_id"]

    def delayed_glob(path, pattern):
        rows = original_glob(path, pattern)
        if path == package_compare.root(comparison_root) and pattern == "cmp_*/*.json":
            values = list(rows)
            delete_checked.set()
            assert delete_resume.wait(30)
            return iter(values)
        return rows

    monkeypatch.setattr(package_compare.Path, "glob", delayed_glob)
    delete_errors = []
    create_errors = []

    def remove_input():
        try:
            package_compare.delete_input(
                comparison_root, "project_alpha", left["input_id"],
            )
        except Exception as error:
            delete_errors.append(error)

    def create_task():
        try:
            package_compare.start_comparison(
                None, "project_alpha",
                comparison_root,
                {"kind": "upload", "input_id": left["input_id"]},
                {"kind": "upload", "input_id": right["input_id"]},
            )
        except Exception as error:
            create_errors.append(error)

    delete_thread = threading.Thread(target=remove_input)
    create_thread = threading.Thread(target=create_task)
    delete_thread.start()
    assert delete_checked.wait(30)
    create_thread.start()
    delete_resume.set()
    delete_thread.join(30)
    create_thread.join(30)
    assert not delete_thread.is_alive() and not create_thread.is_alive()
    assert delete_errors == []
    assert not input_folder.exists()
    assert create_errors and create_errors[0].status in {403, 410}


def test_cross_package_check_uses_manifest_identity_for_uploads(comparison_root):
    left = package_compare.create_input(
        comparison_root, archive(package_id="package-a", **{"objects/a.md": "a"}),
        project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root, archive(package_id="package-b", **{"objects/a.md": "b"}),
        project_id="project_alpha",
    )
    refs = (
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    with pytest.raises(WorkspaceError) as error:
        package_compare.create_comparison("project_alpha", comparison_root, *refs)
    assert error.value.status == 409
    result = package_compare.create_comparison(
        "project_alpha", comparison_root, *refs, allow_cross_package=True,
    )
    assert result["status"] == "ready"


def test_distinct_case_and_unicode_paths_are_preserved(comparison_root):
    nfc = "objects/é.md"
    nfd = unicodedata.normalize("NFD", nfc)
    item = package_compare.create_input(comparison_root, archive(**{
        "objects/A.md": "upper", "objects/a.md": "lower", nfc: "nfc", nfd: "nfd",
    }))
    _path, meta = package_compare._load_input(comparison_root, item["input_id"])
    assert {"objects/A.md", "objects/a.md", nfc, nfd} <= set(meta["files"])


def test_nested_attachment_manifest_is_not_a_package_root(comparison_root):
    item = package_compare.create_input(comparison_root, archive(**{
        "objects/a.md": "a", "attachments/example/manifest.yaml": "example: document",
    }))
    assert item["file_count"] == 3


def test_root_manifest_and_second_wrapped_package_are_rejected(comparison_root):
    value = archive(**{"objects/a.md": "a"})
    mixed = io.BytesIO()
    with zipfile.ZipFile(value) as source, zipfile.ZipFile(mixed, "w") as target:
        for info in source.infolist():
            target.writestr(info.filename, source.read(info))
        target.writestr("other/manifest.yaml", source.read("manifest.yaml"))
        target.writestr("other/objects/b.md", "b")
    mixed.seek(0)
    with pytest.raises(WorkspaceError, match="多个包根"):
        package_compare.create_input(comparison_root, mixed)


@pytest.mark.parametrize("kind", ["encrypted", "unsupported"])
def test_archive_zip_features_return_domain_errors(comparison_root, kind):
    data = bytearray(archive(**{"objects/a.md": "a"}).getvalue())
    for signature, flag_offset, method_offset in ((b"PK\x03\x04", 6, 8), (b"PK\x01\x02", 8, 10)):
        start = 0
        while True:
            position = data.find(signature, start)
            if position < 0:
                break
            if kind == "encrypted":
                flags = struct.unpack_from("<H", data, position + flag_offset)[0]
                struct.pack_into("<H", data, position + flag_offset, flags | 1)
            else:
                struct.pack_into("<H", data, position + method_offset, 99)
            start = position + 4
    with pytest.raises(WorkspaceError) as error:
        package_compare.create_input(comparison_root, io.BytesIO(data))
    assert error.value.status == 400


def test_restart_marks_orphaned_active_job_failed(comparison_root):
    comparison_id = "cmp_restart"
    folder = package_compare._comparison_folder(comparison_root, comparison_id)
    folder.mkdir()
    package_compare._write_json(folder / "job.json", {
        "comparison_id": comparison_id, "project_id": "project_alpha", "owner": comparison_root,
        "status": "running", "phase": "reading",
        "created_at": time.time() - 1,
    })
    package_compare.maintain()
    result = package_compare.get_comparison(comparison_root, "project_alpha", comparison_id)
    assert result["status"] == "failed"
    assert result["error_code"] == "PROCESS_RESTARTED"


def test_expired_result_is_rejected_during_read(comparison_root, monkeypatch):
    left = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}))
    right = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "b"}))
    result = package_compare.create_comparison(
        "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    monkeypatch.setattr(package_compare, "COMPARISON_TTL", -1)
    with pytest.raises(WorkspaceError) as error:
        package_compare.get_comparison(comparison_root, "project_alpha", result["comparison_id"])
    assert error.value.status == 410


def test_maintenance_keeps_expired_input_referenced_by_ready_result(comparison_root, monkeypatch):
    left = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha")
    right = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha")
    result = package_compare.create_comparison(
        "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )

    monkeypatch.setattr(package_compare, "INPUT_TTL", 0)
    package_compare.maintain()

    diff = package_compare.file_diff(
        "project_alpha", comparison_root, result["comparison_id"], "objects/a.md",
    )
    assert diff["left"]["text"] == "a" and diff["right"]["text"] == "b"


def test_accepted_job_keeps_input_after_input_ttl(comparison_root, monkeypatch):
    left = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "left"}),
        project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "right"}),
        project_id="project_alpha",
    )
    comparison_id = "cmp_queued_expiry"
    folder = package_compare._comparison_folder(comparison_root, comparison_id)
    folder.mkdir()
    package_compare._write_json(folder / "job.json", {
        "comparison_id": comparison_id,
        "project_id": "project_alpha",
        "owner": comparison_root,
        "status": "queued",
        "phase": "queued",
        "created_at": time.time(),
        "input_refs": {
            "left": {"kind": "upload", "input_id": left["input_id"]},
            "right": {"kind": "upload", "input_id": right["input_id"]},
        },
    })
    monkeypatch.setattr(package_compare, "INPUT_TTL", 0)
    path, _meta = package_compare._load_input(
        comparison_root, left["input_id"], "project_alpha",
    )
    assert path.is_file()


def test_async_job_crossing_input_ttl_still_completes(comparison_root, monkeypatch):
    left = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "left"}),
        project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "right"}),
        project_id="project_alpha",
    )
    entered = threading.Event()
    resume = threading.Event()
    original = package_compare._run_job

    def delayed_job(*args):
        entered.set()
        assert resume.wait(30)
        return original(*args)

    monkeypatch.setattr(package_compare, "_run_job", delayed_job)
    job = package_compare.start_comparison(
        None, "project_alpha",
        comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    assert entered.wait(30)
    monkeypatch.setattr(package_compare, "INPUT_TTL", 0)
    resume.set()
    for _ in range(3000):
        result = package_compare.get_comparison(
            comparison_root, "project_alpha", job["comparison_id"],
        )
        if result["status"] in {"ready", "failed"}:
            break
        time.sleep(0.01)
    assert result["status"] == "ready"
    diff = package_compare.file_diff(
        "project_alpha", comparison_root, job["comparison_id"], "objects/a.md",
    )
    assert diff["left"]["text"] == "left" and diff["right"]["text"] == "right"

def test_worker_and_diff_use_frozen_release_without_reading_it_again(comparison_root, tmp_path, monkeypatch):
    calls = []
    tokens = fake_release(monkeypatch, tmp_path, archive(**{"objects/a.md": "release"}).getvalue(), calls)
    local = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "local"}),
        project_id="project_alpha",
    )
    job = package_compare.start_comparison(
        "manager", "project_alpha", comparison_root,
        {"kind": "release", "version": "0.1.0"},
        {"kind": "upload", "input_id": local["input_id"]},
        token="login-token",
    )

    def unavailable(*_args, **_kwargs):
        raise WorkspaceError("发布包已不可用", 409, "release_package_unavailable")

    monkeypatch.setattr(publish, "release_package", unavailable)
    for _ in range(3000):
        result = package_compare.get_comparison(
            comparison_root, "project_alpha", job["comparison_id"],
        )
        if result["status"] == "ready":
            assert result["summary"]["files"]["modified"] == 1
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"frozen release comparison did not finish: {result}")
    assert result["left"]["source"] == {"kind": "release", "version": "0.1.0"}
    assert result["left"]["version"] == "0.1.0"
    diff = package_compare.file_diff(
        "project_alpha",
        comparison_root,
        job["comparison_id"],
        "objects/a.md",
    )
    assert diff["left"]["text"] == "release" and diff["right"]["text"] == "local"
    # 发起人的令牌只交给 release_package（留存缺失时从 Hub 取原包），之后不再回源
    assert calls == [("manager", "project_alpha", "0.1.0")] and tokens == ["login-token"]


def test_frozen_release_metadata_cannot_exceed_storage_quota(
    comparison_root, tmp_path, monkeypatch,
):
    local = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "local"}),
        project_id="project_alpha",
    )
    uploaded = archive(**{"objects/a.md": "release"}).getvalue()
    fake_release(monkeypatch, tmp_path, uploaded)
    monkeypatch.setattr(package_compare, "MAX_ARCHIVE_BYTES", len(uploaded) + 100)
    monkeypatch.setattr(package_compare, "RESULT_RESERVATION_BYTES", 100)
    current = package_compare._tree_size(package_compare.root(comparison_root))
    monkeypatch.setattr(
        package_compare,
        "MAX_USER_STORAGE_BYTES",
        current + len(uploaded) + 900,
    )

    try:
        job = package_compare.start_comparison(
            None, "project_alpha",
            comparison_root,
            {"kind": "release", "version": "0.1.0"},
            {"kind": "upload", "input_id": local["input_id"]},
        )
        for _ in range(3000):
            result = package_compare.get_comparison(
                comparison_root,
                "project_alpha",
                job["comparison_id"],
            )
            if result["status"] in {"ready", "failed"}:
                break
            time.sleep(0.01)
    except WorkspaceError as error:
        assert error.status in {429, 507}

    assert (
        package_compare._tree_size(package_compare.root(comparison_root))
        <= package_compare.MAX_USER_STORAGE_BYTES
    )


def test_release_source_reads_project_release_by_version(comparison_root, tmp_path, monkeypatch):
    calls = []
    fake_release(monkeypatch, tmp_path, archive(**{"objects/a.md": "release"}).getvalue(), calls)
    path, meta, temporary = package_compare._release_source(
        "manager",
        "project_alpha",
        comparison_root,
        {"kind": "release", "version": "0.1.0"},
    )
    assert path.is_file() and path.parent == temporary
    # 复制出来解析，不动发布留存
    assert path != tmp_path / "release-store" / "project_alpha-0.1.0.zip"
    assert meta["source"] == {"kind": "release", "version": "0.1.0"}
    assert meta["version"] == "0.1.0" and meta["package_id"] == "synthetic"
    assert calls == [("manager", "project_alpha", "0.1.0")]
    package_compare.shutil.rmtree(temporary)


def test_release_source_rejects_oversized_release(comparison_root, tmp_path, monkeypatch):
    payload = archive(**{"objects/a.md": "release"}).getvalue()
    fake_release(monkeypatch, tmp_path, payload)
    monkeypatch.setattr(package_compare, "MAX_ARCHIVE_BYTES", len(payload) - 1)
    with pytest.raises(WorkspaceError) as error:
        package_compare._release_source("manager", "project_alpha", comparison_root, {"kind": "release", "version": "0.1.0"})
    assert error.value.status == 413
    assert not list(package_compare.root(comparison_root).glob("onto-package-compare-*"))


def test_maintenance_does_not_delete_input_during_archive_validation(
    comparison_root, monkeypatch,
):
    entered = threading.Event()
    resume = threading.Event()
    original = package_compare._source_meta

    def delayed_source_meta(*args):
        entered.set()
        assert resume.wait(30)
        return original(*args)

    monkeypatch.setattr(package_compare, "_source_meta", delayed_source_meta)
    outcome = {}

    def create():
        outcome["input"] = package_compare.create_input(
            comparison_root,
            archive(**{"objects/a.md": "content"}),
            project_id="project_alpha",
            request_id="upload_maintenance",
        )

    thread = threading.Thread(target=create)
    thread.start()
    assert entered.wait(30)
    active_id = package_compare._ACTIVE_UPLOADS[(comparison_root, "upload_maintenance")]
    active_folder = package_compare.root(comparison_root) / active_id
    package_compare.maintain()
    assert active_folder.is_dir()
    resume.set()
    thread.join(30)
    assert not thread.is_alive()
    assert outcome["input"]["input_id"] == active_id


def test_maintenance_rechecks_uploads_started_after_cleanup_begins(
    comparison_root, monkeypatch,
):
    cleanup_entered = threading.Event()
    cleanup_resume = threading.Event()
    upload_entered = threading.Event()
    upload_resume = threading.Event()
    original_read = package_compare._read_json
    original_source_meta = package_compare._source_meta
    result_folder = package_compare.root(comparison_root) / "cmp_cleanup_gate"
    result_folder.mkdir()
    package_compare._write_json(result_folder / "result.json", {
        "comparison_id": "cmp_cleanup_gate",
        "project_id": "project_alpha",
        "owner": comparison_root,
        "status": "ready",
        "created_at": time.time(),
        "left": {},
        "right": {},
    })

    def delayed_read(path):
        if path == result_folder / "result.json":
            cleanup_entered.set()
            assert cleanup_resume.wait(30)
        return original_read(path)

    def delayed_source_meta(*args):
        upload_entered.set()
        assert upload_resume.wait(30)
        return original_source_meta(*args)

    monkeypatch.setattr(package_compare, "_read_json", delayed_read)
    monkeypatch.setattr(package_compare, "_source_meta", delayed_source_meta)
    cleanup = threading.Thread(target=package_compare.maintain)
    cleanup.start()
    assert cleanup_entered.wait(30)
    outcome = {}
    upload = threading.Thread(target=lambda: outcome.setdefault(
        "input",
        package_compare.create_input(
            comparison_root,
            archive(**{"objects/a.md": "content"}),
            request_id="upload_after_cleanup_started",
        ),
    ))
    upload.start()
    assert upload_entered.wait(30)
    active_id = package_compare._ACTIVE_UPLOADS[
        (comparison_root, "upload_after_cleanup_started")
    ]
    cleanup_resume.set()
    cleanup.join(30)
    assert not cleanup.is_alive()
    assert (package_compare.root(comparison_root) / active_id).is_dir()
    upload_resume.set()
    upload.join(30)
    assert not upload.is_alive()
    assert outcome["input"]["input_id"] == active_id


def test_maintenance_keeps_result_completed_during_job_scan(
    comparison_root, monkeypatch,
):
    cleanup_entered = threading.Event()
    cleanup_resume = threading.Event()
    uploaded = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "content"}),
        project_id="project_alpha",
    )
    input_folder = package_compare.root(comparison_root) / uploaded["input_id"]
    input_meta_path = input_folder / "meta.json"
    input_meta = package_compare._read_json(input_meta_path)
    input_meta["created_at"] = time.time() - package_compare.INPUT_TTL - 1
    package_compare._write_json(input_meta_path, input_meta)
    folder = package_compare.root(comparison_root) / "cmp_completed_during_cleanup"
    folder.mkdir()
    job_path = folder / "job.json"
    result_path = folder / "result.json"
    package_compare._write_json(job_path, {
        "comparison_id": folder.name,
        "project_id": "project_alpha",
        "owner": comparison_root,
        "status": "running",
        "created_at": time.time(),
        "input_refs": {},
    })
    original_read = package_compare._read_json
    delayed = False

    def delayed_job_read(path):
        nonlocal delayed
        if path == job_path and not delayed:
            delayed = True
            cleanup_entered.set()
            assert cleanup_resume.wait(30)
        return original_read(path)

    monkeypatch.setattr(package_compare, "_read_json", delayed_job_read)
    cleanup = threading.Thread(target=package_compare.maintain)
    cleanup.start()
    assert cleanup_entered.wait(30)
    with package_compare._TASK_LOCK:
        package_compare._write_json(result_path, {
            "comparison_id": folder.name,
            "project_id": "project_alpha",
            "owner": comparison_root,
            "status": "ready",
            "created_at": time.time(),
            "left": {
                "source": {
                    "kind": "upload",
                    "input_id": uploaded["input_id"],
                },
            },
            "right": {},
        })
        job_path.unlink()
    cleanup_resume.set()
    cleanup.join(30)
    assert not cleanup.is_alive()
    assert result_path.is_file()
    assert input_folder.is_dir()
    assert package_compare._read_json(result_path)["status"] == "ready"


def test_cancel_upload_during_archive_validation_removes_input(
    comparison_root, monkeypatch,
):
    entered = threading.Event()
    errors = []

    def cancelled_source_meta(*_args):
        entered.set()
        while True:
            package_compare._checkpoint()
            time.sleep(0.01)

    monkeypatch.setattr(package_compare, "_source_meta", cancelled_source_meta)

    def create():
        try:
            package_compare.create_input(
                comparison_root,
                archive(**{"objects/a.md": "content"}),
                project_id="project_alpha",
                request_id="upload_cancel",
            )
        except WorkspaceError as error:
            errors.append(error)

    thread = threading.Thread(target=create)
    thread.start()
    assert entered.wait(30)
    input_id = package_compare._ACTIVE_UPLOADS[(comparison_root, "upload_cancel")]
    package_compare.cancel_upload(comparison_root, "upload_cancel")
    thread.join(30)
    assert not thread.is_alive()
    assert errors and errors[0].code == "compare_upload_cancelled"
    assert not (package_compare.root(comparison_root) / input_id).exists()


def test_upload_limit_covers_archive_validation(comparison_root, monkeypatch):
    entered = threading.Event()
    resume = threading.Event()
    original = package_compare._source_meta

    def delayed_source_meta(*args):
        entered.set()
        assert resume.wait(30)
        return original(*args)

    monkeypatch.setattr(package_compare, "_UPLOAD_LIMIT", threading.BoundedSemaphore(1))
    monkeypatch.setattr(package_compare, "_source_meta", delayed_source_meta)
    first = threading.Thread(target=lambda: package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "first"}),
        request_id="upload_first",
    ))
    first.start()
    assert entered.wait(30)
    with pytest.raises(WorkspaceError) as error:
        package_compare.create_input(
            comparison_root,
            archive(**{"objects/a.md": "second"}),
            request_id="upload_second",
        )
    assert error.value.status == 429
    assert error.value.code == "compare_upload_busy"
    resume.set()
    first.join(30)
    assert not first.is_alive()


def test_delete_waits_for_result_touch_and_result_does_not_reappear(
    comparison_root, monkeypatch,
):
    left = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}))
    right = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "b"}))
    result = package_compare.create_comparison(
        "project_alpha",
        comparison_root,
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    comparison_id = result["comparison_id"]
    result_path = package_compare._comparison_folder(comparison_root, comparison_id) / "result.json"
    entered = threading.Event()
    resume = threading.Event()
    original = package_compare._write_json

    def delayed_write(path, value):
        if path == result_path:
            entered.set()
            assert resume.wait(30)
        return original(path, value)

    monkeypatch.setattr(package_compare, "_write_json", delayed_write)
    reader = threading.Thread(target=lambda: package_compare.get_comparison(
        comparison_root, "project_alpha", comparison_id,
    ))
    remover = threading.Thread(target=lambda: package_compare.delete(comparison_root, comparison_id))
    reader.start()
    assert entered.wait(30)
    remover.start()
    resume.set()
    reader.join(30)
    remover.join(30)
    assert not reader.is_alive() and not remover.is_alive()
    assert not result_path.parent.exists()


def test_non_owner_process_does_not_maintain_running_jobs(comparison_root, monkeypatch):
    comparison_id = "cmp_owned_elsewhere"
    folder = package_compare._comparison_folder(comparison_root, comparison_id)
    folder.mkdir()
    package_compare._write_json(folder / "job.json", {
        "comparison_id": comparison_id,
        "project_id": "project_alpha",
        "owner": comparison_root,
        "status": "running",
        "phase": "reading",
        "created_at": time.time(),
    })
    monkeypatch.setattr(package_compare, "_SERVICE_OWNER", False)
    package_compare.maintain()
    assert package_compare._read_json(folder / "job.json")["status"] == "running"


def test_idempotent_replay_returns_same_release_task_without_refreezing(comparison_root, tmp_path, monkeypatch):
    calls = []
    fake_release(monkeypatch, tmp_path, archive(**{"objects/a.md": "release"}).getvalue(), calls)
    local = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "local"}),
        project_id="project_alpha",
    )
    refs = (
        {"kind": "release", "version": "0.1.0"},
        {"kind": "upload", "input_id": local["input_id"]},
    )
    first = package_compare.start_comparison(
        "manager", "project_alpha", comparison_root, *refs, request_id="release_replay",
    )
    for _ in range(3000):
        current = package_compare.get_comparison(
            comparison_root, "project_alpha", first["comparison_id"],
        )
        if current["status"] == "ready":
            break
        time.sleep(0.01)
    else:
        pytest.fail(f"comparison did not finish: {current}")
    replay = package_compare.start_comparison(
        "manager", "project_alpha", comparison_root, *refs, request_id="release_replay",
    )
    assert replay["comparison_id"] == first["comparison_id"] and replay["status"] == "ready"
    assert len(calls) == 1
    # 换一个项目，同一个 request_id 不会拿到别的项目的结果
    other = package_compare.create_input(
        comparison_root, archive(**{"objects/a.md": "other"}), project_id="project_beta",
    )
    fresh = package_compare.start_comparison(
        "manager", "project_beta", comparison_root,
        {"kind": "release", "version": "0.1.0"},
        {"kind": "upload", "input_id": other["input_id"]},
        request_id="release_replay",
    )
    assert fresh["comparison_id"] != first["comparison_id"]


def test_slow_release_fetch_does_not_hold_task_lock(comparison_root, tmp_path, monkeypatch):
    started = threading.Event()
    resume = threading.Event()

    def wait_for_release():
        started.set()
        assert resume.wait(30)

    fake_release(monkeypatch, tmp_path, archive(**{"objects/a.md": "release"}).getvalue(), before=wait_for_release)
    local = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "local"}),
        project_id="project_alpha",
    )
    errors = []

    def create():
        try:
            package_compare.start_comparison(
                "manager", "project_alpha", comparison_root,
                {"kind": "release", "version": "0.1.0"},
                {"kind": "upload", "input_id": local["input_id"]},
            )
        except Exception as error:
            errors.append(error)

    thread = threading.Thread(target=create)
    thread.start()
    assert started.wait(10)
    package_compare.cancel_upload(comparison_root, "another_upload")
    resume.set()
    thread.join(30)
    assert not thread.is_alive()
    assert errors == []


def test_duplicate_active_upload_request_id_is_rejected(comparison_root, monkeypatch):
    entered = threading.Event()
    resume = threading.Event()
    original = package_compare._source_meta

    def delayed_meta(*args):
        entered.set()
        assert resume.wait(30)
        return original(*args)

    monkeypatch.setattr(package_compare, "_source_meta", delayed_meta)
    errors = []

    def upload_first():
        try:
            package_compare.create_input(
                comparison_root,
                archive(**{"objects/a.md": "first"}),
                request_id="same_upload",
            )
        except WorkspaceError as error:
            errors.append(error)

    thread = threading.Thread(target=upload_first)
    thread.start()
    assert entered.wait(30)
    with pytest.raises(WorkspaceError) as duplicate:
        package_compare.create_input(
            comparison_root,
            archive(**{"objects/a.md": "second"}),
            request_id="same_upload",
        )
    assert duplicate.value.status == 409
    package_compare.maintain()
    resume.set()
    thread.join(30)
    assert not thread.is_alive()
    assert errors == []


def test_release_ref_is_persisted_before_freezing_finishes(comparison_root, tmp_path, monkeypatch):
    entered = threading.Event()
    resume = threading.Event()

    def wait_for_release():
        entered.set()
        assert resume.wait(30)

    fake_release(monkeypatch, tmp_path, archive(**{"objects/a.md": "release"}).getvalue(), before=wait_for_release)
    local = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "local"}),
        project_id="project_alpha",
    )
    refs = (
        {"kind": "release", "version": "0.1.0"},
        {"kind": "upload", "input_id": local["input_id"]},
    )
    outcome = {}

    def create():
        outcome["job"] = package_compare.start_comparison(
            "manager",
            "project_alpha",
            comparison_root,
            *refs,
            request_id="persisted_release",
        )

    thread = threading.Thread(target=create)
    thread.start()
    assert entered.wait(10)
    job_path = next(package_compare.root(comparison_root).glob("cmp_*/job.json"))
    persisted = package_compare._read_json(job_path)["input_refs"]["left"]
    assert persisted == {"kind": "release", "version": "0.1.0"}
    resume.set()
    thread.join(30)
    assert not thread.is_alive()
    assert outcome["job"]["status"] == "queued"


def test_result_storage_is_reserved_before_accepting_task(comparison_root, monkeypatch):
    left = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "left"}),
        project_id="project_alpha",
    )
    right = package_compare.create_input(
        comparison_root,
        archive(**{"objects/a.md": "right"}),
        project_id="project_alpha",
    )
    current = package_compare._tree_size(package_compare.root(comparison_root))
    monkeypatch.setattr(package_compare, "MAX_USER_STORAGE_BYTES", current + 100)
    refs = (
        {"kind": "upload", "input_id": left["input_id"]},
        {"kind": "upload", "input_id": right["input_id"]},
    )
    with pytest.raises(WorkspaceError) as error:
        package_compare.start_comparison(
            None, "project_alpha",
            comparison_root,
            *refs,
        )
    assert error.value.status == 429


def test_upload_metadata_cannot_exceed_storage_quota(comparison_root, monkeypatch):
    value = archive(**{f"objects/object_{index}.md": "content" for index in range(5)})
    archive_size = len(value.getbuffer())
    monkeypatch.setattr(package_compare, "MAX_ARCHIVE_BYTES", archive_size + 100)
    monkeypatch.setattr(package_compare, "MAX_USER_STORAGE_BYTES", archive_size + 200)

    with pytest.raises(WorkspaceError) as error:
        package_compare.create_input(comparison_root, value, project_id="project_alpha")

    assert error.value.status == 429
    assert not list(package_compare.root(comparison_root).glob("cmpin_*"))
    assert comparison_root not in package_compare._STORAGE_RESERVATIONS


def test_default_worker_memory_budget_accepts_regular_text_package(comparison_root):
    item = package_compare.create_input(
        comparison_root,
        archive(**{"objects/regular.md": random.Random(1).randbytes(1024 * 1024)}),
        project_id="project_alpha",
    )

    assert item["file_count"] == 2


# ---- 经 /api/v1 的端到端：真实发布、上传、所有者守卫 ----

OWNER, OTHER = {"Authorization": "Bearer owner-token"}, {"Authorization": "Bearer other-token"}


@pytest.fixture
def api(tmp_path, monkeypatch, synthetic_hub):
    data = tmp_path / "data"
    monkeypatch.setattr(config, "DATA_DIR", data)
    monkeypatch.setattr(package_compare, "_SERVICE_OWNER", True)
    monkeypatch.setattr(package_compare, "_SERVICE_AVAILABLE", True)
    if package_compare._EXECUTOR._shutdown:
        monkeypatch.setattr(package_compare, "_EXECUTOR", ThreadPoolExecutor(max_workers=2))
    manager = WorkspaceManager(data, Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", manager)
    tokens = {
        "owner-token": {"sub": "employee_0001", "role": "user"},
        "other-token": {"sub": "employee_0002", "role": "user"},
    }
    monkeypatch.setattr(auth, "ENABLED", True)
    monkeypatch.setattr(auth, "verify_access_token", lambda token: tokens.get(token))
    monkeypatch.setattr(auth, "check_access_token", lambda token: (tokens.get(token), "" if token in tokens else "invalid"))
    project_id = manager.create_project("版本对比演示", owner="employee_0001")["id"]
    yield manager, project_id, TestClient(app)
    drain_tasks()


def _release(manager, project_id, files: dict[str, str]) -> str:
    root = manager.project_root(project_id)
    for name, text in files.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "test: synthetic release content")
    # 发布走 Hub（ADR-0098），这里接 conftest 的内存 Hub
    return publish.publish_package(
        manager, project_id, allow_unbound=True, token="synthetic-access", subject="scope_a",
    )["version"]


def _ready(client, base, comparison_id):
    for _ in range(300):
        response = client.get(f"{base}/{comparison_id}", headers=OWNER)
        assert response.status_code == 200, response.text
        if response.json()["status"] in {"ready", "failed"}:
            return response.json()
        time.sleep(0.05)
    pytest.fail("comparison did not finish")


def test_api_compares_two_project_releases_and_guards_owner(api):
    manager, project_id, client = api
    first = _release(manager, project_id, {"ontology/object/order.md": "# Order\n\nA synthetic order.\n"})
    second = _release(manager, project_id, {
        "ontology/object/order.md": "# Order\n\nA synthetic order with status.\n",
        "ontology/object/item.md": "# Item\n\nA synthetic item.\n",
    })
    base = f"/api/v1/projects/{project_id}/package-comparisons"
    body = {"left": {"kind": "release", "version": first}, "right": {"kind": "release", "version": second},
            "request_id": "api_release_pair"}

    assert client.post(base, json=body, headers=OTHER).status_code == 403
    started = client.post(base, json=body, headers=OWNER)
    assert started.status_code == 202, started.text
    comparison_id = started.json()["comparison_id"]
    result = _ready(client, base, comparison_id)
    assert result["status"] == "ready", result
    assert result["left"]["version"] == first and result["right"]["version"] == second
    assert result["created_at"].endswith("Z")
    assert result["summary"]["files"]["added"] >= 1 and result["summary"]["files"]["modified"] >= 1

    listing = client.get(f"{base}/{comparison_id}/files", params={"status": "changed", "limit": 1}, headers=OWNER)
    assert listing.status_code == 200
    page = listing.json()
    assert len(page["items"]) == 1 and page["next_cursor"] and page["total"] >= 2
    rest = client.get(f"{base}/{comparison_id}/files",
                      params={"status": "changed", "limit": 1, "cursor": page["next_cursor"]}, headers=OWNER).json()
    assert rest["items"][0]["path"] != page["items"][0]["path"]
    changed = client.get(f"{base}/{comparison_id}/files", params={"q": "order.md"}, headers=OWNER).json()["items"]
    order = next(row for row in changed if row["path"].endswith("objects/order.md"))
    assert order["status"] == "modified" and order["category"] == "objects"

    diff = client.get(f"{base}/{comparison_id}/file-diff", params={"path": order["path"]}, headers=OWNER)
    assert diff.status_code == 200
    assert "status" in diff.json()["right"]["text"] and "status" not in diff.json()["left"]["text"]

    for method, path in (("GET", f"{base}/{comparison_id}"), ("GET", f"{base}/{comparison_id}/files"),
                         ("GET", f"{base}/{comparison_id}/file-diff?path={order['path']}"),
                         ("DELETE", f"{base}/{comparison_id}")):
        response = client.request(method, path, headers=OTHER)
        assert response.status_code == 403 and response.json()["code"] == "project_owner_only"

    # 同一 request_id 重放拿回同一任务
    assert client.post(base, json=body, headers=OWNER).json()["comparison_id"] == comparison_id
    assert client.delete(f"{base}/{comparison_id}", headers=OWNER).status_code == 204
    gone = client.get(f"{base}/{comparison_id}", headers=OWNER)
    assert gone.status_code == 410 and gone.json()["code"] == "compare_expired"


def _edited_copy(source: Path, target: Path, member_suffix: str, text: str) -> Path:
    """发布包改一个文件另存：manifest 不动，仍是同一个知识包（像别人下载后改过再传回来）。"""
    with zipfile.ZipFile(source) as src, zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as out:
        for info in src.infolist():
            data = src.read(info)
            out.writestr(info, text.encode("utf-8") if info.filename.endswith(member_suffix) else data)
    return target


def _upload(client, base, path: Path, request_id: str) -> dict:
    with path.open("rb") as handle:
        uploaded = client.post(f"{base}/inputs", files={"file": (path.name, handle, "application/zip")},
                               headers={**OWNER, "X-Compare-Upload-Id": request_id})
    assert uploaded.status_code == 201, uploaded.text
    return uploaded.json()


def test_api_upload_input_against_release(api, tmp_path):
    manager, project_id, client = api
    version = _release(manager, project_id, {"ontology/object/order.md": "# Order\n\nA synthetic order.\n"})
    released, _record = publish.release_package(manager, project_id, version)
    edited = _edited_copy(released, tmp_path / "edited.zip", "objects/order.md", "# Order\n\nA synthetic order, edited.\n")
    base = f"/api/v1/projects/{project_id}/package-comparisons"
    item = _upload(client, base, edited, "upload_demo")
    assert item["package_version"] == version and item["expires_at"].endswith("Z")

    started = client.post(base, json={"left": {"kind": "release", "version": version},
                                      "right": {"kind": "upload", "input_id": item["input_id"]}}, headers=OWNER)
    assert started.status_code == 202
    result = _ready(client, base, started.json()["comparison_id"])
    assert result["status"] == "ready", result
    assert result["summary"]["files"] == {"added": 0, "deleted": 0, "modified": 1,
                                          "unchanged": result["summary"]["left_file_count"] - 1}

    busy = client.delete(f"{base}/inputs/{item['input_id']}", headers=OWNER)
    assert busy.status_code == 409 and busy.json()["code"] == "compare_input_in_use"
    assert client.delete(f"{base}/{result['comparison_id']}", headers=OWNER).status_code == 204
    assert client.delete(f"{base}/inputs/{item['input_id']}", headers=OWNER).status_code == 204
    assert client.delete(f"{base}/uploads/upload_demo", headers=OWNER).status_code == 204


def test_api_cross_package_upload_needs_confirmation(api, tmp_path):
    manager, project_id, client = api
    version = _release(manager, project_id, {"ontology/object/order.md": "# Order\n\nA synthetic order.\n"})
    # 没走 Hub 的本地导出，manifest id 是项目 ID，和发布包（Hub 包标识）不是同一个知识包
    exported = publish.export_package_zip(manager, project_id, tmp_path / "local.zip", allow_unbound=True)
    base = f"/api/v1/projects/{project_id}/package-comparisons"
    item = _upload(client, base, exported, "upload_cross")
    body = {"left": {"kind": "release", "version": version}, "right": {"kind": "upload", "input_id": item["input_id"]}}
    first = _ready(client, base, client.post(base, json=body, headers=OWNER).json()["comparison_id"])
    assert first["status"] == "failed" and first["error_code"] == "compare_cross_package"
    confirmed = client.post(base, json={**body, "allow_cross_package": True}, headers=OWNER)
    assert confirmed.status_code == 202
    assert _ready(client, base, confirmed.json()["comparison_id"])["status"] == "ready"


def test_api_rejects_unknown_release_and_invalid_sources(api):
    _manager, project_id, client = api
    base = f"/api/v1/projects/{project_id}/package-comparisons"
    missing = client.post(base, json={"left": {"kind": "release", "version": "0.9.0"},
                                      "right": {"kind": "release", "version": "0.8.0"}}, headers=OWNER)
    assert missing.status_code == 404 and missing.json()["code"] == "release_not_found"
    assert not list(package_compare.root("employee_0001").glob("cmp_*"))
    invalid = client.post(base, json={"left": {"kind": "release", "release_id": "rel_demo"},
                                      "right": {"kind": "release", "version": "0.8.0"}}, headers=OWNER)
    assert invalid.status_code == 400 and invalid.json()["code"] == "invalid_argument"


def test_restart_after_shutdown_accepts_new_comparisons(comparison_root, monkeypatch):
    monkeypatch.setattr(package_compare, "_SERVICE_LOCK", None)
    monkeypatch.setattr(package_compare, "_EXECUTOR", ThreadPoolExecutor(max_workers=1))
    assert package_compare.start() is True
    package_compare.shutdown()
    with pytest.raises(WorkspaceError) as error:
        package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha")
    assert error.value.status == 503
    assert package_compare.start() is True
    left = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "a"}), project_id="project_alpha")
    right = package_compare.create_input(comparison_root, archive(**{"objects/a.md": "b"}), project_id="project_alpha")
    job = package_compare.start_comparison(
        None, "project_alpha", comparison_root,
        {"kind": "upload", "input_id": left["input_id"]}, {"kind": "upload", "input_id": right["input_id"]},
    )
    assert job["status"] == "queued"
    drain_tasks()
    package_compare.shutdown()

