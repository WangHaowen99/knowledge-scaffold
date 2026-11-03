"""可观测公共标签（详设 4.10.2）：项目、会话、执行、变更集、包版本、节点、trace id。

用 contextvars 承载：请求入口、会话宿主、写入事务各自设置自己知道的标签，日志、指标、链路、审计
读同一份。没设置的标签为空串。
"""

from __future__ import annotations

import contextvars
import uuid
from contextlib import contextmanager

LABELS = ("trace_id", "project", "session", "exec", "changeset", "pack", "node")

_vars: dict[str, contextvars.ContextVar[str]] = {
    name: contextvars.ContextVar(f"onto_label_{name}", default="") for name in LABELS
}
# 本次请求的链路 ID：trace 中间件写入，错误体、审计、日志读取
trace_id_var = _vars["trace_id"]


def new_trace_id() -> str:
    return uuid.uuid4().hex[:16]


def get(name: str) -> str:
    return _vars[name].get()


def labels() -> dict[str, str]:
    """当前上下文里有值的公共标签。"""
    return {name: var.get() for name, var in _vars.items() if var.get()}


def set_label(name: str, value: str) -> contextvars.Token:
    return _vars[name].set(value or "")


@contextmanager
def bind(**values: str):
    """在 with 块内设置一组标签，退出时还原。"""
    tokens = [(name, _vars[name].set(str(value))) for name, value in values.items() if name in _vars and value]
    try:
        yield
    finally:
        for name, token in reversed(tokens):
            _vars[name].reset(token)
