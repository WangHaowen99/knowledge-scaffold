"""项目流与用户流（API 契约 1.11、第 4.3、4.4 节；P-15、A-12）。

两条流的事件不进磁盘，靠快照接口重建（契约 8.1#12）：

- 每个项目、每个用户一个频道，事件 id 是 `<epoch>.<n>`：`epoch` 是本进程的启动标识，`n` 在频道内单调递增；
- 频道留最近 2000 条或 30 分钟的持久事件作回放缓冲；瞬时事件（`command.progress` 的中间百分比）不带 id、
  不进缓冲；
- 重连带 `Last-Event-ID`：缓冲里还有就补发之后的；epoch 不同（服务重启过）、已被挤出缓冲、或者
  订阅者处理太慢把队列撑满，先发一条 `stream.reset`，前端按 `resync` 重拉快照。

发布方可以在任意线程调用 `publish_*`（写入事务在线程池里提交）；推给订阅者经它所在事件循环的
`call_soon_threadsafe`，回放快照与登记订阅在同一把锁里做，不会漏发也不会重发。

对话流的持久事件在 JSONL 历史里（会话宿主），本模块只给它共用 SSE 帧、心跳、连接数限制。
"""

from __future__ import annotations

import asyncio
import json
import logging
import secrets
import threading
import time
from collections import deque
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

_log = logging.getLogger(__name__)

# 进程启动标识：重启后前端带来的旧 id 都对不上，按 epoch_changed 重置
EPOCH = f"{int(time.time()):x}{secrets.token_hex(2)}"
BUFFER_MAX = 2000
BUFFER_SECONDS = 30 * 60
QUEUE_MAX = 1000
HEARTBEAT_SECONDS = 15
RETRY_MS = 3000
MAX_STREAMS_PER_USER = 8

# 项目流 stream.reset 之后要重拉的快照（契约 1.11）
PROJECT_RESYNC = ("FL-01", "IN-01", "CS-06", "PL-12", "AB-05")
USER_RESYNC = ("P-01", "A-13")


def iso(moment: float) -> str:
    from datetime import datetime, timezone

    value = datetime.fromtimestamp(moment, timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


@dataclass(eq=False)
class Subscriber:
    loop: asyncio.AbstractEventLoop
    queue: asyncio.Queue = field(default_factory=asyncio.Queue)
    overflowed: bool = False

    def offer(self, envelope: dict) -> None:
        """在订阅者自己的事件循环里调用：队列满了记溢出，之后由流发一次 stream.reset。"""
        if self.queue.qsize() >= QUEUE_MAX:
            self.overflowed = True
            return
        self.queue.put_nowait(envelope)


class Channel:
    """一个项目或一个用户的事件频道：环形缓冲 + 在线订阅者。"""

    def __init__(self, kind: str, key: str):
        self.kind = kind
        self.key = key
        self._lock = threading.Lock()
        self._n = 0
        self._buffer: deque[tuple[int, float, dict]] = deque()
        self._subscribers: set[Subscriber] = set()

    def publish(self, event_type: str, data: dict, *, transient: bool = False,
                project_id: str | None = None) -> dict:
        now = time.time()
        envelope = {"type": event_type, "ts": iso(now), "project_id": project_id, "data": data}
        with self._lock:
            if not transient:
                self._n += 1
                envelope = {"id": f"{EPOCH}.{self._n}", **envelope}
                self._buffer.append((self._n, now, envelope))
                self._trim(now)
            subscribers = list(self._subscribers)
        for subscriber in subscribers:
            try:
                subscriber.loop.call_soon_threadsafe(subscriber.offer, envelope)
            except RuntimeError:  # 订阅者的事件循环已关闭：连接已断，摘掉
                self.unsubscribe(subscriber)
        return envelope

    def _trim(self, now: float) -> None:
        while self._buffer and (len(self._buffer) > BUFFER_MAX or now - self._buffer[0][1] > BUFFER_SECONDS):
            self._buffer.popleft()

    def open(self, last_event_id: str, loop: asyncio.AbstractEventLoop) -> tuple[Subscriber, list[dict], str | None]:
        """登记订阅并取回放：(订阅者, 要补发的事件, 重置原因或 None)。

        last_event_id 为空表示首次打开（前端先拉快照），不补发也不重置。
        """
        subscriber = Subscriber(loop)
        with self._lock:
            self._trim(time.time())
            replay, reason = self._replay(last_event_id)
            self._subscribers.add(subscriber)
        return subscriber, replay, reason

    def _replay(self, last_event_id: str) -> tuple[list[dict], str | None]:
        if not last_event_id:
            return [], None
        epoch, _, raw = last_event_id.rpartition(".")
        if epoch != EPOCH or not raw.isdigit():
            return [], "epoch_changed"
        last = int(raw)
        if last >= self._n:
            return [], None
        oldest = self._buffer[0][0] if self._buffer else self._n + 1
        if last + 1 < oldest:
            return [], "gap"
        return [envelope for n, _, envelope in self._buffer if n > last], None

    def unsubscribe(self, subscriber: Subscriber) -> None:
        with self._lock:
            self._subscribers.discard(subscriber)

    def subscriber_count(self) -> int:
        with self._lock:
            return len(self._subscribers)

    def last_id(self) -> str | None:
        with self._lock:
            return f"{EPOCH}.{self._n}" if self._n else None


_channels: dict[tuple[str, str], Channel] = {}
_channels_lock = threading.Lock()


def channel(kind: str, key: str) -> Channel:
    with _channels_lock:
        found = _channels.get((kind, key))
        if found is None:
            found = _channels[(kind, key)] = Channel(kind, key)
        return found


def project_channel(project_id: str) -> Channel:
    return channel("project", project_id)


def user_channel(username: str) -> Channel:
    return channel("user", username or "")


def publish_project(project_id: str, event_type: str, data: dict, *, transient: bool = False) -> dict:
    """往项目流发一条事件（任意线程可调）。"""
    return project_channel(project_id).publish(event_type, data, transient=transient, project_id=project_id)


def publish_user(username: str, event_type: str, data: dict, *, project_id: str | None = None) -> dict:
    return user_channel(username).publish(event_type, data, project_id=project_id)


def publish_everywhere(event_type: str, data: dict) -> None:
    """全局状态（维护模式）：已开过的项目流与用户流各发一条；本地模式的用户流键是空串。"""
    with _channels_lock:
        known = list(_channels.values())
    keys = {(item.kind, item.key) for item in known}
    if ("user", "") not in keys:
        known.append(user_channel(""))
    for item in known:
        item.publish(event_type, data, project_id=item.key if item.kind == "project" else None)


def forget_project(project_id: str) -> None:
    """项目删除后丢掉它的频道（在线订阅者的连接随之只剩心跳，前端已离开项目页）。"""
    with _channels_lock:
        _channels.pop(("project", project_id), None)


def reset_all() -> None:
    """测试用：清空全部频道。"""
    with _channels_lock:
        _channels.clear()
    _slots.clear()


# ---- 每个用户同时最多 8 条连接（契约 1.11） ----


class _Slots:
    def __init__(self):
        self._lock = threading.Lock()
        self._counts: dict[str, int] = {}

    def acquire(self, user: str) -> bool:
        with self._lock:
            count = self._counts.get(user, 0)
            if count >= MAX_STREAMS_PER_USER:
                return False
            self._counts[user] = count + 1
            return True

    def release(self, user: str) -> None:
        with self._lock:
            count = self._counts.get(user, 0) - 1
            if count > 0:
                self._counts[user] = count
            else:
                self._counts.pop(user, None)

    def count(self, user: str) -> int:
        with self._lock:
            return self._counts.get(user, 0)

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()


_slots = _Slots()


def acquire_slot(user: str) -> None:
    """占一个连接名额；超过上限抛 429 too_many_streams。"""
    if not _slots.acquire(user):
        from ..api.errors import ApiError

        raise ApiError("too_many_streams", f"同时打开的事件流不能超过 {MAX_STREAMS_PER_USER} 条", 429,
                       {"limit": MAX_STREAMS_PER_USER})


def release_slot(user: str) -> None:
    _slots.release(user)


def open_count(user: str) -> int:
    return _slots.count(user)


# ---- SSE 帧 ----


def frame(envelope: dict) -> str:
    head = f"id: {envelope['id']}\n" if envelope.get("id") else ""
    return f"{head}data: {json.dumps(envelope, ensure_ascii=False)}\n\n"


def control(event_type: str, data: dict, *, project_id: str | None = None, conversation_id: str | None = None) -> dict:
    """不带 id 的控制事件（ping、stream.reset、auth.expired）。"""
    envelope = {"type": event_type, "ts": iso(time.time()), "project_id": project_id, "data": data}
    if conversation_id is not None:
        envelope["conversation_id"] = conversation_id
    return envelope


async def channel_frames(
    item: Channel,
    subscriber: Subscriber,
    replay: list[dict],
    reset: str | None,
    *,
    resync: tuple[str, ...],
    expires_at: float | None = None,
    heartbeat: float = HEARTBEAT_SECONDS,
) -> AsyncIterator[str]:
    """项目流、用户流的帧序列：retry → （重置）→ 回放 → 实时事件与心跳；令牌到期发 auth.expired 后结束。"""
    project_id = item.key if item.kind == "project" else None
    sent = 0
    try:
        yield f"retry: {RETRY_MS}\n\n"
        if reset:
            yield frame(control("stream.reset", {"reason": reset, "resync": list(resync)}, project_id=project_id))
        for envelope in replay:
            sent = max(sent, _seq(envelope))
            yield frame(envelope)
        while True:
            if expires_at is not None and time.time() >= expires_at:
                yield frame(control("auth.expired", {}, project_id=project_id))
                return
            timeout = heartbeat if expires_at is None else max(0.0, min(heartbeat, expires_at - time.time()))
            try:
                envelope = await asyncio.wait_for(subscriber.queue.get(), timeout=timeout)
            except asyncio.TimeoutError:
                if expires_at is not None and time.time() >= expires_at:
                    continue
                yield frame(control("ping", {}, project_id=project_id))
                continue
            if subscriber.overflowed:
                # 队列撑满时丢过事件：先让前端重拉快照，再从当前这条接着推
                subscriber.overflowed = False
                yield frame(control("stream.reset", {"reason": "overflow", "resync": list(resync)}, project_id=project_id))
            n = _seq(envelope)
            if n and n <= sent:
                continue
            sent = max(sent, n)
            yield frame(envelope)
    finally:
        item.unsubscribe(subscriber)


def _seq(envelope: dict) -> int:
    raw = str(envelope.get("id") or "")
    _, _, tail = raw.rpartition(".")
    return int(tail) if tail.isdigit() else 0


def token_expiry(request) -> float | None:
    """当前请求令牌的到期时间（epoch 秒）；本地模式没有令牌，返回 None。"""
    claims = getattr(getattr(request, "state", None), "user", None) or {}
    exp = claims.get("exp") if isinstance(claims, dict) else None
    try:
        return float(exp) if exp else None
    except (TypeError, ValueError):
        return None


def last_event_id(request, query_value: str = "") -> str:
    return query_value or request.headers.get("last-event-id", "") or ""


def check_slot(user: str) -> None:
    """建流之前先看名额（429 要在响应头发出之前给）；真正占名额在流开始时。"""
    if open_count(user) >= MAX_STREAMS_PER_USER:
        acquire_slot(user)  # 抛 429


def event_stream(user: str, frames_factory: Callable[[], AsyncIterator[str]]):
    """SSE 响应：开始推送时占一个连接名额、登记订阅，连接断开时释放（含没开始就断开的情况）。"""
    from fastapi.responses import StreamingResponse

    check_slot(user)

    async def body():
        if not _slots.acquire(user):  # 检查之后、开始之前名额被并发的连接占满：直接关流，前端退避重连
            return
        try:
            async for chunk in frames_factory():
                yield chunk
        finally:
            release_slot(user)

    return StreamingResponse(body(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def channel_stream(request, item: Channel, last_id: str, *, user: str, resync: tuple[str, ...]):
    """项目流、用户流的 SSE 响应。"""
    expires_at = token_expiry(request)

    async def frames():
        subscriber, replay, reset = item.open(last_id, asyncio.get_running_loop())
        async for chunk in channel_frames(item, subscriber, replay, reset, resync=resync, expires_at=expires_at):
            yield chunk

    return event_stream(user, frames)
