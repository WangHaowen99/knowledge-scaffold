"""`GET /api/v1/projects/{id}/flow` 的响应（契约 FL-01、FL-02；详设 6.7 第 6 项）。

P1 只做流程定义与读取：节点、连线、分组都来自装配好的 flow.yaml；节点状态按完成判定
（flow_state 探针）与进行中的命令锁粗算——已确认 / 进行中 / 可开始 / 未就绪四种。按执行记录、
变更集、产物指纹算 8 种状态与按切片的进度是 P2（方案 D13），这里不假装算出来。
下一步建议只按主线给（第一个没完成的必经步骤），打断、过期、报告错误这几条优先级在 P2 补齐。
"""

from __future__ import annotations

from pathlib import Path

from . import model

SECTION_TITLES = {
    "required": "必经步骤",
    "pre_publish": "发布前检查",
    "optional": "可选步骤",
    "on_demand": "有需要时",
}
_DISPLAY = {"confirmed": "confirmed", "running": "running", "ready": "ready", "not_ready": "not_ready"}


def _running_nodes(flow: dict, root: Path, state) -> set[str]:
    """`.ontology-state/lock-<命令>.json` 在的命令所属的节点（state_helper 的命令锁）。"""
    directory = Path(root) / ".ontology-state"
    running = set()
    try:
        locks = list(directory.glob("lock-*.json"))
    except OSError:
        return running
    for path in locks:
        node = state.command_node(flow, path.name[len("lock-"):-len(".json")])
        if node:
            running.add(node)
    return running


def _node_state(node: dict, hits: dict, running: set[str], by_id: dict) -> str:
    if node["id"] in running:
        return "running"
    if hits.get(node["id"]):
        return "confirmed"
    anchor = node.get("after")
    while anchor and by_id.get(anchor, {}).get("optional") and not hits.get(anchor):
        anchor = by_id[anchor].get("after")
    return "ready" if not anchor or hits.get(anchor) else "not_ready"


def _suggestion(sid: str, label: str, command: str, *, primary: bool, reason: str, priority: int) -> dict:
    return {
        "id": sid, "label": label, "primary": primary, "disabled": False, "reason": reason,
        "priority": priority, "action": {"type": "send", "text": command, "command": command.split()[0]},
    }


def suggestions(flow: dict, nodes: list[dict], stage: str) -> dict:
    """主线下一步（详设 6.10 第 4、5 条）；1 个主推，最多 2 个次选。"""
    primary = None
    secondary: list[dict] = []
    declared = {node["id"]: node for node in flow.get("nodes") or []}
    pending = [node for node in nodes
               if node["section"] == "required" and node["state"] != "confirmed"]
    if stage == "校验待审阅":
        primary = _suggestion("core.validate", "继续校验", "/ontology-validate", primary=True,
                              reason="报告里还有待处理的问题", priority=3)
    elif pending:
        current = pending[0]
        actions = [action for action in declared[current["id"]].get("actions") or [] if action.get("command")]
        if stage == "资料分析待核验":
            # 报告已在、验收没过：核验排第一（原前端 researchGate 的口径）
            actions = sorted(actions, key=lambda action: not action.get("verify"))
        for index, action in enumerate(actions[:3]):
            item = _suggestion(f"{current['id']}#{index}", action["label"], action["command"],
                               primary=index == 0, reason="主线下一步", priority=4)
            if index == 0:
                primary = item
            else:
                secondary.append(item)
    else:
        primary = _suggestion("overview", "整体视图", "/ontology-overview", primary=True,
                              reason="必经步骤都已完成", priority=5)
        # 扫描脚本候选属于代码生成与求解包：没启用就不推荐（装配时记下了本项目能用的命令）
        available = flow.get("available_commands")
        if available is None or "ontology-codegen-scan" in available:
            secondary.append(_suggestion("codegen-scan", "扫描脚本候选", "/ontology-codegen-scan",
                                         primary=False, reason="可选步骤", priority=5))
    return {"mode": "normal", "primary": primary, "secondary": secondary[:2], "all_commands": True}


def build(root: Path) -> dict:
    flow = model.load_for(root)
    state = model.flow_state(root)
    probes = state.evaluate(flow, root)
    hits = state.node_hits(flow, root, probes)
    stage = state.legacy_stage(flow, root, probes)
    running = _running_nodes(flow, root, state)
    by_id = {node["id"]: node for node in flow.get("nodes") or []}
    nodes = []
    for node in flow.get("nodes") or []:
        node_state = _node_state(node, hits, running, by_id)
        nodes.append({
            "id": node["id"],
            "title": node["title"],
            "kind": node.get("kind", "agent"),
            "placement": node.get("placement", "spine"),
            "section": node.get("section", "required"),
            "state": node_state,
            "flags": {"needs_review": False, "disabled": False, "backfill": False},
            "display_state": _DISPLAY[node_state],
            "progress": None,
            "version": node.get("version") or "",
            "per_slice": bool(node.get("per_slice")),
            "note": node.get("note"),
            "blocks": list(node.get("blocks") or []),
            "actions": [
                {"kind": "start", "label": action["label"], "target": action["command"],
                 **({"verify": True} if action.get("verify") else {})}
                for action in node.get("actions") or [] if action.get("command")
            ],
            "plugin": None if node.get("pack", "core") == "core" else {"name": node["pack"], "label": node["pack"]},
            "optional": bool(node.get("optional")),
            "panel": bool(node.get("panel")),
            "counts_in_progress": bool(node.get("progress", True)),
        })
    sections = [
        {"id": key, "title": title, "nodes": [node["id"] for node in nodes if node["section"] == key]}
        for key, title in SECTION_TITLES.items()
    ]
    commands = {
        name: spec.get("node") for name, spec in (flow.get("commands") or {}).items() if spec.get("node")
    }
    progress_nodes = [node for node in nodes if node["counts_in_progress"]]
    return {
        "flow_hash": flow.get("flow_hash") or "",
        "nodes": nodes,
        "edges": list(flow.get("edges") or []),
        "sections": sections,
        "slice_stages": [],
        "slices": [],
        "suggestions": suggestions(flow, nodes, stage),
        "notices": [],
        "stage": stage,
        "progress": {
            "done": sum(1 for node in progress_nodes if hits.get(node["id"])),
            "total": len(progress_nodes),
        },
        "commands": commands,
    }
