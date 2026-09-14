# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
import json

from integration.observability.speedscope_files import (
    find_speedscope_session_file,
    get_user_speedscope_session_path,
    remove_speedscope_session_files,
)
from utils.identifiers import resolve_session_storage_dir, safe_user_storage_key
from workspace.workspaces import create_workspace, get_workspace_paths


def _create_workspace_for_test(tmp_path, monkeypatch):
    monkeypatch.setenv("PROJECT_ROOT", str(tmp_path / ".onto-runtime"))
    create_workspace("Demo Workspace", workspace_id="demo", set_as_default=True)


def _write_chat_session(workspace_id, user_id, session_id):
    chat_root = get_workspace_paths(workspace_id).users_runtime_dir / safe_user_storage_key(user_id) / "chats"
    session_dir = resolve_session_storage_dir(chat_root, session_id)
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "snapshot.json").write_text(json.dumps({"id": session_id}), encoding="utf-8")


def test_admin_lookup_is_limited_to_existing_workspace_user_sessions(tmp_path, monkeypatch):
    _create_workspace_for_test(tmp_path, monkeypatch)
    _write_chat_session("demo", "alice@example.com", "sess-a")
    trace_path = get_user_speedscope_session_path("demo", "sess-a", "alice@example.com")
    trace_path.parent.mkdir(parents=True, exist_ok=True)
    trace_path.write_text("{}", encoding="utf-8")

    assert find_speedscope_session_file("demo", "sess-a", any_user=True) == trace_path
    assert find_speedscope_session_file("demo", "missing", any_user=True) is None


def test_deleting_user_session_trace_does_not_touch_another_user(tmp_path, monkeypatch):
    _create_workspace_for_test(tmp_path, monkeypatch)
    for user_id in ("alice", "bob"):
        trace_path = get_user_speedscope_session_path("demo", "sess-a", user_id)
        trace_path.parent.mkdir(parents=True, exist_ok=True)
        trace_path.write_text("{}", encoding="utf-8")

    assert remove_speedscope_session_files("demo", "sess-a", user_id="alice") == 1
    assert not get_user_speedscope_session_path("demo", "sess-a", "alice").exists()
    assert get_user_speedscope_session_path("demo", "sess-a", "bob").exists()
