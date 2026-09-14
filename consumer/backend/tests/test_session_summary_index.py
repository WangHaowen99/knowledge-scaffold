# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from internal_access import internal
import inspect
import json
import threading

import pytest
from persistence import session_summary_index as session_summary_index_module
from persistence.session_summary_index import (
    SCHEMA_VERSION,
    InvalidSessionCursor,
    SessionSummary,
    SessionSummaryIndex,
    SessionSummaryIndexCorruptionError,
    SessionSummaryIndexDirtyError,
    SessionSummaryIndexWriteError,
    session_summary_index_enabled,
)


def _capture_thread_error(errors, operation):
    try:
        operation()
    except BaseException as exc:
        errors.append(exc)


def _capture_thread_result(errors, results, operation):
    try:
        results.append(operation())
    except BaseException as exc:
        errors.append(exc)


def _summary(
    session_id: str,
    *,
    updated_at: str,
    pinned: bool = False,
    active_run_id: str | None = None,
    message_count: int = 0,
) -> SessionSummary:
    return SessionSummary.create(
        session_id=session_id,
        title=f"Session {session_id}",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at=updated_at,
        active_run_id=active_run_id,
        message_count=message_count,
        metadata={"pinned": pinned, "selected_ontology_id": "synthetic-ontology"},
    )


def test_index_orders_pages_by_pinned_updated_at_and_session_id_without_gaps(tmp_path):
    index = SessionSummaryIndex(tmp_path / "summary-index.json")
    for summary in (
        _summary("normal-old", updated_at="2026-08-31T01:00:00+00:00"),
        _summary("pinned-a", updated_at="2026-08-31T01:00:00+00:00", pinned=True),
        _summary("normal-new", updated_at="2026-08-31T02:00:00+00:00"),
        _summary("pinned-b", updated_at="2026-08-31T01:00:00+00:00", pinned=True),
    ):
        index.upsert(summary)

    first = index.list_page(limit=2)
    second = index.list_page(limit=2, cursor=first.next_cursor)

    assert [item.session_id for item in first.items] == ["pinned-b", "pinned-a"]
    assert [item.session_id for item in second.items] == ["normal-new", "normal-old"]
    assert first.has_more is True
    assert second.has_more is False
    assert second.next_cursor is None
    assert first.index_revision == second.index_revision


def test_cursor_is_opaque_strict_and_rejected_after_index_revision_changes(tmp_path):
    index = SessionSummaryIndex(tmp_path / "summary-index.json")
    index.upsert(_summary("one", updated_at="2026-08-31T01:00:00+00:00"))
    index.upsert(_summary("two", updated_at="2026-08-31T02:00:00+00:00"))
    cursor = index.list_page(limit=1).next_cursor

    assert cursor and "two" not in cursor
    with pytest.raises(InvalidSessionCursor, match="invalid"):
        index.list_page(limit=1, cursor="not-base64!")

    index.upsert(_summary("three", updated_at="2026-08-31T03:00:00+00:00"))
    with pytest.raises(InvalidSessionCursor, match="stale"):
        index.list_page(limit=1, cursor=cursor)


def test_cursor_decoder_catches_the_value_error_family_once():
    source = inspect.getsource(internal(session_summary_index_module).decode_cursor)

    assert "except ValueError as exc:" in source
    assert "except (UnicodeError, ValueError)" not in source


def test_turn_count_round_trips_and_get_summary_reads_one_record(tmp_path):
    path = tmp_path / "summary-index.json"
    index = SessionSummaryIndex(path)
    index.upsert(
        SessionSummary.create(
            session_id="turn-count",
            title="Turn count",
            created_at="2026-08-31T00:00:00+00:00",
            updated_at="2026-08-31T01:00:00+00:00",
            metadata={},
            turn_count=7,
        )
    )

    restarted = SessionSummaryIndex(path)
    summary = restarted.get_summary("turn-count")

    assert summary is not None
    assert summary.turn_count == 7
    document = json.loads(path.read_text(encoding="utf-8"))
    assert document["schema_version"] == SCHEMA_VERSION
    assert document["records"][0]["turn_count"] == 7


def test_hot_projection_appends_delta_without_rewriting_base_index(tmp_path):
    path = tmp_path / "summary-index.json"
    index = SessionSummaryIndex(path)
    index.upsert(_summary("hot", updated_at="2026-08-31T01:00:00+00:00"))
    base_bytes = path.read_bytes()

    index.upsert_chat(
        _summary(
            "hot",
            updated_at="2026-08-31T02:00:00+00:00",
            message_count=3,
        )
    )

    assert path.read_bytes() == base_bytes
    delta_path = path.with_name(f"{path.name}.delta")
    assert delta_path.is_file()

    restarted = SessionSummaryIndex(path)
    summary = restarted.get_summary("hot")
    assert summary is not None
    assert summary.updated_at == "2026-08-31T02:00:00+00:00"
    assert summary.message_count == 3


def test_hot_projection_compacts_delta_after_threshold(tmp_path, monkeypatch):
    import persistence.session_summary_index as index_module

    monkeypatch.setattr(index_module, "_DELTA_COMPACT_THRESHOLD", 2)
    path = tmp_path / "summary-index.json"
    index = SessionSummaryIndex(path)
    index.upsert(_summary("hot", updated_at="2026-08-31T01:00:00+00:00"))

    index.upsert_chat(
        _summary("hot", updated_at="2026-08-31T02:00:00+00:00", message_count=2)
    )
    index.upsert_chat(
        _summary("hot", updated_at="2026-08-31T03:00:00+00:00", message_count=3)
    )

    assert not path.with_name(f"{path.name}.delta").exists()
    assert json.loads(path.read_text(encoding="utf-8"))["index_revision"] == 3


def test_atomic_write_failure_preserves_last_published_file_and_snapshot(tmp_path, monkeypatch):
    from persistence import session_summary_index as index_module

    path = tmp_path / "summary-index.json"
    index = SessionSummaryIndex(path)
    index.upsert(_summary("stable", updated_at="2026-08-31T01:00:00+00:00"))
    published = path.read_bytes()

    monkeypatch.setattr(index_module.os, "replace", lambda *_args: (_ for _ in ()).throw(OSError("replace failed")))
    with pytest.raises(SessionSummaryIndexWriteError, match="publish") as caught:
        index.upsert(_summary("rejected", updated_at="2026-08-31T02:00:00+00:00"))

    assert caught.value.committed is False
    assert path.read_bytes() == published
    assert [item.session_id for item in index.list_page(limit=10).items] == ["stable"]
    assert index.state == "degraded"


def test_schema_mismatch_rebuilds_and_corrupt_json_is_quarantined(tmp_path):
    path = tmp_path / "summary-index.json"
    path.write_text(json.dumps({"schema_version": 0, "index_revision": 99, "records": []}), encoding="utf-8")
    rebuild_calls = []
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: rebuild_calls.append("schema")
        or [_summary("rebuilt", updated_at="2026-08-31T01:00:00+00:00")],
    )

    assert [item.session_id for item in index.list_page(limit=10).items] == ["rebuilt"]
    assert rebuild_calls == ["schema"]
    assert json.loads(path.read_text(encoding="utf-8"))["schema_version"] == SCHEMA_VERSION

    path.write_text("{broken", encoding="utf-8")
    recovered = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: [_summary("recovered", updated_at="2026-08-31T02:00:00+00:00")],
    )
    assert [item.session_id for item in recovered.list_page(limit=10).items] == ["recovered"]
    quarantined = list(tmp_path.glob("summary-index.json.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text(encoding="utf-8") == "{broken"


def test_valid_index_with_unfinished_source_projection_rebuilds_on_restart(tmp_path):
    path = tmp_path / "summary-index.json"
    records = [_summary("before", updated_at="2026-08-31T01:00:00+00:00")]
    index = SessionSummaryIndex(path, rebuild_loader=lambda: list(records))
    assert [item.session_id for item in index.list_all()] == ["before"]

    projection = index.begin_source_projection()
    records.append(_summary("after", updated_at="2026-08-31T03:00:00+00:00"))

    restarted = SessionSummaryIndex(path, rebuild_loader=lambda: list(records))
    assert [item.session_id for item in restarted.list_all()] == ["after", "before"]
    restarted.complete_source_projection(projection)


def test_one_concurrent_projection_cannot_clear_another_dirty_marker(tmp_path):
    path = tmp_path / "summary-index.json"
    index = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: [_summary("stable", updated_at="2026-08-31T01:00:00+00:00")],
    )
    index.list_all()

    first = index.begin_source_projection()
    second = index.begin_source_projection()
    index.complete_source_projection(second)

    rebuild_calls = []
    restarted = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: rebuild_calls.append(True)
        or [_summary("stable", updated_at="2026-08-31T01:00:00+00:00")],
    )
    restarted.list_all()
    assert rebuild_calls == [True]

    index.complete_source_projection(first)


def test_rebuild_does_not_clear_a_projection_still_owned_by_this_process(tmp_path):
    path = tmp_path / "summary-index.json"
    summary = _summary("stable", updated_at="2026-08-31T01:00:00+00:00")
    index = SessionSummaryIndex(path, rebuild_loader=lambda: [summary])
    index.list_all()
    index.begin_source_projection()

    with pytest.raises(SessionSummaryIndexDirtyError):
        index.rebuild()

    rebuild_calls = []
    restarted = SessionSummaryIndex(
        path,
        rebuild_loader=lambda: rebuild_calls.append(True) or [summary],
    )
    restarted.list_all()
    assert rebuild_calls == [True]


def test_abandoned_projection_stays_dirty_until_same_process_rebuild(tmp_path):
    path = tmp_path / "summary-index.json"
    records = [_summary("before", updated_at="2026-08-31T01:00:00+00:00")]
    index = SessionSummaryIndex(path, rebuild_loader=lambda: list(records))
    index.list_all()

    projection = index.begin_source_projection()
    records.append(_summary("after", updated_at="2026-08-31T02:00:00+00:00"))
    assert index.abandon_source_projection(projection) is True
    assert internal(index).dirty_path.exists() is True

    assert [item.session_id for item in index.list_all()] == ["after", "before"]
    assert internal(index).dirty_path.exists() is False
    assert index.state == "ready"


def test_corrupt_projection_marker_is_quarantined_before_source_rebuild(tmp_path):
    path = tmp_path / "summary-index.json"
    records = [_summary("before", updated_at="2026-08-31T01:00:00+00:00")]
    index = SessionSummaryIndex(path, rebuild_loader=lambda: list(records))
    index.list_all()
    records.append(_summary("after", updated_at="2026-08-31T02:00:00+00:00"))
    dirty_path = path.with_name(f"{path.name}.dirty")
    dirty_path.write_text("{broken", encoding="utf-8")

    restarted = SessionSummaryIndex(path, rebuild_loader=lambda: list(records))

    assert [item.session_id for item in restarted.list_all()] == ["after", "before"]
    quarantined = list(tmp_path.glob("summary-index.json.dirty.corrupt.*"))
    assert len(quarantined) == 1


def test_successful_publish_stays_unreadable_until_outstanding_projection_completes(tmp_path):
    rebuild_calls = []
    records = [_summary("before", updated_at="2026-08-31T01:00:00+00:00")]
    index = SessionSummaryIndex(
        tmp_path / "summary-index.json",
        rebuild_loader=lambda: rebuild_calls.append(True) or list(records),
    )
    index.list_all()
    projection = index.begin_source_projection()
    records.append(_summary("after", updated_at="2026-08-31T02:00:00+00:00"))

    index.upsert(records[-1], projection_token=projection)

    assert index.state == "degraded"
    with pytest.raises(SessionSummaryIndexDirtyError):
        index.list_all()
    assert len(rebuild_calls) >= 2

    index.complete_source_projection(projection)
    assert index.state == "ready"
    assert [item.session_id for item in index.list_all()] == ["after", "before"]


def test_summaries_and_pages_are_immutable_and_return_defensive_documents(tmp_path):
    index = SessionSummaryIndex(tmp_path / "summary-index.json")
    source_metadata = {"pinned": True, "nested": {"items": ["one"]}}
    summary = SessionSummary.create(
        session_id="immutable",
        title="Immutable",
        created_at="2026-08-31T00:00:00+00:00",
        updated_at="2026-08-31T01:00:00+00:00",
        metadata=source_metadata,
    )
    source_metadata["nested"]["items"].append("mutated")
    index.upsert(summary)
    page = index.list_page(limit=10)

    with pytest.raises(TypeError):
        page.items[0].metadata["pinned"] = False
    document = page.items[0].to_dict()
    document["metadata"]["nested"]["items"].append("local")
    assert index.list_page(limit=10).items[0].to_dict()["metadata"]["nested"]["items"] == ["one"]


def test_delete_is_idempotent_and_separate_index_paths_are_isolated(tmp_path):
    first = SessionSummaryIndex(tmp_path / "user-a" / "summary-index.json")
    second = SessionSummaryIndex(tmp_path / "user-b" / "summary-index.json")
    first.upsert(_summary("a", updated_at="2026-08-31T01:00:00+00:00"))
    second.upsert(_summary("b", updated_at="2026-08-31T01:00:00+00:00"))

    revision = first.index_revision
    assert first.delete("missing") is False
    assert first.index_revision == revision
    assert first.delete("a") is True
    assert first.list_page(limit=10).items == ()
    assert [item.session_id for item in second.list_page(limit=10).items] == ["b"]


def test_feature_flag_preserves_legacy_path(monkeypatch):
    monkeypatch.setenv("PERF_SESSION_SUMMARY_INDEX_ENABLED", "false")
    assert session_summary_index_enabled() is False
    monkeypatch.setenv("PERF_SESSION_SUMMARY_INDEX_ENABLED", "true")
    assert session_summary_index_enabled() is True


def test_bounded_read_marks_missing_index_not_ready_without_running_loader(tmp_path):
    rebuild_calls = []
    scheduled = []
    index = SessionSummaryIndex(
        tmp_path / "missing.json",
        rebuild_loader=lambda: rebuild_calls.append(True) or [],
        rebuild_scheduler=lambda: scheduled.append(True) or True,
    )

    assert index.load_persisted_for_read() is False
    assert index.request_rebuild() is True
    assert rebuild_calls == []
    assert scheduled == [True]


def test_rebuild_loader_does_not_hold_index_lock_and_retries_after_concurrent_write(
    tmp_path,
):
    seed = _summary("seed", updated_at="2026-08-31T00:00:00+00:00")
    concurrent = _summary("concurrent", updated_at="2026-08-31T01:00:00+00:00")
    index = SessionSummaryIndex(tmp_path / "summary.json")
    index.upsert(seed)

    loader_entered = threading.Event()
    writer_committed = threading.Event()
    loader_calls = []

    def rebuild_loader():
        loader_calls.append(True)
        if len(loader_calls) == 1:
            loader_entered.set()
            assert writer_committed.wait(timeout=1.0)
            return [seed]
        return [seed, concurrent]

    internal(index).rebuild_loader = rebuild_loader
    rebuild_errors = []
    writer_errors = []
    rebuild_thread = threading.Thread(
        target=lambda: _capture_thread_error(rebuild_errors, index.rebuild),
        daemon=True,
    )

    def write_while_loader_reads_source():
        try:
            index.upsert(concurrent)
        except BaseException as exc:
            writer_errors.append(exc)
        finally:
            writer_committed.set()

    writer_thread = threading.Thread(
        target=write_while_loader_reads_source,
        daemon=True,
    )
    rebuild_thread.start()
    assert loader_entered.wait(timeout=1.0)
    writer_thread.start()
    try:
        assert writer_committed.wait(timeout=0.25), "rebuild loader held the index lock while waiting on source state"
    finally:
        writer_committed.set()
        rebuild_thread.join(timeout=1.0)
        writer_thread.join(timeout=1.0)

    assert rebuild_thread.is_alive() is False
    assert writer_thread.is_alive() is False
    assert rebuild_errors == []
    assert writer_errors == []
    assert len(loader_calls) == 2
    assert {item.session_id for item in index.list_all()} == {"seed", "concurrent"}


def test_rebuild_stops_after_bounded_conflicting_source_projections(tmp_path):
    seed = _summary("seed", updated_at="2026-08-31T00:00:00+00:00")
    index = SessionSummaryIndex(tmp_path / "bounded-rebuild.json")
    index.upsert(seed)
    loader_calls = []

    def rebuild_loader():
        loader_calls.append(True)
        token = index.begin_source_projection()
        index.abandon_source_projection(token)
        return [seed]

    internal(index).rebuild_loader = rebuild_loader

    with pytest.raises(SessionSummaryIndexCorruptionError):
        index.rebuild()

    assert 1 < len(loader_calls) <= 8
    assert index.state == "degraded"


def test_overlapping_rebuilds_share_one_loader_result(tmp_path):
    summary = _summary("shared", updated_at="2026-08-31T00:00:00+00:00")
    loader_entered = threading.Event()
    release_loader = threading.Event()
    second_started = threading.Event()
    loader_calls = []

    def rebuild_loader():
        loader_calls.append(True)
        if len(loader_calls) == 1:
            loader_entered.set()
            assert release_loader.wait(timeout=1.0)
        return [summary]

    index = SessionSummaryIndex(
        tmp_path / "single-flight.json",
        rebuild_loader=rebuild_loader,
    )
    results = []
    errors = []
    first = threading.Thread(
        target=lambda: _capture_thread_result(errors, results, index.rebuild),
        daemon=True,
    )

    def second_rebuild():
        second_started.set()
        _capture_thread_result(errors, results, index.rebuild)

    second = threading.Thread(target=second_rebuild, daemon=True)
    first.start()
    assert loader_entered.wait(timeout=1.0)
    second.start()
    assert second_started.wait(timeout=1.0)
    try:
        assert second.is_alive() is True
    finally:
        release_loader.set()
        first.join(timeout=1.0)
        second.join(timeout=1.0)

    assert first.is_alive() is False
    assert second.is_alive() is False
    assert errors == []
    assert loader_calls == [True]
    assert results == [1, 1]


def test_index_selects_oldest_unpinned_inactive_prune_candidates(tmp_path):
    index = SessionSummaryIndex(tmp_path / "summary-index.json")
    for summary in (
        _summary("protected-pinned", updated_at="2026-08-31T01:00:00+00:00", pinned=True),
        _summary(
            "protected-active",
            updated_at="2026-08-31T02:00:00+00:00",
            active_run_id="run-active",
        ),
        _summary("eligible-old", updated_at="2026-08-31T03:00:00+00:00"),
        _summary("eligible-middle", updated_at="2026-08-31T04:00:00+00:00"),
        _summary("eligible-new", updated_at="2026-08-31T05:00:00+00:00"),
    ):
        index.upsert(summary)

    candidates = index.prune_candidates(max_sessions=3, limit=10)
    assert [(candidate.session_id, candidate.expected_updated_at) for candidate in candidates] == [
        ("eligible-old", "2026-08-31T03:00:00+00:00"),
        ("eligible-middle", "2026-08-31T04:00:00+00:00"),
    ]
    assert [item.session_id for item in index.list_all()] == [
        "protected-pinned",
        "eligible-new",
        "eligible-middle",
        "eligible-old",
        "protected-active",
    ]


def test_prune_candidates_never_rebuilds_authoritative_history_when_index_is_dirty(
    tmp_path,
):
    loader_calls = []
    index = SessionSummaryIndex(
        tmp_path / "summary-index.json",
        rebuild_loader=lambda: loader_calls.append(True) or [],
    )
    index.rebuild()
    loader_calls.clear()
    projection = index.begin_source_projection()
    index.abandon_source_projection(projection)

    with pytest.raises(SessionSummaryIndexDirtyError):
        index.prune_candidates(max_sessions=0, limit=1)

    assert loader_calls == []
