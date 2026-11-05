"""一次自主构建做哪些事（详设 8.5「范围」）：按蓝图的分段顺序，每段要跑的步骤；分段之后主线上的几步（校验、场景模拟）
各算一个单元，做到策略里的 `until_node` 为止。发布是硬性不做的，不在里面。

单元：`{id, name, kind: "slice"|"node", steps: [{id, label, title, command, args}], carried?}`；分段的步骤来自建模
节点的 `steps`（对象、操作、规则，装了场景模拟再加「场景」），已确认的不再做，过期的按 `refresh_stale_first` 决定要不要重做。
之前的运行里「留到下一次」的挂起问题（`carried`）：那一段在范围里时补做问题所在的那一步，即使已经做完（审查 I）。
"""

from __future__ import annotations

from pathlib import Path

DONE_STATES = ("confirmed", "auto")
GROUP_LABELS = {"done": "已确认", "todo": "未开始", "now": "做了一部分"}
SKIP_TAIL = ("core.publish", "core.update")


def flow(root: Path) -> dict:
    from ..flow import model

    return model.load_for(Path(root))


def per_slice_node(flow_doc: dict) -> dict | None:
    from ..flow import slices

    return slices.per_slice_node(flow_doc)


def slice_rows(manager, project_id: str) -> list[dict]:
    """蓝图里按建议顺序、没排除的段（`blueprint.run_slices`）。"""
    from ..flow import blueprint

    return blueprint.run_slices(manager, project_id)


def node_ids(flow_doc: dict) -> list[str]:
    return [node["id"] for node in flow_doc.get("nodes") or []]


def current_label(row: dict) -> str:
    if row.get("group") == "wait":
        return "有过期" if "stale" in (row.get("stages") or {}).values() else "待确认"
    return GROUP_LABELS.get(row.get("group") or "todo", "未开始")


def _step(row: dict, step: dict) -> dict:
    args = {} if row["id"] == "all" else {"slice": row["id"]}
    return {"id": step["id"], "label": step.get("label") or step["id"],
            "title": step.get("title") or step.get("label") or step["id"],
            "action": step.get("action") or step.get("label") or step["id"],
            "command": step["command"], "args": args}


def _slice_steps(row: dict, node: dict, policy: dict, carried: list[dict] = ()) -> list[dict]:
    scope = policy["scope"]
    runnable = [step for step in node.get("steps") or [] if not step.get("derived") and step.get("command")]
    # 留到这一次的问题所在的那一步（不知道哪一步时取第一步）照做
    wanted = {item.get("step") or (runnable[0]["id"] if runnable else None) for item in carried}
    out = []
    for step in runnable:
        state = (row.get("stages") or {}).get(step["id"])
        if step["id"] not in wanted:
            if state in DONE_STATES:
                continue
            if state == "stale" and not scope.get("refresh_stale_first", True):
                continue
            if state == "skipped" and not (step.get("optional") and scope.get("include_optional")):
                continue
            # 可选的一步（规则补抽）和手动建议一样：没过期、没要「含可选」时不排（F8：规则随操作提取一起抽）
            if step.get("optional") and state != "stale" and not scope.get("include_optional"):
                continue
            if step["id"] == "scenario" and not scope.get("generate_scenarios", True):
                continue
        out.append(_step(row, step))
    return out


def carried(project_id: str) -> dict[str, list[dict]]:
    """之前的运行里留到下一次的挂起问题，按段：{段: [{run_id, id, step}]}；撤回了的段上的不算。"""
    from . import store, views

    out: dict[str, list[dict]] = {}
    for run in store.runs(project_id):
        for item in views.live_suspensions(run):
            slice_id = (item.get("slice") or {}).get("id")
            if item.get("state") == "deferred" and slice_id:
                out.setdefault(slice_id, []).append({"run_id": run["run_id"], "id": item["id"],
                                                     "step": item.get("step")})
    return out


def included(row: dict, policy: dict) -> bool:
    scope = policy["scope"]
    if scope.get("mode") == "selected":
        return row["id"] in (scope.get("slice_ids") or [])
    return row.get("group") != "done" or (scope.get("refresh_stale_first", True)
                                          and "stale" in (row.get("stages") or {}).values())


def _tail(root: Path, flow_doc: dict, policy: dict) -> list[dict]:
    """分段之后主线上的几步：按流程顺序，到 `until_node` 为止（含）；已确认的、可选的（不含可选时）、人工与面板节点不做。"""
    from ..flow import view

    node = per_slice_node(flow_doc)
    until = policy["scope"].get("until_node")
    if node is None or until == node["id"]:
        return []
    states = {item["id"]: item for item in view.build(Path(root))["nodes"]}
    nodes = flow_doc.get("nodes") or []
    start = next((index for index, item in enumerate(nodes) if item["id"] == node["id"]), len(nodes))
    out = []
    for item in nodes[start + 1:]:
        state = (states.get(item["id"]) or {}).get("state")
        command = ((item.get("entry") or {}).get("command") or "").strip()
        eligible = not (item["id"] in SKIP_TAIL or item.get("placement", "spine") != "spine"
                        or item.get("kind") == "human" or item.get("panel") or not command
                        or (item.get("optional") and not policy["scope"].get("include_optional"))
                        or state in ("confirmed", "skipped"))
        if eligible:
            title = item.get("title") or item["id"]
            out.append({"id": item["id"], "name": title, "kind": "node",
                        "steps": [{"id": item["id"], "label": title, "title": title, "action": title,
                                   "command": command, "args": {}}]})
        if item["id"] == until:
            break
    return out


def build(manager, project_id: str, policy: dict) -> tuple[list[dict], list[dict]]:
    """(要做的单元, 预览用的全部分段 [{id, name, current, included}])。"""
    root = manager.project_root(project_id)
    flow_doc = flow(root)
    node = per_slice_node(flow_doc) or {}
    held = carried(project_id)
    units, preview = [], []
    for row in slice_rows(manager, project_id):
        take = included(row, policy) or (row["id"] in held and policy["scope"].get("mode") != "selected")
        steps = _slice_steps(row, node, policy, held.get(row["id"]) or []) if take else []
        preview.append({"id": row["id"], "name": row["name"], "current": current_label(row), "included": bool(steps)})
        if steps:
            unit = {"id": row["id"], "name": row["name"], "kind": "slice", "steps": steps,
                    "before": dict(row.get("stages") or {})}
            # 策略里不做的步骤（不生成场景、不刷新过期、不含可选）：运行详情里不能算「自主完成」
            chosen = {step["id"] for step in steps}
            left_out = [step["id"] for step in node.get("steps") or []
                        if not step.get("derived") and step.get("command") and step["id"] not in chosen
                        and (row.get("stages") or {}).get(step["id"]) not in (*DONE_STATES, "skipped")]
            if left_out:
                unit["left_out"] = left_out
            if row["id"] in held:
                unit["carried"] = [{"run_id": item["run_id"], "id": item["id"]} for item in held[row["id"]]]
            units.append(unit)
    if units or policy["scope"].get("mode") != "selected":
        units += _tail(root, flow_doc, policy)
    return units, preview


def tail_titles(units: list[dict]) -> list[str]:
    return [unit["name"] for unit in units if unit["kind"] == "node"]
