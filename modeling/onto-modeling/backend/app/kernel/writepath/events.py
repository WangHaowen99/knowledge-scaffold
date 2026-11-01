"""项目级变更事件 `workspace.changed`（方案 C2）：写入事务提交后发出，SSE、流程运行时、产物索引都订阅它。

订阅方在进程内注册回调；回调出错只记日志，不影响写入本身（提交已经完成）。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import asdict, dataclass, field
from typing import Callable

_log = logging.getLogger(__name__)

EVENT_TYPE = "workspace.changed"


@dataclass(frozen=True)
class WorkspaceChanged:
    project_id: str
    commit: str
    previous: str
    source: str
    author: str
    paths: tuple[str, ...]
    target: str = "mainline"  # P2 起还可能是变更集副本
    changeset: str | None = None
    trace_id: str | None = None
    extra: dict = field(default_factory=dict)

    def as_dict(self) -> dict:
        data = asdict(self)
        data["paths"] = list(self.paths)
        data["type"] = EVENT_TYPE
        return data


_subscribers: list[Callable[[WorkspaceChanged], None]] = []
_lock = threading.Lock()


def subscribe(callback: Callable[[WorkspaceChanged], None]) -> Callable[[], None]:
    """注册订阅；返回取消订阅的函数。"""
    with _lock:
        _subscribers.append(callback)

    def unsubscribe() -> None:
        with _lock:
            if callback in _subscribers:
                _subscribers.remove(callback)

    return unsubscribe


def publish(event: WorkspaceChanged) -> None:
    with _lock:
        subscribers = list(_subscribers)
    for callback in subscribers:
        try:
            callback(event)
        except Exception:  # 订阅方的问题不回滚已完成的提交
            _log.exception("workspace.changed 订阅方出错（项目 %s）", event.project_id)
