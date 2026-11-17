"""da-analyst plugin: conversations outlive page connections and keep a transcript.

Needs jiuwenswarm and openjiuwen (run inside the Swarm image, see deploy/compose/README.md).
"""

from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

EXTENSION = Path(__file__).resolve().parents[1] / "plugin" / "da-analyst" / "extension.py"


@pytest.fixture()
def ext(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    spec = importlib.util.spec_from_file_location("da_analyst_extension", EXTENSION)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    monkeypatch.setenv("DA_ANALYST_HISTORY_DIR", str(tmp_path))
    monkeypatch.setenv("DA_ANALYST_FILES_DIR", str(tmp_path / "pushed"))
    monkeypatch.setattr(module, "_provider_class", lambda engine: FakeProvider)
    FakeHarness.instances.clear()
    return module


@dataclass
class Receipt:
    turn_id: str
    accepted_mode: Any


class FakeHarness:
    instances: list[FakeHarness] = []

    def __init__(self) -> None:
        self.queue: asyncio.Queue = asyncio.Queue()
        self.context: Any = None
        self.sent: list[Any] = []
        self.stopped = False
        self.aborted = False
        self.stop_delay = 0.0
        self.provider_session_id = "da-session"
        self.waiting: dict[str, asyncio.Task] = {}  # turns blocked on a page question, by request id
        FakeHarness.instances.append(self)

    async def start(self, context: Any) -> None:
        self.context = context

    def events(self) -> Any:
        async def cursor() -> Any:
            while True:
                yield await self.queue.get()

        return cursor()

    async def send(self, content: Any, *, mode: Any) -> Receipt:
        self.sent.append((content, mode))
        return Receipt(turn_id=f"turn-{len(self.sent)}", accepted_mode=mode)

    def ask(self, request: Any) -> asyncio.Task:
        """A turn that asks the page, as DeepAnalyzeHarness does on ``ask_user``."""
        from openjiuwen.harness_protocol import InteractionResponseStatus, ToolApprovalDecision

        async def turn() -> Any:
            response = await self.context.interactions.handle(request)
            if getattr(response, "status", None) is not InteractionResponseStatus.COMPLETED and getattr(
                    response, "decision", None) not in (ToolApprovalDecision.ALLOW, ToolApprovalDecision.DENY):
                # not answered: DA's task is cancelled and the turn ends aborted
                await self.queue.put(_turn_end("aborted", request.turn_id))
                await self.queue.put(_envelope(None, "idle"))
            return response

        task = asyncio.create_task(turn())
        self.waiting[request.request_id] = task
        return task

    async def _withdraw(self, reason: Any) -> None:
        # like SerializedTurnHarness: cancel the pending interactions, then wait for the turn the
        # way _do_stop awaits its supervisor (catching Exception only)
        waiting, self.waiting = self.waiting, {}
        for request_id, task in waiting.items():
            if not task.done():
                await self.context.interactions.cancel(request_id, reason=reason)
            try:
                await task
            except Exception:  # noqa: BLE001
                pass

    async def abort(self) -> None:
        from openjiuwen.harness_protocol import InteractionCancelReason

        self.aborted = True  # DeepAnalyzeHarness posts /cancel for the task here
        await self._withdraw(InteractionCancelReason.TURN_ABORTED)

    async def stop(self) -> None:
        from openjiuwen.harness_protocol import InteractionCancelReason

        # like SerializedTurnHarness: questions are withdrawn, the stop is reported as a state change
        self.stopped = True
        await asyncio.sleep(self.stop_delay)
        await self._withdraw(InteractionCancelReason.HARNESS_STOPPED)
        await self.queue.put(_envelope(None, "terminated"))
        for _ in range(3):
            await asyncio.sleep(0)


class FakeProvider:
    class card:  # noqa: N801 - mirrors provider().card
        optional_host_capabilities = frozenset()

    @staticmethod
    def create(config: dict[str, Any]) -> FakeHarness:
        del config
        return FakeHarness()


class DownProvider(FakeProvider):
    @staticmethod
    def create(config: dict[str, Any]) -> FakeHarness:
        harness = FakeProvider.create(config)

        async def start(context: Any) -> None:
            raise RuntimeError("DeepAnalyze is not ready")

        harness.start = start
        return harness


class FakeSocket:
    """A page connection: `put` what the page sends, read what it got from `out`."""

    def __init__(self) -> None:
        self.inbox: asyncio.Queue = asyncio.Queue()
        self.out: list[dict[str, Any]] = []
        self.slow: dict[str, float] = {}  # output_id -> seconds a write of it takes

    async def accept(self) -> None:
        return None

    async def receive_text(self) -> str:
        text = await self.inbox.get()
        if text is None:
            raise RuntimeError("disconnected")
        return text

    async def send_text(self, text: str) -> None:
        msg = json.loads(text)
        await asyncio.sleep(self.slow.get(str(msg.get("output_id")), 0))
        self.out.append(msg)

    def put(self, **msg: Any) -> None:
        self.inbox.put_nowait(json.dumps(msg))

    def leave(self) -> None:
        self.inbox.put_nowait(None)

    def got(self, kind: str) -> list[dict[str, Any]]:
        return [m for m in self.out if m.get("t") == kind]


def serve(ext: Any, socket: FakeSocket) -> asyncio.Task:
    return asyncio.create_task(ext.DaAnalystPlugin()._serve(socket))


def wires(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"seq": i, "item_id": None, **item} for i, item in enumerate(items)]


async def test_transcript_folds_output_deltas_and_skips_unused_events(ext: Any) -> None:
    transcript = ext.Transcript("s1", "deepanalyze")
    transcript.add_user("c1", "  对比   报警值和联锁值  ", ["kb1"])
    for wire in wires(
        {"t": "output", "turn_id": "t1", "output_id": "o1", "channel": "answer", "op": "delta", "content": "第一"},
        {"t": "usage", "turn_id": "t1", "total_tokens": 10},
        {"t": "output", "turn_id": "t1", "output_id": "o2", "channel": "reasoning", "op": "delta", "content": "想"},
        {"t": "output", "turn_id": "t1", "output_id": "o1", "channel": "answer", "op": "delta", "content": "段"},
        {"t": "output", "turn_id": "t1", "output_id": "o2", "channel": "reasoning", "op": "replace", "content": "重写"},
    ):
        transcript.record(wire)

    outputs = [e for e in transcript.events if e["t"] == "output"]
    assert [(e["output_id"], e["content"]) for e in outputs] == [("o1", "第一段"), ("o2", "重写")]
    assert all("seq" not in e for e in transcript.events)
    transcript.record({"seq": 9, "t": "tool", "turn_id": "t1", "item_id": "call-2", "phase": "started", "name": "kb_search"})
    assert transcript.events[-1]["item_id"] == "call-2"  # the page keys tool cards by it
    assert not any(e["t"] == "usage" for e in transcript.events)
    assert transcript.title == "对比 报警值和联锁值"

    await transcript.save()
    loaded = ext.Transcript.load("s1")
    assert loaded.events == transcript.events
    assert loaded.summary()["turns"] == 1
    assert loaded.kb_ids == ["kb1"]


async def test_transcript_keeps_what_da_delivers_but_not_streamed_sub_agent_output(ext: Any) -> None:
    transcript = ext.Transcript("s1", "deepanalyze")
    transcript.add_user("c1", "合成问题", [])
    long_card = "表" * (ext.MAX_TRANSCRIPT_OUTPUT + 10)
    for wire in wires(
        {"t": "provider", "turn_id": "t1", "event_type": "push_content",
         "payload": {"type": "markdown", "title": "合成报告", "data": long_card}},
        {"t": "provider", "turn_id": "t1", "event_type": "todo_update",
         "payload": {"todos": [{"id": "1", "content": "检索", "status": "completed"}]}},
        {"t": "provider", "turn_id": "t1", "event_type": "workflow_event",
         "payload": {"type": "workflow_start", "workflowId": "w1", "agentCount": 2}},
        {"t": "provider", "turn_id": "t1", "event_type": "workflow_event",
         "payload": {"type": "workflow_agent_chunk", "workflowId": "w1", "agentId": "a1"}},
        {"t": "provider", "turn_id": "t1", "event_type": "subagent_start", "payload": {}},
    ):
        transcript.record(wire)

    kept = [(e["event_type"], e["payload"].get("type")) for e in transcript.events if e["t"] == "provider"]
    assert kept == [("push_content", "markdown"), ("todo_update", None), ("workflow_event", "workflow_start")]
    card = next(e for e in transcript.events if e.get("event_type") == "push_content")
    assert len(card["payload"]["data"]) == ext.MAX_TRANSCRIPT_OUTPUT + 1  # clipped, marked with "…"


async def test_transcript_without_a_question_is_not_saved(ext: Any) -> None:
    transcript = ext.Transcript("empty", "deepanalyze")
    transcript.record({"t": "state", "state": "idle"})
    await transcript.save()
    assert ext.Transcript.load("empty") is None
    assert await ext.list_history() == []


async def test_conversation_keeps_running_after_the_page_leaves(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext, "IDLE_STOP_S", 0.05)
    seen: list[dict[str, Any]] = []

    async def page(payload: dict[str, Any]) -> None:
        seen.append(payload)

    convo, was_live = await ext.open_conversation("sess-1", "deepanalyze")
    assert not was_live
    convo.attach(page)
    harness = FakeHarness.instances[0]
    assert harness.context.host_session_id == "sess-1"

    receipt = await convo.send("问题一", ["kb1"], "c1", steer=False, origin=page)
    assert convo.transcript.events[0] == {"t": "user", "client_id": "c1", "text": "问题一", "turn_id": receipt.turn_id}
    await harness.queue.put(_envelope(ext, "running"))
    await _settle()
    assert convo.running and seen[-1] == {"seq": 1, "turn_id": None, "item_id": None, "t": "state", "state": "running"}

    convo.detach(page)  # page switched away mid-analysis
    await asyncio.sleep(0.1)
    assert not harness.stopped and ext.LIVE["sess-1"] is convo

    again, was_live = await ext.open_conversation("sess-1", "deepanalyze")
    assert again is convo and was_live

    await harness.queue.put(_envelope(ext, "idle"))  # analysis ends with nobody watching
    await asyncio.sleep(0.1)
    assert harness.stopped and "sess-1" not in ext.LIVE

    reopened, was_live = await ext.open_conversation("sess-1", "deepanalyze")
    assert not was_live and reopened is not convo
    # the last state is the stop itself
    assert [e.get("state", e["t"]) for e in reopened.transcript.events] == ["user", "running", "idle", "terminated"]
    assert FakeHarness.instances[1].context.host_session_id == "sess-1"  # same DA session id
    await reopened.close()


async def test_history_lists_newest_first_and_deletes(ext: Any) -> None:
    for sid, text in (("a", "旧问题"), ("b", "新问题")):
        convo, _ = await ext.open_conversation(sid, "loopforest")
        await convo.send(text, [], "c", steer=False)
        await convo.close()
        await asyncio.sleep(0.02)  # distinct mtimes

    items = await ext.list_history()
    assert [(i["id"], i["title"], i["engine"], i["live"]) for i in items] == [
        ("b", "新问题", "loopforest", False), ("a", "旧问题", "loopforest", False)]

    await ext.delete_conversation("b")
    assert [i["id"] for i in await ext.list_history()] == ["a"]
    await ext.delete_conversation("../a")  # not a session id: ignored
    assert json.loads(ext.Transcript.path_for("a").read_text(encoding="utf-8"))["title"] == "旧问题"


async def test_invalid_session_id_gets_a_fresh_one(ext: Any) -> None:
    for bad in ("../../etc/passwd", "ok\n"):
        convo, _ = await ext.open_conversation(bad, "deepanalyze")
        assert convo.host_session_id != bad and ext.SESSION_ID_RE.fullmatch(convo.host_session_id)
        await convo.close()


async def test_history_list_reads_only_the_summary(ext: Any) -> None:
    transcript = ext.Transcript("big", "deepanalyze")
    transcript.add_user("c1", "长对话", [])
    for i in range(50):
        transcript.record({"t": "tool", "turn_id": "t1", "item_id": f"i{i}", "phase": "completed", "result": "检" * 2000})
    await transcript.save()
    path = ext.Transcript.path_for("big")
    raw = path.read_bytes()
    path.write_bytes(raw[: len(raw) // 2])  # the events no longer parse; the summary ahead of them does

    assert [(i["id"], i["title"], i["turns"]) for i in await ext.list_history()] == [("big", "长对话", 1)]


async def test_deleted_conversation_stays_deleted(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext, "IDLE_STOP_S", 0.1)
    convo, _ = await ext.open_conversation("gone", "deepanalyze")
    await convo.send("问题", ["kb1"], "c1", steer=False)
    await convo.transcript.save()
    path = ext.Transcript.path_for("gone")
    assert path.exists()

    # opened earlier, nobody watching now: its harness reports the stop after delete has closed it
    await ext.delete_conversation("gone")
    assert not path.exists() and "gone" not in ext.LIVE
    await asyncio.sleep(0.3)  # past the idle stop and the debounced save
    assert not path.exists()
    assert await ext.list_history() == []


async def test_page_leaving_does_not_cancel_its_question(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    async def slow_kbs() -> list[dict[str, Any]]:
        await asyncio.sleep(0.05)
        return [{"id": "kb1", "name": "k", "documents": 1, "backend": ""}]

    monkeypatch.setattr(ext, "_list_kbs", slow_kbs)
    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="leave")
    await asyncio.sleep(0.02)
    socket.put(type="send", text="问题", kb_ids=[], client_id="c1")
    await asyncio.sleep(0.01)
    socket.leave()  # switching Swarm pages reloads the iframe right after sending
    await task
    await asyncio.sleep(0.1)

    harness = FakeHarness.instances[0]
    assert [content.content for content, _ in harness.sent] == ["问题"]
    assert ext.LIVE["leave"].transcript.events[0]["text"] == "问题"
    assert not ext.LIVE["leave"].subscribers


async def test_page_leaving_does_not_cancel_a_delete(ext: Any) -> None:
    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="del")
    await asyncio.sleep(0.02)
    convo = ext.LIVE["del"]
    await convo.send("问题", ["kb1"], "c1", steer=False)
    await convo.transcript.save()
    FakeHarness.instances[0].stop_delay = 0.05

    socket.put(type="delete", host_session_id="del")
    await asyncio.sleep(0.01)  # stopping the harness
    socket.leave()
    await task
    await asyncio.sleep(0.15)
    assert not ext.Transcript.path_for("del").exists() and "del" not in ext.LIVE


async def test_history_entry_is_readable_while_its_engine_is_down(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    old = ext.Transcript("old", "loopforest")
    old.add_user("c1", "以前的问题", ["kb1"])
    await old.save()

    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="current")
    await asyncio.sleep(0.02)
    current = ext.LIVE["current"]
    assert current.subscribers

    monkeypatch.setattr(ext, "_provider_class", lambda engine: DownProvider)
    socket.put(type="start", host_session_id="old")
    await asyncio.sleep(0.02)
    shown = socket.got("started")[-1]
    assert shown["host_session_id"] == "old" and shown["readonly"] and not shown["live"]
    assert [e["text"] for e in shown["transcript"]] == ["以前的问题"]
    assert shown["engine"] == "loopforest" and shown["kb_ids"] == ["kb1"]
    assert socket.out[-1]["t"] == "error" and "not ready" in socket.out[-1]["message"]
    assert not current.subscribers  # the page no longer shows it
    assert "old" not in ext.LIVE

    socket.put(type="start", host_session_id="")  # a new conversation has nothing to show
    await asyncio.sleep(0.02)
    assert socket.got("started")[-1] is shown and socket.out[-1]["t"] == "error"
    socket.leave()
    await task


async def test_idle_stop_ends_a_turn_waiting_for_the_page(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from openjiuwen.harness_protocol import InteractionResponseStatus

    monkeypatch.setattr(ext, "IDLE_STOP_S", 0.05)
    closing: list[asyncio.Task] = []
    monkeypatch.setattr(ext, "_background", lambda coro: closing.append(asyncio.ensure_future(coro)))
    convo, _ = await ext.open_conversation("asks", "deepanalyze")
    await convo.send("问题", ["kb1"], "c1", steer=False)
    harness = FakeHarness.instances[0]
    await harness.queue.put(_envelope(ext, "running"))
    await _settle()
    turn = harness.ask(_question())
    await asyncio.sleep(0.2)  # nobody watching, and the turn waits for an answer

    assert harness.aborted and harness.stopped and "asks" not in ext.LIVE  # the engine is told to drop the task
    assert closing and closing[-1].done() and not closing[-1].cancelled() and closing[-1].exception() is None
    assert (await turn).status is InteractionResponseStatus.CANCELLED  # withdrawn, not cancelled
    assert convo.pump is None
    kinds = [e.get("kind") or e.get("state") or e["t"] for e in ext.Transcript.load("asks").events]
    assert kinds[-4:] == ["ask_cancelled", "aborted", "idle", "terminated"]


async def test_answered_question_keeps_a_running_turn_alive(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext, "IDLE_STOP_S", 0.05)
    convo, _ = await ext.open_conversation("busy", "deepanalyze")
    harness = FakeHarness.instances[0]
    await harness.queue.put(_envelope(ext, "running"))
    await _settle()
    turn = harness.ask(_question())
    await _settle()
    await convo.answer("r1", {"content": "第一个"})
    await asyncio.sleep(0.15)
    assert not harness.stopped and (await turn).content == "第一个"
    await convo.close()


async def test_delete_while_a_question_waits(ext: Any) -> None:
    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="ask-del")
    await asyncio.sleep(0.02)
    convo = ext.LIVE["ask-del"]
    await convo.send("问题", ["kb1"], "c1", steer=False)
    turn = FakeHarness.instances[0].ask(_question())
    await _settle()

    socket.put(type="delete", host_session_id="ask-del")
    await asyncio.sleep(0.05)
    assert socket.got("history") and socket.out[-1]["t"] == "history"
    assert turn.done() and convo.pump is None and "ask-del" not in ext.LIVE
    assert not ext.Transcript.path_for("ask-del").exists()
    socket.leave()
    await task


async def test_delete_right_after_an_idle_stop_stays_deleted(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext, "IDLE_STOP_S", 0.05)
    monkeypatch.setattr(ext, "FLUSH_DELAY_S", 0.2)
    convo, _ = await ext.open_conversation("reclaimed", "deepanalyze")
    await convo.send("问题", ["kb1"], "c1", steer=False)
    FakeHarness.instances[0].ask(_question())
    for _ in range(50):
        await asyncio.sleep(0.01)
        if "reclaimed" not in ext.LIVE:  # stopped while waiting for the page
            break

    await ext.delete_conversation("reclaimed")  # within the save delay
    await asyncio.sleep(0.4)
    assert not ext.Transcript.path_for("reclaimed").exists()


async def test_abort_while_a_question_waits_ends_the_turn(ext: Any) -> None:
    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="abort")
    await asyncio.sleep(0.02)
    socket.put(type="send", text="问题", kb_ids=["kb1"], client_id="c1")
    await asyncio.sleep(0.02)
    harness = FakeHarness.instances[0]
    await harness.queue.put(_envelope(ext, "running"))
    harness.ask(_question("turn-1"))
    await asyncio.sleep(0.02)

    socket.put(type="abort")
    await asyncio.sleep(0.05)
    assert [m.get("kind") for m in socket.got("turn")] == ["aborted"]
    assert not ext.LIVE["abort"].running and not socket.got("error")
    socket.leave()
    await task


async def test_failed_steer_leaves_no_trace(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    class SteerFails(FakeHarness):
        async def send(self, content: Any, *, mode: Any) -> Receipt:
            if mode.value == "steer":
                raise RuntimeError("there is no active turn to steer")
            return await super().send(content, mode=mode)

    class Provider(FakeProvider):
        @staticmethod
        def create(config: dict[str, Any]) -> FakeHarness:
            return SteerFails()

    monkeypatch.setattr(ext, "_provider_class", lambda engine: Provider)
    first, second = FakeSocket(), FakeSocket()
    tasks = [serve(ext, first), serve(ext, second)]
    for socket in (first, second):
        socket.put(type="start", host_session_id="steer")
        await asyncio.sleep(0.02)
    first.put(type="send", text="补充", steer=True, client_id="s1")
    await asyncio.sleep(0.02)

    assert first.out[-1]["t"] == "error" and first.out[-1]["client_id"] == "s1"  # the page gives the text back
    assert not second.got("steer") and not any(e["t"] == "steer" for e in ext.LIVE["steer"].transcript.events)
    for socket in (first, second):
        socket.leave()
    await asyncio.gather(*tasks)


async def test_deleted_id_is_not_reused(ext: Any) -> None:
    convo, _ = await ext.open_conversation("old-id", "deepanalyze")
    await convo.send("问题", ["kb1"], "c1", steer=False)
    await convo.close()
    await ext.delete_conversation("old-id")

    # a page (or an engine key) that still remembers the id gets a new conversation, not DA's old context
    again, was_live = await ext.open_conversation("old-id", "deepanalyze")
    assert not was_live and again.host_session_id != "old-id" and not again.transcript.events
    assert FakeHarness.instances[-1].context.host_session_id == again.host_session_id
    await again.close()


async def test_other_pages_are_told_about_a_delete(ext: Any) -> None:
    first, second = FakeSocket(), FakeSocket()
    tasks = [serve(ext, first), serve(ext, second)]
    for socket in (first, second):
        socket.put(type="start", host_session_id="shown")
        await asyncio.sleep(0.02)
    first.put(type="delete", host_session_id="shown")
    await asyncio.sleep(0.05)

    assert second.got("deleted") == [{"t": "deleted", "host_session_id": "shown"}]
    assert not first.got("deleted")  # it asked for the delete itself
    for socket in (first, second):
        socket.leave()
    await asyncio.gather(*tasks)


async def test_started_snapshot_does_not_pick_up_later_deltas(ext: Any) -> None:
    streaming, _ = await ext.open_conversation("streaming", "deepanalyze")
    await streaming.send("问题", ["kb1"], "c1", steer=False)
    await FakeHarness.instances[0].queue.put(_output("new", "A"))
    await ext.open_conversation("watched", "deepanalyze")
    await _settle()

    socket = FakeSocket()
    socket.slow["old"] = 0.05  # a write that holds the socket while the page switches
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="watched")
    await asyncio.sleep(0.01)
    await FakeHarness.instances[1].queue.put(_output("old", "x"))
    await asyncio.sleep(0.005)
    socket.put(type="start", host_session_id="streaming")
    await asyncio.sleep(0.005)
    await FakeHarness.instances[0].queue.put(_output("new", "B"))
    await asyncio.sleep(0.15)
    socket.leave()
    await task

    started = next(m for m in socket.out if m.get("t") == "started" and m["host_session_id"] == "streaming")
    later = socket.out[socket.out.index(started) + 1:]
    shown = "".join(e["content"] for e in started["transcript"] if e["t"] == "output")
    shown += "".join(m["content"] for m in later if m.get("output_id") == "new")
    assert shown == "AB"


async def test_other_pages_see_what_one_page_put_in(ext: Any) -> None:
    from openjiuwen.harness_protocol import UserInputRequest

    first, second = FakeSocket(), FakeSocket()
    tasks = [serve(ext, first), serve(ext, second)]
    for socket in (first, second):
        socket.put(type="start", host_session_id="shared")
        await asyncio.sleep(0.02)
    first.put(type="send", text="问题", kb_ids=["kb1"], client_id="c1")
    await asyncio.sleep(0.02)
    first.put(type="send", text="补充", steer=True)
    await asyncio.sleep(0.02)
    convo = ext.LIVE["shared"]
    question = asyncio.create_task(convo.interactions.handle(
        UserInputRequest(request_id="r1", prompt="选哪个？", provider_session_id="s", turn_id="turn-1")))
    await asyncio.sleep(0.02)
    first.put(type="answer", request_id="r1", content="第一个", label="第一个")
    await asyncio.sleep(0.02)

    assert [m["t"] for m in second.out if m["t"] in ("user", "steer", "answered")] == ["user", "steer", "answered"]
    assert second.got("user")[0]["client_id"] == "c1" and second.got("steer")[0]["text"] == "补充"
    assert not first.got("user") and not first.got("steer") and not first.got("answered")  # it drew them itself
    await question
    for socket in (first, second):
        socket.leave()
    await asyncio.gather(*tasks)


async def test_empty_selection_means_every_knowledge_base(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    kbs = [{"id": "kb1", "name": "a", "documents": 1, "backend": ""}, {"id": "kb2", "name": "b", "documents": 1, "backend": ""}]

    async def list_kbs() -> list[dict[str, Any]]:
        return list(kbs)

    monkeypatch.setattr(ext, "_list_kbs", list_kbs)
    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="start", host_session_id="all")
    await asyncio.sleep(0.02)
    assert socket.got("started")[0]["kb_ids"] is None  # a new conversation keeps the page's selection
    socket.put(type="send", text="问题一", kb_ids=[], client_id="c1")
    await asyncio.sleep(0.02)
    kbs.clear()  # listing fails or finds nothing: the harness falls back to its own catalog
    socket.put(type="send", text="问题二", kb_ids=[], client_id="c2")
    await asyncio.sleep(0.02)

    sent = [content.metadata for content, _ in FakeHarness.instances[0].sent]
    assert list(sent[0]["kb_ids"]) == ["kb1", "kb2"] and "kb_ids" not in sent[1]
    # and no session-wide scope from the start for the harness to fall back on instead of its catalog
    assert "kb_ids" not in FakeHarness.instances[0].context.metadata
    assert ext.LIVE["all"].transcript.kb_ids == []  # stays "all" when reopened

    socket.put(type="start", host_session_id="")
    await asyncio.sleep(0.02)
    socket.put(type="start", host_session_id="all")
    await asyncio.sleep(0.02)
    assert socket.got("started")[-1]["kb_ids"] == []
    socket.leave()
    await task


async def test_locks_and_checkpoints_do_not_pile_up(ext: Any) -> None:
    for _ in range(3):
        convo, _ = await ext.open_conversation("", "deepanalyze")
        await convo.close()
    assert ext._OPENING._locks == {}

    @dataclass
    class Checkpoint:
        host_session_id: str
        checkpoint_id: str = "cp"
        sequence: int = 1

    store = ext.CheckpointStore(limit=2)
    for sid in ("a", "b", "a", "c"):
        await store.save(Checkpoint(sid), reason=None)
    assert list(store.latest) == ["a", "c"]

    ext.CHECKPOINTS.latest["gone"] = Checkpoint("gone")
    await ext.delete_conversation("gone")
    assert "gone" not in ext.CHECKPOINTS.latest


def _envelope(ext: Any, state: str) -> Any:
    from openjiuwen.harness_protocol import HarnessState, StateChangedEvent

    del ext
    current = HarnessState(state)
    previous = HarnessState.IDLE if current is HarnessState.RUNNING else HarnessState.RUNNING
    return Envelope(event=StateChangedEvent(old=previous, new=current))


def _question(turn_id: str = "t1") -> Any:
    from openjiuwen.harness_protocol import UserInputRequest

    return UserInputRequest(request_id="r1", prompt="选哪个？", provider_session_id="s", turn_id=turn_id)


def _turn_end(kind: str, turn_id: str) -> Any:
    from openjiuwen.harness_protocol import (
        TurnEventKind, TurnLifecycleEvent, TurnResult, TurnStatus, TurnTermination, TurnTerminationKind,
    )

    result = TurnResult(status=TurnStatus.INTERRUPTED, termination=TurnTermination(kind=TurnTerminationKind.USER_ABORT))
    return Envelope(event=TurnLifecycleEvent(kind=TurnEventKind(kind), result=result), turn_id=turn_id)


def _output(output_id: str, content: str) -> Any:
    from openjiuwen.harness_protocol import OutputEvent, OutputKind, OutputOperation

    event = OutputEvent(output_id=output_id, kind=OutputKind.TEXT, content=content, operation=OutputOperation.DELTA)
    return Envelope(event=event, turn_id="turn-1")


@dataclass
class Envelope:
    event: Any
    sequence: int = 1
    turn_id: Any = None
    item_id: Any = None


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


async def test_a_stuck_engine_does_not_block_close_forever(ext, monkeypatch):
    monkeypatch.setattr(ext, "CLOSE_STEP_TIMEOUT_S", 0.05)

    async def hang() -> None:
        await asyncio.Event().wait()

    await asyncio.wait_for(ext._quietly(hang, "stop"), 2)


async def test_hello_names_backends_whose_knowledge_bases_are_missing(ext, monkeypatch):
    import httpx

    def gateway(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={
            "items": [{"id": "kb1", "name": "合成库", "stats": {"completed": 2}, "backend": {"label": "kb-service"}}],
            "unavailable_backends": [{"id": "wk", "label": "WeKnora", "type": "weknora", "error": "connection refused",
                                      "id_prefix": "wk_", "knowledge_bases": [{"id": "wk_kb1", "name": "合成库二"}]}],
        })

    real_client = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(gateway), **kw))
    monkeypatch.setenv("KB_SERVICE_URL", "http://kb.example.com")

    async def no_engines() -> list[dict[str, Any]]:
        return []

    monkeypatch.setattr(ext, "_engines", no_engines)
    socket = FakeSocket()
    task = serve(ext, socket)
    socket.put(type="hello")
    await asyncio.sleep(0.05)
    socket.leave()
    await task
    hello = socket.got("hello")[0]
    assert hello["kbs"] == [{"id": "kb1", "name": "合成库", "documents": 2, "backend": "kb-service"}]
    assert hello["unavailable"] == [{"label": "WeKnora", "id_prefix": "wk_",
                                     "knowledge_bases": [{"id": "wk_kb1", "name": "合成库二"}]}]
    # "every knowledge base" still names the ones on the backend that is down, so the tools report them
    assert [kb["id"] for kb in await ext._list_kbs()] == ["kb1", "wk_kb1"]


# ---- files DA pushes ---------------------------------------------------------------------------------


async def _reply(socket: FakeSocket, client_id: str) -> dict[str, Any]:
    for _ in range(200):
        found = [m for m in socket.out if m.get("client_id") == client_id]
        if found:
            return found[0]
        await asyncio.sleep(0.01)
    raise AssertionError(f"no reply to {client_id}")


async def test_pushed_files_reach_the_page_by_name_in_pieces(ext: Any) -> None:
    from openjiuwen.harness_protocol import ItemEventKind, ItemLifecycleEvent

    convo, _ = await ext.open_conversation("sess-f", "deepanalyze")
    files = Path(FakeHarness.instances[-1].context.cwd)
    assert files.is_dir() and files == ext._files_dir("sess-f")
    payload = bytes(range(256)) * 5000  # 1.28 MB: three pieces
    (files / "合成表.csv").write_bytes(payload)
    item = ItemLifecycleEvent(kind=ItemEventKind.COMPLETED, item_type="file", data={
        "path": str(files / "合成表.csv"), "name": "合成表.csv", "size": len(payload), "mime_type": "text/csv",
        "title": "合成"})
    wire = ext.event_to_wire(Envelope(event=item, turn_id="turn-1", item_id="i1:file"))
    assert (wire["t"], wire["item_type"], wire["name"], wire["size"]) == ("item", "file", "合成表.csv", len(payload))
    assert "path" not in wire  # where it lies on the host stays on the host
    convo.transcript.record(wire)
    assert convo.transcript.events[-1]["name"] == "合成表.csv"  # still downloadable after a reload

    socket = FakeSocket()
    task = serve(ext, socket)
    got = b""
    while True:
        client_id = f"c{len(got)}"
        socket.put(type="file", host_session_id="sess-f", name="合成表.csv", offset=len(got), client_id=client_id)
        piece = await _reply(socket, client_id)
        assert piece["t"] == "file_chunk" and piece["size"] == len(payload)
        got += base64.b64decode(piece["data"])
        if piece["eof"]:
            break
    assert got == payload
    socket.put(type="file", host_session_id="sess-f", name="../sess-f.json", offset=0, client_id="bad")
    assert (await _reply(socket, "bad"))["t"] == "error"
    socket.leave()
    await task
    await ext.delete_conversation("sess-f")
    assert not files.exists()


def test_only_plain_names_of_files_in_the_conversation_are_served(ext: Any, tmp_path: Path) -> None:
    files = ext._files_dir("sess-g")
    files.mkdir(parents=True)
    (files / "ok.txt").write_text("好", encoding="utf-8")
    (files / ".ok.txt.1234.part").write_text("half", encoding="utf-8")
    (files / "sub").mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("no", encoding="utf-8")
    (files / "link.txt").symlink_to(outside)
    assert base64.b64decode(ext.read_pushed_file("sess-g", "ok.txt", 0)["data"]).decode() == "好"
    for name in ("../outside.txt", str(outside), ".ok.txt.1234.part", "link.txt", "sub", "", "missing.txt"):
        with pytest.raises((ValueError, FileNotFoundError)):
            ext.read_pushed_file("sess-g", name, 0)
    with pytest.raises(ValueError):
        ext.read_pushed_file("../sess-g", "ok.txt", 0)
