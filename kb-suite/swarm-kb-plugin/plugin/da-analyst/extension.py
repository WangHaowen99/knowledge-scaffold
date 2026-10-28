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
import importlib
import json
import logging
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any, Awaitable, Callable

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
# wire messages the page's reducer uses to redraw a conversation
RECORDED = frozenset({"output", "tool", "turn", "ask", "ask_cancelled", "state"})
SESSION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")

Send = Callable[[dict[str, Any]], Awaitable[None]]


class CheckpointStore:
    """In-process HarnessCheckpointSink keyed by host session id."""

    def __init__(self) -> None:
        self.latest: dict[str, Any] = {}

    async def save(self, checkpoint: Any, *, reason: Any, expected_storage_revision: str | None = None) -> Any:
        from openjiuwen.harness_protocol import CheckpointSaveReceipt

        del reason, expected_storage_revision
        self.latest[checkpoint.host_session_id] = checkpoint
        return CheckpointSaveReceipt(
            checkpoint_id=checkpoint.checkpoint_id, sequence=checkpoint.sequence,
            storage_revision=str(checkpoint.sequence),
        )


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
        entry = self._waiting.pop(request_id, None)
        if entry and not entry[0].done():
            entry[0].cancel()
        await self._send({"t": "ask_cancelled", "request_id": request_id,
                          "reason": getattr(reason, "value", str(reason))})


def _history_dir() -> Path:
    return Path(os.getenv("DA_ANALYST_HISTORY_DIR") or Path.home() / ".jiuwenswarm" / "da-analyst" / "history")


class Transcript:
    """What the page needs to redraw one conversation, kept as a JSON file.

    Entries are the page's own inputs (``user``, ``steer``, ``answered``,
    ``rejected``) plus the recorded wire messages, in arrival order; the page
    replays them through the same reducer it uses live. Output deltas are
    folded into the first message of their output so a long answer stays one
    entry.
    """

    def __init__(self, host_session_id: str, engine: str, data: dict[str, Any] | None = None) -> None:
        data = data or {}
        now = time.time()
        self.id = host_session_id
        self.engine = str(data.get("engine") or engine)
        self.title = str(data.get("title") or "")
        self.kb_ids: list[str] = list(data.get("kb_ids") or [])
        self.created_at = float(data.get("created_at") or now)
        self.updated_at = float(data.get("updated_at") or now)
        self.events: list[dict[str, Any]] = list(data.get("events") or [])
        self._outputs = {e["output_id"]: e for e in self.events if e.get("t") == "output" and e.get("output_id")}
        self._flush_handle: asyncio.TimerHandle | None = None

    @staticmethod
    def path_for(host_session_id: str) -> Path:
        return _history_dir() / f"{host_session_id}.json"

    @classmethod
    def load(cls, host_session_id: str) -> Transcript | None:
        try:
            data = json.loads(cls.path_for(host_session_id).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return cls(host_session_id, str(data.get("engine") or DEFAULT_ENGINE), data)

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
        if self._flush_handle is None:
            try:
                self._flush_handle = asyncio.get_running_loop().call_later(1.0, self.flush)
            except RuntimeError:  # no loop (tests, shutdown): write now
                self.flush()

    def flush(self) -> None:
        if self._flush_handle is not None:
            self._flush_handle.cancel()
            self._flush_handle = None
        if not any(e.get("t") == "user" for e in self.events):
            return  # nothing asked yet: not worth a history entry
        path = self.path_for(self.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_text(json.dumps({**self.summary(), "events": self.events}, ensure_ascii=False, default=str),
                           encoding="utf-8")
            os.replace(tmp, path)
        except OSError:
            logger.warning("[da-analyst] saving transcript %s failed", self.id, exc_info=True)


def list_history(limit: int = HISTORY_LIMIT) -> list[dict[str, Any]]:
    items = []
    for path in sorted(_history_dir().glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
        transcript = Transcript.load(path.stem)
        if transcript is not None:
            live = LIVE.get(transcript.id)
            items.append({**transcript.summary(), "live": live is not None, "running": bool(live and live.running)})
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
        self._idle_timer: asyncio.TimerHandle | None = None

    async def broadcast(self, payload: dict[str, Any]) -> None:
        for send in list(self.subscribers):
            try:
                await send(payload)
            except Exception:  # noqa: BLE001 - that page is gone; its socket handler detaches it
                logger.debug("[da-analyst] dropping a page event", exc_info=True)

    async def emit(self, wire: dict[str, Any]) -> None:
        if wire.get("t") == "state":
            self.running = wire.get("state") == "running"
            if not self.running and not self.subscribers:
                self.schedule_idle_stop()
        self.transcript.record(wire)
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
        # a running analysis keeps going; the `state` event that ends it schedules the stop
        if self.running or self._idle_timer is not None:
            return

        def fire() -> None:
            self._idle_timer = None
            if not self.subscribers and not self.running:
                asyncio.ensure_future(self.close())

        self._idle_timer = asyncio.get_running_loop().call_later(IDLE_STOP_S, fire)

    async def start(self, kb_ids: list[str]) -> None:
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
            metadata={"kb_ids": list(kb_ids)},
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

    async def send(self, text: str, kb_ids: list[str], client_id: str, steer: bool) -> Any:
        from openjiuwen.harness_protocol import DeliveryMode, HarnessInput

        if self.harness is None:
            raise RuntimeError("会话尚未开始")
        entry = None
        if steer:
            self.transcript.add({"t": "steer", "text": text})
        else:
            entry = self.transcript.add_user(client_id, text, kb_ids)
        content = HarnessInput(content=text, metadata={"kb_ids": list(kb_ids)})
        try:
            receipt = await self.harness.send(content, mode=DeliveryMode.STEER if steer else DeliveryMode.AUTO)
        except Exception as exc:
            if entry is not None:
                self.transcript.add({"t": "rejected", "client_id": client_id, "message": str(exc)[:500]})
            raise
        if entry is not None:
            entry["turn_id"] = receipt.turn_id
        return receipt

    def answer(self, request_id: str, msg: dict[str, Any]) -> None:
        if self.interactions.answer(request_id, msg):
            self.transcript.add({"t": "answered", "request_id": request_id, "label": str(msg.get("label") or "")})

    async def close(self) -> None:
        if LIVE.get(self.host_session_id) is self:
            del LIVE[self.host_session_id]
        if self._idle_timer is not None:
            self._idle_timer.cancel()
            self._idle_timer = None
        harness, self.harness = self.harness, None
        if harness is not None:
            try:
                await harness.stop()
            except Exception:  # noqa: BLE001
                logger.warning("[da-analyst] harness stop failed", exc_info=True)
        pump, self.pump = self.pump, None
        if pump is not None:
            pump.cancel()
        self.running = False
        self.transcript.flush()


LIVE: dict[str, Conversation] = {}

_OPENING: dict[str, asyncio.Lock] = {}


async def open_conversation(host_session_id: str, engine: str, kb_ids: list[str]) -> tuple[Conversation, bool]:
    """The live conversation for this id, or a new harness that continues its transcript.

    Returns the conversation and whether it was already live (its open turns are
    still going) rather than restarted from its transcript. The DA
    session id is derived from the host session id, so a restarted harness
    keeps DA's context as long as DA still has it.
    """
    if not SESSION_ID_RE.match(host_session_id or ""):
        host_session_id = uuid.uuid4().hex
    async with _OPENING.setdefault(host_session_id, asyncio.Lock()):
        live = LIVE.get(host_session_id)
        if live is not None and live.harness is not None:
            return live, True
        transcript = Transcript.load(host_session_id) or Transcript(host_session_id, engine)
        conversation = Conversation(transcript)
        await conversation.start(kb_ids)
        LIVE[host_session_id] = conversation
        conversation.schedule_idle_stop()  # unless a page attaches
        return conversation, False


async def delete_conversation(host_session_id: str) -> None:
    if not SESSION_ID_RE.match(host_session_id or ""):
        return
    live = LIVE.get(host_session_id)
    if live is not None:
        await live.close()
    Transcript.path_for(host_session_id).unlink(missing_ok=True)


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


async def _list_kbs() -> list[dict[str, Any]]:
    base = os.getenv("KB_SERVICE_URL", "").rstrip("/")
    if not base:
        logger.warning("[da-analyst] KB_SERVICE_URL is not set, no knowledge bases to offer")
        return []
    key = os.getenv("KBS_PLUGIN_KEY", "")
    try:
        async with httpx.AsyncClient(timeout=10, trust_env=False) as client:
            resp = await client.get(f"{base}/v1/kbs", headers={"Authorization": f"Bearer {key}"})
            resp.raise_for_status()
            return [{"id": kb["id"], "name": kb["name"], "documents": kb["stats"]["completed"]}
                    for kb in resp.json().get("items", [])]
    except (httpx.HTTPError, ValueError, KeyError):
        logger.warning("[da-analyst] listing knowledge bases failed", exc_info=True)
        return []


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
        await websocket.accept()
        lock = asyncio.Lock()

        async def send(payload: dict[str, Any]) -> None:
            async with lock:
                await websocket.send_text(json.dumps(payload, ensure_ascii=False, default=str))

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

        async def dispatch(msg: dict[str, Any]) -> None:
            nonlocal convo
            kind = msg.get("type")
            try:
                if kind == "hello":
                    engines = await _engines()
                    await send({"t": "hello", "kbs": await _list_kbs(), "engines": engines,
                                "da_ready": any(e["ready"] for e in engines if e["id"] == DEFAULT_ENGINE)})
                elif kind == "start":
                    opened, was_live = await open_conversation(
                        str(msg.get("host_session_id") or ""), str(msg.get("engine") or DEFAULT_ENGINE),
                        list(msg.get("kb_ids") or []))
                    if convo is not None and convo is not opened:
                        convo.detach(send)
                    convo = opened
                    # attach and snapshot without yielding, so every later event follows `started`
                    opened.attach(send)
                    transcript = opened.transcript
                    await send({"t": "started", "host_session_id": opened.host_session_id, "engine": opened.engine,
                                "resumed": was_live or bool(transcript.events), "live": was_live,
                                "provider_session_id": opened.harness.provider_session_id,
                                "title": transcript.title, "kb_ids": transcript.kb_ids,
                                "running": opened.running, "transcript": list(transcript.events)})
                elif kind == "send":
                    steer = bool(msg.get("steer"))
                    kb_ids = list(msg.get("kb_ids") or [])
                    if not kb_ids and not steer:
                        # the page shows an empty selection as "all knowledge bases"
                        kb_ids = [kb["id"] for kb in await _list_kbs()]
                    receipt = await current().send(str(msg.get("text") or ""), kb_ids,
                                                   str(msg.get("client_id") or ""), steer)
                    await send({"t": "accepted", "turn_id": receipt.turn_id, "mode": receipt.accepted_mode.value,
                                "client_id": msg.get("client_id")})
                elif kind == "abort":
                    await current().harness.abort()
                elif kind == "answer":
                    current().answer(str(msg.get("request_id") or ""), msg)
                elif kind == "history":
                    await send({"t": "history", "items": list_history()})
                elif kind == "delete":
                    target = str(msg.get("host_session_id") or "")
                    if convo is not None and convo.host_session_id == target:
                        convo.detach(send)
                        convo = None
                    await delete_conversation(target)
                    await send({"t": "history", "items": list_history()})
                elif kind == "reset":
                    if convo is not None:
                        await convo.close()
                        convo = None
                    await send({"t": "reset"})
            except Exception as exc:  # noqa: BLE001 - report to the page, keep the socket
                logger.warning("[da-analyst] %s failed", kind, exc_info=True)
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
            for task in tasks:
                task.cancel()
            # the conversation keeps running for whoever opens it next
            if convo is not None:
                convo.detach(send)


async def register_extensions(registry: Any) -> list[DaAnalystPlugin]:
    plugin = DaAnalystPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
