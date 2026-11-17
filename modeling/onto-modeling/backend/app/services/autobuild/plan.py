"""一次自主构建做哪些事（详设 8.5「范围」）：流程里资料分析之后、到策略的 `until_node` 为止（含）的节点，一个节点一个
单元、一步，跑节点的主命令（`entry.command`）。发布、增量更新是硬性不做的，不在里面。

单元：`{id: 节点, name: 节点名, kind: "node", steps: [{id, label, title, action, command, args}], carried?}`。

- 哪些节点能做（这一批沿用原来的规则，批次 3 改成按 `need` 判）：主线上的（`placement: spine`）、不是人工或面板节点、
  没声明 `autobuild: false`、有主命令；可选的只在「包括可选步骤」时做；
- 已确认、已跳过的不再做；过期的按 `refresh_stale_first` 决定要不要重做；
- 之前的运行里「留到下一次」的挂起问题（`carried`）所在的节点照做，即使已经做完（审查 I）；开始时从别的对话接过来、
  停在干活中途的节点也照做（redo）。

老运行（这一版之前开始的）的单元按段：`{id: "all"（F8 之前是 s1…）, kind: "slice", steps: [对象, 操作…]}`，在运行记录里原样
留着，运行器、详情、撤回照样认；`unit_node`、`nodes_of` 把它们落到现在的节点上。
"""

from __future__ import annotations

from pathlib import Path

START_AFTER = "core.research"
SKIP = ("core.publish", "core.update")
STATE_TEXT = {"confirmed": "已确认", "stale": "有过期", "skipped": "已跳过", "running": "进行中", "pending_confirm": "待确认"}
# 老运行按段的单元里，哪几步的确认记录落在现在的哪个节点上（撤回时放回去）。规则补抽一步的记录拆分时没搬到指标上
# （两边的输入指纹口径不同），这里也不管
RECORD_STEPS = {"object": "core.object", "operation": "core.operation"}
# 锁定旧 core 的项目：流程里还是建模节点（core.model），主命令只提取对象，这个单元按原来的两步做
LEGACY_MODEL_STEPS = (("object", "提取对象", "/ontology-extract-object"),
                      ("operation", "提取操作", "/ontology-extract-operation"))


def flow(root: Path) -> dict:
    from ..flow import model

    return model.load_for(Path(root))


def node_ids(flow_doc: dict) -> list[str]:
    return [node["id"] for node in flow_doc.get("nodes") or []]


def _command(node: dict) -> str:
    return ((node.get("entry") or {}).get("command") or "").strip()


def _eligible(node: dict, policy: dict) -> bool:
    return not (node["id"] in SKIP or node.get("placement", "spine") != "spine" or node.get("autobuild") is False
                or node.get("kind") == "human" or node.get("panel") or not _command(node)
                or (node.get("optional") and not policy["scope"].get("include_optional")))


def candidates(flow_doc: dict, policy: dict) -> list[dict]:
    """资料分析之后、到 `until_node` 为止（含）能自主做的节点，按流程顺序；不看做没做完。"""
    nodes = flow_doc.get("nodes") or []
    ids = node_ids(flow_doc)
    start = ids.index(START_AFTER) + 1 if START_AFTER in ids else 0
    until = policy["scope"].get("until_node")
    out = []
    for node in nodes[start:]:
        if _eligible(node, policy):
            out.append(node)
        if node["id"] == until:
            break
    return out


def node_states(root: Path, flow_doc: dict) -> dict[str, str | None]:
    """各节点的状态，按确认记录与完成判定算（不叠副本里的命令锁：开始时接管的那一轮，锁还留在它的副本里，叠上会把
    做完的节点算成在跑）。"""
    from ..flow import model
    from ..flow import state as node_state

    root = Path(root)
    computed = node_state.compute(root, flow_doc, model.flow_state(root).node_hits(flow_doc, root), persist=False)
    return {node_id: item.get("state") for node_id, item in computed.items()}


def _to_do(state: str | None, policy: dict) -> bool:
    if state in ("confirmed", "skipped"):
        return False
    return state != "stale" or policy["scope"].get("refresh_stale_first", True)


def _step(step_id: str, title: str, command: str) -> dict:
    return {"id": step_id, "label": title, "title": title, "action": title, "command": command, "args": {}}


def _unit(node: dict, again: dict | None) -> dict:
    from ..session_host.exec_records import LEGACY_MODEL

    title = node.get("title") or node["id"]
    if node["id"] == LEGACY_MODEL:
        steps = [_step(*item) for item in LEGACY_MODEL_STEPS]
    else:
        steps = [_step(node["id"], title, _command(node))]
    if again is not None:
        # 接过来的那一步（老建模节点按命令认，认不出时从第一步做）
        step = next((item for item in steps if item["command"] == again.get("command")), steps[0])
        step["resume_from"] = again.get("phase") or ""
    return {"id": node["id"], "name": title, "kind": "node", "steps": steps}


def lay_out(root: Path, flow_doc: dict, policy: dict, redo: dict | None = None,
            held: dict | None = None) -> tuple[list[dict], list[dict]]:
    """(要做的单元, 预览的节点行 [{id, name, current, included}])。

    redo：开始时从别的对话接过来、停在干活中途的节点 {节点: {step, phase}}——产物写了一半也满足完成判定，要照做（接着
    做完）。held：之前的运行留到这一次的挂起问题 {节点: [{run_id, id}]}。"""
    states = node_states(root, flow_doc)
    units, rows = [], []
    for node in candidates(flow_doc, policy):
        state = states.get(node["id"])
        again = (redo or {}).get(node["id"])
        waiting = (held or {}).get(node["id"]) or []
        take = again is not None or bool(waiting) or _to_do(state, policy)
        rows.append({"id": node["id"], "name": node.get("title") or node["id"],
                     "current": STATE_TEXT.get(state or "", "未开始"), "included": take})
        if take:
            unit = _unit(node, again)
            if waiting:
                unit["carried"] = [{"run_id": item["run_id"], "id": item["id"]} for item in waiting]
            units.append(unit)
    return units, rows


def build(manager, project_id: str, policy: dict, redo: dict | None = None) -> tuple[list[dict], list[dict]]:
    root = manager.project_root(project_id)
    return lay_out(root, flow(root), policy, redo, carried(project_id))


# ---------------------------------------------------------------------------
# 单元落到哪个节点（老运行按段的单元要换算）
# ---------------------------------------------------------------------------


def unit_node(run: dict, unit_id: str | None, step: str | None = None) -> str | None:
    """运行里一个单元（台账、挂起问题记的 `slice.id`）落在哪个节点：新运行就是单元本身；老运行按段的单元按那一步认
    （对象 → 提取对象，操作 → 提取操作，规则 → 指标；不知道哪一步时取这一段的第一步）。"""
    from ..session_host.exec_records import LEGACY_STEPS

    unit = next((item for item in run.get("units") or [] if item["id"] == unit_id), None)
    if unit is None or unit.get("kind") != "slice":
        return unit_id
    first = ((unit.get("steps") or [{}])[0]).get("id")
    return LEGACY_STEPS.get(str(step or first or ""))


def nodes_of(unit: dict) -> list[str]:
    """一个单元的确认记录在哪几个节点上（开始前记下、撤回时放回）。"""
    if unit.get("kind") != "slice":
        return [unit["id"]]
    return list(dict.fromkeys(RECORD_STEPS[step["id"]] for step in unit.get("steps") or []
                              if step["id"] in RECORD_STEPS))


def carried(project_id: str) -> dict[str, list[dict]]:
    """之前的运行里留到下一次的挂起问题，按节点：{节点: [{run_id, id, step}]}；撤回了的单元上的不算。"""
    from . import store, views

    out: dict[str, list[dict]] = {}
    for run in store.runs(project_id):
        for item in views.live_suspensions(run):
            node_id = unit_node(run, (item.get("slice") or {}).get("id"), item.get("step"))
            if item.get("state") == "deferred" and node_id:
                out.setdefault(node_id, []).append({"run_id": run["run_id"], "id": item["id"],
                                                    "step": item.get("step")})
    return out
