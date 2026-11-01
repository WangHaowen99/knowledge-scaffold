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


def image_flow() -> dict:
    """镜像自带的主流程（core 源目录的 flow.yaml 组装结果）。"""
    global _IMAGE_FLOW
    with _LOCK:
        if _IMAGE_FLOW is None:
            data = yaml.safe_load(_IMAGE_FLOW_FILE.read_text(encoding="utf-8")) or {}
            _IMAGE_FLOW = assemble.assemble([("core", "image", data)])
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
    """项目列表上的阶段进度：{done, total, hits, stage, probes}。"""
    flow = load_for(root)
    state = flow_state(root)
    probes = state.evaluate(flow, root)
    info = state.progress(flow, root, probes)
    info["stage"] = state.legacy_stage(flow, root, probes)
    info["probes"] = probes
    return info
