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


GOAL_HEADING = "本轮目标（服务端指派）"


def goal_section(text: str) -> str:
    """只取目标那一段。

    system prompt 里还有 SYSTEM_PROMPT 与 SKILL 全文，直接在整段上断言
    「不包含某词」会误命中 skill 正文——比如「项目负责人」在发布说明里就有。
    """
    index = text.find(GOAL_HEADING)
    return text[index:] if index >= 0 else ""


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

        goal = goal_section(goal_texts[0])
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


@pytest.mark.asyncio
async def test_agent_actionable_wins_over_user_facing_blockers(settings) -> None:
    """有 agentActionable 时用它，而不是面向用户的 blockers。

    blockers 说的是「还差什么」，主语常常是用户；助手需要的是「现在该干什么」。
    把「由项目负责人完成发布」指派给助手只会让它困惑或越界。
    """
    model = ScriptedModel(
        [[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []],
        final="本轮已完成。",
    )
    gateway = FakeGateway({
        "gate_status": {
            "pass": False,
            "blockers": ["由项目负责人确认真实用例覆盖和稳定版本号，并完成正式发布"],
            "agentActionable": ["完成业务操作「demo-operation-alpha」的脚本：定义契约、提交源码、绑定"],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_actionable", token="token_1", message="继续",
        )
        await wait_for_status(
            runtime, started["id"], {"completed", "failed", "waiting", "suspended"},
        )
        goals = [t for t in system_texts(model) if GOAL_HEADING in t]
        assert goals, "没有指派目标"
        goal = goal_section(goals[0])
        assert "demo-operation-alpha" in goal
        assert "项目负责人" not in goal, "用了面向用户的 blockers，没用 agentActionable"
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_empty_agent_actionable_means_no_goal(settings) -> None:
    """明确标注为空时不指派，不能退回 blockers。

    空数组的语义是「这道门禁当前没有助手能独立推进的事」，退回 blockers
    等于又把用户的事派给了助手，整改就白做了。
    """
    model = ScriptedModel(
        [[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []],
        final="本轮已完成。",
    )
    gateway = FakeGateway({
        "gate_status": {
            "pass": False,
            "blockers": ["先在评测界面录入至少 1 条用户真实场景用例"],
            "agentActionable": [],
        },
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_no_actionable", token="token_1", message="继续",
        )
        await wait_for_status(
            runtime, started["id"], {"completed", "failed", "waiting", "suspended"},
        )
        # 没有目标段就等于没有指派——不必再去整段搜 blocker 原文，
        # 那些措辞和 skill 正文会重叠，搜出来的是误命中。
        for text in system_texts(model):
            assert GOAL_HEADING not in text
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_missing_field_falls_back_to_blockers(settings) -> None:
    """服务端还没标注这道门禁时，退回 blockers 保持兼容。"""
    model = ScriptedModel(
        [[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []],
        final="本轮已完成。",
    )
    gateway = FakeGateway({
        "gate_status": {"pass": False, "blockers": ["补全业务操作「demo-op」的输入定义"]},
    })
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_fallback", token="token_1", message="继续",
        )
        await wait_for_status(
            runtime, started["id"], {"completed", "failed", "waiting", "suspended"},
        )
        goals = [t for t in system_texts(model) if GOAL_HEADING in t]
        assert goals, "未标注时应退回 blockers"
        assert "demo-op" in goal_section(goals[0])
    finally:
        await runtime.shutdown()
