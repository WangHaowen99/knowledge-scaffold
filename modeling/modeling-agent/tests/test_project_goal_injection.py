"""开始建模前确认过的创建目标注入系统提示词。

目标由核心服务在 claim_candidate_workspace 里随 projectGoal 返回（未确认或
存量项目为空串）。它只在目标访谈相关的阶段注入：goal.md 定稿后由 goal.md
接替成为事实源，后续阶段不再常驻这段原文。
"""

from __future__ import annotations

import asyncio

import pytest

from modeling_agent.agent import PROJECT_GOAL_PHASES, _system_prompt

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


GOAL_HEADING = "# 用户确认的初始建模目标"
# 合成目标，长度超过 200 字；两条流程分别在开头和结尾，用来确认原文没有被截短
LONG_GOAL = (
    "整理合成仓储场景的库存知识包。流程甲：入库登记与质检。"
    + "补充说明：只覆盖示例仓库的标准作业。" * 12
    + "流程乙：出库复核与盘点。"
)


async def wait_for_status(runtime, run_id: str, statuses: set[str], timeout: float = 20) -> dict:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        run = runtime.store.require_run(run_id).public()
        if run["status"] in statuses:
            return run
        await asyncio.sleep(0.02)
    raise AssertionError(f"run {run_id} did not reach {statuses}")


def system_texts(model) -> list[str]:
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


def gateway(project_goal: str) -> FakeGateway:
    return FakeGateway({
        "claim_candidate_workspace": {
            "ok": True,
            "projectGoal": project_goal,
            "display": {"text": "已准备本次修改"},
        },
        "gate_status": {"pass": True, "blockers": []},
    })


def enter_increment(runtime, project_id: str) -> None:
    initial = runtime.store.project_state(project_id)
    released = runtime.store.transition_project_state(
        project_id,
        expected_revision=initial.revision,
        next_state="release:completed",
        cause="test_released",
    )
    assert released.state == "release:completed"
    started, _created = runtime.store.start_increment_round(
        project_id,
        baseline_revision="sha256:released-1",
        package_version="1.0.0",
    )
    assert started.state == "increment:idle"


def test_project_goal_is_injected_only_in_goal_interview_phases() -> None:
    assert PROJECT_GOAL_PHASES == {"intake", "goal"}


def test_system_prompt_places_goal_between_phase_method_and_constraints() -> None:
    prompt = _system_prompt(
        "技能正文",
        {"validatorVersion": "v1"},
        phase_method="阶段方法正文",
        project_goal="流程甲：入库登记\n\n流程乙：出库复核\n",
        constraints=({"id": "c1", "text": "合成约束"},),
    )

    assert GOAL_HEADING in prompt
    assert "> 流程甲：入库登记\n>\n> 流程乙：出库复核\n" in prompt
    assert "user-decision:开始建模前确认的初始目标" in prompt
    method = prompt.index("# 本阶段方法")
    goal = prompt.index(GOAL_HEADING)
    constraints = prompt.index("# 专家约束")
    assert method < goal < constraints


@pytest.mark.parametrize("project_goal", ["", "  \n "])
def test_system_prompt_omits_goal_section_without_goal(project_goal: str) -> None:
    prompt = _system_prompt("技能正文", {"validatorVersion": "v1"}, project_goal=project_goal)

    assert GOAL_HEADING not in prompt


@pytest.mark.asyncio
async def test_intake_run_gets_confirmed_goal_verbatim(settings) -> None:
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, gateway(LONG_GOAL))
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_goal_intake", token="token_1", message="开始建模",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert GOAL_HEADING in text
            assert "> 整理合成仓储场景的库存知识包。流程甲：入库登记与质检。" in text
            assert "流程乙：出库复核与盘点。" in text
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_intake_run_without_confirmed_goal_has_no_goal_section(settings) -> None:
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, gateway(""))
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_goal_absent", token="token_1", message="开始建模",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert GOAL_HEADING not in text
    finally:
        await runtime.shutdown()


@pytest.mark.asyncio
async def test_increment_run_does_not_carry_creation_goal(settings) -> None:
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, gateway(LONG_GOAL))
    await runtime.startup()
    try:
        enter_increment(runtime, "project_goal_increment")
        started = await runtime.start_run(
            project_id="project_goal_increment", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert GOAL_HEADING not in text
    finally:
        await runtime.shutdown()
