"""JiuwenSwarm application plugin hosting the DeepAnalyze harness provider.

Swarm 0.2.7 (openjiuwen 0.1.19) only spawns its built-in harness providers,
so this plugin is the host: each page connection owns one DeepAnalyzeHarness
driven purely through HarnessProtocol (start / send / steer / abort / stop,
the interaction handler and a checkpoint sink for resuming a conversation).

Environment:
    DA_BASE_URL        e.g. http://da-engine:8192
    DA_ENGINE_SECRET   read by the provider (secret_env), never sent to the page
    DA_TENANT_ID       default "default"
    KB_SERVICE_URL / KBS_PLUGIN_KEY   to list knowledge bases for the scope picker
"""

from __future__ import annotations

import asyncio
import importlib
import json
import logging
import os
import uuid
from typing import Any

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


class Conversation:
    """One page connection: a harness cycle plus its event pump."""

    def __init__(self, send) -> None:  # noqa: ANN001
        self.send = send
        self.harness: Any = None
        self.pump: asyncio.Task | None = None
        self.interactions = PageInteractions(send)
        self.host_session_id = ""

    async def start(self, host_session_id: str, kb_ids: list[str], engine: str = DEFAULT_ENGINE) -> dict[str, Any]:
        from openjiuwen.harness_protocol import HarnessContext, HostCapability, ResumePolicy

        if engine not in ENGINES:
            raise ValueError(f"unknown engine: {engine}")
        provider = _provider_class(engine)
        await self.stop()
        self.host_session_id = host_session_id or uuid.uuid4().hex
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
        await self.harness.start(context)
        cursor = self.harness.events()

        async def pump() -> None:
            try:
                async for envelope in cursor:
                    wire = event_to_wire(envelope)
                    if wire is not None:
                        await self.send(wire)
            except Exception:  # noqa: BLE001 - socket gone or harness stopped
                logger.debug("[da-analyst] event pump ended", exc_info=True)

        self.pump = asyncio.create_task(pump())
        return {"host_session_id": self.host_session_id, "resumed": checkpoint is not None,
                "provider_session_id": self.harness.provider_session_id, "engine": engine}

    async def stop(self) -> None:
        harness, self.harness = self.harness, None
        if harness is not None:
            try:
                await harness.stop()
            except Exception:  # noqa: BLE001
                logger.warning("[da-analyst] harness stop failed", exc_info=True)
        pump, self.pump = self.pump, None
        if pump is not None:
            pump.cancel()




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
                id="da-analyst-page", nav_key=f"app:{PLUGIN_ID}", title="DA 分析",
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

        convo = Conversation(send)
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

        async def dispatch(msg: dict[str, Any]) -> None:
            from openjiuwen.harness_protocol import DeliveryMode, HarnessInput

            kind = msg.get("type")
            try:
                if kind == "hello":
                    engines = await _engines()
                    await send({"t": "hello", "kbs": await _list_kbs(), "engines": engines,
                                "da_ready": any(e["ready"] for e in engines if e["id"] == DEFAULT_ENGINE)})
                elif kind == "start":
                    info = await convo.start(str(msg.get("host_session_id") or ""), list(msg.get("kb_ids") or []),
                                             str(msg.get("engine") or DEFAULT_ENGINE))
                    await send({"t": "started", **info})
                elif kind == "send":
                    if convo.harness is None:
                        raise RuntimeError("会话尚未开始")
                    content = HarnessInput(content=str(msg.get("text") or ""),
                                           metadata={"kb_ids": list(msg.get("kb_ids") or [])})
                    mode = DeliveryMode.STEER if msg.get("steer") else DeliveryMode.AUTO
                    receipt = await convo.harness.send(content, mode=mode)
                    await send({"t": "accepted", "turn_id": receipt.turn_id, "mode": receipt.accepted_mode.value,
                                "client_id": msg.get("client_id")})
                elif kind == "abort":
                    if convo.harness is not None:
                        await convo.harness.abort()
                elif kind == "answer":
                    convo.interactions.answer(str(msg.get("request_id") or ""), msg)
                elif kind == "reset":
                    await convo.stop()
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
            await convo.stop()


async def register_extensions(registry: Any) -> list[DaAnalystPlugin]:
    plugin = DaAnalystPlugin()
    registry.register_application_plugin(plugin)
    return [plugin]
