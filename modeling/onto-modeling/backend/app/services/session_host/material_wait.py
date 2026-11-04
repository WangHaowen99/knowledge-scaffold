"""建模命令开始前等资料转成文本（数据 schema 自动识别、自动处理）。

助手只读资料的文本版（原件不在主容器里解析，ADR-0076）。刚上传的资料还在转换时命令就开始，已经标为数据 Schema
的表读不到表头，会被当成「没有表」、接着去问用户。所以下面这几个建模命令真正交给引擎前，会话层先看项目里有没有
转换中、等待转换的资料：有就等（最多 WAIT_LIMIT_S 秒，会话状态带 REASON，运行状态行显示「等资料转成文本」），
等完照常开始；仍没有文本版的（没等到、转换失败、暂不支持）由宿主注入在这一轮点名，按未读处理。
自主构建的步骤走同一条发送路径，一样等、一样注入。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from pathlib import Path

COMMANDS = frozenset({
    "/ontology-research", "/ontology-extract-object", "/ontology-extract-operation",
    "/ontology-extract-rule-metric", "/ontology-validate",
})
REASON = "waiting_materials"
WAIT_LIMIT_S = 600
POLL_S = 3
# 注入里最多点名这么多份（每份一行路径加状态，约 2KB 以内），其余只给总数
NOTE_LIMIT = 20
_STATES = {"converting": "转换中", "waiting": "等待转换", "failed": "转换失败", "unsupported": "暂不支持"}


def applies(text: str) -> bool:
    """消息开头是要等资料的建模命令（手打的、页面发的、自主构建的步骤都以斜杠命令开头）。"""
    head = str(text or "").strip().split(maxsplit=1)
    return bool(head) and head[0] in COMMANDS


def _active(project_id: str, paths: list[str] | None, *, heal: bool) -> list[str]:
    from ...api import deps
    from ..material_conversion import gateway, service

    # 没部署网关时「等待转换」等不来，不等；部署了、暂时不可用的，网关恢复后会补转
    states = service.ACTIVE if gateway.base_url() else ("converting",)
    root = deps.workspaces.project_root(project_id)
    return [item["path"] for item in service.unready(project_id, root, paths, heal=heal) if item["state"] in states]


def converting(project_id: str) -> list[str]:
    """项目里转换中、等待转换的资料（缺文本版又没排上的顺手排队，免得一直等不到）。"""
    return _active(project_id, None, heal=True)


async def wait(project_id: str, paths: list[str], *, stop: Callable[[], bool]) -> list[str]:
    """等这几份转完（转成文本、转换失败、暂不支持都算完），返回到点仍在转的；stop() 为真时提前返回。"""
    deadline = time.monotonic() + WAIT_LIMIT_S
    left = list(paths)
    while left and not stop() and time.monotonic() < deadline:
        await asyncio.sleep(POLL_S)
        if stop():
            break
        left = await asyncio.to_thread(_active, project_id, left, heal=False)
    return left


def _label(item: dict) -> str:
    state = _STATES.get(item["state"], item["state"])
    return f"{state}：{item['reason']}" if item.get("reason") else state


def note(project_root: Path, user_text: str) -> str:
    """注入给模型的说明：这一轮的建模命令开始时仍没有文本版的资料，按未读处理。不是建模命令、都有文本版时为空串。"""
    root = Path(project_root)
    if not applies(user_text) or not (root / ".claude" / "scripts").is_dir():
        return ""
    from ...kernel.changesets import worktree
    from ..material_conversion import service

    main = Path(worktree.main_root(root))
    items = [item for item in service.unready(worktree.project_id_of(root), main) if not item["has_text"]]
    if not items:
        return ""
    listed = "；".join(f"{item['path']}（{_label(item)}）" for item in items[:NOTE_LIMIT])
    more = f"，另有 {len(items) - NOTE_LIMIT} 份" if len(items) > NOTE_LIMIT else ""
    return ("这些资料没有文本版，按未读处理（不读原件；已标为数据 Schema 的不要再请用户标，资料区能看到原因）："
            f"{listed}{more}。")
