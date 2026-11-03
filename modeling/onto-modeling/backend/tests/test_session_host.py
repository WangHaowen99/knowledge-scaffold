"""会话宿主（详设 4.2、8.4；方案 B4～B7；契约 3.11、第 4.2 节）：

- 对话流各类事件的载荷形状，按契约第 4.2 节逐类参数化核对；
- 事件 id 从 1 开始，`after` 回放与重连、瞬时事件不回放；
- 执行记录的生命周期：开始、阶段、等待、完成、打断、续跑、放弃；
- 两引擎注入一致（Claude 经 UserPromptSubmit 回调、Jiuwen 拼进 query，内容同一份）。

假引擎按协议产出平台事件，不起真实 CLI。测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

import pytest

from app import config
from app.api import deps
from app.services import agent_sessions, streams
from app.services.agent_sessions import SessionManager
from app.services.engines.base import EngineContext
from app.services.session_host import exec_records, injection, timeline
from app.services.workspace import WorkspaceManager

# ---- 契约第 4.2 节：事件类型 → 载荷字段（逐类定义） ----

CONTRACT = {
    "session.state": {"state", "since", "turn_started_at", "reason"},
    "session.queued": {"position", "ahead", "pool", "progress_pct"},
    "session.started": {"engine_label", "resumed", "context_kept"},
    "session.error": {"error_id", "message", "retries", "retryable", "actions"},
    "session.interrupted": {"by", "at", "exec_id", "position_label", "saved"},
    "session.config_pending": {"changes", "effective", "detail_link"},
    "session.reconnected": {"reason", "changes", "context_kept", "duration_ms"},
    # F3 补充：message.user 带命令名（contract-changes F3 第 1 条）
    "message.user": {"message_id", "text", "attachments", "references", "origin", "delivery", "command"},
    "message.delta": {"message_id", "kind", "text"},
    "message.assistant": {"message_id", "text_md", "anchors", "entry_links", "continued"},
    "notice": {"kind", "text", "version_label", "changeset_id", "align"},
    "day.marker": {"label"},
    "progress": {"group_id", "title", "desc", "state", "started_at", "duration_ms", "collapsed", "steps"},
    "progress.updated": {"group_id", "title", "desc", "state", "started_at", "duration_ms", "collapsed", "steps"},
    "subagent.updated": {"task_id", "group_id", "title", "status", "progress_text", "summary", "step"},
    "tool.activity": {"tool_use_id", "visibility", "tool", "summary", "state"},
    "permission.requested": {"request_id", "title", "description", "details_text", "timeout_s", "expires_at"},
    "permission.resolved": {"request_id", "behavior", "by", "message"},
    "question.answered": {"question_id", "answer", "by", "summary", "answered_at"},
    "question.withdrawn": {"question_id", "reason"},
    "turn.completed": {"turn_id", "duration_ms", "is_error", "usage", "cost"},
    "context.usage": {"used", "window", "pct", "compact_at_pct", "breakdown", "last_call"},
    "context.compacted": {"trigger", "pre_tokens", "post_tokens", "freed_tokens", "kept", "at"},
    "suggestions.updated": {"mode", "primary", "secondary", "all_commands"},
}
# 4.6 Question 的共有字段；4.7 卡片的共有字段与两种宿主出的卡
QUESTION = {"question_id", "kind", "conversation_id", "exec_id", "step_label", "eyebrow", "title", "background_md",
            "anchors", "options", "multi", "allow_custom", "custom_placeholder", "state", "answer", "inbox_item_id",
            "affected"}
OPTION = {"id", "key", "label", "consequence", "recommended", "preview", "custom"}
CARD = {"card_id", "card_type", "eyebrow", "title", "subtitle", "status", "plugin", "open"}
CARD_TYPES = {
    "round_changes": {"changeset_id", "title", "checks", "groups", "next_version_label", "state",
                      "merged_version_label", "remaining_failures"},
    "resume": {"exec_id", "title", "reason", "position", "saved", "resumable", "not_resumable_reason"},
}
STEP = {"id", "text", "meta", "state", "duration_ms"}
EXEC = {"id", "conversation_id", "engine_instance", "node", "slice", "run_id", "command_label", "phase", "state",
        "input_fingerprint", "started_at", "heartbeat_at", "ended_at", "interrupted", "saved", "resumable",
        "not_resumable_reason"}
PERSISTENT = set(CONTRACT) - {"message.delta", "context.usage"}


def check_shape(envelope: dict) -> None:
    kind = envelope["type"]
    data = envelope["data"]
    assert set(envelope) - {"id"} == {"type", "ts", "project_id", "conversation_id", "data"}
    assert ("id" in envelope) == (kind not in ("message.delta", "context.usage"))
    if kind == "question.asked":
        assert set(data) == QUESTION
        assert all(set(option) == OPTION for option in data["options"])
        return
    if kind in ("card", "card.updated"):
        assert CARD <= set(data)
        assert CARD_TYPES[data["card_type"]] <= set(data)
        return
    assert set(data) == CONTRACT[kind], kind
    if kind in ("progress", "progress.updated"):
        assert all(set(step) == STEP for step in data["steps"])
    if kind == "turn.completed":
        assert set(data["usage"]) == {"input_tokens", "output_tokens", "cache_read_tokens"}
        assert set(data["cost"]) == {"amount", "currency"}


# ---- 假引擎 ----


class FakeEngine:
    """按协议产出平台事件；每次 send 取下一段脚本（事件或要先做的动作）。"""

    name = "fake"
    scripts: list = []
    sent: list = []

    @classmethod
    def available(cls) -> bool:
        return True

    def __init__(self, ctx: EngineContext):
        self.ctx = ctx
        self.client = object()
        self.resume_attempted = False
        self.queue: asyncio.Queue = asyncio.Queue()

    async def connect(self) -> bool:
        return False

    async def send(self, text: str, context: str | None = None) -> None:
        FakeEngine.sent.append(text)
        script = FakeEngine.scripts.pop(0) if FakeEngine.scripts else [{"type": "result", "subtype": "success"}]
        for item in script:
            await self.queue.put(item if callable(item) else dict(item))

    async def events(self):
        while True:
            event = await self.queue.get()
            if callable(event):  # 脚本里的动作（写、删命令锁）按顺序在两个事件之间做
                event(self.ctx)
                continue
            yield event
            if event.get("type") == "_engine_end":
                return

    async def interrupt(self) -> None:
        await self.queue.put({"type": "result", "subtype": "interrupted", "is_error": False, "duration_ms": 1})

    async def disconnect(self) -> None:
        self.client = None

    async def kill(self) -> None:
        self.client = None

    def model_fingerprint(self) -> str:
        return "fake"

    def config_stale(self) -> bool:
        return False

    def context_limits(self):
        return 200_000, 160_000

    def add_observer(self, observer) -> None:
        pass


def result(**extra) -> dict:
    return {"type": "result", "subtype": "success", "is_error": False, "duration_ms": 1200, "num_turns": 1,
            "total_cost_usd": 0.01, "usage": {"input_tokens": 100, "output_tokens": 20, "cache_read_input_tokens": 5},
            **extra}


def lock(command: str, phase: str):
    def write(ctx):
        path = Path(ctx.cwd) / ".ontology-state" / f"lock-{command}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"command": command, "phase": phase}), encoding="utf-8")
    return write


def release(command: str):
    def remove(ctx):
        (Path(ctx.cwd) / ".ontology-state" / f"lock-{command}.json").unlink(missing_ok=True)
    return remove


@pytest.fixture()
def host(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(config, "DATA_DIR", tmp_path / "data")
    workspaces = WorkspaceManager(tmp_path / "data", Path(config.ASSETS_DIR))
    monkeypatch.setattr(deps, "workspaces", workspaces)
    monkeypatch.setattr(config, "MAX_ACTIVE_SESSIONS", 4)
    FakeEngine.scripts = []
    FakeEngine.sent = []
    manager = SessionManager()
    monkeypatch.setattr(manager, "_engine_class_for", lambda project_id: FakeEngine)
    monkeypatch.setattr(agent_sessions, "manager", manager)
    streams.reset_all()
    project_id = workspaces.create_project("会话宿主", owner="employee_0001")["id"]
    root = workspaces.project_root(project_id)
    yield manager, project_id, root
    streams.reset_all()


async def settle(manager: SessionManager, project_id: str, *, until=None, rounds: int = 200):
    for _ in range(rounds):
        session = manager.get(project_id)
        if session is not None and session.connect_task is not None:
            await session.connect_task
        await asyncio.sleep(0.01)
        session = manager.get(project_id)
        if session is not None and (until(session) if until else session.state == "idle" and not session.sending):
            return session
    return manager.get(project_id)


def project_history(manager: SessionManager, project_id: str, conversation_id: str = "default") -> list[dict]:
    return [envelope for envelope in (timeline.project(event, project_id, conversation_id)
                                      for event in manager.get_history(project_id, conversation_id)) if envelope]


# ---- 一轮对话：载荷、执行记录 ----


def test_command_turns_produce_contract_payloads_and_exec_lifecycle(host):
    manager, project_id, root = host
    FakeEngine.scripts = [
        [lock("extract-object", "source"),
         {"type": "stream_delta", "kind": "text", "text": "先读资料"},
         {"type": "assistant_message", "blocks": [{"kind": "text", "text": "先读资料"},
                                                  {"kind": "tool_use", "tool": "Read", "tool_use_id": "tu-1",
                                                   "input": {"file_path": "research/资料分析.md"}}]},
         {"type": "tool_results", "blocks": [{"kind": "tool_result", "tool_use_id": "tu-1", "is_error": False}]},
         lock("extract-object", "objects"),
         {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Task", "tool_use_id": "tu-2",
                                                   "input": {"description": "语义检查"}}]},
         {"type": "subagent_started", "task_id": "task-1", "tool_use_id": "tu-2", "description": "语义检查"},
         {"type": "subagent_finished", "task_id": "task-1", "tool_use_id": "tu-2", "description": "语义检查",
          "status": "completed", "summary": "没有问题"},
         {"type": "tool_results", "blocks": [{"kind": "tool_result", "tool_use_id": "tu-2", "is_error": False}]},
         {"type": "context_usage", "used": 50_000, "window": 200_000, "compact_at": 160_000},
         {"type": "context_compacted", "trigger": "auto", "pre_tokens": 150_000, "post_tokens": 30_000},
         result()],
        [release("extract-object"), {"type": "assistant_message", "blocks": [{"kind": "text", "text": "做完了"}]},
         result()],
    ]

    async def scenario():
        await manager.send(project_id, root, "/ontology-extract-object 订单", username="employee_0002")
        session = await settle(manager, project_id, until=lambda s: s.exec_id and (
            exec_records.get(project_id, s.exec_id) or {}).get("state") == "waiting")
        first = exec_records.get(project_id, session.exec_id)
        await manager.send(project_id, root, "继续")
        session = await settle(manager, project_id, until=lambda s: s.state == "idle" and s.exec_id is None)
        await manager.shutdown()
        return first

    first = asyncio.run(scenario())
    # 第一轮结束命令锁还在：等待；阶段取自命令锁
    assert first["state"] == "waiting" and first["command_label"] == "对象提取"
    assert first["phase"]["label"] == "写入对象" and first["phase"]["index"] == 2
    record = exec_records.get(project_id, first["id"])
    assert record["state"] == "done" and record["ended_at"]
    view = exec_records.view(record, root=root)
    assert set(view) == EXEC and view["resumable"] is False

    envelopes = project_history(manager, project_id)
    ids = [int(item["id"]) for item in envelopes if "id" in item]
    assert ids == sorted(ids) and ids[0] >= 1
    for envelope in envelopes:
        check_shape(envelope)
    kinds = [item["type"] for item in envelopes]
    for expected in ("message.user", "message.assistant", "tool.activity", "progress", "progress.updated",
                     "subagent.updated", "turn.completed", "session.state", "session.started", "context.compacted",
                     "suggestions.updated"):
        assert expected in kinds, expected
    user = next(item for item in envelopes if item["type"] == "message.user")
    assert user["data"]["message_id"] == f"m-{user['id']}" and user["data"]["delivery"] == "immediate"
    assert user["data"]["command"] == "/ontology-extract-object"
    group = [item for item in envelopes if item["type"] == "progress.updated"][-1]["data"]
    assert group["title"] == "对象提取" and group["state"] == "done" and group["collapsed"] is True
    texts = [step["text"] for step in group["steps"]]
    assert "读取资料分析报告" in texts and any("写入对象" in text for text in texts)
    assert all("research/" not in text for text in texts)  # 只给业务名，不给路径、文件名与命令原文
    completed = [item for item in envelopes if item["type"] == "turn.completed"]
    assert completed[0]["data"]["usage"] == {"input_tokens": 100, "output_tokens": 20, "cache_read_tokens": 5}
    assert completed[0]["data"]["cost"] == {"amount": 0.01, "currency": "USD"}
    # 契约 1.11：context.usage 是瞬时事件，不回放
    assert "context.usage" not in [item["type"] for item in envelopes if "id" in item]
    # 宿主把执行记录推上项目流（EP-09），状态依次 running → waiting → running → done
    channel = streams.project_channel(project_id)

    async def buffered():
        subscriber, replay, _ = channel.open(f"{streams.EPOCH}.0", asyncio.get_running_loop())
        channel.unsubscribe(subscriber)
        return replay

    states = [item["data"]["state"] for item in asyncio.run(buffered()) if item["type"] == "exec.updated"]
    assert states[0] == "running" and "waiting" in states and states[-1] == "done"
    assert all(set(item["data"]) == EXEC for item in asyncio.run(buffered()) if item["type"] == "exec.updated")


def test_interrupt_marks_exec_and_offers_resume_then_resume_and_abandon(host):
    manager, project_id, root = host
    FakeEngine.scripts = [
        [lock("extract-operation", "source"),
         {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Bash", "tool_use_id": "tu-1",
                                                   "input": {"command": "python3 x.py", "description": "抽取操作"}}]}],
    ]
    # 上游产物（对象）先在
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)

    async def scenario():
        await manager.send(project_id, root, "/ontology-extract-operation")
        await settle(manager, project_id, until=lambda s: s.state == "running" and s.turn and s.turn.steps)
        await manager.interrupt(project_id)
        session = await settle(manager, project_id)
        interrupted = manager.interrupted_execs.get((project_id, "default"))
        await manager.shutdown()
        return interrupted

    interrupted = asyncio.run(scenario())
    assert interrupted["state"] == "interrupted" and interrupted["interrupted"]["reason"] == "user"
    envelopes = project_history(manager, project_id)
    marker = [item for item in envelopes if item["type"] == "session.interrupted"][-1]["data"]
    assert marker["by"] == "user" and marker["exec_id"] == interrupted["id"]
    assert "操作提取" in marker["position_label"] and set(marker["saved"]) == {"files", "version_label"}
    suggestions = [item for item in envelopes if item["type"] == "suggestions.updated"][-1]["data"]
    # 打断之后：主推「从打断处继续」（这一轮随后出的 result 不覆盖它）
    assert suggestions["mode"] == "interrupted"
    assert suggestions["primary"]["action"] == {"type": "resume_exec", "exec_id": interrupted["id"]}
    group = [item for item in envelopes if item["type"].startswith("progress")][-1]["data"]
    assert group["state"] == "interrupted"
    view = exec_records.view(exec_records.get(project_id, interrupted["id"]), root=root,
                             flow=__import__("app.services.flow.model", fromlist=["model"]).load_for(root))
    assert view["resumable"] is True

    # 别人改了输入（资料）之后不能续跑
    (root / "resource").mkdir(exist_ok=True)
    (root / "resource" / "新资料.txt").write_text("合成资料\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=root, check=True, capture_output=True)
    subprocess.run(["git", "commit", "-qm", "test: 补资料"], cwd=root, check=True, capture_output=True)
    from app.services.flow import model

    assert exec_records.not_resumable_reason(exec_records.get(project_id, interrupted["id"]), root,
                                             model.load_for(root)) == "inputs_changed"


def test_result_arriving_while_interrupting_does_not_finish_the_turn(host, monkeypatch):
    """引擎停下前先出 result（命令锁也已放掉）：工作过程仍记为打断，执行记录记为中断而不是完成。"""
    manager, project_id, root = host
    FakeEngine.scripts = [
        [lock("extract-operation", "source"),
         {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Bash", "tool_use_id": "tu-1",
                                                   "input": {"command": "python3 x.py", "description": "抽取操作"}}]},
         release("extract-operation")],
    ]
    (root / "ontology" / "object").mkdir(parents=True, exist_ok=True)

    async def slow_interrupt(self) -> None:
        await self.queue.put({"type": "result", "subtype": "interrupted", "is_error": False, "duration_ms": 1})
        for _ in range(50):  # 等读循环把这条 result 收完再返回
            await asyncio.sleep(0.01)

    monkeypatch.setattr(FakeEngine, "interrupt", slow_interrupt)

    async def scenario():
        await manager.send(project_id, root, "/ontology-extract-operation")
        await settle(manager, project_id, until=lambda s: s.state == "running" and s.turn and s.turn.steps)
        await manager.interrupt(project_id)
        session = await settle(manager, project_id)
        flag = session.interrupting
        interrupted = manager.interrupted_execs.get((project_id, "default"))
        await manager.shutdown()
        return interrupted, flag

    interrupted, flag = asyncio.run(scenario())
    assert interrupted is not None and interrupted["state"] == "interrupted" and flag is False
    envelopes = project_history(manager, project_id)
    groups = [item["data"] for item in envelopes if item["type"].startswith("progress")]
    assert groups[-1]["state"] == "interrupted" and all(group["state"] != "done" for group in groups)
    suggestions = [item for item in envelopes if item["type"] == "suggestions.updated"][-1]["data"]
    assert suggestions["mode"] == "interrupted"


def test_startup_sweep_interrupts_open_execs_with_resume_card(host):
    manager, project_id, root = host
    from app.services.flow import model
    from app.services.session_host import recovery

    record = exec_records.start(project_id, "default", root=root, flow=model.load_for(root),
                                command="/ontology-research", command_name="research", engine_instance="old")
    item = recovery.sweep_execs(deps.workspaces)
    assert item["id"] == "interrupted_execs" and item["result"] == "awaiting_user"
    swept = exec_records.get(project_id, record["id"])
    assert swept["state"] == "interrupted" and swept["interrupted"]["reason"] == "restart"
    cards = [item for item in project_history(manager, project_id) if item["type"] == "card"]
    assert cards and cards[-1]["data"]["card_type"] == "resume" and cards[-1]["data"]["exec_id"] == record["id"]
    check_shape(cards[-1])
    assert cards[-1]["data"]["resumable"] is True and "资料分析" in cards[-1]["data"]["title"]
    # 再跑一次什么都不做（幂等）
    assert recovery.sweep_execs(deps.workspaces)["result"] == "ok"


# ---- 单条记录的投影（宿主自己出的事件） ----


def _record(record_type: str, **fields) -> dict:
    return {"type": record_type, "seq": 4, "ts": 1_700_000_000.0, **fields}


RECORDS = {
    "session.queued": _record("queue_position", position=2, pool={"active": 4, "max": 4}),
    "session.error": _record("session_error", error="模型服务暂时不稳定", error_id="err-1a2b3c", retries=1,
                             retryable=True, actions=["retry", "redirect"]),
    "session.interrupted": _record("session_interrupted", reason="shutdown", exec_id="ex-1",
                                   position_label="你在「对象提取」进行中打断了", saved={"files": 2, "version_label": "v0.3"}),
    "session.config_pending": _record("session_config_pending", changes=[{"kind": "assembly", "summary": "建模能力有更新"}],
                                      effective="next_message", detail_link=None),
    "session.reconnected": _record("session_reconnected", reason="config_changed", changes=["assembly"],
                                   context_kept=True, duration_ms=120),
    "message.delta": _record("stream_delta", kind="text", text="部分", message_id="a-1"),
    "permission.requested": _record("permission_request", request_id="r1", tool_name="Bash",
                                    tool_input={"command": "ls"}, title=None, description=None, timeout_s=600,
                                    expires_at=1_700_000_600.0),
    "permission.resolved": _record("permission_resolved", request_id="r1", behavior="expired"),
    "question.answered": _record("question_answered", payload={"question_id": "q1", "answer": {"option_id": "0.0"},
                                                               "by": "user", "summary": "选了 A：按天", "answered_at": "x"}),
    "question.withdrawn": _record("question_withdrawn", payload={"question_id": "q1", "reason": "问题已失效"}),
    "notice": _record("notice", payload={"kind": "info", "text": "已放弃", "version_label": None,
                                         "changeset_id": None, "align": "center"}),
    "day.marker": _record("day_marker", payload={"label": "11月1日"}),
    "context.usage": _record("context_usage", used=10, window=100, compact_at=80),
}


@pytest.mark.parametrize("kind", sorted(RECORDS))
def test_host_record_projects_to_contract_payload(kind):
    envelope = timeline.project(RECORDS[kind], "p1", "c1")
    assert envelope["type"] == kind
    check_shape(envelope)
    if "id" in envelope:
        assert envelope["id"] == "5"  # 历史第 5 行（seq 4）


def test_legacy_records_project_without_missing_fields():
    legacy = [
        {"type": "user_message", "text": "旧消息", "seq": 0, "ts": 1.0},
        {"type": "session_interrupted", "reason": "stale", "message": "上一轮未正常结束", "seq": 1, "ts": 2.0},
        {"type": "session_state", "state": "idle", "seq": 2, "ts": 3.0},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "旧回答"}, {"kind": "tool_use", "tool": "Read"}],
         "seq": 3, "ts": 4.0},
        {"type": "workspace_changes", "added": ["ontology/object/订单.md"], "modified": ["reports/r.md"],
         "deleted": [], "from": "a", "to": "b", "seq": 4, "ts": 5.0},
        {"type": "model_usage", "usage": {"input_tokens": 1}, "seq": 5, "ts": 6.0},
    ]
    envelopes = [envelope for envelope in (timeline.project(event, "p1", "c1") for event in legacy) if envelope]
    assert [item["type"] for item in envelopes] == ["message.user", "session.interrupted", "session.state",
                                                     "message.assistant", "card"]
    for envelope in envelopes:
        check_shape(envelope)
    assert envelopes[0]["data"]["message_id"] == "m-1" and envelopes[1]["data"]["by"] == "restart"
    card = envelopes[-1]["data"]
    assert card["card_type"] == "round_changes" and card["title"] == "改了 2 处"
    assert card["groups"][0]["items"][0]["target"]["id"] == "object:订单"


def test_ask_user_question_becomes_decision_questions():
    questions = timeline.build_questions(
        "req1", {"questions": [{"question": "订单按什么口径统计？", "header": "统计口径",
                                "options": [{"label": "按天（推荐）", "description": "每日汇总"}, {"label": "按周"}]},
                               {"question": "要不要拆明细？", "options": [{"label": "要"}], "multiSelect": True}]},
        conversation_id="c1", exec_id="ex-1", step_label="写入对象")
    assert [item["question_id"] for item in questions] == ["req1-0", "req1-1"]
    first = questions[0]
    assert set(first) == QUESTION and first["kind"] == "decision" and first["state"] == "open"
    assert [option["key"] for option in first["options"]] == ["A", "B", "C"]
    assert first["options"][0]["recommended"] is True and first["options"][-1]["custom"] is True
    assert questions[1]["multi"] is True
    summary = timeline.answer_summary(first, {"option_id": "0.1"})
    assert summary == "选了 B：按周"


def test_s06_collapse_keeps_first_position_with_latest_content():
    envelopes = [
        {"id": "1", "type": "card", "data": {"card_id": "c", "title": "旧"}},
        {"id": "2", "type": "message.user", "data": {}},
        {"id": "3", "type": "card.updated", "data": {"card_id": "c", "title": "新"}},
        {"id": "4", "type": "question.asked", "data": {"question_id": "q", "state": "open"}},
        {"id": "5", "type": "question.answered", "data": {"question_id": "q", "answer": {"option_id": "0.0"}}},
    ]
    collapsed = timeline.collapse(envelopes)
    assert [item["id"] for item in collapsed] == ["1", "2", "4", "5"]
    assert collapsed[0]["data"]["title"] == "新" and collapsed[2]["data"]["state"] == "answered"


# ---- 对话流：id、回放、重连 ----


def test_conversation_stream_replays_after_id_and_skips_transient(host):
    manager, project_id, root = host
    from app.routers import sessions

    history = [
        {"type": "user_message", "text": "一"},
        {"type": "context_usage", "used": 1, "window": 10},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "二"}]},
        {"type": "session_state", "state": "idle"},
    ]
    for event in history:
        agent_sessions.append_history(project_id, "default", event)

    async def scenario(after):
        frames = sessions.conversation_frames(project_id, "default", after, heartbeat=0.05)
        chunks = [await asyncio.wait_for(frames.__anext__(), 2) for _ in range(4)]
        await frames.aclose()
        return chunks

    def parse(chunks):
        out = []
        for chunk in chunks[1:]:
            envelope = json.loads(chunk.split("data: ", 1)[1])
            out.append((envelope.get("id"), envelope["type"]))
        return out

    # 从头：id 从 1 开始，context.usage（瞬时）不回放，之后是心跳
    assert parse(asyncio.run(scenario(0))) == [("1", "message.user"), ("3", "message.assistant"),
                                               ("4", "session.state")]
    # after=3：只补发 id 大于 3 的
    assert parse(asyncio.run(scenario(3)))[:2] == [("4", "session.state"), (None, "ping")]


def test_s06_pages_by_external_ids(host):
    manager, project_id, root = host
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services import auth

    for index in range(5):
        agent_sessions.append_history(project_id, "default", {"type": "user_message", "text": f"第{index}条"})
    auth_enabled = auth.ENABLED
    auth.ENABLED = False
    try:
        client = TestClient(app)
        page = client.get(f"/api/v1/projects/{project_id}/conversations/default/messages?limit=2").json()
        assert [item["id"] for item in page["items"]] == ["4", "5"] and page["has_more"] is True
        assert page["first_seq"] == 4 and page["last_seq"] == 5
        earlier = client.get(f"/api/v1/projects/{project_id}/conversations/default/messages?before_seq=4").json()
        assert [item["id"] for item in earlier["items"]] == ["1", "2", "3"] and earlier["has_more"] is False
        state = client.get(f"/api/v1/projects/{project_id}/conversations/default").json()
        assert state["last_seq"] == 5 and state["active_exec"] is None
    finally:
        auth.ENABLED = auth_enabled


# ---- 两引擎注入一致 ----


def test_both_engines_inject_the_same_context(tmp_path, monkeypatch):
    from app.services.engines.claude import ClaudeEngine
    from app.services.engines.jiuwen import JiuwenEngine

    from tests.test_session_injection import knowledge_provider

    stage = {"text": "本体项目当前阶段：未开始。"}
    monkeypatch.setattr(injection, "stage_hint", lambda root: stage["text"])
    monkeypatch.setattr(injection, "next_step_hint", lambda root: "下一步建议：资料分析（/ontology-research）。")
    monkeypatch.setattr(injection, "_prompt_hint_provider", knowledge_provider)

    def ctx():
        return EngineContext(project_id="p1", cwd=tmp_path, request_permission=None, load_resume_token=lambda: None,
                             save_resume_token=lambda token: None, clear_resume_token=lambda: None,
                             log_error=lambda kind, text: None, injector=injection.Injector(tmp_path),
                             engine_instance="e1")

    claude = ClaudeEngine(ctx())
    jiuwen = JiuwenEngine(ctx())
    messages = ["开始建模", "好的", "其实我们公司按周统计", "继续"]

    async def scenario():
        out = []
        for index, message in enumerate(messages):
            if index == 3:
                stage["text"] = "本体项目当前阶段：对象已提取。"
            hook = await claude._on_prompt({"prompt": message})
            claude_text = (hook.get("hookSpecificOutput") or {}).get("additionalContext", "")
            query = await jiuwen._model_query(message)
            out.append((claude_text, query))
        return out

    results = asyncio.run(scenario())
    for message, (claude_text, query) in zip(messages, results):
        assert query == injection.compose_query(message, claude_text)
    assert results[0][0].startswith("本体项目当前阶段：未开始。") and "下一步建议" in results[0][0]
    assert results[1][0] == ""  # 内容没变不重复
    assert results[2][0].startswith("【业务知识信号】") and "当前阶段" not in results[2][0]
    assert "对象已提取" in results[3][0]
    # Claude 的回调随 SDK 选项注册（UserPromptSubmit），会话身份进 agent 的环境（方案 B7）
    options = claude._build_options(None)
    assert options.hooks["UserPromptSubmit"][0].hooks == [claude._on_prompt]
    assert options.env.get("ONTO_RUN_ENGINE") == "e1" and options.env.get("ONTO_RUN_PROJECT") == "p1"
