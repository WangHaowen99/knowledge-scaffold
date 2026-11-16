"""DeepAnalyzeHarness: HarnessProtocol 1.0 over DA-java's /api/agents HTTP+SSE.

DA endpoints used (all need X-Engine-Secret, X-Da-Tenant-Id, X-Da-Session-Id):

* ``GET  /ready``                  readiness probe when a cycle starts
* ``POST /run-stream``             one protocol Turn (SSE, ``id:`` on every event)
* ``GET  /stream/{taskId}``        reconnect with ``Last-Event-ID``
* ``GET  /status/{taskId}``        fallback when a stream ends without ``done``
* ``POST /answer/{taskId}``        reply to ``ask_user`` (question or permission)
* ``POST /inject/{taskId}``        steer the running task
* ``POST /cancel/{taskId}``        graceful abort
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any, AsyncIterator

import httpx
from openjiuwen.harness_protocol import (
    AbortMode,
    CheckpointReason,
    HarnessCapability,
    HarnessCard,
    HarnessContext,
    HarnessInput,
    HostCapability,
    InteractionResponseStatus,
    McpTransport,
    ResumePolicy,
    ToolApprovalDecision,
    ToolApprovalRequest,
    TurnError,
    TurnEventKind,
    TurnResult,
    UserInputRequest,
)
from openjiuwen.harness_providers.base import (
    PendingTurn,
    ProviderStartupError,
    SerializedTurnHarness,
    TurnTiming,
)

from .bridge import McpBridge, passthrough
from .config import DeepAnalyzeConfig
from .mapping import DaTurnState, json_safe

logger = logging.getLogger(__name__)

VERSION = "0.2.0"
API = "/api/agents"
PERMISSION_KIND = "permission_confirm"
ALLOW_ANSWER = "允许"
DENY_ANSWER = "拒绝"
# Appended to a host-supplied role prompt (a team member): DA's file tools act
# on the engine's own workspace, which nobody on the host side can read.
REMOTE_RUNTIME_NOTE = (
    "【运行环境】你运行在远端的 {engine} 引擎上：你的文件读写工具操作的是引擎自己的工作区，"
    "宿主和其他成员都看不到。需要交付的结果请直接写进消息正文（例如 send_message）或最终回复，不要只写成文件。"
)

DEEPANALYZE_CARD = HarnessCard(
    name="deepanalyze",
    implementation_version=VERSION,
    capabilities=frozenset({
        HarnessCapability.GRACEFUL_ABORT,
        HarnessCapability.STEER,
        HarnessCapability.CHECKPOINT,
        # stdio servers are bridged to DA over streamable HTTP (see bridge.py)
        HarnessCapability.MCP_TOOLS,
    }),
    optional_host_capabilities=frozenset({
        HostCapability.USER_INPUT,
        HostCapability.TOOL_APPROVAL,
        HostCapability.CHECKPOINT_SINK,
        HostCapability.MCP_SERVERS,
    }),
)


class _StreamBroken(Exception):
    """The SSE connection ended or stalled before ``done``."""


def _option_labels(options: Any) -> list[str]:
    """Choice labels from DA's options: plain strings, or ``{"label", "description"}`` objects."""

    labels = []
    for option in options or []:
        label = option.get("label") if isinstance(option, dict) else option
        if label is not None and str(label):
            labels.append(str(label))
    return labels


def _error(message: str, *, code: str, category: str, retryable: bool | None = None) -> TurnError:
    return TurnError(message=message[:500], code=code, category=category, retryable=retryable)


def _http_error(exc: httpx.HTTPStatusError, engine: str) -> TurnError:
    status = exc.response.status_code
    if status in (401, 403):
        return _error(f"{engine} rejected the engine secret", code=f"HTTP_{status}", category="auth_required")
    if status == 503:
        return _error(f"{engine} is busy", code="HTTP_503", category="rate_limited", retryable=True)
    return _error(f"{engine} HTTP {status}", code=f"HTTP_{status}", category="unknown")


class DeepAnalyzeHarness(SerializedTurnHarness):
    """Harness for an engine speaking DA-java's /api/agents (DeepAnalyze, or a compatible one)."""

    card = DEEPANALYZE_CARD
    engine_name = "DeepAnalyze"  # shown in errors and in the remote-runtime note

    def __init__(self, config: DeepAnalyzeConfig, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        super().__init__()
        self._config = config  # no network until start()
        self._transport = transport  # test seam
        self._http: httpx.AsyncClient | None = None
        self._tenant_id = ""
        self._kb_ids: tuple[str, ...] = ()
        self._task_id: str | None = None
        self._last_event_id: str | None = None
        self._stream: httpx.Response | None = None
        self._abort_timer: asyncio.Task[None] | None = None
        self._task_started = asyncio.Event()
        self._bridge: McpBridge | None = None
        self._runtime_mcp: list[dict[str, Any]] = []
        self._system_prompt = ""  # sent once, ahead of the first turn of a new DA session
        self._kb_catalog: tuple[float, tuple[str, ...]] | None = None

    # -- lifecycle -----------------------------------------------------------

    def _secret(self, context: HarnessContext) -> str:
        name = self._config.secret_env
        return context.env.get(name) or self._config.getenv(name)

    def _validate_context(self, context: HarnessContext) -> None:
        super()._validate_context(context)
        if not self._secret(context):
            raise ProviderStartupError(
                f"environment variable {self._config.secret_env} is not set",
                error=_error(f"{self.engine_name} engine secret is missing", code="SECRET_MISSING", category="auth_required"),
            )
        if not (self._config.tenant_id or str(context.metadata.get("tenant_id") or "")):
            raise ProviderStartupError(
                "no tenant_id in provider config or context metadata",
                error=_error(f"{self.engine_name} tenant is not configured", code="TENANT_MISSING", category="config"),
            )

    async def _open_session(self, context: HarnessContext) -> str | None:
        self._tenant_id = self._config.tenant_id or str(context.metadata.get("tenant_id") or "")
        meta_kbs = context.metadata.get("kb_ids")
        self._kb_ids = tuple(str(k) for k in meta_kbs) if isinstance(meta_kbs, (list, tuple)) else self._config.kb_ids
        restored = None
        if context.resume_policy is not ResumePolicy.NEW:
            restored = self._restored_checkpoint_data(context)
            if restored is None and context.resume_policy is ResumePolicy.REQUIRE_RESUME:
                raise ProviderStartupError(
                    "resume required but no deepanalyze checkpoint was supplied",
                    error=_error("no checkpoint to resume", code="NO_CHECKPOINT", category="config"),
                )
        session_id = str((restored or {}).get("da_session_id") or "") or (
            f"{self._tenant_id}:{self._config.session_prefix}{context.host_session_id}"
        )
        self._http = httpx.AsyncClient(
            base_url=self._config.base_url,
            headers={
                "X-Engine-Secret": self._secret(context),
                "X-Da-Tenant-Id": self._tenant_id,
                "X-Da-Session-Id": session_id,
            },
            timeout=httpx.Timeout(30.0, connect=self._config.connect_timeout_s),
            trust_env=False,
            transport=self._transport,
        )
        try:
            ready = await self._http.get("/ready")
        except httpx.HTTPError as exc:
            raise ProviderStartupError(
                f"{self.engine_name} unreachable: {type(exc).__name__}",
                error=_error(f"{self.engine_name} is unreachable", code=type(exc).__name__, category="server_unavailable",
                             retryable=True),
            ) from exc
        if ready.status_code >= 400:
            raise ProviderStartupError(
                f"{self.engine_name} not ready (HTTP {ready.status_code})",
                error=_error(f"{self.engine_name} is not ready", code=f"HTTP_{ready.status_code}",
                             category="server_unavailable", retryable=True),
            )
        self._session_id = session_id
        prompt = "" if restored else (context.system_prompt or "").strip()
        self._system_prompt = f"{prompt}\n\n{REMOTE_RUNTIME_NOTE.format(engine=self.engine_name)}" if prompt else ""
        await self._mount_mcp_servers(context)
        await self._publish_checkpoint(self._checkpoint_data(), reason=CheckpointReason.SESSION_ACTIVATED)
        return session_id

    async def _mount_mcp_servers(self, context: HarnessContext) -> None:
        """Make the host's MCP servers reachable from DA for every task of this session."""
        servers = tuple(context.mcp_servers)
        mounted = [passthrough(s) for s in servers if s.transport is McpTransport.HTTP]
        stdio = [s for s in servers if s.transport is McpTransport.STDIO]
        for server in servers:
            if server.transport is McpTransport.IN_PROCESS:
                logger.warning("deepanalyze cannot reach in-process MCP server %s; skipped", server.name)
        if stdio and not self._config.mcp_bridge_host:
            logger.warning("no MCP bridge host configured; %s runs without MCP servers %s", self.engine_name,
                           ", ".join(s.name for s in stdio))
        elif stdio:
            self._bridge = McpBridge(stdio, advertise_host=self._config.mcp_bridge_host)
            try:
                mounted.extend(await self._bridge.start())
            except Exception as exc:
                self._bridge = None
                raise ProviderStartupError(
                    f"cannot bridge MCP servers to {self.engine_name}: {type(exc).__name__}: {exc}",
                    error=_error("MCP bridge failed to start", code="MCP_BRIDGE", category="config"),
                ) from exc
        self._runtime_mcp = [m.runtime_entry() for m in mounted]

    async def _close_session(self) -> None:
        stream, self._stream = self._stream, None
        if stream is not None:
            await stream.aclose()
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            await bridge.stop()
        self._runtime_mcp = []
        http, self._http = self._http, None
        if http is not None:
            await http.aclose()

    def _checkpoint_data(self) -> dict[str, Any]:
        return {
            "da_session_id": self._session_id,
            "tenant_id": self._tenant_id,
            "last_task_id": self._task_id,
            "last_event_id": self._last_event_id,
        }

    # -- turn ----------------------------------------------------------------

    async def _turn_kbs(self, turn: PendingTurn) -> list[str]:
        override = turn.content.metadata.get("kb_ids") if turn.content.metadata else None
        if isinstance(override, (list, tuple)):
            return [str(k) for k in override]
        if self._kb_ids:
            return list(self._kb_ids)
        return await self._catalog_kbs()

    async def _catalog_kbs(self) -> list[str]:
        """Every knowledge base of the tenant, from the configured catalog (cached briefly)."""
        url = self._config.kb_catalog_url
        if not url:
            return []
        cached = self._kb_catalog
        if cached is not None and time.monotonic() - cached[0] < 30:
            return list(cached[1])
        key_env = self._config.kb_catalog_key_env
        headers = {"Authorization": f"Bearer {self._config.getenv(key_env)}"} if key_env else {}
        try:
            async with httpx.AsyncClient(timeout=10, trust_env=False, transport=self._transport) as client:
                response = await client.get(url, headers=headers)
                response.raise_for_status()
                ids = tuple(str(kb["id"]) for kb in response.json().get("items", []) if kb.get("id"))
        except (httpx.HTTPError, ValueError, KeyError, TypeError, AttributeError) as exc:
            logger.warning("deepanalyze knowledge-base catalog unavailable: %s", type(exc).__name__)
            return list(cached[1]) if cached else []
        self._kb_catalog = (time.monotonic(), ids)
        return list(ids)

    @staticmethod
    def _text(content: HarnessInput) -> str:
        value = content.content
        if isinstance(value, str):
            return value
        return json.dumps(json_safe(value), ensure_ascii=False)

    async def _events(self, response: httpx.Response) -> AsyncIterator[tuple[str, dict[str, Any]]]:
        event, data_lines, event_id = "message", [], None
        async for line in response.aiter_lines():
            if line == "":
                if data_lines:
                    if event_id:
                        self._last_event_id = event_id
                    raw = "\n".join(data_lines)
                    try:
                        data = json.loads(raw)
                    except ValueError:
                        data = {"raw": raw}
                    yield event, data if isinstance(data, dict) else {"value": data}
                event, data_lines, event_id = "message", [], None
                continue
            if line.startswith(":"):
                continue
            key, _, value = line.partition(":")
            value = value[1:] if value.startswith(" ") else value
            if key == "event":
                event = value
            elif key == "data":
                data_lines.append(value)
            elif key == "id":
                event_id = value

    def _client(self) -> httpx.AsyncClient:
        if self._http is None:
            raise RuntimeError(f"{self.engine_name} harness is not started")
        return self._http

    def _stream_timeout(self) -> httpx.Timeout:
        return httpx.Timeout(self._config.read_idle_timeout_s, connect=self._config.connect_timeout_s)

    async def _open_run(self, turn: PendingTurn, text: str) -> httpx.Response:
        http = self._client()
        body: dict[str, Any] = {
            "sessionId": self._session_id,
            "input": text,
            "scope": {"kbIds": await self._turn_kbs(turn)},
        }
        if self._runtime_mcp:
            body["runtimeConfig"] = {
                "version": 1,
                "tenantId": self._tenant_id,
                "sessionId": self._session_id,
                "mcpServers": self._runtime_mcp,
            }
        if self._config.agent_type:
            body["agentType"] = self._config.agent_type
        if self._config.max_turns:
            body["maxTurns"] = self._config.max_turns
        request = http.build_request(
            "POST", f"{API}/run-stream", json=body,
            headers={"Accept": "text/event-stream"}, timeout=self._stream_timeout(),
        )
        response = await http.send(request, stream=True)
        if response.status_code >= 400:
            await response.aread()
            response.raise_for_status()
        return response

    async def _open_reconnect(self) -> httpx.Response:
        http = self._client()
        if not self._task_id:
            raise RuntimeError(f"no {self.engine_name} task to reconnect to")
        headers = {"Accept": "text/event-stream"}
        if self._last_event_id:
            headers["Last-Event-ID"] = self._last_event_id
        request = http.build_request(
            "GET", f"{API}/stream/{self._task_id}", headers=headers, timeout=self._stream_timeout(),
        )
        response = await http.send(request, stream=True)
        if response.status_code >= 400:
            await response.aread()
            response.raise_for_status()
        return response

    async def _execute_turn(self, turn: PendingTurn) -> tuple[TurnEventKind, TurnResult]:
        timing = TurnTiming()
        text = self._text(turn.content)
        if self._system_prompt:
            # DA takes no system prompt per request; the host's (e.g. a team
            # member's role and team-tool instructions) leads the first input.
            text = f"{self._system_prompt}\n\n---\n\n{text}"
            self._system_prompt = ""
        deadline = time.monotonic() + self._config.busy_retry_s
        while True:
            kind, result, state = await self._run_task(turn, text, timing)
            busy = (
                kind is TurnEventKind.FAILED and result.error is not None and result.error.code == "ENGINE_BUSY"
                and not state.answer and state.tool_calls == 0 and state.asks == 0
            )
            if not busy or turn.abort_requested or time.monotonic() >= deadline:
                return kind, result
            logger.info("deepanalyze conversation busy, retrying turn %s", turn.turn_id)
            await asyncio.sleep(self._config.busy_poll_s)

    async def _run_task(
        self, turn: PendingTurn, text: str, timing: TurnTiming,
    ) -> tuple[TurnEventKind, TurnResult, DaTurnState]:
        """One DA task for this turn: run-stream, reconnects, interactions."""

        state = DaTurnState(user_text=text, engine=self.engine_name)
        self._task_id = None
        self._last_event_id = None
        self._task_started.clear()
        attempts = 0

        def finish(**kwargs: Any) -> tuple[TurnEventKind, TurnResult, DaTurnState]:
            kind, result = state.result(
                started_at=timing.started_at, started_monotonic=timing.started_monotonic, **kwargs,
            )
            return kind, result, state

        try:
            response = await self._open_run(turn, text)
        except httpx.HTTPStatusError as exc:
            return finish(failure=_http_error(exc, self.engine_name))
        except httpx.HTTPError as exc:
            if turn.abort_requested:
                return finish(aborted=not turn.stop_requested, stopped=turn.stop_requested)
            return finish(failure=_error(f"{self.engine_name} request failed: {type(exc).__name__}",
                                         code=type(exc).__name__, category="server_unavailable", retryable=True))

        while True:
            self._stream = response
            try:
                async for name, data in self._events(response):
                    attempts = 0
                    if name == "start" and data.get("taskId"):
                        self._task_id = str(data["taskId"])
                        self._task_started.set()
                        if turn.abort_requested:
                            await self._cancel_task()  # abort arrived before the task id
                    if name == "ask_user":
                        if not await self._handle_ask(turn, state, data):
                            await self._cancel_task()
                        continue
                    for mapped in state.consume(name, data):
                        await self._emit(mapped.payload, turn=turn, item_id=mapped.item_id)
                    if name == "done":
                        break
                if state.status is None:
                    raise _StreamBroken("stream closed before done")
            except Exception as exc:  # noqa: BLE001 - every stream failure takes the same path
                await response.aclose()
                if turn.abort_requested:
                    return finish(aborted=not turn.stop_requested, stopped=turn.stop_requested)
                if not isinstance(exc, (httpx.HTTPError, httpx.StreamError, _StreamBroken)):
                    raise
                logger.info("deepanalyze stream interrupted (%s), task=%s", type(exc).__name__, self._task_id)
                status = await self._task_status()
                if status in {"completed", "failed", "cancelled"}:
                    state.status = state.status or status
                    break
                reconnected = await self._reconnect(attempts)
                if reconnected is None:
                    return finish(failure=_error(f"{self.engine_name} stream lost: {type(exc).__name__}",
                                                 code="STREAM_LOST", category="network_timeout", retryable=True))
                attempts, response = reconnected
                continue
            finally:
                if self._stream is response:
                    self._stream = None
            await response.aclose()
            break

        if self._abort_timer is not None:
            self._abort_timer.cancel()
            self._abort_timer = None
        await self._publish_checkpoint(self._checkpoint_data(), reason=CheckpointReason.TURN_COMPLETED)
        return finish(aborted=turn.abort_requested and state.status != "completed" and not turn.stop_requested,
                      stopped=turn.stop_requested and state.status != "completed")

    async def _reconnect(self, attempts: int) -> tuple[int, httpx.Response] | None:
        """Reopen the task stream from the last event id, with backoff."""

        while self._task_id and attempts < self._config.reconnect_max_attempts:
            attempts += 1
            await asyncio.sleep(self._config.reconnect_backoff_s * attempts)
            try:
                return attempts, await self._open_reconnect()
            except httpx.HTTPError:
                logger.info("deepanalyze reconnect attempt %s failed", attempts)
        return None

    async def _task_status(self) -> str | None:
        if self._http is None or not self._task_id:
            return None
        try:
            resp = await self._http.get(f"{API}/status/{self._task_id}")
            if resp.status_code >= 400:
                return None
            return str(resp.json().get("status") or "") or None
        except (httpx.HTTPError, ValueError):
            return None

    # -- interactions --------------------------------------------------------

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> bool:
        if self._http is None:
            return False
        try:
            resp = await self._http.post(path, json=body or {})
        except httpx.HTTPError:
            logger.warning("deepanalyze %s failed", path, exc_info=True)
            return False
        if resp.status_code >= 400:
            logger.warning("deepanalyze %s -> HTTP %s", path, resp.status_code)
            return False
        return True

    async def _answer(self, state: DaTurnState, answer: str, tool_call_id: Any) -> bool:
        body: dict[str, Any] = {"answer": answer}
        if tool_call_id:
            body["toolCallId"] = str(tool_call_id)
        ok = await self._post(f"{API}/answer/{state.task_id}", body)
        if ok:
            state.seg += 1  # DA restarts tool numbering for the resumed segment
        return ok

    async def _handle_ask(self, turn: PendingTurn, state: DaTurnState, data: dict[str, Any]) -> bool:
        """Route a DA interrupt to the host; False means the task should be cancelled."""

        state.asks += 1
        question = str(data.get("question") or "")
        options = _option_labels(data.get("options"))
        asked = [q for q in (data.get("questions") or []) if isinstance(q, dict) and str(q.get("question") or "")]
        if len(asked) > 1:
            # several questions in one card: list each with its own choices, the user answers them by number
            question = "\n".join(
                f"{i}. {q['question']}" + (f"（可选：{'、'.join(_option_labels(q.get('options')))}）"
                                           if _option_labels(q.get("options")) else "")
                for i, q in enumerate(asked, start=1)) + "\n请按序号逐个回答。"
            options = []
        tool_call_id = data.get("toolCallId")
        request_id = f"{state.task_id}:{state.seg}:ask:{state.asks}"
        caps = self._context.host_capabilities if self._context else frozenset()
        if data.get("kind") == PERMISSION_KIND:
            if self._is_host_tool(str(data.get("toolName") or "")):
                # The host mounted this MCP server for DA itself (e.g. a team's
                # coordination tools); DA's default policy still asks, but there
                # is nothing for the host to approve about its own tools.
                return await self._answer(state, ALLOW_ANSWER, tool_call_id)
            if HostCapability.TOOL_APPROVAL in caps and self._has_host_interactions():
                arguments = data.get("arguments") or data.get("input") or {}
                response = await self._request_interaction(ToolApprovalRequest(
                    request_id=request_id,
                    call_id=str(tool_call_id or request_id),
                    tool_name=str(data.get("toolName") or "unknown"),
                    arguments=json_safe(arguments if isinstance(arguments, dict) else {"value": arguments}),
                    provider_session_id=self._session_id,
                    turn_id=turn.turn_id,
                    title=question or None,
                ))
                if response is None or response.decision is ToolApprovalDecision.ABORT:
                    return False
                allow = response.decision in (ToolApprovalDecision.ALLOW, ToolApprovalDecision.ALLOW_FOR_SESSION)
                return await self._answer(state, ALLOW_ANSWER if allow else DENY_ANSWER, tool_call_id)
            # No approval channel: never grant on the user's behalf.
            return await self._answer(state, DENY_ANSWER, tool_call_id)
        if HostCapability.USER_INPUT in caps and self._has_host_interactions():
            response = await self._request_interaction(UserInputRequest(
                request_id=request_id,
                prompt=question,
                provider_session_id=self._session_id,
                turn_id=turn.turn_id,
                choices=tuple(options),
            ))
            if response is None or response.status is not InteractionResponseStatus.COMPLETED:
                return False
            content = response.content
            answer = content if isinstance(content, str) else json.dumps(json_safe(content), ensure_ascii=False)
            return await self._answer(state, answer, tool_call_id)
        return await self._answer(state, self._config.unanswered_reply, tool_call_id)

    def _is_host_tool(self, tool_name: str) -> bool:
        """DA exposes runtime MCP tools as ``mcp_<serverName>_<tool>``."""
        return any(tool_name.startswith(f"mcp_{entry['serverName']}_") for entry in self._runtime_mcp)

    async def _cancel_task(self) -> None:
        if self._task_id:
            await self._post(f"{API}/cancel/{self._task_id}")

    # -- commands ------------------------------------------------------------

    async def _steer(self, turn: PendingTurn, content: HarnessInput, *, message_id: str | None = None) -> None:
        # message_id: newer openjiuwen hands over the receipt's id; DA's inject
        # API has nowhere to carry it.
        _ = turn, message_id
        if not self._task_id:
            # STEER may arrive right after STARTED, before DA has sent its task id.
            try:
                await asyncio.wait_for(self._task_started.wait(), timeout=30)
            except asyncio.TimeoutError as exc:
                raise RuntimeError(f"{self.engine_name} task did not start in time for steering") from exc
        if not await self._post(f"{API}/inject/{self._task_id}", {"message": self._text(content)}):
            raise RuntimeError(f"{self.engine_name} rejected the steering input")

    async def _interrupt_turn(self, turn: PendingTurn, mode: AbortMode) -> None:
        _ = turn, mode
        await self._cancel_task()

        async def _deadline() -> None:
            await asyncio.sleep(self._config.abort_wait_s)
            stream = self._stream
            if stream is not None:
                await stream.aclose()

        if self._abort_timer is None or self._abort_timer.done():
            self._abort_timer = asyncio.create_task(_deadline(), name="deepanalyze_abort_deadline")
