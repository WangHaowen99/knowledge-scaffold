"""后台任务公共件：进程内编排、持久记录，以及启动时把仍标 running 的改判中断。

沿用原有后台编排（求解端到端验证等）的做法，收成一处：

- **编排只活在进程内**（JobRunner）：同一个键（通常是项目 ID）同时只跑一个；同步函数放到
  线程里执行，任务对象留强引用，不会被垃圾回收中途取消；结束时无论成败都释放键。
- **进度和结论写持久记录**（JobStore）：一类任务一个 JSON 文件，新的在前、有条数上限，
  调用方经记录轮询。写入原子替换，读到的总是完整文件。
- **启动收敛**：进程重启后内存里没有任何编排，记录却可能永远停在 queued / running，把界面
  按钮永久锁死。各类任务在启动时登记收敛步骤（register_startup_sweep），lifespan 统一执行
  run_startup_sweeps，把这些记录改判中断。

后面的证据源层（采集任务）复用这里。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Iterable

logger = logging.getLogger(__name__)

STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_INTERRUPTED = "interrupted"
ACTIVE_STATUSES = (STATUS_QUEUED, STATUS_RUNNING)
TERMINAL_STATUSES = (STATUS_SUCCEEDED, STATUS_FAILED, STATUS_INTERRUPTED)
INTERRUPTED_MESSAGE = "任务被服务重启中断，请重新发起"
DEFAULT_MAX_RECORDS = 50

_store_locks: dict[str, threading.Lock] = {}
_store_locks_guard = threading.Lock()


def _store_lock(path: Path) -> threading.Lock:
    key = str(Path(path).absolute())
    with _store_locks_guard:
        lock = _store_locks.get(key)
        if lock is None:
            lock = _store_locks[key] = threading.Lock()
        return lock


class JobBusy(Exception):
    """同一个键已有任务在跑（接口层映射为 409）。"""


class JobStore:
    """一类任务的持久记录：{list_key: [记录...]}，新的在前，最多 max_records 条。

    记录至少有 id、status、created_at、completed_at；status 进入 terminal_statuses 时自动
    补 completed_at。其余字段由调用方决定，原样保存。
    """

    def __init__(
        self,
        path: Path,
        *,
        list_key: str = "jobs",
        max_records: int = DEFAULT_MAX_RECORDS,
        terminal_statuses: Iterable[str] = TERMINAL_STATUSES,
    ):
        self.path = Path(path)
        self.list_key = list_key
        self.max_records = max_records
        self.terminal_statuses = tuple(terminal_statuses)

    def list(self) -> list[dict]:
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        records = data.get(self.list_key) if isinstance(data, dict) else None
        return [item for item in records or [] if isinstance(item, dict)]

    def get(self, job_id: str) -> dict | None:
        return next((item for item in self.list() if item.get("id") == job_id), None)

    def latest(self, **match: Any) -> dict | None:
        """最新一条满足全部 字段=值 条件的记录；不给条件时就是最新一条。"""
        for item in self.list():
            if all(item.get(key) == value for key, value in match.items()):
                return item
        return None

    def create(self, **fields: Any) -> dict:
        now = int(time.time())
        record = {
            "id": uuid.uuid4().hex,
            "status": STATUS_QUEUED,
            "created_at": now,
            "completed_at": None,
            **fields,
        }
        if record["status"] in self.terminal_statuses and not record.get("completed_at"):
            record["completed_at"] = now
        with _store_lock(self.path):
            self._save([record, *self.list()])
        return record

    def update(self, job_id: str, **fields: Any) -> dict | None:
        with _store_lock(self.path):
            records = self.list()
            for item in records:
                if item.get("id") != job_id:
                    continue
                item.update(fields)
                if fields.get("status") in self.terminal_statuses:
                    item["completed_at"] = int(time.time())
                self._save(records)
                return item
        return None

    def sweep_interrupted(self, **fields: Any) -> list[str]:
        """把仍是 queued / running 的记录改判中断，返回改动的记录 ID。

        fields 缺省为 status=interrupted 加通用说明；求解验证这类沿用旧状态集的任务可传
        自己的终态与错误码。
        """
        fields = fields or {"status": STATUS_INTERRUPTED, "message": INTERRUPTED_MESSAGE}
        with _store_lock(self.path):
            records = self.list()
            touched = []
            for item in records:
                if item.get("status") in ACTIVE_STATUSES:
                    item.update(fields)
                    if fields.get("status") in self.terminal_statuses:
                        item["completed_at"] = int(time.time())
                    touched.append(str(item.get("id") or ""))
            if touched:
                self._save(records)
        return touched

    def _save(self, records: list[dict]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, temporary = tempfile.mkstemp(dir=self.path.parent, prefix=f".{self.path.name}.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(
                    {self.list_key: records[: self.max_records]}, handle, ensure_ascii=False, indent=2
                )
            os.replace(temporary, self.path)
        except BaseException:
            Path(temporary).unlink(missing_ok=True)
            raise


def sweep_workspace_stores(
    workspaces_dir: Path, rel_path: str, make_store: Callable[[Path], JobStore], **fields: Any
) -> list[str]:
    """各项目工作区里同一相对路径的任务记录统一收敛；返回有改动的项目 ID。"""
    base = Path(workspaces_dir)
    if not base.is_dir():
        return []
    touched = []
    for path in sorted(base.glob(f"*/{rel_path}")):
        root = path
        for _ in Path(rel_path).parts:
            root = root.parent
        if make_store(root).sweep_interrupted(**fields):
            touched.append(root.name)
    return touched


class JobRunner:
    """进程内编排：每个键同时只跑一个任务，同步函数在线程里执行。"""

    def __init__(self, name: str):
        self.name = name
        self._running: set[str] = set()
        self._tasks: set[asyncio.Task] = set()
        self._lock = threading.Lock()

    def busy(self, key: str) -> bool:
        with self._lock:
            return key in self._running

    def claim(self, key: str) -> None:
        """占住键；已被占用时抛 JobBusy。占住后必须 launch 或 release。"""
        with self._lock:
            if key in self._running:
                raise JobBusy(f"{self.name}：{key} 已有任务在运行")
            self._running.add(key)

    def release(self, key: str) -> None:
        with self._lock:
            self._running.discard(key)

    def launch(self, key: str, func: Callable[..., Any], *args: Any) -> asyncio.Task:
        """在当前事件循环里后台执行已 claim 的任务；结束时释放键，异常只记日志。"""

        async def run() -> Any:
            try:
                return await asyncio.to_thread(func, *args)
            except Exception:
                logger.exception("后台任务失败：%s %s", self.name, key)
                return None
            finally:
                self.release(key)

        try:
            task = asyncio.get_running_loop().create_task(run())
        except BaseException:
            self.release(key)
            raise
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def start(self, key: str, func: Callable[..., Any], *args: Any) -> asyncio.Task:
        self.claim(key)
        return self.launch(key, func, *args)

    async def wait_idle(self) -> None:
        """等进程内的任务全部结束（测试与停机用）。"""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


# ---- 启动收敛注册表 ----

_startup_sweeps: dict[str, Callable[[], Any]] = {}


def register_startup_sweep(name: str, sweep: Callable[[], Any]) -> None:
    """登记一类任务的启动收敛步骤；同名重复登记以后者为准。"""
    _startup_sweeps[name] = sweep


def run_startup_sweeps() -> dict[str, Any]:
    """依次执行全部收敛步骤。各步互不拖累：失败的记日志，结果里记为异常对象。"""
    results: dict[str, Any] = {}
    for name, sweep in list(_startup_sweeps.items()):
        try:
            results[name] = sweep()
        except Exception as exc:
            logger.exception("启动收敛失败：%s", name)
            results[name] = exc
    return results
