"""Jiuwen 同步子代理步骤流测试（Rail 回调 → 平台事件）。"""

import asyncio
import json
import uuid
from types import SimpleNamespace

from app.services.engines.jiuwen import (
    JiuwenEngine,
    _ChunkBridge,
    _TOOL_NAME_MAP,
    _bind_subagent_workspace_rotation,
    _general_purpose_spec,
)
from app.services.engines.jiuwen_progress import (
    _MAX_STEPS,
    JiuwenProgressTracker,
    build_progress_rail,
    progress_scope,
)


class FakeSession:
    def __init__(self):
        self.events = []

    async def write_stream(self, event):
        self.events.append(event)


def context(tool_name, tool_args, tool_call_id):
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name=tool_name,
            tool_args=tool_args,
            tool_call=SimpleNamespace(id=tool_call_id),
            tool_result=SimpleNamespace(success=True),
            tool_msg=None,
            response=None,
        ),
        session=FakeSession(),
        extra={},
        exception=None,
    )


def test_tracker_emits_step_stream_without_heartbeat():
    async def scenario():
        events = []
        clock = [0.0]
        tracker = JiuwenProgressTracker(events.append, clock=lambda: clock[0])
        task = await tracker.start(
            "call-1", {"description": "分析资料", "subagent_type": "general-purpose"}
        )
        await tracker.step(task, "step-1", "Read", {"file_path": "notes.md"})
        await tracker.step_done(task, "step-1", False)
        await tracker.say(task, "已读完资料，开始整理")
        clock[0] = 63.0
        await tracker.finish(task, "completed")
        return events

    events = asyncio.run(scenario())

    assert [event["type"] for event in events] == [
        "subagent_started",
        "subagent_activity",
        "subagent_activity",
        "subagent_activity",
        "subagent_finished",
    ]
    assert events[1]["kind"] == "tool" and events[1]["tool"] == "Read"
    assert events[1]["input"] == {"file_path": "notes.md"}
    assert "status" not in events[1]
    assert events[2]["step_id"] == "step-1" and events[2]["status"] == "ok"
    assert events[3]["kind"] == "text" and events[3]["text"] == "已读完资料，开始整理"
    # 运行中不报耗时，终态只报一次
    assert all("usage" not in event for event in events[:-1])
    assert events[-1]["usage"] == {"duration_ms": 63000}


def test_step_input_keeps_only_summary_keys():
    async def scenario():
        events = []
        tracker = JiuwenProgressTracker(events.append)
        task = await tracker.start("call-1", {"task_description": "# 合成提示词\n" * 400})
        await tracker.step(
            task,
            "step-1",
            "Write",
            {
                "file_path": "ontology/objects.md",
                "content": "SYNTHETIC-BODY " * 200,
                "description": "落盘候选对象",
            },
        )
        return events

    events = asyncio.run(scenario())

    # 整段提示词不当名称，整段作任务说明（只留前 2000 字），名称由会话宿主回退
    assert events[0]["description"] == ""
    assert events[0]["brief"].startswith("# 合成提示词") and len(events[0]["brief"]) == 2000
    assert set(events[1]["input"]) == {"file_path", "description"}
    assert "SYNTHETIC-BODY" not in json.dumps(events, ensure_ascii=False)


def test_short_task_description_names_the_card():
    async def scenario():
        events = []
        tracker = JiuwenProgressTracker(events.append)
        await tracker.start("call-1", {"task_description": "检查合成操作的语义", "subagent_type": "general-purpose"})
        await tracker.start("call-2", {"description": "规则检查", "task_description": "第一行\n第二行"})
        return events

    events = asyncio.run(scenario())
    assert events[0]["description"] == "检查合成操作的语义" and events[0]["brief"] == "检查合成操作的语义"
    assert events[1]["description"] == "规则检查" and events[1]["brief"] == "第一行\n第二行"


def test_thinking_reported_every_interval_until_the_model_returns():
    """子代理只在想、不调工具：每个间隔报一次「思考中」（带开始时刻），模型回来就停；不占步骤额度。"""
    async def scenario():
        rail = build_progress_rail(_TOOL_NAME_MAP)
        events = []
        tracker = JiuwenProgressTracker(events.append, thinking_every=0.02)
        parent = context("task_tool", {"task_description": "合成任务"}, "parent-call")
        child = context("", {}, "child-model")
        with progress_scope(tracker):
            await rail.before_tool_call(parent)
            await rail.before_model_call(child)
            await asyncio.sleep(0.07)
            child.inputs.response = SimpleNamespace(content="想完了", tool_calls=None)
            await rail.after_model_call(child)
            count = len(events)
            await asyncio.sleep(0.05)
            assert len(events) == count  # 模型回来之后不再报
            await rail.before_model_call(child)
            await rail.on_model_exception(child)  # 出错也停
            await asyncio.sleep(0.05)
            assert len(events) == count
            await rail.after_tool_call(parent)
        return events, tracker

    events, tracker = asyncio.run(scenario())
    thinking = [event for event in events if event.get("kind") == "thinking"]
    assert len(thinking) >= 2
    assert all(event["type"] == "subagent_activity" and event["step_id"] == "parent-call-thinking" for event in thinking)
    assert thinking[0]["since"] == thinking[-1]["since"] and thinking[-1]["elapsed_ms"] >= thinking[0]["elapsed_ms"]
    assert [event.get("text") for event in events if event.get("kind") == "text"] == ["想完了"]
    assert tracker._tasks == {}


def test_thinking_does_not_count_as_activity_for_the_stall_guard(tmp_path):
    from app.services.engines.base import EngineContext

    engine = JiuwenEngine(EngineContext(
        project_id="project_alpha", cwd=tmp_path, request_permission=None,
        load_resume_token=lambda: None, save_resume_token=lambda token: None,
        clear_resume_token=lambda: None, log_error=lambda kind, text: None,
    ))

    async def scenario():
        engine._last_activity = 0.0
        await engine._progress_put({"type": "subagent_activity", "kind": "thinking", "since": 1.0})
        thinking_seen = engine._last_activity
        await engine._progress_put({"type": "subagent_activity", "kind": "tool", "step_id": "s1"})
        return thinking_seen, engine._last_activity

    thinking_seen, after_step = asyncio.run(scenario())
    assert thinking_seen == 0.0 and after_step > 0.0


def test_finish_reports_a_short_summary_and_hides_raw_failures():
    from app.services.engines.jiuwen import TRUNCATION_FAILURE

    async def scenario():
        rail = build_progress_rail(_TOOL_NAME_MAP)
        events = []
        tracker = JiuwenProgressTracker(events.append)
        ok = context("task_tool", {"task_description": "合成任务甲"}, "call-ok")
        ok.inputs.tool_result = SimpleNamespace(success=True, data={"output": "检查完了：\n  3 个问题" + "。" * 400})
        cut = context("task_tool", {"task_description": "合成任务乙"}, "call-cut")
        raw = context("task_tool", {"task_description": "合成任务丙"}, "call-raw")
        cut.exception = RuntimeError(f"Subagent general-purpose execution failed: {TRUNCATION_FAILURE}")
        raw.exception = RuntimeError("synthetic private exception detail")
        with progress_scope(tracker):
            for parent in (ok, cut, raw):
                await rail.before_tool_call(parent)
                await rail.after_tool_call(parent)
        return {event["task_id"]: event for event in events if event["type"] == "subagent_finished"}

    final = asyncio.run(scenario())
    assert final["call-ok"]["status"] == "completed"
    assert final["call-ok"]["summary"].startswith("检查完了： 3 个问题") and len(final["call-ok"]["summary"]) == 301
    assert final["call-cut"]["status"] == "failed" and final["call-cut"]["summary"] == TRUNCATION_FAILURE
    assert final["call-raw"]["summary"] == "子任务出错，没有交回结果"
    assert "synthetic private exception detail" not in json.dumps(final, ensure_ascii=False)


def test_json_result_is_not_a_step_and_finishes_as_business_text():
    """规则检查子代理按约定交回 JSON：过程里不出原文，结论换成说法（截断前换）。"""
    issues = [{"severity": "警告", "category": "命名", "file": "operation/门店补货.md", "message": "名称" + "长" * 400}]

    async def scenario():
        rail = build_progress_rail(_TOOL_NAME_MAP)
        events = []
        tracker = JiuwenProgressTracker(events.append)
        results = {"call-none": "[]", "call-some": json.dumps(issues, ensure_ascii=False)}
        with progress_scope(tracker):
            for call_id, output in results.items():
                parent = context("task_tool", {"task_description": "规则检查"}, call_id)
                parent.inputs.tool_result = SimpleNamespace(success=True, data={"output": output})
                await rail.before_tool_call(parent)
                task = tracker._tasks[call_id]
                await tracker.say(task, output)
                await rail.after_tool_call(parent)
        return events

    events = asyncio.run(scenario())
    assert not [event for event in events if event.get("kind") == "text"]
    final = {event["task_id"]: event["summary"] for event in events if event["type"] == "subagent_finished"}
    assert final["call-none"] == "没有发现问题"
    head, detail = final["call-some"].split("\n")
    assert head == "发现 1 个问题：警告 1" and detail.startswith("· 警告 · 操作「门店补货」：名称长") and detail.endswith("…")


def test_step_budget_caps_activity_events():
    async def scenario():
        events = []
        tracker = JiuwenProgressTracker(events.append)
        task = await tracker.start("call-1", {})
        for index in range(_MAX_STEPS + 5):
            await tracker.step(task, f"step-{index}", "Read", {"path": f"f{index}.md"})
            await tracker.step_done(task, f"step-{index}", False)
        return events

    events = asyncio.run(scenario())
    activity = [event for event in events if event["type"] == "subagent_activity"]
    starts = [event for event in activity if "status" not in event]

    assert _MAX_STEPS == 200
    # 最坏一个子任务的步骤事件总量约 100 KB 以内（参数摘要截断仍然生效）
    assert len(json.dumps(events, ensure_ascii=False).encode()) < 100_000
    assert len(starts) == _MAX_STEPS + 1  # 最后一条是省略提示
    assert starts[-1]["kind"] == "text" and "省略" in starts[-1]["text"]
    # 被省略的步骤不会再出现状态更新
    assert len([event for event in activity if event.get("status")]) == _MAX_STEPS


def test_progress_rail_forwards_parent_and_child_activity():
    async def scenario():
        rail = build_progress_rail(_TOOL_NAME_MAP)
        events = []
        tracker = JiuwenProgressTracker(events.append)
        parent = context(
            "task_tool",
            {"description": "分析资料", "subagent_type": "general-purpose"},
            "parent-call",
        )
        child = context("read_file", {"file_path": "notes.md"}, "child-call")
        with progress_scope(tracker):
            await rail.before_tool_call(parent)
            await rail.before_tool_call(child)
            child.inputs.response = SimpleNamespace(content="读完了", tool_calls=None)
            await rail.after_model_call(child)
            await rail.after_tool_call(child)
            await rail.after_tool_call(parent)
        return events

    events = asyncio.run(scenario())

    assert events[0]["type"] == "subagent_started"
    assert events[1]["tool"] == "Read" and events[1]["step_id"] == "child-call"
    assert [event.get("text") for event in events if event.get("kind") == "text"] == ["读完了"]
    assert any(event.get("status") == "ok" for event in events)
    assert events[-1]["type"] == "subagent_finished"
    assert events[-1]["status"] == "completed"
    # 父 task_tool 自身不作为步骤出现在面板里
    assert all(event.get("tool") != "Task" for event in events)


def test_rail_isolates_parallel_parent_tasks():
    async def scenario():
        rail = build_progress_rail(_TOOL_NAME_MAP)
        events = []
        tracker = JiuwenProgressTracker(events.append)
        gate = asyncio.Event()

        async def delegate(call_id, tool_name, failed):
            parent = context("task_tool", {}, call_id)
            child = context(tool_name, {}, f"{call_id}-child")
            await rail.before_tool_call(parent)
            await gate.wait()
            await rail.before_tool_call(child)
            if failed:
                child.exception = RuntimeError("synthetic private exception detail")
                await rail.on_tool_exception(child)
                parent.inputs.tool_result = SimpleNamespace(success=False)
            await rail.after_tool_call(child)
            await rail.after_tool_call(parent)

        with progress_scope(tracker):
            tasks = [
                asyncio.create_task(delegate("call-a", "read_file", False)),
                asyncio.create_task(delegate("call-b", "write_file", True)),
            ]
            await asyncio.sleep(0)
            gate.set()
            await asyncio.gather(*tasks)
            # 主 agent 自己的模型调用不属于任何子任务
            count = len(events)
            await rail.after_model_call(context("", {}, "main"))
            assert len(events) == count
        return events

    events = asyncio.run(scenario())
    steps = {
        event["tool_use_id"]: event
        for event in events
        if event["type"] == "subagent_activity" and "status" not in event
    }
    final = {
        event["tool_use_id"]: event
        for event in events
        if event["type"] == "subagent_finished"
    }

    assert steps["call-a"]["tool"] == "Read" and steps["call-b"]["tool"] == "Write"
    assert final["call-a"]["status"] == "completed"
    assert final["call-b"]["status"] == "failed"
    assert "synthetic private exception detail" not in json.dumps(events)


def test_bridge_uses_native_ids_for_results_arriving_out_of_order():
    bridge = _ChunkBridge()
    for call_id in ("call-a", "call-b"):
        events, _ = bridge.feed(SimpleNamespace(
            type="tool_call", payload={"tool_use_id": call_id, "tool_name": "task_tool", "tool_args": {}},
        ))
        assert events[0]["blocks"][0]["tool_use_id"] == call_id
    for call_id in ("call-b", "call-a"):
        events, _ = bridge.feed(SimpleNamespace(
            type="tool_result", payload={"tool_use_id": call_id, "is_error": False, "tool_result": "synthetic"},
        ))
        assert events[0]["blocks"][0]["tool_use_id"] == call_id
    assert not bridge._open_calls


def test_engine_cancellation_finishes_child_before_round_result(tmp_path, monkeypatch):
    from openjiuwen.core.runner import Runner
    from app.services.engines.base import EngineContext

    rail = build_progress_rail(_TOOL_NAME_MAP)

    async def stream(*_args, **_kwargs):
        parent = context("task_tool", {}, "synthetic-cancel-call")
        await rail.before_tool_call(parent)
        yield parent.session.events[0]
        await asyncio.Event().wait()

    monkeypatch.setattr(Runner, "run_agent_streaming", staticmethod(stream))

    async def permission(*_args):
        return {"behavior": "allow"}

    async def scenario():
        engine = JiuwenEngine(EngineContext(
            project_id="project_alpha", cwd=tmp_path, request_permission=permission,
            load_resume_token=lambda: None, save_resume_token=lambda token: None,
            clear_resume_token=lambda: None, log_error=lambda kind, text: None,
        ))
        engine.client = object()
        running = asyncio.create_task(engine._run_turn("synthetic task"))
        first = await asyncio.wait_for(engine._queue.get(), timeout=1)
        running.cancel()
        try:
            await running
        except asyncio.CancelledError:
            pass
        tail = []
        while not engine._queue.empty():
            tail.append(engine._queue.get_nowait())
        assert engine._progress_tracker._tasks == {}
        return first, tail

    first, tail = asyncio.run(scenario())
    assert first["type"] == "subagent_started"
    assert tail[-2]["type"] == "subagent_finished"
    assert tail[-2]["status"] == "stopped"
    assert tail[-1]["type"] == "result" and tail[-1]["subtype"] == "interrupted"


def test_real_runner_runs_repeated_subagent_dispatches(tmp_path):
    """合成模型 + 真实 TaskTool/Runner：连续两次派发都要跑起来并回报过程。

    两个回归点：
    1. 子代理 spec 若沿用主 agent 的 workspace 根（SDK 的 add_general_purpose_agent
       注入版行为），第一个子代理就会在首个模型调用前永久挂住；
    2. 两次派发若共用同一个子代理 workspace 根，第二个子代理写同名脚手架文件会卡满
       write_file 的 300 秒 lock_timeout——卡片一直停在"已启动，等待子代理反馈"。
    任一条回归都会让这个用例超时。
    """
    from openjiuwen.core.foundation.llm import init_model
    from openjiuwen.core.foundation.llm.model import Model
    from openjiuwen.core.foundation.llm.schema.message import AssistantMessage, UsageMetadata
    from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk
    from openjiuwen.core.foundation.llm.schema.tool_call import ToolCall
    from openjiuwen.core.runner import Runner
    from openjiuwen.harness import create_deep_agent
    from openjiuwen.harness.workspace.workspace import Workspace

    # 父 agent 的第 1、3 次模型调用派发子任务，其余轮次直接作答
    DISPATCH_ROUNDS = (1, 3)

    class SyntheticModel(Model):
        def __init__(self):
            # 合成模型不发真实请求（invoke/stream 都被覆盖），凭据位用一次性随机值，
            # 仓库里不出现任何形似密钥的常量
            template = init_model(
                provider="OpenAI", model_name="synthetic-progress",
                api_key=uuid.uuid4().hex, api_base="http://127.0.0.1:9/unused",
            )
            super().__init__(template.model_client_config, template.model_config)
            self.calls = 0

        def response(self):
            self.calls += 1
            if self.calls in DISPATCH_ROUNDS:
                return AssistantMessage(tool_calls=[ToolCall(
                    id=f"synthetic-parent-call-{self.calls}", type="function", name="task_tool",
                    arguments=json.dumps({
                        "subagent_type": "general-purpose",
                        "task_description": "分析合成资料",
                    }),
                )])
            return AssistantMessage(
                content="合成任务完成", usage_metadata=UsageMetadata(input_tokens=5, output_tokens=3),
            )

        async def invoke(self, *_args, **_kwargs):
            return self.response()

        async def stream(self, *_args, **_kwargs):
            response = self.response()
            yield AssistantMessageChunk(
                content=response.content, tool_calls=response.tool_calls,
                usage_metadata=response.usage_metadata, finish_reason="stop",
            )

    async def scenario():
        await Runner.start()
        events = []
        tracker = JiuwenProgressTracker(events.append)
        model = SyntheticModel()
        rails = [build_progress_rail(_TOOL_NAME_MAP)]
        subagent_root = tmp_path / "sub_agents"
        spec = _general_purpose_spec(
            model=model, system_prompt="Use only synthetic data.", rails=rails,
            workspace_root=subagent_root, sys_operation=None,
        )
        agent = create_deep_agent(
            model=model, rails=rails,
            workspace=Workspace(root_path=str(tmp_path), language="cn"),
            system_prompt="Use only synthetic data.", add_general_purpose_agent=True,
            subagents=[spec], enable_task_loop=False, parallel_tool_calls=False,
            enable_read_image_multimodal=False,
        )
        _bind_subagent_workspace_rotation(agent, spec, subagent_root)
        try:
            async with asyncio.timeout(60):
                with progress_scope(tracker):
                    async for _ in Runner.run_agent_streaming(
                        agent, {"query": "派发合成任务"}, session=f"synthetic-{uuid.uuid4().hex}",
                    ):
                        pass
        finally:
            await tracker.finish_all("stopped")
            await Runner.stop()
        return events, subagent_root

    events, subagent_root = asyncio.run(scenario())

    started = [event for event in events if event["type"] == "subagent_started"]
    finished = [event for event in events if event["type"] == "subagent_finished"]
    assert [event["tool_use_id"] for event in started] == [
        "synthetic-parent-call-1",
        "synthetic-parent-call-3",
    ]
    assert [event["status"] for event in finished] == ["completed", "completed"]
    # 每个子任务都回报了自己的过程
    for event in started:
        assert any(
            other["type"] == "subagent_activity" and other["tool_use_id"] == event["tool_use_id"]
            for other in events
        )
    # 每次派发落在自己的工件目录，彼此不共用脚手架路径
    assert len([path for path in subagent_root.iterdir() if path.is_dir()]) == 2
