"""Jiuwen 引擎的桥接与编排测试：chunk → 平台事件、确认卡映射、历史回放、引擎路由。

不触碰真实模型：轮驱动用假 Runner 流，桥接用手工 chunk。
"""

import asyncio
import json
import secrets
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services.engines.base import EngineContext
from app.services.engines.jiuwen import (
    JiuwenEngine,
    _call_usage_rail,
    _ChunkBridge,
    _fill_interactive_input,
    _history_to_messages,
    _interaction_to_permission,
)


def chunk(ctype, payload):
    return SimpleNamespace(type=ctype, payload=payload)


def make_ctx(tmp_path, permission=None, history=None, tokens=None):
    """tokens：可选的续接点记录 {"value": 当前 token, "ops": 存取操作序列}。"""

    async def default_permission(tool_name, tool_input, title, description):
        return {"behavior": "allow", "updated_input": None}

    tokens = tokens if tokens is not None else {"value": None, "ops": []}

    def save(token):
        tokens["value"] = token
        tokens["ops"].append(("save", token))

    def clear():
        tokens["value"] = None
        tokens["ops"].append(("clear", None))

    return EngineContext(
        project_id="p1",
        cwd=tmp_path,
        request_permission=permission or default_permission,
        load_resume_token=lambda: tokens["value"],
        save_resume_token=save,
        clear_resume_token=clear,
        log_error=lambda kind, text: None,
        load_history=(lambda: history or []),
    )


# ---- ChunkBridge ----


def test_bridge_stream_and_flush_with_tool_call():
    bridge = _ChunkBridge()
    events, _ = bridge.feed(chunk("llm_output", {"content": "我先看"}))
    assert events == [{"type": "stream_delta", "kind": "text", "text": "我先看"}]
    events, _ = bridge.feed(chunk("llm_reasoning", {"content": "想一想"}))
    assert events[0]["kind"] == "thinking"
    # tool_call：冲刷累计文本 + tool_use 块进同一条 assistant_message
    events, _ = bridge.feed(
        chunk("tool_call", {"tool_name": "write_file", "tool_args": {"file_path": "a.md"}})
    )
    assert len(events) == 1
    blocks = events[0]["blocks"]
    assert [b["kind"] for b in blocks] == ["thinking", "text", "tool_use"]
    assert blocks[2]["tool"] == "Write"  # jiuwen 名映射为前端已知语义名
    call_id = blocks[2]["tool_use_id"]
    # tool_result 按 FIFO 配对同一个自造 id
    events, _ = bridge.feed(chunk("tool_result", {"tool_result": "x" * 500}))
    block = events[0]["blocks"][0]
    assert block["tool_use_id"] == call_id
    assert block["excerpt"].endswith("…") and len(block["excerpt"]) == 401
    # 尾部纯文本由 finish 冲刷
    bridge.feed(chunk("llm_output", {"content": "完成"}))
    tail = bridge.finish()
    assert tail[0]["blocks"] == [{"kind": "text", "text": "完成"}]


def test_bridge_collects_interaction_and_ignores_noise():
    bridge = _ChunkBridge()
    assert bridge.feed(chunk("answer", {"content": "dup"})) == ([], None)
    assert bridge.feed(chunk("context.usage", {"pct": 1})) == ([], None)
    events, pending = bridge.feed(
        chunk(
            "__interaction__",
            SimpleNamespace(id="i1", value=SimpleNamespace(tool_name="ask_user")),
        )
    )
    iid, request = pending
    assert iid == "i1"
    assert getattr(request, "tool_name") == "ask_user"


def test_bridge_context_usage_rides_on_next_assistant_message():
    bridge = _ChunkBridge()
    bridge.feed(chunk("llm_output", {"content": "先看"}))
    # context.usage 先到（服务端未报时为本地估算），llm_usage 随后给出实报的输入与输出
    assert bridge.feed(
        chunk("context.usage", {"context_window": {"local_estimated_input_tokens": 1200}})
    ) == ([], None)
    assert bridge.feed(
        chunk("llm_usage", {"usage_metadata": {"input_tokens": 1500, "output_tokens": 80}})
    ) == ([], None)
    events, _ = bridge.feed(chunk("tool_call", {"tool_name": "read_file", "tool_args": {}}))
    assert events[0]["context_tokens"] == 1580
    # 用后即清：下一条不再带旧值
    bridge.feed(chunk("llm_output", {"content": "完成"}))
    assert "context_tokens" not in bridge.finish()[0]

    bridge.feed(chunk("context.usage", {"context_window": {"local_estimated_input_tokens": 900}}))
    bridge.feed(chunk("llm_output", {"content": "只有估算"}))
    assert bridge.finish()[0]["context_tokens"] == 900


def test_bridge_compression_state_maps_to_context_compacted():
    bridge = _ChunkBridge()
    state = {
        "before": {"tokens": 90000},
        "after": {"tokens": 12000},
        "compact_summary": "<memory_block_dialogue>早先对话的摘要</memory_block_dialogue>",
    }
    started = {**state, "status": "started", "processor": "DialogueCompressor"}
    assert bridge.feed(chunk("context.compression_state", started)) == ([], None)

    bridge.feed(chunk("llm_usage", {"usage_metadata": {"input_tokens": 95000}}))
    completed = {**state, "status": "completed", "processor": "DialogueCompressor"}
    events, _ = bridge.feed(chunk("context.compression_state", completed))
    assert events == [
        {
            "type": "context_compacted",
            "trigger": "auto",
            "pre_tokens": 90000,
            "post_tokens": 12000,
            "summary": state["compact_summary"],
        }
    ]
    # 压缩前的用量已过时，不再带到下一条消息上
    bridge.feed(chunk("llm_output", {"content": "接着做"}))
    assert "context_tokens" not in bridge.finish()[0]

    # 当轮压缩的摘要不覆盖此前对话，不随事件落盘
    current_round = {**state, "status": "completed", "processor": "CurrentRoundCompressor"}
    events, _ = bridge.feed(chunk("context.compression_state", current_round))
    assert events[0]["type"] == "context_compacted" and "summary" not in events[0]
    # 压缩器以外的处理器不算压缩
    offloader = {**state, "status": "completed", "processor": "MessageSummaryOffloader"}
    assert bridge.feed(chunk("context.compression_state", offloader)) == ([], None)


# ---- interaction ↔ 确认卡映射 ----


def test_ask_user_maps_to_question_card_schema():
    request = SimpleNamespace(
        tool_name="ask_user",
        questions=[
            {"question": "建模目标？", "header": "目标", "options": [{"label": "A"}, {"label": "B"}]}
        ],
    )
    tool_name, tool_input = _interaction_to_permission(request)
    assert tool_name == "AskUserQuestion"  # 前端"请专家确认"卡的识别名
    assert tool_input["questions"][0]["options"][0]["label"] == "A"

    class FakeII:
        def __init__(self):
            self.filled = {}

        def update(self, iid, value):
            self.filled[iid] = value

    ii = FakeII()
    _fill_interactive_input(
        ii, "i1", request,
        {"behavior": "allow", "updated_input": {"answers": {"建模目标？": "B"}}},
    )
    assert ii.filled["i1"] == {"answers": {"建模目标？": "B"}}


def test_approval_interaction_maps_to_permission_card():
    request = SimpleNamespace(
        tool_name="write_file", tool_args='{"file_path": "ontology/a.md"}', questions=None
    )
    tool_name, tool_input = _interaction_to_permission(request)
    assert tool_name == "write_file"
    assert tool_input == {"file_path": "ontology/a.md"}

    class FakeII:
        def __init__(self):
            self.filled = {}

        def update(self, iid, value):
            self.filled[iid] = value

    ii = FakeII()
    _fill_interactive_input(ii, "i2", request, {"behavior": "deny", "message": "不允许"})
    assert ii.filled["i2"]["approved"] is False
    assert ii.filled["i2"]["feedback"] == "不允许"


# ---- 历史回放 ----


def test_history_to_messages_roles_and_budget():
    events = [
        {"type": "user_message", "text": "第一问"},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "第一答"}]},
        {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Read"}]},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "补充"}]},
        {"type": "session_state", "state": "idle"},
        {"type": "user_message", "text": "第二问"},
    ]
    messages = _history_to_messages(events)
    roles = [type(m).__name__ for m in messages]
    # 尾部未获回复的用户消息（即将作为 prompt 重发）不注水
    assert roles == ["UserMessage", "AssistantMessage"]
    assert "第一答" in messages[1].content and "补充" in messages[1].content  # 相邻合并

    huge = [{"type": "user_message", "text": "长" * 30_000},
            {"type": "assistant_message", "blocks": [{"kind": "text", "text": "答"}]},
            {"type": "user_message", "text": "近"},
            {"type": "assistant_message", "blocks": [{"kind": "text", "text": "近答"}]}]
    trimmed = _history_to_messages(huge)
    assert [m.content for m in trimmed] == ["答", "近", "近答"]  # 超预算的长消息被丢弃


def test_history_to_messages_resumes_from_latest_summary():
    events = [
        {"type": "user_message", "text": "第一问"},
        {"type": "context_compacted", "trigger": "auto", "summary": "更早的摘要"},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "第一答"}]},
        {"type": "user_message", "text": "第二问"},
        {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Read"}]},
        {"type": "context_compacted", "trigger": "auto", "summary": "最近的摘要"},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "第二答"}]},
        {"type": "user_message", "text": "第三问"},
        # 当轮压缩不带摘要，不改变续接起点
        {"type": "context_compacted", "trigger": "auto", "pre_tokens": 9, "post_tokens": 3},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "第三答"}]},
    ]
    messages = _history_to_messages(events)
    # 最近的摘要在前，其后从压缩所在那一轮的用户消息起回放
    assert [m.content for m in messages] == ["最近的摘要", "第二问", "第二答", "第三问", "第三答"]
    assert type(messages[0]).__name__ == "UserMessage"

    # 预算紧张时摘要总是保留，其余从最近往前收
    tight = _history_to_messages(events, char_budget=len("最近的摘要") + len("第三答"))
    assert [m.content for m in tight] == ["最近的摘要", "第三答"]


# ---- 轮驱动（假 Runner 流）----


def test_run_turn_with_interaction_and_result(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    asked = {}

    async def permission(tool_name, tool_input, title, description):
        asked["tool_name"] = tool_name
        asked["questions"] = tool_input.get("questions")
        return {
            "behavior": "allow",
            "updated_input": {"answers": {"目标？": "全域建模"}},
        }

    rounds = []

    def fake_stream(agent, inputs, session=None):
        rounds.append(inputs["query"])

        async def first():
            yield chunk("llm_output", {"content": "先确认"})
            yield chunk(
                "__interaction__",
                SimpleNamespace(
                    id="i1",
                    value=SimpleNamespace(
                        tool_name="ask_user",
                        questions=[{"question": "目标？", "options": [{"label": "全域建模"}]}],
                    ),
                ),
            )

        async def second():
            yield chunk("llm_output", {"content": "已按确认目标完成"})

        return first() if len(rounds) == 1 else second()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))

    tokens = {"value": "jiuwen:onto-p1", "ops": []}
    engine = JiuwenEngine(make_ctx(tmp_path, permission=permission, tokens=tokens))
    engine.client = object()  # 假 agent（本测试不触 create_deep_agent）
    engine._tracker = SimpleNamespace(total_input_tokens=100, total_output_tokens=40)

    async def scenario():
        await engine._run_turn("开始")
        events = []
        while not engine._queue.empty():
            events.append(engine._queue.get_nowait())
        return events

    events = asyncio.run(scenario())
    types = [e["type"] for e in events]
    # 第一轮流式 + 冲刷，回填后第二轮，最终 result
    assert asked["tool_name"] == "AskUserQuestion"
    assert asked["questions"][0]["question"] == "目标？"
    assert len(rounds) == 2  # 第二轮的 query 是 InteractiveInput 回填
    assert types[-1] == "result"
    assert events[-1]["subtype"] == "success"
    assert "已按确认目标完成" in json.dumps(events, ensure_ascii=False)
    # 等确认期间存档带挂起中断，先清续接点；整轮收尾后再写回
    assert tokens["ops"] == [("clear", None), ("save", "jiuwen:onto-p1")]


def test_run_turn_reports_context_usage_before_result(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    def fake_stream(agent, inputs, session=None):
        async def gen():
            yield chunk(
                "context.compression_state",
                {
                    "status": "completed",
                    "processor": "DialogueCompressor",
                    "before": {"tokens": 150000},
                    "after": {"tokens": 20000},
                    "compact_summary": "摘要",
                },
            )
            yield chunk("llm_output", {"content": "答"})
            yield chunk("llm_usage", {"usage_metadata": {"input_tokens": 21000, "output_tokens": 500}})

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()
    engine._context_window, engine._compact_at = 200000, 160000

    async def scenario():
        await engine._run_turn("开始")
        events = []
        while not engine._queue.empty():
            events.append(engine._queue.get_nowait())
        return events

    events = asyncio.run(scenario())
    types = [e["type"] for e in events]
    assert types.index("context_compacted") < types.index("assistant_message")
    # 与稳定引擎同一事件：本轮结束（result 之前）报一次用量
    assert types[-2:] == ["context_usage", "result"]
    assert events[-2] == {
        "type": "context_usage",
        "used": 21500,
        "window": 200000,
        "compact_at": 160000,
    }


def test_run_turn_error_emits_engine_end(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    def broken_stream(agent, inputs, session=None):
        async def gen():
            yield chunk("llm_output", {"content": "刚开始"})
            raise RuntimeError("401 Unauthorized")

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(broken_stream))
    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()

    async def scenario():
        await engine._run_turn("开始")
        events = []
        while not engine._queue.empty():
            events.append(engine._queue.get_nowait())
        return events

    events = asyncio.run(scenario())
    assert events[-1]["type"] == "_engine_end"
    assert events[-1]["reason"] == "error"
    assert "模型网关未配置或凭据无效" in events[-1]["error"]
    # 结束引擎前先以出错终态收轮，统计口径与正常收尾一致
    assert events[-2]["type"] == "result"
    assert events[-2]["subtype"] == "error_during_execution"
    assert events[-2]["is_error"] is True
    assert events[-2]["usage_per_call"] is True


# ---- 逐次用量 ----


def model_call(input_tokens, output_tokens):
    """after_model_call 的回调上下文：响应带 usage_metadata（框架流式汇总后的形状）。"""
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens)
    return SimpleNamespace(inputs=SimpleNamespace(response=SimpleNamespace(usage_metadata=usage)))


def test_call_usage_rail_reports_each_model_call():
    calls = []
    rail = _call_usage_rail(lambda input_tokens, output_tokens: calls.append((input_tokens, output_tokens)))

    async def scenario():
        await rail.after_model_call(model_call(1200, 80))
        await rail.after_model_call(model_call(1500, 40))
        # 响应没带用量：照常计调用次数，不回报
        await rail.after_model_call(SimpleNamespace(inputs=SimpleNamespace(response=None)))

    asyncio.run(scenario())
    assert calls == [(1200, 80), (1500, 40)]
    assert (rail.total_input_tokens, rail.total_output_tokens) == (2700, 120)
    assert rail.call_count == 3


def test_interrupted_turn_reports_usage_recorded_so_far(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = object()
    engine._tracker = _call_usage_rail(engine._record_call_usage)
    engine._tracker.total_input_tokens = 5000  # 上一轮的累计，不算进这一轮
    called = asyncio.Event()

    def fake_stream(agent, inputs, session=None):
        async def gen():
            await engine._tracker.after_model_call(model_call(1000, 50))
            yield chunk("llm_output", {"content": "先看资料"})
            await engine._tracker.after_model_call(model_call(1200, 30))
            called.set()
            await asyncio.Event().wait()

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))

    async def scenario():
        running = asyncio.create_task(engine._run_turn("开始"))
        await asyncio.wait_for(called.wait(), timeout=1)
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        events = []
        while not engine._queue.empty():
            events.append(engine._queue.get_nowait())
        return events

    events = asyncio.run(scenario())
    # 每次调用返回就下发一条，进程中途退出也已落盘
    assert [e["usage"] for e in events if e["type"] == "model_usage"] == [
        {"input_tokens": 1000, "output_tokens": 50},
        {"input_tokens": 1200, "output_tokens": 30},
    ]
    result = events[-1]
    assert result["type"] == "result" and result["subtype"] == "interrupted"
    assert result["usage"] == {"input_tokens": 2200, "output_tokens": 80}
    assert result["usage_per_call"] is True


# ---- 打断与断开 ----


def test_interrupt_returns_when_abort_cancels_turn(tmp_path):
    # abort 会取消在飞的流，轮任务以 CancelledError 收尾；不能把它抛给接口层，否则打断请求返回 500
    engine = JiuwenEngine(make_ctx(tmp_path))

    async def scenario():
        turn = asyncio.create_task(asyncio.Event().wait())

        async def abort():
            turn.cancel()

        engine._turn_task = turn
        engine.client = SimpleNamespace(abort=abort)
        await engine.interrupt()
        return turn

    assert asyncio.run(scenario()).cancelled()


def test_disconnect_awaits_async_task_resource_cleanup(tmp_path):
    cleaned = []

    async def cleanup_task_resources():
        cleaned.append(True)

    engine = JiuwenEngine(make_ctx(tmp_path))
    engine.client = SimpleNamespace(cleanup_task_resources=cleanup_task_resources)
    asyncio.run(engine.disconnect())
    assert cleaned == [True]
    assert engine.client is None


# ---- 续接：checkpoint 优先，历史回放兜底 ----


class FakeCheckpointer:
    def __init__(self, sessions=()):
        self.sessions = set(sessions)
        self.released = []

    async def session_exists(self, session_id):
        return session_id in self.sessions

    async def release(self, session_id):
        self.released.append(session_id)
        self.sessions.discard(session_id)


class FakeAgent:
    def __init__(self):
        self.injected = []

    async def create_new_context_engine(self, session_id=None, messages=None):
        self.injected.append((session_id, messages))
        return session_id


# 测试密钥运行时生成，代码里不写凭据样式的字面量
FAKE_API_KEY = secrets.token_hex(8)

HISTORY = [
    {"type": "user_message", "text": "读一下资料"},
    {"type": "assistant_message", "blocks": [{"kind": "tool_use", "tool": "Read"}]},
    {"type": "assistant_message", "blocks": [{"kind": "text", "text": "资料里有三张表"}]},
]


def connect_with(tmp_path, monkeypatch, token, checkpointer, history=None):
    from openjiuwen.core.session.checkpointer import CheckpointerFactory

    from app.services.engines import jiuwen

    async def no_runner(_log_error):
        return None

    agent = FakeAgent()
    monkeypatch.setattr(
        jiuwen, "model_config",
        lambda owner="": {
            "base_url": "https://gateway.example.com/v1",
            "api_key": FAKE_API_KEY,
            "model": "m",
            "context": {"max_context_tokens": 128000, "max_output_tokens": 16384, "trigger_ratio": 0.77},
        },
    )
    monkeypatch.setattr(jiuwen, "_ensure_runner_started", no_runner)
    monkeypatch.setattr(JiuwenEngine, "_build_agent", lambda self, cfg: (agent, None))
    monkeypatch.setattr(CheckpointerFactory, "get_checkpointer", lambda *a, **k: checkpointer)
    tokens = {"value": token, "ops": []}
    history = HISTORY if history is None else history
    engine = JiuwenEngine(make_ctx(tmp_path, history=history, tokens=tokens))
    resumed = asyncio.run(engine.connect())
    return engine, agent, resumed


def test_connect_resumes_from_checkpoint_without_replay(tmp_path, monkeypatch):
    checkpointer = FakeCheckpointer(sessions={"onto-p1"})
    engine, agent, resumed = connect_with(
        tmp_path, monkeypatch, "jiuwen:onto-p1", checkpointer
    )
    # 存档带完整上下文（含工具调用），不再注水只有问答文本的历史回放
    assert resumed is True and engine.resume_attempted is True
    assert not agent.injected
    assert not checkpointer.released
    # 续接后同样按配置报窗口与压缩触发点
    assert engine.context_limits() == (128000, 98560)


def test_connect_replays_from_summary_when_checkpoint_missing(tmp_path, monkeypatch):
    history = [
        *HISTORY,
        {"type": "user_message", "text": "接着建模"},
        {"type": "context_compacted", "trigger": "auto", "summary": "压缩摘要"},
        {"type": "assistant_message", "blocks": [{"kind": "text", "text": "已建好对象"}]},
    ]
    _, agent, resumed = connect_with(
        tmp_path, monkeypatch, "jiuwen:onto-p1", FakeCheckpointer(), history=history
    )
    # 没有存档时退回回放：摘要代替压缩前的对话
    assert resumed is True
    _, messages = agent.injected[0]
    assert [m.content for m in messages] == ["压缩摘要", "接着建模", "已建好对象"]


def test_connect_falls_back_to_replay_when_checkpoint_missing(tmp_path, monkeypatch):
    checkpointer = FakeCheckpointer()
    _, agent, resumed = connect_with(tmp_path, monkeypatch, "jiuwen:onto-p1", checkpointer)
    assert resumed is True
    assert checkpointer.released == ["onto-p1"]
    session_id, messages = agent.injected[0]
    assert session_id == "onto-p1"
    assert [m.content for m in messages] == ["读一下资料", "资料里有三张表"]


def test_connect_without_token_discards_stale_checkpoint(tmp_path, monkeypatch):
    # 显式关闭会话（token 已清）或停在确认卡上：残留存档必须清掉，
    # 否则 Runner 首轮仍会从它恢复
    checkpointer = FakeCheckpointer(sessions={"onto-p1"})
    _, agent, resumed = connect_with(tmp_path, monkeypatch, None, checkpointer)
    assert checkpointer.released == ["onto-p1"]
    assert "onto-p1" not in checkpointer.sessions
    assert resumed is True and len(agent.injected) == 1

    checkpointer = FakeCheckpointer()
    _, agent, resumed = connect_with(tmp_path, monkeypatch, None, checkpointer, history=[])
    assert resumed is False and not agent.injected


def test_runner_starts_with_persistent_checkpointer(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.runner_config import get_runner_config, set_runner_config

    from app.services.engines import jiuwen

    seen = {}

    async def fake_start():
        seen["checkpointer"] = get_runner_config().checkpointer_config
        seen["distributed_mode"] = get_runner_config().distributed_mode
        return True

    original = get_runner_config()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(jiuwen, "_RUNNER_STARTED", False)
    monkeypatch.setattr(jiuwen, "_RUNNER_LOCK", None)
    monkeypatch.setattr(Runner, "start", fake_start)
    try:
        asyncio.run(jiuwen._ensure_runner_started(lambda kind, text: None))
    finally:
        set_runner_config(original)
    checkpointer = seen["checkpointer"]
    assert checkpointer.type == "persistence"
    assert checkpointer.conf["db_type"] == "sqlite"
    assert Path(checkpointer.conf["db_path"]) == tmp_path / "jiuwen" / "checkpoints.db"
    # 只替换 checkpointer，Runner 其余配置保持框架默认
    assert seen["distributed_mode"] == original.distributed_mode


def test_runner_falls_back_to_memory_checkpointer(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner
    from openjiuwen.core.runner.runner_config import get_runner_config, set_runner_config

    from app.services.engines import jiuwen

    attempts = []

    async def flaky_start():
        checkpointer = get_runner_config().checkpointer_config
        attempts.append(checkpointer and checkpointer.type)
        if checkpointer is not None:
            raise ModuleNotFoundError("No module named 'aiosqlite'")
        return True

    errors = []
    original = get_runner_config()
    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(jiuwen, "_RUNNER_STARTED", False)
    monkeypatch.setattr(jiuwen, "_RUNNER_LOCK", None)
    monkeypatch.setattr(Runner, "start", flaky_start)
    try:
        asyncio.run(jiuwen._ensure_runner_started(lambda kind, text: errors.append(kind)))
    finally:
        set_runner_config(original)
    # 持久化装配失败不阻断会话：留档后以内存存档启动
    assert attempts == ["persistence", None]
    assert errors == ["checkpoint"]
    assert jiuwen._RUNNER_STARTED is True


# ---- 引擎路由与模型配置 ----


def test_engine_routing_by_project_meta(tmp_path, monkeypatch):
    from app.services.agent_sessions import SessionManager
    from app.services.engines.claude import ClaudeEngine

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    manager = SessionManager()
    assert manager._engine_class_for("nope") is ClaudeEngine  # 无 meta 回落默认

    meta_dir = tmp_path / "workspaces" / "p9"
    meta_dir.mkdir(parents=True)
    (meta_dir / ".onto-web.json").write_text(
        json.dumps({"engine": "jiuwen"}), encoding="utf-8"
    )
    assert manager._engine_class_for("p9") is JiuwenEngine
    (meta_dir / ".onto-web.json").write_text(
        json.dumps({"engine": "claude"}), encoding="utf-8"
    )
    assert manager._engine_class_for("p9") is ClaudeEngine


def test_jiuwen_model_config_ui_overrides_env(tmp_path, monkeypatch):
    from app.services import model_settings
    from app.services.engines import jiuwen

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JIUWEN_BASE_URL", "https://env.example.invalid/v1")
    monkeypatch.setattr(config, "JIUWEN_MODEL", "env-model")
    cfg = jiuwen.model_config()
    assert cfg["base_url"] == "https://env.example.invalid/v1"

    model_settings.save_jiuwen("https://ui.example.invalid/v1", "test-key-000000", "ui-model")
    cfg = jiuwen.model_config()
    assert cfg["base_url"] == "https://ui.example.invalid/v1"
    assert cfg["model"] == "ui-model"
    masked = model_settings.masked()
    assert masked["jiuwen"]["configured"] is True
    assert "test-key" not in json.dumps(masked)  # 读取接口只出掩码


def test_jiuwen_model_config_carries_context_into_fingerprint(tmp_path, monkeypatch):
    from app.services import model_settings
    from app.services.engines import jiuwen

    monkeypatch.setattr(config, "DATA_DIR", tmp_path)
    monkeypatch.setattr(config, "JIUWEN_MAX_TOKENS", 8192)
    cfg = jiuwen.model_config()
    assert cfg["context"] == {
        "max_context_tokens": 200000,
        "max_output_tokens": 8192,
        "trigger_ratio": 0.8,
    }

    before = jiuwen._fingerprint(cfg)
    model_settings.save_jiuwen(
        "https://ui.example.invalid/v1",
        "",
        "ui-model",
        {"max_context_tokens": 128000, "max_output_tokens": 16384},
    )
    cfg = jiuwen.model_config()
    assert cfg["context"]["trigger_ratio"] == 0.77
    # 改了上下文参数，空闲会话按新配置重建
    assert jiuwen._fingerprint(cfg) != before
