"""节点状态上插件相关的部分（详设 6.9 第 4～6 步、4.10.3 熔断；契约 7.8 FlowNode.flags、O-29）。

节点的 8 种状态由 `state.compute` 按执行记录、变更集、指纹算；这里在它前后补上与包的启停有关的三件事，
不改那套算法：

- **熔断**：运维把某个包的某个版本熔断（O-29）后，它的节点暂时跳过，它挡住的关卡解除——算状态前把这些节点的
  `blocks` 拿掉，算完标成已跳过；
- **可补做**：项目已经越过了还没做的节点（插件节点后面的主线节点已经完成，或主线节点的产出已被后面完成的节点
  用上，比如导入知识包后的资料分析），标 `backfill`，不计入进度的分母，下一步建议不推它；
- **已停用**：锁文件里停用了的包（enabled=false），它的节点不在组装后的流程里、挡不住任何节点；产物保留，
  节点以「已停用」列出（不进进度、没有动作）。
"""

from __future__ import annotations

import copy
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

MAINLINE = ("spine", "step", "gate")


def _pack_version(node: dict) -> tuple[str, str]:
    pack, _, version = str(node.get("version") or "").partition("@")
    return pack or str(node.get("pack") or ""), version


def broken_nodes(flow: dict) -> set[str]:
    """所属包版本已被运维熔断的节点（半开试探时照常跑）。"""
    from ...governance import ops_state

    out = set()
    for node in flow.get("nodes") or []:
        pack, version = _pack_version(node)
        plugin = bool(pack) and pack != "core"
        if plugin and version and ops_state.breaker_state(pack, version) == "open":
            out.add(node["id"])
    return out


def released(flow: dict, broken: set[str]) -> dict:
    """熔断的节点不再挡别的节点：返回拿掉了它们 blocks 的流程副本（没有熔断时原样返回）。"""
    if not broken:
        return flow
    flow = copy.copy(flow)
    flow["nodes"] = [{**node, "blocks": []} if node["id"] in broken else node for node in flow.get("nodes") or []]
    return flow


def mark_broken(states: dict, broken: set[str]) -> None:
    from . import state as node_state

    for node_id in broken:
        item = states.get(node_id)
        if item is None or item["state"] in ("running", "pending_confirm"):
            continue
        item["state"] = "skipped"
        item["display_state"] = node_state.display("skipped", item["flags"])
        item["broken"] = True


def _kinds(node: dict) -> set[str]:
    """节点产出落在哪些产物类别里（produces 的目录对 `state.KIND_DIRS`）。"""
    from .state import KIND_DIRS

    patterns = [str(item.get("path") if isinstance(item, dict) else item) for item in node.get("produces") or []]
    kinds = set()
    for kind, dirs in KIND_DIRS.items():
        if any(pattern.startswith(f"{directory}/") for directory in dirs for pattern in patterns):
            kinds.add(kind)
    return kinds


def backfill_ids(flow: dict, hits: dict) -> set[str]:
    """节点自己还没完成、项目已经越过了它（可补做）。两种越过：

    - 串入、关卡这类插件节点：后面的主线节点已经在主线上完成，越过了插入位置；
    - 主线位置（spine）的节点：后面已完成的节点用的正是它的产出（`stale_on` 里有它产出的类别）。导入知识包后建模已经
      完成，资料分析就是这样被越过的（ADR-0084）。后面完成的节点不用它的产出时不算（场景模拟做了、校验没做），没做就是没做。"""
    order = [node for node in flow.get("nodes") or []
             if node.get("placement", "spine") in MAINLINE and not node.get("optional")]
    out = set()
    for index, node in enumerate(order):
        later = [item for item in order[index + 1:] if hits.get(item["id"])]
        if hits.get(node["id"]) or not later:
            continue
        if node.get("placement", "spine") == "spine":
            kinds = _kinds(node)
            if any(kinds & set(item.get("stale_on") or []) for item in later):
                out.add(node["id"])
        elif node.get("pack", "core") != "core":
            out.add(node["id"])
    return out


def first_open(flow: dict, hits: dict) -> dict | None:
    """主线上第一个要做的必经节点：没完成、也没被越过（导入预览的起点，与下一步建议第 9 条同一口径）。"""
    passed = backfill_ids(flow, hits)

    def open_required(node: dict) -> bool:
        if node.get("section", "required") != "required" or node.get("optional"):
            return False
        return not hits.get(node["id"]) and node["id"] not in passed

    return next((node for node in flow.get("nodes") or [] if open_required(node)), None)


def mark_backfill(flow: dict, states: dict, hits: dict) -> None:
    """越过插入位置、还能开始做的插件节点标「可补做」。"""
    from . import state as node_state

    for node_id in backfill_ids(flow, hits):
        item = states.get(node_id)
        if item is None or item["state"] not in ("ready", "not_ready"):
            continue
        item["flags"]["backfill"] = True
        item["display_state"] = node_state.display(item["state"], item["flags"])


def disabled_nodes(root: Path, flow: dict) -> list[dict]:
    """锁文件里停用了的包声明的节点（不在当前流程里的）：[{**节点, pack, version}]。"""
    from ...api import deps
    from ..packs import lockfile
    from . import conditions

    try:
        lock = lockfile.read(root)
    except (ValueError, OSError):
        return []
    if lock is None:
        return []
    present = {node["id"] for node in flow.get("nodes") or []}
    out = []
    for entry in lock.packs:
        if entry.enabled:
            continue
        try:
            declared = _declared_nodes(deps.workspaces.packs, entry.name, entry.version)
        except Exception:  # noqa: BLE001 - 一个包读不出来不影响流程
            _log.warning("读取停用包 %s %s 的节点失败", entry.name, entry.version, exc_info=True)
            continue
        # 按项目条件本来就不出现的节点（ADR-0083）也不列成已停用
        for node in declared:
            if not node.get("id") or node["id"] in present:
                continue
            if node.get("visible_if") and not conditions.holds(node["visible_if"], root):
                continue
            out.append({**node, "pack": entry.name, "version": f"{entry.name}@{entry.version}"})
    return out


def _declared_nodes(runtime, name: str, version: str) -> list[dict]:
    from ..packs.assembler import flow_declaration

    repository = runtime.repository
    if not repository.has(name, version):
        return []
    declared = flow_declaration(name, repository.manifest(name, version), repository.version_dir(name, version))
    return [node for node in (declared or {}).get("nodes") or [] if isinstance(node, dict)]
