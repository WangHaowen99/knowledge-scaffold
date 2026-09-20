"""Agent 引擎层：把"会话编排"与"具体 agent 运行时"解耦。

- SessionManager（agent_sessions.py）负责池化、排队、TTL 回收、事件持久化、
  SSE 广播、确认卡 future——全部引擎无关；
- 引擎实现（claude.py，后续 jiuwen.py）负责具体运行时的连接、消息归一化、
  resume 续接与错误翻译，向上只暴露 AgentEngine 协议与平台事件。

设计见 docs/openjiuwen-engine-migration.md §3.1。
"""

from .base import CONFIG_ERROR, FRIENDLY_ERROR, AgentEngine, EngineContext

DEFAULT_ENGINE = "claude"


def get_engine_class(name: str = DEFAULT_ENGINE):
    """按名称取引擎实现；未知名称回落默认引擎（存量项目 meta 无该字段）。"""
    from .claude import ClaudeEngine

    registry: dict = {"claude": ClaudeEngine}
    try:
        from .jiuwen import JiuwenEngine

        registry["jiuwen"] = JiuwenEngine
    except ImportError:  # pragma: no cover - 取决于部署环境
        pass
    return registry.get(name or DEFAULT_ENGINE, ClaudeEngine)


__all__ = [
    "AgentEngine",
    "EngineContext",
    "FRIENDLY_ERROR",
    "CONFIG_ERROR",
    "DEFAULT_ENGINE",
    "get_engine_class",
]
