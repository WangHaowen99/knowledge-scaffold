"""Agent 会话编排：会话池 + 空闲回收 + resume 续接（方案 4.1.1）。

引擎无关的编排层（引擎抽象见 engines/，设计 docs/openjiuwen-engine-migration.md §3.1）：
- 每个活跃会话 = 一个引擎实例（Claude 引擎为 CLI 子进程，几百 MB 量级）。池上限
  ONTO_WEB_MAX_SESSIONS；超限先驱逐最久空闲的会话，全部在跑则排队并广播位次
- 空闲超过 ONTO_WEB_SESSION_IDLE_TTL 的会话被后台回收；事件历史落盘可回放，
  模型上下文靠引擎的 resume token 恢复
- 引擎产出归一化平台事件，进入事件历史 + 广播给所有 SSE 订阅者
- 引擎的权限回调转换成"待确认卡"事件，等待前端答复（超时按拒绝处理）
- 引擎不可用时（如 SDK 未安装）后端其余功能可用，会话接口返回明确错误
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
from .engines import EngineContext, get_engine_class
from .engines.base import CONFIG_ERROR, FRIENDLY_ERROR, safe_json as _safe_json

# 兼容重导出：既有测试与调用方从本模块取这些符号（引擎实现已移至 engines/claude.py）
from .engines.claude import (  # noqa: F401
    SDK_AVAILABLE,
    _looks_like_config_error,
    _looks_like_raw_error,
    _tool_result_excerpt,
    normalize_message,
)


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


def _reconcile_stale_history(project_id: str) -> list[dict] | None:
    """会话进程被重启/强杀后，事件日志尾部可能停在 running 等非终态，
    前端回放即成"幽灵运行态"（永远执行中、打断 404 无效）。无活跃会话时
    补写中断标记与 idle 终态使日志自愈；返回补写后的事件，未补写返回 None。"""
    events = load_history(project_id)
    state = None
    for event in reversed(events):
        if event.get("type") == "session_state":
            state = event.get("state")
            break
    if state not in ("running", "queued", "connecting"):
        return None
    now = time.time()
    patch = [
        {
            "type": "session_interrupted",
            "reason": "stale",
            "message": "会话进程已重启，上一轮未正常结束，已标记为中断",
            "ts": now,
            "seq": len(events),
        },
        {"type": "session_state", "state": "idle", "ts": now, "seq": len(events) + 1},
    ]
    try:
        with open(_history_path(project_id), "a", encoding="utf-8") as f:
            for event in patch:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return events + patch


def _meta_path(project_id: str) -> Path:
    return _history_path(project_id).with_suffix(".meta.json")


def load_resume_id(project_id: str) -> str | None:
    """上次会话的引擎续接 token；重连时恢复模型上下文。"""
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


# 打断的应答超时：引擎/CLI 悬死（如网关调用无响应）时优雅打断永远等不到应答，
# 超时即硬终止引擎（续接点保留，下一条消息 resume）——保证"停止"按钮总能停
INTERRUPT_TIMEOUT_SECONDS = 10
KILL_TIMEOUT_SECONDS = 5


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
    engine: Any = None
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
    last_activity: float = field(default_factory=time.time)
    round_base_commit: str = ""  # 本轮起点 HEAD，用于 result 后产出"产物变更"事件

    @property
    def client(self) -> Any:
        """引擎底层原始客户端；None = 未连接（运维连接态与既有测试的观察点）。"""
        return self.engine.client if self.engine is not None else None

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


def _recyclable(session: "Session") -> bool:
    """可驱逐/可回收：引擎在、空闲、无待确认卡、无积压消息。"""
    return (
        session.engine is not None
        and session.state == "idle"
        and not session.pending_permissions
        and not session.pending_prompts
    )


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
        reconciled = _reconcile_stale_history(project_id)
        if reconciled is not None:
            return reconciled
        return load_history(project_id)

    def active_count(self) -> int:
        """占用槽位的会话数：引擎已连接的 + 正在连接的。"""
        return sum(
            1
            for s in self._sessions.values()
            if s.engine is not None or s.is_connecting()
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
        engine_cls = self._engine_class_for(project_id)
        if engine_cls.name == "claude":
            # Claude 引擎沿用模块级开关（测试可 monkeypatch SDK_AVAILABLE）
            if not SDK_AVAILABLE:
                raise SessionError("claude-agent-sdk 未安装，无法启动建模会话", 503)
        elif not engine_cls.available():
            raise SessionError(
                getattr(engine_cls, "UNAVAILABLE_MESSAGE", "所选引擎不可用"), 503
            )
        session = self._sessions.get(project_id)
        if session is None or session.state == "closed":
            session = Session(id=uuid.uuid4().hex[:12], project_id=project_id)
            session.events = load_history(project_id)  # 续接历史，seq 连续
            self._sessions[project_id] = session
            _expire_orphan_permissions(session)
        session.cwd = cwd
        session.last_activity = time.time()
        if session.state != "running":
            session.round_base_commit = await asyncio.to_thread(_git_head, cwd)
        # 管理员更新模型配置后，旧引擎实例仍带着旧环境：配置指纹不一致时
        # 断开重连（resume 续接上下文），让新网关/Key 立即生效
        if session.engine is not None and session.engine.config_stale():
            if session.reader_task:
                session.reader_task.cancel()
            await session.engine.disconnect()
            session.engine = None
        session.broadcast({"type": "user_message", "text": text})
        if session.engine is not None:
            await session.engine.send(text)
            session.set_state("running")
            return session
        session.pending_prompts.append(text)
        await self._request_slot(session)
        return session

    async def _request_slot(self, session: Session) -> None:
        """为无引擎的会话申请槽位：够用直接连，可驱逐则驱逐，否则排队。"""
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
        candidates = [s for s in self._sessions.values() if _recyclable(s)]
        if not candidates:
            return None
        return min(candidates, key=lambda s: s.last_activity)

    def _start_connect(self, session: Session) -> None:
        session.connect_task = asyncio.create_task(self._connect(session))

    # ---- 引擎装配 ----

    def _engine_class_for(self, project_id: str):
        """按项目 meta 的 engine 字段路由（无字段回落 claude；灰度开关）。"""
        meta_path = (
            Path(config.DATA_DIR) / "workspaces" / project_id / ".onto-web.json"
        )
        name = ""
        try:
            name = json.loads(meta_path.read_text(encoding="utf-8")).get("engine") or ""
        except (OSError, json.JSONDecodeError):
            pass
        return get_engine_class(name)

    def _make_engine(self, session: Session):
        project_id = session.project_id

        def save_token(token: str) -> None:
            session.sdk_session_id = token
            save_resume_id(project_id, token)

        async def request_permission(
            tool_name: str, tool_input: dict, title: str | None, description: str | None
        ) -> dict:
            return await self._request_permission(
                session, tool_name, tool_input, title, description
            )

        ctx = EngineContext(
            project_id=project_id,
            cwd=session.cwd,
            request_permission=request_permission,
            load_resume_token=lambda: load_resume_id(project_id),
            save_resume_token=save_token,
            clear_resume_token=lambda: clear_resume_id(project_id),
            log_error=lambda kind, text: error_log.log(project_id, kind, text),
            load_history=lambda: list(session.events),
        )
        return self._engine_class_for(project_id)(ctx)

    async def _connect(self, session: Session) -> None:
        project_id = session.project_id
        try:
            engine = self._make_engine(session)
            resumed = await engine.connect()
            session.resume_attempted = resumed
            session.engine = engine
            session.reader_task = asyncio.create_task(self._read_loop(session))
            session.broadcast(
                {
                    "type": "session_started",
                    "session_id": session.id,
                    "resumed": resumed,
                }
            )
            prompts, session.pending_prompts = session.pending_prompts, []
            if prompts and session.cwd:
                session.round_base_commit = await asyncio.to_thread(
                    _git_head, session.cwd
                )
            for prompt in prompts:
                await engine.send(prompt)
            session.set_state("running" if prompts else "idle")
        except Exception as exc:
            error_log.log(project_id, "session_connect", str(exc))
            session.engine = None
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
                    or session.engine is not None
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
        """回收引擎但保留续接点：resume token 已随消息流落盘，下次消息重连恢复上下文。"""
        self._sessions.pop(session.project_id, None)
        session.set_state("closed")
        if session.reader_task:
            session.reader_task.cancel()
        if session.engine is not None:
            await session.engine.disconnect()
            session.engine = None

    async def recycle_project(self, project_id: str) -> bool:
        """运维手动回收：断开引擎但保留续接点（与空闲回收同语义）。"""
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
                if _recyclable(s) and now - s.last_activity > ttl
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
        """服务停机：停后台回收并断开全部引擎（resume 点已落盘）。"""
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
            request.future.set_result(
                {"behavior": "allow", "updated_input": updated_input}
            )
        else:
            request.future.set_result(
                {"behavior": "deny", "message": message or "用户拒绝了该操作"}
            )

    async def interrupt(self, project_id: str) -> bool:
        """打断当前轮。返回是否做了幽灵态和解（调用方据此让前端重放刷新）。"""
        session = self._sessions.get(project_id)
        if not session:
            # 无活跃会话：不再 404——若历史尾部停在非终态（重启幽灵），补写终态
            return _reconcile_stale_history(project_id) is not None
        if session.engine is None:
            # 排队/连接中：清空积压消息并退出队列（排队的取消出口）
            async with self._lock:
                if project_id in self._waiters:
                    self._waiters.remove(project_id)
                session.pending_prompts.clear()
                self._broadcast_queue_positions()
            if not session.is_connecting():
                session.set_state("idle")
            return False
        engine = session.engine
        try:
            await asyncio.wait_for(engine.interrupt(), timeout=INTERRUPT_TIMEOUT_SECONDS)
        except Exception:  # 含 wait_for 的 TimeoutError
            # 优雅打断失败：硬终止引擎，槽位与状态立即恢复可用
            error_log.log(project_id, "interrupt_force", "打断超时，已硬终止引擎进程")
            if session.reader_task:
                session.reader_task.cancel()
            try:
                await asyncio.wait_for(engine.kill(), timeout=KILL_TIMEOUT_SECONDS)
            except Exception:
                pass
            session.engine = None
            session.broadcast({"type": "session_interrupted"})
            session.set_state("idle")
            await self._drain_waiters()
            return False
        session.broadcast({"type": "session_interrupted"})
        return False

    # ---- 确认卡（引擎权限回调 → 前端待确认事件 → 中立决策）----

    async def _request_permission(
        self,
        session: Session,
        tool_name: str,
        tool_input: dict,
        title: str | None,
        description: str | None,
    ) -> dict:
        request = PendingPermission(
            id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            tool_input=tool_input,
            title=title,
            description=description,
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
            decision = await asyncio.wait_for(
                request.future, timeout=config.PERMISSION_TIMEOUT_SECONDS
            )
        except asyncio.TimeoutError:
            decision = {"behavior": "deny", "message": "等待确认超时，已自动拒绝"}
        finally:
            session.pending_permissions.pop(request.id, None)
        session.broadcast(
            {
                "type": "permission_resolved",
                "request_id": request.id,
                "behavior": decision.get("behavior"),
            }
        )
        return decision

    # ---- 事件消费 ----

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
        engine = session.engine
        # 不加 try：本任务只被 cancel 结束，引擎侧的错误走 _engine_end 事件
        async for event in engine.events():
            session.last_activity = time.time()
            if event.get("type") == "_engine_end":
                # 引擎终止：closed（重启/终止）静默收尾，error 给用户横幅
                if event.get("reason") == "closed":
                    session.set_state("closed")
                else:
                    session.broadcast(
                        {
                            "type": "session_error",
                            "error": event.get("error") or FRIENDLY_ERROR,
                        }
                    )
                    session.set_state("error")
                # 引擎已自断开：释放槽位并唤醒排队者
                session.engine = None
                await self._drain_waiters()
                return
            session.broadcast(
                event, transient=event.get("type") == "stream_delta"
            )
            if event.get("type") == "result":
                await self._emit_workspace_changes(session)
                session.set_state("idle")


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
