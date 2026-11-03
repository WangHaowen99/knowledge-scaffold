"""F 第一批第 7 条：模型流超时重试 2 次后，第 3 次请求挂了 32 分钟没有兜住（自主构建线，内测引擎）。

框架自己的帧超时没有管到那一段。引擎这边加一道兜底：一轮里超过 ONTO_WEB_JIUWEN_STALL_SECONDS 没有任何动静
（流事件、模型调用开始与返回、子任务进展都算），按出错收尾（出错终态 + 引擎结束），执行记为中断、走续跑。
等用户回答确认卡的时间不算。
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

from app import config
from app.services.engines.jiuwen import JiuwenEngine, _call_usage_rail
from tests.test_jiuwen_engine import chunk, make_ctx


def _drain(engine) -> list[dict]:
    events = []
    while not engine._queue.empty():
        events.append(engine._queue.get_nowait())
    return events


def test_stalled_model_request_ends_turn_as_error(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    def hanging_stream(agent, inputs, session=None):
        async def gen():
            yield chunk("llm_output", {"content": "开始"})
            await asyncio.sleep(3600)  # 第 3 次请求挂住，框架没有兜住
            yield chunk("llm_output", {"content": "不会到这里"})

        return gen()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(hanging_stream))
    monkeypatch.setattr(config, "JIUWEN_STALL_SECONDS", 0.3)
    errors = []
    ctx = make_ctx(tmp_path)
    ctx.log_error = lambda kind, text: errors.append((kind, text))
    engine = JiuwenEngine(ctx)
    aborted = []
    engine.client = SimpleNamespace(abort=lambda: aborted.append(True))

    async def scenario():
        await asyncio.wait_for(engine._run_turn("开始"), timeout=10)
        return _drain(engine)

    events = asyncio.run(scenario())
    assert aborted
    assert events[-2]["type"] == "result" and events[-2]["is_error"] is True
    assert events[-1]["type"] == "_engine_end" and events[-1]["reason"] == "error"
    assert errors and "没有响应" in errors[-1][1]


def test_waiting_for_user_answer_is_not_a_stall(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner

    rounds = []

    def fake_stream(agent, inputs, session=None):
        rounds.append(inputs)

        async def first():
            yield chunk("__interaction__", SimpleNamespace(id="i1", value=SimpleNamespace(
                tool_name="ask_user", questions=[{"question": "按哪种口径？", "options": [{"label": "甲"}]}])))

        async def second():
            yield chunk("llm_output", {"content": "按甲做完了"})

        return first() if len(rounds) == 1 else second()

    async def slow_answer(tool_name, tool_input, title, description):
        await asyncio.sleep(1.0)  # 用户想了一会儿才回答
        return {"behavior": "allow", "updated_input": {"answers": {"按哪种口径？": "甲"}}}

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(fake_stream))
    monkeypatch.setattr(config, "JIUWEN_STALL_SECONDS", 0.3)
    engine = JiuwenEngine(make_ctx(tmp_path, permission=slow_answer))
    engine.client = SimpleNamespace(abort=lambda: None)

    async def scenario():
        await asyncio.wait_for(engine._run_turn("开始"), timeout=10)
        return _drain(engine)

    events = asyncio.run(scenario())
    assert events[-1]["type"] == "result" and events[-1]["subtype"] == "success"


def test_model_call_start_counts_as_activity():
    started = []
    rail = _call_usage_rail(lambda *args: None, on_activity=lambda: started.append(True))
    asyncio.run(rail.before_model_call(SimpleNamespace()))
    assert started
