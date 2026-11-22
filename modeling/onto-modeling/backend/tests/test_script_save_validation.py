"""Synthetic script revisions verify save rejection, diagnostics and publication boundaries (develop !616 on the PR555 write path)."""
import json
import subprocess
import zipfile
from pathlib import Path

import pytest

from artifact_process_policy import child_environment, git_executable
from app import config
from app.services import publish as publish_service
from app.services.toolkit import script_verify
from app.services.workspace import WorkspaceError, WorkspaceManager

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
OPERATION = (
    "# 业务描述\n合成操作。\n\n# 与operation的关联\n- 被operation调用 > 无\n- 调用operation > 无\n"
    "\n# 输入\n| 参数名称 | 描述 | 输入源 |\n| --- | --- | --- |\n| quantity | 合成数量 | 用户输入 |\n"
    "\n# 输出\n| 参数名称 | 描述 |\n| --- | --- |\n| result | 合成结果 |\n\n# 逻辑描述\n1. 合成计算。\n"
)


def git(root, *args, scratch):
    # Read-only inspection of repositories the workspace manager created; scratch holds the isolated process home.
    allowed = {("rev-parse", "HEAD"), ("status", "--porcelain")}
    if args not in allowed:
        raise ValueError("Git operation is outside the synthetic fixture allowlist")
    # Reviewed S4721 hotspot: fixed Git operations on synthetic tmp_path repositories only.
    # Resolve Git from the OS path; isolate host settings, disable hooks/transports and bound execution.
    return subprocess.run(  # NOSONAR(S4721)
        [git_executable(), *args], cwd=root, env=child_environment(scratch),
        capture_output=True, text=True, check=True, shell=False, timeout=30,
    ).stdout.strip()


@pytest.fixture()
def project(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    manager = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    pid = manager.create_project("workspace_demo", owner="operator_a")["id"]
    root = manager.project_root(pid)
    manager.write_file(pid, "ontology/operation/operation_demo.md", OPERATION, author="operator_a")
    manager.write_file(pid, REL, SOURCE, author="operator_a")
    return manager, pid, root


@pytest.fixture()
def scratch(tmp_path):
    folder = tmp_path / "git-scratch"
    folder.mkdir()
    return folder


@pytest.mark.parametrize("args", [
    ("push", "origin", "HEAD"),
    ("status", "--porcelain", "--ignored"),
    ("config", "core.hooksPath", "untrusted-hooks"),
])
def test_git_fixture_rejects_unapproved_commands_before_execution(tmp_path, args):
    with pytest.raises(ValueError, match="allowlist"):
        git(tmp_path, *args, scratch=tmp_path)
    assert not list(tmp_path.iterdir())


def test_git_fixture_ignores_host_executable_path_and_git_environment(project, scratch, monkeypatch, tmp_path):
    _manager, _pid, root = project
    expected = git(root, "rev-parse", "HEAD", scratch=scratch)
    expected_status = git(root, "status", "--porcelain", scratch=scratch)
    monkeypatch.setenv("PATH", str(tmp_path / "no-executables"))
    monkeypatch.setenv("GIT_DIR", str(tmp_path / "not-a-repository"))
    monkeypatch.setenv("GIT_WORK_TREE", str(tmp_path / "not-a-worktree"))
    monkeypatch.setenv("GIT_INDEX_FILE", str(tmp_path / "unused-index"))
    assert git(root, "rev-parse", "HEAD", scratch=scratch) == expected
    assert git(root, "status", "--porcelain", scratch=scratch) == expected_status
    assert not (tmp_path / "unused-index").exists()


@pytest.mark.parametrize("revision, message", [
    (SOURCE + "\ndef broken(:\n", "脚本语法错误"),
    (SOURCE + "\nreturn 1\n", "脚本语法错误"),  # ast.parse alone accepts this.
    (SOURCE.replace("quantity    integer", "other    integer"), "头部参数与 argparse 不一致"),
    (SOURCE.replace('return {"result": quantity}', 'return {"other": quantity}'), "输出字段与声明不一致"),
])
def test_invalid_save_preserves_source_descriptor_index_and_history(project, scratch, revision, message):
    manager, pid, root = project
    before = {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "ontology").rglob("*") if p.is_file()}
    head, status = git(root, "rev-parse", "HEAD", scratch=scratch), git(root, "status", "--porcelain", scratch=scratch)
    with pytest.raises(WorkspaceError, match=message) as error:
        manager.write_file(pid, REL, revision, base_revision=manager.read_file(pid, REL, 10000)["revision"])
    # PR555 错误体：F-03 的 check_rejected，details.errors 是 Finding（develop 的 script_validation_failed）
    assert error.value.status == 422 and error.value.code == "check_rejected"
    finding, = error.value.details["errors"]
    assert finding["code"] == "script_contract" and finding["level"] == "error" and finding["path"] == REL
    assert error.value.details["warnings"] == []
    assert before == {p.relative_to(root).as_posix(): p.read_bytes() for p in (root / "ontology").rglob("*") if p.is_file()}
    assert git(root, "rev-parse", "HEAD", scratch=scratch) == head
    assert git(root, "status", "--porcelain", scratch=scratch) == status


def test_invalid_new_script_does_not_create_file(project):
    manager, pid, root = project
    with pytest.raises(WorkspaceError, match="第 .* 行，第 .* 列") as error:
        manager.write_file(pid, "ontology/scripts/new_demo.py", "def broken(:")
    finding, = error.value.details["errors"]
    assert finding["line"] == 1 and finding["column"] >= 1
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
    assert warning == {"path": REL, "line": len(code.splitlines()), "column": 3, "level": "warning",
                       "code": "unfinished_marker", "message": f"存在 {marker.upper()} 未完成标记"}
    assert manager.write_file(pid, REL, code)["warnings"] == saved["warnings"]
    assert manager.write_file(pid, "ontology/rule/rule_demo.md", "TODO: synthetic note")["warnings"] == []


def test_v1_file_api_reports_rejection_and_warning(project, monkeypatch):
    from fastapi.testclient import TestClient

    from app.api import deps
    from app.main import app
    from app.services import auth, streams

    manager, pid, root = project
    monkeypatch.setattr(deps, "workspaces", manager)
    monkeypatch.setattr(auth, "ENABLED", False)
    streams.reset_all()
    url = f"/api/v1/projects/{pid}/files/content"
    try:
        with TestClient(app, raise_server_exceptions=False) as client:
            current = client.get(url, params={"path": REL}).json()["revision"]
            rejected = client.put(url, json={"path": REL, "content": SOURCE + "\nreturn 1\n", "base_revision": current})
            body = rejected.json()
            assert rejected.status_code == 422 and body["code"] == "check_rejected" and "行" in body["message"]
            assert body["details"]["errors"][0]["path"] == REL
            assert (root / REL).read_text(encoding="utf-8") == SOURCE
            saved = client.put(url, json={"path": REL, "content": SOURCE + "\n# FIXME: synthetic\n", "base_revision": current})
            assert saved.status_code == 200
            assert [(item["code"], item["level"]) for item in saved.json()["warnings"]] == [("unfinished_marker", "warning")]
            other = client.put(url, json={"path": "ontology/rule/rule_demo.md", "content": "# 合成规则\n", "base_revision": ""})
            assert other.status_code == 200 and other.json()["warnings"] == []
    finally:
        streams.reset_all()


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
        publish_service.export_package_zip(manager, pid, archive)
    assert not archive.exists()


def run_verification(root, tmp_path, monkeypatch, capsys):
    cases = tmp_path / "synthetic.cases.json"
    cases.write_text(json.dumps([{"name": "synthetic success", "argv": [], "expect": "success"}]), encoding="utf-8")
    monkeypatch.setattr(script_verify.sandbox, "gateway_url", lambda: "")
    assert script_verify.main(["--root", str(root), "run", REL, "--cases", str(cases)]) == 0
    capsys.readouterr()


def test_dynamic_output_requires_current_verification_in_frozen_release(project, tmp_path, monkeypatch, capsys):
    from app.services import hub_packages, knowledge_hub
    manager, pid, root = project
    code = SOURCE.replace('return {"result": quantity}', 'return dict(result=quantity)')
    saved = manager.write_file(pid, REL, code)
    assert any("尚未验证" in item["message"] for item in saved["warnings"])
    archive = tmp_path / "package.zip"
    with pytest.raises(WorkspaceError) as error:
        publish_service.export_package_zip(manager, pid, archive)
    assert error.value.code == "script_output_unverified" and error.value.status == 422 and not archive.exists()
    run_verification(root, tmp_path, monkeypatch, capsys)
    # Hub 发布冻结（PR555：只读树直接冻结在 input/）带上验证记录，但记录不算进发布输入
    frozen = tmp_path / "frozen"
    value, _digest = hub_packages.freeze(manager, pid, frozen, False)
    assert (frozen / "input/.ontology-state/script-verifications.json").is_file()
    assert not any("script-verifications" in name for name in value["files"])
    publish_service.export_package_zip(manager, pid, archive, source_root=frozen / "input", version="0.1.0")
    assert knowledge_hub.file_digest(archive)
    with zipfile.ZipFile(archive) as package:
        assert not any("script-verifications" in name for name in package.namelist())
    # Frozen verification is independent of subsequent edits to the live workspace.
    manager.write_file(pid, REL, code + "\n# synthetic change\n")
    publish_service.export_package_zip(manager, pid, archive, source_root=frozen / "input", version="0.1.0")
    with pytest.raises(WorkspaceError, match="重新运行验证"):
        publish_service.export_package_zip(manager, pid, tmp_path / "stale.zip")


def test_publish_check_reads_workspace_record_for_committed_source(project, tmp_path, monkeypatch, capsys):
    """发布检查按提交导出的只读树（不带记录）：读工作区的记录，哈希按提交里的源码算。"""
    manager, pid, root = project
    code = SOURCE.replace('return {"result": quantity}', 'return dict(result=quantity)')
    manager.write_file(pid, REL, code)

    def findings():
        check = publish_service.package_binding_check(manager, pid)
        return [item for gate in check["gates"] for item in gate.get("findings") or []
                if item.get("code") == "script_output_unverified"]

    unverified, = findings()
    assert unverified["path"] == REL and "script_verify.py run" in unverified["message"]
    run_verification(root, tmp_path, monkeypatch, capsys)
    assert findings() == []
    # 工作区改了但没提交：提交里的源码仍对得上记录
    (root / REL).write_text(code + "\n# synthetic uncommitted change\n", encoding="utf-8")
    assert findings() == []


def test_helper_change_invalidates_dynamic_output_verification(project, tmp_path, monkeypatch, capsys):
    manager, pid, root = project
    helper = root / "ontology/scripts/_helper_demo.py"
    helper.write_text("def calculate(quantity):\n    return dict(result=quantity)\n", encoding="utf-8")
    code = SOURCE.replace('def calculate(quantity):\n    return {"result": quantity}', "from _helper_demo import calculate")
    manager.write_file(pid, REL, code)
    run_verification(root, tmp_path, monkeypatch, capsys)
    publish_service.export_package_zip(manager, pid, tmp_path / "verified.zip")
    helper.write_text(helper.read_text(encoding="utf-8") + "\n# synthetic dependency change\n", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="依赖已修改"):
        publish_service.export_package_zip(manager, pid, tmp_path / "stale.zip")
