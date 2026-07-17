"""Tests for chat session persistence."""

import hashlib
import json

from persistence.chat_store import ChatStore, ToolOutputLimits
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


def test_chat_store_persists_complete_tool_output_across_compaction(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(session_id="tool-output-session", title="trace")
    complete_output = "完整输出🙂" * 1200

    artifact = store.store_tool_output(
        created.id,
        "run:with:opaque:id",
        "call-1",
        complete_output,
    )

    assert artifact is not None
    assert artifact["output_size"] == len(complete_output.encode("utf-8"))
    artifact_root = tmp_path / "chats" / created.id / "tool-outputs"
    metadata_files = list(artifact_root.glob("*/*.json"))
    content_files = list(artifact_root.glob("*/*.txt"))
    assert len(metadata_files) == 1
    assert len(content_files) == 1
    assert "output" not in json.loads(metadata_files[0].read_text(encoding="utf-8"))
    loaded = store.get_tool_output(
        created.id,
        "run:with:opaque:id",
        artifact["output_ref"],
    )
    assert loaded is not None
    assert loaded["output"] == complete_output
    assert loaded["tool_call_id"] == "call-1"

    assert store.compact_session(created.id) is True
    retained = store.get_tool_output(
        created.id,
        "run:with:opaque:id",
        artifact["output_ref"],
    )
    assert retained is not None
    assert retained["output"] == complete_output
    reloaded_store = ChatStore(str(tmp_path / "chats"))
    reloaded = reloaded_store.get_tool_output(
        created.id,
        "run:with:opaque:id",
        artifact["output_ref"],
    )
    assert reloaded is not None
    assert reloaded["output"] == complete_output
    assert store.get_tool_output(
        created.id,
        "another-run",
        artifact["output_ref"],
    ) is None


def test_chat_store_reads_legacy_inline_tool_output_artifact(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="legacy-artifact")
    output_ref = "a" * 32
    run_id = "legacy-run"
    run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    artifact_path = (
        tmp_path
        / "chats"
        / session.id
        / "tool-outputs"
        / run_key
        / "{}.json".format(output_ref)
    )
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps({
            "version": 1,
            "output_ref": output_ref,
            "session_id": session.id,
            "run_id": run_id,
            "tool_call_id": "legacy-call",
            "content_type": "text/plain; charset=utf-8",
            "output_size": 6,
            "output": "legacy",
        }),
        encoding="utf-8",
    )

    loaded = store.get_tool_output(session.id, run_id, output_ref)
    streamed = store.get_tool_output_file(session.id, run_id, output_ref)

    assert loaded is not None
    assert loaded["output"] == "legacy"
    assert streamed is not None
    assert streamed["artifact_path"] == str(artifact_path)
    assert streamed["response_content_type"].startswith("application/json")


def test_chat_store_rejects_tool_output_above_single_output_quota(tmp_path):
    store = ChatStore(
        str(tmp_path / "chats"),
        tool_output_limits=ToolOutputLimits(
            max_output_bytes=5,
            max_run_bytes=1000,
            max_session_bytes=2000,
            max_run_count=10,
            max_session_count=20,
        ),
    )
    session = store.create_session(session_id="single-quota")

    assert store.store_tool_output(session.id, "run-1", "call-1", "123456") is None
    assert not (tmp_path / "chats" / session.id / "tool-outputs").exists()


def test_chat_store_enforces_run_byte_and_count_quotas(tmp_path):
    byte_limited = ChatStore(
        str(tmp_path / "byte-chats"),
        tool_output_limits=ToolOutputLimits(
            max_output_bytes=500,
            max_run_bytes=900,
            max_session_bytes=5000,
            max_run_count=10,
            max_session_count=20,
        ),
    )
    byte_session = byte_limited.create_session(session_id="run-byte-quota")
    assert byte_limited.store_tool_output(byte_session.id, "run-1", "call-1", "x" * 400)
    assert byte_limited.store_tool_output(byte_session.id, "run-1", "call-2", "x" * 400) is None

    count_limited = ChatStore(
        str(tmp_path / "count-chats"),
        tool_output_limits=ToolOutputLimits(
            max_output_bytes=500,
            max_run_bytes=5000,
            max_session_bytes=5000,
            max_run_count=1,
            max_session_count=20,
        ),
    )
    count_session = count_limited.create_session(session_id="run-count-quota")
    assert count_limited.store_tool_output(count_session.id, "run-1", "call-1", "one")
    assert count_limited.store_tool_output(count_session.id, "run-1", "call-2", "two") is None
    assert count_limited.store_tool_output(count_session.id, "run-2", "call-3", "three")


def test_chat_store_enforces_session_byte_and_count_quotas(tmp_path):
    byte_limited = ChatStore(
        str(tmp_path / "byte-chats"),
        tool_output_limits=ToolOutputLimits(
            max_output_bytes=500,
            max_run_bytes=5000,
            max_session_bytes=900,
            max_run_count=10,
            max_session_count=20,
        ),
    )
    byte_session = byte_limited.create_session(session_id="session-byte-quota")
    assert byte_limited.store_tool_output(byte_session.id, "run-1", "call-1", "x" * 400)
    assert byte_limited.store_tool_output(byte_session.id, "run-2", "call-2", "x" * 400) is None

    count_limited = ChatStore(
        str(tmp_path / "count-chats"),
        tool_output_limits=ToolOutputLimits(
            max_output_bytes=500,
            max_run_bytes=5000,
            max_session_bytes=5000,
            max_run_count=10,
            max_session_count=1,
        ),
    )
    count_session = count_limited.create_session(session_id="session-count-quota")
    assert count_limited.store_tool_output(count_session.id, "run-1", "call-1", "one")
    assert count_limited.store_tool_output(count_session.id, "run-2", "call-2", "two") is None


def test_chat_store_tool_output_follows_run_and_session_deletion_lifecycle(tmp_path):
    storage_dir = tmp_path / "chats"
    store = ChatStore(str(storage_dir))
    first = store.create_session(session_id="first-session")
    second = store.create_session(session_id="second-session")
    first_artifact = store.store_tool_output(first.id, "run-1", "call-1", "first")
    second_artifact = store.store_tool_output(second.id, "run-2", "call-2", "second")
    assert first_artifact is not None
    assert second_artifact is not None

    assert store.delete_tool_outputs_for_run(first.id, "run-1") is True
    assert store.get_tool_output(first.id, "run-1", first_artifact["output_ref"]) is None
    assert store.get_tool_output(second.id, "run-2", second_artifact["output_ref"]) is not None

    assert store.delete_session(second.id) is True
    assert not (storage_dir / second.id).exists()
    assert store.get_tool_output(second.id, "run-2", second_artifact["output_ref"]) is None


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
