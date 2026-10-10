"""引擎协议与平台事件契约。

平台事件（引擎 events() 产出的归一化 dict，前端与事件 JSONL 的唯一稳定接口）：

- ``stream_delta``        流式增量（kind: text | thinking），transient 不进历史
- ``assistant_message``   助手消息（blocks: text / thinking / tool_use）；
  ``parent_tool_use_id`` 非空表示这是子代理消息，归到对应子任务卡
- ``tool_results``        工具结果摘录（blocks: tool_result）
- ``subagent_started``    子代理开始执行（task_id / tool_use_id / description）
- ``subagent_activity``   子代理单步过程（step_id / kind: tool | text），同
  step_id 的后续事件只更新 status；参数只留摘要键，步骤数有上限
- ``subagent_finished``   子代理终态（completed / failed / stopped，usage.duration_ms）
- ``system``              运行时系统事件（已过滤，不进入会话历史）
- ``result``              一轮终态（成本 / usage / 时长）
- ``_engine_end``         引擎内部终止信号（reason: closed | error），由
  SessionManager 消化为 session_error / 状态迁移，不落历史、不发给前端

以下事件由 SessionManager 产出（引擎不产）：``user_message`` /
``session_started`` / ``session_state`` / ``session_error`` /
``permission_request`` / ``permission_resolved`` / ``queue_position`` /
``workspace_changes`` / ``session_interrupted``。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

# 面向用户的通用错误提示；原始错误只进运维留档，不出现在聊天流里
FRIENDLY_ERROR = "模型服务暂时不稳定，本次调用未完成。系统已记录详细信息，请重试；若持续出现请联系管理员。"
CONFIG_ERROR = (
    "模型网关未配置或凭据无效。请管理员在「配置中心 → 模型接入」填写网关地址与 API Key，"
    "保存后重新发送消息即可生效（无需重启）。"
)


def safe_json(value: Any) -> Any:
    """事件载荷兜底：不可 JSON 序列化的值降级为字符串。"""
    import json

    try:
        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


# request_permission 返回的中立决策 dict（不依赖任何 SDK 类型）：
#   {"behavior": "allow", "updated_input": dict | None}
#   {"behavior": "deny", "message": str}
PermissionRequester = Callable[
    [str, dict, str | None, str | None], Awaitable[dict]
]


@dataclass
class EngineContext:
    """SessionManager 交给引擎的全部依赖——引擎不得反向 import 会话层。"""

    project_id: str
    cwd: Path
    # 确认卡：引擎在工具调用需要人工确认时调用，等待专家答复（超时由会话层处理）
    request_permission: PermissionRequester
    # resume 续接点存取（格式由会话层管理，引擎只透传 token）
    load_resume_token: Callable[[], str | None]
    save_resume_token: Callable[[str], None]
    clear_resume_token: Callable[[], None]
    # 运维错误留档（kind, 原始文本）
    log_error: Callable[[str, str], None]
    # 事件历史（平台事件 dict 列表）：Jiuwen 引擎无可用 checkpoint 时用它回放重建模型上下文
    load_history: Callable[[], list[dict]] | None = None
    # 引擎绑定的会话；同一项目的多条会话各有独立的模型上下文
    conversation_id: str = "default"


class AgentEngine(Protocol):
    """引擎实现协议。

    实例与一次子进程/运行时生命周期一一对应，回收后重建实例
    （resume token 负责上下文续接）。
    """

    name: str
    client: Any  # 底层原始客户端；None = 未连接（admin 连接态与测试观察点）
    resume_attempted: bool

    async def connect(self) -> bool:
        """建立运行时连接；返回是否带 resume 续接。失败抛异常。"""
        ...

    async def send(self, text: str) -> None:
        """向运行中的会话投递一条用户消息。"""
        ...

    def events(self) -> AsyncIterator[dict]:
        """归一化平台事件流。运行时终止时产出 ``_engine_end`` 后结束，
        不向外抛异常（CancelledError 除外）。"""
        ...

    async def interrupt(self) -> None:
        """打断当前轮。"""
        ...

    async def disconnect(self) -> None:
        """断开运行时（保留续接点）。幂等。"""
        ...

    def config_stale(self) -> bool:
        """模型配置是否已变更（会话层据此断开重连使新配置生效）。"""
        ...
