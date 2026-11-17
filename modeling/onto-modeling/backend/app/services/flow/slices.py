"""批次 1B 删除：自主构建（`services/autobuild`）改成按节点出单元之前，它还在用的分段接口。

建模拆成提取对象（core.object）、提取操作（core.operation）两个节点以后，流程里没有按段的节点了，分段的状态、
写入范围、建议都已去掉。自主构建仍按「一段『全部』× 步骤（对象、操作）」排单元、记执行、撤回：这里把两个节点包成
那样一个虚拟的建模节点，每一步的状态与确认记录都落在它自己的节点上（`state.py`）。项目锁定旧 core 时两步都落在
core.model 上。
"""

from __future__ import annotations

from pathlib import Path

from . import state as node_state

FULL_SLICE = {"id": "all", "name": "全部", "core_flow": ""}
DONE = ("confirmed", "auto")
# (步骤, 列名, 业务名, 动作名, 命令)：命令归哪个节点按流程的 commands 表
_STEPS = (("object", "对象", "对象提取", "提取对象", "extract-object"),
          ("operation", "操作", "操作提取", "提取操作", "extract-operation"))


def per_slice_node(flow: dict) -> dict | None:
    """虚拟的建模节点 {id, title, steps: [{id, label, title, action, command, node}]}：编号取最后一步的节点，自主构建
    从它后面接主线上的几步。两条命令有一条不归流程里的节点时 None。"""
    ids = {node.get("id") for node in flow.get("nodes") or []}
    commands = flow.get("commands") or {}
    steps = []
    for step_id, label, title, action, command in _STEPS:
        node_id = (commands.get(command) or {}).get("node") if isinstance(commands.get(command), dict) else None
        if node_id not in ids:
            return None
        steps.append({"id": step_id, "label": label, "title": title, "action": action,
                      "command": f"/ontology-{command}", "node": node_id})
    return {"id": steps[-1]["node"], "title": "建模", "steps": steps}


def stage_defs(node: dict) -> list[dict]:
    return [{"id": step["id"], "label": step["label"]} for step in node.get("steps") or []]


def stages(root: Path, flow: dict) -> dict[str, str]:
    """各步的状态，取它的节点的：自主构建合入、待复核的显示 auto，其余照节点状态。没有虚拟节点时为空。"""
    from . import model

    node = per_slice_node(flow)
    if node is None:
        return {}
    hits = model.flow_state(Path(root)).node_hits(flow, Path(root))
    states = node_state.compute(Path(root), flow, hits, persist=False)
    out = {}
    for step in node["steps"]:
        item = states.get(step["node"]) or {}
        out[step["id"]] = "auto" if item.get("display_state") == "auto" else item.get("state") or "not_ready"
    return out


def group_of(cells: dict[str, str]) -> str:
    """一段的分组（同原来按段的汇总）：在跑 now；待确认、过期 wait；都做完 done；做了一部分 now；都没开始 todo。"""
    values = list(cells.values())
    if any(value == "running" for value in values):
        return "now"
    if any(value in ("pending_confirm", "stale") for value in values):
        return "wait"
    if values and all(value in DONE for value in values):
        return "done"
    if any(value not in ("ready", "not_ready", "skipped", "blocked") for value in values):
        return "now"
    return "todo"


def _step_nodes(root: Path) -> dict[str, str]:
    from . import model

    node = per_slice_node(model.load_for(Path(root))) or {}
    return {step["id"]: step["node"] for step in node.get("steps") or []}


def records_of(root: Path, slice_id: str) -> dict:
    """这一段各步（按步骤编号）的确认记录：自主构建开始做这一段前记下，撤回时放回去。只有一段「全部」，slice_id 不再区分。"""
    mapping = _step_nodes(root)
    found = node_state.records_of(root, list(dict.fromkeys(mapping.values())))
    return {step_id: found[node_id] for step_id, node_id in mapping.items() if node_id in found}


def put_back(root: Path, slice_id: str, step_ids: list[str], before: dict) -> None:
    """这几步的确认记录放回成 before 里的；不认识的步骤（写入即查 check）不管。"""
    mapping = _step_nodes(root)
    wanted = [step_id for step_id in step_ids if step_id in mapping]
    node_state.put_back(root, [mapping[step_id] for step_id in wanted],
                        {mapping[step_id]: before[step_id] for step_id in wanted if step_id in before})


def forget(root: Path, slice_id: str, step_ids: list[str]) -> None:
    put_back(root, slice_id, step_ids, {})
