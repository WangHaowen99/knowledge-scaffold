"""项目流、用户流的事件来源（契约第 4.3、4.4 节）：把内核各处的变化接到 `streams`。

- `workspace.changed`（EP-05）：订阅写入事务的变更事件；agent 自己的提交由会话宿主在每轮结束时补发；
- `flow.updated`（EP-01）：工作区变了、一轮结束、装配切换、执行记录变化之后重算流程，只发变了的节点；
- `assembly.updated`（EP-08）、`extensions.updated`（EP-07）：锁文件提交后（待生效）与切换装配后（已生效：
  消息前切换，或只动纯数据包时插件运行时当场切换）；
- `artifacts.updated`（EP-06）：内核产物索引经 `kernel/event_port` 发出，启动时（`app/wiring.py`）接到这里；
- `knowledge.updated`（EP-23）：知识接口写入后；
- `scope.updated`（EP-03）：写入范围锁由变更集一侧算（ADR-0047），这里只留数据源登记点，默认「没有锁」；
- `inbox.updated`（EP-02）：每次重算流程时顺带重算待处理，变了才发（ADR-0049）；
- `conversation.updated`（EP-21）、`project.updated`（EP-20）、`maintenance.updated`（EP-24 / EU-04）、
  `project.card.updated`（EU-02）、`project.removed`（EU-03）。

发事件出错只记日志：事件是增强信息，不能让写入、会话因此失败。
"""

from __future__ import annotations

import logging
import threading
from pathlib import Path
from typing import Callable

from . import streams

_log = logging.getLogger(__name__)

PATHS_MAX = 200
FLOW_DEBOUNCE_SECONDS = 0.3
_SOURCES = {"agent": "assistant"}
# 变了哪几类扩展项：扩展项注册表（PL-12，B4c）登记精确的比较；没登记时整份重拉
EXTENSION_KINDS = ("nav", "pages", "subpages", "entries", "artifact_types", "views", "gates")


def _manager():
    from ..api import deps

    return deps.workspaces


def _guard(what: str, func: Callable, *args, **kwargs):
    try:
        return func(*args, **kwargs)
    except Exception:  # noqa: BLE001 - 事件是增强信息
        _log.exception("项目流事件 %s 发送失败", what)
        return None


# ---------------------------------------------------------------------------
# 登记点：产物、扩展项、写入范围由以后的阶段提供
# ---------------------------------------------------------------------------

_artifact_mappers: list[Callable[[Path, list[str]], list[dict]]] = []
_extension_differs: list[Callable[[str, str | None, str | None], list[str] | None]] = []
_scope_provider: Callable[[str], dict] | None = None


def register_artifact_mapper(mapper: Callable[[Path, list[str]], list[dict]]) -> None:
    """插件产物登记（B4c）：改动路径 → ArtifactRef 列表，进 workspace.changed 的 artifacts。"""
    if mapper not in _artifact_mappers:
        _artifact_mappers.append(mapper)


def register_extension_differ(differ: Callable[[str, str | None, str | None], list[str] | None]) -> None:
    """扩展项注册表（B4c）：(项目, 旧装配哈希, 新装配哈希) → 变了的扩展项类别。"""
    if differ not in _extension_differs:
        _extension_differs.append(differ)


def register_scope_provider(provider: Callable[[str], dict] | None) -> None:
    """写入范围锁（B4b，方案 C10）：项目 → ScopeLock。登记前一律「没有锁」。"""
    global _scope_provider
    _scope_provider = provider


def empty_scope() -> dict:
    return {
        "state": "none", "changeset_id": None, "conversation_id": None, "run_id": None, "slice": None,
        "node": None, "entries": [], "artifacts": [], "paths": [], "reason_text": "", "since": None,
    }


def scope_lock(project_id: str) -> dict:
    if _scope_provider is None:
        return empty_scope()
    try:
        return _scope_provider(project_id) or empty_scope()
    except Exception:  # noqa: BLE001
        _log.exception("写入范围数据源出错（项目 %s）", project_id)
        return empty_scope()


def scope_changed(project_id: str) -> None:
    """一轮开始、结束（以后：变更集确认、撤回）时调：发当前的写入范围锁。"""
    _guard("scope.updated", streams.publish_project, project_id, "scope.updated", scope_lock(project_id))


# ---------------------------------------------------------------------------
# workspace.changed
# ---------------------------------------------------------------------------


def entry_refs(root: Path | None, paths: list[str]) -> list[dict]:
    """改动路径里的本体条目（按项目锁定版本的元模型认目录）。"""
    from ..kernel import metamodel

    try:
        model = metamodel.for_project(root) if root is not None else metamodel.mm
    except Exception:  # noqa: BLE001 - 元模型加载不了：退回镜像版本
        model = metamodel.mm
    refs, seen = [], set()
    for path in paths:
        entry = model.entry_key(path)
        if not entry or entry in seen:
            continue
        seen.add(entry)
        kind, _, key = entry.partition(":")
        refs.append({"kind": kind, "id": entry, "key": key, "name": key})
    return refs


def artifact_refs(root: Path, paths: list[str]) -> list[dict]:
    refs: list[dict] = []
    for mapper in list(_artifact_mappers):
        try:
            refs.extend(mapper(root, paths) or [])
        except Exception:  # noqa: BLE001
            _log.exception("产物映射出错")
    return refs


def workspace_changed(project_id: str, *, commit: str, source: str, by: str, paths: list[str],
                      changeset: str | None = None) -> dict | None:
    """发 EP-05；返回发出的信封。agent 的提交由会话宿主调（source=agent）。"""
    from . import mainline

    manager = _manager()
    try:
        root = manager.project_root(project_id)
    except Exception:  # noqa: BLE001 - 项目已删除
        return None
    paths = list(dict.fromkeys(paths))
    data = {
        "commit": commit,
        "version_label": mainline.label(mainline.count(root, commit)) if commit else None,
        "source": _SOURCES.get(source, source),
        "changeset_id": changeset,
        "by": by or "",
        "entries": entry_refs(root, paths),
        "artifacts": artifact_refs(root, paths),
        "paths": paths[:PATHS_MAX],
    }
    envelope = streams.publish_project(project_id, "workspace.changed", data)
    from .packs import lockfile

    if lockfile.LOCK_REL in paths:
        assembly_changed(project_id)
    schedule_flow_refresh(project_id)
    return envelope


def _on_write(event) -> None:
    if event.target != "mainline":
        return
    _guard("workspace.changed", workspace_changed, event.project_id, commit=event.commit, source=event.source,
           by=event.author, paths=list(event.paths), changeset=event.changeset)


_installed = False
_install_lock = threading.Lock()


def install() -> None:
    """订阅写入事务的变更事件（幂等）；接口层导入事件路由时调用一次。"""
    global _installed
    with _install_lock:
        if _installed:
            return
        from ..kernel.writepath import events

        events.subscribe(_on_write)
        _installed = True


# ---------------------------------------------------------------------------
# flow.updated
# ---------------------------------------------------------------------------

_flow_cache: dict[str, dict] = {}
_flow_lock = threading.Lock()
_flow_timers: dict[str, threading.Timer] = {}


def _flow_view(project_id: str) -> dict | None:
    from .flow import view

    try:
        root = _manager().project_root(project_id)
    except Exception:  # noqa: BLE001
        return None
    if not root.is_dir():
        return None
    return view.build(root)


def _node_key(node: dict) -> tuple:
    return (node.get("state"), repr(node.get("flags")), repr(node.get("progress")))


def ensure_baseline(project_id: str) -> None:
    """项目流打开时记下当前流程，之后的变化才能只发变了的节点。"""
    with _flow_lock:
        if project_id in _flow_cache:
            return
    flow = _guard("flow baseline", _flow_view, project_id)
    if flow is not None:
        with _flow_lock:
            _flow_cache.setdefault(project_id, flow)


def refresh_flow(project_id: str) -> dict | None:
    """重算流程，和上次比：节点状态、切片、建议有变化就发 flow.updated（只含变化的部分）。"""
    flow = _flow_view(project_id)
    if flow is None:
        return None
    with _flow_lock:
        previous = _flow_cache.get(project_id)
        _flow_cache[project_id] = flow
    before = {node["id"]: node for node in (previous or {}).get("nodes") or []}
    nodes = [
        {"id": node["id"], "state": node["state"], "flags": node["flags"], "progress": node["progress"],
         "display_state": node.get("display_state")}
        for node in flow.get("nodes") or []
        if previous is None or node["id"] not in before or _node_key(before[node["id"]]) != _node_key(node)
    ]
    old_slices = {item.get("id"): item for item in (previous or {}).get("slices") or []}
    slices = [
        {"id": item.get("id"), "stages": item.get("stages") or {}, "note": item.get("note") or ""}
        for item in flow.get("slices") or []
        if old_slices.get(item.get("id")) != item
    ]
    suggestions_changed = previous is None or previous.get("suggestions") != flow.get("suggestions")
    hash_changed = previous is not None and previous.get("flow_hash") != flow.get("flow_hash")
    if not (nodes or slices or suggestions_changed or hash_changed):
        # 流程没变，待处理的来源（决策卡、续跑、报告……）可能变了
        _guard("inbox.updated", _refresh_inbox, project_id)
        return None
    data = {"flow_hash": flow.get("flow_hash") or "", "nodes": nodes, "slices": slices,
            "suggestions_changed": suggestions_changed}
    envelope = streams.publish_project(project_id, "flow.updated", data)
    _guard("inbox.updated", _refresh_inbox, project_id)
    if previous is None or previous.get("progress") != flow.get("progress"):
        card_changed(project_id)
    return envelope


def _refresh_inbox(project_id: str) -> None:
    """EP-02：待处理汇总重算，变了才发（详设 4.7）。"""
    from .flow import inbox

    inbox.refresh(_manager(), project_id)


def schedule_flow_refresh(project_id: str, delay: float | None = None) -> None:
    """合并短时间内的多次变化，延迟一点在后台线程里重算（提交线程不等它）。"""
    wait = FLOW_DEBOUNCE_SECONDS if delay is None else delay
    with _flow_lock:
        timer = _flow_timers.pop(project_id, None)
        if timer is not None:
            timer.cancel()

        def run() -> None:
            with _flow_lock:
                _flow_timers.pop(project_id, None)
            _guard("flow.updated", refresh_flow, project_id)

        timer = threading.Timer(wait, run)
        timer.daemon = True
        _flow_timers[project_id] = timer
        timer.start()


def forget(project_id: str) -> None:
    from .flow import inbox

    inbox.forget(project_id)
    with _flow_lock:
        _flow_cache.pop(project_id, None)
        timer = _flow_timers.pop(project_id, None)
    if timer is not None:
        timer.cancel()
    streams.forget_project(project_id)


# ---------------------------------------------------------------------------
# 装配与扩展项
# ---------------------------------------------------------------------------


def assembly_changed(project_id: str) -> dict | None:
    """EP-08：锁文件改了（待生效）或装配切换了（已生效）之后调。"""
    from .packs import service as packs_service

    view = packs_service.assembly_view(_manager(), project_id)
    error = view.get("last_error") or {}
    data = {
        "state": view["state"],
        "pending_changes": view["pending_changes"],
        "effective": view["effective"],
        "error_id": error.get("error_id"),
    }
    envelope = streams.publish_project(project_id, "assembly.updated", data)
    extensions_changed(project_id, None, view.get("active_hash"), pending_hash=view.get("pending_hash"))
    return envelope


def extensions_changed(project_id: str, before: str | None, after: str | None, *,
                       pending_hash: str | None = None) -> dict:
    """EP-07：生效中或待生效的装配变了。"""
    changed: list[str] | None = None
    for differ in list(_extension_differs):
        try:
            changed = differ(project_id, before, after)
        except Exception:  # noqa: BLE001
            _log.exception("扩展项比较出错")
        if changed is not None:
            break
    data = {"active_hash": after, "pending_hash": pending_hash,
            "changed": list(EXTENSION_KINDS) if changed is None else list(changed)}
    return streams.publish_project(project_id, "extensions.updated", data)


def assembly_switched(project_id: str, before: str | None, after: str | None) -> None:
    """消息前切换了装配目录（会话宿主调）：发已生效、扩展项变化，并重算流程。"""
    if before == after:
        return
    _guard("assembly.updated", assembly_changed, project_id)
    schedule_flow_refresh(project_id, delay=0)


def knowledge_changed(project_id: str, ids: list[str], change: str) -> None:
    """EP-23：知识条目新增、修改、删除（知识沉淀页刷新列表）。"""
    _guard("knowledge.updated", streams.publish_project, project_id, "knowledge.updated",
           {"ids": list(ids), "change": change})


# ---------------------------------------------------------------------------
# 项目、对话、维护、用户流
# ---------------------------------------------------------------------------


def conversation_changed(project_id: str, conversation_id: str, *, deleted: bool = False,
                         snapshot: dict | None = None) -> None:
    """EP-21；删除时传删除前的 Conversation（snapshot）。"""
    from .session_host import views

    def publish():
        conversation = snapshot or views.conversation(project_id, conversation_id)
        if conversation is None:
            return
        streams.publish_project(project_id, "conversation.updated", {"conversation": conversation, "deleted": deleted})

    _guard("conversation.updated", publish)


def project_changed(project_id: str, fields: list[str]) -> None:
    """EP-20：项目元数据改了（改名、构建方式、归档）。"""
    from ..api import views

    def publish():
        manager = _manager()
        meta = manager.get_project(project_id)
        root = manager.project_root(project_id)
        streams.publish_project(project_id, "project.updated", {"fields": fields, "meta": views.project_meta(meta, root)})
        card_changed(project_id)

    _guard("project.updated", publish)


def card_changed(project_id: str) -> None:
    """EU-02：项目卡片的进度、状态、最近活动变了，发给所有者的用户流。"""

    def publish():
        from ..routers import projects

        manager = _manager()
        meta = manager.get_project(project_id)
        owner = meta.get("owner") or ""
        streams.publish_user(owner, "project.card.updated", {"project_id": project_id, "card": projects._card(meta)},
                             project_id=project_id)

    _guard("project.card.updated", publish)


def project_removed(owner: str, project_id: str, reason: str) -> None:
    """EU-03：项目删了或转给了别人。"""
    _guard("project.removed", streams.publish_user, owner or "", "project.removed",
           {"project_id": project_id, "reason": reason}, project_id=project_id)
    if reason == "deleted":
        forget(project_id)


def maintenance_changed() -> None:
    """EP-24 / EU-04：维护模式进入、推进、退出。"""
    from ..routers.auth_routes import _maintenance_state

    # 退出维护时 C-01 给 null；事件载荷是对象，给 active=false
    view = _maintenance_state() or {"active": False, "mode": None, "message": "", "steps": []}
    _guard("maintenance.updated", streams.publish_everywhere, "maintenance.updated", view)
