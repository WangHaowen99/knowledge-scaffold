"""在业务进程的事件循环里起运维监听（方案 E8：会话池在内存里，必须同进程）。

由 run.py 设 `ONTO_WEB__OPS_LISTENER=1` 启用（测试与被别的程序 import 时不起监听、不占端口）；
`ONTO_WEB_OPS_PORT=0` 关闭。信号由业务面的 uvicorn 处理，这里不装信号处理器。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os

import uvicorn

from .. import config

_log = logging.getLogger(__name__)
ENABLE_ENV = "ONTO_WEB__OPS_LISTENER"


class _EmbeddedServer(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield

    def install_signal_handlers(self) -> None:  # 旧版 uvicorn 的接口
        return None


def enabled() -> bool:
    return os.environ.get(ENABLE_ENV) == "1" and int(config.OPS_PORT or 0) > 0


async def start():
    """起运维监听，返回 (server, task)；没启用时 None。"""
    if not enabled():
        return None
    from ..ops_app import ops_app

    server = _EmbeddedServer(uvicorn.Config(
        ops_app, host=config.OPS_HOST, port=int(config.OPS_PORT), lifespan="off", log_config=None,
        timeout_graceful_shutdown=3,
    ))
    task = asyncio.create_task(server.serve())
    _log.info("运维管理面监听 %s:%s", config.OPS_HOST, config.OPS_PORT)
    return server, task


async def stop(handle) -> None:
    if not handle:
        return
    server, task = handle
    server.should_exit = True
    try:
        await asyncio.wait_for(task, timeout=5)
    except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
        task.cancel()
