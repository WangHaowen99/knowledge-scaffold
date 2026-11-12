"""自主构建的运行记录（数据目录，不进 git）：每次运行一份 JSON，含状态、进度、用量、决策台账与挂起的问题。

`DATA_DIR/autobuild/<项目>/runs/<run_id>.json`，策略在同一目录的 `policy.json`。台账与运行报告另在运行结束时
写进工作区 `reports/autobuild/<运行号>/`（进 git，见 runner）；这里是运行期间与复核时的工作副本——复核状态
（接受、改判）常改，不能每次都变成一个版本。
"""

from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
from contextlib import suppress
from pathlib import Path
from typing import Callable

from ... import config

# 还没结束的运行：同一项目同一时刻最多一个
ACTIVE = ("queued", "running", "pausing", "paused", "stopping", "converting", "interrupted")
# 占着平台并发名额的（在跑、或正走向安全点）
RUNNING = ("running", "pausing", "stopping", "converting")
# budget_exhausted 是 F6 之前「预算用完」停下的老运行，之后不再产生
ENDED = ("completed", "stopped", "budget_exhausted", "failed", "converted")
_lock = threading.RLock()


def root_dir() -> Path:
    return Path(config.DATA_DIR) / "autobuild"


def project_dir(project_id: str) -> Path:
    return root_dir() / project_id


def _runs_dir(project_id: str) -> Path:
    return project_dir(project_id) / "runs"


def _path(project_id: str, run_id: str) -> Path:
    return _runs_dir(project_id) / f"{run_id}.json"


def write_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".tmp-", suffix=".json")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=1)
        os.replace(temporary, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temporary)


def read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def get(project_id: str, run_id: str) -> dict | None:
    if not run_id or "/" in run_id or run_id.startswith("."):
        return None
    with _lock:
        return read_json(_path(project_id, run_id))


def save(record: dict) -> dict:
    with _lock:
        write_json(_path(record["project_id"], record["run_id"]), record)
    return record


def update(project_id: str, run_id: str, change: Callable[[dict], None]) -> dict | None:
    """读、改、写在同一把锁里：change(record) 原地改。运行器与接口（复核、控制）都经这里改运行记录。"""
    with _lock:
        record = read_json(_path(project_id, run_id))
        if record is None:
            return None
        change(record)
        write_json(_path(project_id, run_id), record)
        return record


def runs(project_id: str) -> list[dict]:
    """本项目全部运行，新的在前。"""
    directory = _runs_dir(project_id)
    with _lock:
        try:
            paths = list(directory.glob("*.json"))
        except OSError:
            return []
        items = [record for record in (read_json(path) for path in paths) if record]
    return sorted(items, key=lambda record: int(record.get("run_no") or 0), reverse=True)


def active(project_id: str) -> dict | None:
    return next((record for record in runs(project_id) if record.get("state") in ACTIVE), None)


def all_runs(states: tuple[str, ...] | None = None) -> list[dict]:
    """全部项目的运行（运维总览、并发名额、启动恢复）。"""
    base = root_dir()
    try:
        projects = [path.name for path in base.iterdir() if path.is_dir()]
    except OSError:
        return []
    items = [record for project_id in projects for record in runs(project_id)]
    return [record for record in items if states is None or record.get("state") in states]


def next_run_no(project_id: str) -> int:
    return max((int(record.get("run_no") or 0) for record in runs(project_id)), default=0) + 1


def find_entry(project_id: str, entry_id: str) -> tuple[dict, dict] | None:
    """台账条目所在的运行与条目本身。"""
    for record in runs(project_id):
        for entry in record.get("ledger") or []:
            if entry.get("id") == entry_id:
                return record, entry
    return None


def find_suspension(project_id: str, suspension_id: str) -> tuple[dict, dict] | None:
    for record in runs(project_id):
        for item in record.get("suspensions") or []:
            if item.get("id") == suspension_id:
                return record, item
    return None


def purge(project_id: str) -> None:
    with _lock:
        shutil.rmtree(project_dir(project_id), ignore_errors=True)


def ensure_no_run(project_id: str, *, conversation_id: str | None = None) -> None:
    """有运行没结束时，改引擎、发消息、发命令、回退版本、删项目等都回 409 autobuild_running（契约 2.10）。

    conversation_id 给了时只拦这次运行的对话（打断、重启、删对话）。"""
    from ...api.errors import ApiError

    run = active(project_id)
    if run is None or (conversation_id is not None and run.get("conversation_id") != conversation_id):
        return
    raise ApiError("autobuild_running", "自主构建运行中", 409, {"run_id": run["run_id"]})
