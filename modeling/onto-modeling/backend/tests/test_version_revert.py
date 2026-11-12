"""版本回退的业务口径与并发保护（ADR-0094；移植 develop !567、!570 的回退测试）。数据全部合成。"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app import config
from app.api import deps
from app.main import app
from app.services import auth, streams, studio, version_revert
from app.services.onto_mcp.errors import McpError
from app.services.workspace import WorkspaceError, WorkspaceManager
from app.services.workspace.versions import managed_dirty_paths
from tests.proc import git as proc_git



def _git(root: Path, *args: str) -> str:
    return proc_git(root, "-c", "core.hooksPath=/dev/null", *args)


def _write(root: Path, rel: str, text: str) -> None:
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _commit(root: Path, message: str) -> str:
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", message)
    return _git(root, "rev-parse", "HEAD").strip()


@pytest.fixture()
def manager(tmp_path: Path, monkeypatch) -> WorkspaceManager:
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    return WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))


@pytest.fixture()
def project(manager):
    meta = manager.create_project("回退合成", owner="operator_a")
    return meta["id"], manager.project_root(meta["id"])


@pytest.fixture()
def idle(monkeypatch):
    """没有助手会话：回退前后的会话处理换成记录调用的替身。"""
    calls = {"recycled": [], "cleared": []}

    async def recycle(session, reason="recycled", **_):
        calls["recycled"].append((session, reason))

    async def drain():
        return None

    sessions = SimpleNamespace(_lock=asyncio.Lock(), _sessions={}, _recycle=recycle, _drain_waiters=drain)
    sessions.get = lambda project_id: sessions._sessions.get(project_id)
    from app.services import agent_sessions, conversation_store

    sessions.workspace_busy = lambda project_id: agent_sessions.SessionManager.workspace_busy(sessions, project_id)

    monkeypatch.setattr(agent_sessions, "manager", sessions)
    monkeypatch.setattr(conversation_store, "clear_all_resume_tokens", calls["cleared"].append)
    calls["sessions"] = sessions
    return calls


# ---- 恢复什么 ----


def test_revert_message_names_the_target_version(manager, project):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    first = _commit(root, "c1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "c2")

    result = manager.revert_to(project_id, first)
    assert (root / "ontology/object/item.md").read_text(encoding="utf-8") == "v1"
    number = int(_git(root, "rev-list", "--first-parent", "--count", first).strip())
    assert manager.versions(project_id)[0]["message"] == f"revert: 回退到版本 {number}（{first[:8]}）"
    assert result["changes"] == {"added": [], "modified": ["ontology/object/item.md"], "deleted": [], "total": 1}


def test_revert_restores_complete_business_snapshot_and_keeps_metadata(manager, project):
    project_id, root = project
    _write(root, "ontology/object/kept.md", "old")
    _write(root, "ontology/object/restored.md", "restore me")
    target = _commit(root, "target")

    _write(root, "ontology/object/kept.md", "new")
    (root / "ontology/object/restored.md").unlink()
    _write(root, "ontology/object/later.md", "remove me")
    current_ignore = (root / ".gitignore").read_text(encoding="utf-8") + "custom-cache/\n"
    (root / ".gitignore").write_text(current_ignore, encoding="utf-8")
    _commit(root, "later")
    manager._update_meta(project_id, name="current name")

    from app.services import version_views

    preview = version_views.revert_preview(root, target, _git(root, "rev-parse", "HEAD").strip())
    assert preview["changes"]["total"] == 3
    result = manager.revert_to(project_id, target, expected_head=preview["expected_head"])

    assert result["changes"] == preview["changes"]
    assert (root / "ontology/object/kept.md").read_text(encoding="utf-8") == "old"
    assert (root / "ontology/object/restored.md").read_text(encoding="utf-8") == "restore me"
    assert not (root / "ontology/object/later.md").exists()
    assert manager.get_project(project_id)["name"] == "current name"
    assert (root / ".gitignore").read_text(encoding="utf-8") == current_ignore
    assert _git(root, "ls-tree", "HEAD", "--", "ontology/object/kept.md").startswith("100644 ")
    assert managed_dirty_paths(root) == []


def test_revert_ignores_generated_index_only_difference(manager, project):
    project_id, root = project
    _write(root, "ontology/object/index.md", "# 旧的目录投影\n")
    target = _commit(root, "historical index")
    manager._refresh_indexes(root)
    _commit(root, "refresh generated index")

    with pytest.raises(WorkspaceError, match="业务内容.*一致") as caught:
        manager.revert_to(project_id, target)
    assert caught.value.code == "nothing_to_revert"


def test_revert_commits_regenerated_indexes_and_allows_another_revert(manager, project):
    project_id, root = project
    _write(root, "ontology/object/old.md", "# old")
    manager._refresh_indexes(root)
    target = _commit(root, "old object")
    (root / "ontology/object/old.md").unlink()
    _write(root, "ontology/object/new.md", "# new")
    manager._refresh_indexes(root)
    current = _commit(root, "new object")

    manager.revert_to(project_id, target)

    assert _git(root, "status", "--porcelain").strip() == ""
    assert "old.md" in (root / "ontology/object/index.md").read_text(encoding="utf-8")
    from app.services import version_views

    preview = version_views.revert_preview(root, current, _git(root, "rev-parse", "HEAD").strip())
    assert version_revert.blockers(manager, project_id) == []
    assert preview["changes"]["total"] == 2


@pytest.mark.parametrize("dirty_kind", ["modified", "staged", "untracked"])
def test_revert_rejects_dirty_workspace(manager, project, dirty_kind):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    if dirty_kind == "untracked":
        _write(root, "ontology/object/untracked.md", "draft")
    else:
        _write(root, "ontology/object/item.md", "draft")
        if dirty_kind == "staged":
            _git(root, "add", "ontology/object/item.md")

    with pytest.raises(WorkspaceError, match="未提交或暂存") as caught:
        manager.revert_to(project_id, target)
    assert caught.value.code == "workspace_dirty" and caught.value.status == 409
    assert version_revert.blockers(manager, project_id)[-1]["code"] == "workspace_dirty"


def test_metadata_and_runtime_state_do_not_count_as_dirty(manager, project):
    project_id, root = project
    manager._update_meta(project_id, name="renamed")
    _write(root, ".ontology-state/progress.json", "{}")
    _write(root, ".onto/run/engine.json", "{}")
    assert managed_dirty_paths(root) == []


def test_revert_rejects_current_missing_and_non_ancestor(manager, project):
    project_id, root = project
    current = _git(root, "rev-parse", "HEAD").strip()
    for commit, code, text in ((current, "nothing_to_revert", "当前已是"), ("f" * 40, "invalid_commit", "不存在"),
                               ("HEAD", "invalid_commit", "无效的版本号")):
        with pytest.raises(WorkspaceError, match=text) as caught:
            manager.revert_to(project_id, commit)
        assert caught.value.code == code

    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(root, "checkout", "-q", "--orphan", "side")
    _write(root, "side.md", "side")
    side = _commit(root, "side")
    _git(root, "checkout", "-q", "-f", branch)
    with pytest.raises(WorkspaceError, match="当前版本历史") as caught:
        manager.revert_to(project_id, side)
    assert caught.value.code == "invalid_commit"


def test_revert_rejects_stale_preview_and_busy_project(manager, project):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    previewed = _commit(root, "v2")
    _write(root, "ontology/object/item.md", "v3")
    _commit(root, "v3")

    with pytest.raises(WorkspaceError, match="重新预览") as caught:
        manager.revert_to(project_id, target, expected_head=previewed)
    assert caught.value.code == "head_moved"

    def busy(_root):
        raise WorkspaceError("busy", 409, "session_busy")

    with pytest.raises(WorkspaceError, match="busy"):
        manager.revert_to(project_id, target, busy_check=busy)
    assert (root / "ontology/object/item.md").read_text(encoding="utf-8") == "v3"


def test_revert_failure_restores_business_files_without_overwriting_metadata(manager, project, monkeypatch):
    from app.kernel.writepath import tx

    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _write(root, "ontology/object/later.md", "later")
    head = _commit(root, "v2")
    manager._update_meta(project_id, name="metadata kept")

    def fail(*_args, **_kwargs):
        raise WorkspaceError("simulated commit failure", 500)

    monkeypatch.setattr(tx, "commit_paths", fail)
    with pytest.raises(WorkspaceError, match="simulated"):
        manager.revert_to(project_id, target)

    assert (root / "ontology/object/item.md").read_text(encoding="utf-8") == "v2"
    assert (root / "ontology/object/later.md").read_text(encoding="utf-8") == "later"
    assert manager.get_project(project_id)["name"] == "metadata kept"
    assert _git(root, "rev-parse", "HEAD").strip() == head
    assert managed_dirty_paths(root) == []


def test_revert_removes_later_symlink_without_following_it(manager, project, tmp_path):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    outside = tmp_path / "outside.txt"
    outside.write_text("keep", encoding="utf-8")
    link = root / "ontology/object/later-link"
    link.symlink_to(outside)
    _commit(root, "symlink")

    manager.revert_to(project_id, target)
    assert not link.exists() and not link.is_symlink()
    assert outside.read_text(encoding="utf-8") == "keep"


def test_revert_handles_business_filename_with_newline(manager, project):
    project_id, root = project
    path = root / "ontology/object/line\nbreak.md"
    path.write_text("v1", encoding="utf-8")
    target = _commit(root, "v1")
    path.write_text("v2", encoding="utf-8")
    _commit(root, "v2")

    manager.revert_to(project_id, target)
    assert path.read_text(encoding="utf-8") == "v1"


def test_revert_treats_glob_characters_literally(manager, project):
    project_id, root = project
    _write(root, "ontology/object/a*.md", "v1")
    _write(root, "ontology/object/ab.md", "keep")
    target = _commit(root, "v1")
    _write(root, "ontology/object/a*.md", "v2")
    _commit(root, "v2")
    _write(root, "ontology/object/ab.md", "keep-2")
    _commit(root, "v3")
    _write(root, "ontology/object/ab.md", "keep")
    _commit(root, "v4")

    manager.revert_to(project_id, target)
    assert (root / "ontology/object/a*.md").read_text(encoding="utf-8") == "v1"
    assert (root / "ontology/object/ab.md").read_text(encoding="utf-8") == "keep"


@pytest.mark.parametrize("target_is_directory", [True, False])
def test_revert_handles_file_directory_type_changes(manager, project, target_is_directory):
    project_id, root = project
    path = root / "ontology" / "object" / "sample"
    child = path / "item.md"
    if target_is_directory:
        _write(root, "ontology/object/sample/item.md", "target child")
    else:
        _write(root, "ontology/object/sample", "target file")
    target = _commit(root, "target")
    if target_is_directory:
        child.unlink()
        path.rmdir()
        path.write_text("current file", encoding="utf-8")
    else:
        path.unlink()
        _write(root, "ontology/object/sample/item.md", "current child")
    _commit(root, "current")

    manager.revert_to(project_id, target)
    if target_is_directory:
        assert child.read_text(encoding="utf-8") == "target child"
    else:
        assert path.read_text(encoding="utf-8") == "target file"
    assert managed_dirty_paths(root) == []


def test_revert_invalidates_derived_state(manager, project):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    for rel in (
        ".ontology-state/research-review.json",
        ".ontology-state/script-verifications.json",
        ".ontology-state/solver-validations.json",
        ".ontology-state/artifact-reviews/review.json",
    ):
        _write(root, rel, "{}")
    _write(root, ".ontology-state/eval-sets.json", "keep")

    manager.revert_to(project_id, target)
    assert not (root / ".ontology-state/research-review.json").exists()
    assert not (root / ".ontology-state/artifact-reviews").exists()
    assert (root / ".ontology-state/eval-sets.json").read_text(encoding="utf-8") == "keep"


def test_revert_reports_derived_state_it_could_not_clear(manager, project, monkeypatch):
    from app.services.workspace import versions

    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    monkeypatch.setattr(versions, "invalidate_derived_state", lambda _root: ["progress.json"])
    assert manager.revert_to(project_id, target)["warning"] == "部分派生状态未能清理，请重新运行相关检查"


def test_revert_rejects_staged_metadata(manager, project):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    (root / ".gitattributes").write_text("*.md text\n", encoding="utf-8")
    _git(root, "add", ".gitattributes")
    with pytest.raises(WorkspaceError, match="已暂存的元数据修改") as caught:
        manager.revert_to(project_id, target)
    assert caught.value.code == "workspace_dirty"


# ---- 资料随回退恢复（develop !567 同口径） ----


def test_resource_upload_can_be_previewed_and_reverted(manager, project):
    from app.services import version_views

    project_id, root = project
    target = _git(root, "rev-parse", "HEAD").strip()
    saved = manager.save_resource(project_id, "others", "source.md", b"source", purpose="goal")
    assert saved.get("commit") and managed_dirty_paths(root) == []
    assert version_revert.blockers(manager, project_id) == []

    preview = version_views.revert_preview(root, target, _git(root, "rev-parse", "HEAD").strip())
    deleted = sorted(item["path"] for item in preview["files"] if item["change"] == "delete")
    assert "resource/others/source.md" in deleted and "resource/materials.json" in deleted
    manager.revert_to(project_id, target, expected_head=preview["expected_head"])
    assert not (root / "resource/others/source.md").exists()
    assert manager.resource_directives(project_id) == {}


def test_resource_delete_can_be_reverted(manager, project):
    project_id, root = project
    manager.save_resource(project_id, "others", "source.md", b"source", purpose="goal")
    target = _git(root, "rev-parse", "HEAD").strip()
    manager.delete_resource(project_id, "resource/others/source.md")
    assert version_revert.blockers(manager, project_id) == []

    manager.revert_to(project_id, target)
    assert (root / "resource/others/source.md").read_bytes() == b"source"
    assert manager.resource_directives(project_id)["resource/others/source.md"]["purpose"] == "goal"


def test_resource_directive_version_can_be_previewed_and_reverted(manager, project, idle):
    project_id, root = project
    _write(root, "resource/others/source.md", "source")
    _commit(root, "test: add source")
    target = manager.versions(project_id)[0]["n"]
    manager.set_resource_directive(project_id, "resource/others/source.md", "goal")
    assert version_revert.blockers(manager, project_id) == []

    preview = version_revert.preview_by_version(manager, project_id, target, "synthetic-token", "operator_a")
    assert preview["ready"] is True and preview["changes"]["deleted"] == ["resource/materials.json"]
    asyncio.run(version_revert.apply_preview(manager, project_id, preview["preview_id"], "synthetic-token",
                                             "operator_a"))
    assert not (root / "resource/materials.json").exists()
    assert (root / "resource/others/source.md").exists()
    assert manager.resource_directives(project_id) == {}


# ---- 挡住回退的情况与会话处理 ----


def test_studio_draft_blocks_revert(manager, project, idle):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    studio.save_draft(project_id, "ontology/object/item.md", "draft", "rev", "operator_a")

    found = version_revert.blockers(manager, project_id)
    assert [item["code"] for item in found] == ["studio_draft_pending"]
    assert found[0]["details"]["entry_id"] == "object:item"
    with pytest.raises(WorkspaceError) as caught:
        asyncio.run(version_revert.apply(manager, project_id, target))
    assert caught.value.code == "studio_draft_pending"
    studio.discard_draft(project_id, "ontology/object/item.md", "operator_a")
    asyncio.run(version_revert.apply(manager, project_id, target))
    assert (root / "ontology/object/item.md").read_text(encoding="utf-8") == "v1"


def test_busy_assistant_blocks_and_idle_session_is_recycled(manager, project, idle):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    session = SimpleNamespace(state="idle", pending_permissions=[], sending=0, pending_prompts=["/命令"],
                              chaining=False, is_connecting=lambda: False)
    idle["sessions"]._sessions[project_id] = session

    assert [item["code"] for item in version_revert.blockers(manager, project_id)] == ["session_busy"]
    with pytest.raises(WorkspaceError) as caught:
        asyncio.run(version_revert.apply(manager, project_id, target))
    assert caught.value.code == "session_busy" and idle["cleared"] == []

    session.pending_prompts = []
    result = asyncio.run(version_revert.apply(manager, project_id, target, author="operator_a"))
    assert result["reverted_to"] == target
    assert idle["recycled"] == [(session, "recycled")] and idle["cleared"] == [project_id]


# ---- 接口：V-05 预览、V-06 回退、V-01 并入发布记录 ----


@pytest.fixture()
def api(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(auth, "ENABLED", False)
    monkeypatch.setattr(config, "BACKUP_INTERVAL_HOURS", 0)
    project_id = workspaces.create_project("回退接口合成", owner="operator_a")["id"]
    streams.reset_all()
    with TestClient(app) as client:
        yield client, project_id, workspaces.project_root(project_id)
    streams.reset_all()


def test_preview_reports_blockers_and_revert_checks_expected_head(api):
    client, pid, root = api
    base = f"/api/v1/projects/{pid}/versions"
    _write(root, "ontology/object/item.md", "v1")
    target = _commit(root, "web: v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "web: v2")

    preview = client.get(f"{base}/revert-preview", params={"commit": target}).json()
    assert preview["ready"] is True and preview["blockers"] == []
    assert preview["expected_head"] == _git(root, "rev-parse", "HEAD").strip()
    _write(root, "ontology/object/item.md", "v3")
    _commit(root, "web: v3")
    stale = client.post(f"{base}/revert", json={"commit": target, "expected_head": preview["expected_head"]})
    assert stale.status_code == 409 and stale.json()["code"] == "head_moved"

    studio.save_draft(pid, "ontology/object/item.md", "draft", "rev", "operator_a")
    blocked = client.get(f"{base}/revert-preview", params={"commit": target}).json()
    assert blocked["ready"] is False and blocked["blockers"][0]["code"] == "studio_draft_pending"
    refused = client.post(f"{base}/revert", json={"commit": target})
    assert refused.status_code == 409 and refused.json()["code"] == "studio_draft_pending"
    studio.discard_draft(pid, "ontology/object/item.md", "operator_a")

    fresh = client.get(f"{base}/revert-preview", params={"commit": target}).json()
    done = client.post(f"{base}/revert", json={"commit": target, "expected_head": fresh["expected_head"]})
    assert done.status_code == 200 and done.json()["version_label"] == fresh["next_version_label"]


def test_preview_rejects_commit_outside_mainline_history(api):
    client, pid, root = api
    branch = _git(root, "rev-parse", "--abbrev-ref", "HEAD").strip()
    _git(root, "checkout", "-q", "--orphan", "side")
    _write(root, "side.md", "side")
    side = _commit(root, "side")
    _git(root, "checkout", "-q", "-f", branch)
    response = client.get(f"/api/v1/projects/{pid}/versions/revert-preview", params={"commit": side})
    assert response.status_code == 400 and response.json()["code"] == "invalid_commit"


def test_version_list_marks_releases_and_survives_release_errors(api, monkeypatch):
    from app.services import publish

    client, pid, root = api
    _write(root, "ontology/object/item.md", "v1")
    first = _commit(root, "web: v1")
    _write(root, "ontology/object/item.md", "v2")
    head = _commit(root, "web: v2")
    later = int(_git(root, "log", "-1", "--format=%ct", head).strip()) + 100
    # 没带提交的记录（别处发布的）按发布时间归到当时的最新版本
    rows = [{"version": "0.1.1", "commit": None, "released_at": later},
            {"version": "0.1.0", "commit": first, "released_at": later - 50}]
    monkeypatch.setattr(publish, "releases", lambda _manager, _pid: rows)
    listed = client.get(f"/api/v1/projects/{pid}/versions").json()
    marks = {item["commit"]: item["release"] for item in listed["items"]}
    assert marks[head] == "0.1.1" and marks[first] == "0.1.0" and listed["release_error"] == ""
    assert client.get(f"/api/v1/projects/{pid}/versions/{first}").json()["release"] == "0.1.0"

    def unavailable(_manager, _pid):
        raise WorkspaceError("知识包仓库暂时不可用", 503, "upstream_unavailable")

    monkeypatch.setattr(publish, "releases", unavailable)
    degraded = client.get(f"/api/v1/projects/{pid}/versions").json()
    assert len(degraded["items"]) == len(listed["items"])
    assert degraded["release_error"] == "知识包仓库暂时不可用"


# ---- MCP：列版本、预览、确认回退 ----


def test_mcp_preview_is_bound_and_replayable(manager, project, idle):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    _commit(root, "v1")
    _write(root, "ontology/object/later.md", "later")
    _commit(root, "v2")
    listed = version_revert.list_versions(manager, project_id, limit=1)
    current = listed["current_version"]
    assert listed["items"][0]["version"] == current and listed["next_cursor"]
    second = version_revert.list_versions(manager, project_id, limit=1, cursor=listed["next_cursor"])
    target = second["items"][0]["version"]

    preview = version_revert.preview_by_version(manager, project_id, target, "token-a", "operator_a")
    assert preview["ready"] is True and preview["changes"]["deleted"] == ["ontology/object/later.md"]
    with pytest.raises(McpError) as caught:
        asyncio.run(version_revert.apply_preview(manager, project_id, preview["preview_id"], "token-b",
                                                 "operator_a"))
    assert caught.value.code == "REVERT_PREVIEW_FORBIDDEN"

    done = asyncio.run(version_revert.apply_preview(manager, project_id, preview["preview_id"], "token-a",
                                                    "operator_a"))
    assert done["reverted_to_version"] == target and done["created_version"] == current + 1
    again = asyncio.run(version_revert.apply_preview(manager, project_id, preview["preview_id"], "token-a",
                                                     "operator_a"))
    assert again == done and manager.versions(project_id)[0]["n"] == current + 1
    with pytest.raises(McpError) as caught:
        version_revert.list_versions(manager, project_id, limit=1, cursor=listed["next_cursor"])
    assert caught.value.code == "CURSOR_REVISION_MISMATCH"


def test_mcp_preview_lists_blockers_and_stale_preview_is_refused(manager, project, idle):
    project_id, root = project
    _write(root, "ontology/object/item.md", "v1")
    target = int(manager.versions(project_id)[0]["n"]) + 1
    _commit(root, "v1")
    _write(root, "ontology/object/item.md", "v2")
    _commit(root, "v2")
    with pytest.raises(McpError) as caught:
        version_revert.preview_by_version(manager, project_id, 999, "token-a", "operator_a")
    assert caught.value.code == "VERSION_NOT_FOUND"

    studio.save_draft(project_id, "ontology/object/item.md", "draft", "rev", "operator_a")
    blocked = version_revert.preview_by_version(manager, project_id, target, "token-a", "operator_a")
    assert blocked["ready"] is False and blocked["blockers"][0]["code"] == "PROJECT_EDITING_BUSY"
    assert "preview_id" not in blocked
    studio.discard_draft(project_id, "ontology/object/item.md", "operator_a")

    preview = version_revert.preview_by_version(manager, project_id, target, "token-a", "operator_a")
    _write(root, "ontology/object/item.md", "v3")
    _commit(root, "v3")
    with pytest.raises(McpError) as caught:
        asyncio.run(version_revert.apply_preview(manager, project_id, preview["preview_id"], "token-a",
                                                 "operator_a"))
    assert caught.value.code == "REVISION_CONFLICT"
    assert json.loads(json.dumps(caught.value.public()))["retryable"] is True
