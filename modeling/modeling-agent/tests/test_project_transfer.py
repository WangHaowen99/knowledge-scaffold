from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest

from modeling_agent.errors import AgentRuntimeError
from modeling_agent.project_transfer import (
    delete_project_transfer,
    export_project_transfer,
    import_project_transfer,
)
from modeling_agent.schemas import RUN_QUEUED, RUN_RUNNING, RunRecord
from modeling_agent.store import RuntimeStore


def _run(project_id: str) -> RunRecord:
    run_id = "run_source"
    return RunRecord(
        id=run_id,
        project_id=project_id,
        session_id=f"project:{project_id}:run:{run_id}",
        status=RUN_QUEUED,
        prompt="处理合成资料",
        token_fingerprint="111111111111",
        run_credential="synthetic-runtime-credential",
    )


def _checkpoint(path: Path, key: str, value: str) -> None:
    database = sqlite3.connect(path)
    try:
        database.execute("CREATE TABLE kv_store (key VARCHAR(255) PRIMARY KEY, value VARCHAR(4096) NOT NULL)")
        database.execute("INSERT INTO kv_store(key, value) VALUES (?, ?)", (key, value))
        database.execute(
            "INSERT INTO kv_store(key, value) VALUES (?, ?)",
            ("project:project_other:run:run_other:workflow:demo:state", "other"),
        )
        database.commit()
    finally:
        database.close()


def test_agent_runtime_and_checkpoints_round_trip_without_credentials(tmp_path: Path) -> None:
    source_project = "project_alpha"
    source_store = RuntimeStore(tmp_path / "source-runtime.sqlite")
    source_checkpoint = tmp_path / "source-checkpoints.sqlite"
    try:
        source_store.create_run(_run(source_project))
        source_store.update_run("run_source", status=RUN_RUNNING)
        source_store.recover_interrupted()
        source_store.append_event(
            source_project,
            "run_source",
            "message",
            {"role": "assistant", "projectId": source_project, "runId": "run_source"},
        )
        source_store.save_tool_receipt(
            "run_source",
            "call_source",
            "read_project",
            {"projectId": source_project},
            {"ok": True, "runId": "run_source"},
        )
        source_store.record_external_event(
            "event_source",
            source_project,
            "material_parse",
            {"projectId": source_project},
        )
        source_store.queue_steering(source_project, "run_source", "继续处理")
        session = f"project:{source_project}:run:run_source"
        _checkpoint(source_checkpoint, f"{session}:workflow:demo:state", "synthetic-checkpoint")

        payload = export_project_transfer(source_store, source_checkpoint, source_project)
    finally:
        source_store.close()

    serialized = json.dumps(payload, ensure_ascii=False)
    assert source_project not in serialized
    assert "synthetic-runtime-credential" not in serialized
    assert payload["tables"]["agent_runs"][0]["run_credential"] == ""
    assert payload["tables"]["agent_runs"][0]["token_fingerprint"] == ""
    assert len(payload["checkpoints"]) == 1

    target_store = RuntimeStore(tmp_path / "target-runtime.sqlite")
    target_checkpoint = tmp_path / "target-checkpoints.sqlite"
    try:
        import_project_transfer(target_store, target_checkpoint, "project_target_a", payload)
        import_project_transfer(target_store, target_checkpoint, "project_target_b", payload)

        runs = target_store._db.execute(  # noqa: SLF001 - migration invariant test
            "SELECT * FROM agent_runs ORDER BY project_id",
        ).fetchall()
        assert len(runs) == 2
        assert runs[0]["project_id"] == "project_target_a"
        assert runs[0]["id"] != "run_source"
        assert runs[0]["status"] == "suspended"
        assert runs[0]["run_credential"] == ""
        assert runs[0]["token_fingerprint"] == ""
        state = target_store._db.execute(  # noqa: SLF001
            "SELECT * FROM project_agent_state WHERE project_id = 'project_target_a'",
        ).fetchone()
        assert state["state"] == "intake:suspended"
        event = target_store._db.execute(  # noqa: SLF001
            "SELECT * FROM agent_events WHERE project_id = 'project_target_a'",
        ).fetchone()
        event_payload = json.loads(event["payload_json"])
        assert event_payload["projectId"] == "project_target_a"
        assert event_payload["runId"] == runs[0]["id"]

        checkpoint_database = sqlite3.connect(target_checkpoint)
        try:
            keys = [row[0] for row in checkpoint_database.execute("SELECT key FROM kv_store ORDER BY key")]
        finally:
            checkpoint_database.close()
        assert len(keys) == 2
        assert any(key.startswith(f"project:project_target_a:run:{runs[0]['id']}:") for key in keys)
        assert all("project_other" not in key for key in keys)

        delete_project_transfer(target_store, target_checkpoint, "project_target_a")
        assert target_store._db.execute(  # noqa: SLF001
            "SELECT 1 FROM agent_runs WHERE project_id = 'project_target_a'",
        ).fetchone() is None
        assert target_store._db.execute(  # noqa: SLF001
            "SELECT 1 FROM agent_runs WHERE project_id = 'project_target_b'",
        ).fetchone() is not None
    finally:
        target_store.close()


def test_agent_import_rejects_runtime_credentials(tmp_path: Path) -> None:
    source_store = RuntimeStore(tmp_path / "source-runtime.sqlite")
    try:
        source_store.create_run(_run("project_alpha"))
        source_store.update_run("run_source", status="completed")
        payload = export_project_transfer(
            source_store,
            tmp_path / "missing-checkpoints.sqlite",
            "project_alpha",
        )
    finally:
        source_store.close()
    payload["tables"]["agent_runs"][0]["run_credential"] = "not-allowed"

    target_store = RuntimeStore(tmp_path / "target-runtime.sqlite")
    try:
        with pytest.raises(AgentRuntimeError) as caught:
            import_project_transfer(
                target_store,
                tmp_path / "target-checkpoints.sqlite",
                "project_target",
                payload,
            )
        assert caught.value.code == "PROJECT_TRANSFER_SECRET"
    finally:
        target_store.close()


def test_agent_export_rejects_running_project(tmp_path: Path) -> None:
    store = RuntimeStore(tmp_path / "runtime.sqlite")
    try:
        store.create_run(_run("project_alpha"))
        store.update_run("run_source", status=RUN_RUNNING)
        with pytest.raises(AgentRuntimeError) as caught:
            export_project_transfer(
                store,
                tmp_path / "checkpoints.sqlite",
                "project_alpha",
            )
        assert caught.value.code == "PROJECT_TRANSFER_BUSY"
    finally:
        store.close()
