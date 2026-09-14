"""Tests for chat session persistence."""

import hashlib
import json
from operator import getitem
from pathlib import Path

import persistence.chat_store as chat_store_module
import persistence.session_summary_index as summary_index_module
import pytest
from persistence.chat_store import ChatStore, ToolOutputLimits
from persistence.session_summary_index import (
    SessionSummaryIndex,
    SessionSummaryIndexWriteError,
)
from utils.streaming import build_agent_event


def _only_summary(index: SessionSummaryIndex):
    page = index.list_page(limit=10)
    assert len(page.items) == 1
    return page.items[0]


def test_chat_store_projects_durable_summary_mutations_but_not_stream_tokens(tmp_path):
    index = SessionSummaryIndex(tmp_path / "session-summary-index.json")
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)

    created = store.create_session(
        session_id="summary-chat",
        title="Initial",
        metadata={"selected_ontology_id": "synthetic-ontology"},
    )
    summary = _only_summary(index)
    assert summary.session_id == created.id
    assert summary.message_count == 0

    assert store.add_message(created.id, "user", "synthetic request") is not None
    assert store.update_session_title(created.id, "Updated") is not None
    assert store.merge_session_metadata(created.id, {"agent_mode": "quick"}) is not None
    assert store.set_session_pinned(created.id, True) is not None
    assert (
        store.record_interaction_cancellation(
            created.id,
            "interaction-summary",
            "Synthetic interaction cancelled",
            cancelled_at="2026-08-31T12:00:00+00:00",
        )
        is True
    )
    assert store.set_active_run(created.id, "run-summary") is True

    summary = _only_summary(index)
    assert summary.title == "Updated"
    assert summary.message_count == 2
    assert summary.metadata["agent_mode"] == "quick"
    assert summary.pinned is True
    assert summary.active_run_id == "run-summary"

    revision = index.index_revision
    token = build_agent_event(
        event_type="token.delta",
        run_id="run-summary",
        session_id=created.id,
        turn_id=1,
        payload={"channel": "final", "text": "synthetic"},
    )
    assert store.append_event(created.id, token) is True
    assert index.index_revision == revision

    assert store.clear_active_run(created.id) is True
    assert store.compact_session(created.id) is True
    assert _only_summary(index).active_run_id is None
    assert store.delete_session(created.id) is True
    assert index.list_page(limit=10).items == ()


def test_get_session_metadata_uses_ready_summary_without_full_session_read(
    tmp_path,
    monkeypatch,
):
    index = SessionSummaryIndex(tmp_path / "session-summary-index.json")
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)
    store.create_session(
        session_id="metadata-ready",
        metadata={"selected_ontology_id": "ontology-ready", "nested": {"v": 1}},
    )
    monkeypatch.setattr(
        store,
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ready metadata lookup must not load a full session")
        ),
    )
    monkeypatch.setattr(
        store.source_store_for_test(),
        "get_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("ready metadata lookup must not replay chat events")
        ),
    )

    metadata = store.get_session_metadata("metadata-ready")

    assert metadata["selected_ontology_id"] == "ontology-ready"
    metadata["nested"]["v"] = 2
    assert index.get_summary("metadata-ready").to_dict()["metadata"]["nested"]["v"] == 1


def test_get_session_metadata_cold_index_schedules_rebuild_and_reads_snapshot_only(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "chats"
    writer = ChatStore(str(storage_dir))
    writer.create_session(
        session_id="metadata-cold",
        metadata={"selected_ontology_id": "ontology-cold"},
    )
    scheduled = []
    index = SessionSummaryIndex(
        tmp_path / "missing-session-summary-index.json",
        rebuild_loader=lambda: (),
        rebuild_scheduler=lambda: scheduled.append(True) or True,
    )
    reader = ChatStore(str(storage_dir), summary_index=index)
    monkeypatch.setattr(
        reader.source_store_for_test(),
        "_replay_event_log_unlocked",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("cold metadata fallback must not replay chat events")
        ),
    )

    metadata = reader.get_session_metadata("metadata-cold")

    assert metadata["selected_ontology_id"] == "ontology-cold"
    assert scheduled == [True]


def test_chat_store_summary_failure_does_not_rollback_durable_write(tmp_path):
    class FailingIndex:
        def __init__(self):
            self.degraded_reason = None

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def upsert_chat(self, _summary, **_options):
            raise OSError("synthetic index failure")

        def mark_degraded(self, reason):
            self.degraded_reason = reason

    index = FailingIndex()
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)

    created = store.create_session(session_id="summary-write-survives")

    assert store.get_session(created.id) is not None
    assert index.degraded_reason == "chat_projection_failed"


def test_chat_projection_crash_marker_repairs_valid_index_after_restart(tmp_path, monkeypatch):
    holder = {}
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: getitem(holder, "store").rebuild_session_summaries(),
    )
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)
    holder["store"] = store
    store.create_session(session_id="before-crash", title="Before")

    monkeypatch.setattr(
        index,
        "upsert_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic process exit before projection")),
    )
    store.create_session(session_id="after-fact", title="After fact")
    assert store.get_session("after-fact") is not None

    restarted = SessionSummaryIndex(
        path,
        rebuild_loader=store.rebuild_session_summaries,
    )
    assert {item.session_id for item in restarted.list_all()} == {
        "before-crash",
        "after-fact",
    }


def test_chat_projection_failure_rebuilds_in_same_process(tmp_path, monkeypatch):
    holder = {}
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: getitem(holder, "store").rebuild_session_summaries(),
    )
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)
    holder["store"] = store
    store.create_session(session_id="before-failure", title="Before")

    monkeypatch.setattr(
        index,
        "upsert_chat",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("synthetic projection failure")),
    )
    store.create_session(session_id="after-fact", title="After")

    assert index.dirty_marker_exists_for_test() is True
    assert {item.session_id for item in index.list_all()} == {
        "before-failure",
        "after-fact",
    }
    assert index.dirty_marker_exists_for_test() is False


def test_chat_source_failure_abandons_projection_for_same_process_rebuild(
    tmp_path,
    monkeypatch,
):
    holder = {}
    path = tmp_path / "session-summary-index.json"
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: getitem(holder, "store").rebuild_session_summaries(),
    )
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)
    holder["store"] = store
    store.create_session(session_id="stable", title="Stable")
    monkeypatch.setattr(
        store.source_store_for_test(),
        "create_session",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("synthetic source failure")),
    )

    with pytest.raises(OSError, match="source failure"):
        store.create_session(session_id="failed", title="Failed")

    assert [item.session_id for item in index.list_all()] == ["stable"]
    assert index.dirty_marker_exists_for_test() is False


def test_marker_publish_failure_does_not_abort_source_fact_before_restart(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session-summary-index.json"
    holder = {}
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: getitem(holder, "store").rebuild_session_summaries(),
    )
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)
    holder["store"] = store
    store.create_session(session_id="stable", title="Stable")
    original_write = summary_index_module.write_atomic_json

    def fail_dirty_marker(target, document):
        if str(target).endswith(".dirty"):
            raise SessionSummaryIndexWriteError(
                "synthetic marker failure",
                committed=False,
            )
        return original_write(target, document)

    monkeypatch.setattr(summary_index_module, "write_atomic_json", fail_dirty_marker)

    store.create_session(session_id="must-commit", title="Committed")
    assert store.get_session("must-commit") is not None

    restarted = SessionSummaryIndex(path, rebuild_loader=store.rebuild_session_summaries)
    assert {item.session_id for item in restarted.list_all()} == {
        "stable",
        "must-commit",
    }


def test_marker_directory_fsync_failure_does_not_abort_source_fact(
    tmp_path,
    monkeypatch,
):
    path = tmp_path / "session-summary-index.json"
    holder = {}
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: getitem(holder, "store").rebuild_session_summaries(),
    )
    store = ChatStore(str(tmp_path / "chats"), summary_index=index)
    holder["store"] = store
    index.rebuild()
    store.create_session(session_id="stable", title="Stable")
    original_write = summary_index_module.write_atomic_json

    def fail_after_marker_replace(target, document):
        if str(target).endswith(".dirty"):
            original_write(target, document)
            raise SessionSummaryIndexWriteError(
                "synthetic marker directory fsync failure",
                committed=True,
            )
        return original_write(target, document)

    monkeypatch.setattr(
        summary_index_module,
        "write_atomic_json",
        fail_after_marker_replace,
    )

    store.create_session(session_id="must-commit", title="Committed")
    assert store.get_session("must-commit") is not None

    restarted = SessionSummaryIndex(path, rebuild_loader=store.rebuild_session_summaries)
    assert {item.session_id for item in restarted.list_all()} == {
        "stable",
        "must-commit",
    }


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


def test_chat_store_append_events_once_recovers_partial_terminal_batch(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(session_id="terminal-recovery")
    source = {"node": "deterministic_execution", "component": "service"}
    events = [
        build_agent_event(
            run_id="run-recovery",
            session_id=created.id,
            turn_id=1,
            event_type="tool.finished",
            source=source,
            payload={"status": "completed"},
        ),
        build_agent_event(
            run_id="run-recovery",
            session_id=created.id,
            turn_id=1,
            event_type="turn.finished",
            source=source,
            payload={"status": "success"},
        ),
    ]

    assert store.append_events(created.id, events[:1]) is True
    assert store.append_events_once(created.id, events) is True
    assert store.append_events_once(created.id, events) is True

    loaded = store.get_session(created.id)
    assert loaded is not None
    recovered_ids = [event["data"]["event_id"] for event in loaded.events]
    assert recovered_ids == [event["data"]["event_id"] for event in events]


def test_chat_store_sync_event_log_is_explicit_and_syncs_file_and_directory(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(session_id="durable-terminal")
    event = build_agent_event(
        run_id="run-durable-terminal",
        session_id=created.id,
        turn_id=1,
        event_type="turn.finished",
        source={"node": "deterministic_execution", "component": "service"},
        payload={"status": "success"},
    )
    file_syncs = []
    directory_syncs = []
    monkeypatch.setattr(
        chat_store_module.os,
        "fsync",
        lambda descriptor: file_syncs.append(descriptor),
    )
    monkeypatch.setattr(
        chat_store_module,
        "_fsync_directory",
        lambda path: directory_syncs.append(path),
    )

    assert store.append_events(created.id, [event]) is True
    assert file_syncs == []
    assert directory_syncs == []

    assert store.sync_event_log(created.id) is True
    assert len(file_syncs) == 1
    assert directory_syncs == [tmp_path / "chats" / created.id]


def test_chat_store_append_events_once_repairs_interrupted_jsonl_tail(tmp_path):
    storage_dir = tmp_path / "chats"
    store = ChatStore(str(storage_dir))
    created = store.create_session(session_id="interrupted-terminal")
    event = build_agent_event(
        run_id="run-interrupted",
        session_id=created.id,
        turn_id=1,
        event_type="turn.finished",
        source={"node": "deterministic_execution", "component": "service"},
        payload={"status": "success"},
    )
    event_log_path = storage_dir / created.id / "events.jsonl"
    event_log_path.write_bytes(b'{"event":"agent.v2","data":')

    assert store.append_events_once(created.id, [event]) is True

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert [item["data"]["event_id"] for item in loaded.events] == [event["data"]["event_id"]]
    lines = event_log_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0])["data"]["event_id"] == event["data"]["event_id"]


def test_chat_store_preserves_complete_final_jsonl_line_without_newline(tmp_path):
    storage_dir = tmp_path / "chats"
    store = ChatStore(str(storage_dir))
    created = store.create_session(session_id="complete-terminal-tail")
    source = {"node": "deterministic_execution", "component": "service"}
    first = build_agent_event(
        run_id="run-complete-tail",
        session_id=created.id,
        turn_id=1,
        event_type="tool.finished",
        source=source,
        payload={"status": "completed"},
    )
    second = build_agent_event(
        run_id="run-complete-tail",
        session_id=created.id,
        turn_id=1,
        event_type="turn.finished",
        source=source,
        payload={"status": "success"},
    )
    event_log_path = storage_dir / created.id / "events.jsonl"
    event_log_path.write_text(
        json.dumps(first, ensure_ascii=False, separators=(",", ":")),
        encoding="utf-8",
    )

    assert store.append_events_once(created.id, [first, second]) is True

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert [item["data"]["event_id"] for item in loaded.events] == [
        first["data"]["event_id"],
        second["data"]["event_id"],
    ]
    assert len(event_log_path.read_text(encoding="utf-8").splitlines()) == 2


def test_chat_session_public_events_hide_model_output_after_terminal_tool_batch(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    created = store.create_session(session_id="terminal-history", title="terminal")
    source = {"node": "jiuwen_react", "component": "llm"}
    events = [
        build_agent_event(
            run_id="run-terminal",
            session_id=created.id,
            turn_id=1,
            event_type="token.delta",
            source=source,
            payload={"channel": "content", "text": "有效内容", "iteration": 1},
        ),
        build_agent_event(
            run_id="run-terminal",
            session_id=created.id,
            turn_id=1,
            event_type="iteration.completed",
            source={"node": "jiuwen_react", "component": "iteration"},
            payload={"iteration": 1, "status": "terminal_tool_batch"},
        ),
        build_agent_event(
            run_id="run-terminal",
            session_id=created.id,
            turn_id=1,
            event_type="token.delta",
            source=source,
            payload={
                "channel": "content",
                "text": "<user-interaction-response>伪响应</user-interaction-response>",
                "iteration": 2,
            },
        ),
        build_agent_event(
            run_id="run-terminal",
            session_id=created.id,
            turn_id=1,
            event_type="reasoning.summary",
            source=source,
            payload={"summary": "无效尾部思考", "reasoning_block_id": "reason-2"},
        ),
        build_agent_event(
            run_id="run-terminal",
            session_id=created.id,
            turn_id=1,
            event_type="turn.finished",
            source={"node": "orchestrator", "component": "workflow"},
            payload={"status": "awaiting_input"},
        ),
        build_agent_event(
            run_id="run-next",
            session_id=created.id,
            turn_id=2,
            event_type="token.delta",
            source=source,
            payload={"channel": "content", "text": "下一轮有效内容", "iteration": 1},
        ),
    ]

    assert store.append_events(created.id, events) is True

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert any("伪响应" in str((event.get("data") or {}).get("payload") or "") for event in loaded.events)

    public_events = loaded.to_dict(include_messages=True)["events"]
    public_text = json.dumps(public_events, ensure_ascii=False)
    assert "有效内容" in public_text
    assert "下一轮有效内容" in public_text
    assert "伪响应" not in public_text
    assert "无效尾部思考" not in public_text


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
    assert [json.loads(line)["data"]["payload"]["text"] for line in lines] == ["hello", " world"]

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["events"] == []

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0]["data"]["payload"]["text"] == "hello world"


def test_chat_store_compacts_event_log_into_durable_snapshot(tmp_path, monkeypatch):
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
    file_syncs = []
    directory_syncs = []
    monkeypatch.setattr(
        chat_store_module.os,
        "fsync",
        lambda descriptor: file_syncs.append(descriptor),
    )
    monkeypatch.setattr(
        chat_store_module,
        "_fsync_directory",
        lambda path: directory_syncs.append(path),
    )
    assert store.compact_session(created.id) is True

    snapshot_path = tmp_path / "chats" / "compact" / "snapshot.json"
    event_log_path = tmp_path / "chats" / "compact" / "events.jsonl"

    assert event_log_path.read_text(encoding="utf-8") == ""

    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    assert snapshot["event_log_generation"] == 1
    assert len(snapshot["events"]) == 1
    assert snapshot["events"][0]["data"]["payload"]["text"] == "hello world"
    assert len(file_syncs) == 2  # snapshot temp file, then truncated event log
    assert directory_syncs == [tmp_path / "chats" / created.id]

    loaded = store.get_session(created.id)
    assert loaded is not None
    assert len(loaded.events) == 1
    assert loaded.events[0]["data"]["payload"]["text"] == "hello world"


def test_chat_store_recovery_skips_pre_compaction_generation_after_truncate_failure(
    tmp_path,
    monkeypatch,
):
    storage_dir = tmp_path / "chats"
    store = ChatStore(str(storage_dir))
    created = store.create_session(session_id="compact-crash", title="stream")
    first = build_agent_event(
        run_id="run-compact-crash",
        session_id=created.id,
        turn_id=1,
        event_type="artifact.produced",
        source={"node": "deterministic_execution", "component": "service"},
        payload={"artifact": {"name": "result.json"}},
    )
    assert store.append_events(created.id, [first]) is True

    def fail_before_truncate(_session_id):
        raise RuntimeError("injected failure before event-log truncate")

    monkeypatch.setattr(
        store.source_store_for_test(),
        "_truncate_event_log_unlocked",
        fail_before_truncate,
    )
    with pytest.raises(RuntimeError, match="injected failure"):
        store.compact_session(created.id)

    snapshot_path = storage_dir / created.id / "snapshot.json"
    event_log_path = storage_dir / created.id / "events.jsonl"
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    stale_lines = event_log_path.read_text(encoding="utf-8").splitlines()
    assert snapshot["event_log_generation"] == 1
    assert len(stale_lines) == 1
    assert json.loads(stale_lines[0])["_chat_store_generation"] == 0

    recovered_store = ChatStore(str(storage_dir))
    recovered = recovered_store.get_session(created.id)
    assert recovered is not None
    assert [event["data"]["event_id"] for event in recovered.events] == [first["data"]["event_id"]]

    second = build_agent_event(
        run_id="run-compact-crash",
        session_id=created.id,
        turn_id=1,
        event_type="turn.finished",
        source={"node": "deterministic_execution", "component": "service"},
        payload={"status": "success"},
    )
    assert recovered_store.append_events(created.id, [second]) is True
    generations = [
        json.loads(line)["_chat_store_generation"] for line in event_log_path.read_text(encoding="utf-8").splitlines()
    ]
    assert generations == [0, 1]

    reloaded = ChatStore(str(storage_dir)).get_session(created.id)
    assert reloaded is not None
    assert [event["data"]["event_id"] for event in reloaded.events] == [
        first["data"]["event_id"],
        second["data"]["event_id"],
    ]


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
    assert artifact["sha256"] == ("sha256:" + hashlib.sha256(complete_output.encode("utf-8")).hexdigest())
    artifact_root = tmp_path / "chats" / created.id / "tool-outputs"
    metadata_files = list(artifact_root.glob("*/*.json"))
    content_files = list(artifact_root.glob("*/*.txt"))
    assert len(metadata_files) == 1
    assert len(content_files) == 1
    assert "output" not in json.loads(metadata_files[0].read_text(encoding="utf-8"))
    metadata = json.loads(metadata_files[0].read_text(encoding="utf-8"))
    assert metadata["version"] == 3
    assert metadata["sha256"] == artifact["sha256"]
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
    assert (
        store.get_tool_output(
            created.id,
            "another-run",
            artifact["output_ref"],
        )
        is None
    )


def test_chat_store_preserves_explicit_artifact_content_type(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="typed-artifact")

    artifact = store.store_tool_output(
        session.id,
        "run-typed",
        "deterministic:result.json",
        '{"value":"ok"}',
        content_type="application/json",
    )
    streamed = store.get_tool_output_file(
        session.id,
        "run-typed",
        artifact["output_ref"],
    )

    assert artifact["content_type"] == "application/json"
    assert streamed["response_content_type"] == "application/json"


def test_chat_store_tool_output_syncs_payload_metadata_and_directory(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="durable-artifact")
    file_syncs = []
    directory_syncs = []
    monkeypatch.setattr(
        chat_store_module.os,
        "fsync",
        lambda descriptor: file_syncs.append(descriptor),
    )
    monkeypatch.setattr(
        chat_store_module,
        "_fsync_directory",
        lambda path: directory_syncs.append(path),
    )

    artifact = store.store_tool_output(
        session.id,
        "run-durable-artifact",
        "deterministic:result.json",
        '{"value":"ok"}',
        content_type="application/json",
    )

    assert artifact is not None
    assert len(file_syncs) == 2
    run_key = hashlib.sha256(b"run-durable-artifact").hexdigest()
    assert directory_syncs == [
        tmp_path / "chats" / session.id / "tool-outputs" / run_key,
        tmp_path / "chats" / session.id / "tool-outputs",
        tmp_path / "chats" / session.id,
    ]


def test_chat_store_reads_legacy_inline_tool_output_artifact(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="legacy-artifact")
    output_ref = "a" * 32
    run_id = "legacy-run"
    run_key = hashlib.sha256(run_id.encode("utf-8")).hexdigest()
    artifact_path = tmp_path / "chats" / session.id / "tool-outputs" / run_key / "{}.json".format(output_ref)
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "version": 1,
                "output_ref": output_ref,
                "session_id": session.id,
                "run_id": run_id,
                "tool_call_id": "legacy-call",
                "content_type": "text/plain; charset=utf-8",
                "output_size": 6,
                "output": "legacy",
            }
        ),
        encoding="utf-8",
    )

    loaded = store.get_tool_output(session.id, run_id, output_ref)
    streamed = store.get_tool_output_file(session.id, run_id, output_ref)

    assert loaded is not None
    assert loaded["output"] == "legacy"
    assert streamed is not None
    assert streamed["artifact_path"] == str(artifact_path)
    assert streamed["response_content_type"].startswith("application/json")


def test_chat_store_can_rollback_scoped_tool_output(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="rollback-artifact")
    artifact = store.store_tool_output(
        session.id,
        "run-rollback",
        "deterministic:result.json",
        '{"value":"ok"}',
        content_type="application/json",
    )
    assert artifact is not None

    assert (
        store.delete_tool_output(
            session.id,
            "run-rollback",
            artifact["output_ref"],
        )
        is True
    )
    assert (
        store.get_tool_output(
            session.id,
            "run-rollback",
            artifact["output_ref"],
        )
        is None
    )


def test_chat_store_rejects_same_size_tool_output_corruption(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="corrupt-artifact")
    artifact = store.store_tool_output(
        session.id,
        "run-corrupt",
        "deterministic:result.txt",
        "abc",
    )
    assert artifact is not None
    content_path = next((tmp_path / "chats" / session.id / "tool-outputs").glob("*/*.txt"))
    content_path.write_text("xyz", encoding="utf-8")

    assert (
        store.get_tool_output(
            session.id,
            "run-corrupt",
            artifact["output_ref"],
        )
        is None
    )
    assert (
        store.get_tool_output_file(
            session.id,
            "run-corrupt",
            artifact["output_ref"],
        )
        is None
    )


def test_chat_store_tool_output_rollback_can_retry_payload_delete_failure(
    tmp_path,
    monkeypatch,
):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(session_id="retry-rollback-artifact")
    artifact = store.store_tool_output(
        session.id,
        "run-retry-rollback",
        "deterministic:result.json",
        '{"value":"ok"}',
        content_type="application/json",
    )
    assert artifact is not None
    artifact_root = tmp_path / "chats" / session.id / "tool-outputs"
    metadata_path = next(artifact_root.glob("*/*.json"))
    content_path = next(artifact_root.glob("*/*.txt"))
    original_unlink = Path.unlink
    failed_once = False

    def flaky_unlink(path, *args, **kwargs):
        nonlocal failed_once
        if path == content_path and not failed_once:
            failed_once = True
            raise OSError("transient payload delete failure")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)

    assert (
        store.delete_tool_output(
            session.id,
            "run-retry-rollback",
            artifact["output_ref"],
        )
        is False
    )
    assert content_path.exists()
    assert metadata_path.exists()

    assert (
        store.delete_tool_output(
            session.id,
            "run-retry-rollback",
            artifact["output_ref"],
        )
        is True
    )
    assert not content_path.exists()
    assert not metadata_path.exists()


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


def test_default_tool_output_quota_accepts_deterministic_artifact_limit():
    assert ToolOutputLimits().max_output_bytes == 10 * 1024 * 1024


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


def test_chat_store_tool_output_follows_session_deletion_lifecycle(tmp_path):
    storage_dir = tmp_path / "chats"
    store = ChatStore(str(storage_dir))
    first = store.create_session(session_id="first-session")
    second = store.create_session(session_id="second-session")
    first_artifact = store.store_tool_output(first.id, "run-1", "call-1", "first")
    second_artifact = store.store_tool_output(second.id, "run-2", "call-2", "second")
    assert first_artifact is not None
    assert second_artifact is not None

    assert store.delete_session(first.id) is True
    assert not (storage_dir / first.id).exists()
    assert store.get_tool_output(first.id, "run-1", first_artifact["output_ref"]) is None
    assert store.get_tool_output(second.id, "run-2", second_artifact["output_ref"]) is not None

    assert store.delete_session(second.id) is True
    assert not (storage_dir / second.id).exists()
    assert store.get_tool_output(second.id, "run-2", second_artifact["output_ref"]) is None


def test_chat_store_reserved_output_ref_has_idempotent_recovery_delete(tmp_path):
    store = ChatStore(str(tmp_path / "chats"))
    session = store.create_session(title="reserved-ref")
    output_ref = "c" * 32

    artifact = store.store_tool_output(
        session.id,
        "run-reserved",
        "call-reserved",
        "payload",
        output_ref=output_ref,
    )

    assert artifact["output_ref"] == output_ref
    assert (
        store.delete_tool_output(
            session.id,
            "run-reserved",
            output_ref,
            missing_ok=True,
        )
        is True
    )
    assert (
        store.delete_tool_output(
            session.id,
            "run-reserved",
            output_ref,
            missing_ok=True,
        )
        is True
    )


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
