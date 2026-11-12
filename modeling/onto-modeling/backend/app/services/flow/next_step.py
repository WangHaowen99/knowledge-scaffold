"""建议条上的动作此刻能不能执行（详设 6.10，F7）。

生成建议时（view.build 交给 flow_state.suggest 的 check）用它过滤，接口收到建议条发起的请求（请求体带
`from_suggestion`）时用它校验，两处同一份：检查不过的按钮不出现；看到按钮到点下去之间情况变了，接口回
409 `suggestion_stale` 并带上重算后的建议，界面直接换上，不弹错误。

只查「点下去会不会被拒」，不重复规则表（规则在 flow_state.suggest）。
"""

from __future__ import annotations

from pathlib import Path

# 引擎占着（在跑、排队、连接中、等你回答）时发不出去的动作
_NEEDS_ENGINE = ("send", "resume_exec", "retry")


def blocker(project_id: str, conversation_id: str | None, action: dict, root: Path | None = None) -> str | None:
    """能执行返回 None，不能返回原因（一句话）。不认识的动作（跳页、打开弹窗、自主构建控制）不拦。"""
    kind = (action or {}).get("type")
    if kind == "send" and action.get("command") and not _installed(project_id, str(action["command"]), root):
        return "这条命令所属的能力没有启用"
    from ..agent_sessions import _busy, manager
    from ..autobuild import store as autobuild_store

    session = manager.get(project_id)
    if kind in _NEEDS_ENGINE or kind == "abandon_exec":
        if autobuild_store.active(project_id) is not None:
            return "自主构建正在运行"
    if kind in _NEEDS_ENGINE and session is not None and _busy(session):
        # 打断已经发出、引擎还没停下的这一会儿不算占着：建议条给打断后的那一份（同 view._engine_running）
        if not getattr(session, "interrupt_requested", False):
            return "助手正在运行" if not session.pending_permissions else "有等你回答的问题"
    if kind == "retry":
        if session is None or session.conversation_id != conversation_id:
            return "没有可以重试的一轮"
        if session.state != "error" or not session.last_prompt:
            return "没有可以重试的一轮"
    if kind in ("resume_exec", "abandon_exec"):
        from ..session_host import exec_records

        record = exec_records.get(project_id, str(action.get("exec_id") or ""))
        if record is None:
            return "这一步的执行记录不在了"
        if kind == "resume_exec" and (record.get("state") != "interrupted" or record.get("run_id")):
            return "这一步已经不是中断状态"
        if kind == "abandon_exec":
            if record.get("state") in ("done", "abandoned"):
                return "这一步已经结束"
            if record.get("state") == "running" and session is not None and session.exec_id == record["id"]:
                return "这一步正在跑"
    if kind == "focus_question":
        from .view import _pending_question

        question = _pending_question(session) if session is not None and \
            session.conversation_id == conversation_id else None
        wanted = action.get("question_id")
        if question is None or (wanted and not _question_pending(session, wanted)):
            return "这个问题已经回答或失效"
    return None


def allowed(project_id: str, conversation_id: str | None, action: dict, root: Path | None = None) -> bool:
    return blocker(project_id, conversation_id, action, root) is None


def _installed(project_id: str, command: str, root: Path | None) -> bool:
    """命令在当前装配里（S-20 发命令时同一个判断：`.claude/commands/<命令>.md` 在）；取不到工作区时不拦。"""
    name = command.split()[0].lstrip("/")
    try:
        if root is None:
            from ...api import deps

            root = deps.workspaces.project_root(project_id)
        return (Path(root) / ".claude" / "commands" / f"{name}.md").is_file()
    except Exception:  # noqa: BLE001
        return True


def _question_pending(session, question_id: str) -> bool:
    for request in session.pending_permissions.values():
        if request.future is None or request.future.done():
            continue
        if request.id == question_id and not request.questions:
            return True
        if any(item.get("question_id") == question_id for item in request.questions) \
                and question_id not in request.answers:
            return True
    return False


def guard(project_id: str, conversation_id: str | None, action: dict, root: Path) -> None:
    """建议条发起的请求：点下去之前情况变了，回 409 suggestion_stale（details 带原因和重算后的建议）。"""
    reason = blocker(project_id, conversation_id, action, root)
    if reason is None:
        return
    from ...api.errors import ApiError
    from . import view

    fresh = view.build(Path(root), conversation_id=conversation_id or None)["suggestions"] \
        if conversation_id else None
    raise ApiError("suggestion_stale", "情况变了，建议已更新", 409, {"reason": reason, "suggestions": fresh})
