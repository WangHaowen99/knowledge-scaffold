"""Tests for the skill-evolution service: gates, diff, poll, approve, drift.

Uses a real temp ontology fixture + a fake skill-evolver HTTP client (no network).
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from agent.skill_evolution.models import TaskRecord
from agent.skill_evolution.service import (
    SkillEvolutionService,
    SkillEvolverClient,
    _interpret_status,
    _line_diff_counts,
    _locate_evolved_root,
    _read_zip_members,
    _strip_archive_root,
)
from agent.skill_evolution.store import SkillEvolutionStore


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
def _write_ontology(onto_dir: Path, ops: Dict[str, str], active_version: str = "1.0.0",
                    versions: Optional[List[str]] = None) -> Path:
    """Create a minimal ontology with a version dir + operations files."""
    versions = versions or [active_version]
    onto_dir.mkdir(parents=True, exist_ok=True)
    # Create every version dir so set_ontology_active_version can switch to any.
    for v in versions:
        vdir = onto_dir / "versions" / v
        (vdir / "operations").mkdir(parents=True, exist_ok=True)
        for name, content in ops.items():
            (vdir / "operations" / name).write_text(content, encoding="utf-8")
        (vdir / "operations" / "index.md").write_text(
            "\n".join("- {}".format(Path(n).stem) for n in ops) + "\n", encoding="utf-8"
        )
    manifest = (
        "kind: ontology-package\n"
            "schema_version: 0.1\n"
        "id: onto1\n"
        "name: 测试本体\n"
        "description: test\n"
        "version: {}\n"
        "active_version: {}\n"
        "versions: [{}]\n"
        "language: zh-CN\n"
        "enabled: true\n"
    ).format(active_version, active_version, ", ".join(versions))
    (onto_dir / "manifest.yaml").write_text(manifest, encoding="utf-8")
    return onto_dir / "versions" / active_version


def _build_ontology_zip(root_name: str, ops: Dict[str, str], operations_index: str) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(
            "{}/manifest.yaml".format(root_name),
            "kind: ontology-package\nschema_version: 0.1\nid: onto1\n",
        )
        zf.writestr("{}/operations/index.md".format(root_name), operations_index)
        for name, content in ops.items():
            zf.writestr("{}/operations/{}".format(root_name, name), content)
    return buf.getvalue()


class FakeClient:
    def __init__(self, status_payload: Dict[str, Any], artifact_bytes: bytes = b""):
        self._status = status_payload
        self._artifact = artifact_bytes
        self.submit_calls = 0
        self.last_request = None

    def submit_run(self, trace_bytes, baseline_bytes, request_json):
        self.submit_calls += 1
        self.last_request = request_json
        return "run-fake-1"

    def get_run_status(self, run_id):
        return self._status

    def download_artifact(self, run_id):
        return self._artifact


class FakeExecutionStore:
    def __init__(self, turns: List[Dict[str, Any]]):
        self._history = {
            "session_id": "sess1",
            "turns": turns,
            "session_history": [],
            "read_documents": {},
        }

    def get_session_history(self, session_id):
        return self._history


def _make_service(tmp_path, client=None):
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store, client=client)
    # Enable the feature for tests and provide a base_url.
    service._config = lambda: {  # noqa: SLF001
        "enabled": True,
        "base_url": "http://skill-evolver.test",
        "timeout_seconds": 30,
        "poll_interval_seconds": 10,
        "task_timeout_seconds": 1800,
        "max_iterations": 15,
    }
    return store, service


def _make_record(store, workspace_id="ws1", ontology_id="onto1", status="completed"):
    rec = TaskRecord(
        task_id="t1",
        workspace_id=workspace_id,
        ontology_id=ontology_id,
        session_id="sess1",
        triggered_by="alice",
        run_id="run-fake-1",
        status=status,
        active_version="1.0.0",
        version_sha="abc",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    store.create_task(rec)
    return rec


# --------------------------------------------------------------------------- #
# Pure helpers
# --------------------------------------------------------------------------- #
def test_interpret_status():
    assert _interpret_status({"status": "running"})[0] == "running"
    assert _interpret_status({"status": "completed"})[0] == "completed"
    assert _interpret_status({"status": "succeeded"})[0] == "completed"
    assert _interpret_status({"status": "failed", "error": "boom"})[0] == "failed"
    assert _interpret_status({"status": ""})[0] == "running"
    assert _interpret_status({})[0] == "running"


def test_line_diff_counts():
    added, removed = _line_diff_counts("a\nb\nc", "a\nB\nc")
    assert added >= 1 and removed >= 1


def test_strip_archive_root():
    assert _strip_archive_root("onto1/operations/foo.md") == "operations/foo.md"
    assert _strip_archive_root("foo.md") == "foo.md"


def test_read_zip_members():
    z = _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n")
    members = _read_zip_members(z)
    assert "onto1/operations/foo.md" in members
    assert members["onto1/operations/foo.md"] == "# foo"


def test_read_zip_members_enforces_resource_limits(monkeypatch):
    from api.routes import skills as skills_routes

    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as handle:
        handle.writestr("skill-demo/data.txt", "a" * 100)

    monkeypatch.setattr(skills_routes, "SKILL_ARCHIVE_MAX_MEMBER_BYTES", 50)
    with pytest.raises(ValueError, match="member is too large"):
        _read_zip_members(archive.getvalue())

    monkeypatch.setattr(skills_routes, "SKILL_ARCHIVE_MAX_MEMBER_BYTES", 200)
    monkeypatch.setattr(skills_routes, "SKILL_ARCHIVE_MAX_COMPRESSION_RATIO", 2)
    with pytest.raises(ValueError, match="compression ratio"):
        _read_zip_members(archive.getvalue())


def test_read_zip_members_rejects_path_traversal():
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("../escaped.txt", "synthetic")

    with pytest.raises(ValueError, match="unsafe path"):
        _read_zip_members(archive.getvalue())


def test_locate_evolved_root(tmp_path):
    root = tmp_path / "extract"
    (root / "onto1" / "operations").mkdir(parents=True)
    (root / "onto1" / "operations" / "index.md").write_text("# operations\n")
    assert _locate_evolved_root(root).name == "onto1"
    (root / "operations").mkdir(parents=True, exist_ok=True)
    (root / "operations" / "index.md").write_text("# operations\n")
    # top-level has operations now
    assert _locate_evolved_root(root) == root


# --------------------------------------------------------------------------- #
# Diff
# --------------------------------------------------------------------------- #
def test_compute_diff_modified_added_deleted(tmp_path):
    store, service = _make_service(tmp_path)
    rec = _make_record(store)
    # Baseline: foo only
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    # Evolved: foo modified, bar added, (baz deleted — only in baseline)
    # Add baz to baseline to test deletion:
    store.save_baseline("ws1", "t1", _build_ontology_zip(
        "onto1", {"foo.md": "# foo\nstep1\n", "baz.md": "# baz"}, "- foo\n- baz\n"))
    evolved_dir = store.ensure_temp_dir("ws1", "t1") / "onto1"
    (evolved_dir / "operations").mkdir(parents=True)
    (evolved_dir / "operations" / "foo.md").write_text("# foo\nstep1\nstep2\n")
    (evolved_dir / "operations" / "bar.md").write_text("# bar")
    (evolved_dir / "operations" / "index.md").write_text("- foo\n- bar\n")

    diff = service.compute_diff(rec)
    assert not diff.is_empty
    by_path = {f.path: f for f in diff.files}
    assert "operations/foo.md" in by_path and by_path["operations/foo.md"].change_type == "modified"
    assert "operations/bar.md" in by_path and by_path["operations/bar.md"].change_type == "added"
    assert "operations/baz.md" in by_path and by_path["operations/baz.md"].change_type == "deleted"


def test_compute_diff_empty_when_identical(tmp_path):
    store, service = _make_service(tmp_path)
    rec = _make_record(store)
    zbytes = _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n")
    store.save_baseline("ws1", "t1", zbytes)
    evolved_dir = store.ensure_temp_dir("ws1", "t1") / "onto1"
    (evolved_dir / "operations").mkdir(parents=True)
    (evolved_dir / "operations" / "foo.md").write_text("# foo")
    (evolved_dir / "operations" / "index.md").write_text("- foo\n")
    diff = service.compute_diff(rec)
    assert diff.is_empty


def test_compute_diff_normalizes_crlf_vs_lf(tmp_path):
    """Baseline (Windows disk → CRLF) vs evolved (skill-evolver Linux → LF)
    must NOT register as a change when content is identical, and a real
    1-line change must count as +1 -1, not a full-file rewrite. Regression
    for the CRLF/LF mismatch that made unchanged files show as modified and
    made real changes render as full delete+add."""
    store, service = _make_service(tmp_path)
    rec = _make_record(store)
    # Baseline stored with CRLF (as _build_ontology_zip_buffer would from disk).
    store.save_baseline(
        "ws1", "t1",
        _build_ontology_zip("onto1", {"foo.md": "# foo\r\nstep1\r\nstep2\r\n"}, "- foo\r\n"),
    )
    # Evolved with LF, plus one real added line.
    evolved_dir = store.ensure_temp_dir("ws1", "t1") / "onto1"
    (evolved_dir / "operations").mkdir(parents=True)
    (evolved_dir / "operations" / "foo.md").write_text("# foo\nstep1\nstep2\nstep3\n")
    (evolved_dir / "operations" / "index.md").write_text("- foo\n")

    diff = service.compute_diff(rec)
    # operations/index.md differs only by CRLF vs LF → must be skipped.
    by_path = {f.path: f for f in diff.files}
    assert "operations/index.md" not in by_path
    # foo.md has one real added line → +1 -1, content normalized to LF.
    assert "operations/foo.md" in by_path
    foo = by_path["operations/foo.md"]
    assert (foo.added_lines, foo.removed_lines) == (1, 0)
    assert "\r" not in foo.original and "\r" not in foo.evolved


# --------------------------------------------------------------------------- #
# Gates
# --------------------------------------------------------------------------- #
def _patch_ontology(service, onto_dir, monkeypatch):
    from ontology.ontologies import OntologyLoader
    # Re-load fresh on each call so manifest changes (e.g. active_version switch)
    # are reflected.
    monkeypatch.setattr(
        service, "_resolve_ontology", lambda ws, oid: OntologyLoader.load_ontology(onto_dir)
    )
    monkeypatch.setattr(
        service, "_ontology_dir", lambda ws, oid: onto_dir
    )


def _stamp_real_sha(service, store, rec, onto_dir, version="1.0.0"):
    """Set the record's version_sha to the real sha of a version dir."""
    rec.version_sha = service._compute_version_sha(onto_dir / "versions" / version)  # noqa: SLF001
    store.save_task(rec)


def test_gate_too_few_turns(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    store, service = _make_service(tmp_path)
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "hi", "ontology_version": "1.0.0"},
    ])
    with pytest.raises(Exception) as exc:
        service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                      triggered_by="alice", execution_store=exec_store)
    assert "至少需要 2 轮" in str(exc.value)


def test_gate_cross_version_blocked(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"}, versions=["1.0.0", "1.1.0"])
    store, service = _make_service(tmp_path)
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1", "ontology_version": "1.0.0"},
        {"turn_id": 2, "user_request": "q2", "ontology_version": "1.1.0"},  # mismatch
    ])
    with pytest.raises(Exception) as exc:
        service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                      triggered_by="alice", execution_store=exec_store)
    assert "跨多个本体版本" in str(exc.value)


def test_gate_stale_version_distinct_message(tmp_path, monkeypatch):
    """All turns stamped with the same version, but ≠ current active (stale
    after approval). Must say '基于版本 X，当前激活为 Y', not '跨多个版本'."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"}, active_version="1.0.1", versions=["1.0.0", "1.0.1"])
    store, service = _make_service(tmp_path)
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1", "ontology_version": "1.0.0"},
        {"turn_id": 2, "user_request": "q2", "ontology_version": "1.0.0"},  # same, but ≠ active 1.0.1
    ])
    with pytest.raises(Exception) as exc:
        service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                      triggered_by="alice", execution_store=exec_store)
    msg = str(exc.value)
    assert "基于版本 1.0.0" in msg
    assert "当前激活版本为 1.0.1" in msg
    assert "跨多个" not in msg


def test_gate_single_version_allows_missing_stamp(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})  # single version
    client = FakeClient(
        {"status": "running"},
        _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n"),
    )
    store, service = _make_service(tmp_path, client=client)
    _patch_ontology(service, onto_dir, monkeypatch)
    # No ontology_version stamps (old session) but single version → allowed
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1"},
        {"turn_id": 2, "user_request": "q2"},
    ])
    record = service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                           triggered_by="alice", execution_store=exec_store)
    assert record.status == "submitted"
    assert record.run_id == "run-fake-1"
    # max_iterations must be forwarded to skill-evolver (default 15, range 1-30).
    assert client.last_request["evolution"]["max_iterations"] == 15


def test_gate_multi_version_allows_no_stamps(tmp_path, monkeypatch):
    """Tool-based sessions (agent loaded ontology via ontology_context) have no
    per-turn version stamps. Gate 3 must NOT block them on multi-version
    ontologies — only enforce strict alignment when stamps are present."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"}, versions=["1.0.0", "1.1.0"])
    store, service = _make_service(tmp_path, client=FakeClient({"status": "running"}, b""))
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1"},  # no stamp
        {"turn_id": 2, "user_request": "q2"},
    ])
    record = service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                           triggered_by="alice", execution_store=exec_store)
    assert record.status == "submitted"


def test_start_infers_ontology_from_trace(tmp_path, monkeypatch):
    """When ontology_id is not provided (tool-based session, no UI selection),
    infer the target ontology from read_documents in the trace."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    store, service = _make_service(tmp_path, client=FakeClient({"status": "running"}, b""))
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1"},
        {"turn_id": 2, "user_request": "q2"},
    ])
    exec_store._history["read_documents"] = {
        "by_path": {"x": {"ontology_id": "onto1"}, "y": {"ontology_id": "onto1"}},
        "recent_paths": [],
        "active_object_docs": [],
    }
    record = service.start(workspace_id="ws1", ontology_id=None, session_id="sess1",
                           triggered_by="alice", execution_store=exec_store)
    assert record.ontology_id == "onto1"


def test_gate_excludes_running_turn(tmp_path, monkeypatch):
    """A mid-stream click leaves the current turn status='running' (user
    question, no answer). gate 2 must count only completed turns so a partial
    trace is never sent."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    store, service = _make_service(tmp_path, client=FakeClient({"status": "running"}, b""))
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1", "status": "success"},
        {"turn_id": 2, "user_request": "q2", "status": "running"},  # mid-stream
    ])
    with pytest.raises(Exception) as exc:
        service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                      triggered_by="alice", execution_store=exec_store)
    assert "至少需要 2 轮" in str(exc.value)


def test_infer_toolbased_ontology_version(tmp_path, monkeypatch):
    """The orchestrator helper stamps tool-based sessions' turns with the
    ontology_version the agent actually read — taken from the read-document
    records' ``ontology_version`` field (written at read time), NOT by
    re-reading the manifest at turn completion (which races when active is
    switched mid-turn)."""
    from agent.core.orchestrator import AgenticOrchestrator

    infer = AgenticOrchestrator._infer_toolbased_ontology_version
    # single ontology, all reads agree on version → that version
    rd = {"by_path": {
        "a": {"ontology_id": "onto1", "ontology_version": "1.0.0"},
        "b": {"ontology_id": "onto1", "ontology_version": "1.0.0"},
    }}
    assert infer("ws", rd) == "1.0.0"
    # records without a version field (old reads) are ignored → no version → None
    rd_old = {"by_path": {"a": {"ontology_id": "onto1"}, "b": {"ontology_id": "onto1"}}}
    assert infer("ws", rd_old) is None
    # ambiguous (two ontologies) → None
    rd2 = {"by_path": {
        "a": {"ontology_id": "onto1", "ontology_version": "1.0.0"},
        "b": {"ontology_id": "onto2", "ontology_version": "1.0.0"},
    }}
    assert infer("ws", rd2) is None
    # cross-version within the turn (records disagree) → None (ambiguous)
    rd3 = {"by_path": {
        "a": {"ontology_id": "onto1", "ontology_version": "1.0.0"},
        "b": {"ontology_id": "onto1", "ontology_version": "1.0.1"},
    }}
    assert infer("ws", rd3) is None
    # no reads → None
    assert infer("ws", {"by_path": {}}) is None
    # the version read at read time wins even if active was switched later —
    # the helper must NOT consult the current manifest (regression for the
    # mid-turn active-switch race).
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"}, active_version="1.0.1")
    monkeypatch.setattr("workspace.workspaces.get_workspace_ontologies_root", lambda ws: tmp_path / "ontologies")
    rd_read_old = {"by_path": {"a": {"ontology_id": "onto1", "ontology_version": "1.0.0"}}}
    assert infer("ws", rd_read_old) == "1.0.0"


def test_read_document_record_carries_ontology_version():
    """build_read_document_record stores the read-time ontology_version so the
    tool-based stamp captures the version actually served, not a later one."""
    from utils.read_docs import build_read_document_record, merge_read_documents_registry

    record = build_read_document_record(
        file_path="ontologies/onto1/versions/1.0.0/operations/foo.md",
        content="# foo",
        source="ontology_context",
        turn_id=1,
        ontology_version="1.0.0",
    )
    assert record["ontology_version"] == "1.0.0"
    # omitted → None (old callers / non-ontology reads)
    assert build_read_document_record(
        file_path="ontologies/onto1/manifest.yaml",
        content="",
        source="ontology_context",
        turn_id=1,
    )["ontology_version"] is None
    # merge preserves the field and feeds the infer helper
    merged = merge_read_documents_registry(None, [record])
    by_path = merged["by_path"]
    assert by_path[record["normalized_path"]]["ontology_version"] == "1.0.0"
    from agent.core.orchestrator import AgenticOrchestrator
    assert AgenticOrchestrator._infer_toolbased_ontology_version("ws", merged) == "1.0.0"


def test_start_blocks_when_ontology_not_inferrable(tmp_path, monkeypatch):
    """No ontology_id provided AND trace references 0 ontologies → block."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    store, service = _make_service(tmp_path, client=FakeClient({"status": "running"}, b""))
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1"},
        {"turn_id": 2, "user_request": "q2"},
    ])
    # no read_documents, no ontology_context calls
    with pytest.raises(Exception) as exc:
        service.start(workspace_id="ws1", ontology_id=None, session_id="sess1",
                      triggered_by="alice", execution_store=exec_store)
    assert "无法确定进化目标本体" in str(exc.value)


# --------------------------------------------------------------------------- #
# Poll
# --------------------------------------------------------------------------- #
def test_poll_completed_extracts_and_marks_completed(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)

    rec = _make_record(store, status="submitted")
    # Need a baseline for compute_diff
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    out = service.poll_once(rec)
    assert out.status == "completed"
    assert not out.change_summary == ""


def test_poll_no_change_marks_no_change(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n"))
    out = service.poll_once(rec)
    assert out.status == "no_change"
    # temp cleaned on terminal
    assert not store.temp_path("ws1", "t1").exists()


def test_poll_failed_marks_failed(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    store, service = _make_service(tmp_path, client=FakeClient({"status": "failed", "error": "boom"}))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    out = service.poll_once(rec)
    assert out.status == "failed"
    assert "boom" in out.error_message


def test_poll_running_stays_running(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    store, service = _make_service(tmp_path, client=FakeClient({"status": "running"}))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="running")
    out = service.poll_once(rec)
    assert out.status == "running"


class _DownloadClient:
    """Fake client whose download can raise or return arbitrary bytes — for
    download / extract / structure-mismatch failure paths (D2)."""

    def __init__(self, *, artifact_bytes=b"", download_raises=None):
        self._artifact = artifact_bytes
        self._download_raises = download_raises

    def get_run_status(self, run_id):
        return {"status": "completed"}

    def download_artifact(self, run_id):
        if self._download_raises is not None:
            raise self._download_raises
        return self._artifact


def test_poll_download_failure_marks_failed_no_temp(tmp_path, monkeypatch):
    """skill-evolver reports completed but artifact download fails → failed at
    stage=download. No temp dir is created (download happens before extract)."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    client = _DownloadClient(download_raises=RuntimeError("HTTP 502"))
    store, service = _make_service(tmp_path, client=client)
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    out = service.poll_once(rec)
    assert out.status == "failed"
    assert out.failed_stage == "download"
    assert "502" in out.error_message
    assert not store.temp_path("ws1", "t1").exists()


def test_poll_extract_failure_cleans_temp(tmp_path, monkeypatch):
    """Artifact downloads but is not a valid zip → failed at stage=extract, and
    the half-created temp dir is cleaned (no residue left for a retry)."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    client = _DownloadClient(artifact_bytes=b"this is not a zip")
    store, service = _make_service(tmp_path, client=client)
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    out = service.poll_once(rec)
    assert out.status == "failed"
    assert out.failed_stage == "extract"
    assert not store.temp_path("ws1", "t1").exists()


def test_poll_artifact_missing_operations_marks_failed(tmp_path, monkeypatch):
    """A valid zip with no operations/ dir → structure mismatch → failed at
    stage=extract, temp cleaned."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("onto1/random.txt", "junk")  # no operations/ anywhere
    client = _DownloadClient(artifact_bytes=buf.getvalue())
    store, service = _make_service(tmp_path, client=client)
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    out = service.poll_once(rec)
    assert out.status == "failed"
    assert out.failed_stage == "extract"
    assert "operations" in out.error_message
    assert not store.temp_path("ws1", "t1").exists()


def test_poll_artifact_path_traversal_blocked(tmp_path, monkeypatch):
    """A zip whose member escapes the extract root is refused by
    _safe_extract_zip → failed at stage=extract. Regression for the
    skill-evolution extract entry (the shared helper, but invoked here)."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("onto1/operations/foo.md", "# foo")
        zf.writestr("../../escape.md", "pwn")  # traversal
    client = _DownloadClient(artifact_bytes=buf.getvalue())
    store, service = _make_service(tmp_path, client=client)
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    out = service.poll_once(rec)
    assert out.status == "failed"
    assert out.failed_stage == "extract"
    # The traversal file must not have been written outside the temp dir.
    assert not (tmp_path / "escape.md").exists()


# --------------------------------------------------------------------------- #
# Approve / drift / cancel
# --------------------------------------------------------------------------- #
def test_approve_new_version(tmp_path, monkeypatch):
    import ontology.view_cache as view_cache_module
    from ontology.view_cache import OntologyViewCache, get_ontology_mutation_revision

    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8),
    )
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)  # → completed + temp populated

    view_revision = get_ontology_mutation_revision("ws1", "onto1")
    out = service.approve(rec, mode="new_version", version_name="evol-1", decided_by="admin")
    assert out.status == "approved"
    assert out.new_version == "evol-1"
    # New version dir created
    assert (onto_dir / "versions" / "evol-1" / "operations" / "foo.md").exists()
    # temp cleaned
    assert not store.temp_path("ws1", "t1").exists()
    # history written
    assert store.get_history("ws1", "t1") is not None
    assert get_ontology_mutation_revision("ws1", "onto1") == view_revision + 1


def test_approve_version_conflict_rejected(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"}, versions=["1.0.0", "1.1.0"])
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)
    with pytest.raises(ValueError) as exc:
        service.approve(rec, mode="new_version", version_name="1.1.0", decided_by="admin")
    assert "已存在" in str(exc.value)


def test_drift_content_modified_blocks_approve(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)
    # Simulate external edit to the active version
    (onto_dir / "versions" / "1.0.0" / "operations" / "foo.md").write_text("# foo\nCHANGED\n")
    drift = service.detect_drift(rec)
    assert drift.drifted
    assert drift.reason == "content_modified"
    with pytest.raises(ValueError):
        service.approve(rec, mode="new_version", version_name="evol-1", decided_by="admin")


def test_drift_active_version_switched(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"}, versions=["1.0.0", "1.1.0"])
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")  # active_version="1.0.0"
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo"}, "- foo\n"))
    service.poll_once(rec)
    # Switch active version
    from ontology.ontologies import set_ontology_active_version
    set_ontology_active_version(onto_dir, "1.1.0")
    drift = service.detect_drift(rec)
    assert drift.drifted
    assert drift.reason == "active_version_switched"


def test_drift_detects_objects_edit(tmp_path, monkeypatch):
    """Whole-version sha must catch edits to non-operations files (objects/),
    not just operations. Regression for the operations-only sha under-scope:
    once skill-evolver evolves objects/scripts, an operations-only sha would
    miss approval-time edits to those files."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    # Add an objects/ file to the active version (skill-evolver may evolve this).
    objects_dir = onto_dir / "versions" / "1.0.0" / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    (objects_dir / "equipment.md").write_text("# equipment\nfield: a\n", encoding="utf-8")

    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)
    # External edit to objects/ (NOT operations) during approval window.
    (objects_dir / "equipment.md").write_text("# equipment\nfield: CHANGED\n", encoding="utf-8")
    drift = service.detect_drift(rec)
    assert drift.drifted
    assert drift.reason == "content_modified"


def test_approve_rejects_partial_snapshot(tmp_path, monkeypatch):
    """If the evolved artifact omits a component the active version has
    (e.g. skill-evolver returned only operations, no objects/), approve must
    abort rather than rmtree+copy and silently delete objects/."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    objects_dir = onto_dir / "versions" / "1.0.0" / "objects"
    objects_dir.mkdir(parents=True, exist_ok=True)
    (objects_dir / "equipment.md").write_text("# equipment\n", encoding="utf-8")

    # Evolved artifact has operations only — no objects/.
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)

    with pytest.raises(ValueError) as exc:
        service.approve(rec, mode="new_version", version_name="evol-1", decided_by="admin")
    assert "完整快照" in str(exc.value)
    # objects/ in the active version must NOT have been deleted.
    assert (objects_dir / "equipment.md").exists()
    # Task stays completed (approval did not go through).
    assert store.get_task("ws1", "t1").status == "completed"


def test_get_diff_serves_from_history_after_approve(tmp_path, monkeypatch):
    """Terminal tasks have temp+baseline cleaned; get_diff must return the diff
    persisted in history (not an empty recompute). Regression: approved tasks
    showed 'no changes' because compute_diff read deleted temp/baseline."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)  # → completed + temp populated
    service.approve(rec, mode="new_version", version_name="evol-h", decided_by="admin")

    # temp + baseline cleaned by finalize
    assert not store.temp_path("ws1", "t1").exists()
    assert not store.baseline_path("ws1", "t1").exists()
    # get_diff must still return the non-empty diff from history
    diff = service.get_diff(rec)
    assert not diff.is_empty
    assert any(f.path == "operations/foo.md" for f in diff.files)
    foo = next(f for f in diff.files if f.path == "operations/foo.md")
    assert "step2" in foo.evolved


def test_reject_and_cancel(tmp_path, monkeypatch):
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)

    rejected = service.reject(rec, decided_by="admin")
    assert rejected.status == "rejected"
    assert not store.temp_path("ws1", "t1").exists()

    # cancel on a running task
    rec2 = _make_record(store)  # completed (already rejected above); make a fresh running one
    rec2.task_id = "t2"
    rec2.status = "running"
    store.create_task(rec2)
    cancelled = service.cancel(rec2, decided_by="alice")
    assert cancelled.status == "cancelled"


# --------------------------------------------------------------------------- #
# Overwrite approve / rejected diff / max_iterations forwarding
# --------------------------------------------------------------------------- #
def test_approve_overwrite_replaces_active_version(tmp_path, monkeypatch):
    """Overwrite mode replaces the active version's contents in place — version
    number unchanged, no new version dir, manifest untouched, temp cleaned."""
    import ontology.view_cache as view_cache_module
    from ontology.view_cache import OntologyViewCache, get_ontology_mutation_revision

    monkeypatch.setattr(
        view_cache_module,
        "_view_cache",
        OntologyViewCache(max_entries=8),
    )
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)  # → completed + temp populated

    view_revision = get_ontology_mutation_revision("ws1", "onto1")
    out = service.approve(rec, mode="overwrite", version_name=None, decided_by="admin")
    assert out.status == "approved"
    assert out.decision == "approve_overwrite"
    assert out.new_version == ""  # overwrite keeps the version number
    # Active version dir contents replaced with the evolved text.
    foo = (onto_dir / "versions" / "1.0.0" / "operations" / "foo.md").read_text(encoding="utf-8")
    assert "step2" in foo
    # No new version dir created.
    assert not (onto_dir / "versions" / "evol-1").exists()
    # Stable-version byte changes are still authoritative mutations: the
    # coordinator must increment revision and persist a complete invalidation
    # event so Evidence Index cannot keep serving the pre-approval snapshot.
    from ontology.mutations import OntologyMutationCoordinator
    from workspace.workspaces import get_workspace_paths

    coordinator = OntologyMutationCoordinator(get_workspace_paths("ws1").runtime_dir)
    state = coordinator.store.get_state(workspace_id="ws1", ontology_id="onto1")
    assert state is not None
    assert state["mutation_revision"] == 1
    assert state["active_version"] == "1.0.0"
    outbox = coordinator.store.list_outbox(status="pending")
    assert len(outbox) == 1
    event = json.loads(outbox[0]["payload_json"])
    assert event["mutation_type"] == "ONTOLOGY_FILE_CHANGED"
    assert event["before_version"] == event["after_version"] == "1.0.0"
    assert event["known_file_digests"]["completeness"] == "complete"
    assert event["operator"] == {"type": "user", "id": "admin"}
    assert event["request_id"] == "run-fake-1"
    assert event["trigger"] == "admin"
    assert get_ontology_mutation_revision("ws1", "onto1") == view_revision + 1
    # temp cleaned, history written.
    assert not store.temp_path("ws1", "t1").exists()
    assert store.get_history("ws1", "t1") is not None


def test_get_diff_serves_from_history_after_reject(tmp_path, monkeypatch):
    """Reject also persists the full diff to history; get_diff must return the
    non-empty diff for a rejected task after temp+baseline are finalized away."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo\nstep1\n"})
    evolved_zip = _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\nstep2\n"}, "- foo\n")
    store, service = _make_service(tmp_path, client=FakeClient({"status": "completed"}, evolved_zip))
    _patch_ontology(service, onto_dir, monkeypatch)
    rec = _make_record(store, status="submitted")
    _stamp_real_sha(service, store, rec, onto_dir)
    store.save_baseline("ws1", "t1", _build_ontology_zip("onto1", {"foo.md": "# foo\nstep1\n"}, "- foo\n"))
    service.poll_once(rec)
    service.reject(rec, decided_by="admin")

    assert not store.temp_path("ws1", "t1").exists()
    assert not store.baseline_path("ws1", "t1").exists()
    diff = service.get_diff(rec)
    assert not diff.is_empty
    assert any(f.path == "operations/foo.md" for f in diff.files)


def test_max_iterations_forwarded_from_config(tmp_path, monkeypatch):
    """A non-default max_iterations from config must be forwarded to skill-evolver
    in request.evolution.max_iterations (not just the default 15)."""
    onto_dir = tmp_path / "ontologies" / "onto1"
    _write_ontology(onto_dir, {"foo.md": "# foo"})
    client = FakeClient({"status": "running"}, b"")
    store = SkillEvolutionStore(runtime_root=str(tmp_path))
    service = SkillEvolutionService(store, client=client)
    service._config = lambda: {  # noqa: SLF001
        "enabled": True,
        "base_url": "http://skill-evolver.test",
        "timeout_seconds": 30,
        "poll_interval_seconds": 10,
        "task_timeout_seconds": 1800,
        "max_iterations": 25,
    }
    _patch_ontology(service, onto_dir, monkeypatch)
    exec_store = FakeExecutionStore([
        {"turn_id": 1, "user_request": "q1"},
        {"turn_id": 2, "user_request": "q2"},
    ])
    service.start(workspace_id="ws1", ontology_id="onto1", session_id="sess1",
                  triggered_by="alice", execution_store=exec_store)
    assert client.last_request["evolution"]["max_iterations"] == 25
