"""ACP 数据模型定义

基于 ACP (Agent Communication Protocol) 规范定义核心数据结构。
参考: https://github.com/i-am-bee/ACP
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class MessageRole(str, Enum):
    USER = "user"
    AGENT = "agent"


class MessagePartType(str, Enum):
    TEXT = "text"
    DATA = "data"
    FILE = "file"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"


class RunStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    AWAITING = "awaiting"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class MessagePart(BaseModel):
    type: MessagePartType = Field(..., description="内容类型")
    content: str = Field(..., description="内容")
    content_type: str | None = Field(None, description="MIME类型")
    encoding: str | None = Field(None, description="编码方式")
    metadata: dict[str, Any] | None = Field(default_factory=dict, description="元数据")


class Message(BaseModel):
    role: MessageRole = Field(..., description="消息角色")
    parts: list[MessagePart] = Field(default_factory=list, description="消息部分列表")
    created_at: datetime | None = Field(default_factory=lambda: datetime.now(timezone.utc))
    metadata: dict[str, Any] | None = Field(default_factory=dict, description="元数据")


class AgentCapabilities(BaseModel):
    supported_content_types: list[str] = Field(
        default_factory=lambda: ["text/plain", "application/json"],
    )
    supported_modes: list[str] = Field(
        default_factory=lambda: ["sync", "async", "stream"],
    )
    features: list[str] = Field(
        default_factory=lambda: [
            "ontology_query",
            "entity_search",
            "relation_search",
            "multi_turn_conversation",
        ],
    )


class AgentDetail(BaseModel):
    name: str = Field(default="Onto-Agent", description="Agent名称")
    version: str = Field(default="1.0.0", description="版本号")
    description: str = Field(
        default="基于 AgentLoopEngine 的本体查询智能体，支持多轮会话、流式推理展示、技能热加载与管理",
        description="Agent描述",
    )
    capabilities: AgentCapabilities = Field(default_factory=AgentCapabilities)
    authentication: dict[str, Any] | None = Field(
        default_factory=lambda: {"type": "none"},
    )
    endpoints: dict[str, str] | None = Field(
        default_factory=lambda: {
            "agent": "/acp/agent",
            "runs": "/acp/runs",
            "health": "/health",
        },
    )
    metadata: dict[str, Any] | None = Field(default_factory=dict)


class RunRequest(BaseModel):
    messages: list[Message] = Field(..., description="消息列表")
    context: dict[str, Any] | None = Field(default_factory=dict, description="上下文信息")
    arguments: dict[str, Any] | None = Field(default_factory=dict, description="可选结构化参数")
    requested_operation_key: str | None = Field(None, description="可选确定性 Operation key")
    metadata: dict[str, Any] | None = Field(default_factory=dict, description="元数据")
    lang: str | None = Field(None, description="用户语言类型(ISO 639-1)，如en/zh/ja/ko等。不传则自动检测")


class AwaitRequest(BaseModel):
    run_id: str = Field(..., description="运行ID")
    await_id: str = Field(..., description="等待ID")
    response: Message = Field(..., description="用户响应消息")


class AwaitResponse(BaseModel):
    await_id: str = Field(..., description="等待ID")
    message: Message = Field(..., description="请求消息")
    options: list[dict[str, Any]] | None = Field(None, description="选项列表")
    timeout: int | None = Field(None, description="超时时间（秒）")


class RunResponse(BaseModel):
    run_id: str = Field(..., description="运行ID")
    status: RunStatus = Field(..., description="运行状态")
    messages: list[Message] = Field(default_factory=list, description="消息列表")
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    awaiting: AwaitResponse | None = Field(None, description="等待用户输入")
    error: str | None = Field(None, description="错误信息")
    error_code: str | None = Field(None, description="稳定错误码")
    result: Any | None = Field(None, description="结构化终态结果")
    metadata: dict[str, Any] | None = Field(default_factory=dict, description="元数据")


class RunState(BaseModel):
    run_id: str = Field(..., description="运行ID")
    status: RunStatus = Field(default=RunStatus.PENDING)
    messages: list[Message] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    awaiting: AwaitResponse | None = Field(None)
    error: str | None = Field(None)
    lang: str | None = Field(None)
    metadata: dict[str, Any] | None = Field(default_factory=dict)

    model_config = {"arbitrary_types_allowed": True}


class StreamEvent(BaseModel):
    event_type: str = Field(..., description="事件类型")
    run_id: str = Field(..., description="运行ID")
    data: dict[str, Any] = Field(default_factory=dict, description="事件数据")
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
