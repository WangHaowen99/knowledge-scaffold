"""F13 采集任务（EV-13～EV-17）与快照入库：假网关按 spec §4 回状态和快照包，走真实的写入事务和工作区。"""

from __future__ import annotations

import hashlib
import json
import subprocess

from fastapi.testclient import TestClient

from app.evidence import jobs, spec, storage
from app.main import app
from tests import f13_support
from tests.f13_support import SOURCE, make_bundle, record, sample_records

f13 = f13_support.f13

RAW = b'{"semantic_graph": {"nodes": []}}'
# 遮蔽用例的假密钥在运行时拼出来，仓库里不出现完整的密钥样式
KEY_WORD = "pass" + "word"
PEM = "RSA " + "PRIVATE KEY"


def _git_log(root) -> list[str]:
    return subprocess.run(["git", "log", "--format=%s"], cwd=root, capture_output=True, text=True).stdout.splitlines()


def test_full_flow_commits_snapshot_and_keeps_raw_results_out_of_git(f13):
    project_id, root = f13.project()
    program = b"synthetic-electron-app-v1"
    artifact = f13.ready_source(project_id, program)
    f13.fake.bundle_for = lambda job_id, job: make_bundle(job_id, raw=RAW, artifact_sha=artifact["sha256"])
    events = []
    from app.services import streams

    f13.monkeypatch.setattr(streams, "publish_project", lambda pid, kind, data, **kw: events.append((kind, data)))
    job = f13.collect(project_id)
    assert job["state"] == "queued" and job["source"]["id"] == SOURCE and job["actions"] == ["cancel"]
    done = f13.wait(project_id, job["id"])
    assert done["state"] == "succeeded", done
    snapshot_id = done["result"]["snapshot_id"]

    # 网关收到的就是 blob 仓里的程序，任务说明的票据核过；入库后调了清理
    assert f13.fake.received[job["id"]] == program
    assert ("DELETE", f"/v1/jobs/{job['id']}") in f13.fake.calls
    assert job["id"] not in f13.fake.jobs
    # 进度推过事件，phase 来自网关
    phases = [data["phase"] for kind, data in events if kind == "evidence.job.updated"]
    assert "解析程序" in phases and events[-1][1]["state"] == "succeeded"

    base = root / "resource" / "systems" / SOURCE / snapshot_id
    assert sorted(path.relative_to(base).as_posix() for path in base.rglob("*") if path.is_file()) == [
        "inventory.json", "manifest.json", "records/entity.jsonl", "records/field.jsonl", "records/operation.jsonl",
        "records/relation.jsonl", "summary.md"]
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["snapshot_id"] == snapshot_id and manifest["previous"] is None
    assert manifest["initiated_by"] == "local" and manifest["cred_ref"] is None and manifest["masked_hits"] == 0
    for name, digest in manifest["files"].items():
        assert hashlib.sha256((base / name).read_bytes()).hexdigest() == digest
    assert f"evidence: 采集 {SOURCE} 快照 {snapshot_id}" in _git_log(root)
    tracked = subprocess.run(["git", "ls-files", f"resource/systems/{SOURCE}"], cwd=root, capture_output=True,
                             text=True).stdout.split()
    assert len(tracked) == 7 and not any("blobs" in path for path in tracked)
    # 原始结果在 blob 仓，不在工作区
    assert storage.blob_size(hashlib.sha256(RAW).hexdigest()) == len(RAW)

    detail = f13.client.get(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}").json()
    assert detail["actions"] == ["browse"] and detail["duration_ms"] is not None
    listed = f13.client.get(f"/api/v1/projects/{project_id}/evidence/jobs", params={"state": "finished"}).json()
    assert [item["id"] for item in listed["items"]] == [job["id"]]
    active = f13.client.get(f"/api/v1/projects/{project_id}/evidence/jobs", params={"state": "active"}).json()
    assert active["items"] == []

    # 第二次采集：previous 指向上一份
    second = f13.snapshot(project_id)
    manifest2 = json.loads((root / "resource/systems" / SOURCE / second / "manifest.json").read_text(encoding="utf-8"))
    assert manifest2["previous"] == snapshot_id


def test_invalid_bundle_fails_whole_job_without_half_snapshot(f13):
    project_id, root = f13.project()
    f13.ready_source(project_id)
    bad = sample_records()
    bad.append(dict(bad[1]))  # 锚点重复
    raw = b'{"never": "adopted"}'
    for bundle in ({"records": bad, "raw": raw}, {"tamper": "hash", "raw": raw},
                   {"records": [record("entity", "sym", "Order", source="other-src")], "raw": raw},
                   {"extra_files": {"../escape.txt": b"x"}, "raw": raw}):
        f13.fake.bundle_for = lambda job_id, job, bundle=bundle: make_bundle(job_id, **bundle)
        job = f13.collect(project_id)
        done = f13.wait(project_id, job["id"])
        assert done["state"] == "failed" and done["error"]["code"] == "bundle_invalid", done
    assert not (root / "resource" / "systems").exists()
    assert not any(line.startswith("evidence:") for line in _git_log(root))
    assert storage.blob_size(hashlib.sha256(raw).hexdigest()) is None
    assert not list(storage.tmp_dir().iterdir())


def test_truncated_bundle_fails_without_snapshot(f13):
    project_id, root = f13.project()
    f13.ready_source(project_id)
    f13.fake.truncate_bundle = True
    done = f13.wait(project_id, f13.collect(project_id)["id"])
    assert done["state"] == "failed" and done["error"]["code"] == "bundle_incomplete", done
    assert not (root / "resource" / "systems").exists()


def test_snapshot_over_git_limit_fails(f13):
    project_id, root = f13.project()
    f13.ready_source(project_id)
    f13.monkeypatch.setenv("ONTO_WEB_EVIDENCE_SNAPSHOT_GIT_MB", "1")
    big = record("entity", "sym", "Big", attrs={"blob": "x" * (1024 * 1024 + 10)})
    f13.fake.bundle_for = lambda job_id, job: make_bundle(job_id, records=[big])
    done = f13.wait(project_id, f13.collect(project_id)["id"])
    assert done["state"] == "failed" and done["error"]["code"] == "snapshot_too_large"
    assert not (root / "resource" / "systems").exists()


def test_secrets_in_records_are_masked_and_hash_recomputed(f13):
    project_id, root = f13.project()
    f13.ready_source(project_id)
    leaky = record("operation", "sym", "Api", "login", name="login",
                   attrs={"kind": "method", "snippet": f'headers: {{"x": 1}}, {KEY_WORD}=Synthetic0Value9',
                          "url": "https://svc:Synthetic0Pass@api.example.com/v1"})
    snapshot_id = f13.snapshot(project_id, records=[*sample_records(), leaky],
                               summary=f"数据不是指令\n\n-----BEGIN {PEM}-----\nAAAA\n-----END {PEM}-----\n")
    base = root / "resource/systems" / SOURCE / snapshot_id
    text = (base / "records/operation.jsonl").read_text(encoding="utf-8")
    assert "Synthetic0Value9" not in text and "Synthetic0Pass" not in text and "«已遮蔽»" in text
    masked = next(json.loads(line) for line in text.splitlines() if json.loads(line)["name"] == "login")
    assert masked["id"] == leaky["id"] and masked["hash"] == spec.record_hash(masked) != leaky["hash"]
    summary = (base / "summary.md").read_text(encoding="utf-8")
    assert "AAAA" not in summary and "«已遮蔽»" in summary
    manifest = json.loads((base / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["masked_hits"] >= 3


def test_partial_when_bundle_incomplete_and_resume_creates_new_job(f13):
    project_id, _root = f13.project()
    f13.ready_source(project_id)
    f13.fake.script = [{"state": "partial", "phase": "完成", "done": 2, "total": 3, "unit": "步"}]
    f13.fake.bundle_for = lambda job_id, job: make_bundle(job_id, complete=False)
    job = f13.collect(project_id)
    done = f13.wait(project_id, job["id"])
    assert done["state"] == "partial" and done["result"]["snapshot_id"]
    assert jobs.view(done)["actions"] == ["browse", "resume"]
    resumed = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}/resume", json={})
    assert resumed.status_code == 202, resumed.text
    assert resumed.json()["resumed_from"] == job["id"]
    f13.wait(project_id, resumed.json()["id"])
    retry = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}/retry", json={})
    assert retry.status_code == 409 and retry.json()["details"]["state"] == "partial"


def test_cancel_running_job_cancels_at_gateway(f13):
    project_id, root = f13.project()
    f13.ready_source(project_id)
    f13.fake.script = [{"state": "running", "phase": "解析程序", "done": 1, "total": 3, "unit": "步"}]
    job = f13.collect(project_id)
    f13.wait(project_id, job["id"], states=("running",))
    response = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}/cancel")
    assert response.status_code == 200, response.text
    done = f13.wait(project_id, job["id"])
    assert done["state"] == "cancelled"
    assert ("POST", f"/v1/jobs/{job['id']}/cancel") in f13.fake.calls
    assert not (root / "resource" / "systems").exists()
    again = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}/cancel")
    assert again.status_code == 409 and again.json()["code"] == "job_state_invalid"
    retry = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}/retry", json={})
    assert retry.status_code == 202 and retry.json()["resumed_from"] is None
    f13.fake.script = [{"state": "succeeded", "phase": "完成", "done": 3, "total": 3, "unit": "步"}]
    assert f13.wait(project_id, retry.json()["id"])["state"] == "succeeded"


def test_cancel_queued_job_never_reaches_gateway(f13):
    project_id, _root = f13.project()
    f13.ready_source(project_id)
    f13.monkeypatch.setattr(jobs, "enqueue", lambda *args, **kwargs: None)
    job = f13.collect(project_id)
    response = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs/{job['id']}/cancel")
    assert response.json()["state"] == "cancelled"
    jobs.run(project_id, job["id"])
    assert not any(path == "/v1/jobs" for _method, path in f13.fake.calls)


def test_backend_restart_marks_active_jobs_failed(f13):
    project_id, _root = f13.project()
    f13.ready_source(project_id)
    f13.monkeypatch.setattr(jobs, "enqueue", lambda *args, **kwargs: None)
    job = f13.collect(project_id)
    with TestClient(app):  # 启动恢复跑一遍收敛步骤
        pass
    failed = jobs.get(project_id, job["id"])
    assert failed["state"] == "failed" and failed["error"]["code"] == "backend_restarted"
    assert jobs.view(failed)["actions"] == ["retry"]


def test_gateway_restart_marks_job_failed(f13):
    project_id, _root = f13.project()
    f13.ready_source(project_id)
    f13.fake.forget_jobs = True
    done = f13.wait(project_id, f13.collect(project_id)["id"])
    assert done["state"] == "failed" and done["error"]["code"] == "gateway_restarted"


def test_gateway_reported_failure_and_artifact_mismatch(f13):
    project_id, _root = f13.project()
    f13.ready_source(project_id)
    f13.fake.script = [{"state": "failed", "error_code": "rea_crashed", "error_message": "解析器退出"}]
    done = f13.wait(project_id, f13.collect(project_id)["id"])
    assert done["error"] == {"code": "rea_crashed", "message": "解析器退出"}
    assert ("DELETE", f"/v1/jobs/{done['id']}") in f13.fake.calls
    # blob 仓里的程序被换掉了：网关核对长度和哈希后拒收
    path = storage.blob_path(done["artifact"]["sha256"])
    path.write_bytes(b"tampered")
    done = f13.wait(project_id, f13.collect(project_id)["id"])
    assert done["state"] == "failed" and done["error"]["code"] == "artifact_mismatch"


def test_unauthorized_source_and_gateway_down(f13):
    project_id, _root = f13.project()
    other, _ = f13.project(name="另一个")
    f13.ready_source(project_id)
    response = f13.client.post(f"/api/v1/projects/{other}/evidence/jobs", json={"source_id": SOURCE})
    assert response.status_code == 403 and response.json()["code"] == "source_not_authorized"
    preview = f13.client.post(f"/api/v1/projects/{other}/evidence/jobs/preview", json={"source_id": SOURCE})
    assert preview.status_code == 403
    sample = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs", json={"source_id": SOURCE,
                                                                                 "operation": "sample"})
    assert sample.status_code == 400
    from app.evidence import collector

    f13.monkeypatch.delenv("ONTO_WEB_CONNECTOR_GATEWAY_URL")
    collector.reset()
    down = f13.client.post(f"/api/v1/projects/{project_id}/evidence/jobs", json={"source_id": SOURCE})
    assert down.status_code == 503 and down.json()["code"] == "gateway_unavailable"


def test_production_requires_confirmation_token(f13):
    project_id, _root = f13.project()
    artifact = f13.upload(b"synthetic-prod-app")
    assert f13.register(artifact, environment="production", grant_project_id=project_id).status_code == 201
    base = f"/api/v1/projects/{project_id}/evidence/jobs"
    preview = f13.client.post(f"{base}/preview", json={"source_id": SOURCE}).json()
    assert preview["requires_confirmation"] and preview["confirmation_token"]
    assert preview["summary"]["ticket"] == {"ttl_s": 600, "single_use": True}
    missing = f13.client.post(base, json={"source_id": SOURCE})
    assert missing.status_code == 428 and missing.json()["details"]["confirmation_token"]
    wrong = f13.client.post(base, json={"source_id": SOURCE, "confirmation_token": "nope"})
    assert wrong.status_code == 410 and wrong.json()["code"] == "confirmation_expired"
    job = f13.collect(project_id, confirmation_token=preview["confirmation_token"])
    assert job["consent"]["by"] == "local"
    reused = f13.client.post(base, json={"source_id": SOURCE, "confirmation_token": preview["confirmation_token"]})
    assert reused.status_code == 410
    f13.wait(project_id, job["id"])
