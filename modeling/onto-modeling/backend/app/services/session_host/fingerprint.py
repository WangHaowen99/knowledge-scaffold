"""会话配置指纹（方案 B2；详设 3.5、8.1 第 4 步）：宿主统一算，引擎不再各算各的。

指纹由几部分组成，任何一部分变了，下一条消息前断开引擎、按新配置 resume 重连；正在跑的
一轮不切断，跑完旧版本（P0 的 B1 已经做到，这里把判断收口到一处）：

| 部分 | 来源 | 说明 |
|---|---|---|
| model | 引擎的 model_fingerprint() | 网关、Key、模型名、上下文参数 |
| owner | 会话的模型配置归属 | 个人 Key 或管理员配置 |
| assembly | 锁文件要求的装配（只算 H1 内容） | 只改了纯数据包（H0）不重连 |
| flow | 组装后的流程 | 节点声明变化 |
| mcp | 本项目启用的 MCP 服务器 | 只对 Claude（Jiuwen 不接 MCP） |
| permission_mode | 运行时权限模式 | 只对 Claude |

装配目录的切换（`.claude` 原子换指向）也在这里：引擎连上之前（没有正在跑的一轮）调 prepare。
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from typing import Any

_log = logging.getLogger(__name__)

KINDS = ("model", "owner", "assembly", "flow", "mcp", "permission_mode")
SUMMARIES = {
    "model": "模型配置有变化",
    "owner": "改用另一份模型配置",
    "assembly": "建模能力有更新",
    "flow": "建模流程有更新",
    "mcp": "MCP 服务器配置有变化",
    "permission_mode": "操作确认方式有变化",
}


@dataclass(frozen=True)
class Fingerprint:
    parts: dict[str, str] = field(default_factory=dict)

    @property
    def digest(self) -> str:
        body = json.dumps(self.parts, sort_keys=True).encode("utf-8")
        return hashlib.sha256(body).hexdigest()[:16]

    def diff(self, other: "Fingerprint | None") -> list[str]:
        """和另一份比，变了的部分（按 KINDS 的顺序）。"""
        if other is None:
            return []
        keys = set(self.parts) | set(other.parts)
        return [kind for kind in KINDS if kind in keys and self.parts.get(kind) != other.parts.get(kind)]


def _workspaces():
    # 宿主与接口层共用同一个工作区管理器（测试替换 deps.workspaces 时跟着换）
    from ...api import deps

    return deps.workspaces


def _hash(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def prepare(project_id: str) -> dict:
    """下一条消息前（这个项目没有正在跑的一轮）：跟随通道、按锁文件装配并原子切换 `.claude`。

    出错只记日志：装配不了就保持原装配目录，不挡消息。
    """
    from ..packs import service as packs_service

    try:
        return packs_service.prepare_for_message(_workspaces(), project_id)
    except Exception as exc:  # noqa: BLE001 - 项目已删除、git 失败等：沿用当前装配
        _log.warning("项目 %s 消息前装配切换失败，沿用当前装配：%s", project_id, exc)
        return {}


def compute(engine: Any, project_id: str, owner: str) -> Fingerprint:
    """当前应当生效的指纹（锁文件要求的装配，不是 `.claude` 现在指的那个）。"""
    from ..packs import service as packs_service

    parts: dict[str, str] = {"owner": owner or ""}
    model = getattr(engine, "model_fingerprint", None)
    if callable(model):
        try:
            parts["model"] = str(model())
        except Exception as exc:  # noqa: BLE001 - 配置读不出：按变化处理交给重连去报错
            parts["model"] = f"error:{type(exc).__name__}"
    try:
        parts.update(packs_service.desired_parts(_workspaces(), project_id))
    except Exception as exc:  # noqa: BLE001 - 指纹的装配部分算不出：不参与比较
        _log.warning("项目 %s 的装配指纹算不出：%s", project_id, exc)
    if getattr(engine, "name", "") == "claude":
        from ..engines import claude

        parts["mcp"] = _hash(claude.mcp_fingerprint_source(project_id))
        parts["permission_mode"] = claude.current_permission_mode()
    return Fingerprint(parts)


def changes(engine: Any, project_id: str, owner: str, connected: Fingerprint | None) -> list[str]:
    """连上以来变了的部分；连上时没留指纹（旧实例）时退回引擎自己的 config_stale()。"""
    if connected is None:
        stale = getattr(engine, "config_stale", None)
        return ["model"] if callable(stale) and stale() else []
    current = compute(engine, project_id, owner)
    found = current.diff(connected)
    stale = getattr(engine, "config_stale", None)
    if "model" not in found and callable(stale) and not callable(getattr(engine, "model_fingerprint", None)) and stale():
        found.insert(0, "model")
    return found


def describe(kinds: list[str]) -> list[dict]:
    return [{"kind": kind, "summary": SUMMARIES.get(kind, kind)} for kind in kinds]
