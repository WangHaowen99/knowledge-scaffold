"""Jiuwen 独立 worker 进程（详设 4.2 目标 6、8.3 措施 5；方案 B11）：内核把 Jiuwen 会话放进这个子进程，
一个 worker 承载多个会话；它崩了不拖垮内核，内核给它上面的会话发结束事件、下次连接重拉一个。

协议：stdin / stdout 各一行一个 JSON（stdout 只给协议用，库自己的 print 改走 stderr）。

内核 → worker：
- `{"op": "connect", "sid", "ctx": {...}}`：建引擎并连接；ctx 是 EngineContext 里能序列化的部分，
  加连接时的续接 token 与事件历史快照；
- `{"op": "send", "sid", "text", "context"}`、`{"op": "interrupt" | "disconnect" | "kill", "sid", "id"}`；
- `{"op": "rpc_result", "id", "result"}`：worker 发起的回调（确认卡）的答复。

worker → 内核：
- `{"event": "connected", "sid", "resumed", "limits"}` / `{"event": "connect_failed", "sid", "error"}`；
- `{"event": "engine", "sid", "data": 平台事件}`：引擎产出的事件，原样转发（含 `_engine_end`）；
- `{"event": "rpc", "sid", "id", "method": "request_permission", "args"}`：确认卡，等内核答复；
- `{"event": "save_resume_token" | "clear_resume_token" | "log_error", "sid", ...}`：回调通知；
- `{"event": "done", "id"}`：interrupt / disconnect / kill 做完。

引擎类默认是 JiuwenEngine，`--engine 模块:类` 可换（测试用假引擎）。
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import sys
import threading
import uuid
from pathlib import Path

LINE_LIMIT = 64 * 1024 * 1024


class Worker:
    def __init__(self, engine_class, out):
        self.engine_class = engine_class
        self.out = out
        self.engines: dict[str, object] = {}
        self.pumps: dict[str, asyncio.Task] = {}
        self.pending: dict[str, asyncio.Future] = {}
        self._lock = threading.Lock()  # 引擎的回调可能在线程里调（留档、续接点），整行写出去

    def emit(self, message: dict) -> None:
        line = (json.dumps(message, ensure_ascii=False, default=str) + "\n").encode("utf-8")
        with self._lock:
            self.out.write(line)
            self.out.flush()

    def _context(self, sid: str, data: dict):
        from .base import EngineContext

        history = list(data.get("history") or [])
        resume_token = data.get("resume_token")

        async def request_permission(tool_name, tool_input, title, description):
            request_id = uuid.uuid4().hex
            future = asyncio.get_running_loop().create_future()
            self.pending[request_id] = future
            self.emit({"event": "rpc", "sid": sid, "id": request_id, "method": "request_permission",
                       "args": [tool_name, tool_input, title, description]})
            try:
                return await future
            finally:
                self.pending.pop(request_id, None)

        def save(token):
            self.emit({"event": "save_resume_token", "sid": sid, "token": token})

        def clear():
            self.emit({"event": "clear_resume_token", "sid": sid})

        def log_error(kind, text):
            self.emit({"event": "log_error", "sid": sid, "kind": kind, "text": str(text)})

        assembly = data.get("assembly_dir")
        return EngineContext(
            project_id=data["project_id"],
            cwd=Path(data["cwd"]),
            request_permission=request_permission,
            load_resume_token=lambda: resume_token,
            save_resume_token=save,
            clear_resume_token=clear,
            log_error=log_error,
            load_history=lambda: history,
            conversation_id=data.get("conversation_id") or "default",
            model_owner=data.get("model_owner") or "",
            assembly_dir=Path(assembly) if assembly else None,
            engine_instance=data.get("engine_instance") or "",
        )

    async def handle(self, message: dict) -> None:
        op = message.get("op")
        sid = str(message.get("sid") or "")
        if op == "rpc_result":
            future = self.pending.get(str(message.get("id")))
            if future is not None and not future.done():
                future.set_result(message.get("result") or {"behavior": "deny", "message": "确认请求已失效"})
            return
        if op == "connect":
            await self._connect(sid, message.get("ctx") or {})
            return
        engine = self.engines.get(sid)
        if op == "send":
            if engine is not None:
                await engine.send(message.get("text") or "", message.get("context"))
            return
        if op in ("interrupt", "disconnect", "kill"):
            if engine is not None:
                try:
                    await getattr(engine, op)()
                except Exception as exc:  # noqa: BLE001 - 结果照常回，错误留档
                    self.emit({"event": "log_error", "sid": sid, "kind": f"worker_{op}", "text": str(exc)})
            if op in ("disconnect", "kill"):
                self.engines.pop(sid, None)
                pump = self.pumps.pop(sid, None)
                if pump is not None:
                    pump.cancel()
            self.emit({"event": "done", "id": message.get("id")})

    async def _connect(self, sid: str, data: dict) -> None:
        try:
            engine = self.engine_class(self._context(sid, data))
            resumed = await engine.connect()
        except Exception as exc:  # noqa: BLE001 - 连接失败交给内核按出错处理
            self.emit({"event": "connect_failed", "sid": sid, "error": str(exc)})
            return
        self.engines[sid] = engine
        self.pumps[sid] = asyncio.create_task(self._pump(sid, engine))
        self.emit({"event": "connected", "sid": sid, "resumed": bool(resumed), "limits": list(engine.context_limits())})

    async def _pump(self, sid: str, engine) -> None:
        async for event in engine.events():
            self.emit({"event": "engine", "sid": sid, "data": event})
            if event.get("type") == "_engine_end":
                self.engines.pop(sid, None)
                return


async def serve(engine_class, reader: asyncio.StreamReader, out) -> None:
    worker = Worker(engine_class, out)
    tasks: set[asyncio.Task] = set()
    while True:
        line = await reader.readline()
        if not line:
            break  # 内核关了 stdin（停机或自己退出）：worker 跟着退
        try:
            message = json.loads(line)
        except ValueError:
            continue
        # 每条消息独立处理：一个会话在等确认卡时，别的会话照常收发
        task = asyncio.create_task(worker.handle(message))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
    for engine in list(worker.engines.values()):
        try:
            await engine.kill()
        except Exception:  # noqa: BLE001
            pass


def _load(path: str):
    module, _, name = path.partition(":")
    return getattr(importlib.import_module(module), name)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--engine", default="app.services.engines.jiuwen:JiuwenEngine")
    parser.add_argument("--data-dir", default="")
    parser.add_argument("--assets-dir", default="")
    args = parser.parse_args(argv)
    # stdout 只给协议用：先复制一份，再把 1 号描述符指到 stderr，库里的 print 不会搅乱协议
    protocol = os.fdopen(os.dup(1), "wb", buffering=0)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    from ... import config

    if args.data_dir:
        config.DATA_DIR = Path(args.data_dir)
    if args.assets_dir:
        config.ASSETS_DIR = Path(args.assets_dir)
    engine_class = _load(args.engine)

    async def run() -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=LINE_LIMIT)
        await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
        await serve(engine_class, reader, protocol)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
