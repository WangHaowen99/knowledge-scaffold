"""da-analyst plugin: conversations outlive page connections and keep a transcript.

Needs jiuwenswarm and openjiuwen (run inside the Swarm image, see deploy/hc/README.md).
"""

from __future__ import annotations

import asyncio
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
        self.provider_session_id = "da-session"
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

    async def stop(self) -> None:
        self.stopped = True


class FakeProvider:
    class card:  # noqa: N801 - mirrors provider().card
        optional_host_capabilities = frozenset()

    @staticmethod
    def create(config: dict[str, Any]) -> FakeHarness:
        del config
        return FakeHarness()


def wires(*items: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"seq": i, "item_id": None, **item} for i, item in enumerate(items)]


def test_transcript_folds_output_deltas_and_skips_unused_events(ext: Any) -> None:
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

    transcript.flush()
    loaded = ext.Transcript.load("s1")
    assert loaded.events == transcript.events
    assert loaded.summary()["turns"] == 1
    assert loaded.kb_ids == ["kb1"]


def test_transcript_without_a_question_is_not_saved(ext: Any) -> None:
    transcript = ext.Transcript("empty", "deepanalyze")
    transcript.record({"t": "state", "state": "idle"})
    transcript.flush()
    assert ext.Transcript.load("empty") is None
    assert ext.list_history() == []


async def test_conversation_keeps_running_after_the_page_leaves(ext: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(ext, "IDLE_STOP_S", 0.05)
    seen: list[dict[str, Any]] = []

    async def page(payload: dict[str, Any]) -> None:
        seen.append(payload)

    convo, was_live = await ext.open_conversation("sess-1", "deepanalyze", ["kb1"])
    assert not was_live
    convo.attach(page)
    harness = FakeHarness.instances[0]
    assert harness.context.host_session_id == "sess-1"

    receipt = await convo.send("问题一", ["kb1"], "c1", steer=False)
    assert convo.transcript.events[0] == {"t": "user", "client_id": "c1", "text": "问题一", "turn_id": receipt.turn_id}
    await harness.queue.put(_envelope(ext, "running"))
    await _settle()
    assert convo.running and seen[-1] == {"seq": 1, "turn_id": None, "item_id": None, "t": "state", "state": "running"}

    convo.detach(page)  # page switched away mid-analysis
    await asyncio.sleep(0.1)
    assert not harness.stopped and ext.LIVE["sess-1"] is convo

    again, was_live = await ext.open_conversation("sess-1", "deepanalyze", [])
    assert again is convo and was_live

    await harness.queue.put(_envelope(ext, "idle"))  # analysis ends with nobody watching
    await asyncio.sleep(0.1)
    assert harness.stopped and "sess-1" not in ext.LIVE

    reopened, was_live = await ext.open_conversation("sess-1", "deepanalyze", [])
    assert not was_live and reopened is not convo
    assert [e["t"] for e in reopened.transcript.events] == ["user", "state", "state"]
    assert FakeHarness.instances[1].context.host_session_id == "sess-1"  # same DA session id
    await reopened.close()


async def test_history_lists_newest_first_and_deletes(ext: Any) -> None:
    for sid, text in (("a", "旧问题"), ("b", "新问题")):
        convo, _ = await ext.open_conversation(sid, "loopforest", [])
        await convo.send(text, [], "c", steer=False)
        await convo.close()
        await asyncio.sleep(0.02)  # distinct mtimes

    items = ext.list_history()
    assert [(i["id"], i["title"], i["engine"], i["live"]) for i in items] == [
        ("b", "新问题", "loopforest", False), ("a", "旧问题", "loopforest", False)]

    await ext.delete_conversation("b")
    assert [i["id"] for i in ext.list_history()] == ["a"]
    await ext.delete_conversation("../a")  # not a session id: ignored
    assert json.loads(ext.Transcript.path_for("a").read_text(encoding="utf-8"))["title"] == "旧问题"


async def test_invalid_session_id_gets_a_fresh_one(ext: Any) -> None:
    convo, _ = await ext.open_conversation("../../etc/passwd", "deepanalyze", [])
    assert ext.SESSION_ID_RE.match(convo.host_session_id)
    await convo.close()


def _envelope(ext: Any, state: str) -> Any:
    from openjiuwen.harness_protocol import HarnessState, StateChangedEvent

    @dataclass
    class Envelope:
        event: Any
        sequence: int = 1
        turn_id: Any = None
        item_id: Any = None

    current = HarnessState(state)
    previous = HarnessState.IDLE if current is HarnessState.RUNNING else HarnessState.RUNNING
    return Envelope(event=StateChangedEvent(old=previous, new=current))


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)
