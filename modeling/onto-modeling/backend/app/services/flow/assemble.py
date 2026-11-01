"""流程组装（详设 6.7 第 1 项；方案 D12）：各包的流程声明 → 一张有向无环图。

组装时检查：锚点存在、没有环、节点编号唯一且带包前缀、同一路径不被两个节点同时声明为输出、
和主流程版本兼容。任何一项不通过就拒绝（装配失败，项目保持原版本）。

可选包（方案 D7 后半）用自己的 flow.yaml 往主流程上挂节点：
- `after` 可以写成列表，取第一个存在的锚点（如代码生成挂在场景验证之后，没启用场景走查就挂在质量检查之后）；
- `display_after` 只管显示顺序（缺省同 after）：节点排在锚点后面，同一锚点的按包顺序排；
- 包可以补命令前置（`commands`）与探针（`probes`），和已有的重名就拒绝；
- 主流程的下一步建议（`next_steps`）里指向没启用的包的命令去掉，只列本项目能用的命令。
节点包的四种挂法与节点钩子（详设 6.4）在 P3。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json

FLOW_API = 1
PLACEMENTS = ("spine", "step", "branch", "gate", "on_demand")
SECTIONS = ("required", "pre_publish", "optional", "on_demand")
KINDS = ("agent", "script", "human", "external")


class FlowAssemblyError(ValueError):
    def __init__(self, problems: list[str]):
        super().__init__("；".join(problems))
        self.problems = problems


def _node_problems(pack: str, node: dict) -> list[str]:
    problems = []
    node_id = str(node.get("id") or "")
    if not node_id.startswith(f"{pack}."):
        problems.append(f"节点 {node_id or '（空）'} 的编号要以包名 {pack}. 开头")
    if node.get("placement", "spine") not in PLACEMENTS:
        problems.append(f"节点 {node_id} 的挂法 {node.get('placement')} 不认识")
    if node.get("section", "required") not in SECTIONS:
        problems.append(f"节点 {node_id} 的分组 {node.get('section')} 不认识")
    if node.get("kind", "agent") not in KINDS:
        problems.append(f"节点 {node_id} 的类型 {node.get('kind')} 不认识")
    if not node.get("title"):
        problems.append(f"节点 {node_id} 缺少 title")
    return problems


def _cycle(nodes: list[str], edges: list[tuple[str, str]]) -> list[str] | None:
    graph: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in edges:
        graph.setdefault(source, []).append(target)
    state: dict[str, int] = {}
    stack: list[str] = []

    def visit(node: str) -> list[str] | None:
        state[node] = 1
        stack.append(node)
        for nxt in graph.get(node, []):
            if state.get(nxt) == 1:
                return stack[stack.index(nxt):] + [nxt]
            if state.get(nxt) is None:
                found = visit(nxt)
                if found:
                    return found
        stack.pop()
        state[node] = 2
        return None

    for node in nodes:
        if state.get(node) is None:
            found = visit(node)
            if found:
                return found
    return None


def _overlaps(a: str, b: str) -> bool:
    return a == b or fnmatch.fnmatch(a, b) or fnmatch.fnmatch(b, a)


def _anchors(value) -> list[str]:
    if not value:
        return []
    return [str(item) for item in value] if isinstance(value, list) else [str(value)]


def _ordered(nodes: list[dict], problems: list[str]) -> list[dict]:
    """core 节点保持原顺序；包的节点按 display_after（缺省 after）插到锚点后面。"""
    placed = [node for node in nodes if node["pack"] == "core"]
    pending = [node for node in nodes if node["pack"] != "core"]
    while pending:
        progressed = False
        for node in list(pending):
            ids = [item["id"] for item in placed]
            wanted = _anchors(node.get("display_after")) or _anchors(node.get("after")) or _anchors(node.get("before"))
            anchor = next((item for item in wanted if item in ids), None)
            if anchor is None and any(item in {other["id"] for other in pending} for item in wanted):
                continue  # 锚点还没放上去，下一轮再放
            if anchor is None:
                placed.append(node)
            else:
                position = ids.index(anchor) + 1
                # 已经挂在同一锚点后面的节点排在前面（按包顺序）
                while position < len(placed) and placed[position].get("_display_anchor") == anchor:
                    position += 1
                node["_display_anchor"] = anchor
                placed.insert(position, node)
            pending.remove(node)
            progressed = True
        if not progressed:
            problems.append("节点的显示锚点互相依赖：" + "、".join(node["id"] for node in pending))
            placed += pending
            break
    for node in placed:
        node.pop("_display_anchor", None)
    return placed


def _command_name(text: str) -> str:
    return str(text or "").split()[0].lstrip("/") if str(text or "").startswith("/") else ""


def assemble(contributions: list[tuple[str, str, dict]], available_commands: set[str] | None = None) -> dict:
    """contributions：[(包名, 版本, 流程声明)]，core 必须在第一个并带主流程。

    available_commands：本次装配里所有包提供的命令名（不带 /）；给出时下一步建议只留这些命令。
    返回组装后的流程（含 edges 与 flow_hash）；不通过抛 FlowAssemblyError。
    """
    if not contributions or contributions[0][0] != "core":
        raise FlowAssemblyError(["缺少 core 包的主流程定义"])
    problems: list[str] = []
    base_pack, base_version, base = contributions[0]
    if int(base.get("flow_api") or 0) != FLOW_API:
        problems.append(f"主流程格式版本 {base.get('flow_api')} 不是 {FLOW_API}")
    flow = {key: value for key, value in base.items() if key != "nodes"}
    flow["flow_api"] = FLOW_API
    nodes: list[dict] = []
    sources: dict[str, str] = {}
    flow["commands"] = dict(flow.get("commands") or {})
    flow["probes"] = dict(flow.get("probes") or {})
    for pack, version, declared in contributions:
        sources[pack] = version
        if pack != "core":
            requires = str(declared.get("requires_flow") or "")
            if requires and not requires.startswith("core"):
                problems.append(f"{pack} 的 requires_flow 只能约束 core")
            for key in ("commands", "probes"):
                for name, spec in (declared.get(key) or {}).items():
                    if name in flow[key]:
                        problems.append(f"{pack} 声明的{'命令前置' if key == 'commands' else '探针'} {name} 已经存在")
                    else:
                        flow[key][name] = spec
        for node in declared.get("nodes") or []:
            if not isinstance(node, dict):
                problems.append(f"{pack} 的流程节点必须是映射")
                continue
            problems += _node_problems(pack, node)
            nodes.append({**node, "pack": pack, "version": f"{pack}@{version}"})
    ids = [str(node.get("id")) for node in nodes]
    duplicates = sorted({node_id for node_id in ids if ids.count(node_id) > 1})
    problems += [f"节点编号重复：{node_id}" for node_id in duplicates]
    known = set(ids)
    edges: list[tuple[str, str]] = []
    for node in nodes:
        for key in ("after", "before"):
            candidates = _anchors(node.get(key))
            if not candidates:
                continue
            anchor = next((item for item in candidates if item in known), None)
            if anchor is None:
                problems.append(f"节点 {node['id']} 的锚点 {'、'.join(candidates)} 不存在")
                continue
            node[key] = anchor
            edges.append((anchor, node["id"]) if key == "after" else (node["id"], anchor))
    nodes = _ordered(nodes, problems)
    ids = [node["id"] for node in nodes]
    cycle = _cycle(ids, edges)
    if cycle:
        problems.append("流程有环：" + " → ".join(cycle))
    owners: list[tuple[str, str]] = []
    for node in nodes:
        for item in node.get("produces") or []:
            pattern = str(item.get("path") if isinstance(item, dict) else item)
            for other, other_pattern in owners:
                if other != node["id"] and _overlaps(pattern, other_pattern):
                    problems.append(f"{node['id']} 与 {other} 同时声明输出 {pattern}")
            owners.append((node["id"], pattern))
    # 命令 → 节点的映射只能指向存在的节点
    for command, spec in (flow.get("commands") or {}).items():
        target = (spec or {}).get("node") if isinstance(spec, dict) else None
        if target and target not in known:
            problems.append(f"命令 {command} 指向的节点 {target} 不存在")
    if problems:
        raise FlowAssemblyError(problems)
    if available_commands is not None:
        flow["next_steps"] = {
            stage: [item for item in items if not _command_name(item.get("command"))
                    or _command_name(item.get("command")) in available_commands]
            for stage, items in (flow.get("next_steps") or {}).items()
        }
        flow["available_commands"] = sorted(available_commands)
    flow["nodes"] = nodes
    flow["edges"] = [{"from": source, "to": target} for source, target in edges]
    flow["sources"] = sources
    flow["flow_hash"] = flow_hash(flow)
    return flow


def flow_hash(flow: dict) -> str:
    body = {key: value for key, value in flow.items() if key != "flow_hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def render(flow: dict) -> str:
    """装配目录里的 flow.yaml：JSON 写法（合法 YAML），工作区脚本用标准库 json 就能读。"""
    return json.dumps(flow, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
