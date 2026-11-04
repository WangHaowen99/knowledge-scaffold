"""页面命令任务（详设 5.9.3；契约 7.7 CommandTask、S-19、S-20、EP-10）。

插件页、资料页只发命令，助手在工作台对话里跑；页面靠任务对象显示进度条和「去对话里看」，
刷新页面后经 S-19 恢复。任务存在数据目录 `exec/<项目>/tasks.json`（新的在前，留最近 100 条）。

状态跟着执行记录与这一轮走：
- 消息排队或要等前一步结束（deferred）：queued；
- 命令的执行记录开始：running；执行在等你回答：waiting_user；
- 执行做完：done（带结果摘要）；中断：interrupted；放弃：cancelled；这一轮出错：failed；
- 不是流程登记的命令（没有执行记录）：这一轮开始 running，结束 done / failed。

状态变化在项目流发持久的 `command.progress`；只是阶段文字变了发瞬时的（契约 1.11）。
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path

from ... import config
from .. import streams

ACTIVE = ("queued", "running", "waiting_user")
STATES = ("queued", "running", "waiting_user", "done", "failed", "interrupted", "cancelled")
KEEP = 100
LIST_MAX = 50
_lock = threading.RLock()
# 执行记录状态 → 任务状态
_FROM_EXEC = {"running": "running", "waiting": "waiting_user", "interrupted": "interrupted", "done": "done",
              "abandoned": "cancelled"}


def _path(project_id: str) -> Path:
    return Path(config.DATA_DIR) / "exec" / project_id / "tasks.json"


def _load(project_id: str) -> list[dict]:
    try:
        data = json.loads(_path(project_id).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return data if isinstance(data, list) else []


def _save(project_id: str, tasks: list[dict]) -> None:
    path = _path(project_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".tasks.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(json.dumps(tasks[:KEEP], ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def view(task: dict) -> dict:
    from .views import iso

    return {
        "task_id": task["task_id"],
        "origin": task.get("origin") or {"page": "", "label": ""},
        "title": task.get("title") or "",
        "state": task.get("state") or "queued",
        "percent": task.get("percent"),
        "step_label": task.get("step_label"),
        "conversation_id": task.get("conversation_id") or "",
        "message_id": task.get("message_id") or "",
        "exec_id": task.get("exec_id"),
        "started_at": iso(task.get("started_at")),
        "finished_at": iso(task.get("finished_at")),
        "result": task.get("result"),
    }


def create(project_id: str, *, origin: dict, title: str, conversation_id: str, command: str) -> dict:
    task = {
        "task_id": f"task-{uuid.uuid4().hex[:10]}",
        "origin": {"page": str(origin.get("page") or ""), "label": str(origin.get("label") or "")},
        "title": title,
        "state": "queued",
        "percent": None,
        "step_label": None,
        "conversation_id": conversation_id,
        "message_id": "",
        "exec_id": None,
        "command": command,
        "started_at": time.time(),
        "finished_at": None,
        "result": None,
    }
    with _lock:
        _save(project_id, [task, *_load(project_id)])
    return task


def get(project_id: str, task_id: str) -> dict | None:
    return next((task for task in _load(project_id) if task.get("task_id") == task_id), None)


def update(project_id: str, task_id: str, *, transient: bool = False, **fields) -> dict | None:
    """改任务并在项目流发 command.progress；终态补结束时间。"""
    with _lock:
        tasks = _load(project_id)
        task = next((item for item in tasks if item.get("task_id") == task_id), None)
        if task is None:
            return None
        before = task.get("state")
        task.update(fields)
        if task.get("state") not in ACTIVE and not task.get("finished_at"):
            task["finished_at"] = time.time()
        _save(project_id, tasks)
    persistent = not transient or task.get("state") != before
    streams.publish_project(project_id, "command.progress", view(task), transient=not persistent)
    return task


def query(project_id: str, *, origin_page: str = "", states: list[str] | None = None) -> list[dict]:
    states = states or list(ACTIVE)
    return [
        view(task) for task in _load(project_id)
        if task.get("state") in states and (not origin_page or (task.get("origin") or {}).get("page") == origin_page)
    ][:LIST_MAX]


def active_for_conversation(project_id: str, conversation_id: str) -> list[dict]:
    return [task for task in _load(project_id)
            if task.get("conversation_id") == conversation_id and task.get("state") in ACTIVE]


def on_exec(project_id: str, record: dict) -> None:
    """执行记录变了：同步关联的任务（状态、阶段文字、结果）。"""
    task_id = record.get("task_id")
    if not task_id:
        return
    task = get(project_id, task_id)
    if task is None:
        return
    state = _FROM_EXEC.get(record.get("state") or "", task.get("state"))
    phase = (record.get("phase") or {}).get("label") or None
    fields = {"state": state, "exec_id": record["id"], "step_label": phase}
    if state == "done":
        saved = record.get("saved") or {}
        summary = f"「{record.get('command_label') or '命令'}」做完了"
        if saved.get("files"):
            summary += f"，改了 {saved['files']} 个文件"
        if saved.get("version_label"):
            summary += f" · {saved['version_label']}"
        fields.update(result={"summary": summary, "refs": []}, percent=100)
    elif state == "interrupted":
        fields["result"] = {"summary": (record.get("interrupted") or {}).get("reason_text") or "中断了", "refs": []}
    transient = state == task.get("state")
    update(project_id, task_id, transient=transient, **fields)


def on_turn_end(project_id: str, conversation_id: str, *, failed: bool, exec_bound: set[str]) -> None:
    """这一轮结束：没有执行记录的任务（不是流程命令）随这一轮收尾。"""
    for task in active_for_conversation(project_id, conversation_id):
        if task.get("exec_id") or task["task_id"] in exec_bound or task.get("state") == "queued":
            continue
        update(project_id, task["task_id"], state="failed" if failed else "done",
               result={"summary": "这一轮出错了" if failed else "做完了", "refs": []},
               percent=None if failed else 100)


def on_interrupt(project_id: str, conversation_id: str, *, keep: set[str] | frozenset = frozenset()) -> None:
    """打断：没有执行记录的任务记为中断；keep 是代存着、打断后暂停的命令的任务，照旧排着。"""
    for task in active_for_conversation(project_id, conversation_id):
        if not task.get("exec_id") and task["task_id"] not in keep:
            update(project_id, task["task_id"], state="interrupted",
                   result={"summary": "你打断了这一步", "refs": []})


def start_running(project_id: str, task_id: str) -> None:
    task = get(project_id, task_id)
    if task is not None and task.get("state") == "queued":
        update(project_id, task_id, state="running")


def purge(project_id: str) -> None:
    try:
        _path(project_id).unlink()
    except OSError:
        pass
