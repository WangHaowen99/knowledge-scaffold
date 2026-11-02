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
            _IMAGE_FLOW = assemble.assemble([("core", "image", data), *_image_pack_flows()])
        return _IMAGE_FLOW


def load_for(root: Path) -> dict:
    """本项目生效中的主流程。"""
    link = Path(root) / ".claude"
    if os.path.lexists(link):
        real = Path(os.path.realpath(link))
        path = real / "flow.yaml"
        if real.parent.name == "assembled" and path.is_file():
            try:
                return json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                pass
    return image_flow()


def flow_state(root: Path):
    """本项目锁定版本的 flow_state（探针实现）。"""
    from ..packs import contracts

    return contracts.module_for(root, "flow_state")


def progress(root: Path) -> dict:
    """项目列表上的阶段进度：{done, total, hits, stage, probes}。按切片建模时建模节点要全部片做完才算（同 FL-01）。"""
    flow = load_for(root)
    state = flow_state(root)
    probes = state.evaluate(flow, root)
    info = state.progress(flow, root, probes)
    sliced = _sliced(root, flow)
    if sliced is not None and sliced["node_id"] in info["hits"]:
        before = info["hits"][sliced["node_id"]]
        info["hits"][sliced["node_id"]] = sliced["hit"]
        counted = any(node["id"] == sliced["node_id"] for node in state.progress_nodes(flow))
        info["done"] += (int(sliced["hit"]) - int(before)) if counted else 0
    # 越过插入位置的插件节点（可补做）做完之前不计入分母，和 FL-01 的进度同一口径（按片覆盖之后再判越没越过）
    from .overlay import backfill_ids

    progress_ids = {node["id"] for node in state.progress_nodes(flow)}
    info["total"] -= len(backfill_ids(flow, info["hits"]) & progress_ids)
    info["stage"] = state.legacy_stage(flow, root, probes)
    info["probes"] = probes
    return info


def _sliced(root: Path, flow: dict) -> dict | None:
    """按片模式时建模节点按切片的汇总；全量模式（没有蓝图或只有一片）时 None。"""
    from . import blueprint, slices

    doc = blueprint.load_doc(root)
    if blueprint.script(root).mode(doc) != "sliced":
        return None
    try:
        return slices.compute(root, flow, doc=doc, persist=False)
    except Exception:  # noqa: BLE001 - 进度是概览，算不出时按完成判定
        return None


_CELLS = {"done": "confirmed", "now": "running", "wait": "pending", "todo": "ready"}


def slice_summary(root: Path) -> dict | None:
    """项目卡片上的切片进度（契约 7.3 ProjectCard.slices）：按片建模时 {total, confirmed, cells}，每片一格；
    全量模式（没有蓝图或只有一片）时 None（切片待规划）。"""
    flow = load_for(root)
    sliced = _sliced(root, flow)
    if sliced is None:
        return None
    items = sliced["slices"]
    cells = []
    for item in items:
        stale = any(value == "stale" for value in item["stages"].values())
        cells.append("stale" if stale else _CELLS.get(item["group"], "ready"))
    return {"total": len(items), "confirmed": sum(1 for item in items if item["group"] == "done"), "cells": cells}
