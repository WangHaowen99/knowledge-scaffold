"""节点状态上插件相关的部分（详设 6.9 第 4～6 步、4.10.3 熔断；契约 7.8 FlowNode.flags、O-29）。

节点的 8 种状态由 `state.compute` 按执行记录、变更集、指纹算；这里在它前后补上与包的启停有关的三件事，
不改那套算法：

- **熔断**：运维把某个包的某个版本熔断（O-29）后，它的节点暂时跳过，它挡住的关卡解除——算状态前把这些节点的
  `blocks` 拿掉，算完标成已跳过；
- **可补做**：项目已经越过插入位置（插件节点后面的主线节点已经在主线上完成），新串入的节点标 `backfill`，
  不计入进度的分母，主线状态不往回退；
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
        if pack and pack != "core" and version and ops_state.breaker_state(pack, version) == "open":
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


def backfill_ids(flow: dict, hits: dict) -> set[str]:
    """串入、关卡这类插件节点后面的主线节点已经在主线上完成、它自己还没完成：越过了插入位置。

    主线位置（spine）的节点不算：它们随 core 或从 core 拆出的包（场景走查）本来就在流程里，没做就是没做，不是「新插进来的」。"""
    order = [node for node in flow.get("nodes") or []
             if node.get("placement", "spine") in MAINLINE and not node.get("optional")]
    out = set()
    for index, node in enumerate(order):
        if node.get("pack", "core") == "core" or node.get("placement", "spine") == "spine" or hits.get(node["id"]):
            continue
        if any(hits.get(later["id"]) for later in order[index + 1:]):
            out.add(node["id"])
    return out


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
        out += [{**node, "pack": entry.name, "version": f"{entry.name}@{entry.version}"}
                for node in declared if node.get("id") and node["id"] not in present]
    return out


def _declared_nodes(runtime, name: str, version: str) -> list[dict]:
    from ..packs.assembler import flow_declaration

    repository = runtime.repository
    if not repository.has(name, version):
        return []
    declared = flow_declaration(name, repository.manifest(name, version), repository.version_dir(name, version))
    return [node for node in (declared or {}).get("nodes") or [] if isinstance(node, dict)]
