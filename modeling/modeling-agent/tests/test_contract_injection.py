"""契约跟着它管的工具走（C1）。

生成契约管 create/upsert/patch 等写入面的产物合法性，而这批工具已按阶段
收敛。判据直接耦合：本阶段暴露的工具集与契约管辖集有交集，契约就注入；
无交集（如 intake），几 KB 的契约 JSON 就是纯负重。契约和工具走同一个
开关，机制上不可能出现「用得上却不在场」。
"""

from __future__ import annotations

import asyncio

import pytest

from modeling_agent.agent import _system_prompt
from modeling_agent.tools import (
    CONTRACT_GOVERNED_TOOLS,
    contract_governed_tool_exposed,
    core_tool_specs_for_phase,
)

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


ALL_PHASES = ("intake", "goal", "process", "knowledge", "increment", "validation", "evaluation", "release")

PLACEHOLDER = "当前阶段无生成类工具，生成契约将在进入生成相关阶段时自动提供"


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


def test_contract_exposure_matches_phase_expectations() -> None:
    """早期阶段无管辖工具、生成相关阶段有——这是 C1 的全部行为面。"""
    expected = {
        "intake": False,
        "goal": False,
        "process": False,
        "knowledge": True,
        "increment": True,
        "validation": True,
        "evaluation": False,
        "release": False,
    }
    for phase, exposure in expected.items():
        assert contract_governed_tool_exposed(phase) is exposure, phase
    # 空阶段保留全量工具面（旧测试与恢复调用方），契约必须在场
    assert contract_governed_tool_exposed("") is True


def test_recovery_required_tool_brings_the_contract_back() -> None:
    """恢复性放行把管辖工具带进早期阶段时，契约必须跟着出现——
    按实际暴露算，不按阶段名猜。（upsert_* 是 legacy 别名，非 legacy 档
    永远不暴露，所以用 stage_change_set 验证。）"""
    assert contract_governed_tool_exposed("intake") is False
    assert contract_governed_tool_exposed("intake", frozenset({"stage_change_set"})) is True


def test_exposure_property_holds_for_every_phase() -> None:
    """性质断言：任意阶段，「暴露了管辖集内工具 ⇔ 判定为需要契约」恒成立。"""
    for phase in ALL_PHASES:
        exposed_names = {spec.name for spec in core_tool_specs_for_phase(phase)}
        assert contract_governed_tool_exposed(phase) == bool(
            exposed_names & CONTRACT_GOVERNED_TOOLS
        ), phase


def test_system_prompt_contract_section_follows_the_flag() -> None:
    contract = {"validatorVersion": "package-format-v1"}
    active = _system_prompt("技能正文", contract, contract_active=True)
    assert '"validatorVersion": "package-format-v1"' in active
    assert PLACEHOLDER not in active

    inactive = _system_prompt("技能正文", contract, contract_active=False)
    # SYSTEM_PROMPT 正文提到 validatorVersion 字样，按 JSON 形态断言契约本体
    assert '"validatorVersion"' not in inactive
    # 留一行占位而不是完全删除，避免模型在早期阶段虚构契约概念
    assert PLACEHOLDER in inactive
    assert "本轮知识包生成契约" in inactive


@pytest.mark.asyncio
async def test_intake_run_receives_placeholder_instead_of_contract(settings) -> None:
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_contract_intake", token="token_1", message="开始建模",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert PLACEHOLDER in text
            assert '"validatorVersion"' not in text
    finally:
        await runtime.shutdown()


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


@pytest.mark.asyncio
async def test_increment_run_receives_the_full_contract(settings) -> None:
    """生成相关阶段契约完整注入——瘦身红线：缺指令是急性事故。"""
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    gateway = FakeGateway({"gate_status": {"pass": True, "blockers": []}})
    runtime = service(settings, model, gateway)
    await runtime.startup()
    try:
        enter_increment(runtime, "project_contract_increment")
        started = await runtime.start_run(
            project_id="project_contract_increment", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert '"validatorVersion"' in text
            assert PLACEHOLDER not in text
    finally:
        await runtime.shutdown()
