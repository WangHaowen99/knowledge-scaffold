"""Translate DA SSE events into HarnessProtocol observations.

Level alignment (see the 10-12 integration design):

* protocol Turn  == one DA task (taskId), including answer-resumed segments
* protocol Step  == one DA ``turn`` (a ReAct iteration)
* DA restarts its tool-call numbering after every answer, so item ids carry a
  segment counter (``seg``) bumped by the harness after each POST /answer.

Besides ``tool`` and ``step`` the turn carries three item types whose ``data``
is already in the shape of a JiuwenSwarm panel event (see README, "Items"):
``todo`` (todo.updated), ``subagent`` (subagent_updated / subagent_activity)
and ``file`` (one chat.file entry). The DA events they come from are still
forwarded as ProviderEvents.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any

from openjiuwen.harness_protocol import (
    ContentBlock,
    ItemEventKind,
    ItemLifecycleEvent,
    MessageRole,
    OutputChannel,
    OutputEvent,
    OutputKind,
    OutputOperation,
    ProviderEvent,
    TurnError,
    TurnEventKind,
    TurnMessage,
    TurnResult,
    TurnStatus,
    TurnTermination,
    TurnTerminationKind,
    TurnUsage,
    UsageUpdatedEvent,
    UsageUpdateMode,
)

PROVIDER = "deepanalyze"
MAX_TOOL_RESULT_CHARS = 20000
SILENT_EVENTS = {
    "content_start", "content_end", "thinking_start", "thinking_end", "heartbeat", "reconnect_done",
}
PUSH_FILE_TOOL = "push_file"
# Same cap as a JiuwenSwarm subagent activity summary.
ACTIVITY_SUMMARY_CHARS = 2000
# Workflow members remembered per harness (finished ones too: DA replays their start events).
MAX_TRACKED_SUBAGENTS = 512
# One rendered DA todo line: "⬜ [id] content (status)".
TODO_LINE = re.compile(r"^\s*(?:\S+\s+)?\[(?P<id>[^\]]+)\]\s*(?P<content>.*?)\s*\((?P<status>[A-Za-z_]+)\)\s*$")
TODO_STATUS = {
    "pending": "pending", "waiting": "pending", "in_progress": "in_progress", "running": "in_progress",
    "completed": "completed", "done": "completed", "cancelled": "cancelled", "canceled": "cancelled",
}
OUTCOMES = {
    "completed": "completed", "success": "completed", "ok": "completed", "done": "completed",
    "failed": "failed", "error": "failed", "cancelled": "cancelled", "canceled": "cancelled", "aborted": "cancelled",
}


def json_safe(value: Any) -> Any:
    """Round-trip through JSON so payloads are plain, protocol-acceptable values."""

    try:
        return json.loads(json.dumps(value, ensure_ascii=False, default=str))
    except (TypeError, ValueError):
        return str(value)


def _int(value: Any) -> int | None:
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _now_ms() -> float:
    return time.time() * 1000


def _clip(text: str, limit: int = ACTIVITY_SUMMARY_CHARS) -> str:
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _compact(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(json_safe(value), ensure_ascii=False, separators=(",", ":"))


def _safe_id(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9_-]", "_", text) or "_"


@dataclass
class Mapped:
    payload: Any
    item_id: str | None = None


@dataclass(frozen=True)
class FileFetch:
    """A file DA pushed (``push_file``); the harness fetches it and emits the ``file`` item."""

    file_name: str
    download_url: str
    title: str = ""
    mime_type: str = ""
    size: int | None = None


def todo_item_data(data: dict[str, Any]) -> dict[str, Any] | None:
    """``todo_update`` -> ``{"todos": [{id, content, activeForm, status}]}`` (Swarm's todo.updated shape).

    DA sends the whole list as rendered lines (``"⬜ [id] content (pending)"``,
    cancelled items left out); a list of objects is accepted as well.
    """

    raw = data.get("todos")
    entries: list[dict[str, Any]] = []
    if isinstance(raw, str):
        for line in raw.splitlines():
            match = TODO_LINE.match(line)
            if match:
                entries.append({"id": match["id"], "content": match["content"], "status": match["status"]})
    elif isinstance(raw, list):
        entries = [entry for entry in raw if isinstance(entry, dict)]
    else:
        return None
    todos = []
    for entry in entries:
        todo_id = str(entry.get("id") or "").strip()
        if not todo_id:
            continue
        content = str(entry.get("content") or "")
        active = entry.get("activeForm")
        status = TODO_STATUS.get(str(entry.get("status") or "").lower(), "pending")
        todos.append({"id": todo_id, "content": content,
                      "activeForm": active if isinstance(active, str) and active else content, "status": status})
    return {"todos": todos}


@dataclass
class _Member:
    subagent_id: str
    workflow_id: str
    agent_id: str
    role: str
    task: str
    subagent_type: str
    created_at: float
    revision: int = 0
    seq: int = 0
    closed: bool = False
    call_ids: dict[str, str] = field(default_factory=dict)


class DaItemTracker:
    """Item state that outlives one DA task: the workflow members seen in this harness session.

    A delegated task can run into the next turn, and DA then replays the run's
    ``workflow_start`` and every member's ``workflow_agent_start`` ahead of the
    next event, including members that already finished. Those replays must
    neither reopen a finished member nor reset an open one.
    """

    def __init__(self) -> None:
        self._members: OrderedDict[str, _Member] = OrderedDict()
        self._modes: OrderedDict[str, str] = OrderedDict()

    def workflow_items(self, data: dict[str, Any], prefix: str) -> list[Mapped]:
        """``workflow_event`` -> ``subagent`` items (one item per member; ids are ``{prefix}:sub:{subagent_id}``)."""

        kind = str(data.get("type") or "")
        workflow_id = str(data.get("workflowId") or "")
        if not workflow_id:
            return []
        if kind == "workflow_start":
            self._modes[workflow_id] = str(data.get("mode") or "")
            while len(self._modes) > MAX_TRACKED_SUBAGENTS:
                self._modes.popitem(last=False)
            return []
        if kind == "workflow_complete":
            outcome = OUTCOMES.get(str(data.get("status") or "").lower(), "completed")
            items = [self._closed(member, outcome, None, prefix) for member in list(self._members.values())
                     if member.workflow_id == workflow_id and not member.closed]
            self._modes.pop(workflow_id, None)
            return items
        if not kind.startswith("workflow_agent_") or not data.get("agentId"):
            return []
        agent_id = str(data["agentId"])
        subagent_id = f"da-{_safe_id(workflow_id)}-{_safe_id(agent_id)}"
        member = self._members.get(subagent_id)
        if member is not None and member.closed:
            return []  # a replayed start, or a late event of a finished member
        items: list[Mapped] = []
        if member is None:
            member = self._remember(_Member(
                subagent_id=subagent_id, workflow_id=workflow_id, agent_id=agent_id,
                role=str(data.get("role") or ""), task=str(data.get("task") or ""),
                subagent_type=self._modes.get(workflow_id) or PROVIDER, created_at=_now_ms(),
            ))
            items.append(self._roster_item(member, ItemEventKind.STARTED, prefix))
            if kind == "workflow_agent_start":
                return items
        elif kind == "workflow_agent_start":
            return [self._roster_item(member, ItemEventKind.STARTED, prefix)]
        if kind == "workflow_agent_complete":
            outcome = OUTCOMES.get(str(data.get("status") or "").lower())
            error = str(data.get("error") or "") or None
            outcome = outcome or ("failed" if error else "completed")
            items.append(self._closed(member, outcome, error, prefix))
            return items
        activity = self._activity(member, kind, data)
        if activity is not None:
            items.append(Mapped(
                ItemLifecycleEvent(kind=ItemEventKind.UPDATED, item_type="subagent",
                                   data={"subagent_id": member.subagent_id, "activity": activity}),
                item_id=f"{prefix}:sub:{member.subagent_id}",
            ))
        return items

    def _remember(self, member: _Member) -> _Member:
        self._members[member.subagent_id] = member
        while len(self._members) > MAX_TRACKED_SUBAGENTS:
            self._members.popitem(last=False)
        return member

    def _closed(self, member: _Member, outcome: str, error: str | None, prefix: str) -> Mapped:
        member.closed = True
        return self._roster_item(member, ItemEventKind.COMPLETED, prefix, outcome=outcome, error=error)

    @staticmethod
    def _roster_item(member: _Member, kind: ItemEventKind, prefix: str, *, outcome: str | None = None,
                     error: str | None = None) -> Mapped:
        """The member in Swarm's ``subagent_updated`` shape (minus ``parent_session_id``, which the host owns).

        A DA member runs once and takes no input: it is ``running`` until it
        ends, then ``closed`` with ``turn_outcome`` = ``closed_reason`` =
        completed / failed / cancelled.
        """

        now = _now_ms()
        member.revision += 1
        closed = outcome is not None
        roster = {
            "subagent_id": member.subagent_id,
            "sub_session_id": member.subagent_id,
            "subagent_type": member.subagent_type,
            "display_name": member.agent_id,
            "role": member.role,
            "task_description": member.task,
            "status": "closed" if closed else "running",
            "turn_outcome": outcome,
            "closed_at": now if closed else None,
            "closed_reason": outcome,
            "error": {"code": "DA_MEMBER_FAILED", "message": error or "failed"} if outcome == "failed" else None,
            "created_at": member.created_at,
            "updated_at": now,
            "revision": member.revision,
            "lifecycle": "closed" if closed else "live",
            "can_send_input": False,
            "needs_resume": False,
        }
        return Mapped(ItemLifecycleEvent(kind=kind, item_type="subagent", data=json_safe(roster)),
                      item_id=f"{prefix}:sub:{member.subagent_id}")

    @staticmethod
    def _activity(member: _Member, kind: str, data: dict[str, Any]) -> dict[str, Any] | None:
        """A member event in Swarm's ``SubagentActivity`` shape (minus ``parent_session_id``)."""

        tool_name = str(data.get("toolName") or "") or None
        seq_key = str(data.get("seq") or "")
        activity: dict[str, Any] = {}
        if kind == "workflow_agent_tool_call":
            call_id = str(data.get("callId") or "") or f"{member.subagent_id}-{seq_key or member.seq + 1}"
            if seq_key:
                member.call_ids[seq_key] = call_id
            arguments = data.get("input")
            summary = f"{tool_name or 'tool'}({_compact(arguments)})" if arguments not in (None, "", {}) else (
                tool_name or "tool")
            activity = {"kind": "tool_call", "summary": summary, "tool_name": tool_name, "tool_call_id": call_id}
        elif kind == "workflow_agent_tool_result":
            status = str(data.get("status") or "").lower()
            activity = {"kind": "tool_result", "summary": _compact(data.get("output") or "") or (tool_name or ""),
                        "tool_name": tool_name, "tool_call_id": member.call_ids.pop(seq_key, None),
                        "ok": status not in ("error", "failed")}
        elif kind == "workflow_agent_chunk":
            text = str(data.get("content") or "")
            if not text:
                return None
            activity = {"kind": "thinking", "summary": text}
        else:
            return None
        member.seq += 1
        shaped = {"subagent_id": member.subagent_id, "task_id": member.subagent_id, "seq": member.seq,
                  "kind": activity["kind"], "summary": _clip(activity["summary"]), "at_ms": _now_ms(),
                  "phase_id": 0}
        for key in ("tool_name", "tool_call_id", "ok"):
            if activity.get(key) is not None:
                shaped[key] = activity[key]
        return shaped


@dataclass
class DaTurnState:
    """Accumulates one protocol Turn while DA events stream in."""

    user_text: str
    task_id: str | None = None
    seg: int = 0
    turn: int = 0
    answer: list[str] = field(default_factory=list)
    final_output: str | None = None
    status: str | None = None
    error: TurnError | None = None
    usage: dict[str, int] = field(default_factory=dict)
    tool_calls: int = 0
    asks: int = 0
    engine: str = "DeepAnalyze"  # name used in error messages
    # DA tool id -> protocol item id for calls still waiting for their result.
    # DA renumbers tools after an answered ask_user, so the result of a call
    # opened before the answer must reuse the item id it started with.
    open_tools: dict[str, str] = field(default_factory=dict)
    # DA tool id -> (tool name, arguments) of the same open calls: DA's permission
    # question names the tool and a call id of another numbering, not the arguments.
    open_args: dict[str, tuple[str, Any]] = field(default_factory=dict)
    # DA tool ids whose arguments an approval request already showed
    approved_calls: set[str] = field(default_factory=set)
    # Workflow members, shared by every turn of the harness session.
    items: DaItemTracker = field(default_factory=DaItemTracker)

    def pending_arguments(self, tool_name: str) -> Any:
        """Arguments of the oldest open call of ``tool_name`` no approval has shown yet; ``{}`` if none.

        DA asks before running a call, so the call it asks about is open (no
        result yet); calls of one tool are asked about in the order they started.
        """

        for call_id, (name, arguments) in self.open_args.items():
            if name == tool_name and call_id not in self.approved_calls:
                self.approved_calls.add(call_id)
                return arguments
        return {}

    def _output_id(self, channel: str) -> str:
        return f"{self.task_id}:{self.seg}:{channel}:{self.turn}"

    @staticmethod
    def _provider_event(event: str, data: dict[str, Any]) -> Mapped:
        return Mapped(ProviderEvent(provider=PROVIDER, event_type=event, schema_version="1", payload=json_safe(data)))

    def _file_fetch(self, data: dict[str, Any], item_id: str) -> list[Mapped]:
        """A pushed file's ``tool_result`` -> a :class:`FileFetch` the harness turns into a ``file`` item."""

        if str(data.get("toolName") or "") != PUSH_FILE_TOOL:
            return []
        result = data.get("result")
        if not isinstance(result, dict) and isinstance(data.get("output"), str):
            try:
                result = json.loads(data["output"])
            except ValueError:
                result = None
        if not isinstance(result, dict) or result.get("pushed") is not True or result.get("error"):
            return []
        name, url = str(result.get("fileName") or ""), str(result.get("downloadUrl") or "")
        if not name or not url:
            return []
        return [Mapped(FileFetch(file_name=name, download_url=url, title=str(result.get("title") or ""),
                                 mime_type=str(result.get("mimeType") or ""), size=_int(result.get("fileSize"))),
                       item_id=f"{item_id}:file")]

    def consume(self, event: str, data: dict[str, Any]) -> list[Mapped]:
        if isinstance(data.get("turn"), int):
            self.turn = data["turn"]
        if event == "start":
            self.task_id = str(data.get("taskId") or self.task_id or "")
            return []
        if event in SILENT_EVENTS:
            return []
        if event == "content_delta":
            delta = str(data.get("delta") or "")
            if not delta:
                return []
            self.answer.append(delta)
            return [Mapped(OutputEvent(
                output_id=self._output_id("a"), kind=OutputKind.TEXT, content=delta,
                operation=OutputOperation.DELTA, channel=OutputChannel.ANSWER,
            ))]
        if event == "thinking_delta":
            delta = str(data.get("delta") or "")
            if not delta:
                return []
            return [Mapped(OutputEvent(
                output_id=self._output_id("r"), kind=OutputKind.TEXT, content=delta,
                operation=OutputOperation.DELTA, channel=OutputChannel.REASONING,
            ))]
        if event == "content":
            text = data.get("content")
            if not isinstance(text, str) or not text:
                return []
            return [Mapped(OutputEvent(
                output_id=self._output_id("a"), kind=OutputKind.TEXT, content=text,
                operation=OutputOperation.SNAPSHOT, channel=OutputChannel.ANSWER,
            ))]
        if event == "turn":
            return [Mapped(
                ItemLifecycleEvent(kind=ItemEventKind.COMPLETED, item_type="step", data={"turn": self.turn}),
                item_id=f"{self.task_id}:{self.seg}:step:{self.turn}",
            )]
        if event == "turn_usage":
            usage = data.get("usage") or {}
            delta = {
                "input_tokens": _int(usage.get("input_tokens")),
                "output_tokens": _int(usage.get("output_tokens")),
                "cached_input_tokens": _int(usage.get("cache_tokens")),
                "total_tokens": _int(usage.get("total_tokens")),
            }
            for key, value in delta.items():
                if value is not None:
                    self.usage[key] = self.usage.get(key, 0) + value
            provider_data = {"model_name": usage.get("model_name")} if usage.get("model_name") else {}
            return [Mapped(UsageUpdatedEvent(
                usage=TurnUsage(**delta, provider_data=provider_data), mode=UsageUpdateMode.DELTA,
            ))]
        if event == "tool_call":
            self.tool_calls += 1
            item_id = f"{self.task_id}:{self.seg}:{data.get('id')}"
            self.open_tools[str(data.get("id"))] = item_id
            self.open_args[str(data.get("id"))] = (str(data.get("toolName") or ""), json_safe(data.get("input") or {}))
            return [Mapped(
                ItemLifecycleEvent(
                    kind=ItemEventKind.STARTED,
                    item_type="tool",
                    data={"name": str(data.get("toolName") or ""), "arguments": json_safe(data.get("input") or {})},
                ),
                item_id=item_id,
            )]
        if event == "tool_result":
            output = data.get("output")
            if isinstance(output, str) and len(output) > MAX_TOOL_RESULT_CHARS:
                output = output[:MAX_TOOL_RESULT_CHARS] + "…[truncated]"
            item_id = self.open_tools.pop(str(data.get("id")), f"{self.task_id}:{self.seg}:{data.get('id')}")
            self.open_args.pop(str(data.get("id")), None)
            return [Mapped(
                ItemLifecycleEvent(
                    kind=ItemEventKind.COMPLETED,
                    item_type="tool",
                    data={"tool_name": str(data.get("toolName") or ""), "result": json_safe(output)},
                ),
                item_id=item_id,
            ), *self._file_fetch(data, item_id)]
        if event == "todo_update":
            todo = todo_item_data(data)
            if todo is None:
                return [self._provider_event(event, data)]
            return [self._provider_event(event, data), Mapped(
                ItemLifecycleEvent(kind=ItemEventKind.UPDATED, item_type="todo", data=json_safe(todo)),
                item_id=f"{self.task_id}:todo",
            )]
        if event == "workflow_event":
            return [self._provider_event(event, data), *self.items.workflow_items(data, str(self.task_id))]
        if event == "complete":
            if isinstance(data.get("output"), str):
                self.final_output = data["output"]
            return []
        if event == "error":
            self.error = TurnError(
                message=str(data.get("message") or data.get("error") or f"{self.engine} reported an error")[:500],
                code=str(data.get("code") or "") or None,
                category="sdk_error",
                retryable=str(data.get("code") or "") == "ENGINE_BUSY" or None,
            )
            return []
        if event == "done":
            self.status = str(data.get("status") or "completed")
            if self.final_output is None and isinstance(data.get("output"), str):
                self.final_output = data["output"]
            return []
        # Unknown and DA-specific events are kept, not dropped.
        return [self._provider_event(event, data)]

    # -- results -------------------------------------------------------------

    def _messages(self, final_text: str) -> tuple[TurnMessage, ...]:
        messages = [TurnMessage(
            message_id=f"user-{uuid.uuid4().hex[:12]}", role=MessageRole.USER,
            content=(ContentBlock(block_id="b0", kind="text", content=self.user_text),),
        )]
        if final_text:
            messages.append(TurnMessage(
                message_id=f"assistant-{self.task_id or uuid.uuid4().hex[:12]}", role=MessageRole.ASSISTANT,
                content=(ContentBlock(block_id="b0", kind="text", content=final_text),),
            ))
        return tuple(messages)

    def _usage(self) -> TurnUsage | None:
        return TurnUsage(**self.usage) if self.usage else None

    def result(
        self, *, started_at: float, started_monotonic: float, aborted: bool = False,
        stopped: bool = False, failure: TurnError | None = None,
    ) -> tuple[TurnEventKind, TurnResult]:
        elapsed = max(0.0, time.monotonic() - started_monotonic)
        final_text = self.final_output if self.final_output is not None else "".join(self.answer)
        common = {
            "messages": self._messages(final_text),
            "final_output": final_text,
            "usage": self._usage(),
            "started_at": started_at,
            # measured on the monotonic clock: the wall clock can step back mid-turn
            # (NTP / WSL resync), and a completed_at before started_at is rejected
            "completed_at": started_at + elapsed,
            "duration_ms": int(elapsed * 1000),
            "provider_data": json_safe({
                "task_id": self.task_id, "segments": self.seg + 1, "tool_calls": self.tool_calls,
                "asks": self.asks, "da_status": self.status,
            }),
        }
        if aborted or stopped or self.status == "cancelled":
            kind = TurnTerminationKind.HARNESS_STOP if stopped else TurnTerminationKind.USER_ABORT
            return TurnEventKind.ABORTED, TurnResult(
                status=TurnStatus.INTERRUPTED,
                termination=TurnTermination(kind=kind, message=f"{self.engine} task was cancelled"),
                **common,
            )
        error = failure or self.error
        if error is not None or self.status == "failed":
            return TurnEventKind.FAILED, TurnResult(
                status=TurnStatus.FAILED,
                error=error or TurnError(message=f"{self.engine} task failed", category="sdk_error"),
                **common,
            )
        return TurnEventKind.FINISHED, TurnResult(status=TurnStatus.COMPLETED, stop_reason="completed", **common)
