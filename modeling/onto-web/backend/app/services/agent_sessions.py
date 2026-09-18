"""Agent 会话编排：每个项目一个 Claude Agent SDK 长驻会话。

- 消息流经 normalize 后进入事件历史 + 广播给所有 SSE 订阅者
- canUseTool 回调转换成"待确认卡"事件，等待前端答复（超时按拒绝处理）
- SDK 未安装时后端其余功能可用，会话接口返回明确错误
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config

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


class SessionError(Exception):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class PendingPermission:
    id: str
    tool_name: str
    tool_input: dict
    title: str | None
    description: str | None
    future: asyncio.Future = field(repr=False, default=None)


@dataclass
class Session:
    id: str
    project_id: str
    client: Any
    state: str = "idle"  # idle | running | error | closed
    events: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    pending_permissions: dict[str, PendingPermission] = field(default_factory=dict)
    reader_task: asyncio.Task | None = None

    def broadcast(self, event: dict) -> None:
        event.setdefault("ts", time.time())
        event["seq"] = len(self.events)
        self.events.append(event)
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def set_state(self, state: str) -> None:
        if self.state != state:
            self.state = state
            self.broadcast({"type": "session_state", "state": state})


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}  # project_id -> session

    def get(self, project_id: str) -> Session | None:
        return self._sessions.get(project_id)

    async def ensure_session(self, project_id: str, cwd: Path) -> Session:
        if not SDK_AVAILABLE:
            raise SessionError("claude-agent-sdk 未安装，无法启动建模会话", 503)
        existing = self._sessions.get(project_id)
        if existing and existing.state != "closed":
            return existing

        session = Session(id=uuid.uuid4().hex[:12], project_id=project_id, client=None)

        async def can_use_tool(tool_name: str, tool_input: dict, context) -> Any:
            request = PendingPermission(
                id=uuid.uuid4().hex[:12],
                tool_name=tool_name,
                tool_input=tool_input,
                title=getattr(context, "title", None),
                description=getattr(context, "description", None),
                future=asyncio.get_running_loop().create_future(),
            )
            session.pending_permissions[request.id] = request
            session.broadcast(
                {
                    "type": "permission_request",
                    "request_id": request.id,
                    "tool_name": tool_name,
                    "tool_input": _safe_json(tool_input),
                    "title": request.title,
                    "description": request.description,
                }
            )
            try:
                result = await asyncio.wait_for(
                    request.future, timeout=config.PERMISSION_TIMEOUT_SECONDS
                )
            except asyncio.TimeoutError:
                result = PermissionResultDeny(message="等待确认超时，已自动拒绝")
            finally:
                session.pending_permissions.pop(request.id, None)
            session.broadcast(
                {
                    "type": "permission_resolved",
                    "request_id": request.id,
                    "behavior": result.behavior,
                }
            )
            return result

        options = ClaudeAgentOptions(
            cwd=str(cwd),
            system_prompt={"type": "preset", "preset": "claude_code"},
            # user 级设置承载部署机的模型网关配置（~/.claude/settings.json 的 env）
            setting_sources=["user", "project"],
            permission_mode="default",
            can_use_tool=can_use_tool,
        )
        client = ClaudeSDKClient(options=options)
        await client.connect()
        session.client = client
        session.reader_task = asyncio.create_task(self._read_loop(session))
        self._sessions[project_id] = session
        session.broadcast({"type": "session_started", "session_id": session.id})
        return session

    async def send(self, project_id: str, cwd: Path, text: str) -> Session:
        text = (text or "").strip()
        if not text:
            raise SessionError("消息不能为空")
        session = await self.ensure_session(project_id, cwd)
        session.broadcast({"type": "user_message", "text": text})
        await session.client.query(text)
        session.set_state("running")
        return session

    async def respond_permission(
        self, project_id: str, request_id: str, behavior: str, message: str = ""
    ) -> None:
        session = self._sessions.get(project_id)
        if not session:
            raise SessionError("会话不存在", 404)
        request = session.pending_permissions.get(request_id)
        if not request or request.future.done():
            raise SessionError("确认请求不存在或已处理", 404)
        if behavior == "allow":
            request.future.set_result(PermissionResultAllow())
        else:
            request.future.set_result(
                PermissionResultDeny(message=message or "用户拒绝了该操作")
            )

    async def interrupt(self, project_id: str) -> None:
        session = self._sessions.get(project_id)
        if not session:
            raise SessionError("会话不存在", 404)
        await session.client.interrupt()

    async def close(self, project_id: str) -> None:
        session = self._sessions.pop(project_id, None)
        if not session:
            return
        session.set_state("closed")
        if session.reader_task:
            session.reader_task.cancel()
        try:
            await session.client.disconnect()
        except Exception:
            pass

    async def _read_loop(self, session: Session) -> None:
        try:
            async for message in session.client.receive_messages():
                event = normalize_message(message)
                if event:
                    session.broadcast(event)
                if event and event.get("type") == "result":
                    session.set_state("idle")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # 会话崩溃要让前端可见，而不是静默断流
            session.broadcast({"type": "session_error", "error": str(exc)})
            session.set_state("error")


def normalize_message(message: Any) -> dict | None:
    """把 SDK 消息 dataclass 转成前端可渲染的扁平事件。"""
    if isinstance(message, AssistantMessage):
        blocks = []
        for block in message.content:
            if isinstance(block, TextBlock):
                blocks.append({"kind": "text", "text": block.text})
            elif isinstance(block, ThinkingBlock):
                blocks.append({"kind": "thinking", "text": block.thinking})
            elif isinstance(block, ToolUseBlock):
                blocks.append(
                    {
                        "kind": "tool_use",
                        "tool": block.name,
                        "input": _safe_json(block.input),
                        "tool_use_id": block.id,
                    }
                )
        return {"type": "assistant_message", "blocks": blocks, "model": message.model}
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
        }
    if isinstance(message, StreamEvent):
        return None  # 首片未开启 partial 流
    return None


def _safe_json(value: Any) -> Any:
    try:
        import json

        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


manager = SessionManager()
