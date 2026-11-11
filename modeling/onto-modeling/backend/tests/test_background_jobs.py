"""后台任务公共件：持久记录、进程内编排去重、启动时把 running 改判中断。fixture 全部为合成数据。"""

import asyncio
import json
import threading
from pathlib import Path

import pytest

from app.services import background_jobs
from app.services.background_jobs import JobBusy, JobRunner, JobStore


def test_store_create_update_and_terminal_timestamp(tmp_path):
    store = JobStore(tmp_path / "state" / "jobs.json")
    job = store.create(kind="collect", project="p1")
    assert job["status"] == background_jobs.STATUS_QUEUED and job["completed_at"] is None
    store.update(job["id"], status=background_jobs.STATUS_RUNNING, message="采集中")
    assert store.get(job["id"])["completed_at"] is None
    done = store.update(job["id"], status=background_jobs.STATUS_SUCCEEDED)
    assert done["completed_at"] and store.latest(project="p1")["id"] == job["id"]
    assert store.update("missing", status="failed") is None


def test_store_keeps_newest_first_within_limit(tmp_path):
    store = JobStore(tmp_path / "jobs.json", max_records=3)
    ids = [store.create(n=index)["id"] for index in range(5)]
    assert [item["id"] for item in store.list()] == ids[::-1][:3]


def test_store_reads_corrupt_file_as_empty(tmp_path):
    path = tmp_path / "jobs.json"
    path.write_text("{oops", encoding="utf-8")
    assert JobStore(path).list() == []


def test_sweep_marks_active_records_interrupted(tmp_path):
    store = JobStore(tmp_path / "jobs.json")
    queued = store.create()
    running = store.create(status=background_jobs.STATUS_RUNNING)
    finished = store.create(status=background_jobs.STATUS_SUCCEEDED)

    assert sorted(store.sweep_interrupted()) == sorted([queued["id"], running["id"]])
    statuses = {item["id"]: item for item in store.list()}
    assert statuses[queued["id"]]["status"] == background_jobs.STATUS_INTERRUPTED
    assert statuses[running["id"]]["completed_at"]
    assert statuses[finished["id"]]["status"] == background_jobs.STATUS_SUCCEEDED
    assert store.sweep_interrupted() == []  # 幂等


def test_runner_dedupes_per_key_and_releases_after_finish():
    runner = JobRunner("collect")
    gate = threading.Event()
    done = []

    def work(value):
        gate.wait(5)
        done.append(value)
        return value

    async def scenario():
        task = runner.start("p1", work, "first")
        assert runner.busy("p1")
        with pytest.raises(JobBusy):
            runner.start("p1", work, "second")
        other = runner.start("p2", work, "other")
        gate.set()
        assert await task == "first" and await other == "other"
        await runner.wait_idle()
        assert not runner.busy("p1")
        assert await runner.start("p1", work, "again") == "again"

    asyncio.run(scenario())
    assert sorted(done) == ["again", "first", "other"]


def test_runner_failure_is_logged_and_releases_key(caplog):
    runner = JobRunner("collect")

    def boom():
        raise RuntimeError("合成失败")

    async def scenario():
        assert await runner.start("p1", boom) is None
        assert not runner.busy("p1")

    asyncio.run(scenario())
    assert "后台任务失败" in caplog.text


def test_launch_without_loop_releases_claim():
    runner = JobRunner("collect")
    runner.claim("p1")
    with pytest.raises(RuntimeError):
        runner.launch("p1", lambda: None)
    assert not runner.busy("p1")


def test_startup_sweeps_run_independently(monkeypatch):
    monkeypatch.setattr(background_jobs, "_startup_sweeps", {})
    calls = []

    def broken():
        raise OSError("合成错误")

    background_jobs.register_startup_sweep("broken", broken)
    background_jobs.register_startup_sweep("ok", lambda: calls.append("ok") or ["p1"])
    results = background_jobs.run_startup_sweeps()
    assert isinstance(results["broken"], OSError)
    assert results["ok"] == ["p1"] and calls == ["ok"]


def test_sweep_workspace_stores_skips_missing_dir(tmp_path: Path):
    assert background_jobs.sweep_workspace_stores(
        tmp_path / "absent", "state/jobs.json", lambda root: JobStore(root / "state/jobs.json"),
    ) == []
