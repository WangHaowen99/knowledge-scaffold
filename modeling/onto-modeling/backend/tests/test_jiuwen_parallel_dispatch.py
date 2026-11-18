"""Jiuwen 并行派发与按 agent 文件关闭思考（自主构建提速）。

- 同一条消息里的多个 task_tool 调用并行执行：两个子代理都开始了才有一个结束；
- 任务说明开头引用的 agent 文件第一行是 QUIET_MARKER 的，子代理用关闭思考的模型实例，其余用原模型；
- 桥接层按原生调用 id 配对，并行时结果乱序回来也配得上。

测试数据全部为合成内容。
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from types import SimpleNamespace

from app import config
from app.services.engines.jiuwen import (
    QUIET_MARKER,
    _bind_subagent_workspace_rotation,
    _ChunkBridge,
    _general_purpose_spec,
    _install_dispatch_brief_capture,
    quiet_dispatch,
    quiet_extra_body,
)

BRIEF = "请先完整阅读 .claude/agents/{}.md，严格按其中的规则与输出格式执行。\nPART: synthetic\n"


def _agent_file(root, name: str, first_line: str) -> None:
    path = root / ".claude" / "agents" / f"{name}.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(first_line + "\nSynthetic agent.\n", encoding="utf-8")


def test_quiet_dispatch_reads_marker_from_referenced_agent_file(tmp_path):
    _agent_file(tmp_path, "quiet-agent", QUIET_MARKER)
    _agent_file(tmp_path, "busy-agent", "You are a synthetic agent.")
    assert quiet_dispatch(BRIEF.format("quiet-agent"), tmp_path)
    assert not quiet_dispatch(BRIEF.format("busy-agent"), tmp_path)
    assert not quiet_dispatch(BRIEF.format("missing-agent"), tmp_path)
    assert not quiet_dispatch("分析合成资料", tmp_path)
    assert not quiet_dispatch("一\n二\n三\n" + BRIEF.format("quiet-agent"), tmp_path)  # 只认开头几行
    assert not quiet_dispatch("请先完整阅读 .claude/agents/../quiet-agent.md", tmp_path)


def test_quiet_extra_body_parses_config(monkeypatch):
    monkeypatch.setattr(config, "JIUWEN_QUIET_EXTRA_BODY", '{"enable_thinking": false}')
    assert quiet_extra_body() == {"enable_thinking": False}
    for raw in ("", "   ", "not json", "[]", "{}"):
        monkeypatch.setattr(config, "JIUWEN_QUIET_EXTRA_BODY", raw)
        assert quiet_extra_body() is None, raw


def test_bridge_pairs_parallel_results_by_native_id():
    bridge = _ChunkBridge()
    for call_id in ("call-a", "call-b"):
        bridge.feed(SimpleNamespace(type="tool_call", payload={
            "tool_name": "task_tool", "tool_args": {"task_description": call_id}, "tool_use_id": call_id}))
    paired = []
    for call_id in ("call-b", "call-a"):  # 并行时后派的先回来
        events, _ = bridge.feed(SimpleNamespace(type="tool_result", payload={
            "tool_result": f"result of {call_id}", "tool_use_id": call_id}))
        block = events[0]["blocks"][0]
        paired.append((block["tool_use_id"], block["excerpt"]))
    assert paired == [("call-b", "result of call-b"), ("call-a", "result of call-a")]


def test_real_runner_runs_dispatches_in_one_message_in_parallel_with_quiet_model(tmp_path):
    """合成模型 + 真实 TaskTool/Runner：父 agent 一条消息里派两个子任务。"""
    from openjiuwen.core.foundation.llm import init_model
    from openjiuwen.core.foundation.llm.model import Model
    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        UsageMetadata,
    )
    from openjiuwen.core.foundation.llm.schema.message_chunk import (
        AssistantMessageChunk,
    )
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.workspace.workspace import Workspace

    _agent_file(tmp_path, "quiet-agent", QUIET_MARKER)
    _agent_file(tmp_path, "busy-agent", "You are a synthetic agent.")
    log: list[tuple[str, str, str, float]] = []

    def text_of(messages) -> str:
        if isinstance(messages, str):
            return messages
        return json.dumps([getattr(item, "content", item) for item in messages or []], ensure_ascii=False, default=str)

    def has_tool_results(messages) -> bool:
        return any((getattr(item, "role", None) or (item.get("role") if isinstance(item, dict) else None)) == "tool"
                   for item in (messages if isinstance(messages, list) else []))

    class SyntheticModel(Model):
        def __init__(self, label: str):
            # 不发真实请求（invoke/stream 都被覆盖），凭据位用一次性随机值
            template = init_model(provider="OpenAI", model_name="synthetic-parallel", api_key=uuid.uuid4().hex,
                                  api_base="http://127.0.0.1:9/unused")
            super().__init__(template.model_client_config, template.model_config)
            self.label = label

        async def respond(self, messages):
            text = text_of(messages)
            usage = UsageMetadata(input_tokens=5, output_tokens=3)
            if "派发合成任务" not in text:  # 子代理：任务说明里引用了哪个 agent 文件
                name = "quiet" if "quiet-agent" in text else "busy"
                log.append((self.label, name, "start", time.monotonic()))
                await asyncio.sleep(0.6)
                log.append((self.label, name, "end", time.monotonic()))
                return AssistantMessage(content=f"{name} 完成", usage_metadata=usage)
            if has_tool_results(messages):
                return AssistantMessage(content="合成任务完成", usage_metadata=usage)
            return AssistantMessage(tool_calls=[
                ToolCall(id=f"synthetic-call-{name}", type="function", name="task_tool", arguments=json.dumps({
                    "subagent_type": "general-purpose", "task_description": BRIEF.format(f"{name}-agent")}))
                for name in ("quiet", "busy")
            ], usage_metadata=usage)

        async def invoke(self, messages, *_args, **_kwargs):
            return await self.respond(messages)

        async def stream(self, messages, *_args, **_kwargs):
            response = await self.respond(messages)
            yield AssistantMessageChunk(content=response.content, tool_calls=response.tool_calls,
                                        usage_metadata=response.usage_metadata, finish_reason="stop")

    async def scenario():
        await Runner.start()
        model, quiet = SyntheticModel("normal"), SyntheticModel("quiet")
        subagent_root = tmp_path / "sub_agents"
        spec = _general_purpose_spec(model=model, system_prompt="Use only synthetic data.", rails=[],
                                     workspace_root=subagent_root, sys_operation=None)
        agent = create_deep_agent(
            model=model, rails=[], workspace=Workspace(root_path=str(tmp_path), language="cn"),
            system_prompt="Use only synthetic data.", add_general_purpose_agent=True,
            subagents=[spec], enable_task_loop=False, parallel_tool_calls=True, enable_read_image_multimodal=False,
        )
        _install_dispatch_brief_capture()
        _bind_subagent_workspace_rotation(agent, spec, subagent_root, quiet_model=quiet, cwd=tmp_path)
        try:
            async with asyncio.timeout(60):
                async for _ in Runner.run_agent_streaming(agent, {"query": "派发合成任务"},
                                                          session=f"synthetic-{uuid.uuid4().hex}"):
                    pass
        finally:
            await Runner.stop()

    asyncio.run(scenario())

    served = {(label, name) for label, name, kind, _ in log if kind == "start"}
    assert served == {("quiet", "quiet"), ("normal", "busy")}, log
    starts = [at for _, _, kind, at in log if kind == "start"]
    ends = [at for _, _, kind, at in log if kind == "end"]
    assert len(starts) == 2 and max(starts) < min(ends), log  # 两个都开始了才有一个结束：并行
