"""Tests for filesystem-backed memory manager behavior."""

from pathlib import Path

import pytest
from agent.memory.filesystem_manager import FileSystemMemoryManager


def test_filesystem_manager_persists_notes_rebuilds_index_and_recalls_by_scope(tmp_path):
    manager = FileSystemMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "memory",
    )

    user_result = manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )
    profile_result = manager.store_memory(
        content="用户画像：用户是质量工程师，关注风险说明。",
        memory_type="profile",
        scope="user",
    )

    user_note = Path(user_result.source_uri)
    profile_note = Path(profile_result.source_uri)
    index_path = tmp_path / "memory" / "MEMORY.md"

    assert user_note.exists()
    assert profile_note.exists()
    assert index_path.exists()
    assert user_result.created_at
    assert user_result.updated_at
    assert user_result.to_dict()["created_at"] == user_result.created_at
    user_note_text = user_note.read_text(encoding="utf-8")
    assert "stored_at:" in user_note_text
    assert "created_at:" in user_note_text
    assert "updated_at:" in user_note_text

    index_text = index_path.read_text(encoding="utf-8")
    assert "默认使用中文回答" in index_text
    assert "质量工程师" in index_text

    profile_hits = manager.recall_memory(
        query="质量工程师",
        memory_type="profile",
        scope="user",
        k=5,
    )

    assert len(profile_hits) == 1
    assert profile_hits[0].memory_type == "profile"
    assert profile_hits[0].scope == "user"
    assert "质量工程师" in profile_hits[0].content
    assert profile_hits[0].created_at
    assert profile_hits[0].updated_at
    assert profile_hits[0].to_dict()["updated_at"] == profile_hits[0].updated_at

    context = manager.build_memory_context(
        query="What should the agent remember?",
        limit=3,
    )

    assert context.startswith("## Relevant Long-Term Memory")
    assert "默认使用中文回答" in context
    assert "质量工程师" in context


def test_filesystem_manager_preserves_created_at_on_merge(tmp_path):
    manager = FileSystemMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "memory",
    )

    first = manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )
    second = manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )

    assert second.merged is True
    assert second.created_at == first.created_at
    assert second.updated_at


def test_filesystem_manager_archives_memory_and_rebuilds_index(tmp_path):
    manager = FileSystemMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "memory",
    )

    result = manager.store_memory(
        content="以后使用英文回答计算处理成本的问题。",
        memory_type="preference",
        scope="user",
    )

    assert manager.archive_memory(source_uri=result.source_uri, reason="updated preference") is True

    index_text = (tmp_path / "memory" / "MEMORY.md").read_text(encoding="utf-8")
    assert "英文回答计算处理成本" not in index_text

    active_hits = manager.recall_memory(query="计算处理成本", memory_type="preference", k=5)
    assert active_hits == []

    archived_hits = manager.recall_memory(
        query="计算处理成本",
        memory_type="preference",
        k=5,
        include_archived=True,
    )
    assert len(archived_hits) == 1
    assert archived_hits[0].archived is True
    assert "英文回答" in archived_hits[0].content


def test_filesystem_manager_rejects_non_personalization_memory(tmp_path):
    manager = FileSystemMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "memory",
    )

    with pytest.raises(ValueError, match="Unsupported memory_type"):
        manager.store_memory(
            content="项目约束：不要修改现有 SSE 事件协议。",
            memory_type="constraint",
        )

    with pytest.raises(ValueError, match="Unsupported scope"):
        manager.store_memory(
            content="用户偏好：默认使用中文回答。",
            memory_type="preference",
            scope="project",
        )


def test_filesystem_manager_list_memories_filters_by_type_and_archived(tmp_path):
    manager = FileSystemMemoryManager(
        user_id="alice",
        workspace_dir=tmp_path / "memory",
    )

    manager.store_memory(
        content="用户偏好：默认使用中文回答。",
        memory_type="preference",
        scope="user",
    )
    manager.store_memory(
        content="用户画像：用户是质量工程师，关注风险说明。",
        memory_type="profile",
        scope="user",
    )

    all_hits = manager.list_memories(limit=10)
    assert len(all_hits) == 2
    assert {hit.memory_type for hit in all_hits} == {"preference", "profile"}

    profile_hits = manager.list_memories(memory_type="profile", limit=10)
    assert len(profile_hits) == 1
    assert profile_hits[0].memory_type == "profile"
    assert "质量工程师" in profile_hits[0].content

    preference_uri = next(hit.source_uri for hit in all_hits if hit.memory_type == "preference")
    assert manager.archive_memory(source_uri=preference_uri, reason="test") is True

    active_hits = manager.list_memories(limit=10)
    assert len(active_hits) == 1
    assert active_hits[0].memory_type == "profile"

    archived_hits = manager.list_memories(include_archived=True, limit=10)
    assert len(archived_hits) == 2
    assert any(hit.archived and hit.memory_type == "preference" for hit in archived_hits)
