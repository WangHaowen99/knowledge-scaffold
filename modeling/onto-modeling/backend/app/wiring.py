"""插件运行时（扩展项注册表、插件产物、包的提示规则）与会话宿主、项目流之间的登记（ADR-0045）。

两边各自只留了登记点，接线全在这里，启动时调一次：
- S-20 页面按钮 `action` → 命令文本：`routers/sessions.register_action_resolver`；
- 提交改到的插件产物 → `workspace.changed.artifacts`：`services/project_events.register_artifact_mapper`；
- 装配变了以后扩展项哪几类变了 → `extensions.updated.changed`：`register_extension_differ`；
- 内核产物索引发的 `artifacts.updated` → 项目流：`kernel/event_port.register_sink`；
- 包的提示规则（知识沉淀的纠错信号）→ 宿主注入：`session_host/injection.register_prompt_hint_provider`；
- 一轮改到的插件产物 → 对话里的产物卡（ADR-0055）：`services/project_events.register_round_card_provider`。

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


def _round_artifact_cards(project_id: str, changes: dict, base: str, head: str) -> list[dict]:
    """一轮结束：改到的插件产物按包的 card 声明出卡；写入范围锁按当前的算（卡上的「只读」）。"""
    from .kernel.artifacts import cards
    from .kernel.artifacts import service as artifact_service

    manager = _workspaces()
    lock = artifact_service._scope_lock(manager.project_root(project_id))
    return cards.round_cards(manager, project_id, changes, base=base, seed=head, lock=lock)


def _publish_kernel_event(project_id: str, event_type: str, data: dict) -> None:
    from .services import streams

    streams.publish_project(project_id, event_type, data)


def _prompt_hints(root: Path, user_text: str) -> list[str]:
    from .services.packs import registry

    return registry.prompt_hints(_workspaces(), root, user_text)


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


def install_worker(data_dir: Path, assets_dir: Path) -> None:
    """Jiuwen worker 进程的接线：只有宿主注入要用，按内核给的数据目录建一个工作区管理器读装配与包仓库。"""
    from .services.packs import registry
    from .services.session_host import injection
    from .services.workspace import WorkspaceManager

    manager = WorkspaceManager(data_dir, assets_dir)
    injection.register_prompt_hint_provider(lambda root, user_text: registry.prompt_hints(manager, root, user_text))
