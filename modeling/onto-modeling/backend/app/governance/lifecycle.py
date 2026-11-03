"""进程生命周期记录（详设 8.3 措施 7）：上次怎么退出的、重启了几次。

启动时读上一次留下的状态：还标着 running 说明没走正常停机（被杀、崩溃、断电），记为非正常退出；
正常停机时 lifespan 收尾写 stopped。O-35、O-13 的 `last_exit` 读这里。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

from .. import config

_last_exit: dict | None = None


def _path() -> Path:
    return Path(config.DATA_DIR) / "run" / "process.json"


def _read() -> dict:
    try:
        data = json.loads(_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return data if isinstance(data, dict) else {}


def _write(data: dict) -> None:
    path = _path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data), encoding="utf-8")
        tmp.replace(path)
    except OSError:
        pass


def mark_started() -> dict | None:
    """记下本次启动；返回上一次的退出情况 {at, reason, code}（epoch 秒），第一次启动为 None。"""
    global _last_exit
    previous = _read()
    if previous.get("state") == "running":
        _last_exit = {"at": previous.get("started_at"), "reason": "unclean", "code": None}
    elif previous.get("state") == "stopped":
        _last_exit = {"at": previous.get("stopped_at"), "reason": previous.get("reason") or "shutdown", "code": 0}
    else:
        _last_exit = None
    _write({
        "state": "running", "pid": os.getpid(), "started_at": time.time(),
        "restarts": int(previous.get("restarts") or 0) + (1 if previous else 0),
    })
    return _last_exit


def mark_stopped(reason: str = "shutdown") -> None:
    data = _read()
    data.update(state="stopped", stopped_at=time.time(), reason=reason)
    _write(data)


def last_exit() -> dict | None:
    return _last_exit


def restarts() -> int:
    return int(_read().get("restarts") or 0)
