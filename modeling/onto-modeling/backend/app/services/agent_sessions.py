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
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .. import config
from ..governance import context, telemetry
from . import audit, conversation_store, error_log, personal_model_settings, project_events
from .conversation_store import DEFAULT_ID as DEFAULT_CONVERSATION
from .engines import EngineContext, get_engine_class
from .engines.base import CONFIG_ERROR, FRIENDLY_ERROR, safe_json as _safe_json
from .session_host import fingerprint as _fingerprint
from .session_host import injection, timeline, turns
from .session_host.views import iso as views_iso

# 兼容重导出：既有测试与调用方从本模块取这些符号（引擎实现已移至 engines/claude.py）
from .engines.claude import (  # noqa: F401
    SDK_AVAILABLE,
    _looks_like_config_error,
    _looks_like_raw_error,
    _tool_result_excerpt,
    normalize_message,
)

_log = logging.getLogger(__name__)


def _history_path(project_id: str, conversation_id: str = DEFAULT_CONVERSATION) -> Path:
    return conversation_store.history_path(project_id, conversation_id)


def load_history(project_id: str, conversation_id: str = DEFAULT_CONVERSATION) -> list[dict]:
    path = _history_path(project_id, conversation_id)
    if not path.exists():
        return []
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events


VISIBLE_HISTORY_TAIL = 400


def _is_visible_chat_event(event: dict) -> bool:
    """Return whether an event creates a row in the chat history UI.

    Runtime state, tool result updates, and old engine diagnostics are not
    standalone chat rows and must not consume the initial history window.
    """
    event_type = event.get("type")
    if event_type in {
        "user_message",
        "workspace_changes",
        "session_interrupted",
        "permission_request",
        "context_compacted",
    }:
        return True
    if event_type == "session_error":
        return "exit code: 143" not in str(event.get("error") or "")
    if event_type != "assistant_message":
        return False
    for block in event.get("blocks") or []:
        kind = block.get("kind")
        if kind == "tool_use":
            return True
        if event.get("subagent"):
            continue
        if kind == "text" and str(block.get("text") or "").strip():
            return True
        if kind == "thinking" and str(block.get("text") or "").strip():
            return True
    return False


def visible_history_start(events: list[dict], limit: int = VISIBLE_HISTORY_TAIL) -> int:
    """Find the list offset containing the last ``limit`` visible chat rows."""
    if limit <= 0:
        return len(events)
    visible = 0
    oldest_visible = len(events)
    for index in range(len(events) - 1, -1, -1):
        if _is_visible_chat_event(events[index]):
            visible += 1
            oldest_visible = index
            if visible >= limit:
                return index
    return oldest_visible


def _interrupt_events(reason: str, message: str) -> list[dict]:
    """中断标记：中断事件 + idle 终态（重启自愈与停机共用同一格式）。"""
    now = time.time()
    return [
        {"type": "session_interrupted", "reason": reason, "message": message, "ts": now},
        {"type": "session_state", "state": "idle", "ts": now},
    ]


def _reconcile_stale_history(
    project_id: str, conversation_id: str = DEFAULT_CONVERSATION
) -> list[dict] | None:
    """会话进程被重启/强杀后，事件日志尾部可能停在 running 等非终态，
    前端回放即成"幽灵运行态"（永远执行中、打断 404 无效）。无活跃会话时
    补写中断标记与 idle 终态使日志自愈；返回补写后的事件，未补写返回 None。"""
    events = load_history(project_id, conversation_id)
    state = None
    for event in reversed(events):
        if event.get("type") == "session_state":
            state = event.get("state")
            break
    if state not in ("running", "queued", "connecting"):
        return None
    patch = _interrupt_events("stale", "会话进程已重启，上一轮未正常结束，已标记为中断")
    for offset, event in enumerate(patch):
        event["seq"] = len(events) + offset
    try:
        with open(_history_path(project_id, conversation_id), "a", encoding="utf-8") as f:
            for event in patch:
                f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except OSError:
        return None
    return events + patch


def repair_history(project_id: str, conversation_id: str) -> int:
    """启动时（没有在线会话）修一条对话的历史：尾部停在运行中的补中断与空闲，
    没回复的确认卡、决策卡补失效。返回补了几处。"""
    count = 1 if _reconcile_stale_history(project_id, conversation_id) is not None else 0
    events = load_history(project_id, conversation_id)
    resolved = {e.get("request_id") for e in events if e.get("type") == "permission_resolved"}
    for event in events:
        if event.get("type") == "permission_request" and event.get("request_id") not in resolved:
            resolved.add(event.get("request_id"))
            append_history(project_id, conversation_id, {"type": "permission_resolved",
                                                         "request_id": event.get("request_id"), "behavior": "expired"})
            count += 1
    closed = {(e.get("payload") or {}).get("question_id") for e in events
              if e.get("type") in ("question_answered", "question_withdrawn")}
    for event in events:
        question_id = (event.get("payload") or {}).get("question_id") if event.get("type") == "question_asked" else None
        if question_id and question_id not in closed:
            closed.add(question_id)
            append_history(project_id, conversation_id, {
                "type": "question_withdrawn",
                "payload": {"question_id": question_id, "reason": "服务重启，这个问题已失效"},
            })
            count += 1
    return count


def append_history(project_id: str, conversation_id: str, record: dict, watchers=()) -> dict:
    """不经在线会话往历史追加一条记录（序号 = 当前行数）；写不了时只推给在线订阅者。"""
    events = load_history(project_id, conversation_id)
    record = {**record, "ts": record.get("ts") or time.time(), "seq": len(events)}
    try:
        path = _history_path(project_id, conversation_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError:
        pass
    for queue in list(watchers):
        queue.put_nowait(record)
    return record


def _meta_path(project_id: str, conversation_id: str = DEFAULT_CONVERSATION) -> Path:
    return conversation_store.meta_path(project_id, conversation_id)


def load_resume_id(
    project_id: str, conversation_id: str = DEFAULT_CONVERSATION, *, engine: str = "claude"
) -> str | None:
    """该会话上次的引擎续接 token；重连时恢复模型上下文。

    token 只对写下它的引擎有效（切换引擎后旧 token 不可用）；
    旧格式无 engine 字段，均由 Claude 引擎写入。
    """
    path = _meta_path(project_id, conversation_id)
    if not path.exists():
        return None
    try:
        meta = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if (meta.get("engine") or "claude") != engine:
        return None
    return meta.get("sdk_session_id") or None


def save_resume_id(
    project_id: str,
    session_id: str,
    conversation_id: str = DEFAULT_CONVERSATION,
    *,
    engine: str = "claude",
) -> None:
    try:
        _meta_path(project_id, conversation_id).write_text(
            json.dumps({"sdk_session_id": session_id, "engine": engine}),
            encoding="utf-8",
        )
    except OSError:
        pass  # 落盘失败只影响下次续接，不阻断会话


def clear_resume_id(project_id: str, conversation_id: str = DEFAULT_CONVERSATION) -> None:
    try:
        _meta_path(project_id, conversation_id).unlink(missing_ok=True)
    except OSError:
        pass


def purge_history(project_id: str) -> None:
    """删除项目时清除全部会话的事件历史与续接点：永久删除不留可回放的对话。

    删之前先把历史里还没入账的用量记进配额台账（台账在数据目录 governance/ 下，不随项目删除），
    否则最后一轮的用量随历史一起没了，删项目就能绕开 token 上限。
    """
    from ..governance import quota

    try:
        quota.service().sync()
    except Exception:  # noqa: BLE001 - 入账失败不挡删除
        _log.warning("删除项目 %s 前用量入账失败", project_id, exc_info=True)
    conversation_store.purge(project_id)


# 打断的应答超时：引擎/CLI 悬死（如网关调用无响应）时优雅打断永远等不到应答，
# 超时即硬终止引擎（续接点保留，下一条消息 resume）——保证"停止"按钮总能停
INTERRUPT_TIMEOUT_SECONDS = 10
KILL_TIMEOUT_SECONDS = 5
# 停机时回收全部引擎的总时长上限。docker 停机宽限期 30 秒，uvicorn 先等连接关闭最多
# 3 秒（run.py），这里再留 20 秒，合计控制在 25 秒内，不被宽限期后的 SIGKILL 截断
SHUTDOWN_BUDGET_SECONDS = 20
SHUTDOWN_INTERRUPT_MESSAGE = "服务停机，本轮未正常结束，已标记为中断"


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
    # 决策卡（AskUserQuestion）：拆成的问题与已收到的回答（问题 ID → 回答）
    questions: list = field(default_factory=list)
    answers: dict = field(default_factory=dict)


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
    discarded: bool = False  # 项目已删除：事件不再落盘，也不再申请槽位
    # 引擎绑定的会话：决定事件历史写到哪份文件、续接哪个 token
    conversation_id: str = DEFAULT_CONVERSATION
    sending: int = 0  # 处理中的 send() 数；期间不可回收或换绑，否则消息会落到已丢弃的会话上
    # 引擎使用谁的模型配置：用户名 = 该用户的个人 Key；"" = 管理员配置（原行为）
    model_owner: str = ""
    # 已交给引擎、还没等到 result 的消息数（引擎内积压的也算）
    turns_in_flight: int = 0
    # 运行中模型配置变了：等手头的轮次都出 result 再断开，下一条消息按新配置重连
    reload_pending: bool = False
    # 你正在打断：等引擎停下期间这一轮可能先出 result，收尾时工作过程记为打断、执行记录留给打断处理
    interrupting: bool = False
    # 引擎连上时的会话配置指纹（session_host/fingerprint.py）与等待生效的变化项
    fingerprint: Any = None
    pending_changes: list = field(default_factory=list)
    # 会话宿主（session_host/turns.py、timeline.py）：引擎实例 ID、这一轮、这条对话里开着的执行
    engine_instance: str = ""
    turn: Any = None
    turn_title: str = ""
    turn_started_at: float | None = None
    exec_id: str | None = None
    # 运行中收下、要等前一步结束才开始的消息（原文、页面命令任务 ID）：前一轮出 result 时接上
    deferred: list = field(default_factory=list)
    # 出错卡（EC-04）：最近一次留档的错误编号、连续重试次数、出错那一轮的消息（S-14 重试用）
    last_error_id: str | None = None
    retries: int = 0
    last_prompt: str = ""
    last_day: str = ""
    # 刚被打断：这一轮随后出的 result 不覆盖「打断之后」的建议条
    keep_suggestions: bool = False

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
        if not self.discarded:  # 已删除项目的历史文件已清除，再写会重建出残留
            try:
                path = _history_path(self.project_id, self.conversation_id)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event, ensure_ascii=False) + "\n")
            except OSError:
                pass  # 落盘失败不阻断会话
        for queue in list(self.subscribers):
            queue.put_nowait(event)

    def set_state(self, state: str, reason: str | None = None) -> None:
        if self.state != state:
            self.state = state
            if state == "running":
                self.turn_started_at = self.turn_started_at or time.time()
            elif state in ("idle", "closed", "error"):
                self.turn_started_at = None
            self.broadcast({"type": "session_state", "state": state, "turn_started_at": self.turn_started_at,
                            "reason": reason})
            if not self.discarded:
                project_events.conversation_changed(self.project_id, self.conversation_id)

    def show_state(self, state: str) -> None:
        """只给界面看的状态（等你回答 waiting_user）：不改会话池调度用的 state。"""
        self.broadcast({"type": "session_state", "state": state, "turn_started_at": self.turn_started_at})

    def current_turn(self) -> Any:
        if self.turn is None:
            self.turn = timeline.Turn.new(self.turn_title)
        return self.turn

    def is_connecting(self) -> bool:
        return self.connect_task is not None and not self.connect_task.done()


def _recyclable(session: "Session") -> bool:
    """可驱逐/可回收：引擎在、空闲、无待确认卡、无积压消息、无处理中的发送。"""
    return (
        session.engine is not None
        and session.state == "idle"
        and not session.pending_permissions
        and not session.pending_prompts
        and not session.sending
    )


def _unfinished(session: "Session") -> bool:
    """有没跑完的轮：在跑、排队，或消息已收下但引擎还没接走。"""
    return session.state in ("running", "queued") or bool(session.pending_prompts)


# 会话层中断原因 → 执行记录的中断原因
_EXEC_REASONS = {"shutdown": "shutdown", "ops_disconnect": "ops", "stale": "restart", "user": "user",
                 "engine_exit": "engine_exit", "recycled": "recycled"}


def _mark_interrupted(session: "Session", reason: str, message: str) -> None:
    """停机、运维断开：这一轮写中断标记；开着的执行记为中断，并在对话里放续跑卡。"""
    if session.turn is not None and session.turn.steps:
        session.turn.finish("interrupted")
        session.broadcast(session.turn.progress_record())
    session.turn = None
    record, card = turns.interrupt_exec(session, _EXEC_REASONS.get(reason, "engine_exit"), card=True)
    now = time.time()
    session.broadcast({"type": "session_interrupted", "reason": reason, "message": message, "ts": now,
                       **turns.interrupted_fields(record)})
    if card is not None:
        session.broadcast(card)
    session.broadcast({"type": "session_state", "state": "idle", "ts": now})
    session.state = "idle"
    session.turn_started_at = None


def _bound_to(session: "Session | None", conversation_id: str) -> bool:
    """引擎当前是否绑定着这条会话。"""
    return session is not None and session.conversation_id == conversation_id


def _busy(session: "Session") -> bool:
    """在跑、排队、连接中、有待确认卡或有处理中的发送：此时不能把引擎换绑到别的会话。

    连接期间 state 仍是 idle，所以不能只看 state。
    """
    return (
        session.state in ("running", "queued")
        or session.is_connecting()
        or bool(session.pending_permissions)
        or session.sending > 0
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
    # 决策卡同理：问题撤回（契约 EC-34）
    closed = {
        (e.get("payload") or {}).get("question_id")
        for e in session.events
        if e.get("type") in ("question_answered", "question_withdrawn")
    }
    for event in list(session.events):
        if event.get("type") != "question_asked":
            continue
        question_id = (event.get("payload") or {}).get("question_id")
        if question_id and question_id not in closed:
            closed.add(question_id)
            session.broadcast({"type": "question_withdrawn",
                               "payload": {"question_id": question_id, "reason": "会话已重启，这个问题已失效"}})


async def _stop_engine(engine: Any) -> None:
    """断开引擎；CLI 悬死不应答时超时硬杀，保证调用方总能返回。"""
    # disconnect 要等 CLI 应答，CLI 悬死时会永久挂起，并把 _lock 一起拖住——
    # 走 close() 的调用方（如删除项目）因此永远不返回。超时按 interrupt 同样的方式硬杀。
    try:
        await asyncio.wait_for(engine.disconnect(), timeout=INTERRUPT_TIMEOUT_SECONDS)
    except Exception:  # 含 wait_for 的 TimeoutError
        await _kill_engine(engine)


async def _kill_engine(engine: Any) -> None:
    """硬终止引擎，不等 CLI 应答。"""
    try:
        await asyncio.wait_for(engine.kill(), timeout=KILL_TIMEOUT_SECONDS)
    except Exception:
        pass


class SessionManager:
    def __init__(self):
        self._sessions: dict[str, Session] = {}  # project_id -> session
        self._waiters: list[str] = []  # 等待会话槽位的项目（FIFO）
        self._lock = asyncio.Lock()
        self._reaper_task: asyncio.Task | None = None
        self._tasks: set[asyncio.Task] = set()  # 后台收尾任务的强引用，免得执行中被回收
        # 各（项目, 会话）打开着的事件流队列：会话新建时直接挂上（首条消息、
        # 空闲回收后再发、切换会话后再发）
        self._watchers: dict[tuple[str, str], set[asyncio.Queue]] = {}
        # 最近一次被你打断的执行（S-08 的响应带它）
        self.interrupted_execs: dict[tuple[str, str], dict | None] = {}

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ---- 事件流订阅 ----

    def watch(
        self,
        project_id: str,
        queue: asyncio.Queue,
        conversation_id: str = DEFAULT_CONVERSATION,
    ) -> None:
        """登记事件流队列；引擎正绑定这条会话时立即挂上，之后新建的会话由 _bind_session 挂上。"""
        self._watchers.setdefault((project_id, conversation_id), set()).add(queue)
        session = self._sessions.get(project_id)
        if _bound_to(session, conversation_id) and queue not in session.subscribers:
            session.subscribers.append(queue)

    def unwatch(
        self,
        project_id: str,
        queue: asyncio.Queue,
        conversation_id: str = DEFAULT_CONVERSATION,
    ) -> None:
        key = (project_id, conversation_id)
        watchers = self._watchers.get(key)
        if watchers is not None:
            watchers.discard(queue)
            if not watchers:
                self._watchers.pop(key, None)
        session = self._sessions.get(project_id)
        if session and queue in session.subscribers:
            session.subscribers.remove(queue)

    # ---- 查询 ----

    def get(self, project_id: str) -> Session | None:
        return self._sessions.get(project_id)

    def resolve_conversation(
        self, project_id: str, conversation_id: str | None = None
    ) -> str:
        """校验会话 id；省略时取引擎当前绑定的会话，没有在线引擎则取最近更新的一条。"""
        if conversation_id:
            if not conversation_store.exists(project_id, conversation_id):
                raise SessionError("会话不存在或已删除", 404)
            return conversation_id
        session = self._sessions.get(project_id)
        if session is not None:
            return session.conversation_id
        return conversation_store.list_conversations(project_id)[0]["id"]

    def live_status(self, project_id: str) -> dict | None:
        """引擎当前绑定哪条会话、是否正忙（前端据此标记运行中的会话）。"""
        session = self._sessions.get(project_id)
        if session is None:
            return None
        return {
            "conversation_id": session.conversation_id,
            "state": session.state,
            "busy": _busy(session),
        }

    def get_history(
        self, project_id: str, conversation_id: str | None = None
    ) -> list[dict]:
        """会话的事件：引擎正绑定它时取内存，否则取磁盘上的历史（服务重启后仍可回放）。"""
        conversation_id = self.resolve_conversation(project_id, conversation_id)
        session = self._sessions.get(project_id)
        if session and session.conversation_id == conversation_id:
            return session.events
        reconciled = _reconcile_stale_history(project_id, conversation_id)
        if reconciled is not None:
            return reconciled
        return load_history(project_id, conversation_id)

    def append_record(self, project_id: str, conversation_id: str, record: dict) -> dict:
        """往某条对话的历史追加一条宿主记录（续跑卡、卡片更新）：引擎正绑着它时照常广播，
        否则直接追加到 JSONL（序号接着行号）并推给已打开的事件流。只在事件循环线程或启动时调用。"""
        session = self._sessions.get(project_id)
        if _bound_to(session, conversation_id):
            session.broadcast(record)
            return record
        return append_history(project_id, conversation_id, record,
                              self._watchers.get((project_id, conversation_id), ()))

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

    async def send(
        self,
        project_id: str,
        cwd: Path,
        text: str,
        conversation_id: str | None = None,
        username: str = "",
        *,
        meta: dict | None = None,
    ) -> Session:
        """发一条消息。meta 是界面上显示的内容（契约 EC-10）：display_text（用户原话，text 是发给模型的
        全文）、attachments、references、origin，以及页面命令任务 task_id。

        模型配置固定用项目所有者的（方案 B3）：谁发的消息都一样，Studio 复核等后端内部发的消息也是。
        username 只用于审计口径，不再决定用谁的 Key。
        """
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
        # 换绑要回收旧引擎（await 断开，且第一步就把旧 Session 移出字典）：不加锁时
        # 并发的 send 会各建一个 Session，被覆盖的那个带着引擎却不在字典里，无人回收
        owner = personal_model_settings.model_owner(project_owner(project_id), engine_cls.name)
        # 配额（ADR-0043）：发起一轮的入口都经这里（S-07、S-20、重试、续跑、Studio 转复核）。token 先查；
        # 并发会话和绑定会话在同一把锁里，两个同时发起的发送不会都过（sending 计入占用）
        from ..governance import quota

        await asyncio.to_thread(quota.check_before_turn, project_id, username, "tokens")
        async with self._lock:
            await asyncio.to_thread(quota.check_before_turn, project_id, username, "concurrency", self._sessions)
            session = await self._bind_session(project_id, conversation_id)
            self._bind_model_owner(session, owner, engine_cls.name)
            session.sending += 1
        try:
            await self._deliver(session, cwd, text, meta or {})
        finally:
            session.sending -= 1
        return session

    async def _bind_session(
        self, project_id: str, conversation_id: str | None
    ) -> Session:
        """取得绑定目标会话的 Session；引擎绑着别的会话时先回收（调用方持有 _lock）。"""
        conversation_id = self.resolve_conversation(project_id, conversation_id)
        session = self._sessions.get(project_id)
        if session is not None and session.conversation_id != conversation_id:
            if _busy(session):
                raise SessionError("另一条会话正在运行，请等它结束或先打断", 409)
            await self._recycle(session)  # 续接 token 已落盘，切回时 resume
            session = None
        if session is None or session.state == "closed":
            session = Session(
                id=uuid.uuid4().hex[:12],
                project_id=project_id,
                conversation_id=conversation_id,
            )
            # 续接历史，seq 连续
            session.events = load_history(project_id, conversation_id)
            session.last_day = next(
                (e.get("day") or "" for e in reversed(session.events) if e.get("type") == "day_marker"), ""
            )
            # 这条会话已打开的事件流在广播任何事件之前挂上，本轮过程即时推送
            session.subscribers.extend(self._watchers.get((project_id, conversation_id), ()))
            self._sessions[project_id] = session
            _expire_orphan_permissions(session)
        return session

    @staticmethod
    def _bind_model_owner(session: Session, owner: str, engine: str) -> None:
        """引擎用项目所有者的模型配置（方案 B3；调用方持有 _lock）。

        归属计入会话指纹：所有者设了或清了个人 Key、项目移交给别人，下一条消息前按新归属重连
        （运行中的一轮跑完再切，同其他配置变化）。
        """
        # 引擎实例自己记着连接时的归属（ctx.model_owner）；这里记的是应当生效的归属，
        # 两者不同时指纹的 owner 部分不同，_deliver 按配置变化处理
        session.model_owner = owner
        if owner:
            try:
                personal_model_settings.resolve(owner, engine)
            except personal_model_settings.PersonalConfigError as exc:
                raise SessionError(str(exc), exc.status) from exc

    async def _deliver(self, session: Session, cwd: Path, text: str, meta: dict | None = None) -> None:
        meta = meta or {}
        session.last_activity = time.time()
        running = session.state == "running"
        if not running:
            # 助手在变更集副本里改（方案 C7）：一轮开始前建或修副本、跟上主线；建不了时退回主线工作区
            session.cwd = await asyncio.to_thread(_agent_cwd, session.project_id, cwd)
            session.round_base_commit = await asyncio.to_thread(_git_head, session.cwd)
        elif session.cwd is None:
            session.cwd = cwd
        # 会话配置指纹（模型、归属、装配、流程、MCP、权限模式）变了：断开重连（resume 续接上下文），
        # 让新配置、新装配目录在这条消息生效（方案 B2、详设 8.1 第 4 步）。正在跑的一轮不能切断：
        # 只记待重载，这条消息仍交给旧引擎，等手头的轮次都出 result 再断开（_read_loop）
        if session.engine is not None:
            if session.state != "running":
                # 没有正在跑的一轮：先跟随通道、按锁文件装配并切换 .claude（只动纯数据包时不必重连）
                await asyncio.to_thread(_fingerprint.prepare, session.project_id)
            changes = await asyncio.to_thread(
                _fingerprint.changes, session.engine, session.project_id, session.model_owner, session.fingerprint
            )
            if changes:
                telemetry.count("onto_reconnects_total", project=session.project_id)
                session.pending_changes = changes
                if session.state == "running":
                    if not session.reload_pending:
                        session.broadcast({
                            "type": "session_config_pending",
                            "changes": _fingerprint.describe(changes),
                            "effective": "next_message",
                            "detail_link": None,
                        })
                    session.reload_pending = True
                else:
                    if session.reader_task:
                        session.reader_task.cancel()
                    await session.engine.disconnect()
                    session.engine = None
        self._user_message(session, text, meta, deferred=running)
        if not session.discarded:  # 已删除项目再写标题会重建出会话索引
            conversation_store.note_message(
                session.project_id, session.conversation_id, meta.get("display_text") or text
            )
        # 执行记录与这一轮：运行中收下的消息等前一步结束再开始（_finish_turn 接上）
        if running:
            session.deferred.append((text, meta.get("task_id")))
        else:
            await asyncio.to_thread(turns.begin, session, text, task_id=meta.get("task_id"),
                                    resume_exec=meta.get("resume_exec"))
            session.turn = timeline.Turn.new(session.turn_title)
            session.last_prompt = text
            project_events.scope_changed(session.project_id)
        if session.engine is not None:
            await self._send_turn(session, session.engine, text)
            session.set_state("running")
            return
        session.pending_prompts.append(text)
        await self._request_slot(session)

    @staticmethod
    def _user_message(session: Session, text: str, meta: dict, *, deferred: bool) -> None:
        """用户气泡（EC-10）：界面显示原话，发给模型的全文另存（Jiuwen 回放历史用）；新的一天先放日期分隔。"""
        today = time.strftime("%Y-%m-%d")
        if not session.last_day and session.events:
            ts = session.events[-1].get("ts")
            session.last_day = time.strftime("%Y-%m-%d", time.localtime(ts)) if isinstance(ts, (int, float)) else ""
        if session.last_day and session.last_day != today:
            # 跨天才放日期分隔（同一天里接着聊不放；对话的第一条前面也不放）
            month, day = int(today[5:7]), int(today[8:10])
            session.broadcast({"type": "day_marker", "payload": {"label": f"{month}月{day}日"}, "day": today})
        session.last_day = today
        display = meta.get("display_text") or text
        record = {
            "type": "user_message",
            "text": display,
            "message_id": f"m-{len(session.events) + 1}",
            "attachments": meta.get("attachments") or [],
            "references": meta.get("references") or [],
            "origin": meta.get("origin"),
            "delivery": "deferred" if deferred else "immediate",
        }
        if meta.get("command"):
            record["command"] = meta["command"]
        if display != text:
            record["prompt"] = text
        session.broadcast(record)

    async def _request_slot(self, session: Session) -> None:
        """为无引擎的会话申请槽位：够用直接连，可驱逐则驱逐，否则排队。"""
        async with self._lock:
            if session.discarded or session.is_connecting():
                return
            if self.active_count() < config.MAX_ACTIVE_SESSIONS:
                self._start_connect(session)
                return
            # 池满：先驱逐空闲会话再排队；运维把策略改成只排队（O-16）时不驱逐
            victim = self._pick_idle_victim() if config.SESSION_OVERFLOW_POLICY != "queue_only" else None
            if victim is not None:
                await self._recycle(victim)
                if not session.discarded:  # 驱逐期间项目被删除：不再为它连接
                    self._start_connect(session)
                return
            if session.project_id not in self._waiters:
                self._waiters.append(session.project_id)
            session.set_state("queued")
            self._broadcast_queue_positions()

    @staticmethod
    async def _send_turn(session: Session, engine: Any, text: str) -> None:
        # 先计数再发送：发送期间读循环可能收到上一轮的 result，计数为零就会触发待重载断开
        session.turns_in_flight += 1
        try:
            await engine.send(text)
        except BaseException:
            session.turns_in_flight -= 1
            raise

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
        # 构造时定住会话 id：续接 token 的存取必须跟着这个引擎实例绑定的会话走
        conversation_id = session.conversation_id
        engine_cls = self._engine_class_for(project_id)
        engine_name = engine_cls.name

        def save_token(token: str) -> None:
            if session.discarded:  # 续接点文件已随项目清除
                return
            session.sdk_session_id = token
            save_resume_id(project_id, token, conversation_id, engine=engine_name)

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
            load_resume_token=lambda: load_resume_id(
                project_id, conversation_id, engine=engine_name
            ),
            save_resume_token=save_token,
            clear_resume_token=lambda: clear_resume_id(project_id, conversation_id),
            log_error=lambda kind, text: self._log_error(session, kind, text),
            load_history=lambda: list(session.events),
            conversation_id=conversation_id,
            model_owner=session.model_owner,
            assembly_dir=_assembly_dir(session.cwd),
            # 宿主注入（方案 B4）：每个引擎实例一份注入状态，两个引擎同一份内容
            injector=injection.Injector(session.cwd) if session.cwd is not None else None,
            engine_instance=session.engine_instance,
        )
        return engine_cls(ctx)

    @staticmethod
    def _log_error(session: Session, kind: str, text: str) -> str:
        """运维错误留档（O-50），带错误编号；出错卡（EC-04）引用同一个编号。"""
        error_id = f"err-{uuid.uuid4().hex[:6]}"
        error_log.log(session.project_id, kind, f"[{error_id}] {text}")
        session.last_error_id = error_id
        return error_id

    async def _connect(self, session: Session) -> None:
        project_id = session.project_id
        started_at = time.monotonic()
        try:
            # 连接前（没有正在跑的一轮）按锁文件装配并原子切换 .claude，新实例读到的就是新版本
            await asyncio.to_thread(_fingerprint.prepare, project_id)
            # agent 提交收口（方案 C11）：这个项目的 git 指向内核提交钩子（新建、导入的项目在这里补上）
            await asyncio.to_thread(_install_commit_hooks, session.cwd)
            session.engine_instance = uuid.uuid4().hex
            engine = self._make_engine(session)
            # 连接前写：state_helper 按新实例认锁，agent 的 git 钩子按它写来源 trailer
            _write_engine_marker(
                session.cwd, getattr(engine, "name", ""), session.conversation_id,
                engine_id=session.engine_instance, owner=project_owner(project_id),
            )
            resumed = await engine.connect()
            if self._sessions.get(project_id) is not session:
                # 连接期间会话已被关闭、换绑或删除项目：晚到的引擎没人认领，不当场断开就成了
                # 游离子进程——不占池上限，也不会被空闲回收。项目已删除时直接硬杀
                await (_kill_engine if session.discarded else _stop_engine)(engine)
                return
            session.resume_attempted = resumed
            session.engine = engine
            session.turns_in_flight = 0
            session.reload_pending = False
            session.fingerprint = await asyncio.to_thread(
                _fingerprint.compute, engine, project_id, session.model_owner
            )
            session.reader_task = asyncio.create_task(self._read_loop(session))
            if session.pending_changes:
                # 配置变化后的这次重连：告诉前端已切到新版本（契约 EC-07）
                session.broadcast({
                    "type": "session_reconnected",
                    "reason": "config_changed",
                    "changes": list(session.pending_changes),
                    "context_kept": bool(resumed),
                    "duration_ms": int((time.monotonic() - started_at) * 1000),
                })
                session.pending_changes = []
            started = {
                "type": "session_started",
                "session_id": session.id,
                "resumed": resumed,
                "engine": getattr(engine, "name", ""),
            }
            # 连接时已知的窗口与触发点随之下发：切换引擎或改配置后，第一轮进行中
            # 用量圈就按新值显示，不必等本轮结束的 context_usage
            window, compact_at = engine.context_limits()
            if window:
                started.update(window=window, compact_at=compact_at)
            session.broadcast(started)
            prompts, session.pending_prompts = session.pending_prompts, []
            if prompts and session.cwd:
                session.round_base_commit = await asyncio.to_thread(
                    _git_head, session.cwd
                )
            for prompt in prompts:
                await self._send_turn(session, engine, prompt)
            session.set_state("running" if prompts else "idle")
        except Exception as exc:
            if session.discarded:  # 项目已删除，工作区没了连接失败是预期内的，不报错
                return
            error_id = self._log_error(session, "session_connect", str(exc))
            session.engine = None
            self._turn_failed(session, error_id, FRIENDLY_ERROR)
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
        """排队位次（EC-02，持久）：排队卡显示前面还有几个、池子占用。"""
        total = len(self._waiters)
        pool = {"active": self.active_count(), "max": config.MAX_ACTIVE_SESSIONS}
        for index, project_id in enumerate(self._waiters):
            session = self._sessions.get(project_id)
            if session:
                session.broadcast({
                    "type": "queue_position", "position": index + 1, "waiting": total, "pool": pool,
                    "progress_pct": round(100 * (1 - index / total)) if total else 0,
                })

    # ---- 回收与关闭 ----

    async def _recycle(self, session: Session, reason: str = "recycled") -> None:
        """回收引擎但保留续接点：resume token 已随消息流落盘，下次消息重连恢复上下文。

        这条对话里还在等的执行（命令跨几轮问答，正等你的下一句）记为中断，对话里放续跑卡。
        """
        self._sessions.pop(session.project_id, None)
        if session.exec_id and not session.discarded:
            _, card = turns.interrupt_exec(session, reason, card=True)
            if card is not None:
                session.broadcast(card)
        session.set_state("closed")
        if session.reader_task:
            session.reader_task.cancel()
        engine, session.engine = session.engine, None
        if engine is None:
            return
        await _stop_engine(engine)

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
        """服务停机：先给没跑完的轮写中断标记，再并发断开全部引擎（resume 点已落盘）。

        中断标记先写：断开可能超时，进程随时会被宽限期后的 SIGKILL 打断。
        不排 _lock：持锁的操作若卡在悬死的 CLI 上，排锁会吃掉停机预算；
        摘除会话是同步代码，事件循环不会在中途切走。
        """
        if self._reaper_task:
            self._reaper_task.cancel()
            self._reaper_task = None
        for task in list(self._tasks):
            task.cancel()
        self._waiters.clear()
        sessions = list(self._sessions.values())
        for session in sessions:
            if _unfinished(session):
                _mark_interrupted(session, "shutdown", SHUTDOWN_INTERRUPT_MESSAGE)
            elif session.exec_id and not session.discarded:
                # 命令还在等你的下一句：停机后续不上，记为中断并放续跑卡
                _, card = turns.interrupt_exec(session, "shutdown", card=True)
                if card is not None:
                    session.broadcast(card)
        engines = []
        for session in sessions:
            self._sessions.pop(session.project_id, None)
            session.set_state("closed")
            if session.reader_task:
                session.reader_task.cancel()
            engine, session.engine = session.engine, None
            if engine is not None:
                engines.append(engine)
        if not engines:
            return
        try:
            await asyncio.wait_for(
                asyncio.gather(*(_stop_engine(e) for e in engines), return_exceptions=True),
                timeout=SHUTDOWN_BUDGET_SECONDS,
            )
        except asyncio.TimeoutError:
            pass  # 单个引擎的断开另有超时硬杀，这里只兜总时长

    async def close(
        self,
        project_id: str,
        conversation_id: str | None = None,
        all_conversations: bool = False,
    ) -> None:
        """丢弃会话的模型上下文（历史保留，续接点清除）；下次发消息全新开始。

        引擎正绑定该会话时一并终止；绑定的是别的会话时只清续接点，不动引擎。
        all_conversations 用于切换引擎：终止引擎，全部会话的续接点作废（删除项目走 discard）。
        """
        async with self._lock:
            session = self._sessions.get(project_id)
            if not all_conversations:
                conversation_id = self.resolve_conversation(project_id, conversation_id)
            bound = session is not None and (
                all_conversations or session.conversation_id == conversation_id
            )
            if (bound or session is None) and project_id in self._waiters:
                self._waiters.remove(project_id)
            if bound:
                session.pending_prompts.clear()
                await self._recycle(session, reason="user")
        if all_conversations:
            conversation_store.clear_all_resume_tokens(project_id)
        else:
            clear_resume_id(project_id, conversation_id)
        await self._drain_waiters()

    async def discard(self, project_id: str) -> None:
        """删除项目专用：立即硬杀引擎、退出队列、清除历史，不等 CLI 应答也不等 _lock。

        close() 先排 _lock、再等 CLI 优雅断开（悬死时最长 15 秒）；删除什么都不保留，
        直接摘掉会话并硬杀。摘除在同一段同步代码里完成，事件循环不会在中途切走，无需加锁。
        连接中的会话不取消连接任务：引擎连上后由 _connect 发现会话已摘除，当场硬杀。
        """
        if project_id in self._waiters:
            self._waiters.remove(project_id)
        session = self._sessions.pop(project_id, None)
        engine = None
        if session is not None:
            session.discarded = True
            session.pending_prompts.clear()
            for request in list(session.pending_permissions.values()):
                if not request.future.done():
                    request.future.set_result({"behavior": "deny", "message": "项目已删除"})
            if session.reader_task:
                session.reader_task.cancel()
            engine, session.engine = session.engine, None
            session.set_state("closed")  # 在线订阅者收到终态；discarded 后不落盘
        purge_history(project_id)
        from .session_host import exec_records

        exec_records.purge(project_id)  # 执行记录与页面命令任务在同一目录
        _purge_changesets(project_id)
        self._broadcast_queue_positions()
        if engine is not None:
            await _kill_engine(engine)
        # 让出的槽位交给排队者；_drain_waiters 要排 _lock，放后台，删除不等它
        self._spawn(self._drain_waiters())

    async def delete_conversation(
        self, project_id: str, conversation_id: str
    ) -> list[dict]:
        """删除会话的历史与续接点，返回剩余会话；引擎正绑定它时先回收，忙则拒绝。"""
        async with self._lock:
            conversation_id = self.resolve_conversation(project_id, conversation_id)
            session = self._sessions.get(project_id)
            if session is not None and session.conversation_id == conversation_id:
                if _busy(session):
                    raise SessionError("会话正在运行，请等它结束或先打断后再删除", 409)
                await self._recycle(session)
            remaining = conversation_store.delete(project_id, conversation_id)
        await self._drain_waiters()
        return remaining

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

    async def interrupt(
        self, project_id: str, conversation_id: str | None = None
    ) -> bool:
        """打断当前轮。返回是否做了幽灵态和解（调用方据此让前端重放刷新）。"""
        conversation_id = self.resolve_conversation(project_id, conversation_id)
        session = self._sessions.get(project_id)
        if not session or session.conversation_id != conversation_id:
            # 引擎没绑定这条会话（未启动，或正绑着另一条——那条不该被这里打断）：
            # 不 404——若历史尾部停在非终态（重启幽灵），补写终态
            return _reconcile_stale_history(project_id, conversation_id) is not None
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
        session.interrupting = True
        try:
            await asyncio.wait_for(engine.interrupt(), timeout=INTERRUPT_TIMEOUT_SECONDS)
        except Exception:  # 含 wait_for 的 TimeoutError
            # 优雅打断失败：硬终止引擎，槽位与状态立即恢复可用
            error_log.log(project_id, "interrupt_force", "打断超时，已硬终止引擎进程")
            if session.reader_task:
                session.reader_task.cancel()
            await _kill_engine(engine)
            session.engine = None
            await self._mark_user_interrupt(session)
            session.set_state("idle")
            await self._drain_waiters()
            return False
        await self._mark_user_interrupt(session)
        return False

    async def _mark_user_interrupt(self, session: Session) -> None:
        """你打断了这一步（EC-05）：执行记为中断（不放续跑卡，建议条给「从打断处继续」），工作过程收起。"""
        from .session_host import command_tasks

        session.interrupting = False
        if session.turn is not None and session.turn.steps:
            session.turn.finish("interrupted")
            session.broadcast(session.turn.progress_record())
        session.turn = None
        session.deferred.clear()
        record, _ = await asyncio.to_thread(turns.interrupt_exec, session, "user", card=False)
        self.interrupted_execs[(session.project_id, session.conversation_id)] = record
        session.broadcast({"type": "session_interrupted", "reason": "user", "by": "user",
                           **turns.interrupted_fields(record)})
        suggestions = turns.suggestions_record(session, interrupted=record or {})
        if suggestions is not None:
            session.broadcast(suggestions)
            session.keep_suggestions = True
        try:
            await asyncio.to_thread(command_tasks.on_interrupt, session.project_id, session.conversation_id)
        except Exception:  # noqa: BLE001 - 任务收尾是增强信息
            pass

    # ---- 确认卡（引擎权限回调 → 前端待确认事件 → 中立决策）----

    async def _request_permission(
        self,
        session: Session,
        tool_name: str,
        tool_input: dict,
        title: str | None,
        description: str | None,
    ) -> dict:
        """确认卡（EC-30）与决策卡（AskUserQuestion → EC-32 question.asked）。等待期间界面显示「等你回答」。"""
        request = PendingPermission(
            id=uuid.uuid4().hex[:12],
            tool_name=tool_name,
            tool_input=tool_input,
            title=title,
            description=description,
            future=asyncio.get_running_loop().create_future(),
        )
        session.pending_permissions[request.id] = request
        timeout = config.PERMISSION_TIMEOUT_SECONDS
        if tool_name == timeline.ASK_TOOL:
            phase = ""
            if session.exec_id:
                from .session_host import exec_records

                record = exec_records.get(session.project_id, session.exec_id) or {}
                phase = (record.get("phase") or {}).get("label") or ""
            request.questions = timeline.build_questions(
                request.id, _safe_json(tool_input) if isinstance(tool_input, dict) else {},
                conversation_id=session.conversation_id, exec_id=session.exec_id, step_label=phase,
            )
            for question in request.questions:
                session.broadcast({"type": "question_asked", "payload": question})
                project_events.schedule_flow_refresh(session.project_id)  # 待处理里多一项决策
        else:
            session.broadcast(
                {
                    "type": "permission_request",
                    "request_id": request.id,
                    "tool_name": tool_name,
                    "tool_input": _safe_json(tool_input),
                    "title": request.title,
                    "description": request.description,
                    "timeout_s": timeout,
                    "expires_at": time.time() + timeout,
                }
            )
        session.show_state("waiting_user")
        by = "user"
        try:
            decision = await asyncio.wait_for(request.future, timeout=timeout)
        except asyncio.TimeoutError:
            decision = {"behavior": "deny", "message": "等待确认超时，已自动拒绝"}
            by = "timeout"
        finally:
            session.pending_permissions.pop(request.id, None)
        if request.questions:
            answered = {item.get("question_id") for item in request.answers.values()}
            for question in request.questions:
                if question["question_id"] in answered:
                    continue
                if decision.get("behavior") == "allow":  # 旧接口（S-10 带 answers）一次答完
                    session.broadcast({"type": "question_answered", "payload": {
                        "question_id": question["question_id"], "answer": {}, "by": "user", "summary": "已回答",
                        "answered_at": views_iso(time.time())}})
                    continue
                reason = "等待回答超时" if by == "timeout" else (decision.get("message") or "问题已失效")
                session.broadcast({"type": "question_withdrawn",
                                   "payload": {"question_id": question["question_id"], "reason": reason}})
        else:
            session.broadcast(
                {
                    "type": "permission_resolved",
                    "request_id": request.id,
                    "behavior": decision.get("behavior"),
                    "by": by,
                    "message": decision.get("message") or "",
                }
            )
        if session.state == "running":
            session.show_state("running")
        return decision

    def answer_question(self, project_id: str, question_id: str, answer: dict) -> dict:
        """回答决策卡（S-11）：一个提问里的问题都答完才把答案交给引擎。返回回答后的 Question。

        answer 是 {option_id?, option_ids?, custom_text?}；选项不存在抛 SessionError(400)，问题已不在抛 410。
        """
        session = self._sessions.get(project_id)
        request, question = None, None
        for pending in (session.pending_permissions.values() if session else ()):
            question = next((item for item in pending.questions if item["question_id"] == question_id), None)
            if question is not None:
                request = pending
                break
        if request is None or request.future.done() or question_id in request.answers:
            raise SessionError("问题已回答、撤回或失效", 410)
        chosen = [answer["option_id"]] if answer.get("option_id") else list(answer.get("option_ids") or [])
        custom = str(answer.get("custom_text") or "").strip()
        by_id = {option["id"]: option for option in question["options"]}
        if not chosen and not custom:
            raise SessionError("没有选择任何选项", 400)
        if any(option not in by_id for option in chosen):
            raise SessionError("选项不存在", 400)
        if any(by_id[option].get("custom") for option in chosen) and not custom:
            raise SessionError("选了「其他」要写上你的说法", 400)
        labels = [by_id[option]["label"] for option in chosen if not by_id[option].get("custom")]
        if custom:
            labels.append(custom)
        record = {"question_id": question_id, "text": "、".join(labels),
                  "answer": {key: answer[key] for key in ("option_id", "option_ids", "custom_text") if answer.get(key)}}
        request.answers[question_id] = record
        summary = timeline.answer_summary(question, answer)
        now = time.time()
        session.broadcast({"type": "question_answered", "payload": {
            "question_id": question_id, "answer": record["answer"], "by": "user", "summary": summary,
            "answered_at": views_iso(now),
        }})
        project_events.schedule_flow_refresh(project_id)
        if len(request.answers) == len(request.questions):
            raw = (request.tool_input or {}).get("questions") or []
            texts = {}
            for index, item in enumerate(request.questions):
                original = raw[index].get("question", item["title"]) if index < len(raw) and isinstance(raw[index], dict) else item["title"]
                texts[original] = request.answers[item["question_id"]]["text"]
            request.future.set_result({"behavior": "allow", "updated_input": {**request.tool_input, "answers": texts}})
        return {**question, "state": "answered", "answer": record["answer"]}

    # ---- 事件消费 ----

    async def _emit_workspace_changes(self, session: Session) -> None:
        """一轮结束后：助手在副本里改的进变更集，放「这一轮改了什么」卡（方案 C12：基点是变更集基点，
        在副本里算，不把你这段时间在 Studio 的保存算进来）；主线这时不变，确认生效才写主线。"""
        if not session.cwd:
            return
        from ..kernel.changesets import worktree

        if worktree.is_copy(session.cwd):
            await self._observe_changeset(session)
            return
        if not session.round_base_commit:
            return
        try:
            head = await asyncio.to_thread(_git_head, session.cwd)
            if not head or head == session.round_base_commit:
                return
            changes = await asyncio.to_thread(
                _git_changes, session.cwd, session.round_base_commit, head
            )
            # 审计：agent 这一轮的提交（不经 HTTP 的专项审计，方案 E4）
            await asyncio.to_thread(audit.record_agent_commits, session.project_id, session.cwd,
                                    session.round_base_commit, head, changes)
            if any(changes.values()):
                from . import mainline

                version_label = await asyncio.to_thread(
                    lambda: mainline.label(mainline.count(session.cwd, head)))
                session.broadcast(
                    {
                        "type": "workspace_changes",
                        **changes,
                        "from": session.round_base_commit,
                        "to": head,
                        "card_id": f"rc-{uuid.uuid4().hex[:10]}",
                        "version_label": version_label,
                    }
                )
                # 项目流 EP-05：agent 的提交不经写入事务，由宿主在这一轮结束时补发
                paths = [path for bucket in ("added", "modified", "deleted") for path in changes.get(bucket) or []]
                await asyncio.to_thread(
                    project_events.workspace_changed, session.project_id, commit=head, source="agent",
                    by=project_owner(session.project_id), paths=paths,
                )
            session.round_base_commit = head
        except Exception:
            pass  # 变更卡是增强信息，失败不影响会话

    async def _observe_changeset(self, session: Session, *, running: bool = False) -> None:
        """看一眼副本里的改动（一轮结束时带检查、出卡；跑的过程中节流，只更新写入范围）。"""
        from ..api import deps
        from ..kernel.changesets import service as changesets
        from .session_host import exec_records

        try:
            record_exec = exec_records.get(session.project_id, session.exec_id) if session.exec_id else None
            head = await asyncio.to_thread(_git_head, session.cwd)
            if not running and head and session.round_base_commit and head != session.round_base_commit:
                # 审计：agent 这一轮在副本里的提交（不经 HTTP 的专项审计，方案 E4）
                changes = await asyncio.to_thread(_git_changes, session.cwd, session.round_base_commit, head)
                await asyncio.to_thread(audit.record_agent_commits, session.project_id, session.cwd,
                                        session.round_base_commit, head, changes)
            record = await asyncio.to_thread(
                changesets.observe, deps.workspaces, session.project_id, running=running,
                conversation_id=session.conversation_id, exec_record=record_exec, checks=not running,
                throttle=running,
            )
            if not running:
                session.round_base_commit = head
                if record is not None:
                    record = await asyncio.to_thread(_capture_change_list, session, record) or record
            if running or record is None or record.get("card_tree") == record.get("tree"):
                return
            records = await asyncio.to_thread(changesets.card_records, deps.workspaces, session.project_id,
                                              {**record, "conversation_id": session.conversation_id}, new_card=True)
            await asyncio.to_thread(changesets.store.update, deps.workspaces.project_root(session.project_id),
                                    record["id"], card_tree=record.get("tree"))
            for conversation_id, item in records:
                if conversation_id == session.conversation_id:
                    session.broadcast(item)
                else:
                    self.append_record(session.project_id, conversation_id, item)
        except Exception:  # noqa: BLE001 - 变更卡是增强信息，失败不影响会话
            _log.warning("项目 %s 观察助手副本失败", session.project_id, exc_info=True)

    async def _read_loop(self, session: Session) -> None:
        engine = session.engine
        # 公共标签（详设 4.10.2）：读循环是独立任务，这里设的标签只作用于这一个会话的日志与指标
        context.set_label("project", session.project_id)
        context.set_label("session", session.conversation_id)
        # 不加 try：本任务只被 cancel 结束，引擎侧的错误走 _engine_end 事件
        async for event in engine.events():
            session.last_activity = time.time()
            kind = event.get("type")
            if kind == "_engine_end":
                # 引擎终止：closed（重启/终止）静默收尾，error 给用户出错卡
                if event.get("reason") == "closed":
                    _, card = await asyncio.to_thread(turns.interrupt_exec, session, "engine_exit", card=True)
                    if card is not None:
                        session.broadcast(card)
                    session.set_state("closed")
                else:
                    error_id = session.last_error_id or self._log_error(
                        session, "session", event.get("error") or FRIENDLY_ERROR)
                    self._turn_failed(session, error_id, event.get("error") or FRIENDLY_ERROR)
                # 引擎已自断开：释放槽位并唤醒排队者
                session.engine = None
                await self._drain_waiters()
                return
            if kind == "result":
                if await self._finish_turn(session, event, getattr(engine, "name", "")):
                    return
                continue
            # 宿主拆开引擎事件：助手正文、每个工具调用、「工作过程」组各一条记录（session_host/timeline）
            for record, transient in timeline.ingest(session.current_turn(), event):
                session.broadcast(record, transient=transient)
            if kind in ("assistant_message", "tool_results"):
                progress = await asyncio.to_thread(turns.observe, session)
                if progress is not None:
                    session.broadcast(progress)
                if kind == "tool_results" and session.cwd is not None:
                    from ..kernel.changesets import worktree

                    if worktree.is_copy(session.cwd):
                        # 跑的过程中节流地看副本：副本里已改的实体随时进写入范围（方案 C10）
                        await self._observe_changeset(session, running=True)

    async def _finish_turn(self, session: Session, event: dict, engine_name: str) -> bool:
        """一轮出 result：收起工作过程、记用量、产物变更卡、执行记录与任务收尾、下一步建议。

        返回 True 表示待重载的会话已断开，读循环结束。
        """
        failed = bool(event.get("is_error")) or str(event.get("subtype") or "").startswith("error")
        turn = session.turn
        if turn is not None and turn.steps and turn.state == "running":
            turn.finish("interrupted" if session.interrupting else "failed" if failed else "done")
            session.broadcast(turn.progress_record())
        session.broadcast({**event, "turn_id": turn.turn_id if turn is not None else f"t-{uuid.uuid4().hex[:10]}"})
        session.turn = None
        telemetry.record_turn(session.project_id, event, engine_name)
        session.turns_in_flight = max(0, session.turns_in_flight - 1)
        await self._emit_workspace_changes(session)
        session.retries = 0 if not failed else session.retries
        session.set_state("idle")
        # 待重载的会话先断开旧引擎（手头的轮次都出了 result），收尾的记账放在后面
        reload = session.reload_pending and session.turns_in_flight == 0
        if reload:
            await self._reload_after_turn(session)
        # 运行中收下的下一条消息：引擎接着跑它，这里开它的执行记录与这一轮（与收尾同一次线程往返）
        deferred = session.deferred.pop(0) if session.deferred else None
        for record in await asyncio.to_thread(self._after_turn, session, failed, deferred):
            if record.get("type") == "suggestions" and session.keep_suggestions:
                continue
            session.broadcast(record)
        session.keep_suggestions = False
        if deferred is not None:
            session.turn = timeline.Turn.new(session.turn_title)
            session.last_prompt = deferred[0]
        return reload

    @staticmethod
    def _after_turn(session: Session, failed: bool, deferred: tuple | None) -> list[dict]:
        """线程里做的收尾：执行记录、任务、下一步建议；接着开下一条（运行中收下的）消息。"""
        records = turns.end_turn(session, failed=failed, waiting_user=bool(session.pending_permissions))
        if deferred is not None:
            text, task_id = deferred
            turns.begin(session, text, task_id=task_id)
        return records

    def _turn_failed(self, session: Session, error_id: str, message: str) -> None:
        """这一轮出错（引擎异常结束、连不上）：工作过程收成出错，执行记为中断并放续跑卡，出错卡带错误编号。"""
        if session.turn is not None and session.turn.steps:
            session.turn.finish("failed")
            session.broadcast(session.turn.progress_record())
        session.turn = None
        session.deferred.clear()
        _, card = turns.interrupt_exec(session, "engine_exit", card=True)
        if card is not None:
            session.broadcast(card)
        from .session_host import command_tasks

        try:
            command_tasks.on_turn_end(session.project_id, session.conversation_id, failed=True, exec_bound=set())
        except Exception:  # noqa: BLE001 - 任务收尾是增强信息
            pass
        session.broadcast({
            "type": "session_error", "error": message, "error_id": error_id, "retries": session.retries,
            "retryable": bool(session.last_prompt), "actions": ["retry", "redirect"] if session.last_prompt else ["redirect"],
        })
        session.set_state("error", reason=message)

    async def _reload_after_turn(self, session: Session) -> None:
        """待重载的会话在本轮收尾后断开旧引擎；下一条消息按新配置 resume 重连。

        在读循环自己里调用，不能 cancel reader_task（那是当前任务），断开后由调用方返回。
        引擎内若还积压着没出 result 的消息，计数不为零，不会走到这里。
        """
        session.reload_pending = False
        engine, session.engine = session.engine, None
        if engine is not None:
            # 断开期间读循环可能被回收（close 等）取消：断开放进独立任务，不被一起打断
            stop = asyncio.ensure_future(_stop_engine(engine))
            self._tasks.add(stop)
            stop.add_done_callback(self._tasks.discard)
            await asyncio.shield(stop)
        await self._drain_waiters()


def usage_summary(project_id: str) -> dict:
    """项目累计模型用量（从事件历史统计），会话接口与运维页共用。

    汇总全部会话，加上已删除会话留存的用量事件。calls 是结束的轮数，token 含未结束轮里
    已逐次记下的调用（口径见 conversation_store.counted_usage）。不走 get_history：
    运维总览每次请求都会调到这里，不该顺带触发幽灵态自愈写盘。
    """
    session = manager.get(project_id)
    histories = [conversation_store.load_deleted_usage(project_id)]
    for conversation in conversation_store.list_conversations(project_id):
        if session and session.conversation_id == conversation["id"]:
            histories.append(session.events)
        else:
            histories.append(load_history(project_id, conversation["id"]))
    total_cost = 0.0
    output_tokens = 0
    input_tokens = 0
    calls = 0
    last_activity = None
    for event in (event for history in histories for event in history):
        ts = event.get("ts")
        if ts and (last_activity is None or ts > last_activity):
            last_activity = ts
        usage = conversation_store.counted_usage(event)
        if usage is None:
            continue
        if event.get("type") == "result":
            calls += 1
            total_cost += event.get("total_cost_usd") or 0
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


# 工作区里当前在线引擎实例的标识。工具箱 state_helper 据此把执行锁绑定到引擎实例，
# 而不是进程号：后端是不是 1 号进程（容器加不加 init）、引擎是 CLI 子进程还是进程内
# 运行，都不影响「同一会话可续写锁、换了实例旧锁即过期」的判断
ENGINE_MARKER = Path(".onto") / "run" / "engine.json"


def _assembly_dir(cwd: Path | None) -> Path | None:
    """项目 .claude 指向的装配目录（<数据目录>/assembled/<哈希>）；不是装配目录返回 None。"""
    if cwd is None:
        return None
    link = Path(cwd) / ".claude"
    if not os.path.lexists(link):
        return None
    real = Path(os.path.realpath(link))
    return real if real.parent.name == "assembled" and (real / "MANIFEST.json").is_file() else None


def _write_engine_marker(cwd: Path | None, engine: str, conversation_id: str, *, engine_id: str = "",
                         owner: str = "") -> None:
    # 工作区已不在（项目刚被删除）时不写：mkdir 会在原处重建出残留目录
    turns.write_identity_marker(cwd, engine=engine, engine_id=engine_id or uuid.uuid4().hex,
                                conversation_id=conversation_id, owner=owner)


def _agent_cwd(project_id: str, root: Path) -> Path:
    """这一轮助手的工作目录：变更集副本（方案 C7）；不是本体项目工作区（没有 git）时原样返回。"""
    root = Path(root)
    if not (root / ".git").is_dir():
        return root
    from ..api import deps
    from ..kernel.changesets import service as changesets
    from .flow import view

    copy = changesets.copy_for_turn(deps.workspaces, project_id, root)
    try:
        # 节点状态与下一步写进副本的 flow-state.json：这一轮的宿主注入、助手收尾的 suggest 都读它（方案 D15）
        view.build(root)
    except Exception:  # noqa: BLE001 - 算不出流程不挡消息
        _log.warning("项目 %s 一轮开始前算流程失败", project_id, exc_info=True)
    return copy


def _install_commit_hooks(cwd: Path | None) -> None:
    if cwd is None:
        return
    from ..kernel.changesets import worktree
    from ..kernel.writepath import agent_commits

    try:
        # 钩子装在主线仓库的配置里，副本（git worktree）共用
        agent_commits.install(Path(worktree.main_root(Path(cwd))))
    except Exception:  # noqa: BLE001 - 装不上时提交检查退回 PreToolUse，不挡会话
        pass


def _capture_change_list(session: Session, record: dict) -> dict | None:
    """增量更新的影响分析（副本里的 update-plan.json）收成变更清单，记下所在对话与变更集（契约 3.14、F3 补充）。"""
    from ..api import deps
    from ..kernel.changesets import store
    from .flow import change_lists

    root = deps.workspaces.project_root(session.project_id)
    captured = change_lists.capture(root, session.cwd, conversation_id=session.conversation_id,
                                    changeset_id=record["id"])
    if captured is None:
        return None
    return store.update(root, record["id"], change_list_id=captured["id"])


def _purge_changesets(project_id: str) -> None:
    """删除项目：变更集记录与助手副本目录一起清掉。"""
    from ..kernel.changesets import service as changesets

    try:
        from ..api import deps

        from .flow import state as node_state

        from .flow import change_lists

        root = Path(deps.workspaces.workspaces_dir) / project_id
        changesets.purge(root)
        node_state.purge(root)
        change_lists.purge(root)
    except Exception:  # noqa: BLE001 - 清不干净的由启动核对兜底
        _log.warning("清除项目 %s 的变更集失败", project_id, exc_info=True)


def project_owner(project_id: str) -> str:
    """项目所有者（方案 B3：会话的模型配置归属、agent 提交的 Onto-Author）；读不出时为空串。"""
    meta_path = Path(config.DATA_DIR) / "workspaces" / project_id / ".onto-web.json"
    try:
        return str(json.loads(meta_path.read_text(encoding="utf-8")).get("owner") or "")
    except (OSError, ValueError, AttributeError):
        return ""


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
