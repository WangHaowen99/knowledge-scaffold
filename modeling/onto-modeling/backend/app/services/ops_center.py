"""运维管控的任务中心、恢复演练、数据保留与导出器（契约 O-11、O-12、O-38～O-41、O-43、O-44）。

都存在数据目录 `ops/` 下（进备份）：
- `tasks.json`：任务记录（备份、包仓库备份、恢复演练），新的在前、最多 TASKS_MAX 条。任务在进程内跑，
  重启后仍在跑的改判失败（启动收敛）。
- `drills.json`：恢复演练记录。
- `retention.json`：各类数据的保留期，接到已有的清理逻辑（日志按天滚动、备份滚动覆盖、诊断包与通知到期删除）。
- `exporters.json`：导出器开关。只列本部署真有的：Prometheus（运维监听 /metrics）与本地日志文件。
"""

from __future__ import annotations

import json
import logging
import subprocess
import tarfile
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .. import config
from ..governance import telemetry
from . import background_jobs, backup

_log = logging.getLogger(__name__)
_LOCK = threading.Lock()

TASKS_MAX = 200
DRILLS_MAX = 50
TASK_TYPES = ("collection", "export", "backup", "sweep", "pack_install", "drill", "pack_repo_backup")
TASK_STATES = ("queued", "running", "succeeded", "partial", "failed", "cancelled")
LIVE_STATES = ("queued", "running")
DRILL_SAMPLE_DEFAULT = 5
DRILL_SAMPLE_MAX = 50
DIAGNOSTICS_KEEP_DAYS = 7
NOTIFICATIONS_KEEP_DAYS = 30


class OpsCenterError(Exception):
    def __init__(self, code: str, message: str, status: int = 400, details: dict | None = None):
        super().__init__(message)
        self.code, self.status, self.details = code, status, details


def _dir() -> Path:
    return Path(config.DATA_DIR) / "ops"


def _read(name: str, default):
    try:
        return json.loads((_dir() / name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write(name: str, data) -> None:
    path = _dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


# ---------------------------------------------------------------------------
# 任务中心（O-40、O-41）
# ---------------------------------------------------------------------------


def _tasks() -> background_jobs.JobStore:
    # JobStore 的状态字段叫 status；终态按契约的 TaskState
    return background_jobs.JobStore(_dir() / "tasks.json", list_key="tasks", max_records=TASKS_MAX,
                                    terminal_statuses=("succeeded", "partial", "failed", "cancelled"))


# 进程内：可取消的任务登记的取消标记
_cancel_flags: dict[str, threading.Event] = {}
# 重试要重跑的函数（按任务类型登记：任务记录里的 params → 再跑一遍）
_retry_handlers: dict[str, Callable[[dict, str], dict]] = {}


def register_retry(task_type: str, handler: Callable[[dict, str], dict]) -> None:
    _retry_handlers[task_type] = handler


def start_task(task_type: str, target: str, initiator: str, *, project_id: str | None = None,
               params: dict | None = None, cancellable: bool = False) -> dict:
    prefix = {"pack_repo_backup": "pack-backup"}.get(task_type, task_type.replace("_", "-"))
    record = _tasks().create(id=f"{prefix}-{uuid.uuid4().hex[:12]}", type=task_type, target=target,
                             project_id=project_id, initiator=initiator or "system",
                             status="running", progress_pct=0, started_at=time.time(), params=params or {}, error=None)
    if cancellable:
        _cancel_flags[record["id"]] = threading.Event()
    return record


def finish_task(task_id: str, *, state: str, error: str | None = None) -> dict | None:
    _cancel_flags.pop(task_id, None)
    return _tasks().update(task_id, status=state, error=error,
                           progress_pct=100 if state == "succeeded" else None)


def progress(task_id: str, pct: int) -> None:
    _tasks().update(task_id, progress_pct=max(0, min(100, int(pct))))


def cancelled(task_id: str) -> bool:
    flag = _cancel_flags.get(task_id)
    return bool(flag and flag.is_set())


def tracked(task_type: str, target: str, initiator: str, func: Callable[[], Any], **kwargs) -> tuple[dict, Any]:
    """同步跑一个任务并记进任务中心：成功记 succeeded，抛异常记 failed（异常照常抛出）。"""
    record = start_task(task_type, target, initiator, **kwargs)
    try:
        result = func()
    except Exception as exc:
        finish_task(record["id"], state="failed", error=str(exc)[:300] or type(exc).__name__)
        raise
    return finish_task(record["id"], state="succeeded") or record, result


def task_view(record: dict, project_view: Callable[[str], dict | None] | None = None) -> dict:
    from ..api import deps

    state = record.get("status") if record.get("status") in TASK_STATES else "failed"
    pct = record.get("progress_pct")
    return {
        "id": record["id"], "type": record.get("type"), "target": record.get("target") or "",
        "project": project_view(record["project_id"]) if project_view and record.get("project_id") else None,
        "initiator": record.get("initiator") or "system", "state": state,
        "progress_pct": int(pct) if isinstance(pct, (int, float)) else (100 if state == "succeeded" else 0),
        "started_at": deps.iso(record.get("started_at") or record.get("created_at")),
        "finished_at": deps.iso(record.get("completed_at")), "error": record.get("error"),
    }


def list_tasks(*, types: list[str] | None = None, states: list[str] | None = None) -> list[dict]:
    return [record for record in _tasks().list()
            if (not types or record.get("type") in types) and (not states or record.get("status") in states)]


def get_task(task_id: str) -> dict:
    record = _tasks().get(task_id)
    if record is None:
        raise OpsCenterError("task_not_found", "任务不存在", 404)
    return record


def act(task_id: str, op: str, actor: str) -> dict:
    """O-41：cancel 只对还在跑、能取消的任务；retry 只对失败的；resume 只有证据采集有（本期没有）。"""
    record = get_task(task_id)
    state = record.get("status")
    if op == "cancel":
        flag = _cancel_flags.get(task_id)
        if state not in LIVE_STATES or flag is None:
            raise OpsCenterError("task_state_invalid", "这个任务不能取消", 409, {"state": state})
        flag.set()
        return _tasks().update(task_id, status="cancelled", error=None) or record
    if op == "retry":
        handler = _retry_handlers.get(str(record.get("type")))
        if state not in ("failed", "partial") or handler is None:
            raise OpsCenterError("task_state_invalid", "这个任务不能重试", 409, {"state": state})
        return handler(record, actor)
    raise OpsCenterError("task_state_invalid", "这个任务不能续跑", 409, {"state": state})


def _sweep_interrupted() -> list[str]:
    _cancel_flags.clear()
    return _tasks().sweep_interrupted(status="failed", error="服务重启时中断")


background_jobs.register_startup_sweep("ops_tasks", _sweep_interrupted)


# ---------------------------------------------------------------------------
# 恢复演练（O-38、O-39）
# ---------------------------------------------------------------------------


def list_drills() -> list[dict]:
    items = _read("drills.json", [])
    return items if isinstance(items, list) else []


def last_drill() -> dict | None:
    from ..api import deps

    items = list_drills()
    return {**items[0], "at": deps.iso(items[0]["at"])} if items else None


def _record_drill(drill: dict) -> None:
    with _LOCK:
        _write("drills.json", [drill, *list_drills()][:DRILLS_MAX])


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "safe.directory=*", *args], cwd=cwd,
                          capture_output=True, text=True, timeout=120)


def _check_project(root: Path) -> str | None:
    """恢复出来的一个项目能不能打开、版本历史全不全：返回问题，没问题返回 None。"""
    try:
        meta = json.loads((root / ".onto-web.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "项目元数据读不出"
    if not isinstance(meta, dict) or not meta.get("id"):
        return "项目元数据不完整"
    if _git(root, "rev-parse", "--verify", "-q", "HEAD").returncode != 0:
        return "没有版本历史"
    if _git(root, "fsck", "--connectivity-only", "--no-progress").returncode != 0:
        return "版本历史不完整"
    return None


def run_drill(task_id: str, backup_name: str, sample: int) -> dict:
    """把一份备份恢复到临时目录，抽检 sample 个项目。结果记进演练记录与任务。"""
    started = time.time()
    path = backup.backups_dir() / backup_name
    problems: list[str] = []
    checked = 0
    try:
        with tempfile.TemporaryDirectory(prefix="onto-drill-") as scratch, tarfile.open(path, "r:gz") as tar:
            members = tar.getmembers()  # 读完整个包：包损坏在这里就会报错
            projects = sorted({parts[2] for parts in (member.name.split("/") for member in members)
                               if len(parts) > 2 and parts[0] == "data" and parts[1] == "workspaces"})
            chosen = projects[:sample]
            wanted = [member for member in members
                      if any(member.name == f"data/workspaces/{name}" or member.name.startswith(f"data/workspaces/{name}/")
                             for name in chosen)]
            progress(task_id, 30)
            if cancelled(task_id):
                _cancel_flags.pop(task_id, None)
                return {}
            tar.extractall(scratch, members=wanted, filter="data")
            for index, name in enumerate(chosen):
                if cancelled(task_id):
                    _cancel_flags.pop(task_id, None)
                    return {}
                problem = _check_project(Path(scratch) / "data" / "workspaces" / name)
                checked += 1
                if problem:
                    problems.append(f"{name}：{problem}")
                progress(task_id, 30 + int(70 * (index + 1) / max(1, len(chosen))))
    except (OSError, tarfile.TarError, EOFError) as exc:
        problems.append(f"备份读不出：{exc}"[:200])
    result = "failed" if problems else "passed"
    detail = "；".join(problems[:3]) if problems else f"抽检 {checked} 个项目"
    drill = {"id": task_id, "backup": backup_name, "at": started, "duration_ms": int((time.time() - started) * 1000),
             "result": result, "detail": detail}
    _record_drill(drill)
    finish_task(task_id, state="succeeded" if result == "passed" else "failed",
                error=None if result == "passed" else detail)
    return drill


def start_drill(backup_name: str, sample: int | None, actor: str) -> dict:
    """O-38：校验参数、记任务；真正的演练由调用方放到后台线程跑（run_drill）。"""
    names = {item["name"] for item in backup.list_backups()}
    if not backup_name or backup_name not in names:
        raise OpsCenterError("not_found", "备份不存在", 404, {"backup": backup_name})
    sample = DRILL_SAMPLE_DEFAULT if sample in (None, "") else sample
    if not isinstance(sample, int) or isinstance(sample, bool) or not 1 <= sample <= DRILL_SAMPLE_MAX:
        raise OpsCenterError("invalid_argument", f"sample_projects 须为 1–{DRILL_SAMPLE_MAX}", 400,
                             {"fields": {"sample_projects": "range"}})
    return start_task("drill", backup_name, actor, params={"backup": backup_name, "sample_projects": sample},
                      cancellable=True)


# ---------------------------------------------------------------------------
# 数据保留（O-43、O-44）
# ---------------------------------------------------------------------------

# id → (标题, 单位, 缺省值, 规则, 可改)；证据快照（snapshots）随证据源本期不出现，会话历史没有清理逻辑不列
RETENTION_SPECS = {
    "logs": ("结构化日志", "days", telemetry.LOG_KEEP_DAYS, "按天滚动", True),
    "backups": ("备份", "keep_last", backup.BACKUP_KEEP, "滚动覆盖最旧的一份", True),
    "diagnostics": ("诊断包", "days", DIAGNOSTICS_KEEP_DAYS, "到期删除", True),
    "notifications": ("通知", "days", NOTIFICATIONS_KEEP_DAYS, "到期删除", True),
    "audit": ("审计日志", "days", 365, "只追加", False),
    "trash": ("回收站", None, None, "不自动清除", False),
}
_LIMITS = {"days": (1, 3650), "keep_last": (1, 100)}


def retention_value(item_id: str) -> int:
    """某类数据现在的保留期（天数或份数）。"""
    _label, unit, default, _rule, _editable = RETENTION_SPECS[item_id]
    stored = _read("retention.json", {})
    value = (stored.get(item_id) or {}).get(unit) if isinstance(stored, dict) else None
    lo, hi = _LIMITS.get(unit, (1, 3650))
    return value if isinstance(value, int) and not isinstance(value, bool) and lo <= value <= hi else default


def _next_run(item_id: str) -> float | None:
    now = time.time()
    if item_id == "logs":  # 每天 UTC 零点滚动
        return (int(now // 86400) + 1) * 86400
    if item_id == "backups" and config.BACKUP_INTERVAL_HOURS > 0:  # 备份之后清掉多出来的
        return now + backup.seconds_until_due(config.BACKUP_INTERVAL_HOURS * 3600)
    return None


def retention_items() -> list[dict]:
    from ..api import deps

    items = []
    for item_id, (label, unit, _default, rule, editable) in RETENTION_SPECS.items():
        items.append({"id": item_id, "label": label,
                      "retention": {unit: retention_value(item_id)} if unit else {},
                      "rule": rule, "next_run_at": deps.iso(_next_run(item_id)), "editable": editable})
    return items


def save_retention(patch: list) -> list[dict]:
    """O-44：只改可编辑的项，单位按项固定；改完马上生效（日志滚动的保留天数、下一次清理按新值）。"""
    if not isinstance(patch, list) or not patch:
        raise OpsCenterError("invalid_argument", "items 不能为空", 400, {"fields": {"items": "required"}})
    problems, updates = {}, {}
    for item in patch:
        item_id = item.get("id") if isinstance(item, dict) else None
        spec = RETENTION_SPECS.get(item_id)
        if spec is None:
            problems[str(item_id)] = "unknown"
            continue
        if not spec[4]:
            problems[item_id] = "not_editable"
            continue
        unit = spec[1]
        value = (item.get("retention") or {}).get(unit) if isinstance(item.get("retention"), dict) else None
        lo, hi = _LIMITS[unit]
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            problems[item_id] = "out_of_range"
            continue
        updates[item_id] = {unit: value}
    if problems:
        raise OpsCenterError("invalid_argument", "保留期不合法", 400, {"fields": problems})
    with _LOCK:
        stored = _read("retention.json", {})
        stored = stored if isinstance(stored, dict) else {}
        stored.update(updates)
        _write("retention.json", stored)
    if "logs" in updates:
        telemetry.set_log_keep_days(updates["logs"]["days"])
    return retention_items()


# ---------------------------------------------------------------------------
# 导出器（O-11、O-12）
# ---------------------------------------------------------------------------


def prometheus_enabled() -> bool:
    stored = _read("exporters.json", {})
    return bool((stored.get("prometheus") or {}).get("enabled", True)) if isinstance(stored, dict) else True


def exporter_items() -> list[dict]:
    return [
        {"id": "prometheus", "label": "Prometheus", "implementation": "builtin", "target": "/metrics",
         "signals": ["metrics"], "enabled": prometheus_enabled(), "configured": True, "required": False},
        # 日志页读的就是它，不能关（required）；内置的两项地址与信号固定
        {"id": "file", "label": "本地文件", "implementation": "builtin",
         "target": f"{telemetry.log_dir().name}/{telemetry.LOG_FILE}", "signals": ["logs"], "enabled": True,
         "configured": True, "required": True},
    ]


def save_exporters(patch: list) -> list[dict]:
    """O-12：Prometheus 能关（运维监听的 /metrics 随之 404）；本地日志文件是日志页的数据源，不能关。"""
    if not isinstance(patch, list):
        raise OpsCenterError("invalid_argument", "items 要是数组", 400, {"fields": {"items": "array"}})
    known = {item["id"] for item in exporter_items()}
    problems = {}
    prometheus = None
    for item in patch:
        item_id = item.get("id") if isinstance(item, dict) else None
        if item_id not in known:
            problems[str(item_id)] = "unknown"
        elif not isinstance(item.get("enabled"), bool):
            problems[item_id] = "enabled"
        elif item_id == "file" and not item["enabled"]:
            problems[item_id] = "required"
        elif item_id == "prometheus":
            prometheus = item["enabled"]
    if problems:
        raise OpsCenterError("invalid_argument", "导出器设置不合法", 400, {"fields": problems})
    if prometheus is not None:
        with _LOCK:
            stored = _read("exporters.json", {})
            stored = stored if isinstance(stored, dict) else {}
            stored["prometheus"] = {"enabled": prometheus}
            _write("exporters.json", stored)
    return exporter_items()
