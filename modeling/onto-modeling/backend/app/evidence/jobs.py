"""采集任务（契约 EV-13～EV-17、EP-13；`design/evidence-collection.md` §6「任务」）。

任务记录 `DATA_DIR/evidence/jobs/<项目>/<任务>.json`。EV-14 建好放进后台队列（全平台同时跑 2 个），线程里：
签票据 → 从 blob 仓把目标程序流式发给网关 `POST /v1/jobs` → 每 2 秒查一次进度、推 `evidence.job.updated` →
成功取快照包、入库 → 调网关清理。后端重启时还在排队、运行的任务标 `failed`（`backend_restarted`）；网关查不到任务
（网关重启过）标 `failed`（`gateway_restarted`），都可以重试。
"""

from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from ..api.errors import ApiError
from . import collector, ingest, sources, storage

_log = logging.getLogger(__name__)

POLL_S = 2.0
WORKERS = 2
GIVE_UP_S = 60               # 网关连续这么久不通，任务判失败
GRACE_S = 120                # 超过预算时长再给的余量（网关自己也按预算杀）
WINDOW_CHECK_S = 30
ACTIVE = ("queued", "running")
FINISHED = ("succeeded", "partial", "failed", "cancelled")
EVENT = "evidence.job.updated"
OPERATIONS = ("discover", "snapshot", "sample")
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

_lock = threading.RLock()
_queue: "queue.Queue[tuple[str, str]]" = queue.Queue()
_threads: list[threading.Thread] = []
_cancel: dict[str, threading.Event] = {}
_waiting: set[tuple[str, str]] = set()   # 等访问窗口的任务


def _manager():
    from ..api import deps

    return deps.workspaces


# ---------------------------------------------------------------------------
# 存取
# ---------------------------------------------------------------------------


def _dir(project_id: str) -> Path:
    return storage.root() / "jobs" / project_id


def _path(project_id: str, job_id: str) -> Path:
    return _dir(project_id) / f"{job_id}.json"


def get(project_id: str, job_id: str) -> dict | None:
    if not job_id.startswith("ej-") or "/" in job_id or ".." in job_id:
        return None
    job = storage.read_json(_path(project_id, job_id), None)
    return job if isinstance(job, dict) and job.get("id") == job_id else None


def require(project_id: str, job_id: str) -> dict:
    job = get(project_id, job_id)
    if job is None:
        raise ApiError("job_not_found", "采集任务不存在", 404)
    return job


def list_jobs(project_id: str) -> list[dict]:
    folder = _dir(project_id)
    jobs = [storage.read_json(path, None) for path in folder.glob("ej-*.json")] if folder.is_dir() else []
    return sorted((job for job in jobs if isinstance(job, dict) and job.get("id")),
                  key=lambda job: job.get("created_at") or 0, reverse=True)


def all_jobs() -> list[dict]:
    base = storage.root() / "jobs"
    out: list[dict] = []
    for folder in sorted(base.iterdir()) if base.is_dir() else []:
        if folder.is_dir():
            out += list_jobs(folder.name)
    return out


def active_for_source(source_id: str) -> list[dict]:
    return [job for job in all_jobs() if job["source"]["id"] == source_id and job["state"] in ACTIVE]


def last_for_source(source_id: str, project_id: str | None = None) -> dict | None:
    jobs = list_jobs(project_id) if project_id else all_jobs()
    matching = [job for job in jobs if job["source"]["id"] == source_id]
    return max(matching, key=lambda job: job.get("created_at") or 0) if matching else None


def _save(job: dict) -> None:
    storage.write_json(_path(job["project_id"], job["id"]), job)


def _update(project_id: str, job_id: str, **changes) -> dict | None:
    with _lock:
        job = get(project_id, job_id)
        if job is None:
            return None
        job.update(changes)
        _save(job)
    _publish(job)
    return job


def _publish(job: dict) -> None:
    try:
        from ..services import streams

        streams.publish_project(job["project_id"], EVENT, view(job))
    except Exception:  # noqa: BLE001 - 事件是增强信息
        _log.exception("采集任务事件发送失败")


# ---------------------------------------------------------------------------
# 视图（CollectionJob）
# ---------------------------------------------------------------------------


def view(job: dict) -> dict:
    from ..api import deps

    state = job["state"]
    progress = job.get("progress")
    if isinstance(progress, dict) and progress.get("total"):
        progress = {**progress, "pct": round(100.0 * progress.get("done", 0) / progress["total"], 1)}
    elif isinstance(progress, dict):
        progress = {**progress, "pct": 0.0}
    actions = []
    if state in ACTIVE:
        actions.append("cancel")
    if state in ("succeeded", "partial") and (job.get("result") or {}).get("snapshot_id"):
        actions.append("browse")
    if state == "partial":
        actions.append("resume")
    if state in ("failed", "cancelled"):
        actions.append("retry")
    if state == "failed" and not job.get("self_service"):
        actions.append("notify_admin")
    started, ended = job.get("started_at"), job.get("ended_at")
    consent = job.get("consent")
    return {
        "id": job["id"], "project_id": job["project_id"], "source": job["source"], "operation": job["operation"],
        "scope_text": job.get("scope_text") or "", "incremental_base": job.get("incremental_base"),
        "state": state, "phase": job.get("phase"), "progress": progress, "queue_reason": job.get("queue_reason"),
        "started_at": deps.iso(started), "ended_at": deps.iso(ended),
        "duration_ms": int((ended - started) * 1000) if started and ended else None,
        "result": job.get("result"), "error": job.get("error"), "resumed_from": job.get("resumed_from"),
        "checkpoint": False, "initiated_by": job.get("initiated_by") or "",
        "consent": {"by": consent["by"], "at": deps.iso(consent["at"])} if consent else None,
        "actions": actions,
    }


# ---------------------------------------------------------------------------
# 访问窗口
# ---------------------------------------------------------------------------


def in_window(window: dict | None, now: float | None = None) -> bool:
    if not window:
        return True
    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(str(window.get("tz") or "UTC"))
    except Exception:  # noqa: BLE001 - 时区写错按 UTC
        zone = timezone.utc
    moment = datetime.fromtimestamp(now if now is not None else time.time(), zone)
    start, end = window["start"], window["end"]
    clock = moment.strftime("%H:%M")
    if start <= end:
        inside, day = start <= clock < end, moment
    else:  # 跨午夜：凌晨那一段算前一天的窗口
        inside = clock >= start or clock < end
        day = moment - timedelta(days=1) if clock < end else moment
    days = window.get("days", "daily")
    if days == "daily":
        return inside
    allowed = {str(item).lower()[:3] for item in days}
    return inside and (WEEKDAYS[day.weekday()] in allowed or str(day.isoweekday()) in allowed)


# ---------------------------------------------------------------------------
# 建任务、取消、重试
# ---------------------------------------------------------------------------


def _budget(registration: dict, override) -> dict:
    budget = dict(registration.get("budget") or sources.SELF_SERVICE_BUDGET)
    if isinstance(override, dict):
        for key in ("max_rows", "max_bytes", "max_duration_s"):
            value = override.get(key)
            if isinstance(value, int) and not isinstance(value, bool) and 0 < value <= budget.get(key, value):
                budget[key] = value  # 只能往小调
    return budget


def create(project_id: str, registration: dict, *, operation: str, scope: list[str], budget, by: str,
           consent: dict | None, incremental_base: str | None = None, resumed_from: str | None = None) -> dict:
    """建任务并排队（不在访问窗口里就等窗口）。"""
    connection = registration.get("connection") or {}
    artifact = None
    if "artifact_sha256" in connection:
        size = storage.blob_size(str(connection["artifact_sha256"]))
        if size is None:
            raise ApiError("artifact_not_found", "目标程序不在 blob 仓里，重新上传后再采集", 422)
        artifact = {"sha256": connection["artifact_sha256"], "size": size,
                    "filename": str(connection.get("artifact_filename") or "")}
    now = time.time()
    job = {
        "id": f"ej-{uuid.uuid4().hex[:16]}", "project_id": project_id, "source": sources.ref(registration),
        "provider": registration["provider"], "self_service": bool(registration.get("self_service")),
        "operation": operation, "scope": list(scope),
        "scope_text": "、".join(scope[:5]) or sources.scope_summary(registration),
        "incremental_base": incremental_base, "budget": _budget(registration, budget), "artifact": artifact,
        "state": "queued", "phase": None, "progress": None, "queue_reason": None,
        "created_at": now, "started_at": None, "ended_at": None, "result": None, "error": None,
        "resumed_from": resumed_from, "initiated_by": by, "consent": consent, "access": None,
    }
    if not in_window(registration.get("access_window")):
        window = registration["access_window"]
        job["queue_reason"] = f"等待访问窗口 {window['start']}"
    with _lock:
        _save(job)
    _publish(job)
    enqueue(project_id, job["id"], waiting=bool(job["queue_reason"]))
    return job


def cancel(project_id: str, job_id: str) -> dict:
    with _lock:
        job = require(project_id, job_id)
        if job["state"] not in ACTIVE:
            raise ApiError("job_state_invalid", "任务已经结束", 409, {"state": job["state"]})
        _cancel_event(job_id).set()
        if job["state"] == "queued":
            job.update(state="cancelled", ended_at=time.time(), phase=None, queue_reason=None)
            _save(job)
            _waiting.discard((project_id, job_id))
    _publish(job)
    return job


def _cancel_event(job_id: str) -> threading.Event:
    with _lock:
        return _cancel.setdefault(job_id, threading.Event())


def sweep_interrupted() -> dict:
    """启动收敛：上次进程里还在排队、运行的任务都标失败（任务线程只活在进程内）。"""
    count = 0
    for job in all_jobs():
        if job["state"] in ACTIVE:
            job.update(state="failed", ended_at=time.time(), phase=None, queue_reason=None,
                       error={"code": "backend_restarted", "message": "后端重启，任务中断，可以重试"})
            _save(job)
            count += 1
    return {"failed": count}


# ---------------------------------------------------------------------------
# 后台执行
# ---------------------------------------------------------------------------


def enqueue(project_id: str, job_id: str, *, waiting: bool = False) -> None:
    _start_workers()
    with _lock:
        if waiting:
            _waiting.add((project_id, job_id))
            return
    _queue.put((project_id, job_id))


def _start_workers() -> None:
    with _lock:
        alive = [thread for thread in _threads if thread.is_alive()]
        _threads[:] = alive
        for number in range(len(alive), WORKERS):
            thread = threading.Thread(target=_worker, name=f"evidence-job-{number}", daemon=True)
            thread.start()
            _threads.append(thread)
        if not any(thread.name == "evidence-window" for thread in _threads):
            thread = threading.Thread(target=_window_loop, name="evidence-window", daemon=True)
            thread.start()
            _threads.append(thread)


def _worker() -> None:
    while True:
        project_id, job_id = _queue.get()
        try:
            run(project_id, job_id)
        except Exception:  # noqa: BLE001 - 一个任务出错不能让线程退出
            _log.exception("采集任务出错：%s", job_id)
            _fail(project_id, job_id, "internal_error", "采集出错，可以重试")
        finally:
            _queue.task_done()


def _window_loop() -> None:
    while True:
        time.sleep(WINDOW_CHECK_S)
        release_waiting()


def release_waiting() -> int:
    """访问窗口开了的任务放进队列。"""
    released = 0
    with _lock:
        waiting = list(_waiting)
    for project_id, job_id in waiting:
        job = get(project_id, job_id)
        registration = sources.get(job["source"]["id"]) if job else None
        if job is None or job["state"] != "queued" or registration is None:
            with _lock:
                _waiting.discard((project_id, job_id))
            continue
        if in_window(registration.get("access_window")):
            with _lock:
                _waiting.discard((project_id, job_id))
            _update(project_id, job_id, queue_reason=None)
            _queue.put((project_id, job_id))
            released += 1
    return released


def _fail(project_id: str, job_id: str, code: str, message: str) -> dict | None:
    job = get(project_id, job_id)
    if job is None or job["state"] in FINISHED:
        return job
    return _update(project_id, job_id, state="failed", ended_at=time.time(), phase=None,
                   error={"code": code, "message": message[:300]})


def job_spec(job: dict, registration: dict) -> dict:
    """网关 `POST /v1/jobs` 的任务说明（spec §4 JobSpec）。"""
    artifact_input = job.get("artifact") is not None
    spec = {"job_id": job["id"], "operation": job["operation"],
            "source": {"id": registration["id"], "provider": registration["provider"],
                       "connection": {} if artifact_input else dict(registration.get("connection") or {})},
            "scope": list(job.get("scope") or []), "budget": dict(job["budget"])}
    if artifact_input:
        spec["artifact"] = dict(job["artifact"])
    return spec


def _progress(status: dict) -> dict | None:
    total = status.get("total")
    if not isinstance(total, int) or total <= 0:
        return None
    done = status.get("done") if isinstance(status.get("done"), int) else 0
    return {"done": done, "total": total, "unit": str(status.get("unit") or "")}


def _bundle_cap(job: dict) -> int:
    return ingest.git_limit_bytes() + int(job["budget"].get("max_bytes") or 0) + 16 * 1024 * 1024


def run(project_id: str, job_id: str) -> None:
    """执行一个排队中的任务，直到结束（同步；后台线程与测试都用它）。"""
    job = get(project_id, job_id)
    if job is None or job["state"] != "queued":
        return
    cancelled = _cancel_event(job_id)
    if cancelled.is_set():
        return
    registration = sources.get(job["source"]["id"])
    if registration is None:
        _fail(project_id, job_id, "source_not_found", "数据源已删除")
        return
    artifact = None
    if job.get("artifact"):
        size = storage.blob_size(job["artifact"]["sha256"])
        if size is None:
            _fail(project_id, job_id, "artifact_not_found", "目标程序不在 blob 仓里，重新上传后再采集")
            return
        artifact = storage.blob_path(job["artifact"]["sha256"])
    budget = job["budget"]
    started = time.time()
    job = _update(project_id, job_id, state="running", started_at=started, phase="提交任务", queue_reason=None)
    try:
        status = collector.submit(job_spec(job, registration), artifact)
    except collector.GatewayUnavailable:
        _fail(project_id, job_id, "gateway_unavailable", "连接器网关不可达")
        return
    except collector.GatewayError as exc:
        _fail(project_id, job_id, exc.code, exc.message)
        return

    unreachable_since = None
    cancel_sent = False
    last = None
    while True:
        state = str(status.get("state") or "")
        snapshot = (state, status.get("phase"), status.get("done"), status.get("total"))
        if snapshot != last and state in ACTIVE:
            _update(project_id, job_id, phase=status.get("phase") or None, progress=_progress(status))
        last = snapshot
        if state in ("succeeded", "partial", "failed", "cancelled"):
            break
        if cancelled.is_set() and not cancel_sent:
            cancel_sent = _try(lambda: collector.cancel(job_id, budget))
        if time.time() - started > budget.get("max_duration_s", 1800) + GRACE_S:
            _try(lambda: collector.cancel(job_id, budget))
            _cleanup(job_id, budget)
            _fail(project_id, job_id, "timeout", "超过预算时长")
            return
        cancelled.wait(POLL_S)
        try:
            status = collector.status(job_id, budget)
            unreachable_since = None
        except collector.GatewayError as exc:
            if exc.status == 404:
                _fail(project_id, job_id, "gateway_restarted", "连接器网关重启过，任务丢了，可以重试")
                return
            _cleanup(job_id, budget)
            _fail(project_id, job_id, exc.code, exc.message)
            return
        except collector.GatewayUnavailable:
            unreachable_since = unreachable_since or time.time()
            if time.time() - unreachable_since > GIVE_UP_S:
                _fail(project_id, job_id, "gateway_unavailable", "连接器网关不可达")
                return
            status = {"state": "running", "phase": job.get("phase")}

    # 先让网关清目录再落最终状态：任务显示结束时网关那边也收拾完了
    if state == "cancelled" or cancelled.is_set():
        _cleanup(job_id, budget)
        _update(project_id, job_id, state="cancelled", ended_at=time.time(), phase=None)
        return
    if state == "failed":
        _cleanup(job_id, budget)
        _fail(project_id, job_id, str(status.get("error_code") or "provider_failed"),
              str(status.get("error_message") or "采集失败"))
        return
    outcome = _collect(project_id, job_id, registration, state)
    _cleanup(job_id, budget)
    if outcome.get("state") == "failed":
        _fail(project_id, job_id, outcome["code"], outcome["message"])
    else:
        _update(project_id, job_id, **outcome)


def _collect(project_id: str, job_id: str, registration: dict, state: str) -> dict:
    """取快照包、入库；返回任务最终要写的字段（失败时 `{state: failed, code, message}`）。"""
    job = _update(project_id, job_id, phase="入库", progress=None)
    bundle = storage.tmp_dir() / f"{job_id}-{uuid.uuid4().hex[:6]}.tar"
    try:
        collector.bundle(job_id, job["budget"], bundle, _bundle_cap(job))
        if _cancel_event(job_id).is_set():
            return {"state": "cancelled", "ended_at": time.time(), "phase": None}
        result = ingest.ingest(_manager(), project_id, registration, job, bundle)
    except collector.GatewayUnavailable:
        return {"state": "failed", "code": "gateway_unavailable", "message": "取快照包时连接器网关不可达"}
    except (collector.GatewayError, ingest.IngestError) as exc:
        return {"state": "failed", "code": exc.code, "message": exc.message}
    finally:
        bundle.unlink(missing_ok=True)
    try:
        from ..services import project_events

        project_events.schedule_flow_refresh(project_id)  # 取证节点有一份完整快照就算完成
    except Exception:  # noqa: BLE001 - 推送是增强信息
        _log.exception("流程刷新失败")
    manifest = result["manifest"]
    complete = bool(manifest.get("complete")) and state == "succeeded"
    used = manifest.get("budget_used") if isinstance(manifest.get("budget_used"), dict) else {}
    counts = manifest.get("records") or {}
    message = f"快照 {result['snapshot_id']}：{sum(int(value or 0) for value in counts.values())} 条记录"
    if manifest.get("masked_hits"):
        message += f"，遮蔽 {manifest['masked_hits']} 处疑似密钥"
    return {"state": "succeeded" if complete else "partial", "ended_at": time.time(), "phase": None,
            "result": {"snapshot_id": result["snapshot_id"], "message": message, "commit": result["commit"]},
            "access": {"rows": int(used.get("records") or 0), "bytes": int(used.get("bytes") or 0),
                       "duration_ms": int(float(used.get("duration_s") or 0) * 1000),
                       "artifact_sha256": (job.get("artifact") or {}).get("sha256")}}


def _try(call) -> bool:
    try:
        call()
        return True
    except (collector.GatewayUnavailable, collector.GatewayError):
        return False


def _cleanup(job_id: str, budget: dict) -> None:
    """网关和旁路容器清目录；不通也没关系，旁路容器 6 小时后自己清。"""
    _try(lambda: collector.delete(job_id, budget))
    with _lock:
        _cancel.pop(job_id, None)
