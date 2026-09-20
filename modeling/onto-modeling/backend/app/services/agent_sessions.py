"""Agent 会话编排：会话池 + 空闲回收 + resume 续接（方案 4.1.1）。

- 每个活跃会话 = 一个 Claude Agent SDK 子进程（几百 MB 量级）。池上限
  ONTO_WEB_MAX_SESSIONS；超限先驱逐最久空闲的会话，全部在跑则排队并广播位次
- 空闲超过 ONTO_WEB_SESSION_IDLE_TTL 的会话被后台回收；事件历史落盘可回放，
  模型上下文靠 resume（CLI transcript 持久化在部署机 ~/.claude/projects/）恢复
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
from . import error_log

# 面向用户的通用错误提示；原始错误只进运维留档，不出现在聊天流里
FRIENDLY_ERROR = "模型服务暂时不稳定，本次调用未完成。系统已记录详细信息，请重试；若持续出现请联系管理员。"
_RAW_ERROR_PATTERN = None  # 延迟编译


def _looks_like_raw_error(text: str) -> bool:
    global _RAW_ERROR_PATTERN
    if _RAW_ERROR_PATTERN is None:
        import re

        _RAW_ERROR_PATTERN = re.compile(
            r"API Error|Claude Code|request id:|exit code|invalid_grant|"
            r"OAuth|Traceback \(most recent call last\)",
            re.IGNORECASE,
        )
    return bool(_RAW_ERROR_PATTERN.search(text or ""))


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


def _meta_path(project_id: str) -> Path:
    return _history_path(project_id).with_suffix(".meta.json")


def load_resume_id(project_id: str) -> str | None:
    """上次会话的 SDK session id；重连时用 resume 恢复模型上下文。"""
    path = _meta_path(project_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8")).get("sdk_session_id") or None
    except (OSError, json.JSONDecodeError):
        return None


def save_resume_id(project_id: str, session_id: str) -> None:
    try:
        _meta_path(project_id).write_text(
            json.dumps({"sdk_session_id": session_id}), encoding="utf-8"
        )
    except OSError:
        pass  # 落盘失败只影响下次续接，不阻断会话


def clear_resume_id(project_id: str) -> None:
    try:
        _meta_path(project_id).unlink(missing_ok=True)
    except OSError:
        pass


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
    state: str = "idle"  # idle | queued | running | error | closed
    cwd: Path | None = None
    events: list[dict] = field(default_factory=list)
    subscribers: list[asyncio.Queue] = field(default_factory=list)
    pending_permissions: dict[str, PendingPermission] = field(default_factory=dict)
    pending_prompts: list[str] = field(default_factory=list)
    reader_task: asyncio.Task | None = None
    connect_task: asyncio.Task | None = None
    sdk_session_id: str | None = None
    resume_attempted: bool = False
    received_any: bool = False
    last_activity: float = field(default_factory=time.time)
    round_base_commit: str = ""  # 本轮起点 HEAD，用于 result 后产出"产物变更"事件

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

    def is_connecting(self) -> bool:
        return self.connect_task is not None and not self.connect_task.done()


def _expire_orphan_permissions(session: "Session") -> None:
    """历史中未决的确认请求（重启/回收后 future 已丢）补失效事件。

    否则前端会永远渲染"等待选择"的幽灵卡，用户回答只能得到 404。
    """
    resolved = {
        e.get("request_id")
        for e in session.events
        if e.get("type") == "permission_resolved"
    }
    orphans = [
        e.get("request_id")
        for e in session.events
        if e.get("type") == "permission_request" and e.get("request_id") not in resolved
    ]
    for request_id in orphans:
        session.broadcast(
            {
                "type": "permission_resolved",
                "request_id": request_id,
                "behavior": "expired",
            }
        )


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}  # project_id -> session
        self._waiters: list[str] = []  # 等待会话槽位的项目（FIFO）
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None

    # ---- 查询 ----

    def get(self, project_id: str) -> Session | None:
        return self._sessions.get(project_id)

    def get_history(self, project_id: str) -> list[dict]:
        """活跃会话的事件，或磁盘上的历史（服务重启后仍可回放）。"""
        session = self._sessions.get(project_id)
        if session:
            return session.events
        return load_history(project_id)

    def active_count(self) -> int:
        """占用槽位的会话数：子进程已连接的 + 正在连接的。"""
        return sum(
            1
            for s in self._sessions.values()
            if s.client is not None or s.is_connecting()
        )

    def queue_position(self, project_id: str) -> int | None:
        try:
            return self._waiters.index(project_id) + 1
        except ValueError:
            return None

    def pool_status(self) -> dict:
        return {
            "max": config.MAX_ACTIVE_SESSIONS,
            "active": self.active_count(),
            "waiting": list(self._waiters),
            "idle_ttl_seconds": config.SESSION_IDLE_TTL_SECONDS,
        }

    # ---- 发送与槽位调度 ----

    async def send(self, project_id: str, cwd: Path, text: str) -> Session:
        text = (text or "").strip()
        if not text:
            raise SessionError("消息不能为空")
        if not SDK_AVAILABLE:
            raise SessionError("claude-agent-sdk 未安装，无法启动建模会话", 503)
        session = self._sessions.get(project_id)
        if session is None or session.state == "closed":
            session = Session(
                id=uuid.uuid4().hex[:12], project_id=project_id, client=None
            )
            session.events = load_history(project_id)  # 续接历史，seq 连续
            self._sessions[project_id] = session
            _expire_orphan_permissions(session)
        session.cwd = cwd
        session.last_activity = time.time()
        if session.state != "running":
            session.round_base_commit = await asyncio.to_thread(_git_head, cwd)
        session.broadcast({"type": "user_message", "text": text})
        if session.client is not None:
            await session.client.query(text)
            session.set_state("running")
            return session
        session.pending_prompts.append(text)
        await self._request_slot(session)
        return session

    async def _request_slot(self, session: Session) -> None:
        """为无子进程的会话申请槽位：够用直接连，可驱逐则驱逐，否则排队。"""
        async with self._lock:
            if session.is_connecting():
                return
            if self.active_count() < config.MAX_ACTIVE_SESSIONS:
                self._start_connect(session)
                return
            victim = self._pick_idle_victim()
            if victim is not None:
                await self._recycle(victim)
                self._start_connect(session)
                return
            if session.project_id not in self._waiters:
                self._waiters.append(session.project_id)
            session.set_state("queued")
            self._broadcast_queue_positions()

    def _pick_idle_victim(self) -> Session | None:
        candidates = [
            s
            for s in self._sessions.values()
            if s.client is not None
            and s.state == "idle"
            and not s.pending_permissions
            and not s.pending_prompts
        ]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.last_activity)

    def _start_connect(self, session: Session) -> None:
        session.connect_task = asyncio.create_task(self._connect(session))

    async def _connect(self, session: Session) -> None:
        project_id = session.project_id
        try:
            _mark_workspace_trusted(session.cwd)
            resume_id = load_resume_id(project_id)
            client = self._create_client(self._build_options(session, resume_id))
            try:
                await client.connect()
            except Exception:
                if not resume_id:
                    raise
                # resume 失败（transcript 缺失/损坏）：清掉续接点，全新会话重试
                clear_resume_id(project_id)
                resume_id = None
                client = self._create_client(self._build_options(session, None))
                await client.connect()
            session.resume_attempted = bool(resume_id)
            session.received_any = False
            session.client = client
            session.reader_task = asyncio.create_task(self._read_loop(session))
            session.broadcast(
                {
                    "type": "session_started",
                    "session_id": session.id,
                    "resumed": bool(resume_id),
                }
            )
            prompts, session.pending_prompts = session.pending_prompts, []
            if prompts and session.cwd:
                session.round_base_commit = await asyncio.to_thread(
                    _git_head, session.cwd
                )
            for prompt in prompts:
                await client.query(prompt)
            session.set_state("running" if prompts else "idle")
        except Exception as exc:
            error_log.log(project_id, "session_connect", str(exc))
            session.client = None
            session.broadcast({"type": "session_error", "error": FRIENDLY_ERROR})
            session.set_state("error")
        finally:
            session.connect_task = None
            await self._drain_waiters()

    async def _drain_waiters(self) -> None:
        """有槽位空出时按排队顺序续接等待中的会话。"""
        async with self._lock:
            while self._waiters and self.active_count() < config.MAX_ACTIVE_SESSIONS:
                project_id = self._waiters.pop(0)
                session = self._sessions.get(project_id)
                if (
                    session is None
                    or session.client is not None
                    or session.is_connecting()
                ):
                    continue
                self._start_connect(session)
            self._broadcast_queue_positions()

    def _broadcast_queue_positions(self) -> None:
        total = len(self._waiters)
        for index, project_id in enumerate(self._waiters):
            session = self._sessions.get(project_id)
            if session:
                session.broadcast(
                    {"type": "queue_position", "position": index + 1, "waiting": total},
                    transient=True,
                )

    # ---- 回收与关闭 ----

    async def _recycle(self, session: Session) -> None:
        """回收子进程但保留续接点：resume id 已随消息流落盘，下次消息重连恢复上下文。"""
        self._sessions.pop(session.project_id, None)
        session.set_state("closed")
        if session.reader_task:
            session.reader_task.cancel()
        if session.client is not None:
            try:
                await session.client.disconnect()
            except Exception:
                pass
            session.client = None

    async def recycle_project(self, project_id: str) -> bool:
        """运维手动回收：断开子进程但保留续接点（与空闲回收同语义）。"""
        async with self._lock:
            session = self._sessions.get(project_id)
            if session is None:
                return False
            await self._recycle(session)
        await self._drain_waiters()
        return True

    async def reap_idle(self) -> int:
        """回收空闲超时的会话，返回回收数量。"""
        ttl = config.SESSION_IDLE_TTL_SECONDS
        if ttl <= 0:
            return 0
        now = time.time()
        async with self._lock:
            victims = [
                s
                for s in self._sessions.values()
                if s.client is not None
                and s.state == "idle"
                and not s.pending_permissions
                and not s.pending_prompts
                and now - s.last_activity > ttl
            ]
            for victim in victims:
                await self._recycle(victim)
        if victims:
            await self._drain_waiters()
        return len(victims)

    async def _reaper_loop(self) -> None:
        while True:
            await asyncio.sleep(config.SESSION_REAPER_INTERVAL)
            try:
                await self.reap_idle()
            except Exception:  # 回收失败不终止后台任务
                pass

    def start_background(self) -> None:
        if self._reaper_task is None or self._reaper_task.done():
            self._reaper_task = asyncio.create_task(self._reaper_loop())

    async def shutdown(self) -> None:
        """服务停机：停后台回收并断开全部子进程（resume 点已落盘）。"""
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        self._waiters.clear()
        for session in list(self._sessions.values()):
            async with self._lock:
                await self._recycle(session)

    async def close(self, project_id: str) -> None:
        """终止并丢弃当前会话（历史保留，续接点清除）；下次发消息全新开始。"""
        async with self._lock:
            if project_id in self._waiters:
                self._waiters.remove(project_id)
            session = self._sessions.get(project_id)
            if session is None:
                clear_resume_id(project_id)
                return
            session.pending_prompts.clear()
            await self._recycle(session)
        clear_resume_id(project_id)
        await self._drain_waiters()

    # ---- 会话内操作 ----

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
            # 服务重启后旧确认卡的点击：会话与待决确认都已不在内存
            raise SessionError("该确认已失效（会话已重启）", 410)
        request = session.pending_permissions.get(request_id)
        if not request or request.future.done():
            raise SessionError("确认请求不存在或已处理", 410)
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
        if session.client is None:
            # 排队/连接中：清空积压消息并退出队列（排队的取消出口）
            async with self._lock:
                if project_id in self._waiters:
                    self._waiters.remove(project_id)
                session.pending_prompts.clear()
                self._broadcast_queue_positions()
            if not session.is_connecting():
                session.set_state("idle")
            return
        await session.client.interrupt()
        session.broadcast({"type": "session_interrupted"})

    # ---- SDK 对接 ----

    def _create_client(self, options: Any) -> Any:
        """测试替换点：单测注入假客户端，避免拉起真实 CLI 子进程。"""
        return ClaudeSDKClient(options=options)

    def _build_options(self, session: Session, resume_id: str | None) -> Any:
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

        kwargs: dict[str, Any] = dict(
            cwd=str(session.cwd),
            system_prompt={"type": "preset", "preset": "claude_code"},
            # user 级设置承载部署机的模型网关配置（~/.claude/settings.json 的 env）
            setting_sources=["user", "project"],
            # bypassPermissions 下 can_use_tool 不会被触发；切回 default 即恢复确认卡
            permission_mode=_permission_mode(),
            can_use_tool=can_use_tool,
            include_partial_messages=True,
            mcp_servers=_mcp_servers(session.project_id),
            env=_session_env(),
        )
        if resume_id:
            kwargs["resume"] = resume_id
        if config.CLI_PATH:
            # 降级兼容包（无 AVX 环境）：指向外部 JS 版 CLI，见部署文档
            kwargs["cli_path"] = config.CLI_PATH
        return ClaudeAgentOptions(**kwargs)

    async def _emit_workspace_changes(self, session: Session) -> None:
        """一轮结束后广播本轮产物变更（对话内"产物变更卡"的数据源）。"""
        if not session.cwd or not session.round_base_commit:
            return
        try:
            head = await asyncio.to_thread(_git_head, session.cwd)
            if not head or head == session.round_base_commit:
                return
            changes = await asyncio.to_thread(
                _git_changes, session.cwd, session.round_base_commit, head
            )
            if any(changes.values()):
                session.broadcast(
                    {
                        "type": "workspace_changes",
                        **changes,
                        "from": session.round_base_commit,
                        "to": head,
                    }
                )
            session.round_base_commit = head
        except Exception:
            pass  # 变更卡是增强信息，失败不影响会话

    async def _read_loop(self, session: Session) -> None:
        try:
            async for message in session.client.receive_messages():
                session.received_any = True
                session.last_activity = time.time()
                sdk_session_id = getattr(message, "session_id", None)
                if not sdk_session_id and isinstance(message, SystemMessage):
                    sdk_session_id = (message.data or {}).get("session_id")
                if sdk_session_id and sdk_session_id != session.sdk_session_id:
                    session.sdk_session_id = sdk_session_id
                    save_resume_id(session.project_id, sdk_session_id)
                event = normalize_message(message)
                if event and event.get("type") == "assistant_message":
                    # CLI 把 API 错误当正文回流时，替换为友好提示并留档原文
                    for block in event["blocks"]:
                        if block.get("kind") == "text" and _looks_like_raw_error(
                            block["text"]
                        ):
                            error_log.log(
                                session.project_id, "assistant_text", block["text"]
                            )
                            block["text"] = FRIENDLY_ERROR
                if event:
                    session.broadcast(
                        event, transient=event.get("type") == "stream_delta"
                    )
                if event and event.get("type") == "result":
                    await self._emit_workspace_changes(session)
                    session.set_state("idle")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # resume 后一条消息都没收到：续接点大概率已失效，清掉避免反复撞墙
            if session.resume_attempted and not session.received_any:
                clear_resume_id(session.project_id)
            # SIGTERM（服务重启/会话终止）不是错误，不给用户留红色横幅
            if getattr(exc, "exit_code", None) == 143 or "exit code 143" in str(exc):
                session.set_state("closed")
            else:
                error_log.log(session.project_id, "session", str(exc))
                session.broadcast({"type": "session_error", "error": FRIENDLY_ERROR})
                session.set_state("error")
            # 子进程已死：释放槽位并唤醒排队者
            if session.client is not None:
                try:
                    await session.client.disconnect()
                except Exception:
                    pass
                session.client = None
            await self._drain_waiters()


def usage_summary(project_id: str) -> dict:
    """项目累计模型用量（从事件历史统计），会话接口与运维页共用。"""
    history = manager.get_history(project_id)
    total_cost = 0.0
    output_tokens = 0
    input_tokens = 0
    calls = 0
    last_activity = None
    for event in history:
        if event.get("ts"):
            last_activity = event["ts"]
        if event.get("type") != "result":
            continue
        calls += 1
        total_cost += event.get("total_cost_usd") or 0
        usage = event.get("usage") or {}
        output_tokens += usage.get("output_tokens") or 0
        input_tokens += (usage.get("input_tokens") or 0) + (
            usage.get("cache_read_input_tokens") or 0
        )
    return {
        "calls": calls,
        "total_cost_usd": round(total_cost, 4),
        "output_tokens": output_tokens,
        "input_tokens": input_tokens,
        "last_activity": last_activity,
    }


def _mcp_servers(project_id: str = "") -> dict:
    from . import mcp_settings

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
        from . import runtime_settings

        return runtime_settings.permission_mode()
    except Exception:
        return config.PERMISSION_MODE


def _session_env() -> dict:
    env = {}
    if config.MAX_THINKING_TOKENS:
        env["MAX_THINKING_TOKENS"] = config.MAX_THINKING_TOKENS
    try:
        from . import model_settings

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


def _git_head(cwd: Path) -> str:
    import subprocess

    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=cwd, capture_output=True, text=True, timeout=10,
        )
        return result.stdout.strip() if result.returncode == 0 else ""
    except Exception:
        return ""


def _git_changes(cwd: Path, base: str, head: str, cap: int = 50) -> dict:
    import subprocess

    changes = {"added": [], "modified": [], "deleted": []}
    # core.quotepath=false：中文路径按 UTF-8 原样输出，不做 C 风格八进制转义
    result = subprocess.run(
        ["git", "-c", "core.quotepath=false", "diff", "--name-status", f"{base}..{head}"],
        cwd=cwd, capture_output=True, text=True, timeout=15,
    )
    if result.returncode != 0:
        return changes
    for line in result.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status, path = parts[0][:1], parts[-1].strip().strip('"')
        bucket = {"A": "added", "M": "modified", "D": "deleted", "R": "modified"}.get(status)
        if bucket and len(changes[bucket]) < cap:
            changes[bucket].append(path)
    return changes


manager = SessionManager()
