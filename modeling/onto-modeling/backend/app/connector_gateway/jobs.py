"""采集任务（证据采集接口 §4；ADR-0082）。

任务表只在网关内存里：网关重启就丢，内核查不到会把任务标 `gateway_restarted`。同时最多 MAX_ACTIVE 个任务没结束，
再多回 503。每个任务一个线程：轮询提供方的进度 → 完成后按预算收记录、写摘要和清单到网关 /tmp 下的任务目录 →
等内核取快照包、调 DELETE。结束超过 6 小时没清的任务自动清掉。
"""

from __future__ import annotations

import http.client
import json
import logging
import os
import re
import shutil
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, BinaryIO

from ..evidence.registry import SOURCE_ID_PATTERN
from . import bundle
from .errors import GatewayError

_log = logging.getLogger(__name__)

JOBS_DIR_ENV = "ONTO_CONNECTOR_JOBS_DIR"
JOB_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,79}$")
SOURCE_ID_RE = re.compile(SOURCE_ID_PATTERN)
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
ACTIVE = ("queued", "running")
TERMINAL = ("succeeded", "partial", "failed", "cancelled")
MAX_ACTIVE = 2
HARD_MAX_ROWS = 1_000_000
MAX_DURATION_S = 7200
JOB_TTL_S = 6 * 3600


@dataclass(eq=False)
class Job:
    id: str
    spec: dict
    budget: dict
    provider: Any
    directory: Path
    started: float = field(default_factory=time.time)
    state: str = "queued"
    phase: str = "接收程序"
    done: int = 0
    total: int = 0
    unit: str = ""
    error_code: str | None = None
    error_message: str | None = None
    ended: float | None = None
    deleted: bool = False
    members: list = field(default_factory=list)
    cancel: threading.Event = field(default_factory=threading.Event)
    wake: threading.Event = field(default_factory=threading.Event)

    def status(self) -> dict:
        return {"job_id": self.id, "state": self.state, "phase": self.phase, "done": self.done, "total": self.total,
                "unit": self.unit, "error_code": self.error_code, "error_message": self.error_message}


def _positive(value) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else None


class Jobs:
    def __init__(self, providers: dict, root: Path | str | None = None, *, max_active: int = MAX_ACTIVE,
                 poll_s: float = 1.0, ttl_s: float = JOB_TTL_S, grace_s: float = 60.0, unreachable_s: float = 30.0,
                 cancel_wait_s: float = 15.0):
        self.providers = providers
        self.root = Path(root or os.environ.get(JOBS_DIR_ENV) or Path(tempfile.gettempdir()) / "connector-jobs")
        self.max_active = max_active
        self.poll_s, self.ttl_s, self.grace_s = poll_s, ttl_s, grace_s
        self.unreachable_s, self.cancel_wait_s = unreachable_s, cancel_wait_s
        self._jobs: dict[str, Job] = {}
        self._lock = threading.Lock()

    # ---- 接口 ----

    def create(self, spec: dict, body: BinaryIO, length: int) -> dict:
        provider, budget = self._check(spec, length)
        self._sweep()
        job_id = spec["job_id"]
        with self._lock:
            if job_id in self._jobs:
                raise GatewayError(409, "job_exists", "任务已存在")
            if sum(job.state in ACTIVE for job in self._jobs.values()) >= self.max_active:
                raise GatewayError(503, "busy", "连接器网关繁忙")
            directory = self.root / job_id
            shutil.rmtree(directory, ignore_errors=True)
            directory.mkdir(parents=True, mode=0o700)
            job = Job(id=job_id, spec=spec, budget=budget, provider=provider, directory=directory)
            self._jobs[job_id] = job
        try:
            digest = provider.submit(spec, budget, body, length)
            artifact = spec.get("artifact")
            if artifact and digest != artifact["sha256"]:
                _quietly(provider.delete, job_id)
                raise GatewayError(400, "artifact_mismatch", "收到的程序和说明里的 sha256 对不上")
        except GatewayError:
            with self._lock:
                self._jobs.pop(job_id, None)
            shutil.rmtree(directory, ignore_errors=True)
            raise
        job.phase = "排队"
        _log.info("采集任务 %s 已提交 source=%s provider=%s bytes=%d", job_id, spec["source"]["id"], provider.id, length)
        threading.Thread(target=self._run, args=(job,), name=f"job-{job_id}", daemon=True).start()
        return job.status()

    def get(self, job_id: str) -> Job:
        self._sweep()
        with self._lock:
            job = self._jobs.get(job_id)
        if job is None:
            raise GatewayError(404, "job_not_found", "网关里没有这个任务")
        return job

    def cancel(self, job_id: str) -> dict:
        job = self.get(job_id)
        if job.state in ACTIVE:
            job.cancel.set()
            job.wake.set()
            job.phase = "取消中"
        elif job.state != "cancelled":
            raise GatewayError(409, "job_state", "任务已结束")
        return job.status()

    def delete(self, job_id: str) -> dict:
        with self._lock:
            job = self._jobs.pop(job_id, None)
        if job is None:
            raise GatewayError(404, "job_not_found", "网关里没有这个任务")
        job.deleted = True
        if job.state in ACTIVE:
            # 线程结束时清
            job.cancel.set()
            job.wake.set()
        else:
            self._purge(job)
        return {"job_id": job_id, "deleted": True}

    def bundle(self, job_id: str) -> tuple[list, int, Job]:
        job = self.get(job_id)
        if job.state not in ("succeeded", "partial"):
            raise GatewayError(409, "job_state", "任务没有成功结束，没有快照包")
        return job.members, bundle.tar_size(job.members), job

    # ---- 核对任务说明 ----

    def _check(self, spec: dict, length: int) -> tuple[Any, dict]:
        job_id, source = spec.get("job_id"), spec.get("source")
        if not isinstance(job_id, str) or not JOB_ID_RE.match(job_id):
            raise GatewayError(400, "bad_request", "任务 ID 不对")
        if not isinstance(source, dict) or not isinstance(source.get("id"), str) \
                or not SOURCE_ID_RE.match(source["id"]):
            raise GatewayError(400, "bad_request", "数据源 ID 不对")
        provider = self.providers.get(str(source.get("provider") or ""))
        if provider is None or not provider.installed():
            raise GatewayError(409, "provider_not_installed", "网关里没有装这个提供方")
        if spec.get("operation") not in provider.capabilities:
            raise GatewayError(400, "operation_not_supported", "提供方不支持这个操作")
        if not isinstance(spec.get("scope", []), list):
            raise GatewayError(400, "bad_request", "scope 要是列表")
        raw_budget = spec.get("budget") if isinstance(spec.get("budget"), dict) else {}
        limits = {"max_rows": HARD_MAX_ROWS, "max_bytes": bundle.RECORDS_CAP_BYTES, "max_duration_s": MAX_DURATION_S}
        budget = {}
        for key, cap in limits.items():
            value = _positive(raw_budget.get(key))
            if value is None:
                raise GatewayError(400, "bad_request", f"预算缺 {key}")
            budget[key] = min(value, cap)
        if provider.input != "artifact":
            if length:
                raise GatewayError(400, "size_mismatch", "这个提供方不收请求体")
            return provider, budget
        artifact = spec.get("artifact")
        if not isinstance(artifact, dict) or not isinstance(artifact.get("sha256"), str) \
                or not SHA256_RE.match(artifact["sha256"]) or _positive(artifact.get("size")) is None \
                or not isinstance(artifact.get("filename"), str) or not 0 < len(artifact["filename"]) <= 255:
            raise GatewayError(400, "bad_request", "artifact 要有 sha256、size、filename")
        if artifact["size"] > provider.max_artifact_bytes:
            raise GatewayError(413, "too_large", "目标程序超过上限")
        if length != artifact["size"]:
            raise GatewayError(400, "size_mismatch", "请求体大小和 artifact.size 不符")
        return provider, budget

    # ---- 任务线程 ----

    def _run(self, job: Job) -> None:
        try:
            state, status = self._follow(job)
            if state is None:
                return
            if state == "timeout":
                self._finish(job, "failed", "timeout", "超过时限")
            elif state == "cancelled" or job.cancel.is_set():
                self._finish(job, "cancelled")
            elif state == "failed":
                self._finish(job, "failed", str(status.get("error_code") or "provider_failed")[:64],
                             str(status.get("error_message") or "提供方没有完成")[:300])
            else:
                self._collect(job, partial=state == "partial")
        except Exception:  # noqa: BLE001 线程里兜底，任务标失败，网关照常
            _log.exception("采集任务 %s 出错", job.id)
            self._finish(job, "failed", "gateway_error", "网关处理任务出错")
        finally:
            if job.deleted:
                self._purge(job)

    def _follow(self, job: Job) -> tuple[str | None, dict]:
        """轮询提供方直到它结束；回 (结束状态, 最后一次进度)。已经在这里定了结局的回 (None, {})。"""
        deadline = job.started + job.budget["max_duration_s"] + self.grace_s
        cancel_sent: float | None = None
        timed_out = False
        last_ok = time.monotonic()
        status: dict = {}
        while True:
            if cancel_sent is None and (job.cancel.is_set() or time.time() > deadline):
                timed_out = not job.cancel.is_set()
                cancel_sent = time.monotonic()
                job.phase = "取消中"
                _quietly(job.provider.cancel, job.id)
            try:
                status = job.provider.status(job.id)
                last_ok = time.monotonic()
            except GatewayError as exc:
                if exc.status == 404:
                    self._finish(job, "failed", "provider_restarted", "旁路容器里的任务丢了")
                    return None, {}
                if time.monotonic() - last_ok > self.unreachable_s:
                    if cancel_sent is not None:
                        return ("timeout" if timed_out else "cancelled"), {}
                    self._finish(job, "failed", "provider_unavailable", "连不上旁路容器")
                    return None, {}
            else:
                state = status.get("state")
                if state in TERMINAL:
                    return ("timeout" if timed_out else state), status
                if cancel_sent is None:
                    job.state = "running" if state == "running" else job.state
                    job.phase = str(status.get("phase") or job.phase)[:40]
                    job.done, job.total = bundle.as_count(status.get("done")), bundle.as_count(status.get("total"))
                    job.unit = str(status.get("unit") or "")[:16]
            if cancel_sent is not None and time.monotonic() - cancel_sent > self.cancel_wait_s:
                return ("timeout" if timed_out else "cancelled"), status
            job.wake.wait(self.poll_s)
            job.wake.clear()

    def _collect(self, job: Job, *, partial: bool) -> None:
        """取结果说明和记录，写任务目录里的快照包文件。"""
        job.state, job.phase, job.done, job.total, job.unit = "running", "整理记录", 0, 0, "条"
        source_id = job.spec["source"]["id"]
        try:
            result = bundle.normalize_result(job.provider.result(job.id))
        except (GatewayError, bundle.ResultError) as exc:
            self._finish(job, "failed", "invalid_result", f"提供方的结果说明不对：{exc}")
            return
        job.total = sum(result["records"].values())
        writer = bundle.RecordWriter(job.directory, source_id, max_rows=job.budget["max_rows"],
                                     max_bytes=job.budget["max_bytes"])
        try:
            with job.provider.records(job.id) as stream:
                while not job.cancel.is_set():
                    line = stream.readline(bundle.MAX_LINE_BYTES + 1)
                    if not line:
                        # 带 Content-Length 的流提前断开时 readline 也回空，看剩余长度才知道没收全
                        if getattr(stream, "length", None):
                            raise GatewayError(502, "provider_error", "记录流没收全")
                        break
                    if len(line) > bundle.MAX_LINE_BYTES:
                        raise bundle.RecordError("一条记录超过 1 MiB")
                    if not writer.add(line):
                        break
                    job.done = writer.rows
        except bundle.RecordError as exc:
            writer.close()
            self._finish(job, "failed", "invalid_record", str(exc))
            return
        except (GatewayError, OSError, http.client.HTTPException):
            writer.close()
            self._finish(job, "failed", "provider_unavailable", "取记录时旁路容器断开")
            return
        files = writer.close()
        if job.cancel.is_set():
            self._finish(job, "cancelled")
            return
        reported = sum(result["records"].values())
        # 提供方说有的比收到的多：不完整（预算截断也是）
        complete = not partial and not writer.truncated and writer.rows >= reported
        coverage = result["coverage_pct"] if result["coverage_pct"] is not None else 100.0
        if reported > writer.rows:
            coverage = round(coverage * writer.rows / reported, 1)
        tool = {"name": "rea", "version": result["tool_version"]} if result["tool_version"] else job.provider.tool()
        artifact = job.spec.get("artifact") or {}
        summary = bundle.render_summary(
            source_id=source_id, digest=writer.digest, counts=writer.counts, complete=complete,
            result={**result, "artifact": {"detected": result["detected"], "filename": artifact.get("filename"),
                                           "size": artifact.get("size")}})
        files = {
            "inventory.json": bundle.write_file(job.directory / "inventory.json", _json_bytes(result["inventory"])),
            "summary.md": bundle.write_file(job.directory / "summary.md", summary.encode("utf-8")),
            **files,
        }
        ended = time.time()
        manifest = bundle.build_manifest(
            spec=job.spec, provider=f"{job.provider.id}@{job.provider.version}", tool=tool, started=job.started,
            ended=ended, complete=complete, coverage_pct=coverage, result=result, counts=writer.counts, files=files,
            rows=writer.rows, nbytes=writer.bytes)
        bundle.write_file(job.directory / "manifest.json", _json_bytes(manifest))
        job.members = bundle.members(job.directory, files, result["blobs"])
        self._finish(job, "succeeded" if complete else "partial", ended=ended)
        _log.info("采集任务 %s 完成 state=%s records=%d bytes=%d", job.id, job.state, writer.rows, writer.bytes)

    def _finish(self, job: Job, state: str, code: str | None = None, message: str | None = None,
                ended: float | None = None) -> None:
        job.ended = ended or time.time()
        job.error_code, job.error_message = code, message
        job.phase = {"succeeded": "完成", "partial": "完成（不完整）", "failed": "失败", "cancelled": "已取消"}[state]
        if state in ("failed", "cancelled"):
            # 没有快照包可取：网关和旁路容器的目录现在就清
            shutil.rmtree(job.directory, ignore_errors=True)
            _quietly(job.provider.delete, job.id)
            if state == "failed":
                _log.warning("采集任务 %s 失败：%s %s", job.id, code, message)
        job.state = state

    # ---- 清理 ----

    def _purge(self, job: Job) -> None:
        shutil.rmtree(job.directory, ignore_errors=True)
        _quietly(job.provider.delete, job.id)

    def _sweep(self) -> None:
        cutoff = time.time() - self.ttl_s
        with self._lock:
            expired = [job for job in self._jobs.values() if job.state in TERMINAL and (job.ended or 0) < cutoff]
            for job in expired:
                del self._jobs[job.id]
        for job in expired:
            self._purge(job)


def _json_bytes(value) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _quietly(action, *args) -> None:
    try:
        action(*args)
    except GatewayError as exc:
        _log.info("提供方清理/取消没成功：%s", exc.code)
