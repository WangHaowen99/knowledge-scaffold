"""Claude Agent SDK 引擎：每会话一个捆绑 CLI 子进程。

从 agent_sessions.py 平移（Phase 1 引擎抽象，行为零变化）：
- 选项构建（权限回调、MCP、环境变量、resume、降级 CLI 路径）
- SDK 消息 → 平台事件归一化
- CLI 原始错误翻译（原文留档，聊天流只出中文提示）
- resume 续接：捕获 SDK session id 经 ctx 落盘；连接失败自动清点重试
- 上下文用量：每轮结束发 context_usage（已用/窗口/压缩触发点），压缩时发 context_compacted
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ... import config
from .base import FRIENDLY_ERROR, EngineContext, ObservableEngine, config_error_for, safe_json

_RAW_ERROR_PATTERN = None  # 延迟编译
_CONFIG_ERROR_PATTERN = None


def _looks_like_config_error(text: str) -> bool:
    """未登录/凭据无效类错误：给专属中文指引而不是笼统的"服务不稳定"。"""
    global _CONFIG_ERROR_PATTERN
    if _CONFIG_ERROR_PATTERN is None:
        import re

        _CONFIG_ERROR_PATTERN = re.compile(
            r"Not logged in|/login|Invalid API key|authentication_error|authentication_failed|"
            r"invalid x-api-key|401 Unauthorized|status.?401",
            re.IGNORECASE,
        )
    return bool(_CONFIG_ERROR_PATTERN.search(text or ""))


def _looks_like_raw_error(text: str) -> bool:
    global _RAW_ERROR_PATTERN
    if _RAW_ERROR_PATTERN is None:
        import re

        _RAW_ERROR_PATTERN = re.compile(
            r"API Error|Claude Code|request id:|exit code|invalid_grant|"
            r"Not logged in|Please run /login|Invalid API key|authentication_error|"
            r"OAuth|Traceback \(most recent call last\)",
            re.IGNORECASE,
        )
    return bool(_RAW_ERROR_PATTERN.search(text or ""))


def _result_failed(message: Any) -> bool:
    """Return whether an SDK result explicitly reports a failed turn."""
    if bool(getattr(message, "is_error", False)):
        return True
    subtype = str(getattr(message, "subtype", "") or "")
    return subtype.startswith("error")


def _result_error_text(message: Any) -> str:
    """Collect structured failure details for a legacy error body."""
    parts: list[str] = []
    for value in (
        getattr(message, "error", None),
        getattr(message, "errors", None),
        getattr(message, "result", None),
        getattr(message, "stop_reason", None),
    ):
        if value:
            parts.append(str(value))
    return "\n".join(parts)


def _mark_workspace_trusted(cwd: Path) -> None:
    """连接前把工作区登记为已信任：CLI 对未信任目录会忽略
    .claude/settings.json 的 allow 规则（守卫 hooks 所在），只提示不生效。"""
    try:
        path = Path.home() / ".claude.json"
        data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        entry = data.setdefault("projects", {}).setdefault(str(cwd), {})
        if not entry.get("hasTrustDialogAccepted"):
            entry["hasTrustDialogAccepted"] = True
            path.write_text(json.dumps(data), encoding="utf-8")
    except Exception:
        pass  # 登记失败只影响 allow 规则加载，不阻断会话


try:
    from claude_agent_sdk import (
        AssistantMessage,
        ClaudeAgentOptions,
        ClaudeSDKClient,
        PermissionResultAllow,
        PermissionResultDeny,
        ResultMessage,
        StreamEvent,
        SystemMessage,
        TextBlock,
        ThinkingBlock,
        ToolResultBlock,
        ToolUseBlock,
        UserMessage,
    )

    SDK_AVAILABLE = True
except ImportError:  # pragma: no cover - 取决于部署环境
    SDK_AVAILABLE = False


def _mcp_servers(project_id: str = "") -> dict:
    from .. import mcp_settings

    disabled: set[str] = set()
    if project_id:
        # 直接读项目 meta，避免与 workspace 管理层互相 import
        meta_path = Path(config.DATA_DIR) / "workspaces" / project_id / ".onto-web.json"
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            disabled = set(meta.get("mcp_disabled") or [])
        except (OSError, json.JSONDecodeError):
            pass
    try:
        return mcp_settings.sdk_config(disabled)
    except Exception:
        return {}


def _permission_mode() -> str:
    try:
        from .. import runtime_settings

        return runtime_settings.permission_mode()
    except Exception:
        return config.PERMISSION_MODE


def mcp_fingerprint_source(project_id: str) -> dict:
    """会话指纹的 MCP 部分（只取摘要，不落盘、不外传）。"""
    return _mcp_servers(project_id)


def current_permission_mode() -> str:
    """会话指纹的权限模式部分。"""
    return _permission_mode()


def _env_fingerprint(owner: str = "") -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(_owner_env(owner), sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _owner_env(owner: str) -> dict:
    # 管理员配置仍按原样无参调用 _session_env
    return _session_env(owner) if owner else _session_env()


def _session_env(owner: str = "") -> dict:
    """owner 为空 = 管理员配置（原行为）；非空 = 该用户的个人 Key。"""
    env = {}
    if config.MAX_THINKING_TOKENS:
        env["MAX_THINKING_TOKENS"] = config.MAX_THINKING_TOKENS
    from .. import model_settings

    if owner:
        # 个人配置出错要让本次连接失败，吞掉会悄悄改用部署机的 Key
        env.update(model_settings.session_env(owner))
        return env
    try:
        env.update(model_settings.session_env())
    except Exception:
        pass
    return env


def normalize_message(message: Any) -> dict | None:
    """把 SDK 消息 dataclass 转成前端可渲染的扁平事件。"""
    if isinstance(message, AssistantMessage):
        blocks = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append({"kind": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                # display: omitted 时 thinking 正文是空串，不产出空卡片
                if (block.thinking or "").strip():
                    blocks.append({"kind": "thinking", "text": block.thinking})
            elif isinstance(block, ToolUseBlock):
                blocks.append(
                    {
                        "kind": "tool_use",
                        "tool": block.name,
                        "input": safe_json(block.input),
                        "tool_use_id": block.id,
                    }
                )
        event = {
            "type": "assistant_message",
            "blocks": blocks,
            "model": message.model,
            "subagent": bool(getattr(message, "parent_tool_use_id", None)),
            # 子代理消息带上派遣它的 Task/Agent 调用 id，前端据此把过程归到对应卡片
            "parent_tool_use_id": getattr(message, "parent_tool_use_id", None),
        }
        if not event["subagent"]:
            context_tokens = _context_tokens(getattr(message, "usage", None))
            if context_tokens:
                event["context_tokens"] = context_tokens
        return event
    if isinstance(message, UserMessage):
        blocks = []
        content = message.content
        if isinstance(content, list):
            for block in content:
                if isinstance(block, ToolResultBlock):
                    blocks.append(
                        {
                            "kind": "tool_result",
                            "tool_use_id": block.tool_use_id,
                            "is_error": bool(block.is_error),
                            "excerpt": _tool_result_excerpt(block.content),
                        }
                    )
        if not blocks:
            return None  # 用户文本消息已在 send() 时广播过
        return {"type": "tool_results", "blocks": blocks}
    lifecycle_event = _normalize_task_event(message)
    if lifecycle_event:
        return lifecycle_event
    if isinstance(message, SystemMessage):
        if message.subtype == "compact_boundary":
            metadata = (message.data or {}).get("compact_metadata") or {}
            return {
                "type": "context_compacted",
                "trigger": metadata.get("trigger") or "auto",
                "pre_tokens": metadata.get("pre_tokens"),
                "post_tokens": metadata.get("post_tokens"),
            }
        # SDK diagnostics can arrive at token frequency. They are not chat
        # content; keeping them in the session log starves the visible history
        # window and makes the unread badge count runtime noise.
        # task_* 生命周期消息已在上面归一化为子代理事件，这里一并丢弃。
        return None
    if isinstance(message, ResultMessage):
        return {
            "type": "result",
            "subtype": message.subtype,
            "is_error": message.is_error,
            "duration_ms": message.duration_ms,
            "total_cost_usd": message.total_cost_usd,
            "num_turns": message.num_turns,
            "usage": safe_json(getattr(message, "usage", None)),
        }
    if isinstance(message, StreamEvent):
        # 子代理的增量不转发：它的过程由带 parent_tool_use_id 的整条消息呈现在
        # 子任务卡里，增量混进主气泡会把内部上下文摊到主对话上
        if message.parent_tool_use_id:
            return None
        raw = message.event
        if raw.get("type") == "content_block_delta":
            delta = raw.get("delta", {})
            if delta.get("type") == "text_delta" and delta.get("text"):
                return {"type": "stream_delta", "kind": "text", "text": delta["text"]}
            if delta.get("type") == "thinking_delta" and delta.get("thinking"):
                return {
                    "type": "stream_delta",
                    "kind": "thinking",
                    "text": delta["thinking"],
                }
        return None
    return None


_CONTEXT_USAGE_KEYS = (
    "input_tokens",
    "cache_creation_input_tokens",
    "cache_read_input_tokens",
    "output_tokens",
)
# CLI 的自动压缩阈值 = 有效窗口 − 13000（有效窗口 = 窗口 − 输出预留）
_AUTOCOMPACT_BUFFER_TOKENS = 13000


def _context_tokens(usage: Any) -> int | None:
    """单次模型调用占用的上下文（输入含缓存读写 + 输出），与 CLI 判断自动压缩的口径一致。

    ResultMessage.usage 是一轮内多次调用的累加，不能拿来算上下文。
    """
    if not isinstance(usage, dict):
        return None
    total = 0
    for key in _CONTEXT_USAGE_KEYS:
        value = usage.get(key)
        if isinstance(value, (int, float)):
            total += int(value)
    return total or None


def context_limits(usage: Any, env: dict) -> tuple[int | None, int | None]:
    """从 get_context_usage 的结果取（模型窗口, 自动压缩触发点）。

    CLI 报告的 autoCompactThreshold 不含 CLAUDE_AUTOCOMPACT_PCT_OVERRIDE，
    这里按 CLI 的规则补算：触发点 = min(有效窗口 × 百分比, 有效窗口 − 13000)。
    """
    if not isinstance(usage, dict):
        return None, None
    window = usage.get("rawMaxTokens") or usage.get("maxTokens")
    window = window if isinstance(window, int) and window > 0 else None
    threshold = usage.get("autoCompactThreshold")
    if not usage.get("isAutoCompactEnabled") or not isinstance(threshold, int):
        return window, None
    try:
        pct = float(env.get("CLAUDE_AUTOCOMPACT_PCT_OVERRIDE") or 0)
    except ValueError:
        pct = 0
    if 0 < pct <= 100:
        effective = threshold + _AUTOCOMPACT_BUFFER_TOKENS
        threshold = min(threshold, int(effective * pct / 100))
    return window, threshold


_TERMINAL_TASK_STATUSES = {"completed", "failed", "stopped", "killed"}
_TASK_SUBTYPES = {"task_started", "task_progress", "task_notification", "task_updated"}


def _task_value(message: Any, name: str, default: Any = None) -> Any:
    """读取 SDK task 生命周期对象字段，并兼容旧版的 data 回退。"""
    value = getattr(message, name, None)
    if value is not None:
        return value
    data = getattr(message, "data", None)
    if isinstance(data, dict):
        return data.get(name, default)
    return default


def _task_usage(value: Any) -> dict:
    """只保留耗时：卡片不展示工具次数与 tokens，其余 SDK 载荷不入事件历史。"""
    if not isinstance(value, dict) or "duration_ms" not in value:
        return {}
    return {"duration_ms": value["duration_ms"]}


def _short_task_summary(value: Any, limit: int = 400) -> str:
    text = str(value or "").strip()
    return text[:limit] + ("…" if len(text) > limit else "")


def _normalize_task_event(message: Any) -> dict | None:
    """把 Claude SDK 的 task_* 系统消息转成稳定的平台子代理事件。

    只取开始与终态：子代理的实时过程由带 ``parent_tool_use_id`` 的子代理消息
    提供，``task_progress`` 这类周期心跳不再入事件历史。
    SDK 的 task 生命周期消息在不同版本中既可能是专用 dataclass，也可能
    回落为带 ``subtype``/``data`` 的 SystemMessage，因此这里按字段读取，
    不把 SDK 类型泄漏到会话层。
    """
    subtype = str(getattr(message, "subtype", "") or "")
    if subtype not in _TASK_SUBTYPES:
        return None
    task_id = str(_task_value(message, "task_id", "") or "")
    if not task_id:
        return None
    tool_use_id = _task_value(message, "tool_use_id")
    description = _short_task_summary(_task_value(message, "description", ""), 240)
    common = {
        "task_id": task_id,
        "tool_use_id": str(tool_use_id) if tool_use_id else None,
        "description": description,
    }
    if subtype == "task_started":
        return {
            "type": "subagent_started",
            **common,
            "agent_type": _short_task_summary(_task_value(message, "task_type", ""), 80),
        }
    if subtype == "task_progress":
        return None
    if subtype == "task_updated":
        patch = _task_value(message, "patch", {})
        if not isinstance(patch, dict):
            patch = {}
        status = str(_task_value(message, "status", "") or patch.get("status") or "")
        if status not in _TERMINAL_TASK_STATUSES:
            return None  # 非终态的后台任务变更对卡片没有新信息
        return {
            "type": "subagent_finished",
            **common,
            "description": description
            or _short_task_summary(patch.get("description"), 240),
            "status": status,
            "summary": _short_task_summary(patch.get("summary")),
            "usage": _task_usage(patch.get("usage", {})),
        }
    return {
        "type": "subagent_finished",
        **common,
        "status": str(_task_value(message, "status", "failed") or "failed"),
        "summary": _short_task_summary(_task_value(message, "summary", "")),
        "usage": _task_usage(_task_value(message, "usage", {})),
    }


def _tool_result_excerpt(content: Any, limit: int = 400) -> str:
    """工具结果的文本摘录（对话内展开详情用）；结构化内容取 text 块拼接。"""
    if content is None:
        return ""
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        text = "\n".join(parts)
    else:
        text = str(content)
    text = text.strip()
    return text[:limit] + ("…" if len(text) > limit else "")


class ClaudeEngine(ObservableEngine):
    """Claude Agent SDK 引擎实现（AgentEngine 协议）。"""

    name = "claude"
    UNAVAILABLE_MESSAGE = "claude-agent-sdk 未安装，无法启动建模会话"

    @classmethod
    def available(cls) -> bool:
        return SDK_AVAILABLE

    def __init__(self, ctx: EngineContext):
        self._ctx = ctx
        self.client: Any = None
        self.resume_attempted = False
        self._received_any = False
        self._sdk_session_id: str | None = None
        self._fingerprint = ""
        # 上下文用量：已用取最近一次主代理调用；窗口与触发点见 _init_context_limits
        self._context_used: int | None = None
        self._context_window: int | None = None
        self._compact_at: int | None = None
        self._limits_task: asyncio.Task | None = None
        self._limits_after_turn = False
        # Hold text-only assistant messages until the same turn's structured
        # result confirms success or failure of a legacy CLI envelope.
        self._pending_assistant: dict | None = None

    # ---- 连接生命周期 ----

    def _create_client(self, options: Any) -> Any:
        """测试替换点：单测注入假客户端，避免拉起真实 CLI 子进程。"""
        return ClaudeSDKClient(options=options)

    async def connect(self) -> bool:
        _mark_workspace_trusted(self._ctx.cwd)
        resume_id = self._ctx.load_resume_token()
        client = self._create_client(self._build_options(resume_id))
        try:
            await client.connect()
        except Exception:
            if not resume_id:
                raise
            # resume 失败（transcript 缺失/损坏）：清掉续接点，全新会话重试
            self._ctx.clear_resume_token()
            resume_id = None
            client = self._create_client(self._build_options(None))
            await client.connect()
        self.resume_attempted = bool(resume_id)
        self._received_any = False
        self.client = client
        self._fingerprint = _env_fingerprint(self._ctx.model_owner)
        self._context_used = None
        self._cancel_limits_task()
        self._init_context_limits()
        return bool(resume_id)

    def _init_context_limits(self) -> None:
        """连接后定窗口与触发点，不能挡住首条消息。

        向 CLI 查用量时，它会先对网关逐类计数 token（一次十几个请求）再处理后续消息；
        网关不支持计数时每个请求要等到失败，首条消息因此空等几十秒到几分钟。
        CLI 按环境变量定窗口的模型（非 Claude 官方模型名）本地按同一规则算出；
        Claude 官方模型名按 CLI 内置窗口，本地不知道，等首轮结束再查。
        """
        from .. import model_settings

        env = _owner_env(self._ctx.model_owner)
        if model_settings.cli_uses_env_window(env.get("ANTHROPIC_MODEL", "")):
            self._context_window, self._compact_at = model_settings.cli_context_limits(env)
            self._limits_after_turn = False
        else:
            self._context_window = self._compact_at = None
            self._limits_after_turn = True

    def _load_limits_after_turn(self) -> None:
        if not self._limits_after_turn or self.client is None:
            return
        self._limits_after_turn = False
        self._limits_task = asyncio.create_task(self._load_context_limits(self.client))

    async def _load_context_limits(self, client: Any) -> None:
        """向 CLI 查窗口与触发点（Claude 官方模型名用），首轮结束后在后台取一次；旧版 CLI 不支持时只显示已用量。"""
        try:
            usage = await asyncio.wait_for(client.get_context_usage(), timeout=60)
        except Exception:
            # 旧版 CLI 不支持或超时：按取不到处理。CancelledError 是 BaseException，不会落到这里
            usage = None
        self._context_window, self._compact_at = context_limits(
            usage, _owner_env(self._ctx.model_owner)
        )

    def _cancel_limits_task(self) -> None:
        if self._limits_task is not None and not self._limits_task.done():
            self._limits_task.cancel()
        self._limits_task = None

    async def send(self, text: str, context: str | None = None) -> None:
        if context:
            from ..session_host import injection

            text = injection.compose_query(text, context)
        await self.client.query(text)

    async def interrupt(self) -> None:
        await self.client.interrupt()

    async def disconnect(self) -> None:
        self._cancel_limits_task()
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        except Exception:
            pass
        self.client = None

    async def kill(self) -> None:
        """硬终止：直接 SIGTERM 底层 CLI 进程。

        interrupt/disconnect 都要等 CLI 应答，CLI 悬死（如网关调用无响应）时
        它们会永久挂起——此路径不与 CLI 对话，杀进程了事（续接点保留）。
        """
        self._cancel_limits_task()
        client, self.client = self.client, None
        if client is None:
            return
        process = getattr(getattr(client, "_transport", None), "_process", None)
        try:
            if process is not None:
                process.terminate()
        except Exception:
            pass

    def model_fingerprint(self) -> str:
        return _env_fingerprint(self._ctx.model_owner)

    def config_stale(self) -> bool:
        return bool(self._fingerprint) and self._fingerprint != self.model_fingerprint()

    def context_limits(self) -> tuple[int | None, int | None]:
        return self._context_window, self._compact_at

    # ---- 事件流 ----

    async def events(self):
        async for event in self._events():
            self._notify(event)
            yield event

    async def _events(self):
        try:
            async for message in self.client.receive_messages():
                self._received_any = True
                token = getattr(message, "session_id", None)
                if not token and isinstance(message, SystemMessage):
                    token = (message.data or {}).get("session_id")
                if token and token != self._sdk_session_id:
                    self._sdk_session_id = token
                    self._ctx.save_resume_token(token)
                if self._pending_assistant is not None:
                    if isinstance(message, ResultMessage):
                        pending = self._pending_assistant
                        self._pending_assistant = None
                        if _result_failed(message):
                            failure_text = _result_error_text(message)
                            for block in pending["blocks"]:
                                if block.get("kind") != "text":
                                    continue
                                original = block["text"]
                                combined = original + "\n" + failure_text
                                source = (
                                    "assistant_text"
                                    if _looks_like_raw_error(original)
                                    else "result_error"
                                )
                                self._ctx.log_error(source, original)
                                block["text"] = (
                                    config_error_for(self._ctx.model_owner)
                                    if _looks_like_config_error(combined)
                                    else FRIENDLY_ERROR
                                )
                        yield pending
                    else:
                        yield self._pending_assistant
                        self._pending_assistant = None

                event = normalize_message(message)
                assistant_error = getattr(message, "error", None)
                usage_event = self._track_context(event) if event else None
                if event and event.get("type") == "assistant_message":
                    if assistant_error:
                        evidence = str(assistant_error)
                        for block in event["blocks"]:
                            if block.get("kind") == "text":
                                self._ctx.log_error("assistant_error", block["text"])
                                combined = block["text"] + "\n" + evidence
                                block["text"] = (
                                    config_error_for(self._ctx.model_owner)
                                    if _looks_like_config_error(combined)
                                    else FRIENDLY_ERROR
                                )
                        yield event
                        continue
                    if not any(b.get("kind") == "tool_use" for b in event["blocks"]):
                        self._pending_assistant = event
                        continue
                if event:
                    if usage_event:
                        yield usage_event
                    yield event
            if self._pending_assistant is not None:
                yield self._pending_assistant
                self._pending_assistant = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # resume 后一条消息都没收到：续接点大概率已失效，清掉避免反复撞墙
            if self.resume_attempted and not self._received_any:
                self._ctx.clear_resume_token()
            await self.disconnect()
            if self._pending_assistant is not None:
                pending = self._pending_assistant
                self._pending_assistant = None
                failure_text = str(exc)
                for block in pending["blocks"]:
                    if block.get("kind") != "text":
                        continue
                    self._ctx.log_error("session_error", block["text"])
                    combined = block["text"] + "\n" + failure_text
                    block["text"] = (
                        config_error_for(self._ctx.model_owner)
                        if _looks_like_config_error(combined)
                        else FRIENDLY_ERROR
                    )
                yield pending
            # SIGTERM（服务重启/会话终止）不是错误，不给用户留红色横幅
            if getattr(exc, "exit_code", None) == 143 or "exit code 143" in str(exc):
                yield {"type": "_engine_end", "reason": "closed"}
            else:
                self._ctx.log_error("session", str(exc))
                friendly = (
                    config_error_for(self._ctx.model_owner)
                    if _looks_like_config_error(str(exc))
                    else FRIENDLY_ERROR
                )
                yield {"type": "_engine_end", "reason": "error", "error": friendly}

    def _track_context(self, event: dict) -> dict | None:
        """跟踪上下文占用；本轮结束（result 之前）返回一条用量事件供前端显示剩余。"""
        kind = event.get("type")
        if kind == "assistant_message" and event.get("context_tokens"):
            self._context_used = event["context_tokens"]
        elif kind == "context_compacted":
            self._context_used = event.get("post_tokens")
        elif kind == "result":
            self._load_limits_after_turn()
            if self._context_used:
                return {
                    "type": "context_usage",
                    "used": self._context_used,
                    "window": self._context_window,
                    "compact_at": self._compact_at,
                }
        return None

    # ---- 权限回调（SDK 契约 → 中立决策 dict）----

    def _build_options(self, resume_id: str | None) -> Any:
        async def can_use_tool(tool_name: str, tool_input: dict, context) -> Any:
            decision = await self._ctx.request_permission(
                tool_name,
                tool_input,
                getattr(context, "title", None),
                getattr(context, "description", None),
            )
            if decision.get("behavior") == "allow":
                return PermissionResultAllow(
                    updated_input=decision.get("updated_input")
                )
            return PermissionResultDeny(
                message=decision.get("message") or "用户拒绝了该操作"
            )

        kwargs: dict[str, Any] = dict(
            cwd=str(self._ctx.cwd),
            system_prompt={"type": "preset", "preset": "claude_code"},
            # user 级设置承载部署机的模型网关配置（~/.claude/settings.json 的 env）
            setting_sources=["user", "project"],
            # bypassPermissions 下 can_use_tool 不会被触发；切回 default 即恢复确认卡
            permission_mode=_permission_mode(),
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            mcp_servers=_mcp_servers(self._ctx.project_id),
            env=_owner_env(self._ctx.model_owner),
        )
        if self._ctx.model_owner:
            # 部署机 ~/.claude/settings.json 的 env 会盖过上面的 env 参数（实测），
            # 个人 Key 若被它覆盖就会悄悄改用部署机的 Key；--settings 优先级更高。
            # 只覆盖网关 / Key / 模型，其余变量仍与管理员配置下的会话一致。
            # 管理员配置（model_owner 为空）不走这里，保持原行为
            gateway_env = {
                name: value
                for name, value in kwargs["env"].items()
                if name.startswith("ANTHROPIC_")
            }
            kwargs["settings"] = json.dumps({"env": gateway_env})
        if resume_id:
            kwargs["resume"] = resume_id
        if config.CLI_PATH:
            # 降级兼容包（无 AVX 环境）：指向外部 JS 版 CLI，见部署文档
            kwargs["cli_path"] = config.CLI_PATH
        return ClaudeAgentOptions(**kwargs)
