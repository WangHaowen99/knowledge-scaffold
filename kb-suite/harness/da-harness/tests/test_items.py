"""todo / subagent / file items (shaped for JiuwenSwarm's panels) next to the unchanged ProviderEvents."""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import httpx
import pytest
from openjiuwen.harness_protocol import (
    ItemEventKind, ItemLifecycleEvent, ProviderEvent, TurnEventKind, TurnLifecycleEvent,
)

from test_harness import TASK, FakeDA, context, make, payloads, run_turn, sse

CSV = "季度,收入\nQ1,10\n".encode()
CSV_URL = "/api/sessions/t1:host-1/output/%E5%AD%A3%E5%BA%A6%20%E6%8A%A5%E5%91%8A.csv"
CSV_PATH = "/api/sessions/t1:host-1/output/季度 报告.csv"


class FileDA(FakeDA):
    """FakeDA that also serves the session output download route."""

    def __init__(self) -> None:
        super().__init__()
        self.files: dict[str, bytes] = {}
        self.hosts: list[str] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.hosts.append(request.url.host)
        if request.method == "GET" and request.url.path.startswith("/api/sessions/"):
            self.calls.append(("GET", request.url.path, {}, dict(request.headers)))
            body = self.files.get(request.url.path)
            if body is None:
                return httpx.Response(404, json={"error": "File not found"})
            return httpx.Response(200, headers={"content-type": "text/csv;charset=UTF-8"}, content=body)
        return super().handler(request)

    def downloads(self) -> list[str]:
        return [path for method, path, _, _ in self.calls if method == "GET" and path.startswith("/api/sessions/")]


def plain(value: Any) -> Any:
    """Frozen protocol JSON (mappingproxy / tuple) as dicts and lists."""
    if isinstance(value, Mapping):
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [plain(v) for v in value]
    return value


def items(events: list[Any], item_type: str) -> list[Any]:
    return [e for e in events if isinstance(e.event, ItemLifecycleEvent) and e.event.item_type == item_type]


def push_file(name: str, url: str, *, tool_id: str = "c9") -> list[tuple[str, dict[str, Any]]]:
    result = {"pushed": True, "type": "file", "title": "季度报告", "fileName": name, "fileSize": len(CSV),
              "mimeType": "text/csv", "downloadUrl": url, "timestamp": "2026-11-16T00:00:00.000Z"}
    return [
        ("tool_call", {"id": tool_id, "toolName": "push_file", "input": {"filePath": name}}),
        ("tool_result", {"id": tool_id, "toolName": "push_file", "output": json.dumps(result, ensure_ascii=False),
                         "result": result}),
    ]


def turn(*events: tuple[str, dict[str, Any]]) -> bytes:
    return sse([("start", {"taskId": TASK}), *events, ("done", {"status": "completed", "output": "ok"})])


def wf(kind: str, **fields: Any) -> tuple[str, dict[str, Any]]:
    return "workflow_event", {"type": kind, "workflowId": "wf-1", **fields}


async def test_todo_update_becomes_a_todo_item(monkeypatch: pytest.MonkeyPatch) -> None:
    lines = ["⬜ [t1] 收集 (Q1) 数据 (pending)", "🔄 [t2] 分析趋势 (in_progress)", "✅ [t3] 写报告 (completed)"]
    rendered = {"todos": "\n".join(lines), "action": "bulk_set"}
    listed = {"todos": [{"id": 7, "content": "核对", "activeForm": "正在核对", "status": "running"},
                        {"content": "无 id"}]}
    fake = FakeDA()
    fake.run_stream = turn(("todo_update", rendered), ("todo_update", listed))
    harness = make(fake, monkeypatch)
    await harness.start(context())
    events = await run_turn(harness)
    assert [plain(p.payload) for p in payloads(events, ProviderEvent)] == [rendered, listed]
    todo = items(events, "todo")
    assert [t.item_id for t in todo] == [f"{TASK}:todo"] * 2
    assert {t.event.kind for t in todo} == {ItemEventKind.UPDATED}
    assert plain(todo[0].event.data) == {"todos": [
        {"id": "t1", "content": "收集 (Q1) 数据", "activeForm": "收集 (Q1) 数据", "status": "pending"},
        {"id": "t2", "content": "分析趋势", "activeForm": "分析趋势", "status": "in_progress"},
        {"id": "t3", "content": "写报告", "activeForm": "写报告", "status": "completed"},
    ]}
    assert plain(todo[1].event.data) == {"todos": [{"id": "7", "content": "核对", "activeForm": "正在核对",
                                             "status": "in_progress"}]}
    await harness.stop()


async def test_workflow_members_become_subagent_items(monkeypatch: pytest.MonkeyPatch) -> None:
    events_in = [
        wf("workflow_start", sessionId="s", teamName="评审", mode="council", goal="评估", agentCount=2),
        wf("workflow_agent_start", agentId="tech", role="技术专家", stepId="s1", task="评估技术风险",
           messageCount=0),
        wf("workflow_agent_tool_call", agentId="tech", toolName="kb_search", input={"query": "油泵"}, seq=1,
           callId="m1"),
        wf("workflow_agent_tool_result", agentId="tech", toolName="kb_search", output="3 条", status="ok", seq=1,
           durationMs=5),
        wf("workflow_agent_chunk", agentId="tech", content="初步结论"),
        wf("workflow_agent_complete", agentId="tech", role="技术专家", status="completed", duration=10,
           toolCallCount=1, outputChars=4),
        wf("workflow_agent_start", agentId="biz", role="业务专家", task="评估成本", messageCount=0),
        wf("workflow_complete", sessionId="s", status="cancelled", totalDuration=20, resultCount=1),
    ]
    fake = FakeDA()
    fake.run_stream = turn(*events_in)
    harness = make(fake, monkeypatch)
    await harness.start(context())
    events = await run_turn(harness)
    assert [plain(p.payload) for p in payloads(events, ProviderEvent)] == [data for _, data in events_in]
    subs = items(events, "subagent")
    assert [(s.event.kind, s.item_id) for s in subs] == [
        (ItemEventKind.STARTED, f"{TASK}:sub:da-wf-1-tech"),
        (ItemEventKind.UPDATED, f"{TASK}:sub:da-wf-1-tech"),
        (ItemEventKind.UPDATED, f"{TASK}:sub:da-wf-1-tech"),
        (ItemEventKind.UPDATED, f"{TASK}:sub:da-wf-1-tech"),
        (ItemEventKind.COMPLETED, f"{TASK}:sub:da-wf-1-tech"),
        (ItemEventKind.STARTED, f"{TASK}:sub:da-wf-1-biz"),
        (ItemEventKind.COMPLETED, f"{TASK}:sub:da-wf-1-biz"),
    ]
    started = subs[0].event.data
    assert {k: started[k] for k in ("subagent_id", "sub_session_id", "subagent_type", "display_name", "role",
                                    "task_description", "status", "turn_outcome", "closed_reason", "error",
                                    "lifecycle", "can_send_input", "needs_resume", "revision", "closed_at")} == {
        "subagent_id": "da-wf-1-tech", "sub_session_id": "da-wf-1-tech", "subagent_type": "council",
        "display_name": "tech", "role": "技术专家", "task_description": "评估技术风险", "status": "running",
        "turn_outcome": None, "closed_reason": None, "error": None, "lifecycle": "live", "can_send_input": False,
        "needs_resume": False, "revision": 1, "closed_at": None,
    }
    activities = [s.event.data["activity"] for s in subs[1:4]]
    assert [s.event.data["subagent_id"] for s in subs[1:4]] == ["da-wf-1-tech"] * 3
    assert [(a["seq"], a["kind"], a["task_id"]) for a in activities] == [
        (1, "tool_call", "da-wf-1-tech"), (2, "tool_result", "da-wf-1-tech"), (3, "thinking", "da-wf-1-tech")]
    assert activities[0]["summary"] == 'kb_search({"query":"油泵"})' and activities[0]["tool_call_id"] == "m1"
    assert activities[1]["tool_call_id"] == "m1" and activities[1]["ok"] is True and activities[1]["summary"] == "3 条"
    assert activities[2]["summary"] == "初步结论" and "tool_name" not in activities[2]
    done = subs[4].event.data
    assert (done["status"], done["turn_outcome"], done["closed_reason"], done["lifecycle"], done["revision"]) == (
        "closed", "completed", "completed", "closed", 2)
    assert done["created_at"] == started["created_at"] and done["closed_at"] is not None
    cancelled = subs[6].event.data
    assert (cancelled["subagent_id"], cancelled["turn_outcome"], cancelled["status"]) == (
        "da-wf-1-biz", "cancelled", "closed")
    await harness.stop()


async def test_a_failed_member_carries_its_error(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeDA()
    fake.run_stream = turn(wf("workflow_agent_tool_call", agentId="tech", toolName="kb_search", input={}, seq=1),
                           wf("workflow_agent_complete", agentId="tech", status="failed", error="模型超时"))
    harness = make(fake, monkeypatch)
    await harness.start(context())
    subs = items(await run_turn(harness), "subagent")
    assert [s.event.kind for s in subs] == [ItemEventKind.STARTED, ItemEventKind.UPDATED, ItemEventKind.COMPLETED]
    assert subs[0].event.data["subagent_type"] == "deepanalyze"  # its workflow_start was never seen
    assert plain(subs[2].event.data["error"]) == {"code": "DA_MEMBER_FAILED", "message": "模型超时"}
    assert subs[2].event.data["turn_outcome"] == "failed"
    await harness.stop()


async def test_replayed_member_starts_do_not_reopen_finished_members(monkeypatch: pytest.MonkeyPatch) -> None:
    start = wf("workflow_start", sessionId="s", teamName="评审", mode="parallel", goal="g", agentCount=2)
    tech = wf("workflow_agent_start", agentId="tech", role="技术专家", task="a")
    biz = wf("workflow_agent_start", agentId="biz", role="业务专家", task="b")
    fake = FakeDA()
    fake.run_stream = [
        turn(start, tech, biz, wf("workflow_agent_complete", agentId="tech", status="completed")),
        # the delegated run went on into the next task: DA replays its start events first
        turn(start, tech, biz, wf("workflow_agent_chunk", agentId="biz", content="继续"),
             wf("workflow_complete", status="completed")),
    ]
    harness = make(fake, monkeypatch)
    await harness.start(context())
    first = items(await run_turn(harness, "一"), "subagent")
    second = items(await run_turn(harness, "二"), "subagent")
    assert [(s.event.kind, s.event.data["subagent_id"]) for s in second] == [
        (ItemEventKind.STARTED, "da-wf-1-biz"), (ItemEventKind.UPDATED, "da-wf-1-biz"),
        (ItemEventKind.COMPLETED, "da-wf-1-biz")]
    biz_first = next(s.event.data for s in first if s.event.data["subagent_id"] == "da-wf-1-biz")
    assert second[0].event.data["created_at"] == biz_first["created_at"]
    assert second[0].event.data["revision"] == biz_first["revision"] + 1
    assert second[1].event.data["activity"]["seq"] == 1
    await harness.stop()


async def test_pushed_file_is_fetched_into_the_working_directory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    (tmp_path / "季度 报告.csv").write_text("用户自己的文件", encoding="utf-8")
    fake = FileDA()
    fake.files[CSV_PATH] = CSV
    fake.run_stream = turn(*push_file("季度 报告.csv", CSV_URL),
                           *push_file("季度 报告.csv", CSV_URL, tool_id="c10"))
    harness = make(fake, monkeypatch)
    await harness.start(context(cwd=str(tmp_path)))
    events = await run_turn(harness)
    assert [t.event.kind for t in items(events, "tool")] == [ItemEventKind.STARTED, ItemEventKind.COMPLETED] * 2
    files = items(events, "file")
    assert [(f.event.kind, f.item_id) for f in files] == [
        (ItemEventKind.COMPLETED, f"{TASK}:0:c9:file"), (ItemEventKind.COMPLETED, f"{TASK}:0:c10:file")]
    landed = tmp_path / "季度 报告 (1).csv"
    assert plain(files[0].event.data) == {"path": str(landed), "name": landed.name, "size": len(CSV),
                                   "mime_type": "text/csv", "title": "季度报告"}
    assert files[1].event.data["path"] == str(landed), "the same bytes again reuse the file"
    assert landed.read_bytes() == CSV
    assert (tmp_path / "季度 报告.csv").read_text(encoding="utf-8") == "用户自己的文件"
    assert sorted(p.name for p in tmp_path.iterdir()) == ["季度 报告 (1).csv", "季度 报告.csv"]
    _, _, _, headers = next(call for call in fake.calls if call[1] == CSV_PATH)
    assert headers["x-engine-secret"] == "s3cret" and headers["x-da-session-id"] == "t1:host-1"
    await harness.stop()


async def test_pushed_file_stays_on_an_engine_without_the_route(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture,
) -> None:
    fake = FileDA()  # serves nothing: 404, like a DA image without the download route
    fake.run_stream = turn(*push_file("季度 报告.csv", CSV_URL))
    harness = make(fake, monkeypatch)
    await harness.start(context(cwd=str(tmp_path)))
    with caplog.at_level(logging.WARNING, logger="deepanalyze_harness.harness"):
        events = await run_turn(harness)
    assert fake.downloads() == [CSV_PATH]
    assert not items(events, "file") and len(items(events, "tool")) == 2
    assert payloads(events, TurnLifecycleEvent)[-1].kind is TurnEventKind.FINISHED
    assert "not fetched" in caplog.text and "404" in caplog.text
    assert list(tmp_path.iterdir()) == []
    await harness.stop()


async def test_pushed_file_from_another_origin_is_not_requested(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    fake = FileDA()
    fake.run_stream = turn(*push_file("a.csv", "https://elsewhere.example/api/sessions/x/output/a.csv"),
                           *push_file("b.csv", "//elsewhere.example/api/sessions/x/output/b.csv", tool_id="c10"))
    harness = make(fake, monkeypatch)
    await harness.start(context(cwd=str(tmp_path)))
    events = await run_turn(harness)
    assert "elsewhere.example" not in fake.hosts and not fake.downloads()
    assert not items(events, "file")
    await harness.stop()


async def test_pushed_file_needs_a_working_directory(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FileDA()
    fake.files[CSV_PATH] = CSV
    fake.run_stream = [turn(*push_file("季度 报告.csv", CSV_URL))] * 2
    without = make(fake, monkeypatch)
    await without.start(context())
    assert not items(await run_turn(without), "file") and not fake.downloads()
    await without.stop()
    # a team host passes the working directory in the provider config instead
    from_config = make(fake, monkeypatch, cwd=str(tmp_path))
    await from_config.start(context(host_session_id="host-1"))
    assert [f.event.data["name"] for f in items(await run_turn(from_config), "file")] == ["季度 报告.csv"]
    await from_config.stop()


async def test_pushed_file_over_the_cap_is_dropped(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    fake = FileDA()
    fake.files[CSV_PATH] = CSV
    fake.run_stream = turn(*push_file("季度 报告.csv", CSV_URL))
    harness = make(fake, monkeypatch, max_file_bytes=4)
    await harness.start(context(cwd=str(tmp_path)))
    assert not items(await run_turn(harness), "file")
    assert list(tmp_path.iterdir()) == [], "no partial download left behind"
    await harness.stop()


async def test_pushed_file_name_keeps_only_its_last_segment(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    work = tmp_path / "work"
    work.mkdir()
    fake = FileDA()
    fake.files[CSV_PATH] = CSV
    fake.run_stream = turn(*push_file("../../evil.csv", CSV_URL))
    harness = make(fake, monkeypatch)
    await harness.start(context(cwd=str(work)))
    files = items(await run_turn(harness), "file")
    assert files[0].event.data["path"] == str(work / "evil.csv")
    assert sorted(p.name for p in tmp_path.iterdir()) == ["work"]
    await harness.stop()
