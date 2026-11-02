"""插件运行时（扩展项注册表、插件产物、包的提示规则）与会话宿主、项目流之间的登记（ADR-0045）。

两边各自只留了登记点，接线全在这里，启动时调一次：
- S-20 页面按钮 `action` → 命令文本：`routers/sessions.register_action_resolver`；
- 提交改到的插件产物 → `workspace.changed.artifacts`：`services/project_events.register_artifact_mapper`；
- 装配变了以后扩展项哪几类变了 → `extensions.updated.changed`：`register_extension_differ`；
- 内核产物索引发的 `artifacts.updated` → 项目流：`kernel/event_port.register_sink`；
- 包的提示规则（知识沉淀的纠错信号）→ 宿主注入：`session_host/injection.register_prompt_hint_provider`；
- 变更集（B4b）：会话状态 → `kernel/changesets/service.register_activity_provider`，变更集变化 → 项目流
  （`changeset.updated`、`scope.updated`、`flow.updated`），写入范围锁 → `project_events.register_scope_provider`
  与插件产物的 `register_lock_provider`。
- 一轮改到的插件产物 → 对话里的产物卡（ADR-0055）：`services/project_events.register_round_card_provider`。
- 包声明的门禁 → 写入事务的门禁链（ADR-0068）：`kernel/writepath.register_gate_checker`。

登记的都是进程内的函数引用，不做 I/O，可以重复调。在建应用时（`app/main.py`）调，而不是在 lifespan 里：
不开 lifespan 的 TestClient、uvicorn 起的服务看到的是同一套接线。工作区管理器一律调用时再取
`deps.workspaces`（测试替换这一处即可）。

Jiuwen worker 是另一个进程，宿主注入在 worker 里算（ADR-0038），它用 `install_worker` 登记自己那份提示规则。
"""

from __future__ import annotations

from pathlib import Path


def _workspaces():
    from .api import deps

    return deps.workspaces


def _page_action_command(project_id: str, root: Path, action: dict) -> str | None:
    """S-20：按锁文件里启用的包把 {pack, page, action_id} 换成命令；认不出返回 None（409 pack_not_enabled）。"""
    from .services.packs import registry

    found = registry.resolve_action(_workspaces(), project_id, action)
    return (found or {}).get("command") or None


def _artifact_refs(root: Path, paths: list[str]) -> list[dict]:
    """一次提交改到的插件产物；顺带让产物索引发 artifacts.updated（含过期翻转）。项目 ID 即工作区目录名。"""
    from .kernel.artifacts import service as artifact_service

    return artifact_service.after_commit(_workspaces(), root.name, paths)


def _round_artifact_cards(project_id: str, changes: dict, base: str, seed: str, root: Path | None = None) -> list[dict]:
    """一轮结束：改到的插件产物按包的 card 声明出卡（只读与否由前端按实时的写入范围判断，卡上不存）。
    助手在变更集副本里改时 root 是副本，内容从副本读（ADR-0046、0055）。"""
    from .kernel.artifacts import cards

    return cards.round_cards(_workspaces(), project_id, changes, base=base, seed=seed, root=root)


def _publish_kernel_event(project_id: str, event_type: str, data: dict) -> None:
    from .services import streams

    streams.publish_project(project_id, event_type, data)


def _prompt_hints(root: Path, user_text: str) -> list[str]:
    from .services.packs import registry

    return registry.prompt_hints(_workspaces(), root, user_text)


def _session_activity(project_id: str) -> dict:
    """变更集一侧要的会话状态（ADR-0046）：助手在不在跑、哪条对话、正在执行的节点、决策卡改动预览。"""
    from .services.agent_sessions import manager
    from .services.session_host import exec_records

    session = manager.get(project_id)
    if session is None:
        return {}
    running = session.state == "running" or session.turns_in_flight > 0 or bool(session.sending)
    record = exec_records.get(project_id, session.exec_id) if session.exec_id else None
    affected = []
    for request in list(session.pending_permissions.values()):
        if request.future is not None and request.future.done():
            continue
        for question in request.questions or []:
            for ref in question.get("affected") or []:
                if ref not in affected:
                    affected.append(ref)
    return {"running": running, "conversation_id": session.conversation_id, "exec": record, "affected": affected}


def _changeset_event(project_id: str, event: str, data: dict) -> None:
    """变更集变了：EP-04 changeset.updated、EP-03 scope.updated，并重算流程（节点「待确认」）。"""
    from .services import project_events, streams

    if event == "changeset" and data and data.get("state") == "merged":
        # 确认生效：变更集里执行过的节点按主线现状重记基线（不再过期；完成判定满足即已确认），人确认过的
        # 需复核标记去掉；自主构建自动合入的片改标「需复核（自主构建）」
        _confirm_nodes(project_id, data.get("nodes") or [], data)
    if event == "changeset" and data:
        _sync_change_lists(project_id, data)
    if event == "changeset" and data:
        merged = data.get("merged") or {}
        streams.publish_project(project_id, "changeset.updated", {
            "changeset_id": data["id"], "state": data["state"], "source": data.get("source") or "assistant",
            "conversation_id": data.get("conversation_id"), "version_label": merged.get("version_label"),
        })
    project_events.scope_changed(project_id)
    project_events.schedule_flow_refresh(project_id)


def _confirm_nodes(project_id: str, node_ids: list[str], record: dict | None = None) -> None:
    from .services.flow import model as flow_model
    from .services.flow import state as node_state

    if not node_ids:
        return
    root = _workspaces().project_root(project_id)
    node_state.confirm(root, flow_model.load_for(root), node_ids)
    if record and record.get("needs_review"):
        node_state.mark_needs_review(root, node_ids, "autobuild", run_id=record.get("run_id"),
                                     slice_id=record.get("slice_id"), note=record.get("purpose") or "")


def _sync_change_lists(project_id: str, record: dict) -> None:
    """变更点清单跟着变更集走（审查第 8、12 条）：开了变更集挂上编号，确认生效记已执行，撤回回到编辑中。"""
    from .kernel.changesets import store
    from .services.flow import change_lists

    root = _workspaces().project_root(project_id)
    if record["state"] in ("merged", "discarded"):
        change_lists.on_changeset_closed(root, record["id"], record["state"])
        return
    linked = change_lists.link(root, record.get("conversation_id"), record["id"])
    if linked and record.get("change_list_id") != linked:
        store.update(root, record["id"], change_list_id=linked)


def _next_steps(root: Path) -> dict | None:
    """统一下一步（D16）：宿主注入与助手收尾同一份——在助手工作目录里跑 state_helper suggest。"""
    from .services.session_host import next_steps

    return next_steps.suggest(root)


def _badge(project_id: str, pack: str) -> int:
    """左栏插件页角标（B4c 的登记点）：待处理里属于这个包的事项数。"""
    from .services.flow import inbox

    return inbox.badge(_workspaces(), project_id, pack)


def _scope_lock(project_id: str) -> dict:
    from .kernel.changesets import service as changesets

    return changesets.scope_lock(_workspaces(), project_id)


def _artifact_lock(root: Path) -> dict | None:
    from .kernel.changesets import service as changesets

    return changesets.scope_lock(_workspaces(), root.name)


_extension_differ = None


def install() -> None:
    """内核进程的接线（幂等）。"""
    global _extension_differ
    from .kernel import event_port
    from .routers import sessions
    from .services import project_events
    from .services.packs import registry
    from .services.session_host import injection

    sessions.register_action_resolver(_page_action_command)
    project_events.register_artifact_mapper(_artifact_refs)
    project_events.register_round_card_provider(_round_artifact_cards)
    if _extension_differ is None:
        _extension_differ = registry.extension_differ()
    project_events.register_extension_differ(_extension_differ)
    event_port.register_sink(_publish_kernel_event)
    injection.register_prompt_hint_provider(_prompt_hints)
    # 变更集（B4b，ADR-0046、0047）：会话状态 → 变更集与写入范围；变更集变化 → 项目流；写入范围 → 项目流与插件产物
    from .kernel.artifacts import service as artifact_service
    from .kernel.changesets import service as changesets

    from .kernel.writepath import scope as write_scope

    changesets.register_activity_provider(_session_activity)
    # 包声明的门禁（B5c，ADR-0068）：写入事务提交与合入时查 on 含 commit 的规则
    from .kernel.writepath import register_gate_checker
    from .services.packs import extensions

    register_gate_checker(extensions.commit_findings)
    injection.register_next_step_provider(_next_steps)
    write_scope.register_checker(changesets.paths_in_scope)
    registry.register_badge_provider(_badge)
    changesets.subscribe(_changeset_event)
    project_events.register_scope_provider(_scope_lock)
    artifact_service.register_lock_provider(_artifact_lock)


def install_worker(data_dir: Path, assets_dir: Path) -> None:
    """Jiuwen worker 进程的接线：只有宿主注入要用，按内核给的数据目录建一个工作区管理器读装配与包仓库。"""
    from .services.packs import registry
    from .services.session_host import injection
    from .services.workspace import WorkspaceManager

    manager = WorkspaceManager(data_dir, assets_dir)
    injection.register_prompt_hint_provider(lambda root, user_text: registry.prompt_hints(manager, root, user_text))
    injection.register_next_step_provider(_next_steps)
