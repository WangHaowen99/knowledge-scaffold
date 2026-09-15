"""自主续跑时，服务端为这一轮指派单一目标。

线上现场：用户只说了「继续」，助手跑了四个多小时、调用工具一千余次，其中
97.8% 是读取，脚本源码一行没提交。它每一轮收到的指令是「立即继续执行能自行
完成的下一步」——没有具体内容、没有完成判据，而「再读一份资料」永远算得上
可执行动作，所以这一轮在逻辑上不会自然结束。

门禁 blockers 本来就是服务端算出来的任务清单，过去只作为 get_project_state
的一个字段返回，和文件正文一样被后续读取挤出窗口。这里把清单的第一项提升为
本轮指派目标，放进 system 段常驻。
"""

from __future__ import annotations

import asyncio

import pytest

from modeling_agent.agent import _system_prompt

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


async def wait_for_status(runtime, run_id: str, statuses: set[str], timeout: float = 15) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.store.require_run(run_id).public()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def system_texts(model) -> list[str]:
    """取出每次模型调用的 system 段正文。

    框架以关键字参数调用 stream，ScriptedModel 已经把整个 kwargs 记进
    stream_inputs，这里从中取 messages。
    """
    out = []
    for item in model.stream_inputs:
        messages = item.get("messages") if isinstance(item, dict) else item
        text = ""
        for message in messages or []:
            role = getattr(message, "role", None) or (
                message.get("role") if isinstance(message, dict) else None
            )
            if role != "system":
                continue
            content = getattr(message, "content", None) or (
                message.get("content") if isinstance(message, dict) else ""
            )
            text += str(content or "")
        out.append(text)
    return out


def test_system_prompt_carries_turn_goal() -> None:
    prompt = _system_prompt("技能正文", {"validatorVersion": "v1"}, "完成某操作的脚本实现")

    assert "本轮目标（服务端指派）" in prompt
    assert "完成某操作的脚本实现" in prompt


def test_system_prompt_without_goal_is_unchanged() -> None:
    # 没有目标时不能凭空多出一段标题，否则模型会以为目标丢了。
    assert "本轮目标" not in _system_prompt("技能正文", {"validatorVersion": "v1"})
    assert "本轮目标" not in _system_prompt("技能正文", {"validatorVersion": "v1"}, "   ")


@pytest.mark.asyncio
async def test_autonomous_continuation_receives_a_single_assigned_goal(settings) -> None:
    """模型自己决定下一步时，必须收到服务端指派的单一目标。"""
    model = ScriptedModel(
        [
            [ToolTurn("update_work_notes", {"markdown": "# 进展\n已核对资料"})],
            [],
        ],
        final="本轮已完成。",
    )
    gateway = FakeGateway({
        "gate_status": {
            "pass": False,
            "blockers": [
                "完成业务操作「demo-operation-alpha」的脚本契约、实现和绑定",
                "完成业务操作「demo-operation-beta」的脚本契约、实现和绑定",
            ],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_turn_goal",
            token="token_1",
            message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})

        texts = system_texts(model)
        assert len(texts) >= 2, "模型没有被调用足够次数，无法验证目标指派"

        # 首轮：用户刚下达指令，指令本身就是目标，服务端不再指派
        assert "本轮目标（服务端指派）" not in texts[0]

        # 自主续跑轮：必须收到来自门禁 blockers 的单一目标。
        # 一次 _run_model_operation 内部会有多次模型调用，所以按内容定位而不是按序号。
        goal_texts = [text for text in texts if "本轮目标（服务端指派）" in text]
        assert goal_texts, "自主续跑轮没有收到服务端指派的目标"

        goal = goal_texts[0]
        assert "demo-operation-alpha" in goal
        # 只指派第一项，不把整张清单倒给模型
        assert "demo-operation-beta" not in goal
        # 必须带上边界与卡住时的出口，否则它又会自己另找事做
        assert "本轮只推进这一项" in goal
        assert "调用 ask" in goal
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_passed_gate_produces_no_goal(settings) -> None:
    """门禁已经通过时不指派目标，避免给出已完成的事项。"""
    model = ScriptedModel(
        [[ToolTurn("update_work_notes", {"markdown": "# 进展\n已核对资料"})], []],
        final="本轮已完成。",
    )
    gateway = FakeGateway({"gate_status": {"pass": True, "blockers": []}})
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_turn_goal_passed",
            token="token_1",
            message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})

        for text in system_texts(model):
            assert "本轮目标（服务端指派）" not in text
    finally:
        await runtime.shutdown()
