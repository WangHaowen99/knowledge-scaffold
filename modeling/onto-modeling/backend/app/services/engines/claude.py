"""Claude Agent SDK 引擎：每会话一个捆绑 CLI 子进程。

从 agent_sessions.py 平移（Phase 1 引擎抽象，行为零变化）：
- 选项构建（权限回调、MCP、环境变量、resume、降级 CLI 路径）
- SDK 消息 → 平台事件归一化
- CLI 原始错误翻译（原文留档，聊天流只出中文提示）
- resume 续接：捕获 SDK session id 经 ctx 落盘；连接失败自动清点重试
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from ... import config
from .base import CONFIG_ERROR, FRIENDLY_ERROR, EngineContext, safe_json

_RAW_ERROR_PATTERN = None  # 延迟编译
_CONFIG_ERROR_PATTERN = None


def _looks_like_config_error(text: str) -> bool:
    """未登录/凭据无效类错误：给专属中文指引而不是笼统的"服务不稳定"。"""
    global _CONFIG_ERROR_PATTERN
    if _CONFIG_ERROR_PATTERN is None:
        import re

        _CONFIG_ERROR_PATTERN = re.compile(
            r"Not logged in|/login|Invalid API key|authentication_error|"
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


def _env_fingerprint() -> str:
    import hashlib

    return hashlib.sha256(
        json.dumps(_session_env(), sort_keys=True).encode("utf-8")
    ).hexdigest()[:16]


def _session_env() -> dict:
    env = {}
    if config.MAX_THINKING_TOKENS:
        env["MAX_THINKING_TOKENS"] = config.MAX_THINKING_TOKENS
    try:
        from .. import model_settings

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
        return {
            "type": "assistant_message",
            "blocks": blocks,
            "model": message.model,
            "subagent": bool(getattr(message, "parent_tool_use_id", None)),
        }
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
    if isinstance(message, SystemMessage):
        return {"type": "system", "subtype": message.subtype}
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
        # 只转发主 agent 的文本/思考增量；子代理的过程以工具行形式呈现，不混进主气泡
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


class ClaudeEngine:
    """Claude Agent SDK 引擎实现（AgentEngine 协议）。"""

    name = "claude"

    def __init__(self, ctx: EngineContext):
        self._ctx = ctx
        self.client: Any = None
        self.resume_attempted = False
        self._received_any = False
        self._sdk_session_id: str | None = None
        self._fingerprint = ""

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
        self._fingerprint = _env_fingerprint()
        return bool(resume_id)

    async def send(self, text: str) -> None:
        await self.client.query(text)

    async def interrupt(self) -> None:
        await self.client.interrupt()

    async def disconnect(self) -> None:
        if self.client is None:
            return
        try:
            await self.client.disconnect()
        except Exception:
            pass
        self.client = None

    def config_stale(self) -> bool:
        return bool(self._fingerprint) and self._fingerprint != _env_fingerprint()

    # ---- 事件流 ----

    async def events(self):
        try:
            async for message in self.client.receive_messages():
                self._received_any = True
                token = getattr(message, "session_id", None)
                if not token and isinstance(message, SystemMessage):
                    token = (message.data or {}).get("session_id")
                if token and token != self._sdk_session_id:
                    self._sdk_session_id = token
                    self._ctx.save_resume_token(token)
                event = normalize_message(message)
                if event and event.get("type") == "assistant_message":
                    # CLI 把 API 错误当正文回流时，替换为友好提示并留档原文
                    for block in event["blocks"]:
                        if block.get("kind") == "text" and _looks_like_raw_error(
                            block["text"]
                        ):
                            self._ctx.log_error("assistant_text", block["text"])
                            block["text"] = (
                                CONFIG_ERROR
                                if _looks_like_config_error(block["text"])
                                else FRIENDLY_ERROR
                            )
                if event:
                    yield event
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # resume 后一条消息都没收到：续接点大概率已失效，清掉避免反复撞墙
            if self.resume_attempted and not self._received_any:
                self._ctx.clear_resume_token()
            await self.disconnect()
            # SIGTERM（服务重启/会话终止）不是错误，不给用户留红色横幅
            if getattr(exc, "exit_code", None) == 143 or "exit code 143" in str(exc):
                yield {"type": "_engine_end", "reason": "closed"}
            else:
                self._ctx.log_error("session", str(exc))
                friendly = (
                    CONFIG_ERROR if _looks_like_config_error(str(exc)) else FRIENDLY_ERROR
                )
                yield {"type": "_engine_end", "reason": "error", "error": friendly}

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
            env=_session_env(),
        )
        if resume_id:
            kwargs["resume"] = resume_id
        if config.CLI_PATH:
            # 降级兼容包（无 AVX 环境）：指向外部 JS 版 CLI，见部署文档
            kwargs["cli_path"] = config.CLI_PATH
        return ClaudeAgentOptions(**kwargs)
