"""项目流事件的发送口（契约 4.3）：插件运行时、产物索引、知识接口往这里发项目级事件。

项目事件流（P-15 `GET /api/v1/projects/{id}/events`）由会话宿主一侧实现；这里只定发送接口，
流那边落地后用 `register_sink` 接上，发送方不用改。没有接收方时事件只留在进程内的最近记录里
（测试与诊断用），不报错、不阻塞调用方。

本模块发的事件：
- `extensions.updated`（EP-07）：启用、停用、升级、回滚包后，左栏页面、子页、产物类型变了；
- `assembly.updated`（EP-08）：锁文件改了，装配待生效或已生效；
- `artifacts.updated`（EP-06）：插件产物新建、修改、删除，或因本体条目变化标「过期」；
- `knowledge.updated`（EP-23）：知识条目新增、修改、删除。
"""

from __future__ import annotations

import collections
import logging
import threading
import time
from typing import Callable

_log = logging.getLogger(__name__)

Sink = Callable[[str, str, dict], None]

_sinks: list[Sink] = []
_recent: collections.deque = collections.deque(maxlen=200)
_lock = threading.Lock()


def register_sink(sink: Sink) -> Callable[[], None]:
    """登记接收方 `sink(project_id, type, data)`；返回取消登记的函数。"""
    with _lock:
        if sink not in _sinks:
            _sinks.append(sink)

    def unregister() -> None:
        with _lock:
            if sink in _sinks:
                _sinks.remove(sink)

    return unregister


def emit(project_id: str, event_type: str, data: dict) -> None:
    """发一条项目事件；接收方出错只记日志（写入已经完成，不能因为推送失败回滚）。"""
    record = {"project_id": project_id, "type": event_type, "data": data, "ts": time.time()}
    with _lock:
        _recent.append(record)
        sinks = list(_sinks)
    for sink in sinks:
        try:
            sink(project_id, event_type, data)
        except Exception:  # noqa: BLE001 - 推送失败不影响发送方
            _log.exception("项目事件 %s 推送失败（项目 %s）", event_type, project_id)


def recent(project_id: str | None = None, event_type: str | None = None) -> list[dict]:
    """进程内最近的事件（新的在后）。"""
    with _lock:
        items = list(_recent)
    return [item for item in items
            if (project_id is None or item["project_id"] == project_id)
            and (event_type is None or item["type"] == event_type)]


def clear() -> None:
    with _lock:
        _recent.clear()
