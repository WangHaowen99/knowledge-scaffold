"""`GET /api/v1/projects/{id}/flow` 的响应（契约 FL-01、FL-02；详设 6.6、6.7 第 6 项、6.10）。

- 节点、连线、分组来自装配好的 flow.yaml；
- 节点状态由内核算（`flow/state.py`，方案 D13）：执行记录 + 变更集 + 产物指纹 → 8 种状态与需复核标记，
  完成判定只看主线；
- 下一步建议由 core 的 `flow_state.suggest` 算（方案 D15、D16）：后端（FL-01/FL-02、对话里的建议条）、
  宿主注入、助手命令收尾的 `state_helper.py suggest` 三处同一份算法、同一份输入——后端每算一次就把节点状态
  与上下文写进助手副本的 `.onto/run/flow-state.json`，`state_helper suggest` 读它。

建模节点按段算（`slices.py`）：`slice_stages` 是段里的步骤，`slices` 是每段每步的状态；没有蓝图或只有一段时是
全量模式，只有一段「全部」。建模蓝图草案等你确认时资料分析显示「待确认」、下一步主推「确认蓝图」。
"""

from __future__ import annotations

import functools
import json
import logging
import os
import threading
import time
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
    """本项目锁定版本的 flow_state 支持这一版建议条判定（SUGGEST_RULES）就用它，旧 core 版本用镜像版本：
    规则表、能否执行的检查与说明行（F7）三处要同一份，升级前的项目不能还按旧的判。"""
    if getattr(state, "SUGGEST_RULES", 0) >= 7:
        return state
    from ..packs import contracts

    return contracts.default().load("flow_state")


def _session_context(project_id: str, conversation_id: str | None, root: Path, flow: dict,
                     interrupted_exec: str | None = None) -> dict:
    """会话侧的上下文（详设 6.10 第 1～4 条），只按给定的对话算：在跑（同一个引擎在别的对话跑也算，这时这里
    发不出去）、等你回答的问题、这一轮出错、刚被打断的那一步。interrupted_exec：调用方刚记为中断的执行。"""
    if not conversation_id:
        return {}
    try:
        from ..agent_sessions import manager
    except Exception:  # noqa: BLE001
        return {}
    context: dict = {}
    session = manager.get(project_id)
    if session is not None:
        question = _pending_question(session) if session.conversation_id == conversation_id else None
        if question is not None:
            context["question"] = question
        elif _engine_running(session):
            context["running"] = True
    try:
        history = manager.get_history(project_id, conversation_id)
    except Exception:  # noqa: BLE001 - 对话不存在
        return context
    error, exec_id = _since_last_message(history)
    if error is not None:
        context["failed"] = {"reason": error}
    interrupted = _interrupted(project_id, interrupted_exec or exec_id, root, flow)
    if interrupted is not None:
        context["interrupted"] = interrupted
    return context


def _engine_running(session) -> bool:
    """引擎在跑一轮（打断已经发出的不算：等它停下的这一会儿，建议条给打断后的那一份）。"""
    if getattr(session, "interrupt_requested", False):
        return False
    return session.state in ("running", "queued") or session.is_connecting()


def _pending_question(session) -> dict | None:
    """第一个还没回答的问题（决策卡里的一问，或者一张确认卡）。"""
    for request in session.pending_permissions.values():
        if request.future is None or request.future.done():
            continue
        questions = getattr(request, "questions", None) or []
        answered = set(getattr(request, "answers", None) or {})
        for question in questions:
            if question.get("question_id") not in answered:
                return {"id": question["question_id"], "kind": "question"}
        if not questions:
            return {"id": request.id, "kind": "permission"}
    return None


def _since_last_message(history: list) -> tuple[str | None, str | None]:
    """最近一条用户消息之后：这一轮出错的说法、被打断（或出错中断）的执行。"""
    error, exec_id = None, None
    for event in reversed(history):
        kind = event.get("type")
        if kind == "user_message":
            break
        payload = event.get("payload") or {}
        if kind == "session_error" and error is None:
            error = str(event.get("error") or "")
        if exec_id is not None:
            continue
        if kind == "session_interrupted" and event.get("exec_id"):
            exec_id = event["exec_id"]
        elif kind == "card" and payload.get("card_type") == "resume" and payload.get("exec_id"):
            exec_id = payload["exec_id"]
        elif kind == "suggestions" and (payload.get("primary") or {}).get("action", {}).get("type") == "resume_exec":
            exec_id = payload["primary"]["action"].get("exec_id")  # F7 之前的历史：打断后推的建议条里有
    return error, exec_id


def _interrupted(project_id: str, exec_id: str | None, root: Path, flow: dict) -> dict | None:
    """还停在中断的执行 → 第 4 条用的 {id, label, mode, why, saved_files, phase}：能续跑 resume，能力升级或输入
    改过 rerun（按新版从头做）。已经续跑、放弃、做完的不算。"""
    if not exec_id:
        return None
    from ..session_host import exec_records

    record = exec_records.get(project_id, exec_id)
    if record is None or record.get("state") != "interrupted" or record.get("run_id"):
        return None
    data = exec_records.view(record, root=root, flow=flow)
    return {"id": record["id"], "label": data["command_label"], "mode": "resume" if data["resumable"] else "rerun",
            "why": data["not_resumable_reason"], "saved_files": data["saved"]["files"],
            "phase": data["phase"]["label"]}


_autobuild_context = None


def register_autobuild_context(provider) -> None:
    """自主构建（B5b）登记：项目 → {"autobuild": True}（运行中，建议条换成运行控制，详设 6.10）、
    {"autobuild_paused": True}（暂停、中断）或 {}。"""
    global _autobuild_context
    _autobuild_context = provider


def _run_context(project_id: str) -> dict:
    if _autobuild_context is None:
        return {}
    try:
        return _autobuild_context(project_id) or {}
    except Exception:  # noqa: BLE001 - 取不到按没有运行
        _log.warning("项目 %s 的自主构建状态取不到", project_id, exc_info=True)
        return {}


def _paused_controls() -> dict:
    """暂停、中断的运行：不推荐下一步，只留「查看运行详情」（续跑、停在安全点在运行条上）。"""
    details = {"id": "autobuild_details", "label": "查看运行详情", "icon": "list", "primary": False, "disabled": False,
               "reason": "", "priority": 1, "action": {"type": "autobuild", "op": "details"}}
    return {"mode": "autobuild_controls", "primary": None, "secondary": [details], "all_commands": True, "note": None}


_rev_lock = threading.Lock()
_last_rev = 0


def next_rev() -> int:
    """建议条的版本号：毫秒时间戳，同一进程里只增不减。界面上推来的和刷新取到的，取版本号大的那份。"""
    global _last_rev
    with _rev_lock:
        _last_rev = max(_last_rev + 1, time.time_ns() // 1_000_000)
        return _last_rev


def running_suggestions() -> dict:
    """第 1 条：运行中不显示建议（会话变为运行中时直接推这一份，不必重算流程）。"""
    return {"mode": "running", "primary": None, "secondary": [], "all_commands": True, "note": None,
            "rev": next_rev()}


def _unfinished_step(project_id: str, conversation_id: str | None, roots: list[Path], flow: dict) -> dict | None:
    """这条对话里的命令这一轮结束了、命令锁却还在（执行记录「等待」，又没有等你回答的问题），锁也不在「等你回复」
    的阶段：助手没收尾（真实模型常见：正文说做完了，没释放锁），节点一直显示进行中。返回 {id, label}。"""
    if not conversation_id:
        return None
    from ..session_host import exec_records

    record = exec_records.open_record(project_id, conversation_id)
    if record is None or record.get("state") != "waiting":
        return None
    command = record.get("command_name") or ""
    if not any(exec_records.unfinished(root, flow, command) for root in roots if Path(root).is_dir()):
        return None
    return {"id": record["id"], "label": record.get("command_label") or ""}


def _awaiting_reply(project_id: str, conversation_id: str | None, roots: list[Path], flow: dict) -> dict | None:
    """这条对话里的命令停下来等你在对话里回复：执行记录「等待」，锁在命令声明的等回复阶段（没有确认卡、靠打字回复的
    那种）。返回 {id, label}。"""
    if not conversation_id:
        return None
    from ..session_host import exec_records

    record = exec_records.open_record(project_id, conversation_id)
    if record is None or record.get("state") != "waiting":
        return None
    command = record.get("command_name") or ""
    for root in roots:
        if not Path(root).is_dir():
            continue
        phase = exec_records.lock_phase(root, command)
        if phase is not None and exec_records.waiting_for_reply(phase, flow, command):
            return {"id": record["id"], "label": record.get("command_label") or ""}
    return None


def _confine_active(flow: dict, root: Path) -> bool:
    """范围限定生效：组装结果带 confine，主线上的范围文件 status 是 active（ADR-0085；读不出算没生效，同守卫）。"""
    spec = flow.get("confine")
    if not isinstance(spec, dict) or not isinstance(spec.get("scope_file"), str) or not spec["scope_file"]:
        return False
    try:
        scope = json.loads((root / spec["scope_file"]).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    return isinstance(scope, dict) and scope.get("status") == "active"


def _stale_upstream(flow: dict, states: dict, computed: dict | None = None) -> dict:
    """过期的节点（按段的除外）→ 确认之后变了的那几类的说法：「对象、操作」（computed 是 node_state.compute 的结果，
    带 changed；没有时按 stale_on 全列）。"""
    from .. import workspace_labels

    out = {}
    for node in flow.get("nodes") or []:
        if node.get("per_slice") or (states.get(node["id"]) or {}).get("state") != "stale":
            continue
        kinds = ((computed or {}).get(node["id"]) or {}).get("changed")
        text = workspace_labels.kinds_text(node.get("stale_on") or [] if kinds is None else kinds)
        if text:
            out[node["id"]] = text
    return out


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
    goal_pending = any(item.get("kind") == "goal" and not item.get("answer") for item in doc.get("questions") or [])
    return doc, {"state": doc.get("state") or "draft", "where": where, "next": build_mode, "goal_pending": goal_pending}


def _apply_slices(states: dict, sliced: dict | None) -> None:
    """建模节点的状态按各段汇总：全部段确认才算确认，任何一段过期就是过期，其余显示进行中（详设 6.6）。"""
    if not sliced or sliced["node_id"] not in states:
        return
    item = states[sliced["node_id"]]
    if item["state"] == "blocked" and sliced["state"] in (None, "running") and not sliced.get("running_exec"):
        return  # 挡住它的关卡没过：做了一部分也显示被挡住（在跑、待确认、过期、做完的照常汇总）
    if sliced["state"] and item["state"] != "running":
        item["state"] = sliced["state"]
        item["display_state"] = node_state.display(sliced["state"], item["flags"])


def node_states(root: Path, flow: dict, hits: dict) -> tuple[dict, dict | None, dict | None, dict, set[str]]:
    """节点状态（含建模节点按段的汇总）：(states, 按段的结果, 蓝图, 蓝图上下文, 熔断的节点)。FL-01 与待处理同一份。

    熔断的节点（O-29）不再挡别的节点：按拿掉它们 `blocks` 的流程算；熔断、可补做的标记由调用方在算完之后补。"""
    doc, blueprint_context = _blueprint(root)
    sliced = slices.compute(root, flow, doc=doc)
    if sliced is not None and sliced["sliced"]:
        hits[sliced["node_id"]] = sliced["hit"]  # 按段模式：全部段做完建模节点才算完成
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
    stale_upstream = _stale_upstream(flow, states, states)
    # F8 不再分段：蓝图草案不用确认，资料分析做完就是做完，不再停在「等你确认」
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
                                                              blueprint_draft=blueprint_context["state"] == "draft",
                                                              probes=probes, shown=getattr(state, "action_shown", None),
                                                              cause=stale_upstream.get(node["id"], "")),
            "hooks": [{key: hook[key] for key in ("id", "title", "when", "command", "pack")}
                      for hook in hooks.get(node["id"], [])],
            "plugin": None if node.get("pack", "core") == "core" else _plugin_ref(node),
            "optional": bool(node.get("optional")),
            "panel": bool(node.get("panel")),
            # 可补做的节点做完之前不计入进度：越过插入位置的项目，主线进度不往回退（详设 6.9 第 4 步）
            "counts_in_progress": bool(node.get("progress", True)) and node["id"] not in passed,
            **({"last_run": script_runs.get(node["id"])} if node.get("kind") == "script" else {}),
        })
    nodes += [_disabled_view(node) for node in overlay.disabled_nodes(root, flow)]
    sections = [
        {"id": key, "title": title, "nodes": [node["id"] for node in nodes if node["section"] == key]}
        for key, title in SECTION_TITLES.items()
    ]
    commands = {
        name: spec.get("node") for name, spec in (flow.get("commands") or {}).items()
        if spec.get("node")
    }
    changeset = _changeset_context(root)
    simple_states = {node_id: {"state": item["state"], "flags": item["flags"]} for node_id, item in states.items()}
    suggester = _suggest_module(root, state)
    suggest_stage = stage
    suggest_probes = probes
    if changeset and (copy / ".git").is_file():
        # 有没确认的变更集：下一步按助手副本里的样子算（和助手收尾时 state_helper suggest 看到的一样）
        copy_probes = state.evaluate(flow, copy)
        suggest_stage = state.legacy_stage(flow, copy, copy_probes)
        suggest_probes = copy_probes
        suggester.overlay_pending(flow, simple_states, state.node_hits(flow, copy, copy_probes))
        for node in nodes:
            if node["id"] not in simple_states:
                continue
            changed = simple_states[node["id"]]["state"]
            if changed != node["state"]:
                node["state"] = changed
                node["display_state"] = node_state.display(changed, node["flags"])
    extra = dict(context or {})
    interrupted_exec = extra.pop("interrupted_exec", None)
    suggest_context = {"stage": suggest_stage, "probes": suggest_probes, "changeset": changeset, "blueprint": blueprint_context,
                       "slice_next": (sliced or {}).get("slice_next"),
                       "slice_stale": (sliced or {}).get("slice_stale") or [],
                       "stale_upstream": _stale_upstream(flow, simple_states, states),
                       "confine_active": _confine_active(flow, root),
                       **_session_context(root.name, conversation_id, root, flow, interrupted_exec),
                       **_run_context(root.name), **extra}
    unfinished = _unfinished_step(root.name, conversation_id, [copy, root], flow)
    if unfinished is not None:
        suggest_context.setdefault("unfinished", unfinished)
    awaiting = _awaiting_reply(root.name, conversation_id, [copy, root], flow)
    if awaiting is not None:
        suggest_context.setdefault("awaiting_reply", awaiting)
    check = None
    if conversation_id:
        # 按对话算的（建议条、FL-02）：只给此刻能执行的，和接口收到建议条的请求时同一份检查（next_step）
        from . import next_step

        check = functools.partial(next_step.allowed, root.name, conversation_id, root=root)
    suggestions = suggester.suggest(flow, simple_states, suggest_context, check)
    if suggest_context.get("autobuild_paused"):
        suggestions = _paused_controls()
    suggestions = {**suggestions, "note": suggestions.get("note"), "rev": next_rev()}
    progress_nodes = [node for node in nodes if node["counts_in_progress"]]
    result = {
        "flow_hash": flow.get("flow_hash") or "",
        "nodes": nodes,
        "edges": list(flow.get("edges") or []),
        "sections": sections,
        # 有没有建模蓝图：没有时建模步骤抽屉的「调整分段」改为发起分段规划（设计调整，gaps B4d 合并）
        "blueprint": {"state": blueprint_context["state"]},
        "slice_stages": (sliced or {}).get("stages") or [],
        "slices": (sliced or {}).get("slices") or [],
        "suggestions": suggestions,
        "notices": _notices(suggest_context) + _assembly_notice(root),
        "stage": stage,
        # 进度：主线上满足完成判定的节点（已确认与过期都算做过）；助手副本里的改动要这一轮合入才算
        "progress": {
            "done": sum(1 for node in progress_nodes if hits.get(node["id"])),
            "total": len(progress_nodes),
        },
        "commands": commands,
    }
    _write_flow_state(root, copy, flow, simple_states, suggest_context, suggestions)
    return result


@functools.lru_cache(maxsize=256)
def _pack_label(data_dir: str, pack: str, version: str) -> str:
    """包清单里的显示名（包仓库里的版本目录不改写，按版本缓存）；读不到用包名。"""
    from ...api import deps

    try:
        return deps.workspaces.packs.repository.manifest(pack, version).label or pack
    except Exception:  # noqa: BLE001 - 包仓库里没有这一版（测试、旧装配）
        return pack


def _plugin_ref(node: dict) -> dict:
    """节点来自哪个包：节点上显示清单里的名字，不是包名。"""
    pack = node["pack"]
    version = str(node.get("version") or "").partition("@")[2]
    from ...api import deps

    data_dir = str(getattr(deps.workspaces, "data_dir", ""))
    return {"name": pack, "label": _pack_label(data_dir, pack, version) if version else pack}


def _disabled_view(node: dict) -> dict:
    """停用了的包的节点：产物保留，列出来标「已停用」，不进进度、没有动作（详设 6.9 第 5 步）。"""
    flags = {"needs_review": False, "disabled": True, "backfill": False}
    return {
        "id": node["id"], "title": str(node.get("title") or node["id"]), "kind": node.get("kind", "agent"),
        "placement": node.get("placement", "step"), "section": node.get("section", "optional"),
        "state": "skipped", "flags": flags, "display_state": node_state.display("skipped", flags),
        "progress": None, "version": node.get("version") or "", "per_slice": False, "blocks": [],
        "actions": [], "plugin": _plugin_ref(node), "optional": True, "panel": False,
        "counts_in_progress": False,
    }


def _hooks_by_node(flow: dict) -> dict[str, list[dict]]:
    out: dict[str, list[dict]] = {}
    for hook in flow.get("hooks") or []:
        out.setdefault(hook["node"], []).append(hook)
    return out


def _actions(node: dict, state: str, hooks: list[dict] | None = None, *, blueprint_draft: bool = False,
             probes: dict | None = None, shown=None, cause: str = "") -> list[dict]:

    def offered(action: dict) -> bool:
        has_target = bool(action.get("command") or action.get("page"))
        return has_target and (shown is None or shown(action, probes))

    actions = [
        {"kind": "start", "label": action["label"], "target": action.get("command") or action["page"],
         **({"verify": True} if action.get("verify") else {})}
        for action in node.get("actions") or [] if offered(action)
    ]
    # 节点钩子（详设 6.4）：之前的排在节点自己的动作前面，之后的排在后面
    before = [{"kind": "start", "label": hook["title"], "target": hook["command"], "hook": hook["id"]}
              for hook in hooks or [] if hook.get("when") == "before"]
    after = [{"kind": "start", "label": hook["title"], "target": hook["command"], "hook": hook["id"]}
             for hook in hooks or [] if hook.get("when") != "before"]
    actions = before + actions + after
    if state == "stale" and actions and actions[0]["target"].startswith("/"):
        # 和建议条同一个说法：「对象有变化，刷新校验」
        actions = [{**actions[0], "kind": "refresh",
                    "label": f"{cause}有变化，刷新{node['title']}" if cause else f"刷新{node['title']}"}, *actions]
    if node["id"] == "core.research" and blueprint_draft:
        actions = [{"kind": "confirm", "label": "去确认", "target": "wb-blueprint"}, *actions]
    if node.get("optional"):
        actions.append({"kind": "restore" if state == "skipped" else "skip",
                        "label": "恢复" if state == "skipped" else "跳过"})
    return actions


def _notices(context: dict) -> list[dict]:
    changeset = context.get("changeset")
    if changeset and changeset.get("state") == "conflict":
        # 一轮结束就合入（F3）：只剩合入冲突要你处理
        return [{"kind": "changeset_conflict", "count": 1, "text": "这一轮和你的修改有冲突",
                 "action": {"type": "navigate", "page": "inbox"}}]
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


# 写给助手的 flow-state.json 里带的上下文字段
_CONTEXT_KEYS = ("stage", "changeset", "autobuild", "blueprint", "slice_next", "slice_stale", "stale_upstream",
                 "confine_active")


def _write_flow_state(root: Path, copy: Path, flow: dict, states: dict, context: dict, suggestions: dict) -> None:
    """节点状态与上下文写进助手副本（没有副本时写主线工作区）的 `.onto/run/flow-state.json`：
    `state_helper.py suggest` 读它，按同一份输入、同一份算法给出下一步（方案 D15）。"""
    from ...services import mainline

    payload = {
        "flow_hash": flow.get("flow_hash") or "",
        "mainline_head": mainline.state(root)["head"],
        # 本项目不出现的节点（ADR-0083）还在装配目录的 flow.yaml 里：记成跳过，state_helper 不会把它当下一步
        "nodes": {**{node_id: {"state": "skipped", "flags": {"hidden": True}} for node_id in flow.get("hidden_nodes") or []},
                  **states},
        "context": {key: value for key, value in context.items() if key in _CONTEXT_KEYS},
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
    的节点；声明 `blocks_in: sliced` 的只在按段建模时挡。没确认就挡住发布，动作是去做这一步。"""
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
