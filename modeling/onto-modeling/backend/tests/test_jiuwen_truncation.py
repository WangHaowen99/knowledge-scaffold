"""Jiuwen 引擎的截断兜底：思考把 max_tokens 用光、没调工具、正文几乎为空的回复不能当正常完成。

单测 Rail 的判定与计数；再用合成模型 + 真实 Runner/TaskTool 跑子代理与主 agent 两条路径。
"""

import asyncio
import json
import uuid
from types import SimpleNamespace

from app.services.engines.jiuwen import (
    TRUNCATED_STOP_REASON,
    TRUNCATION_FAILURE,
    TRUNCATION_NUDGE,
    TRUNCATION_USER_TEXT,
    _ChunkBridge,
    _TOOL_NAME_MAP,
    _bind_subagent_workspace_rotation,
    _general_purpose_spec,
    _truncation_rail,
    is_truncated_empty,
)
from app.services.engines.jiuwen_progress import JiuwenProgressTracker, build_progress_rail, progress_scope


def _reply(content="", finish_reason="stop", tool_calls=None):
    return SimpleNamespace(content=content, finish_reason=finish_reason, tool_calls=tool_calls)


def test_only_truncated_empty_tool_free_reply_counts():
    assert is_truncated_empty(_reply("源", "length"))
    assert is_truncated_empty(_reply("", "max_tokens"))
    assert not is_truncated_empty(_reply("源", "stop"))
    assert not is_truncated_empty(_reply("", "length", tool_calls=[object()]))
    assert not is_truncated_empty(_reply("完整的长回答" * 40, "length"))
    assert not is_truncated_empty(None)


class _Ctx:
    def __init__(self):
        self.extra = {}
        self.added = []
        self.continued = 0
        self.finished = None
        self.context = None
        self.inputs = SimpleNamespace(response=None, model_context=self)

    async def add_messages(self, message):
        self.added.append(message)

    def request_model_continue(self):
        self.continued += 1

    def request_force_finish(self, result):
        self.finished = result


def _after(rail, ctx, reply):
    ctx.inputs.response = reply
    asyncio.run(rail.after_model_call(ctx))


def test_rail_nudges_once_then_fails():
    rail = _truncation_rail()
    ctx = _Ctx()
    _after(rail, ctx, _reply("源", "length"))
    assert ctx.continued == 1 and ctx.finished is None
    # 提示在下一次调用前进上下文，只加一次
    asyncio.run(rail.before_model_call(ctx))
    asyncio.run(rail.before_model_call(ctx))
    assert [m.content for m in ctx.added] == [TRUNCATION_NUDGE]
    _after(rail, ctx, _reply("", "length"))
    assert ctx.continued == 1
    assert ctx.finished == {"output": TRUNCATION_FAILURE, "result_type": "error", "stop_reason": TRUNCATED_STOP_REASON}


def test_rail_counts_only_consecutive_truncations():
    rail = _truncation_rail()
    ctx = _Ctx()
    _after(rail, ctx, _reply("源", "length"))
    asyncio.run(rail.before_model_call(ctx))
    _after(rail, ctx, _reply("", "stop", tool_calls=[object()]))
    _after(rail, ctx, _reply("源", "length"))
    assert ctx.continued == 2 and ctx.finished is None


def test_bridge_shows_main_agent_truncation_and_drops_fragment():
    bridge = _ChunkBridge()
    bridge.feed(SimpleNamespace(type="llm_output", payload={"content": "源"}))
    events, _ = bridge.feed(SimpleNamespace(type="answer", payload={
        "output": TRUNCATION_FAILURE, "result_type": "error", "stop_reason": TRUNCATED_STOP_REASON,
    }))
    assert events[0]["blocks"] == [{"kind": "text", "text": TRUNCATION_USER_TEXT}]
    # 普通 answer 仍不重复展示
    assert bridge.feed(SimpleNamespace(type="answer", payload={"output": "x", "result_type": "answer"})) == ([], None)


def _synthetic_model(script):
    """按脚本依次回复的合成模型；script 的每项是 AssistantMessage 的参数，记下每次收到的消息。"""
    from openjiuwen.core.foundation.llm import init_model
    from openjiuwen.core.foundation.llm.model import Model
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk

    class SyntheticModel(Model):
        def __init__(self):
            # 不发真实请求，凭据位用一次性随机值
            template = init_model(
                provider="OpenAI", model_name="synthetic-truncation",
                api_key=uuid.uuid4().hex, api_base="http://127.0.0.1:9/unused",
            )
            super().__init__(template.model_client_config, template.model_config)
            self.seen = []

        def _next(self, args, kwargs):
            self.seen.append(str(kwargs.get("messages") if "messages" in kwargs else args[:1]))
            return script[min(len(self.seen), len(script)) - 1]

        async def invoke(self, *args, **kwargs):
            from openjiuwen.core.foundation.llm.schema.message import AssistantMessage

            return AssistantMessage(**self._next(args, kwargs))

        async def stream(self, *args, **kwargs):
            yield AssistantMessageChunk(**self._next(args, kwargs))

    return SyntheticModel()


def _dispatch():
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall

    return {"tool_calls": [ToolCall(
        id="synthetic-dispatch", type="function", name="task_tool",
        arguments=json.dumps({"subagent_type": "general-purpose", "task_description": "写合成操作文档"}),
    )], "finish_reason": "tool_calls"}


TRUNCATED = {"content": "源", "finish_reason": "length"}


def _run(tmp_path, script, *, with_subagent):
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.workspace.workspace import Workspace

    async def scenario():
        await Runner.start()
        events, chunks = [], []
        tracker = JiuwenProgressTracker(events.append)
        model = _synthetic_model(script)
        rails = [_truncation_rail(), build_progress_rail(_TOOL_NAME_MAP)]
        subagent_root = tmp_path / "sub_agents"
        spec = _general_purpose_spec(
            model=model, system_prompt="Use only synthetic data.", rails=rails,
            workspace_root=subagent_root, sys_operation=None,
        )
        agent = create_deep_agent(
            model=model, rails=rails,
            workspace=Workspace(root_path=str(tmp_path), language="cn"),
            system_prompt="Use only synthetic data.", add_general_purpose_agent=with_subagent,
            subagents=[spec] if with_subagent else None, enable_task_loop=False, parallel_tool_calls=False,
            enable_read_image_multimodal=False,
        )
        if with_subagent:
            _bind_subagent_workspace_rotation(agent, spec, subagent_root)
        try:
            async with asyncio.timeout(60):
                with progress_scope(tracker):
                    async for chunk in Runner.run_agent_streaming(
                        agent, {"query": "合成任务"}, session=f"synthetic-{uuid.uuid4().hex}",
                    ):
                        chunks.append(chunk)
        finally:
            await tracker.finish_all("stopped")
            await Runner.stop()
        return events, chunks, model

    return asyncio.run(scenario())


def test_subagent_recovers_after_one_nudge(tmp_path):
    script = [_dispatch(), TRUNCATED, {"content": "子任务完成", "finish_reason": "stop"},
              {"content": "全部完成", "finish_reason": "stop"}]
    events, _, model = _run(tmp_path, script, with_subagent=True)
    finished = [e for e in events if e["type"] == "subagent_finished"]
    assert [e["status"] for e in finished] == ["completed"]
    # 续的那次调用带上了提示
    assert TRUNCATION_NUDGE in model.seen[2]


def test_subagent_fails_with_reason_after_two_truncations(tmp_path):
    script = [_dispatch(), TRUNCATED, TRUNCATED, {"content": "知道了", "finish_reason": "stop"}]
    events, _, model = _run(tmp_path, script, with_subagent=True)
    finished = [e for e in events if e["type"] == "subagent_finished"]
    assert [e["status"] for e in finished] == ["failed"]
    # 主 agent 下一次调用看到的是失败原因，不是「源」这种残片
    assert TRUNCATION_FAILURE in model.seen[3]


def _text_blocks(events) -> list[str]:
    texts = []
    for event in events:
        texts += [block["text"] for block in event.get("blocks", []) if block.get("kind") == "text"]
    return texts


def test_main_agent_truncation_tells_user(tmp_path):
    _, chunks, _ = _run(tmp_path, [TRUNCATED, TRUNCATED], with_subagent=False)
    bridge = _ChunkBridge()
    texts = []
    for chunk in chunks:
        events, _ = bridge.feed(chunk)
        texts += _text_blocks(events)
    texts += _text_blocks(bridge.finish())
    assert texts == [TRUNCATION_USER_TEXT]
