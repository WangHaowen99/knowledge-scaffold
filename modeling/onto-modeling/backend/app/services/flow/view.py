"""`GET /api/v1/projects/{id}/flow` 的响应（契约 FL-01、FL-02；详设 6.6、6.7 第 6 项、6.10）。

- 节点、连线、分组来自装配好的 flow.yaml；
- 节点状态由内核算（`flow/state.py`，方案 D13）：执行记录 + 变更集 + 产物指纹 → 8 种状态与需复核标记，
  完成判定只看主线；
- 下一步建议由 core 的 `flow_state.suggest` 算（方案 D15、D16）：后端（FL-01/FL-02、对话里的建议条）、
  宿主注入、助手命令收尾的 `state_helper.py suggest` 三处同一份算法、同一份输入——后端每算一次就把节点状态
  与上下文写进助手副本的 `.onto/run/flow-state.json`，`state_helper suggest` 读它。

建模节点按切片算（`slices.py`）：`slice_stages` 是片里的步骤，`slices` 是每片每步的状态；没有蓝图或只有一片时是
全量模式，只有一片「全部」。建模蓝图草案等你确认时资料分析显示「待确认」、下一步主推「确认蓝图」。
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path

from . import model, overlay, slices
from . import state as node_state

_log = logging.getLogger(__name__)

SECTION_TITLES = {
    "required": "必经步骤",
    "pre_publish": "发布前检查",
    "optional": "可选步骤",
    "on_demand": "有需要时",
}
FLOW_STATE_REL = Path(".onto") / "run" / "flow-state.json"


def _lock_running(flow: dict, roots: list[Path], state) -> set[str]:
    """工具箱命令锁 `.ontology-state/lock-<命令>.json` 在的命令所属节点（主线工作区与助手副本都看）：
    不经网页宿主跑的命令（终端里直接跑 CLI）也算进行中。"""
    running = set()
    for root in roots:
        directory = Path(root) / ".ontology-state"
        try:
            locks = list(directory.glob("lock-*.json"))
        except OSError:
            continue
        for path in locks:
            command = path.name[len("lock-"):-len(".json")]
            node = state.command_node(flow, command)
            if node and _lock_alive(Path(root), command):
                running.add(node)
    return running


def _lock_alive(root: Path, command: str) -> bool:
    """命令锁的持有者还在（与内核守卫同一口径）：引擎换了实例、进程没了的旧锁不算进行中。"""
    from ...kernel_guards import _guard_common

    return _guard_common._command_alive(root, command)


def _suggest_module(root: Path, state):
    """本项目锁定版本的 flow_state 有 suggest 就用它，旧 core 版本没有时用镜像版本。"""
    if hasattr(state, "suggest"):
        return state
    from ..packs import contracts

    return contracts.default().load("flow_state")


def _session_context(project_id: str, conversation_id: str | None) -> dict:
    """会话侧的上下文：这条对话刚被打断（建议条换成打断后的三项）、有等你回答的问题。"""
    try:
        from ..agent_sessions import manager
    except Exception:  # noqa: BLE001
        return {}
    context: dict = {}
    session = manager.get(project_id)
    if session is not None and (conversation_id in (None, "", session.conversation_id)):
        waiting = [request for request in session.pending_permissions.values()
                   if request.future is not None and not request.future.done()]
        if waiting:
            context["waiting_decision"] = True
    if conversation_id:
        try:
            history = manager.get_history(project_id, conversation_id)
        except Exception:  # noqa: BLE001 - 对话不存在
            return context
        for event in reversed(history):
            kind = event.get("type")
            if kind == "user_message":
                break
            if kind == "suggestions":
                payload = event.get("payload") or {}
                if payload.get("mode") == "interrupted":
                    action = (payload.get("primary") or {}).get("action") or {}
                    context["interrupted"] = {"id": action.get("exec_id")} if action.get("type") == "resume_exec" else {}
                break
    return context


def _changeset_context(root: Path) -> dict | None:
    from ...kernel.changesets import store

    record = store.current(root)
    return {"id": record["id"], "state": record["state"]} if record else None


def _blueprint(root: Path) -> tuple[dict | None, dict]:
    """界面上看到的蓝图与给下一步用的上下文 {state: none|draft|confirmed, where, next}。"""
    from ...api import deps
    from . import blueprint

    try:
        base, where, _record = blueprint.locate(deps.workspaces, root.name)
        doc = blueprint.load_doc(base)
        build_mode = (deps.workspaces.get_project(root.name) or {}).get("build_mode") or "L2"
    except Exception:  # noqa: BLE001 - 不是平台管理的工作区（单测的临时目录）
        doc, where, build_mode = blueprint.load_doc(root), "mainline", "L2"
    if doc is None:
        return None, {"state": "none"}
    return doc, {"state": doc.get("state") or "draft", "where": where, "next": build_mode}


def _apply_slices(states: dict, sliced: dict | None) -> None:
    """建模节点的状态按各片汇总：全部片确认才算确认，任何一片过期就是过期，其余显示进行中（详设 6.6）。"""
    if not sliced or sliced["node_id"] not in states:
        return
    item = states[sliced["node_id"]]
    if item["state"] == "blocked" and sliced["state"] in (None, "running") and not sliced.get("running_exec"):
        return  # 挡住它的关卡没过：做了一部分也显示被挡住（在跑、待确认、过期、做完的照常汇总）
    if sliced["state"] and item["state"] != "running":
        item["state"] = sliced["state"]
        item["display_state"] = node_state.display(sliced["state"], item["flags"])


def node_states(root: Path, flow: dict, hits: dict) -> tuple[dict, dict | None, dict | None, dict, set[str]]:
    """节点状态（含建模节点按切片的汇总）：(states, 按切片的结果, 蓝图, 蓝图上下文, 熔断的节点)。FL-01 与待处理同一份。

    熔断的节点（O-29）不再挡别的节点：按拿掉它们 `blocks` 的流程算；熔断、可补做的标记由调用方在算完之后补。"""
    doc, blueprint_context = _blueprint(root)
    sliced = slices.compute(root, flow, doc=doc)
    if sliced is not None and sliced["sliced"]:
        hits[sliced["node_id"]] = sliced["hit"]  # 按片模式：全部片做完建模节点才算完成
    broken = overlay.broken_nodes(flow)
    states = node_state.compute(root, overlay.released(flow, broken), hits, sliced=bool(sliced and sliced["sliced"]))
    _apply_slices(states, sliced)
    return states, sliced, doc, blueprint_context, broken


def build(root: Path, *, conversation_id: str | None = None, context: dict | None = None) -> dict:
    root = Path(root)
    flow = model.load_for(root)
    state = model.flow_state(root)
    probes = state.evaluate(flow, root)
    hits = state.node_hits(flow, root, probes)
    stage = state.legacy_stage(flow, root, probes)
    from ...kernel.changesets import worktree

    copy = worktree.copy_path(root)
    states, sliced, _doc, blueprint_context, broken = node_states(root, flow, hits)
    for node_id in _lock_running(flow, [root, copy], state):
        if node_id in states and states[node_id]["state"] not in ("running", "pending_confirm"):
            states[node_id]["state"] = "running"
            states[node_id]["display_state"] = node_state.display("running", states[node_id]["flags"])
    overlay.mark_broken(states, broken)
    overlay.mark_backfill(flow, states, hits)
    passed = overlay.backfill_ids(flow, hits)
    hooks = _hooks_by_node(flow)
    from . import node_kinds

    script_runs = node_kinds.last_runs(root)
    research = states.get("core.research")
    if research and blueprint_context["state"] == "draft" and research["state"] in ("confirmed", "stale"):
        # 资料分析含切片规划：蓝图草案没确认之前，这一步等你确认（详设 6.8）
        research["state"] = "pending_confirm"
        research["display_state"] = node_state.display("pending_confirm", research["flags"])
    nodes = []
    for node in flow.get("nodes") or []:
        item = states[node["id"]]
        nodes.append({
            "id": node["id"],
            "title": node["title"],
            "kind": node.get("kind", "agent"),
            "placement": node.get("placement", "spine"),
            "section": node.get("section", "required"),
            "state": item["state"],
            "flags": item["flags"],
            "display_state": item["display_state"],
            "progress": sliced["progress"] if sliced and sliced["sliced"] and node["id"] == sliced["node_id"] else None,
            "version": node.get("version") or "",
            "per_slice": bool(node.get("per_slice")),
            "blocks": list(node.get("blocks") or []),
            "actions": [] if item.get("broken") else _actions(node, item["state"], hooks.get(node["id"], []),
                                                              blueprint_draft=blueprint_context["state"] == "draft"),
            "hooks": [{key: hook[key] for key in ("id", "title", "when", "command", "pack")}
                      for hook in hooks.get(node["id"], [])],
            "plugin": None if node.get("pack", "core") == "core" else {"name": node["pack"], "label": node["pack"]},
            "optional": bool(node.get("optional")),
            "panel": bool(node.get("panel")),
            # 可补做的节点做完之前不计入进度：越过插入位置的项目，主线进度不往回退（详设 6.9 第 4 步）
            "counts_in_progress": bool(node.get("progress", True)) and node["id"] not in passed,
            **({"last_run": script_runs.get(node["id"])} if node.get("kind") == "script" else {}),
        })
    nodes += [_disabled_view(node) for node in overlay.disabled_nodes(root, flow)]
    # 外部节点要证据源提供方，本期证据源只留接口（ADR-0056）：不下发
    hidden = {node["id"] for node in nodes if node["kind"] == "external"}
    nodes = [node for node in nodes if node["id"] not in hidden]
    sections = [
        {"id": key, "title": title, "nodes": [node["id"] for node in nodes if node["section"] == key]}
        for key, title in SECTION_TITLES.items()
    ]
    commands = {
        name: spec.get("node") for name, spec in (flow.get("commands") or {}).items()
        if spec.get("node") and spec.get("node") not in hidden
    }
    changeset = _changeset_context(root)
    simple_states = {node_id: {"state": item["state"], "flags": item["flags"]} for node_id, item in states.items()}
    suggester = _suggest_module(root, state)
    suggest_stage = stage
    if changeset and (copy / ".git").is_file():
        # 有没确认的变更集：下一步按助手副本里的样子算（和助手收尾时 state_helper suggest 看到的一样）
        copy_probes = state.evaluate(flow, copy)
        suggest_stage = state.legacy_stage(flow, copy, copy_probes)
        suggester.overlay_pending(flow, simple_states, state.node_hits(flow, copy, copy_probes))
        for node in nodes:
            if node["id"] not in simple_states:
                continue
            changed = simple_states[node["id"]]["state"]
            if changed != node["state"]:
                node["state"] = changed
                node["display_state"] = node_state.display(changed, node["flags"])
    suggest_context = {"stage": suggest_stage, "changeset": changeset, "blueprint": blueprint_context,
                       "slice_next": (sliced or {}).get("slice_next"),
                       **_session_context(root.name, conversation_id), **(context or {})}
    suggestions = suggester.suggest(flow, simple_states, suggest_context)
    progress_nodes = [node for node in nodes if node["counts_in_progress"]]
    result = {
        "flow_hash": flow.get("flow_hash") or "",
        "nodes": nodes,
        "edges": [edge for edge in flow.get("edges") or [] if not hidden & {edge.get("from"), edge.get("to")}],
        "sections": sections,
        # 有没有建模蓝图：没有时建模步骤抽屉的「调整切片」改为发起切片规划（设计调整，gaps B4d 合并）
        "blueprint": {"state": blueprint_context["state"]},
        "slice_stages": (sliced or {}).get("stages") or [],
        "slices": (sliced or {}).get("slices") or [],
        "suggestions": suggestions,
        "notices": _notices(suggest_context) + _assembly_notice(root),
        "stage": stage,
        # 进度：主线上满足完成判定的节点（已确认与过期都算做过）；助手副本里的改动要确认生效才算
        "progress": {
            "done": sum(1 for node in progress_nodes if hits.get(node["id"])),
            "total": len(progress_nodes),
        },
        "commands": commands,
    }
    _write_flow_state(root, copy, flow, simple_states, suggest_context, suggestions)
    return result


def _disabled_view(node: dict) -> dict:
    """停用了的包的节点：产物保留，列出来标「已停用」，不进进度、没有动作（详设 6.9 第 5 步）。"""
    flags = {"needs_review": False, "disabled": True, "backfill": False}
    return {
        "id": node["id"], "title": str(node.get("title") or node["id"]), "kind": node.get("kind", "agent"),
        "placement": node.get("placement", "step"), "section": node.get("section", "optional"),
        "state": "skipped", "flags": flags, "display_state": node_state.display("skipped", flags),
        "progress": None, "version": node.get("version") or "", "per_slice": False, "blocks": [],
        "actions": [], "plugin": {"name": node["pack"], "label": node["pack"]}, "optional": True, "panel": False,
        "counts_in_progress": False,
    }


def _hooks_by_node(flow: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for hook in flow.get("hooks") or []:
        out.setdefault(hook["node"], []).append(hook)
    return out


def _actions(node: dict, state: str, hooks: list[dict] | None = None, *, blueprint_draft: bool = False) -> list[dict]:
    actions = [
        {"kind": "start", "label": action["label"], "target": action.get("command") or action["page"],
         **({"verify": True} if action.get("verify") else {})}
        for action in node.get("actions") or [] if action.get("command") or action.get("page")
    ]
    # 节点钩子（详设 6.4）：之前的排在节点自己的动作前面，之后的排在后面
    before = [{"kind": "start", "label": hook["title"], "target": hook["command"], "hook": hook["id"]}
              for hook in hooks or [] if hook.get("when") == "before"]
    after = [{"kind": "start", "label": hook["title"], "target": hook["command"], "hook": hook["id"]}
             for hook in hooks or [] if hook.get("when") != "before"]
    actions = before + actions + after
    if state == "stale" and actions and actions[0]["target"].startswith("/"):
        actions = [{**actions[0], "kind": "refresh", "label": f"刷新「{node['title']}」"}, *actions]
    if node["id"] == "core.research" and blueprint_draft:
        actions = [{"kind": "confirm", "label": "去确认", "target": "wb-blueprint"}, *actions]
    if node.get("optional"):
        actions.append({"kind": "restore" if state == "skipped" else "skip",
                        "label": "恢复" if state == "skipped" else "跳过"})
    return actions


def _notices(context: dict) -> list[dict]:
    changeset = context.get("changeset")
    if changeset and changeset.get("state") in ("pending_confirm", "conflict"):
        return [{"kind": "pending_changeset", "count": 1, "text": "这一轮的修改还没确认",
                 "action": {"type": "show_changes", "changeset_id": changeset["id"]}}]
    return []


def _assembly_notice(root: Path) -> list[dict]:
    """启用、升级、停用了包还没生效（下一条消息前切换）：流程按生效中的装配显示，另给一条提示（契约 7.8 notices）。"""
    from ...api import deps

    try:
        status = deps.workspaces.packs.status(root)
    except Exception:  # noqa: BLE001 - 取不到装配状态不影响流程
        return []
    if status.get("state") != "pending" or status.get("effective") != "next_message":
        return []
    return [{"kind": "assembly_pending", "count": len(status.get("pending_changes") or []),
             "text": "插件变更下一条消息生效", "action": {"type": "navigate", "page": "proj-settings"}}]


def _write_flow_state(root: Path, copy: Path, flow: dict, states: dict, context: dict, suggestions: dict) -> None:
    """节点状态与上下文写进助手副本（没有副本时写主线工作区）的 `.onto/run/flow-state.json`：
    `state_helper.py suggest` 读它，按同一份输入、同一份算法给出下一步（方案 D15）。"""
    from ...services import mainline

    payload = {
        "flow_hash": flow.get("flow_hash") or "",
        "mainline_head": mainline.state(root)["head"],
        "nodes": states,
        "context": {key: value for key, value in context.items()
                    if key in ("stage", "changeset", "autobuild", "blueprint", "slice_next")},
        "suggestions": suggestions,
    }
    for target in ([copy] if (copy / ".git").is_file() else [root]):
        path = target / FLOW_STATE_REL
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_name(f".flow-state.{uuid.uuid4().hex[:8]}.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            _log.debug("写 flow-state.json 失败：%s", path)


PUBLISH_NODE = "core.publish"


def publish_gates(root: Path) -> list[dict]:
    """发布前的流程关卡（契约 PB-01 `gates` 里 `source.kind=flow` 的几道，7.14 GateResult）：`blocks` 指向发布节点
    的节点；声明 `blocks_in: sliced` 的（全局一致性）只在按片建模时挡。没确认就挡住发布，动作是去做这一步。"""
    root = Path(root)
    flow = model.load_for(root)
    declared = {node["id"]: node for node in flow.get("nodes") or []
                if PUBLISH_NODE in (node.get("blocks") or [])}
    if not declared:
        return []
    built = build(root)
    sliced = any(item["id"] != slices.FULL_SLICE["id"] for item in built["slices"])
    gates = []
    for node in built["nodes"]:
        spec = declared.get(node["id"])
        if spec is None or (spec.get("blocks_in") == "sliced" and not sliced):
            continue
        passed = node["state"] in ("confirmed", "skipped")
        plugin = node.get("plugin")
        gates.append({
            "id": f"flow:{node['id']}", "order": 0, "title": node["title"],
            "source": {"kind": "flow", "label": plugin["label"] if plugin else "主流程",
                       **({"pack": plugin["name"]} if plugin else {})},
            "status": "pass" if passed else "fail" if node["state"] == "stale" else "not_run",
            "summary": "" if passed else "过期" if node["state"] == "stale" else "还没运行",
            "blocking": not passed, "degradable": False, "findings": [],
            "action": None if passed else {"type": "start_node", "node_id": node["id"]},
        })
    return gates
