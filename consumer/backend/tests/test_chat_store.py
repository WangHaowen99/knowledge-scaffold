"""Tests for chat session persistence."""

import json

from persistence.chat_store import ChatStore
from utils.streaming import build_agent_event


def test_chat_store_persists_session_metadata(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))

    created = store.create_session(
        title="新对话",
        metadata={"llm_request_mode": "sync"},
    )

    loaded = store.get_session(created.id)
    sessions = store.list_sessions()

    assert created.metadata["llm_request_mode"] == "sync"
    assert loaded is not None
    assert loaded.metadata["llm_request_mode"] == "sync"
    assert sessions[0].metadata["llm_request_mode"] == "sync"


def test_chat_store_merge_session_metadata_preserves_existing_keys(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(
        title="新对话",
        metadata={"llm_request_mode": "sync"},
    )

    updated = store.merge_session_metadata(
        created.id,
        {
            "ov_session_id": "ov-123",
            "ov_memory_version": "openviking-v1",
        },
    )

    assert updated is not None
    assert updated.metadata["llm_request_mode"] == "sync"
    assert updated.metadata["ov_session_id"] == "ov-123"
    assert updated.metadata["ov_memory_version"] == "openviking-v1"


def test_chat_store_append_events_batches_and_merges_token_deltas(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(title="stream")

    first = build_agent_event(
        run_id="run-batch",
        session_id=created.id,
        turn_id=1,
        event_type="token.delta",
        source={"node": "agent_loop", "component": "llm"},
        payload={"channel": "content", "text": "hello", "iteration": 1},
    )
    second = build_agent_event(
        run_id="run-batch",
        session_id=created.id,
        turn_id=1,
        event_type="token.delta",
        source={"node": "agent_loop", "component": "llm"},
        payload={"channel": "content", "text": " world", "iteration": 1},
    )

    assert store.append_events(created.id, [first, second]) is True

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert len(loaded.events) == 1
    event = loaded.events[0]
    assert event["data"]["type"] == "token.delta"
    assert event["data"]["payload"]["text"] == "hello world"
    assert event["data"]["seq"] == second["data"]["seq"]


def test_chat_store_writes_stream_events_to_append_only_log(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(session_id="append-only", title="stream")

    snapshot_path = tmp_path / "chats" / "append-only" / "snapshot.json"
    event_log_path = tmp_path / "chats" / "append-only" / "events.jsonl"

    assert snapshot_path.exists()
    assert event_log_path.exists()
    assert not (tmp_path / "chats" / "append-only.json").exists()

    first = build_agent_event(
        run_id="run-log",
        session_id=created.id,
        turn_id=1,
        event_type="token.delta",
        source={"node": "agent_loop", "component": "llm"},
        payload={"channel": "content", "text": "hello", "iteration": 1},
    )
    second = build_agent_event(
        run_id="run-log",
        session_id=created.id,
        turn_id=1,
        event_type="token.delta",
        source={"node": "agent_loop", "component": "llm"},
        payload={"channel": "content", "text": " world", "iteration": 1},
    )

    assert store.append_events(created.id, [first, second]) is True

    lines = event_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert [
        json.loads(line)["data"]["payload"]["text"]
        for line in lines
    ] == ["hello", " world"]

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["events"] == []

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0]["data"]["payload"]["text"] == "hello world"


def test_chat_store_compacts_event_log_into_snapshot(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(session_id="compact", title="stream")

    first = build_agent_event(
        run_id="run-compact",
        session_id=created.id,
        turn_id=1,
        event_type="token.delta",
        source={"node": "agent_loop", "component": "llm"},
        payload={"channel": "content", "text": "hello", "iteration": 1},
    )
    second = build_agent_event(
        run_id="run-compact",
        session_id=created.id,
        turn_id=1,
        event_type="token.delta",
        source={"node": "agent_loop", "component": "llm"},
        payload={"channel": "content", "text": " world", "iteration": 1},
    )

    assert store.append_events(created.id, [first, second]) is True
    assert store.compact_session(created.id) is True

    snapshot_path = tmp_path / "chats" / "compact" / "snapshot.json"
    event_log_path = tmp_path / "chats" / "compact" / "events.jsonl"

    assert event_log_path.read_text(encoding="utf-8") == ""

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert len(snapshot["events"]) == 1
    assert snapshot["events"][0]["data"]["payload"]["text"] == "hello world"

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0]["data"]["payload"]["text"] == "hello world"


def test_chat_store_migrates_legacy_json_session(tmp_path):
    storage_dir = tmp_path / "chats"
    storage_dir.mkdir()
    legacy_path = storage_dir / "legacy.json"
    legacy_path.write_text(
        json.dumps(
            {
                "id": "legacy",
                "title": "Legacy",
                "metadata": {"source": "old-store"},
                "events": [
                    {
                        "event": "chat_message",
                        "data": {"role": "user", "content": "old question"},
                        "ts": 1710000000000,
                    }
                ],
                "session_history": [],
                "created_at": "2024-03-09T16:00:00Z",
                "updated_at": "2024-03-09T16:01:00Z",
                "active_run_id": "run-old",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    store = ChatStore(str(storage_dir))
    loaded = store.get_session("legacy")

    assert loaded is not None
    assert loaded.title == "Legacy"
    assert loaded.metadata["source"] == "old-store"
    assert loaded.events[0]["data"]["content"] == "old question"

    assert (storage_dir / "legacy" / "snapshot.json").exists()
    assert (storage_dir / "legacy" / "events.jsonl").exists()
    assert not legacy_path.exists()
    assert (storage_dir / "legacy.json.migrated").exists()
