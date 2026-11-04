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
- ``model_usage``         一次模型调用的用量（usage: input_tokens / output_tokens），
  逐次计入项目用量；不是对话行
- ``result``              一轮终态（成本 / usage / 时长）；``usage_per_call`` 为真表示
  本轮用量已由 model_usage 逐次计入，usage 只供单轮展示，统计时不再累加；
  ``message_ids`` 是这一轮消耗的消息（开这一轮的那条 + 轮内插进来的），None = 引擎说不清
  （旧版 CLI、单测假引擎），会话层按「一次发送一个 result」处理
- ``_delivery``           插话（steer）送到了哪儿（message_id / state: seen | new_turn）：
  seen = 插进这一轮、模型已看到；new_turn = 这一轮结束时还没插进去，引擎拿它开了下一轮
  （紧接着的事件都属于新的一轮）。会话层消化，不落历史
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
PERSONAL_CONFIG_ERROR = (
    "你的个人 API Key 无效或网关不可用。请在「设置 → 我的模型配置」更新或清除个人 Key"
    "（清除后改用管理员配置），保存后重新发送消息即可生效。"
)


def config_error_for(model_owner: str) -> str:
    """凭据类错误的提示：个人 Key 引导用户自己改，管理员配置维持原提示。"""
    return PERSONAL_CONFIG_ERROR if model_owner else CONFIG_ERROR


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
    # 本引擎实例使用谁的模型配置：用户名 = 该用户的个人 Key；"" = 管理员配置
    model_owner: str = ""
    # 本项目生效中的装配目录（项目 .claude 指向的 <数据目录>/assembled/<哈希>，方案 B15、D6）：
    # 规则、命令、守卫都从这里读；None = 旧工作区，没有装配目录
    assembly_dir: Path | None = None
    # 宿主注入（session_host/injection.Injector，方案 B4）：消息送到模型那一刻调 context_for(原文)；
    # None 时引擎自己按工作区建一个（单测直接构造引擎时）
    injector: Any = None
    # 引擎实例 ID（宿主生成，写进工作区的 .onto/run/engine.json）：agent 的命令、git 钩子据此认出
    # 「这是哪个会话的 agent」（方案 B7 会话身份注入）
    engine_instance: str = ""


EventObserver = Callable[[dict], None]


class ObservableEngine:
    """引擎事件的观察入口（方案 B15 add_observer）：插件观察会话、可观测采集用。

    观察者在引擎产出每个平台事件时同步调用，出错只吞掉，不影响会话。
    """

    def add_observer(self, observer: EventObserver) -> None:
        observers = self.__dict__.setdefault("_observers", [])
        if observer not in observers:
            observers.append(observer)

    def _notify(self, event: dict) -> None:
        for observer in list(self.__dict__.get("_observers") or []):
            try:
                observer(event)
            except Exception:  # noqa: BLE001 - 观察者出错不影响会话
                pass


class AgentEngine(Protocol):
    """引擎实现协议（两个引擎共用，方案 B15）。

    实例与一次子进程/运行时生命周期一一对应，回收后重建实例
    （resume token 负责上下文续接）。配置来自装配结果（ctx.assembly_dir）；
    会话配置指纹由宿主统一算（session_host/fingerprint.py），引擎只提供自己的模型部分。
    """

    name: str
    client: Any  # 底层原始客户端；None = 未连接（admin 连接态与测试观察点）
    resume_attempted: bool

    @classmethod
    def available(cls) -> bool:
        """当前部署里这个引擎能不能用（运行时装没装）。"""
        ...

    async def connect(self) -> bool:
        """建立运行时连接；返回是否带 resume 续接。失败抛异常。"""
        ...

    async def send(self, text: str, context: str | None = None, message_id: str | None = None) -> None:
        """空闲时开一轮；context 是宿主在两轮之间注入的上下文（不进会话历史）。
        message_id 是会话层给这条消息的编号，result 的 message_ids 按它报。"""
        ...

    async def steer(self, text: str, message_id: str) -> None:
        """一轮进行中的插话：主 agent 下次调模型前插进去（发 ``_delivery`` seen）；这一轮结束时还没插进去的，
        引擎拿它开下一轮（发 ``_delivery`` new_turn）。没有在跑的一轮时直接开一轮（同样发 new_turn）。"""
        ...

    def events(self) -> AsyncIterator[dict]:
        """归一化平台事件流。运行时终止时产出 ``_engine_end`` 后结束，
        不向外抛异常（CancelledError 除外）。"""
        ...

    async def interrupt(self, cancel_queued: bool = False) -> list[str] | None:
        """打断当前轮。cancel_queued：还没插进去的插话一并撤回，打断后不再自己跑起来；
        返回撤回的 message_id（None = 引擎说不清，会话层按全部撤回处理）。"""
        ...

    async def disconnect(self) -> None:
        """断开运行时（保留续接点）。幂等。"""
        ...

    async def kill(self) -> None:
        """硬终止：运行时悬死时不等它应答，直接结束（续接点保留）。"""
        ...

    def model_fingerprint(self) -> str:
        """当前模型配置（网关、Key、模型名、上下文参数）的摘要，计入宿主的会话指纹。"""
        ...

    def config_stale(self) -> bool:
        """模型配置是否已在连接后变更（兼容旧调用；宿主改用会话指纹判断）。"""
        ...

    def add_observer(self, observer: EventObserver) -> None:
        """登记事件观察者（见 ObservableEngine）。"""
        ...

    def context_limits(self) -> tuple[int | None, int | None]:
        """连接后已知的（模型窗口, 自动压缩触发点）；未知为 None，等每轮结束的 context_usage 再给。"""
        ...
