"""Jiuwen 引擎的桥接与编排测试：chunk → 平台事件、确认卡映射、历史回放、引擎路由。

不触碰真实模型：轮驱动用假 Runner 流，桥接用手工 chunk。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config
from app.services.engines.base import EngineContext
from app.services.engines.jiuwen import (
    JiuwenEngine,
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
        lambda: {"base_url": "https://gateway.example.com/v1", "api_key": "k", "model": "m"},
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
