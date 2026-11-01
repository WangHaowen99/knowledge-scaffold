"""流程组装（详设 6.7 第 1 项；方案 D12）：各包的流程声明 → 一张有向无环图。

组装时检查：锚点存在、没有环、节点编号唯一且带包前缀、同一路径不被两个节点同时声明为输出、
和主流程版本兼容。任何一项不通过就拒绝（装配失败，项目保持原版本）。P1 只有 core 包声明流程；
其他包往主流程上挂节点（四种挂法）是 P3 的事，这里先把检查做全。
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


def assemble(contributions: list[tuple[str, str, dict]]) -> dict:
    """contributions：[(包名, 版本, 流程声明)]，core 必须在第一个并带主流程。

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
    for pack, version, declared in contributions:
        sources[pack] = version
        if pack != "core":
            requires = str(declared.get("requires_flow") or "")
            if requires and not requires.startswith("core"):
                problems.append(f"{pack} 的 requires_flow 只能约束 core")
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
            anchor = node.get(key)
            if not anchor:
                continue
            if anchor not in known:
                problems.append(f"节点 {node['id']} 的锚点 {anchor} 不存在")
                continue
            edges.append((anchor, node["id"]) if key == "after" else (node["id"], anchor))
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
