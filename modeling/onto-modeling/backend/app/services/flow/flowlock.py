"""项目的流程锁 `.onto/flow.lock`（详设 6.7 第 1 项、6.9 第 2 步、14#1；方案 D18）。

锁文件 `packs.lock` 定了用哪些包，组装后的流程写进 flow.lock：进工作区 git，和 packs.lock 在同一个提交里，
能追溯每份产物是在哪一版流程下做的。只记节点层面的东西（编号、来源包与版本、挂法、锚点、挡住谁、
完成判定签名、过期依据、写入范围），不记探针实现和下一步文案——那些随装配目录走。

内容由锁文件唯一决定（同样的包与版本写出同样的字节）：只启用不带节点的包时 flow.lock 不变。
生效仍按 H1：装配目录里的 flow.yaml 在下一条消息前切换，flow.lock 记的是锁文件要求的那一版。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

FLOW_LOCK_REL = ".onto/flow.lock"
FLOW_LOCK_VERSION = 1
_NODE_KEYS = ("title", "kind", "placement", "section", "after", "before", "blocks", "stale_on", "optional")


def _patterns(node: dict) -> list[str]:
    out = []
    for item in [*(node.get("produces") or []), *(node.get("writes") or [])]:
        pattern = item.get("path") if isinstance(item, dict) else item
        if pattern:
            out.append(str(pattern))
    return out


def summarize(flow: dict) -> dict:
    """组装后的流程 → flow.lock 的内容。"""
    from .state import done_signature

    nodes = []
    for node in flow.get("nodes") or []:
        item = {"id": node["id"], "pack": node.get("pack") or "core", "version": node.get("version") or ""}
        item.update({key: node[key] for key in _NODE_KEYS if node.get(key) not in (None, [], "")})
        item["done_sig"] = done_signature(flow, node)
        scope = _patterns(node)
        if scope:
            item["scope"] = scope
        nodes.append(item)
    body = {"flow_lock_version": FLOW_LOCK_VERSION, "flow_api": flow.get("flow_api") or 1, "nodes": nodes}
    body["nodes_hash"] = hashlib.sha256(json.dumps(nodes, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()[:16]
    return body


def render(flow: dict) -> str:
    return json.dumps(summarize(flow), ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def path(root: Path) -> Path:
    return Path(root) / FLOW_LOCK_REL


def read(root: Path) -> dict | None:
    try:
        data = json.loads(path(root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def write(root: Path, flow: dict) -> bool:
    """原子写 flow.lock，返回内容是否变了。"""
    target = path(root)
    text = render(flow)
    try:
        if target.read_text(encoding="utf-8") == text:
            return False
    except (OSError, UnicodeDecodeError):
        pass
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_name(f".{target.name}.{uuid.uuid4().hex[:8]}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, target)
    return True


def node_ids(data: dict | None) -> set[str]:
    return {str(item.get("id")) for item in (data or {}).get("nodes") or [] if item.get("id")}
