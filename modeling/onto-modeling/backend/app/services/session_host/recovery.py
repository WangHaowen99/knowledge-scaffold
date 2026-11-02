"""会话宿主的启动恢复步骤（详设 8.3 措施 1、4；方案 B6、B8；契约 O-35 的 history_repair、interrupted_execs）。

在任何会话开始之前跑（启动恢复注册表 governance/startup_recovery 调）：

- history_repair：对话历史尾部停在「运行中」的（进程被杀、没走停机），补「上一轮未正常结束」与空闲终态；
  没回复的确认卡、决策卡补失效，界面不会留下永远转圈、点了 404 的卡片；
- interrupted_execs：还开着（运行中、等待）的执行一律改判为「服务重启中断」，在它所在的对话里放续跑卡；
  关联的页面命令任务一起改判；进程内后台任务（求解验证等）的启动收敛也在这一步。
"""

from __future__ import annotations

import logging

from . import command_tasks, exec_records

_log = logging.getLogger(__name__)
MAX_LISTED = 50


def _project_ids(manager) -> list[str]:
    return [root.name for root in manager._project_dirs()]


def repair_histories(manager) -> dict:
    from .. import agent_sessions, conversation_store

    projects = []
    for project_id in _project_ids(manager):
        count = 0
        for conversation in conversation_store.list_conversations(project_id):
            try:
                count += agent_sessions.repair_history(project_id, conversation["id"])
            except Exception:  # noqa: BLE001 - 单条对话修不了不挡启动
                _log.exception("修复对话历史失败：%s/%s", project_id, conversation["id"])
        if count:
            projects.append({"project_id": project_id, "count": count, "paths": []})
    if not projects:
        return {"id": "history_repair", "title": "对话历史", "result": "ok", "tone": "ok",
                "detail": "各对话的历史都停在终态"}
    total = sum(item["count"] for item in projects)
    return {"id": "history_repair", "title": "对话历史", "result": "cleared", "tone": "fix",
            "detail": f"{len(projects)} 个项目有 {total} 处没正常结束的对话或卡片，已补中断标记",
            "projects": projects[:MAX_LISTED]}


def sweep_execs(manager) -> dict:
    from .. import agent_sessions, background_jobs
    from .turns import _flow

    from ...kernel.changesets import service as changesets
    from ...kernel.changesets import worktree

    changed = exec_records.sweep_interrupted()
    per_project: dict[str, int] = {}
    for record in changed:
        project_id = record["project_id"]
        per_project[project_id] = per_project.get(project_id, 0) + 1
        try:
            root = manager.project_root(project_id)
        except Exception:  # noqa: BLE001 - 项目已删除：记录留着，不放卡
            continue
        copy = changesets.existing_copy(root)
        if copy is not None:
            # 启动时没有命令在跑：副本里留下的命令锁与写入范围一起清掉（审查第 9 条）
            worktree.clear_run_state(copy)
        view = exec_records.view(record, root=root, flow=_flow(root))
        if record.get("conversation_id"):
            agent_sessions.append_history(project_id, record["conversation_id"],
                                          {"type": "card", "payload": exec_records.resume_card(record, view)})
        try:
            command_tasks.on_exec(project_id, record)
        except Exception:  # noqa: BLE001
            _log.exception("改判页面命令任务失败：%s", record["id"])
    jobs = background_jobs.run_startup_sweeps()
    job_count = sum(len(value) if isinstance(value, (list, tuple, set)) else int(value) if isinstance(value, int) else 0
                    for value in jobs.values())
    if not changed and not job_count:
        return {"id": "interrupted_execs", "title": "中断的执行", "result": "ok", "tone": "ok",
                "detail": "没有停机时还在跑的执行"}
    detail = f"{len(changed)} 个执行改判为中断，已在对话里放续跑卡" if changed else "没有停机时还在跑的执行"
    if job_count:
        detail += f"；{job_count} 个项目的后台任务改判为中断"
    return {"id": "interrupted_execs", "title": "中断的执行", "result": "awaiting_user" if changed else "cleared",
            "tone": "fix", "detail": detail,
            "projects": [{"project_id": pid, "count": count, "paths": []} for pid, count in sorted(per_project.items())][:MAX_LISTED]}


def register_steps(register) -> None:
    register("history_repair", "对话历史", repair_histories, order=90)
    register("interrupted_execs", "中断的执行", sweep_execs, order=100)
