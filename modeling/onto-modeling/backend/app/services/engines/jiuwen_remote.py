"""内核这一侧的 Jiuwen worker 客户端（方案 B11；协议见 jiuwen_worker.py）。

`RemoteJiuwenEngine` 实现引擎协议（AgentEngine），会话层用法与进程内的 JiuwenEngine 一样；
全部会话共用一个 worker 子进程（`WorkerClient`，第一次连接时拉起）。worker 退出或崩溃时，
它上面每个会话都收到 `_engine_end`（出错），会话层按引擎异常结束处理（出错卡、执行记为中断）；
下一条消息连接时重拉一个新 worker。

模型配置指纹、上下文窗口这些不需要 worker 的在内核这边算；确认卡、续接点、错误留档经回调回到
内核，仍由会话层处理。部署配置 ONTO_WEB_JIUWEN_WORKER=false 时退回进程内运行。
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from ... import config
from .base import FRIENDLY_ERROR, EngineContext, ObservableEngine

_log = logging.getLogger(__name__)

BACKEND_DIR = Path(__file__).resolve().parents[3]
LINE_LIMIT = 64 * 1024 * 1024
DEFAULT_ENGINE = "app.services.engines.jiuwen:JiuwenEngine"
OP_TIMEOUT_SECONDS = 30
WORKER_GONE = "内测引擎的工作进程意外结束，本次调用未完成，请重试。"


class WorkerClient:
    """一个 worker 子进程与它上面的会话。"""

    def __init__(self, engine_path: str = DEFAULT_ENGINE):
        self.engine_path = engine_path
        self.process: asyncio.subprocess.Process | None = None
        self.sessions: dict[str, "RemoteJiuwenEngine"] = {}
        self.waiting: dict[str, asyncio.Future] = {}
        self.reader: asyncio.Task | None = None
        self.starts = 0
        self._lock: asyncio.Lock | None = None

    @property
    def alive(self) -> bool:
        return self.process is not None and self.process.returncode is None

    async def ensure(self) -> None:
        if self._lock is None:
            self._lock = asyncio.Lock()
        async with self._lock:
            if self.alive:
                return
            self.process = await asyncio.create_subprocess_exec(
                sys.executable, "-m", "app.services.engines.jiuwen_worker", "--engine", self.engine_path,
                "--data-dir", str(config.DATA_DIR), "--assets-dir", str(config.ASSETS_DIR),
                cwd=str(BACKEND_DIR), stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
                limit=LINE_LIMIT, env=dict(os.environ),
            )
            self.starts += 1
            self.reader = asyncio.create_task(self._read(self.process))

    async def write(self, message: dict) -> None:
        if not self.alive or self.process.stdin is None:
            raise RuntimeError(WORKER_GONE)
        self.process.stdin.write((json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8"))
        await self.process.stdin.drain()

    async def call(self, message: dict, timeout: float = OP_TIMEOUT_SECONDS) -> dict:
        """发一条要等 done 的操作。"""
        request_id = uuid.uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiting[request_id] = future
        try:
            await self.write({**message, "id": request_id})
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiting.pop(request_id, None)

    async def _read(self, process: asyncio.subprocess.Process) -> None:
        stdout = process.stdout
        try:
            while stdout is not None:
                line = await stdout.readline()
                if not line:
                    break
                try:
                    message = json.loads(line)
                except ValueError:
                    continue
                await self._dispatch(message)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 - 读坏了按 worker 没了处理
            _log.exception("读 Jiuwen worker 输出失败")
        await process.wait()
        if process is self.process:
            self._gone(process.returncode)

    async def _dispatch(self, message: dict) -> None:
        kind = message.get("event")
        if kind == "done":
            future = self.waiting.get(str(message.get("id")))
            if future is not None and not future.done():
                future.set_result(message)
            return
        engine = self.sessions.get(str(message.get("sid") or ""))
        if engine is None:
            return
        await engine._on_message(message)

    def _gone(self, code) -> None:
        """worker 退出或崩溃：它上面的会话一律结束（出错），等着的操作一律失败。"""
        _log.warning("Jiuwen worker 退出（退出码 %s），结束它上面的 %d 个会话", code, len(self.sessions))
        for engine in list(self.sessions.values()):
            engine._worker_gone()
        self.sessions.clear()
        for future in list(self.waiting.values()):
            if not future.done():
                future.set_exception(RuntimeError(WORKER_GONE))
        self.waiting.clear()

    async def stop(self) -> None:
        process, self.process = self.process, None
        if process is None or process.returncode is not None:
            return
        try:
            if process.stdin is not None:
                process.stdin.close()
            await asyncio.wait_for(process.wait(), 5)
        except (asyncio.TimeoutError, OSError):
            process.kill()
        if self.reader is not None:
            self.reader.cancel()


_client: WorkerClient | None = None


def client() -> WorkerClient:
    global _client
    if _client is None:
        _client = WorkerClient()
    return _client


def use_client(value: WorkerClient | None) -> None:
    """测试替换点：换一个（跑假引擎的）worker 客户端。"""
    global _client
    _client = value


async def stop() -> None:
    if _client is not None:
        await _client.stop()


class RemoteJiuwenEngine(ObservableEngine):
    """跑在 worker 里的 Jiuwen 引擎（AgentEngine 协议）。"""

    name = "jiuwen"

    @classmethod
    def available(cls) -> bool:
        from .jiuwen import JIUWEN_AVAILABLE

        return JIUWEN_AVAILABLE

    UNAVAILABLE_MESSAGE = "openjiuwen 未安装（需 Python 3.11+ 镜像），无法启动 Jiuwen 引擎会话"

    def __init__(self, ctx: EngineContext):
        self._ctx = ctx
        self.client: Any = None
        self.resume_attempted = False
        self.sid = uuid.uuid4().hex
        self._queue: asyncio.Queue = asyncio.Queue()
        self._connected: asyncio.Future | None = None
        self._limits: tuple[int | None, int | None] = (None, None)
        self._fingerprint = ""
        self._worker: WorkerClient | None = None

    # ---- 连接 ----

    async def connect(self) -> bool:
        worker = client()
        await worker.ensure()
        self._worker = worker
        worker.sessions[self.sid] = self
        self._connected = asyncio.get_running_loop().create_future()
        ctx = self._ctx
        payload = {
            "project_id": ctx.project_id, "cwd": str(ctx.cwd), "conversation_id": ctx.conversation_id,
            "model_owner": ctx.model_owner, "assembly_dir": str(ctx.assembly_dir) if ctx.assembly_dir else None,
            "engine_instance": ctx.engine_instance, "resume_token": ctx.load_resume_token(),
            "history": ctx.load_history() if ctx.load_history else [],
            # 宿主的注入状态不跨进程：worker 按这几项重建，装配换过版本的提醒要带过去
            "assembly_changed": bool(getattr(ctx.injector, "assembly_changed", False)),
        }
        await worker.write({"op": "connect", "sid": self.sid, "ctx": payload})
        message = await asyncio.wait_for(self._connected, OP_TIMEOUT_SECONDS * 4)
        if message.get("event") == "connect_failed":
            worker.sessions.pop(self.sid, None)
            raise RuntimeError(message.get("error") or "Jiuwen 引擎连接失败")
        self.client = self.sid
        self.resume_attempted = bool(message.get("resumed"))
        limits = message.get("limits") or [None, None]
        self._limits = (limits[0], limits[1])
        self._fingerprint = self.model_fingerprint()
        return self.resume_attempted

    async def _on_message(self, message: dict) -> None:
        kind = message.get("event")
        if kind in ("connected", "connect_failed"):
            if self._connected is not None and not self._connected.done():
                self._connected.set_result(message)
            return
        if kind == "engine":
            await self._queue.put(message.get("data") or {})
            return
        if kind == "rpc":
            asyncio.create_task(self._answer(message))
            return
        if kind == "save_resume_token":
            self._ctx.save_resume_token(message.get("token") or "")
        elif kind == "clear_resume_token":
            self._ctx.clear_resume_token()
        elif kind == "log_error":
            self._ctx.log_error(message.get("kind") or "session", message.get("text") or "")

    async def _answer(self, message: dict) -> None:
        try:
            result = await self._ctx.request_permission(*(message.get("args") or [None, {}, None, None]))
        except Exception as exc:  # noqa: BLE001 - 等不到答复按拒绝
            result = {"behavior": "deny", "message": str(exc) or "确认请求已失效"}
        try:
            await self._worker.write({"op": "rpc_result", "id": message.get("id"), "result": result})
        except RuntimeError:
            pass

    def _worker_gone(self) -> None:
        if self._connected is not None and not self._connected.done():
            self._connected.set_result({"event": "connect_failed", "error": WORKER_GONE})
        self.client = None
        self._ctx.log_error("jiuwen_worker", "Jiuwen worker 进程退出，会话随之结束")
        self._queue.put_nowait({"type": "_engine_end", "reason": "error", "error": FRIENDLY_ERROR})

    # ---- 协议 ----

    async def send(self, text: str, context: str | None = None) -> None:
        await self._worker.write({"op": "send", "sid": self.sid, "text": text, "context": context})

    async def _op(self, op: str) -> None:
        worker = self._worker
        if worker is None or not worker.alive or self.sid not in worker.sessions:
            return
        try:
            await worker.call({"op": op, "sid": self.sid})
        except (RuntimeError, asyncio.TimeoutError):
            pass

    async def interrupt(self) -> None:
        await self._op("interrupt")

    async def disconnect(self) -> None:
        await self._op("disconnect")
        if self._worker is not None:
            self._worker.sessions.pop(self.sid, None)
        self.client = None

    async def kill(self) -> None:
        await self._op("kill")
        if self._worker is not None:
            self._worker.sessions.pop(self.sid, None)
        self.client = None

    async def events(self):
        while True:
            event = await self._queue.get()
            self._notify(event)
            yield event
            if event.get("type") == "_engine_end":
                return

    def model_fingerprint(self) -> str:
        from .jiuwen import _fingerprint, model_config

        return _fingerprint(model_config(self._ctx.model_owner))

    def config_stale(self) -> bool:
        return bool(self._fingerprint) and self._fingerprint != self.model_fingerprint()

    def context_limits(self) -> tuple[int | None, int | None]:
        return self._limits
