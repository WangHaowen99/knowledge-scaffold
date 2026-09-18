"""Agent 会话编排：每个项目一个 Claude Agent SDK 长驻会话。

- 消息流经 normalize 后进入事件历史 + 广播给所有 SSE 订阅者
- canUseTool 回调转换成"待确认卡"事件，等待前端答复（超时按拒绝处理）
- SDK 未安装时后端其余功能可用，会话接口返回明确错误
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config


def _history_path(project_id: str) -> Path:
    sessions_dir = Path(config.DATA_DIR) / "sessions"
    sessions_dir.mkdir(parents=True, exist_ok=True)
    return sessions_dir / f"{project_id}.jsonl"


def load_history(project_id: str) -> list[dict]:
    path = _history_path(project_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events

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

    def broadcast(self, event: dict, transient: bool = False) -> None:
        event.setdefault("ts", time.time())
        if transient:
            # 流式增量只发给在线订阅者，不进历史（避免回放时重放上千条 delta）
            for queue in list(self.subscribers):
                queue.put_nowait(event)
            return
        event["seq"] = len(self.events)
        self.events.append(event)
        try:
            with open(_history_path(self.project_id), "a", encoding="utf-8") as f:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError:
            pass  # 落盘失败不阻断会话
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

    def get_history(self, project_id: str) -> list[dict]:
        """活跃会话的事件，或磁盘上的历史（服务重启后仍可回放）。"""
        session = self._sessions.get(project_id)
        if session:
            return session.events
        return load_history(project_id)

    async def ensure_session(self, project_id: str, cwd: Path) -> Session:
        if not SDK_AVAILABLE:
            raise SessionError("claude-agent-sdk 未安装，无法启动建模会话", 503)
        existing = self._sessions.get(project_id)
        if existing and existing.state != "closed":
            return existing

        session = Session(id=uuid.uuid4().hex[:12], project_id=project_id, client=None)
        session.events = load_history(project_id)  # 续接历史，seq 连续

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
            # bypassPermissions 下 can_use_tool 不会被触发；切回 default 即恢复确认卡
            permission_mode=config.PERMISSION_MODE,
            can_use_tool=can_use_tool,
            include_partial_messages=True,
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
        self,
        project_id: str,
        request_id: str,
        behavior: str,
        message: str = "",
        answers: dict | None = None,
    ) -> None:
        session = self._sessions.get(project_id)
        if not session:
            raise SessionError("会话不存在", 404)
        request = session.pending_permissions.get(request_id)
        if not request or request.future.done():
            raise SessionError("确认请求不存在或已处理", 404)
        if behavior == "allow":
            updated_input = None
            if answers:
                # AskUserQuestion：答案经 updated_input.answers 回填（问题文本 → 选项文本）
                updated_input = {**request.tool_input, "answers": answers}
            request.future.set_result(PermissionResultAllow(updated_input=updated_input))
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
                    session.broadcast(
                        event, transient=event.get("type") == "stream_delta"
                    )
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
                # display: omitted 时 thinking 正文是空串，不产出空卡片
                if (block.thinking or "").strip():
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
            "usage": _safe_json(getattr(message, "usage", None)),
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


def _safe_json(value: Any) -> Any:
    try:
        import json

        json.dumps(value)
        return value
    except (TypeError, ValueError):
        return str(value)


manager = SessionManager()
