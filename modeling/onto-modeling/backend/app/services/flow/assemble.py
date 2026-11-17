"""流程组装（详设 6.7 第 1 项；方案 D12）：各包的流程声明 → 一张有向无环图。

组装时检查：锚点存在、没有环、节点编号唯一且带包前缀、同一路径不被两个节点同时声明为输出、
和主流程版本兼容。任何一项不通过就拒绝（装配失败，项目保持原版本）。

可选包（方案 D7 后半）用自己的 flow.yaml 往主流程上挂节点：
- `after` 可以写成列表，取第一个存在的锚点（如代码生成挂在场景模拟之后，没启用场景模拟就挂在校验之后）；
- `display_after` 只管显示顺序（缺省同 after）：节点排在锚点后面，同一锚点的按包顺序排；
- 包可以补命令前置（`commands`）与探针（`probes`），和已有的重名就拒绝；
- 主流程的下一步建议（`next_steps`）里指向没启用的包的命令去掉，只列本项目能用的命令。
节点钩子（详设 6.4，B5c）：包在 flow.yaml 的 `hooks` 里给某个节点之前或之后追加一步检查或补充，不新增节点：
`{id: <包>.<名>, title, node: <宿主节点>, when: before|after, command: /<本包命令>}`；宿主节点要存在、命令要在本次装配里。
按段的节点（详设 6.6，B4d）：`per_slice: true` 的节点带 `steps`（分段里的步骤）；包用 `slice_steps` 往这类节点的
分段里加步骤（`[{node, step}]`，如 F8 之前场景模拟的「场景」），用 `node_titles` 改主线节点的显示名（`{节点: 名称}`，如
F8 之前的「校验与走查」）。这两项和节点钩子（`hooks`）分开声明，各管各的。
core 的 `aliases` 把旧版节点编号映射到现在的节点：没升级的包写的锚点、钩子宿主、命令归属照样认。
范围限定（ADR-0085）：包在 flow.yaml 顶层写 `confine: {scope_file: <路径>}`，范围文件里 status 是 active 时，
基线里有、editable 里没有的本体条目助手不能写也不能删（内核守卫按组装结果执行）。最多一个包声明，
路径要落在这个包某个节点的 produces 里；组装结果记下声明它的包、节点和入口命令，守卫的提示不用认识具体的包。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json

FLOW_API = 1
PLACEMENTS = ("spine", "step", "branch", "gate", "on_demand")
SECTIONS = ("required", "pre_publish", "optional", "on_demand")
KINDS = ("agent", "script", "human")


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


def _hook_problems(pack: str, hook, known: set[str], available: set[str] | None) -> list[str]:
    if not isinstance(hook, dict):
        return [f"{pack} 的节点钩子必须是映射"]
    hook_id = str(hook.get("id") or "")
    problems = []
    if not hook_id.startswith(f"{pack}."):
        problems.append(f"节点钩子 {hook_id or '（空）'} 的编号要以包名 {pack}. 开头")
    if hook.get("node") not in known:
        problems.append(f"节点钩子 {hook_id} 挂的节点 {hook.get('node')} 不存在")
    if hook.get("when", "after") not in ("before", "after"):
        problems.append(f"节点钩子 {hook_id} 的 when 只能是 before 或 after")
    command = _command_name(hook.get("command"))
    if not command:
        problems.append(f"节点钩子 {hook_id} 要有命令")
    elif available is not None and command not in available:
        problems.append(f"节点钩子 {hook_id} 的命令 /{command} 不在本次装配里")
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


def _offered(item: dict, available: set[str]) -> bool:
    """下一步建议里的命令装了（不带命令的建议照留）。"""
    name = _command_name(item.get("command"))
    return not name or name in available


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
    aliases = {str(old): str(new) for old, new in (base.get("aliases") or {}).items() if str(new) in known}
    _apply_aliases(nodes, flow, aliases)
    problems += _apply_slice_steps(contributions, nodes)
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
    # 动作声明了 `pack`（要某个包才能做）而这次装配没有这个包：去掉这个动作
    for node in nodes:
        if node.get("actions"):
            node["actions"] = [action for action in node["actions"]
                               if not (isinstance(action, dict) and action.get("pack") and action["pack"] not in sources)]
    # 动作的条件 `when`（探针名，`!` 取反）：用到的探针要在这次装配的流程里声明
    for node in nodes:
        for action in node.get("actions") or []:
            for name in (action.get("when") or []) if isinstance(action, dict) else []:
                if str(name).lstrip("!") not in flow["probes"]:
                    problems.append(f"节点 {node['id']} 的动作「{action.get('label', '')}」条件用到的探针 {str(name).lstrip('!')} 没有声明")
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
    hooks = []
    for pack, version, declared in contributions:
        for hook in declared.get("hooks") or []:
            if isinstance(hook, dict) and str(hook.get("node") or "") in aliases:
                hook = {**hook, "node": aliases[str(hook["node"])]}  # 还没升级的包挂在 0.2 的节点上
            problems += _hook_problems(pack, hook, known, available_commands)
            if isinstance(hook, dict):
                hooks.append({"id": str(hook.get("id") or ""), "title": str(hook.get("title") or hook.get("id") or ""),
                              "node": str(hook.get("node") or ""), "when": hook.get("when", "after"),
                              "command": str(hook.get("command") or ""), "pack": pack, "version": f"{pack}@{version}"})
    if len({hook.get("id") for hook in hooks}) != len(hooks):
        problems.append("节点钩子编号重复")
    # 命令 → 节点的映射只能指向存在的节点
    for command, spec in (flow.get("commands") or {}).items():
        target = (spec or {}).get("node") if isinstance(spec, dict) else None
        if target and target not in known:
            problems.append(f"命令 {command} 指向的节点 {target} 不存在")
    _activations(contributions, nodes, flow, problems)
    _confine(contributions, nodes, flow, problems)
    if problems:
        raise FlowAssemblyError(problems)
    if available_commands is not None:
        flow["next_steps"] = {
            stage: [item for item in items if _offered(item, available_commands)]
            for stage, items in (flow.get("next_steps") or {}).items()
        }
        flow["available_commands"] = sorted(available_commands)
    flow["nodes"] = nodes
    if hooks:
        flow["hooks"] = hooks
    flow["edges"] = [{"from": source, "to": target} for source, target in edges]
    flow["sources"] = sources
    flow["flow_hash"] = flow_hash(flow)
    return flow


# 范围文件不能放在这些目录下：本体条目归范围管，其余是工具、资料或内核的目录
_CONFINE_FORBIDDEN = ("ontology", "resource", ".claude", ".onto", ".git", ".ontology-state")


def _bad_state_file(text: str) -> bool:
    parts = text.split("/")
    return (not text or text.startswith("/") or "\\" in text or any(ch in text for ch in "*?[")
            or any(part in ("", ".", "..") for part in parts) or parts[0] in _CONFINE_FORBIDDEN)


def _in_produces(nodes: list[dict], pack: str, text: str) -> dict | None:
    return next((node for node in nodes if node.get("pack") == pack and _produces(node, text)), None)


def _produces(node: dict, text: str) -> bool:
    return any(fnmatch.fnmatchcase(text, str(item.get("path") if isinstance(item, dict) else item))
               for item in node.get("produces") or [])


def _activations(contributions: list[tuple[str, str, dict]], nodes: list[dict], flow: dict, problems: list[str]) -> None:
    """包声明的触发（ADR-0091）：校验后写进组装结果的 `activations`（包 → 声明）；没有包声明时去掉。"""
    from ..packs.manifest import ACTIVATION_EVENTS

    flow.pop("activations", None)
    out = {}
    for pack, _, item in contributions:
        spec = item.get("activation")
        if spec in (None, {}):
            continue
        if not isinstance(spec, dict) or set(spec) != {"event", "requires", "session"}:
            problems.append(f"{pack} 的 activation 要写 event、requires、session，不能有别的")
            continue
        if spec["event"] not in ACTIVATION_EVENTS:
            problems.append(f"{pack} 的 activation.event 不认识：{spec['event']}（可用：{'、'.join(ACTIVATION_EVENTS)}）")
        own = item.get("probes") or {}
        requires = spec["requires"] if isinstance(spec["requires"], list) else None
        missing = [name for name in requires or [] if str(name).lstrip("!") not in own]
        if requires is None or missing:
            problems.append(f"{pack} 的 activation.requires 要写本包声明的探针：{'、'.join(map(str, missing)) or '不是列表'}")
        session = str(spec["session"] or "")
        if _bad_state_file(session) or _in_produces(nodes, pack, session) is None:
            problems.append(f"{pack} 的 activation.session 要是本包节点 produces 里的具体文件：{session or '（空）'}")
        out[pack] = {"event": spec["event"], "requires": requires or [], "session": session}
    for node in nodes:
        if node.get("visible_if") == "activated" and node.get("pack") not in out:
            problems.append(f"节点 {node['id']} 写了 visible_if: activated，它的包没有声明 activation")
        if node.get("preempt") and node.get("visible_if") != "activated":
            problems.append(f"节点 {node['id']} 的 preempt 只能用在 visible_if: activated 的节点上")
    if out:
        flow["activations"] = out


def _confine(contributions: list[tuple[str, str, dict]], nodes: list[dict], flow: dict, problems: list[str]) -> None:
    """包声明的范围限定（ADR-0085）：校验后写进组装结果的 `confine`；没有包声明时去掉（core 不带这一项）。"""
    flow.pop("confine", None)
    declared = [(pack, item.get("confine")) for pack, _, item in contributions if item.get("confine") not in (None, {})]
    if not declared:
        return
    if len(declared) > 1:
        problems.append("只能有一个包声明 confine：" + "、".join(pack for pack, _ in declared))
        return
    pack, spec = declared[0]
    text = str(spec.get("scope_file") or "") if isinstance(spec, dict) else ""
    if not isinstance(spec, dict) or "scope_file" not in spec or set(spec) - {"scope_file", "lock_on_activation"}:
        problems.append(f"{pack} 的 confine 只能写 scope_file、lock_on_activation")
        return
    lock = spec.get("lock_on_activation", False)
    if not isinstance(lock, bool) or (lock and pack not in (flow.get("activations") or {})):
        problems.append(f"{pack} 的 confine.lock_on_activation 要是布尔值，为 true 时本包要声明 activation")
        return
    if _bad_state_file(text):
        problems.append(f"{pack} 的 confine.scope_file 要是工作区里的具体文件，不能在 ontology/ 或受保护目录下：{text or '（空）'}")
        return
    owner = _in_produces(nodes, pack, text)
    if owner is None:
        problems.append(f"{pack} 的 confine.scope_file {text} 不在本包任何节点的 produces 里")
        return
    flow["confine"] = {"scope_file": text, "pack": pack, "node": owner["id"],
                       "entry": str((owner.get("entry") or {}).get("command") or "").strip(),
                       **({"lock_on_activation": True} if lock else {})}


def _apply_aliases(nodes: list[dict], flow: dict, aliases: dict[str, str]) -> None:
    """旧节点编号 → 现在的节点：锚点、显示锚点、关卡挡住的节点、命令归属都换过去。"""
    if not aliases:
        return

    def mapped(value):
        if isinstance(value, list):
            return list(dict.fromkeys(aliases.get(str(item), str(item)) for item in value))
        return aliases.get(str(value), value) if value else value

    for node in nodes:
        for key in ("after", "before", "display_after", "blocks"):
            if node.get(key):
                node[key] = mapped(node[key])
        if node.get("before") and node.get("before") == node.get("after"):
            # 原来插在对象提取与操作提取之间的（两者都并进了建模）：挂在建模之后
            node.pop("before")
    for spec in (flow.get("commands") or {}).values():
        if isinstance(spec, dict) and spec.get("node"):
            spec["node"] = aliases.get(spec["node"], spec["node"])


STEP_KEYS = ("id", "label")


def _apply_slice_steps(contributions: list[tuple[str, str, dict]], nodes: list[dict]) -> list[str]:
    """包往按段的节点里加步骤（`slice_steps`，step.after 指定放在哪一步后面），改节点的显示名（`node_titles`）。"""
    problems: list[str] = []
    by_id = {node["id"]: node for node in nodes}
    for node in nodes:
        if node.get("per_slice"):
            steps = node.get("steps") or []
            if not steps or not all(isinstance(step, dict) and all(step.get(key) for key in STEP_KEYS)
                                    for step in steps):
                problems.append(f"按段的节点 {node['id']} 要声明 steps（每一步有 id、label）")
            node["steps"] = [{**step, "pack": node["pack"]} for step in steps if isinstance(step, dict)]
    for pack, _version, declared in contributions[1:]:
        titles = declared.get("node_titles") or {}
        if not isinstance(titles, dict):
            problems.append(f"{pack} 的 node_titles 要是「节点: 名称」的映射")
            titles = {}
        for node_id, title in titles.items():
            if node_id not in by_id or not str(title or "").strip():
                problems.append(f"{pack} 改名的节点 {node_id} 不存在或名称为空")
                continue
            by_id[node_id]["title"] = str(title).strip()
        for item in declared.get("slice_steps") or []:
            target = by_id.get(str((item or {}).get("node") or "")) if isinstance(item, dict) else None
            if target is None:
                problems.append(f"{pack} 加步骤的节点 {(item or {}).get('node') if isinstance(item, dict) else item} 不存在")
                continue
            step = item.get("step")
            if not target.get("per_slice") or not isinstance(step, dict) or not all(step.get(k) for k in STEP_KEYS):
                problems.append(f"{pack} 只能往按段的节点里加步骤（要有 id、label）")
                continue
            steps = target["steps"]
            if any(existing["id"] == step["id"] for existing in steps):
                problems.append(f"{pack} 加的步骤 {step['id']} 在 {target['id']} 里已经有了")
                continue
            position = next((index + 1 for index, existing in enumerate(steps) if existing["id"] == step.get("after")),
                            len(steps))
            steps.insert(position, {**step, "pack": pack})
    return problems


def flow_hash(flow: dict) -> str:
    body = {key: value for key, value in flow.items() if key != "flow_hash"}
    return hashlib.sha256(json.dumps(body, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def render(flow: dict) -> str:
    """装配目录里的 flow.yaml：JSON 写法（合法 YAML），工作区脚本用标准库 json 就能读。"""
    return json.dumps(flow, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
