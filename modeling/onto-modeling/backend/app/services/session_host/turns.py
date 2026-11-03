"""一轮对话在宿主侧的开始与收尾（详设 4.2 目标 3、8.4、6.10）：执行记录、页面命令任务、续跑卡、
下一步建议，以及发给项目流的通知。会话管理器（agent_sessions）在对应时刻调这里。

这里的函数只认 Session 上的几个字段（project_id、conversation_id、cwd、engine_instance、exec_id、
turn），不碰会话池的调度；要写进对话历史的记录（续跑卡、建议条）返回给调用方在事件循环里广播
（这些函数常在线程里跑，会话的订阅队列不是线程安全的）。项目流（streams）可以在任意线程发。
出错只记日志：执行记录与卡片是增强信息，不能让对话因此失败。
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from .. import project_events, streams
from . import command_tasks, exec_records, timeline

_log = logging.getLogger(__name__)

EXEC_MARKER = Path(".onto") / "run" / "exec.json"


def _flow(root: Path | None) -> dict:
    if root is None:
        return {}
    from ..flow import model

    try:
        return model.load_for(root)
    except Exception:  # noqa: BLE001 - 不是本体项目工作区（单测的临时目录）
        return {}


def _safe(what: str, func, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:  # noqa: BLE001
        _log.exception("会话宿主：%s 失败", what)
        return None


def _write_marker(root: Path | None, record: dict | None) -> None:
    """工作区里当前执行的标记（git 钩子写 Onto-Node trailer 用）；执行结束时删掉。"""
    if root is None or not Path(root).is_dir():
        return
    path = Path(root) / EXEC_MARKER
    try:
        if record is None:
            path.unlink(missing_ok=True)
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        node = record.get("node") or {}
        tmp = path.with_name(f".exec.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps({"exec_id": record["id"], "node": node.get("id") or "",
                                   "node_version": node.get("version") or "",
                                   "conversation_id": record.get("conversation_id") or "",
                                   # 按切片的命令：当前这一片（blueprint.py brief、state_helper acquire 读它）
                                   "slice": record.get("slice"), "step": record.get("step")}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass


def publish_exec(project_id: str, record: dict, root: Path | None, flow: dict | None = None) -> dict:
    """EP-09，并同步关联的页面命令任务。"""
    data = exec_records.view(record, root=root, flow=flow if flow is not None else _flow(root))
    streams.publish_project(project_id, "exec.updated", data)
    _safe("同步页面命令任务", command_tasks.on_exec, project_id, record)
    return data


# ---------------------------------------------------------------------------
# 一条用户消息进来 / 开始跑
# ---------------------------------------------------------------------------


def begin(session, text: str, *, task_id: str | None = None, resume_exec: str | None = None) -> None:
    """消息开始交给引擎时：斜杠命令开一份执行记录；普通消息接着这条对话里等待中的执行；
    续跑（S-16）接着原来那份记录。"""
    if resume_exec:
        _safe("续跑执行记录", _resume, session, resume_exec)
    else:
        _safe("开始执行记录", _begin, session, text, task_id)
    title = ""
    if session.exec_id:
        record = exec_records.get(session.project_id, session.exec_id)
        title = (record or {}).get("command_label") or ""
    session.turn_title = title
    if task_id and not session.exec_id:
        _safe("任务开始", command_tasks.start_running, session.project_id, task_id)


def _begin(session, text: str, task_id: str | None) -> None:
    root = session.cwd
    flow = _flow(root)
    parsed = exec_records.parse_command(text, flow)
    current = exec_records.open_record(session.project_id, session.conversation_id)
    if parsed is not None:
        if current is not None:
            _supersede(session, current, root, flow)
        slash, name = parsed
        record = exec_records.start(session.project_id, session.conversation_id, root=root, flow=flow,
                                    command=slash, command_name=name, engine_instance=session.engine_instance,
                                    task_id=task_id, text=text)
        if getattr(session, "run_id", None):
            # 自主构建的一步（契约 ExecRecord.run_id）：重启后由运行器续跑，不放续跑卡、不进待处理
            record = exec_records.update(session.project_id, record["id"], run_id=session.run_id) or record
        session.exec_id = record["id"]
        _write_marker(root, record)
        publish_exec(session.project_id, record, root, flow)
        return
    if current is not None:
        session.exec_id = current["id"]
        if current.get("state") == "waiting":
            record = exec_records.continue_running(session.project_id, current["id"])
            if record is not None:
                publish_exec(session.project_id, record, root, flow)
        return
    session.exec_id = None


def _resume(session, exec_id: str) -> None:
    record = exec_records.resume(session.project_id, exec_id, conversation_id=session.conversation_id,
                                 engine_instance=session.engine_instance)
    if record is None:
        return
    session.exec_id = record["id"]
    _write_marker(session.cwd, record)
    publish_exec(session.project_id, record, session.cwd)


def _supersede(session, current: dict, root: Path, flow: dict) -> None:
    """又开始了一条新命令：前一条命令锁已释放就算做完，否则记为被你打断（不出续跑卡，界面上能续跑）。"""
    if exec_records.lock_phase(root, current.get("command_name") or "") is None:
        record = exec_records.update(session.project_id, current["id"], state="done",
                                     ended_at=time.time(),
                                     saved=exec_records.saved(root, current.get("base_commit") or ""))
    else:
        record = exec_records.interrupt(session.project_id, current["id"], "user", root=root, flow=flow,
                                        reason_text="开始了另一条命令，这一步没做完")
    if record is not None:
        publish_exec(session.project_id, record, root, flow)


# ---------------------------------------------------------------------------
# 过程中
# ---------------------------------------------------------------------------


def observe(session) -> dict | None:
    """工具调用之后看一眼命令锁：阶段变了在工作过程里加一步，并发 exec.updated；否则按节流写心跳。"""
    if not session.exec_id:
        return None
    try:
        record, changed = exec_records.observe_phase(session.project_id, session.exec_id, session.cwd)
        if changed and record is not None:
            label = (record.get("phase") or {}).get("label") or ""
            if session.turn is not None and label:
                session.turn.add_step(None, f"进入「{label}」阶段", state="info")
            publish_exec(session.project_id, record, session.cwd)
            return session.turn.progress_record() if session.turn is not None and label else None
        exec_records.heartbeat(session.project_id, session.exec_id)
    except Exception:  # noqa: BLE001
        _log.exception("会话宿主：读取执行阶段失败")
    return None


# ---------------------------------------------------------------------------
# 一轮结束、打断、中断
# ---------------------------------------------------------------------------


def end_turn(session, *, failed: bool, waiting_user: bool = False) -> list[dict]:
    """一轮出 result 之后：执行记录记为等待或完成，任务收尾，项目流重算流程；返回要广播的下一步建议。"""
    bound: set[str] = set()
    if session.exec_id and not session.interrupting:  # 打断中先出的 result：执行记录由打断记为中断
        record = _safe("执行记录收尾", exec_records.finish_turn, session.project_id, session.exec_id, session.cwd,
                       waiting_user=waiting_user)
        if record is not None:
            if record.get("task_id"):
                bound.add(record["task_id"])
            _safe("发执行记录", publish_exec, session.project_id, record, session.cwd)
            if record.get("state") == "done":
                session.exec_id = None
                _write_marker(session.cwd, None)
    _safe("任务收尾", command_tasks.on_turn_end, session.project_id, session.conversation_id, failed=failed,
          exec_bound=bound)
    project_events.scope_changed(session.project_id)
    _safe("重算流程", project_events.schedule_flow_refresh, session.project_id)
    record = suggestions_record(session)
    return [record] if record is not None else []


def interrupt_exec(session, reason: str, *, card: bool) -> tuple[dict | None, dict | None]:
    """打断、引擎结束、回收、停机：执行记为在某阶段中断。返回 (执行记录, 续跑卡记录)；card=False 时不出卡。"""
    if not session.exec_id:
        return None, None
    root = session.cwd
    flow = _flow(root)
    record = _safe("执行记录中断", exec_records.interrupt, session.project_id, session.exec_id, reason, root=root,
                   flow=flow)
    session.exec_id = None
    _write_marker(root, None)
    if record is None:
        return None, None
    data = _safe("发执行记录", publish_exec, session.project_id, record, root, flow)
    if card and data is not None:
        return record, {"type": "card", "payload": exec_records.resume_card(record, data)}
    return record, None


def interrupted_fields(record: dict | None) -> dict:
    """session.interrupted（EC-05）里与执行相关的字段。"""
    if record is None:
        return {"exec_id": None, "saved": None}
    return {"exec_id": record["id"], "position_label": exec_records.position_label(record),
            "saved": record.get("saved")}


def suggestions_record(session, *, interrupted: dict | None = None) -> dict | None:
    """EC-53：这一轮结束或被打断后，输入框上方的建议条（与 FL-02 同一份；打断时换成打断后的三项）。"""
    data = _safe("算下一步建议", suggestions, session, interrupted=interrupted)
    return {"type": "suggestions", "payload": data} if data is not None else None


def suggestions(session, *, interrupted: dict | None = None) -> dict | None:
    if interrupted is not None:
        primary = {
            "id": "resume", "label": "从打断处继续", "icon": "redo", "primary": True, "disabled": False,
            "reason": "刚被打断", "priority": 1, "action": {"type": "resume_exec", "exec_id": interrupted["id"]},
        } if interrupted.get("id") else {
            "id": "continue", "label": "继续", "primary": True, "disabled": False, "reason": "刚被打断",
            "priority": 1, "action": {"type": "send", "text": "继续"},
        }
        secondary = [
            {"id": "continue", "label": "继续", "primary": False, "disabled": False, "reason": "刚被打断",
             "priority": 1, "action": {"type": "send", "text": "继续"}},
            {"id": "redirect", "label": "换个方向", "primary": False, "disabled": False, "reason": "刚被打断",
             "priority": 1, "action": {"type": "clear_input"}},
        ]
        if primary["id"] == "continue":
            secondary = secondary[1:]
        return {"mode": "interrupted", "primary": primary, "secondary": secondary, "all_commands": True}
    from ...kernel.changesets import worktree

    root = worktree.main_root(session.cwd)
    if root is None or not (Path(root) / ".claude").exists():
        return None
    from ..flow import view

    # 带上对话：「没收尾主推继续」、打断后的建议都按这条对话算，和 FL-02 同一份（界面以推来的这份为准）
    result = dict(view.build(root, conversation_id=session.conversation_id)["suggestions"])
    if session.pending_permissions:
        result["mode"] = "waiting_decision"
        if result.get("primary"):
            result["primary"] = {**result["primary"], "disabled": True, "reason": "先做上面的决定"}
    return result


def resume_text(record: dict) -> str:
    """续跑指令：带上原命令，说明从哪一步接着做、已保存的不要重做。"""
    phase = (record.get("phase") or {}).get("label")
    where = f"「{phase}」阶段" if phase else "中断的地方"
    return (f"{record.get('command') or ''} 继续上次没做完的「{record.get('command_label') or '命令'}」："
            f"从{where}接着做，已经保存的内容不要重做。").strip()


def write_identity_marker(root: Path | None, *, engine: str, engine_id: str, conversation_id: str,
                          owner: str) -> None:
    """引擎标记 `.onto/run/engine.json`：state_helper 认锁、git 钩子写 trailer、Jiuwen 命令取会话身份。"""
    if root is None or not Path(root).is_dir():
        return
    import time

    path = Path(root) / ".onto" / "run" / "engine.json"
    from ...kernel.changesets import worktree

    payload = {
        "project_id": worktree.project_id_of(Path(root)),
        "engine_id": engine_id,
        # state_helper 只认这个进程的子孙：工作区里人工跑的脚本不会冒用引擎的锁
        "host_pid": os.getpid(),
        "engine": engine,
        "conversation_id": conversation_id,
        "owner": owner,
        "started_at": time.time(),
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.{uuid.uuid4().hex[:8]}.tmp")
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        pass  # 写不了时 state_helper 退回按进程号判断，不阻断会话


__all__ = [
    "begin", "observe", "end_turn", "interrupt_exec", "interrupted_fields", "suggestions_record",
    "suggestions", "resume_text", "publish_exec", "write_identity_marker", "timeline",
]
