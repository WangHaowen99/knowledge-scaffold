"""项目的主流程：读装配目录里组装好的 flow.yaml，用 core 包的 flow_state 计算（方案 D12）。

项目 `.claude` 指向装配目录时读它的 flow.yaml（本项目锁定版本的流程）；没有装配目录的旧
工作区与测试里的最小工作区，读镜像里 core 包源目录的 flow.yaml 现组装一份。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import yaml

from ... import config
from . import assemble

_IMAGE_FLOW: dict | None = None
_LOCK = threading.Lock()
# 镜像目录在进程启动时定下（同共享契约库）
_IMAGE_FLOW_FILE = Path(config.ASSETS_DIR) / "workspace-toolkit" / "flow.yaml"


def _image_pack_flows() -> list[tuple[str, str, dict]]:
    """镜像里从 core 拆出去的官方包的流程声明：没有装配目录的旧工作区原来用的是整个工具箱，
    拆包后仍按「全部能力都在」组装，进度口径不变。"""
    out = []
    base = Path(config.ASSETS_DIR) / "packs"
    for directory in sorted(base.iterdir()) if base.is_dir() else []:
        try:
            manifest = yaml.safe_load((directory / "manifest.yaml").read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError):
            continue
        flow_rel = (manifest.get("contributes") or {}).get("flow")
        if not manifest.get("split_from_core") or not flow_rel or not (directory / flow_rel).is_file():
            continue
        out.append((directory.name, "image", yaml.safe_load((directory / flow_rel).read_text(encoding="utf-8")) or {}))
    return out


def image_flow() -> dict:
    """镜像自带的主流程（core 源目录的 flow.yaml 加上拆出去的官方包的节点）。"""
    global _IMAGE_FLOW
    with _LOCK:
        if _IMAGE_FLOW is None:
            data = yaml.safe_load(_IMAGE_FLOW_FILE.read_text(encoding="utf-8")) or {}
            packs = _image_pack_flows()
            _IMAGE_FLOW = assemble.assemble([("core", "image", data), *packs],
                                            trusted={name for name, _version, _flow in packs})
        return _IMAGE_FLOW


def load_for(root: Path) -> dict:
    """本项目生效中的主流程：按项目条件不出现的节点已去掉（ADR-0083）。"""
    from . import conditions

    return conditions.apply(_assembled(root), root)


def _assembled(root: Path) -> dict:
    link = Path(root) / ".claude"
    if os.path.lexists(link):
        real = Path(os.path.realpath(link))
        path = real / "flow.yaml"
        if real.parent.name == "assembled" and path.is_file():
            try:
                # 老装配格式（还没重新装配）的节点没有 need：读进来补上
                return assemble.normalize_flow(json.loads(path.read_text(encoding="utf-8")))
            except (OSError, ValueError):
                pass
    return image_flow()


def flow_state(root: Path):
    """本项目锁定版本的 flow_state（探针实现），补上内核算的探针（ADR-0083）。"""
    from ..packs import contracts
    from . import conditions

    return conditions.with_kernel_probes(contracts.module_for(root, "flow_state"))


def progress(root: Path) -> dict:
    """项目列表上的阶段进度：{done, total, hits, stage, probes}，和 FL-01 的进度同一口径（`overlay.progress_ids`）：
    只算必需节点，可补做的不算。内核自己算，锁定旧 core 的项目也一样（旧版 flow_state 还把可选节点算进去）。"""
    from .overlay import progress_ids

    flow = load_for(root)
    state = flow_state(root)
    probes = state.evaluate(flow, root)
    hits = state.node_hits(flow, root, probes)
    counted = progress_ids(flow, hits)
    return {"done": sum(1 for node_id in counted if hits.get(node_id)), "total": len(counted), "hits": hits,
            "stage": state.legacy_stage(flow, root, probes), "probes": probes}
