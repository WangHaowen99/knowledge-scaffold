"""按项目数据算的内核条件（ADR-0083）：流程节点出不出现、内核探针、提示规则注不注入。

条件名是内核给的固定集合，包只能引用、不能自己写判断（包代码不在内核里跑）。现在只有证据源两条：

- `evidence_sources`：项目有授权的数据源（授权存数据目录，智能体改不了；`app/evidence/grants`）；
- `evidence_snapshot`：授权的源里有一份完整快照（`resource/systems/<源>/<快照>/manifest.json` 的 `complete: true`）。

三处用法：

- 节点 `visible_if: <条件>`：不满足时节点不在本项目的流程里（`apply`，`model.load_for` 调），连线、命令归属、
  钩子一起去掉，挂在它后面的节点改挂到它的上游；
- 探针 `{kernel: <条件>}`：完成判定用。工作区里的 flow_state 不认识，恒为否；后端经 `with_kernel_probes`
  包一层补上（`model.flow_state` 调）。探针定义不变，完成判定签名就不会随结果变；
- 提示规则 `visible_if: <条件>`：不满足时不注入（`packs/registry.prompt_rules`）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

_log = logging.getLogger(__name__)

SYSTEMS_DIR = Path("resource") / "systems"


def _main_root(root: Path) -> Path:
    """助手副本 → 主线工作区（授权按项目记，快照只经内核写入路径进主线）。"""
    from ...kernel.changesets import worktree

    return Path(worktree.main_root(Path(root)) or root)


def _authorized(root: Path) -> list[str]:
    from ...evidence import grants

    return grants.authorized_ids(_main_root(root).name)


def _evidence_sources(root: Path) -> bool:
    return bool(_authorized(root))


def _evidence_snapshot(root: Path) -> bool:
    base = _main_root(root) / SYSTEMS_DIR
    for source in _authorized(root):
        directory = base / source
        if "/" in source or source.startswith(".") or not directory.is_dir():
            continue
        for manifest in directory.glob("*/manifest.json"):
            try:
                if json.loads(manifest.read_text(encoding="utf-8")).get("complete") is True:
                    return True
            except (OSError, ValueError, AttributeError):
                continue
    return False


CONDITIONS = {
    "evidence_sources": _evidence_sources,
    "evidence_snapshot": _evidence_snapshot,
}


def holds(name: str, root: Path) -> bool:
    """条件成不成立；不认识的条件不成立（新版包引用了旧内核没有的条件：宁可不出现）。"""
    check = CONDITIONS.get(str(name or ""))
    if check is None:
        return False
    try:
        return bool(check(Path(root)))
    except Exception:  # noqa: BLE001 - 条件算不出来按不成立，不影响流程其余部分
        _log.warning("内核条件 %s 算不出来", name, exc_info=True)
        return False


def apply(flow: dict, root: Path) -> dict:
    """去掉本项目不出现的节点；没有要去掉的原样返回（不复制）。

    返回的流程多 `hidden_nodes`，`flow_hash` 换成去掉之后的摘要：节点集合变了，推给前端的流程摘要也要变。"""
    nodes = flow.get("nodes") or []
    if not any(node.get("visible_if") for node in nodes):
        return flow
    hidden = {node["id"] for node in nodes if node.get("visible_if") and not holds(node["visible_if"], root)}
    if not hidden:
        return flow
    from .assemble import flow_hash

    upstream = {node["id"]: node.get("after") for node in nodes}

    def anchor(node_id):
        while node_id in hidden:
            node_id = upstream.get(node_id)
        return node_id

    kept = []
    for node in nodes:
        if node["id"] in hidden:
            continue
        if node.get("after") in hidden:
            node = {**node, "after": anchor(node["after"])}
        kept.append(node)
    out = {**flow, "nodes": kept, "hidden_nodes": sorted(hidden)}
    out["edges"] = [edge for edge in flow.get("edges") or [] if not hidden & {edge.get("from"), edge.get("to")}]
    out["commands"] = {name: spec for name, spec in (flow.get("commands") or {}).items()
                       if not (isinstance(spec, dict) and spec.get("node") in hidden)}
    if flow.get("hooks"):
        out["hooks"] = [hook for hook in flow["hooks"] if hook.get("node") not in hidden]
    out["flow_hash"] = flow_hash(out)
    return out


def kernel_probes(flow: dict, root: Path) -> dict[str, bool]:
    """流程里 `{kernel: <条件>}` 探针的结果。"""
    return {name: holds(spec["kernel"], root) for name, spec in (flow.get("probes") or {}).items()
            if isinstance(spec, dict) and spec.get("kernel")}


class _WithKernelProbes:
    """本项目锁定版本的 flow_state 加上内核探针：会自己算探针的几个函数改成先补上内核探针，其余原样转过去。"""

    def __init__(self, module):
        self._module = module

    def __getattr__(self, name):
        return getattr(self._module, name)

    def evaluate(self, flow: dict, root: Path) -> dict:
        probes = self._module.evaluate(flow, root)
        probes.update(kernel_probes(flow, root))
        return probes

    def _probes(self, flow, root, probes):
        return self.evaluate(flow, root) if probes is None else probes

    def node_hits(self, flow: dict, root: Path, probes: dict | None = None) -> dict:
        return self._module.node_hits(flow, root, self._probes(flow, root, probes))

    def progress(self, flow: dict, root: Path, probes: dict | None = None) -> dict:
        return self._module.progress(flow, root, self._probes(flow, root, probes))

    def legacy_stage(self, flow: dict, root: Path, probes: dict | None = None) -> str:
        return self._module.legacy_stage(flow, root, self._probes(flow, root, probes))


def with_kernel_probes(module):
    return _WithKernelProbes(module)
