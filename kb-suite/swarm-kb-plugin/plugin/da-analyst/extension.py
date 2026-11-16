"""JiuwenSwarm application plugin hosting the DeepAnalyze harness provider.

Swarm 0.2.7 (openjiuwen 0.1.19) only spawns its built-in harness providers,
so this plugin is the host: each conversation owns one DeepAnalyzeHarness
driven purely through HarnessProtocol (start / send / steer / abort / stop,
the interaction handler and a checkpoint sink for resuming a conversation).

A conversation outlives the page connection that started it: switching to
another Swarm page reloads the iframe, and a running analysis keeps going
until it ends and nobody has watched it for ``IDLE_STOP_S``. What the page
needs to redraw a conversation is kept as a transcript under
``DA_ANALYST_HISTORY_DIR`` (default ``~/.jiuwenswarm/da-analyst/history``),
which is also the history list.

Environment:
    DA_BASE_URL        e.g. http://da-engine:8192
    DA_ENGINE_SECRET   read by the provider (secret_env), never sent to the page
    DA_TENANT_ID       default "default"
    KB_SERVICE_URL / KBS_PLUGIN_KEY   to list knowledge bases for the scope picker
    DA_ANALYST_HISTORY_DIR            where conversation transcripts are kept
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, AsyncIterator, Awaitable, Callable, Coroutine

import httpx

from jiuwenswarm.extensions.sdk import (
    ApplicationPluginExtension,
    FrontendContribution,
    WebSocketRouteContribution,
)

logger = logging.getLogger(__name__)

# Engines the page can drive. Each speaks DA-java's /api/agents; its settings use the prefix
# (``<prefix>BASE_URL``, ``<prefix>ENGINE_SECRET``, ``<prefix>TENANT_ID``).
ENGINES: dict[str, dict[str, str]] = {
    "deepanalyze": {"label": "DeepAnalyze", "prefix": "DA_", "module": "deepanalyze_harness",
                    "provider": "DeepAnalyzeHarnessProvider", "default_url": ""},
    "loopforest": {"label": "Loop Forest", "prefix": "LF_", "module": "loopforest_harness",
                   "provider": "LoopForestHarnessProvider", "default_url": ""},
}
DEFAULT_ENGINE = "deepanalyze"

PLUGIN_ID = "da-analyst"
WS_PATH = "/ws/plugins/da-analyst"
MAX_TEXT = 20000
# an output assembled from deltas is kept up to this size in the transcript
MAX_TRANSCRIPT_OUTPUT = 200000
IDLE_STOP_S = 600
HISTORY_LIMIT = 50
TITLE_CHARS = 60
FLUSH_DELAY_S = 1.0
# the history list reads only this much of each transcript (the summary is written ahead of the events)
SUMMARY_HEAD_BYTES = 65536
CHECKPOINT_LIMIT = 200
# wire messages the page's reducer uses to redraw a conversation
RECORDED = frozenset({"output", "tool", "turn", "ask", "ask_cancelled", "state"})
SESSION_ID_RE = re.compile(r"[A-Za-z0-9_-]{1,64}")  # used with fullmatch

Send = Callable[[dict[str, Any]], Awaitable[None]]


class CheckpointStore:
    """In-process HarnessCheckpointSink keyed by host session id, keeping the most recent ones.

    Losing one only costs the resume hint: the DA session id is derived from
    the host session id anyway.
    """

    def __init__(self, limit: int = CHECKPOINT_LIMIT) -> None:
        self.latest: dict[str, Any] = {}
        self._limit = limit

    async def save(self, checkpoint: Any, *, reason: Any, expected_storage_revision: str | None = None) -> Any:
        from openjiuwen.harness_protocol import CheckpointSaveReceipt

        del reason, expected_storage_revision
        self.latest.pop(checkpoint.host_session_id, None)
        self.latest[checkpoint.host_session_id] = checkpoint
        while len(self.latest) > self._limit:
            del self.latest[next(iter(self.latest))]
        return CheckpointSaveReceipt(
            checkpoint_id=checkpoint.checkpoint_id, sequence=checkpoint.sequence,
            storage_revision=str(checkpoint.sequence),
        )

    def forget(self, host_session_id: str) -> None:
        self.latest.pop(host_session_id, None)


CHECKPOINTS = CheckpointStore()


def _clip(value: Any) -> Any:
    if isinstance(value, str) and len(value) > MAX_TEXT:
        return value[:MAX_TEXT] + "…"
    return value


def _plain(value: Any) -> Any:
    from openjiuwen.harness_protocol import json_value_to_builtin

    try:
        return json_value_to_builtin(value)
    except Exception:  # noqa: BLE001 - best effort for display
        return str(value)


def event_to_wire(envelope: Any) -> dict[str, Any] | None:
    """Project a HarnessEvent onto the small JSON shape the page renders."""

    from openjiuwen.harness_protocol import (
        ItemLifecycleEvent, OutputEvent, ProviderEvent, StateChangedEvent, TurnLifecycleEvent, UsageUpdatedEvent,
    )

    ev = envelope.event
    base = {"seq": envelope.sequence, "turn_id": envelope.turn_id, "item_id": envelope.item_id}
    if isinstance(ev, OutputEvent):
        return {**base, "t": "output", "channel": ev.channel.value, "op": ev.operation.value,
                "output_id": ev.output_id, "content": _clip(_plain(ev.content))}
    if isinstance(ev, ItemLifecycleEvent):
        data = _plain(ev.data) or {}
        if ev.item_type == "tool":
            return {**base, "t": "tool", "phase": ev.kind.value,
                    "name": data.get("name") or data.get("tool_name"),
                    "arguments": data.get("arguments"), "result": _clip(data.get("result"))}
        return {**base, "t": "item", "item_type": ev.item_type, "phase": ev.kind.value}
    if isinstance(ev, UsageUpdatedEvent):
        return {**base, "t": "usage", "mode": ev.mode.value,
                "input_tokens": ev.usage.input_tokens, "output_tokens": ev.usage.output_tokens,
                "total_tokens": ev.usage.total_tokens}
    if isinstance(ev, TurnLifecycleEvent):
        result = ev.result
        payload = {**base, "t": "turn", "kind": ev.kind.value}
        if result is not None:
            payload.update({
                "status": result.status.value,
                "final_output": _clip(_plain(result.final_output)),
                "duration_ms": result.duration_ms,
                "error": {"message": result.error.message, "code": result.error.code} if result.error else None,
                "termination": result.termination.kind.value if result.termination else None,
                "total_tokens": result.usage.total_tokens if result.usage else None,
            })
        return payload
    if isinstance(ev, StateChangedEvent):
        return {**base, "t": "state", "state": ev.new.value}
    if isinstance(ev, ProviderEvent):
        return {**base, "t": "provider", "event_type": ev.event_type, "payload": _plain(ev.payload)}
    return None


class PageInteractions:
    """HarnessInteractionHandler that asks the user on the page."""

    def __init__(self, send) -> None:  # noqa: ANN001
        self._send = send
        self._waiting: dict[str, tuple[asyncio.Future, Any]] = {}

    @property
    def pending(self) -> bool:
        """A question or approval is waiting for the page."""
        return any(not future.done() for future, _ in self._waiting.values())

    async def handle(self, request: Any) -> Any:
        from openjiuwen.harness_protocol import ToolApprovalRequest

        future: asyncio.Future = asyncio.get_running_loop().create_future()
        self._waiting[request.request_id] = (future, request)
        if isinstance(request, ToolApprovalRequest):
            await self._send({"t": "ask", "kind": "approval", "request_id": request.request_id,
                              "turn_id": request.turn_id, "prompt": request.title or "",
                              "tool_name": request.tool_name, "arguments": _plain(request.arguments)})
        else:
            await self._send({"t": "ask", "kind": "question", "request_id": request.request_id,
                              "turn_id": request.turn_id, "prompt": getattr(request, "prompt", ""),
                              "choices": list(getattr(request, "choices", ()) or ())})
        try:
            return await future
        finally:
            self._waiting.pop(request.request_id, None)

    def answer(self, request_id: str, msg: dict[str, Any]) -> bool:
        from openjiuwen.harness_protocol import (
            InteractionResponseStatus, ToolApprovalDecision, ToolApprovalRequest, ToolApprovalResponse,
            UserInputResponse,
        )

        entry = self._waiting.get(request_id)
        if entry is None:
            return False
        future, request = entry
        if future.done():
            return False
        if isinstance(request, ToolApprovalRequest):
            decision = {"allow": ToolApprovalDecision.ALLOW, "deny": ToolApprovalDecision.DENY,
                        "abort": ToolApprovalDecision.ABORT}.get(str(msg.get("decision")), ToolApprovalDecision.DENY)
            future.set_result(ToolApprovalResponse(request_id=request_id, decision=decision))
        elif msg.get("decline"):
            future.set_result(UserInputResponse(request_id=request_id, status=InteractionResponseStatus.DECLINED))
        else:
            future.set_result(UserInputResponse(
                request_id=request_id, status=InteractionResponseStatus.COMPLETED, content=str(msg.get("content") or ""),
            ))
        return True

    async def cancel(self, request_id: str, *, reason: Any = None) -> None:
        from openjiuwen.harness_protocol import (
            InteractionResponseStatus, ToolApprovalDecision, ToolApprovalRequest, ToolApprovalResponse,
            UserInputResponse,
        )

        entry = self._waiting.pop(request_id, None)
        if entry and not entry[0].done():
            # Answered as withdrawn, never cancelled: a CancelledError would escape the harness's
            # turn (it catches Exception) and then its stop(), leaving the turn without an end.
            future, request = entry
            if isinstance(request, ToolApprovalRequest):
                future.set_result(ToolApprovalResponse(request_id=request_id, decision=ToolApprovalDecision.ABORT))
            else:
                future.set_result(UserInputResponse(request_id=request_id, status=InteractionResponseStatus.CANCELLED))
        await self._send({"t": "ask_cancelled", "request_id": request_id,
                          "reason": getattr(reason, "value", str(reason))})


def _history_dir() -> Path:
    return Path(os.getenv("DA_ANALYST_HISTORY_DIR") or Path.home() / ".jiuwenswarm" / "da-analyst" / "history")


_BACKGROUND: set[asyncio.Task] = set()


def _background(coro: Coroutine[Any, Any, Any]) -> None:
    """Run a coroutine nobody awaits, keeping it referenced until it is done."""
    task = asyncio.ensure_future(coro)
    _BACKGROUND.add(task)
    task.add_done_callback(_BACKGROUND.discard)


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


class Transcript:
    """What the page needs to redraw one conversation, kept as a JSON file.

    Entries are the page's own inputs (``user``, ``steer``, ``answered``,
    ``rejected``) plus the recorded wire messages, in arrival order; the page
    replays them through the same reducer it uses live. Output deltas are
    folded into the first message of their output so a long answer stays one
    entry. Files are written off the event loop, at most once per
    ``FLUSH_DELAY_S``; once discarded, nothing writes the file again.
    """

    def __init__(self, host_session_id: str, engine: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        now = time.time()
        self.id = host_session_id
        self.engine = str(data.get("engine") or engine)
        self.title = str(data.get("title") or "")
        # the page's selection for the latest question; [] means every knowledge base
        self.kb_ids: list[str] = list(data.get("kb_ids") or [])
        self.created_at = float(data.get("created_at") or now)
        self.updated_at = float(data.get("updated_at") or now)
        self.events: list[dict[str, Any]] = list(data.get("events") or [])
        self._outputs = {e["output_id"]: e for e in self.events if e.get("t") == "output" and e.get("output_id")}
        self._flush_handle: asyncio.TimerHandle | None = None
        self._io = asyncio.Lock()
        self._sealed = False  # after the last save or a delete nothing writes the file again

    @staticmethod
    def path_for(host_session_id: str) -> Path:
        return _history_dir() / f"{host_session_id}.json"

    @classmethod
    def load(cls, host_session_id: str) -> Transcript | None:
        """Blocking read; call it through ``asyncio.to_thread``."""
        try:
            data = json.loads(cls.path_for(host_session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls(host_session_id, str(data.get("engine") or DEFAULT_ENGINE), data)

    @property
    def asked(self) -> bool:
        return any(e.get("t") == "user" for e in self.events)

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "engine": self.engine, "title": self.title, "kb_ids": self.kb_ids,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "turns": sum(1 for e in self.events if e.get("t") == "user")}

    def add_user(self, client_id: str, text: str, kb_ids: list[str]) -> dict[str, Any]:
        entry = {"t": "user", "client_id": client_id, "text": text}
        if not self.title:
            self.title = " ".join(text.split())[:TITLE_CHARS]
        self.kb_ids = list(kb_ids)
        self.add(entry)
        return entry

    def add(self, entry: dict[str, Any]) -> None:
        self.events.append(entry)
        self.touch()

    def record(self, wire: dict[str, Any]) -> None:
        if wire.get("t") not in RECORDED:
            return
        entry = {k: v for k, v in wire.items() if k != "seq"}
        if entry["t"] == "output":
            text = str(entry.get("content") or "")
            first = self._outputs.get(entry.get("output_id"))
            if first is not None:
                merged = first["content"] + text if entry.get("op") == "delta" else text
                first["content"] = merged[:MAX_TRANSCRIPT_OUTPUT]
                self.touch()
                return
            entry["content"] = text
            self._outputs[entry.get("output_id")] = entry
        self.add(entry)

    def touch(self) -> None:
        self.updated_at = time.time()
        if self._flush_handle is not None or self._sealed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # no loop: whoever owns it saves explicitly
            return
        self._flush_handle = loop.call_later(FLUSH_DELAY_S, lambda: _background(self.save()))

    async def save(self, *, last: bool = False) -> None:
        """Write the file now, unless nothing was asked yet or it is sealed; ``last`` seals it."""
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        async with self._io:
            if self._sealed or not self.asked:
                self._sealed = self._sealed or last
                return
            self._sealed = last
            # entries change in place (folded deltas, a question's turn id): copy them before leaving the loop
            data = {**self.summary(), "events": [dict(e) for e in self.events]}
            try:
                await asyncio.to_thread(_write_json, self.path_for(self.id), data)
            except OSError:
                logger.warning("[da-analyst] saving transcript %s failed", self.id, exc_info=True)

    async def discard(self) -> None:
        """Delete the file; a save already under way finishes first and no later one writes it back."""
        self._sealed = True
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        async with self._io:
            await asyncio.to_thread(_delete_files, self.id)


def _tombstone(host_session_id: str) -> Path:
    return _history_dir() / f"{host_session_id}.deleted"


def _delete_files(host_session_id: str) -> None:
    """Remove the transcript and remember the id, so a page still holding it gets a new conversation."""
    Transcript.path_for(host_session_id).unlink(missing_ok=True)
    tombstone = _tombstone(host_session_id)
    tombstone.parent.mkdir(parents=True, exist_ok=True)
    tombstone.touch()


def _read_summary(path: Path) -> dict[str, Any] | None:
    """The fields ``Transcript.save`` writes ahead of ``events``, without parsing the events."""
    try:
        with path.open("rb") as file:
            head = file.read(SUMMARY_HEAD_BYTES)
            # a quote opening a key is never escaped, so this cannot match inside a string value
            cut = head.find(b', "events": ')
            data = json.loads(head[:cut] + b"}" if cut >= 0 else head + file.read())
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _scan_history(limit: int) -> list[dict[str, Any]]:
    """Summaries of the newest transcript files (blocking; run it in a thread)."""
    found = []
    for path in _history_dir().glob("*.json"):
        try:
            found.append((path.stat().st_mtime, path))
        except OSError:  # deleted meanwhile
            continue
    items = []
    for _, path in sorted(found, reverse=True)[:limit]:
        data = _read_summary(path)
        if data is None:
            continue
        items.append({
            "id": path.stem, "engine": str(data.get("engine") or DEFAULT_ENGINE), "title": str(data.get("title") or ""),
            "kb_ids": list(data.get("kb_ids") or []), "created_at": float(data.get("created_at") or 0),
            "updated_at": float(data.get("updated_at") or 0), "turns": int(data.get("turns") or 0),
        })
    return items


async def list_history(limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    items = []
    for item in await asyncio.to_thread(_scan_history, limit):
        live = LIVE.get(item["id"])
        if live is not None:
            item = {**item, **live.transcript.summary()}  # newer than the file
        items.append({**item, "live": live is not None and not live.closed, "running": bool(live and live.running)})
    return items


class Conversation:
    """A harness cycle with its event pump; page connections attach and detach."""

    def __init__(self, transcript: Transcript) -> None:
        self.transcript = transcript
        self.host_session_id = transcript.id
        self.engine = transcript.engine
        self.harness: Any = None
        self.pump: asyncio.Task | None = None
        self.subscribers: set[Send] = set()
        self.interactions = PageInteractions(self.emit)
        self.running = False
        self.closed = False
        self._closed = asyncio.Event()
        self._idle_timer: asyncio.TimerHandle | None = None

    @property
    def busy(self) -> bool:
        """Still working; a turn waiting for an answer nobody may give does not count."""
        return self.running and not self.interactions.pending

    async def broadcast(self, payload: dict[str, Any], skip: Send | None = None) -> None:
        for send in list(self.subscribers):
            if send is skip:
                continue
            try:
                await send(payload)
            except Exception:  # noqa: BLE001 - that page is gone; its socket handler detaches it
                logger.debug("[da-analyst] dropping a page event", exc_info=True)

    async def emit(self, wire: dict[str, Any]) -> None:
        if wire.get("t") == "state":
            self.running = wire.get("state") == "running"
        self.transcript.record(wire)
        if not self.subscribers:
            self.schedule_idle_stop()  # also when a turn starts waiting for a page that is not there
        await self.broadcast(wire)

    def attach(self, send: Send) -> None:
        self.subscribers.add(send)
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None

    def detach(self, send: Send) -> None:
        self.subscribers.discard(send)
        if not self.subscribers:
            self.schedule_idle_stop()

    def schedule_idle_stop(self) -> None:
        # a busy analysis keeps going; the event that ends it (or makes it wait for the page) schedules the stop
        if self.closed or self.busy or self._idle_timer is not None:
            return

        def fire() -> None:
            self._idle_timer = None
            if not self.subscribers and not self.busy:
                _background(self.close())

        self._idle_timer = asyncio.get_running_loop().call_later(IDLE_STOP_S, fire)

    async def start(self) -> None:
        from openjiuwen.harness_protocol import HarnessContext, HostCapability, ResumePolicy

        engine = self.engine
        if engine not in ENGINES:
            raise ValueError(f"unknown engine: {engine}")
        provider = _provider_class(engine)
        self.harness = provider.create({
            "base_url": _engine_url(engine),
            "tenant_id": _engine_env(engine, "TENANT_ID", "default"),
            "session_prefix": "swarm-",
        })
        offered = frozenset({
            HostCapability.USER_INPUT, HostCapability.TOOL_APPROVAL, HostCapability.CHECKPOINT_SINK,
        })
        checkpoint = CHECKPOINTS.latest.get(self.host_session_id)
        context = HarnessContext(
            agent_name=engine,
            agent_id="swarm-da-analyst",
            host_session_id=self.host_session_id,
            system_prompt="",
            # only what the engine can use: Loop Forest never asks, so no question/approval channel
            host_capabilities=offered & provider().card.optional_host_capabilities,
            resume_policy=ResumePolicy.RESUME_IF_AVAILABLE if checkpoint else ResumePolicy.NEW,
            checkpoint=checkpoint,
            checkpoint_sink=CHECKPOINTS,
            interactions=self.interactions,
            # no session-wide scope: each question carries its own, and one without a list
            # ("every knowledge base", listing failed) leaves the choice to the harness's catalog
            metadata={},
        )
        try:
            await self.harness.start(context)
        except BaseException:
            self.harness = None
            raise
        cursor = self.harness.events()

        async def pump() -> None:
            try:
                async for envelope in cursor:
                    wire = event_to_wire(envelope)
                    if wire is not None:
                        await self.emit(wire)
            except Exception:  # noqa: BLE001 - harness stopped
                logger.debug("[da-analyst] event pump ended", exc_info=True)

        self.pump = asyncio.create_task(pump())

    async def send(self, text: str, kb_ids: list[str], client_id: str, steer: bool, origin: Send | None = None) -> Any:
        """Deliver a page input; ``origin`` (the page that sent it) already shows it, the other pages are told."""
        from openjiuwen.harness_protocol import DeliveryMode, HarnessInput

        if self.harness is None:
            raise RuntimeError("会话尚未开始")
        metadata: dict[str, Any] = {}
        if not steer:
            # an empty selection means every knowledge base; without a list the harness uses its own catalog
            scope = list(kb_ids) or [kb["id"] for kb in await _list_kbs()]
            if scope:
                metadata["kb_ids"] = scope
        harness = self.harness
        if harness is None:
            raise RuntimeError("会话已结束")
        if steer:
            # kept only once accepted: a steer that finds no running turn leaves no trace
            receipt = await harness.send(HarnessInput(content=text), mode=DeliveryMode.STEER)
            entry = {"t": "steer", "text": text}
            self.transcript.add(entry)
            await self.broadcast(entry, skip=origin)
            return receipt
        entry = self.transcript.add_user(client_id, text, kb_ids)
        await self.broadcast(dict(entry), skip=origin)
        try:
            receipt = await harness.send(HarnessInput(content=text, metadata=metadata), mode=DeliveryMode.AUTO)
        except Exception as exc:
            rejected = {"t": "rejected", "client_id": client_id, "message": str(exc)[:500]}
            self.transcript.add(rejected)
            await self.broadcast(rejected, skip=origin)
            raise
        entry["turn_id"] = receipt.turn_id
        return receipt

    async def answer(self, request_id: str, msg: dict[str, Any], origin: Send | None = None) -> None:
        if self.interactions.answer(request_id, msg):
            entry = {"t": "answered", "request_id": request_id, "label": str(msg.get("label") or "")}
            self.transcript.add(entry)
            await self.broadcast(entry, skip=origin)

    async def close(self) -> None:
        """Stop the harness and save the transcript; a second call waits for the first.

        The conversation stays in ``LIVE`` until it is saved, so opening or
        deleting it meanwhile waits here instead of racing the last write.
        """
        if self.closed:
            await self._closed.wait()
            return
        self.closed = True
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        harness, self.harness = self.harness, None
        try:
            if harness is not None:
                if self.running:
                    # a stop only closes the stream; aborting first tells the engine to drop the task
                    await _quietly(harness.abort, "abort")
                await _quietly(harness.stop, "stop")
        finally:
            # whatever the stop did, the pump goes and the transcript gets its last save
            pump, self.pump = self.pump, None
            if pump is not None:
                pump.cancel()
            self.running = False
            try:
                await self.transcript.save(last=True)
            finally:
                if LIVE.get(self.host_session_id) is self:
                    del LIVE[self.host_session_id]
                self._closed.set()


# opening or deleting a conversation waits for its close, so a stuck engine may not hold it forever
CLOSE_STEP_TIMEOUT_S = 30


async def _quietly(command: Callable[[], Awaitable[Any]], name: str) -> None:
    """Run a harness command while closing; nothing it raises may skip the rest of the close."""
    try:
        await asyncio.wait_for(command(), CLOSE_STEP_TIMEOUT_S)
    except asyncio.CancelledError:
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            raise  # this close itself is being cancelled
        # raised inside the harness (e.g. by one of its turns), not a cancellation of ours
        logger.warning("[da-analyst] harness %s was cut short", name, exc_info=True)
    except Exception:  # noqa: BLE001
        logger.warning("[da-analyst] harness %s failed", name, exc_info=True)


LIVE: dict[str, Conversation] = {}


class KeyedLocks:
    """One asyncio.Lock per key, dropped once nobody holds or waits for it."""

    def __init__(self) -> None:
        self._locks: dict[str, tuple[asyncio.Lock, int]] = {}

    @contextlib.asynccontextmanager
    async def hold(self, key: str) -> AsyncIterator[None]:
        lock, users = self._locks.get(key) or (asyncio.Lock(), 0)
        self._locks[key] = (lock, users + 1)
        try:
            async with lock:
                yield
        finally:
            lock, users = self._locks[key]
            if users > 1:
                self._locks[key] = (lock, users - 1)
            else:
                del self._locks[key]


_OPENING = KeyedLocks()


class ConversationUnavailable(Exception):
    """The harness did not start; the transcript can still be shown."""

    def __init__(self, transcript: Transcript, cause: Exception) -> None:
        super().__init__(str(cause))
        self.transcript = transcript


async def open_conversation(host_session_id: str, engine: str) -> tuple[Conversation, bool]:
    """The live conversation for this id, or a new harness that continues its transcript.

    Returns the conversation and whether it was already live (its open turns are
    still going) rather than restarted from its transcript. The DA
    session id is derived from the host session id, so a restarted harness
    keeps DA's context as long as DA still has it; for the same reason a
    deleted id is never reused. Raises ``ConversationUnavailable`` when the
    engine does not start.
    """
    if not SESSION_ID_RE.fullmatch(host_session_id or ""):
        host_session_id = uuid.uuid4().hex
    async with _OPENING.hold(host_session_id):
        live = LIVE.get(host_session_id)
        if live is not None:
            if not live.closed:
                return live, True
            await live.close()  # still saving: continue from what it wrote
        if await asyncio.to_thread(_tombstone(host_session_id).exists):
            return await open_conversation("", engine)
        transcript = await asyncio.to_thread(Transcript.load, host_session_id) or Transcript(host_session_id, engine)
        conversation = Conversation(transcript)
        try:
            await conversation.start()
        except Exception as exc:
            raise ConversationUnavailable(transcript, exc) from exc
        LIVE[host_session_id] = conversation
        conversation.schedule_idle_stop()  # unless a page attaches
        return conversation, False


async def delete_conversation(host_session_id: str) -> None:
    if not SESSION_ID_RE.fullmatch(host_session_id or ""):
        return
    async with _OPENING.hold(host_session_id):
        CHECKPOINTS.forget(host_session_id)
        live = LIVE.get(host_session_id)
        if live is None:
            await asyncio.to_thread(_delete_files, host_session_id)
            return
        # other pages showing it move on to a new conversation
        await live.broadcast({"t": "deleted", "host_session_id": host_session_id})
        await live.transcript.discard()
        await live.close()


def _started(transcript: Transcript) -> dict[str, Any]:
    """What the page needs to show a conversation it opens."""
    return {"t": "started", "host_session_id": transcript.id, "engine": transcript.engine,
            "resumed": bool(transcript.events), "title": transcript.title,
            # a new conversation keeps whatever the page has selected
            "kb_ids": transcript.kb_ids if transcript.asked else None,
            "transcript": transcript.events}


def _provider_class(engine: str) -> Any:
    """The engine's provider: pip-installed (Swarm 0.2.7 image) or a Swarm custom agent (develop)."""
    spec = ENGINES[engine]
    try:
        module = importlib.import_module(spec["module"])
    except ImportError:
        from jiuwenswarm.common.custom_agent_runtime import activate_custom_agent_paths

        activate_custom_agent_paths()
        module = importlib.import_module(spec["module"])
    return getattr(module, spec["provider"])


def _engine_env(engine: str, name: str, default: str = "") -> str:
    return os.getenv(ENGINES[engine]["prefix"] + name, default)


def _engine_url(engine: str) -> str:
    return _engine_env(engine, "BASE_URL", ENGINES[engine]["default_url"]).rstrip("/")


async def _catalog() -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Knowledge bases for the scope picker, and the backends whose knowledge bases are missing from it.

    Each unavailable backend comes with its id prefix ("" for the one without, None when unknown) and the
    knowledge bases kb-gateway last saw there, so the page keeps a selection made on it.
    """
    base = os.getenv("KB_SERVICE_URL", "").rstrip("/")
    if not base:
        logger.warning("[da-analyst] KB_SERVICE_URL is not set, no knowledge bases to offer")
        return [], []
    key = os.getenv("KBS_PLUGIN_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.get(f"{base}/v1/kbs", headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            data = resp.json()
            kbs = [{"id": kb["id"], "name": kb["name"], "documents": kb["stats"]["completed"],
                    "backend": (kb.get("backend") or {}).get("label") or ""}
                   for kb in data.get("items", [])]
            return kbs, [_unlisted(b) for b in data.get("unavailable_backends") or [] if b.get("label") or b.get("id")]
    except (httpx.HTTPError, ValueError, KeyError, AttributeError):
        logger.warning("[da-analyst] listing knowledge bases failed", exc_info=True)
        return [], []


def _unlisted(backend: dict[str, Any]) -> dict[str, Any]:
    prefix = backend.get("id_prefix")
    seen = [{"id": str(kb["id"]), "name": str(kb.get("name") or kb["id"])}
            for kb in backend.get("knowledge_bases") or [] if isinstance(kb, dict) and kb.get("id")]
    return {"label": str(backend.get("label") or backend.get("id")), "id_prefix": prefix if isinstance(prefix, str) else None,
            "knowledge_bases": seen}


async def _list_kbs() -> list[dict[str, Any]]:
    """What "every knowledge base" covers for a question.

    The listed ones, plus those last seen on a backend that is down: kb-gateway's tools then say they were not
    searched, instead of the question silently covering less.
    """
    kbs, down = await _catalog()
    listed = {kb["id"] for kb in kbs}
    return kbs + [{"id": kb["id"], "name": kb["name"], "documents": 0, "backend": b["label"]}
                  for b in down for kb in b["knowledge_bases"] if kb["id"] not in listed]


async def _engine_ready(engine: str) -> bool:
    try:
        async with httpx.AsyncClient(timeout=5, trust_env=False) as client:
            resp = await client.get(f"{_engine_url(engine)}/ready",
                                    headers={"X-Engine-Secret": _engine_env(engine, "ENGINE_SECRET")})
            return resp.status_code < 400
    except httpx.HTTPError:
        return False


async def _engines() -> list[dict[str, Any]]:
    """Engines configured here (a base URL) whose provider is installed, with readiness."""
    found = []
    for engine, spec in ENGINES.items():
        if not _engine_url(engine):
            continue
        try:
            _provider_class(engine)
        except Exception:  # noqa: BLE001 - provider not installed in this Swarm build
            continue
        found.append({"id": engine, "label": spec["label"], "ready": await _engine_ready(engine)})
    return found


async def handshake_denied(websocket: Any) -> bool:
    """Apply Swarm's own login check to this route; True when the connection was refused.

    Swarm checks a handshake (``WebChannel.authenticate_handshake``, set up when Swarm login is on) only for
    its main ``/ws``, not for plugin routes, so the plugin asks the same hook. Without Swarm login there is
    no hook and nothing changes; foreign Origins are refused at the edge (deploy/compose/edge/nginx.conf).
    Kept the same as kb-manager's: Swarm installs each plugin directory on its own.
    """

    scope = getattr(websocket, "scope", None) or {}
    channel = getattr(getattr(scope.get("app"), "state", None), "web_channel", None)
    authenticate = getattr(channel, "authenticate_handshake", None)
    if authenticate is None:
        return False
    client = scope.get("client") or ("", "")
    query = scope.get("query_string", b"").decode("latin-1")
    path = scope.get("path", "") + (f"?{query}" if query else "")
    try:
        result = await authenticate(path=path, headers=websocket.headers, remote=f"{client[0]}:{client[1]}" if client[0] else "",
                                    channel="web")
        allowed = result is None or bool(getattr(result, "success", True))
    except Exception:  # noqa: BLE001 - a login check that fails refuses the connection
        logger.warning("[%s] handshake check failed", PLUGIN_ID, exc_info=True)
        allowed = False
    if not allowed:
        await websocket.close(code=1008)
    return not allowed


class DaAnalystPlugin(ApplicationPluginExtension):
    plugin_id = PLUGIN_ID

    async def initialize(self, config: Any) -> None:
        del config

    async def shutdown(self) -> None:
        return None

    def frontend_contributions(self) -> tuple[FrontendContribution, ...]:
        return (
            FrontendContribution(
                id="da-analyst-page", nav_key=f"app:{PLUGIN_ID}", title="知识求索",
                render_mode="iframe", entrypoint="da.html", position=21,
            ),
        )

    def websocket_routes(self) -> tuple[WebSocketRouteContribution, ...]:
        return (WebSocketRouteContribution(path=WS_PATH, endpoint=self._serve),)

    async def _serve(self, websocket: Any) -> None:
        if await handshake_denied(websocket):
            return
        await websocket.accept()
        lock = asyncio.Lock()
        gone = False

        async def send(payload: dict[str, Any]) -> None:
            if gone:
                return
            # serialized here, not once the socket is free: a snapshot must not pick up later changes
            text = json.dumps(payload, ensure_ascii=False, default=str)
            async with lock:
                await websocket.send_text(text)

        convo: Conversation | None = None
        tasks: set[asyncio.Task] = set()
        # Session-shaping messages run in arrival order (a `send` right behind a
        # `start` must see the new harness); abort/answer never wait behind them.
        ordered = asyncio.Lock()

        async def handle(msg: dict[str, Any]) -> None:
            if msg.get("type") in ("abort", "answer"):
                await dispatch(msg)
            else:
                async with ordered:
                    await dispatch(msg)

        def current() -> Conversation:
            if convo is None or convo.harness is None:
                raise RuntimeError("会话尚未开始")
            return convo

        def leave() -> None:
            nonlocal convo
            if convo is not None:
                convo.detach(send)
                convo = None

        async def dispatch(msg: dict[str, Any]) -> None:
            nonlocal convo
            kind = msg.get("type")
            try:
                if kind == "hello":
                    engines = await _engines()
                    kbs, unavailable = await _catalog()
                    # unavailable: backends whose knowledge bases are not offered (and not searched) right now
                    await send({"t": "hello", "kbs": kbs, "unavailable": unavailable, "engines": engines,
                                "da_ready": any(e["ready"] for e in engines if e["id"] == DEFAULT_ENGINE)})
                elif kind == "start":
                    try:
                        opened, was_live = await open_conversation(
                            str(msg.get("host_session_id") or ""), str(msg.get("engine") or DEFAULT_ENGINE))
                    except ConversationUnavailable as exc:
                        leave()
                        if exc.transcript.events:
                            # still readable; the page cannot send until the engine is back
                            await send({**_started(exc.transcript), "live": False, "running": False, "readonly": True})
                        raise
                    if gone:
                        return  # the page left meanwhile; the idle stop takes care of it
                    if convo is not opened:
                        leave()
                    convo = opened
                    # attach and snapshot without yielding, so every later event follows `started`
                    opened.attach(send)
                    await send({**_started(opened.transcript), "resumed": was_live or bool(opened.transcript.events),
                                "live": was_live, "running": opened.running,
                                "provider_session_id": opened.harness.provider_session_id})
                elif kind == "send":
                    receipt = await current().send(str(msg.get("text") or ""), list(msg.get("kb_ids") or []),
                                                   str(msg.get("client_id") or ""), bool(msg.get("steer")), send)
                    await send({"t": "accepted", "turn_id": receipt.turn_id, "mode": receipt.accepted_mode.value,
                                "client_id": msg.get("client_id")})
                elif kind == "abort":
                    await current().harness.abort()
                elif kind == "answer":
                    await current().answer(str(msg.get("request_id") or ""), msg, send)
                elif kind == "history":
                    await send({"t": "history", "items": await list_history()})
                elif kind == "delete":
                    target = str(msg.get("host_session_id") or "")
                    if convo is not None and convo.host_session_id == target:
                        leave()
                    await delete_conversation(target)
                    await send({"t": "history", "items": await list_history()})
                elif kind == "reset":
                    if convo is not None:
                        await convo.close()
                        convo = None
                    await send({"t": "reset"})
            except Exception as exc:  # noqa: BLE001 - report to the page, keep the socket
                logger.warning("[da-analyst] %s failed", kind, exc_info=True)
                with contextlib.suppress(Exception):
                    await send({"t": "error", "request": kind, "message": str(exc)[:500],
                                "client_id": msg.get("client_id")})

        try:
            while True:
                text = await websocket.receive_text()
                try:
                    msg = json.loads(text)
                except ValueError:
                    continue
                task = asyncio.create_task(handle(msg))
                tasks.add(task)
                task.add_done_callback(tasks.discard)
        except Exception:  # noqa: BLE001 - disconnect
            pass
        finally:
            # What the page already asked for still completes (a question it sent, a
            # delete); only replies are dropped. The conversation keeps running for
            # whoever opens it next.
            gone = True
            leave()


async def register_extensions(registry: Any) -> list[DaAnalystPlugin]:
    plugin = DaAnalystPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
