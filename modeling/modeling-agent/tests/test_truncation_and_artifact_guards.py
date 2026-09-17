"""脚本契约确认后运行空转的两道守卫。

线上现场：专家确认脚本契约后，模型连续几轮把输出预算全用在推理上，一个
token 的正文和工具调用都没产生。这些零产出轮次被当成「成功但没话说」，
耗完停滞预算后直接结算完成——页面上表现为待办消失、没有下一张卡、也没有
任何报错。这里覆盖识别截断的判据，以及本轮到底有没有形成脚本产物的判断。
"""

from __future__ import annotations

import asyncio

import pytest
from openjiuwen.core.foundation.llm.schema.message_chunk import AssistantMessageChunk

import modeling_agent.service as service_module
from modeling_agent.schemas import RUN_QUEUED, RunRecord

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


async def wait_for_status(runtime, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.store.require_run(run_id).public()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def run_record(run_id: str, project_id: str) -> RunRecord:
    return RunRecord(
        id=run_id,
        project_id=project_id,
        session_id=f"project:{project_id}",
        status=RUN_QUEUED,
        prompt="开始建模",
        token_fingerprint="000000000000",
    )


def model_call(**overrides) -> dict:
    """构造一次主模型调用的度量，字段与 ActivityRail 发出的 model_call 一致。"""
    metrics = {
        "actor": "modeling_leader",
        "phase": "completed",
        "outcome": "reasoning_only",
        "reasoningChars": 15_568,
        "contentChars": 0,
        "finishReason": "length",
        "outputTokens": 8_192,
        "reasoningTokens": 8_192,
    }
    metrics.update(overrides)
    return metrics


def test_truncation_is_detected_when_reasoning_consumes_the_whole_budget() -> None:
    # 推理占满全部输出 token 且没有正文：预算全被思维链吃掉，本轮零产出。
    assert service_module._model_output_truncated(model_call()) is True


def test_saturated_budget_with_tool_calls_is_not_truncation() -> None:
    # 线上实测：输出预算同样用满 8192，但推理只占 7174，剩下的吐成了工具调用。
    # 只看「预算用满」会把这种成功轮误判成截断。
    assert service_module._model_output_truncated(
        model_call(outcome="tool_calls", reasoningTokens=7_174),
    ) is False


def test_character_counts_do_not_decide_truncation() -> None:
    # 截断轮的推理是 15568 字符，成功轮到过 14655 字符，字符数互有高低。
    # 同一组 token 关系下，字符数多少都必须得到同一个结论。
    assert service_module._model_output_truncated(
        model_call(reasoningChars=14_655),
    ) is True
    assert service_module._model_output_truncated(
        model_call(outcome="tool_calls", reasoningChars=19_613, reasoningTokens=7_174),
    ) is False


def test_reasoning_only_turn_below_the_cap_is_still_zero_output() -> None:
    # 没顶到预算上限，但推理占掉了全部 output token、正文为空：同样是零产出，
    # 继续往下走只会把它当成「成功但没话说」。
    assert service_module._model_output_truncated(
        model_call(outputTokens=6_995, reasoningTokens=6_995),
    ) is True


def test_turn_with_visible_content_is_not_truncation() -> None:
    assert service_module._model_output_truncated(
        model_call(outcome="content", contentChars=320),
    ) is False


def test_missing_usage_metrics_never_report_truncation() -> None:
    # 上游不回 usage 时不能瞎猜，否则会把正常轮次拦下来。
    assert service_module._model_output_truncated({}) is False
    assert service_module._model_output_truncated(
        model_call(outputTokens=0, reasoningTokens=0),
    ) is False


@pytest.mark.asyncio
async def test_other_script_artifacts_still_count_after_acceptance(settings) -> None:
    """批准契约后，真正的后续交付物仍然算数。

    排除的只有 define_script_contract 一个——它产出的是被批准的对象本身。
    落脚本候选、跑脚本测试这些都是确认之后该有的产物。
    """
    runtime = service(settings, ScriptedModel([]))
    runtime.store.create_run(run_record("run_artifact", "project_artifact"))
    runtime.store.update_run(
        "run_artifact",
        latest_interaction={
            "kind": "scope_confirmation",
            "actionKey": "accept_contract",
            "requiresArtifact": True,
        },
    )
    record = runtime.store.require_run("run_artifact")
    live = service_module.LiveRun(
        run_id="run_artifact",
        project_id="project_artifact",
        token="token_artifact",
    )
    try:
        assert runtime._script_artifact_produced(live, record) is False

        live.observed_tools.add("stage_script_source")
        assert runtime._script_artifact_produced(live, record) is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_redefining_the_contract_is_not_an_artifact_after_acceptance(settings) -> None:
    """专家批准契约后，再定义一遍契约不算交付物。

    define_script_contract 产出的正是被批准的那个对象本身；把它算作产物，
    等于允许「确认完又原地重定义一次契约」通过检查。
    """
    runtime = service(settings, ScriptedModel([]))
    runtime.store.create_run(run_record("run_redefine", "project_redefine"))
    runtime.store.update_run(
        "run_redefine",
        latest_interaction={
            "kind": "scope_confirmation",
            "actionKey": "accept_contract",
            "requiresArtifact": True,
        },
    )
    record = runtime.store.require_run("run_redefine")
    live = service_module.LiveRun(
        run_id="run_redefine",
        project_id="project_redefine",
        token="token_redefine",
    )
    try:
        live.observed_tools.add("define_script_contract")
        assert runtime._script_artifact_produced(live, record) is False

        live.observed_tools.add("stage_script_source")
        assert runtime._script_artifact_produced(live, record) is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_contract_definition_still_counts_for_other_interactions(settings) -> None:
    """不是「按当前契约继续实现」的交互，新定义契约仍然是正常产物。"""
    runtime = service(settings, ScriptedModel([]))
    runtime.store.create_run(run_record("run_define", "project_define"))
    runtime.store.update_run(
        "run_define",
        latest_interaction={
            "kind": "scope_confirmation",
            "actionKey": "adjust_contract",
            "requiresArtifact": True,
        },
    )
    record = runtime.store.require_run("run_define")
    live = service_module.LiveRun(
        run_id="run_define",
        project_id="project_define",
        token="token_define",
    )
    try:
        live.observed_tools.add("define_script_contract")
        assert runtime._script_artifact_produced(live, record) is True
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_contract_acceptance_with_zero_output_does_not_settle_as_completed(settings) -> None:
    """复现线上现场：契约确认后模型零产出，不得被结算成成功完成。

    现场时间线是——专家回答「按上述契约实现」，模型随即把契约又定义了一遍
    （define_script_contract 成功），之后连续几轮把输出预算全用在推理上，
    一个 token 的正文和工具调用都没产生。修复前那次重定义满足了产物检查，
    空正文恢复走紧凑摘要把 Run 结算成 completed，页面上待办消失、没有下一张
    卡也没有报错。

    这里走完整 _execute 控制流：回答之后那一轮只重定义了契约、别的什么都
    没产生。结论必须是失败，而不是假完成。
    """
    question = {
        "question": "该操作的运行契约已核对，是否按该契约进入实现？",
        "kind": "scope_confirmation",
        "options": [
            {"label": "按上述契约实现", "description": "按已确认契约继续", "value": "accept_contract"},
            {"label": "本轮不生成", "description": "不生成脚本", "value": "skip"},
        ],
        "operation": "demo_operation_alpha",
    }

    class _RedefiningModel(ScriptedModel):
        """回答之后每一轮都只重定义契约，再不产出任何正文。

        紧凑摘要走的是 model.stream(..., max_tokens=...) 这条直调路径，用它把
        「恢复摘要」和「正常轮次」区分开：正常轮次一律零产出（模拟被思维链吃光
        预算），恢复摘要正常出文字。修复前正是这段摘要被当成本轮成果结算掉。
        """

        runtime = None
        run_id = ""

        async def stream(self, *args, **kwargs):
            if "max_tokens" in kwargs:
                yield AssistantMessageChunk(
                    content="已核对当前进展，现有处理记录均已保留。",
                    finish_reason="stop",
                )
                return
            if self.calls >= 1 and self.runtime is not None:
                live = self.runtime._live.get(self.run_id)
                if live is not None:
                    live.observed_tools.add("define_script_contract")
            async for chunk in super().stream(*args, **kwargs):
                yield chunk

    model = _RedefiningModel([[ToolTurn("ask", question)], []], final="")
    runtime = service(settings, model, FakeGateway())
    model.runtime = runtime
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_contract_zero_output",
            token="token_1",
            message="开始确定性脚本建模",
        )
        model.run_id = started["id"]
        waiting = await wait_for_status(runtime, started["id"], {"waiting"})
        interaction = runtime.store.get_interaction(waiting["interactionId"])
        assert interaction is not None

        await runtime.answer_interaction(
            project_id="project_contract_zero_output",
            interaction_id=interaction.id,
            token="token_2",
            answer={"value": "accept_contract", "text": "按上述契约实现"},
        )
        finished = await wait_for_status(runtime, started["id"], {"completed", "failed"})

        assert finished["status"] == "failed", (
            "契约确认后零产出被结算成成功完成，这正是页面上「待办消失无下文」的成因"
        )
        events = runtime.store.events_after("project_contract_zero_output", 0)
        visible = "".join(
            event.payload.get("delta", "") for event in events if event.kind == "message_delta"
        )
        # 必须是「没有形成产物」这条出口，而不是碰巧因为摘要也为空而失败。
        assert "没有形成新的契约、脚本候选或下一张确认卡" in visible
        assert "已核对当前进展" not in visible, "紧凑摘要不该被当成本轮成果发出去"

        snapshot = runtime.store.require_run(started["id"]).latest_interaction
        assert snapshot["actionKey"] == "accept_contract"
        assert snapshot["requiresArtifact"] is True
        assert not any(
            event.kind == "status" and event.payload.get("status") == "completed"
            for event in events
        )
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_truncated_turn_is_retried_instead_of_settling_silently(settings) -> None:
    """被思维链吃光预算的一轮要走截断重试，而不是当成「成功但没话说」。

    第一轮只产生推理、usage 显示推理占满了全部 output token；守卫应当认出
    这是零产出，用更小步的提示词重试，而不是让它掉进自主续跑分支被结算掉。
    """

    class _TruncatingModel(ScriptedModel):
        async def stream(self, *args, **kwargs):
            if "max_tokens" in kwargs:
                yield AssistantMessageChunk(content="已保留当前进展。", finish_reason="stop")
                return
            if self.calls == 0:
                self.calls += 1
                # 推理占满全部输出预算，正文和工具调用一个 token 都没产生。
                # 推理内容保持在 REASONING_EVENT_BATCH_CHARS 以内：这个模型替身
                # 一旦跨过批量推送阈值就会让框架报错，与被测的截断判定无关。
                yield AssistantMessageChunk(reasoning_content="反复推演" * 25, finish_reason="null")
                yield AssistantMessageChunk(
                    content="",
                    finish_reason="length",
                    usage_metadata={
                        "input_tokens": 12_000,
                        "output_tokens": 8_192,
                        "reasoning_tokens": 8_192,
                    },
                )
                return
            async for chunk in super().stream(*args, **kwargs):
                yield chunk

    model = _TruncatingModel([[], []], final="已按最小步骤完成这一步，并记录了处理结果。")
    runtime = service(settings, model, FakeGateway())
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_truncation_retry",
            token="token_1",
            message="继续推进当前阶段",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "suspended"})

        events = runtime.store.events_after("project_truncation_retry", 0)
        truncation_events = [
            event for event in events
            if event.kind == "tool" and event.payload.get("name") == "model_output_truncated"
        ]
        assert truncation_events, "截断轮次没有被识别，仍然走了原来的静默路径"
        assert truncation_events[0].payload["code"] == "MODEL_OUTPUT_TRUNCATED"
        assert truncation_events[0].payload["phase"] == "repairing"
        # 重试用的是压缩思维链的提示词，而不是原样再跑一遍。
        assert any(
            "只做下一步最小的一件事" in str(seen) for seen in model.stream_inputs
        )
    finally:
        await runtime.shutdown()
