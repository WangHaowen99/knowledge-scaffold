"""Synthetic script revisions verify save rejection, diagnostics and publication boundaries."""
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from artifact_process_policy import child_environment, git_executable
from app import config
from app.services.toolkit import script_contract, script_verify
from app.services.workspace import META_FILE, WorkspaceError, WorkspaceManager

SOURCE = '''"""
关联operation：operation_demo
功能描述：合成计算
输入参数：
    quantity    integer    合成数量；默认值：1
输出：
    result    integer    合成结果
"""
import argparse
import json

def calculate(quantity):
    return {"result": quantity}

def _cli():
    parser = argparse.ArgumentParser()
    parser.add_argument("--quantity", type=int, default=1)
    print(json.dumps(calculate(**vars(parser.parse_args()))))

if __name__ == "__main__":
    _cli()
'''
REL = "ontology/scripts/script_demo.py"


def git(root, *args):
    allowed = {
        ("init", "-b", "test/script-save"),
        ("config", "user.name", "operator_a"),
        ("config", "user.email", "user@example.com"),
        ("config", "commit.gpgsign", "false"),
        ("config", "core.hooksPath", str(root.parent / "process-home/empty-hooks")),
        ("rev-parse", "HEAD"),
        ("status", "--porcelain"),
    }
    if args not in allowed:
        raise ValueError("Git operation is outside the synthetic fixture allowlist")
    # Reviewed S4721 hotspot: fixed Git operations on synthetic tmp_path repositories only.
    # Resolve Git from the OS path; isolate host settings, disable hooks/transports and bound execution.
    return subprocess.run(  # NOSONAR(S4721)
        [git_executable(), *args], cwd=root, env=child_environment(root.parent),
        capture_output=True, text=True, check=True, shell=False, timeout=30,
    ).stdout.strip()


@pytest.fixture()
def project(tmp_path):
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    pid = "project-alpha"
    root = manager.workspaces_dir / pid
    (root / "ontology/scripts").mkdir(parents=True)
    (root / "ontology/operation").mkdir()
    (root / META_FILE).write_text(json.dumps({"id": pid, "name": "workspace_demo"}), encoding="utf-8")
    (root / "ontology/operation/operation_demo.md").write_text(
        "# 业务描述\n合成操作。\n\n# 与operation的关联\n- 被operation调用 > 无\n- 调用operation > 无\n"
        "\n# 输入\n| 参数名称 | 描述 | 输入源 |\n| --- | --- | --- |\n| quantity | 合成数量 | 用户输入 |\n"
        "\n# 输出\n| 参数名称 | 描述 |\n| --- | --- |\n| result | 合成结果 |\n\n# 逻辑描述\n1. 合成计算。\n",
        encoding="utf-8",
    )
    git(root, "init", "-b", "test/script-save")
    git(root, "config", "user.name", "operator_a")
    git(root, "config", "user.email", "user@example.com")
    git(root, "config", "commit.gpgsign", "false")
    git(root, "config", "core.hooksPath", str(root.parent / "process-home/empty-hooks"))
    manager.write_file(pid, REL, SOURCE)
    return manager, pid, root


@pytest.mark.parametrize("args", [
    ("push", "origin", "HEAD"),
    ("status", "--porcelain", "--ignored"),
    ("config", "core.hooksPath", "untrusted-hooks"),
])
def test_git_fixture_rejects_unapproved_commands_before_execution(tmp_path, args):
    with pytest.raises(ValueError, match="allowlist"):
        git(tmp_path, *args)
    assert not list(tmp_path.iterdir())


def test_git_fixture_ignores_host_executable_path_and_git_environment(project, monkeypatch, tmp_path):
    _manager, _pid, root = project
    expected = git(root, "rev-parse", "HEAD")
    expected_status = git(root, "status", "--porcelain")
    monkeypatch.setenv("PATH", str(tmp_path / "no-executables"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "not-a-worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "unused-index"))
    assert git(root, "rev-parse", "HEAD") == expected
    assert git(root, "status", "--porcelain") == expected_status
    assert not (tmp_path / "unused-index").exists()


@pytest.mark.parametrize("revision, message", [
    (SOURCE + "\ndef broken(:\n", "脚本语法错误"),
    (SOURCE + "\nreturn 1\n", "脚本语法错误"),  # ast.parse alone accepts this.
    (SOURCE.replace("quantity    integer", "other    integer"), "头部参数与 argparse 不一致"),
    (SOURCE.replace('return {"result": quantity}', 'return {"other": quantity}'), "输出字段与声明不一致"),
])
def test_invalid_save_preserves_source_descriptor_index_and_history(project, revision, message):
    manager, pid, root = project
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "ontology").rglob("*") if p.is_file()}
    head, status = git(root, "rev-parse", "HEAD"), git(root, "status", "--porcelain")
    with pytest.raises(WorkspaceError, match=message) as error:
        manager.write_file(pid, REL, revision, base_revision=manager.read_file(pid, REL, 10000)["revision"])
    assert error.value.status == 422 and error.value.code == "script_validation_failed"
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "ontology").rglob("*") if p.is_file()}
    assert git(root, "rev-parse", "HEAD") == head
    assert git(root, "status", "--porcelain") == status


def test_invalid_new_script_does_not_create_file(project):
    manager, pid, root = project
    with pytest.raises(WorkspaceError, match="第 .* 行，第 .* 列"):
        manager.write_file(pid, "ontology/scripts/new_demo.py", "def broken(:")
    assert not (root / "ontology/scripts/new_demo.py").exists()


def test_save_compiles_without_executing_source(project):
    manager, pid, root = project
    code = SOURCE + '\nraise RuntimeError("synthetic sentinel: must never execute during save")\n'
    saved = manager.write_file(pid, REL, code)
    assert saved["commit"] and (root / REL).read_text(encoding="utf-8") == code


@pytest.mark.parametrize("marker", ["TODO", "FIXME", "todo", "fixme"])
def test_markers_save_with_location_and_commit(project, marker):
    manager, pid, root = project
    code = SOURCE + f"\n# {marker}: synthetic unfinished item\n"
    saved = manager.write_file(pid, REL, code)
    assert saved["commit"] and (root / REL).read_text(encoding="utf-8") == code
    warning, = saved["warnings"]
    assert warning == {"path": REL, "line": len(code.splitlines()), "column": 3,
                       "code": "unfinished_marker", "message": f"存在 {marker.upper()} 未完成标记"}
    assert manager.write_file(pid, REL, code)["warnings"] == saved["warnings"]
    assert manager.write_file(pid, "notes.md", "TODO: synthetic note")["warnings"] == []


def test_native_file_api_reports_rejection_and_warning(project, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import JSONResponse
    from fastapi.testclient import TestClient
    from app.routers import projects

    manager, pid, root = project
    app = FastAPI()
    app.include_router(projects.router)
    monkeypatch.setattr(projects, "workspaces", manager)
    monkeypatch.setattr(projects, "ensure_access", lambda *_args, **_kwargs: root)
    monkeypatch.setattr(projects, "_request_user", lambda _request: "operator_a")

    @app.exception_handler(WorkspaceError)
    async def error_handler(_request, exc):
        return JSONResponse(status_code=exc.status, content={"error": str(exc), "code": exc.code})

    with TestClient(app) as client:
        rejected = client.put(f"/api/projects/{pid}/file", json={"path": REL, "content": SOURCE + "\nreturn 1\n"})
        assert rejected.status_code == 422 and "行" in rejected.json()["error"]
        assert (root / REL).read_text(encoding="utf-8") == SOURCE
        saved = client.put(f"/api/projects/{pid}/file", json={"path": REL, "content": SOURCE + "\n# FIXME: synthetic\n"})
        assert saved.status_code == 200 and saved.json()["warnings"][0]["code"] == "unfinished_marker"


def test_docstring_marker_warns_but_runtime_string_and_identifier_do_not(project):
    manager, pid, _root = project
    code = SOURCE.replace("功能描述：合成计算", "功能描述：合成计算\nTODO: synthetic documentation")
    code += '\nTODO_ITEMS = "FIXME: synthetic input text"\n'
    warnings = manager.write_file(pid, REL, code)["warnings"]
    assert len(warnings) == 1 and warnings[0]["line"] == 4


def test_marker_warnings_are_bounded(project):
    manager, pid, _root = project
    warnings = manager.write_file(pid, REL, SOURCE + "\n# TODO: synthetic\n" * 80)["warnings"]
    assert len(warnings) == 50 and warnings[-1]["code"] == "warnings_truncated"


def test_revision_conflict_precedes_validation(project):
    manager, pid, _root = project
    with pytest.raises(WorkspaceError) as error:
        manager.write_file(pid, REL, "def broken(:", base_revision="stale-synthetic-revision")
    assert error.value.status == 409


@pytest.mark.parametrize("code", [
    SOURCE + "\nreturn 1\n",
    SOURCE.replace('return {"result": quantity}', 'return {"other": quantity}'),
])
def test_export_cannot_bypass_validation_by_direct_file_write(project, tmp_path, code):
    manager, pid, root = project
    (root / REL).write_text(code, encoding="utf-8")
    archive = tmp_path / "package.zip"
    with pytest.raises(WorkspaceError, match="契约无效"):
        manager.export_package_zip(pid, archive)
    assert not archive.exists()


def test_dynamic_output_requires_current_verification_in_frozen_release(project, tmp_path, monkeypatch, capsys):
    from app.services import hub_packages, knowledge_hub
    manager, pid, root = project
    code = SOURCE.replace('return {"result": quantity}', 'return dict(result=quantity)')
    saved = manager.write_file(pid, REL, code)
    assert any("尚未验证" in item["message"] for item in saved["warnings"])
    archive = tmp_path / "package.zip"
    with pytest.raises(WorkspaceError) as error:
        manager.export_package_zip(pid, archive)
    assert error.value.code == "script_output_unverified" and not archive.exists()
    cases = tmp_path / "synthetic.cases.json"
    cases.write_text(json.dumps([{"name": "synthetic success", "argv": [], "expect": "success"}]), encoding="utf-8")
    monkeypatch.setattr(script_verify.sandbox, "gateway_url", lambda: "")
    assert script_verify.main(["--root", str(root), "run", REL, "--cases", str(cases)]) == 0
    capsys.readouterr()
    frozen = tmp_path / "frozen"
    hub_packages.freeze(manager, pid, frozen, False)
    snapshot = WorkspaceManager(frozen / "input", manager.assets_dir)
    snapshot.export_package_zip(pid, archive, version="0.1.0")
    assert knowledge_hub.file_digest(archive)
    with zipfile.ZipFile(archive) as package:
        assert not any("script-verifications" in name for name in package.namelist())
    # Frozen verification is independent of subsequent edits to the live workspace.
    manager.write_file(pid, REL, code + "\n# synthetic change\n")
    snapshot.export_package_zip(pid, archive, version="0.1.0")
    with pytest.raises(WorkspaceError, match="重新运行验证"):
        manager.export_package_zip(pid, tmp_path / "stale.zip")


def test_helper_change_invalidates_dynamic_output_verification(project, tmp_path, monkeypatch, capsys):
    manager, pid, root = project
    helper = root / "ontology/scripts/_helper_demo.py"
    helper.write_text("def calculate(quantity):\n    return dict(result=quantity)\n", encoding="utf-8")
    code = SOURCE.replace('def calculate(quantity):\n    return {"result": quantity}', "from _helper_demo import calculate")
    manager.write_file(pid, REL, code)
    cases = tmp_path / "synthetic.cases.json"
    cases.write_text(json.dumps([{"name": "synthetic", "argv": [], "expect": "success"}]), encoding="utf-8")
    monkeypatch.setattr(script_verify.sandbox, "gateway_url", lambda: "")
    assert script_verify.main(["--root", str(root), "run", REL, "--cases", str(cases)]) == 0
    capsys.readouterr()
    manager.export_package_zip(pid, tmp_path / "verified.zip")
    helper.write_text(helper.read_text(encoding="utf-8") + "\n# synthetic dependency change\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="依赖已修改"):
        manager.export_package_zip(pid, tmp_path / "stale.zip")
