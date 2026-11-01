"""启动恢复注册表（详设 8.3 措施 1；方案 B8、第 3 章 governance/startup_recovery.py；契约 O-35）。

原则：内核进程随时可以死，磁盘状态在任何时刻都自洽，启动过程就是恢复过程。各模块把自己的启动恢复
动作登记成步骤，lifespan 在任何会话开始之前按顺序跑一遍：

- 每步返回 O-35 报告里的一项 `{id, title, detail, result, tone, ...}`，或 None（只做事、不进报告，
  如存量数据迁移）；
- 前台步骤做完才算启动恢复完成（就绪探针 C-04 / `/api/health/ready` 等它）；
- 后台步骤（耗时的：清扫删到一半的目录、探测外部依赖）不挡就绪，做完后补进报告；
- 单步出错只记日志、在报告里记 failed，不挡启动。

内核自己的步骤在 `_load_defaults` 里登记（实现都在各自模块）；插件运行时等以后的模块用 `register` 加。
报告存在数据目录（services/startup_report），重启次数、上次退出原因由 lifecycle 记。
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Step:
    id: str
    title: str
    run: Callable[[object], dict | None]
    order: int
    background: bool = False


_steps: dict[str, Step] = {}
_lock = threading.RLock()
_defaults_loaded = False


def register(step_id: str, title: str, run: Callable[[object], dict | None], *, order: int,
             background: bool = False) -> None:
    """登记一个启动恢复步骤（同 ID 再登记会替换）。run(manager) 返回报告项或 None。"""
    with _lock:
        _steps[step_id] = Step(step_id, title, run, order, background)


def unregister(step_id: str) -> None:
    with _lock:
        _steps.pop(step_id, None)


def steps() -> list[Step]:
    _load_defaults()
    with _lock:
        return sorted(_steps.values(), key=lambda step: (step.order, step.id))


def _load_defaults() -> None:
    """内核的启动恢复步骤：实现分在各模块，这里按顺序登记一次。"""
    global _defaults_loaded
    with _lock:
        if _defaults_loaded:
            return
        _defaults_loaded = True
    from ..services import startup_report
    from ..services.session_host import recovery as session_recovery

    startup_report.register_steps(register)
    session_recovery.register_steps(register)


def _failed(step: Step, exc: Exception) -> dict:
    return {"id": step.id, "title": step.title, "detail": f"执行出错：{type(exc).__name__}: {exc}"[:300],
            "result": "failed", "tone": "warn"}


def _run_step(step: Step, manager) -> dict | None:
    started = time.monotonic()
    try:
        item = step.run(manager)
    except Exception as exc:  # noqa: BLE001 - 单步失败不挡启动
        _log.exception("启动恢复步骤 %s 出错", step.id)
        item = _failed(step, exc)
    elapsed = int((time.monotonic() - started) * 1000)
    if item is not None:
        item.setdefault("id", step.id)
        item.setdefault("title", step.title)
        level = logging.WARNING if item.get("tone") != "ok" else logging.INFO
        _log.log(level, "启动自检 · %s：%s（%d ms）", item["title"], item.get("detail", ""), elapsed)
        for project in item.get("projects") or []:
            if isinstance(project, dict) and project.get("project_id"):
                _log.log(level, "  %s：%s", project["project_id"], project.get("count", ""))
    else:
        _log.info("启动自检 · %s 完成（%d ms）", step.title, elapsed)
    return item


def run(manager, *, include_background: bool = True) -> dict:
    """跑登记的步骤并留存报告；include_background=False 时只跑前台步骤（lifespan 用，后台的另起）。"""
    from ..services import startup_report
    from . import lifecycle

    last_exit = lifecycle.mark_started()
    items = []
    for step in steps():
        if step.background and not include_background:
            continue
        item = _run_step(step, manager)
        if item is not None:
            items.append(item)
    report = {"at": time.time(), "last_exit": last_exit, "items": items}
    startup_report.save(report)
    return report


async def run_background(manager) -> list[dict]:
    """后台步骤：逐个在线程里跑，做完一个就补进报告。"""
    from ..services import startup_report

    done = []
    for step in steps():
        if not step.background:
            continue
        item = await asyncio.to_thread(_run_step, step, manager)
        if item is not None:
            startup_report.add_item(item)
            done.append(item)
    return done
