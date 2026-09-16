# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

import json
import os
import tempfile

import pytest
from solver_coordination.verification.service import (
    ContentAddressedSolverArtifactStore,
    SolverArtifactStoreError,
)


def test_content_addressed_artifact_store_deduplicates_and_verifies_reads(tmp_path):
    store = ContentAddressedSolverArtifactStore(tmp_path / "artifacts")
    payload = {
        "schema_version": "solver.synthetic-artifact.v1",
        "status": "verified",
        "evidence": [{"kind": "synthetic_proof"}],
    }

    first = store.put(artifact_type="verification_evidence", payload=payload)
    second = store.put(artifact_type="verification_evidence", payload=payload)

    assert second == first
    assert first["artifact_ref"].startswith("artifact://verification_evidence/")
    assert store.get(first["artifact_ref"]) == payload
    assert len(tuple((tmp_path / "artifacts").rglob("*.json"))) == 1


def test_content_addressed_artifact_store_rejects_tampered_content(tmp_path):
    store = ContentAddressedSolverArtifactStore(tmp_path / "artifacts")
    record = store.put(
        artifact_type="solver_result",
        payload={"schema_version": "solver.result.v1", "status": "feasible"},
    )
    digest_hex = record["artifact_ref"].rsplit("/", 1)[-1]
    target = tmp_path / "artifacts" / "solver_result" / "{}.json".format(digest_hex)
    envelope = json.loads(target.read_text(encoding="utf-8"))
    envelope["payload"]["status"] = "optimal"
    target.write_text(json.dumps(envelope), encoding="utf-8")

    with pytest.raises(SolverArtifactStoreError, match="integrity"):
        store.get(record["artifact_ref"])
    with pytest.raises(SolverArtifactStoreError, match="different content"):
        store.put(
            artifact_type="solver_result",
            payload={"schema_version": "solver.result.v1", "status": "feasible"},
        )


@pytest.mark.parametrize(
    "reference",
    [
        "",
        "file:///tmp/result.json",
        "artifact://solver_result/../escape",
        "artifact://solver_result/not-a-digest",
        "artifact://solver_result/" + "a" * 64 + "/extra",
    ],
)
def test_content_addressed_artifact_store_rejects_unsafe_references(tmp_path, reference):
    store = ContentAddressedSolverArtifactStore(tmp_path / "artifacts")

    with pytest.raises(SolverArtifactStoreError, match="reference"):
        store.get(reference)


def test_content_addressed_artifact_store_closes_unclaimed_temp_descriptor(
    tmp_path,
    monkeypatch,
):
    store = ContentAddressedSolverArtifactStore(tmp_path / "artifacts")
    temporary = tmp_path / "unclaimed-artifact.tmp"
    temporary.write_bytes(b"")
    descriptor = 91
    closed: list[int] = []

    monkeypatch.setattr(
        tempfile,
        "mkstemp",
        lambda **_kwargs: (descriptor, str(temporary)),
    )

    def fail_fdopen(*_args, **_kwargs):
        raise OSError("synthetic fdopen failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "close", closed.append)

    with pytest.raises(SolverArtifactStoreError, match="persist"):
        store.put(
            artifact_type="solver_result",
            payload={"schema_version": "solver.result.v1", "status": "feasible"},
        )

    assert closed == [descriptor]
    assert not temporary.exists()


def test_content_addressed_artifact_store_removes_temp_when_descriptor_close_fails(
    tmp_path,
    monkeypatch,
):
    store = ContentAddressedSolverArtifactStore(tmp_path / "artifacts")
    temporary = tmp_path / "close-failure-artifact.tmp"
    temporary.write_bytes(b"")

    monkeypatch.setattr(
        tempfile,
        "mkstemp",
        lambda **_kwargs: (92, str(temporary)),
    )

    def fail_fdopen(*_args, **_kwargs):
        raise OSError("synthetic fdopen failure")

    def fail_close(_descriptor):
        raise OSError("synthetic close failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    monkeypatch.setattr(os, "close", fail_close)

    with pytest.raises(SolverArtifactStoreError, match="persist"):
        store.put(
            artifact_type="solver_result",
            payload={"schema_version": "solver.result.v1", "status": "feasible"},
        )

    assert not temporary.exists()
