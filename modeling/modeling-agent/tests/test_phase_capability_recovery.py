from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import pytest

from modeling_agent.rails import PhaseTransitionRail, WriteSafetyRail
from modeling_agent.tools import (
    CORE_TOOL_SPECS,
    PHASE_CAPABILITY_STRATEGY,
    PhaseUnavailableTool,
    ToolContext,
    build_tools,
)

from conftest import FakeGateway


pytestmark = pytest.mark.asyncio


def _spec(name: str):
    return next(item for item in CORE_TOOL_SPECS if item.name == name)


class _TransitionCallback:
    """Replay a transition_project_state result through the rail."""

    def __init__(self, result: dict, arguments: dict | None = None) -> None:
        self.inputs = SimpleNamespace(
            tool_name="transition_project_state",
            tool_args=arguments or {},
            tool_result=json.dumps(result),
        )
        self.finish = None

    def request_force_finish(self, result: dict) -> None:
        self.finish = result


def _context(phase: str) -> tuple[ToolContext, list[str], list[dict]]:
    required: list[str] = []
    events: list[dict] = []
    context = ToolContext(
        project_id="project_phase_capability",
        run_id="run_phase_capability",
        token="token",
        gateway=FakeGateway({}),
        emit=lambda kind, payload: events.append({"kind": kind, **payload}),
        phase=phase,
    )
    context.require_tool = lambda name, recovery: required.append(name)
    context.release_required_tool = lambda name: required.remove(name) if name in required else None
    return context, required, events


async def test_out_of_phase_repair_write_redirects_to_the_owning_phase() -> None:
    context, required, _events = _context("evaluation")
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)

    result = json.loads(await tool.invoke({"operation_key": "demo_operation"}))

    assert result["error"] == "TOOL_PHASE_UNAVAILABLE"
    assert result["requiredPhase"] == "validation"
    assert result["issueStatus"] == "repairing"
    assert result["repairAttempt"] == 1
    # 自动恢复先接手：切阶段是系统自己能做的事，不进用户视线。
    assert result["problemVisibility"] == "activity"
    assert required == ["transition_project_state"]
    assert "transition_project_state" in result["instruction"]
    # 助手不得把阶段边界讲成平台缺陷，也不得让用户等待。
    assert "不得向用户声称写入能力不可用" in result["instruction"]
    assert context.gateway.calls == []


async def test_repeated_out_of_phase_writes_hand_the_user_an_actionable_choice() -> None:
    context, _required, _events = _context("evaluation")
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)

    attempts = [
        json.loads(await tool.invoke({"operation_key": "demo_operation"}))
        for _ in range(3)
    ]

    assert [item["repairAttempt"] for item in attempts] == [1, 2, 2]
    stopped = attempts[-1]
    assert stopped["issueStatus"] == "awaiting_user"
    assert stopped["repairStopped"] is True
    assert stopped["tracePhase"] == "waiting"
    assert stopped["problemVisibility"] == "recovery"
    assert [action["id"] for action in stopped["recoveryActions"]] == [
        "retry_from_latest", "keep_current_progress",
    ]
    assert "确定性验证" in stopped["customerSummary"]
    assert context.recovery_waiting is True


async def test_cancelled_run_leaves_no_phase_capability_issue_behind() -> None:
    context, required, _events = _context("evaluation")
    context.cancel_event = asyncio.Event()
    context.cancel_event.set()
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)

    with pytest.raises(asyncio.CancelledError):
        await tool.invoke({"operation_key": "demo_operation"})

    # 用户已经停掉了这一跳。越过取消点还把 issue 记下来的话，这次运行下次被恢复时
    # 仍会弹出他刚刚取消掉的那张恢复卡。
    assert required == []
    assert context.technical_recoveries == {}
    assert context.recovery_waiting is False


async def test_phase_capability_issue_is_closed_once_the_phase_carries_it() -> None:
    context, required, _events = _context("evaluation")
    resolved: list[list[str]] = []
    context.resolve_issues = resolved.append
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)
    await tool.invoke({"operation_key": "demo_operation"})

    issue_id = next(iter(context.technical_recoveries))
    assert context.technical_recoveries[issue_id]["strategy"] == PHASE_CAPABILITY_STRATEGY
    assert context.technical_recoveries[issue_id]["requiredPhase"] == "validation"

    class Callback:
        def __init__(self, result: dict) -> None:
            self.inputs = SimpleNamespace(
                tool_name="transition_project_state",
                tool_result=json.dumps(result),
            )
            self.finish = None

        def request_force_finish(self, result: dict) -> None:
            self.finish = result

    callback = Callback({"phase": "validation", "revision": 7})
    await PhaseTransitionRail(context).after_tool_call(callback)  # type: ignore[arg-type]

    assert context.phase == "validation"
    assert context.technical_recoveries == {}
    assert resolved == [[issue_id]]
    assert required == []


async def test_transition_to_another_phase_keeps_the_issue_open() -> None:
    context, _required, _events = _context("evaluation")
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)
    await tool.invoke({"operation_key": "demo_operation"})

    class Callback:
        def __init__(self, result: dict) -> None:
            self.inputs = SimpleNamespace(
                tool_name="transition_project_state",
                tool_result=json.dumps(result),
            )
            self.finish = None

        def request_force_finish(self, result: dict) -> None:
            self.finish = result

    await PhaseTransitionRail(context).after_tool_call(  # type: ignore[arg-type]
        Callback({"phase": "knowledge", "revision": 7}),
    )

    assert context.technical_recoveries != {}


async def test_whole_repair_chain_shares_one_recovery_budget() -> None:
    # 恢复预算按工具名分账时，模型在同一条受控链路里换个工具重试就能各拿一份预算，
    # 用户要看着十几次失败才等到出口。同一个承载阶段上的写入必须共用一条 issue。
    context, _required, _events = _context("evaluation")
    contract = PhaseUnavailableTool(_spec("define_script_contract"), "validation", context)
    patch = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)

    first = json.loads(await contract.invoke({"operation_key": "demo_operation"}))
    second = json.loads(await patch.invoke({"operation_key": "demo_operation"}))
    third = json.loads(await patch.invoke({"operation_key": "demo_operation"}))

    assert first["issueId"] == second["issueId"] == third["issueId"]
    assert list(context.technical_recoveries) == [first["issueId"]]
    assert [item["repairAttempt"] for item in (first, second, third)] == [1, 2, 2]
    assert third["issueStatus"] == "awaiting_user"


async def test_unreachable_host_phase_stops_instead_of_burning_the_budget() -> None:
    # 承载阶段一跳走不通时，再让模型去试只是把预算烧在死路上，用户点「继续」
    # 还会回到同一个循环。这种情况要立刻给出可操作的选择。
    context, required, _events = _context("goal")
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)

    result = json.loads(await tool.invoke({"operation_key": "demo_operation"}))

    assert result["issueStatus"] == "awaiting_user"
    assert result["repairAttempt"] == 0
    assert result["problemVisibility"] == "recovery"
    assert [action["id"] for action in result["recoveryActions"]] == [
        "retry_from_latest", "keep_current_progress",
    ]
    assert "get_project_state" in result["instruction"]
    assert "不得向用户声称写入能力不可用" in result["instruction"]
    # 转换本身走不通，就不该再把 transition_project_state 挂成必需工具。
    assert required == []
    assert context.recovery_waiting is True


async def test_refused_transition_turns_the_issue_into_a_user_choice() -> None:
    # 门禁挡住承载阶段时这条 issue 已经没有自愈路径了。继续放模型回去重提写入
    # 只会把剩余预算烧完，所以要立刻标成 blocked 并带上真正的未满足项。
    context, required, _events = _context("evaluation")
    saved: list[tuple[str, dict]] = []
    context.save_recovery = lambda issue_id, recovery: saved.append((issue_id, recovery))
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)
    await tool.invoke({"operation_key": "demo_operation"})
    issue_id = next(iter(context.technical_recoveries))
    assert required == ["transition_project_state"]

    await PhaseTransitionRail(context).after_tool_call(  # type: ignore[arg-type]
        _TransitionCallback(
            {
                "error": "还不能进入“确定性验证”",
                "code": "STATE_GATE_BLOCKED",
                "blockers": ["先完成确定性计算范围确认"],
            },
            {"to_phase": "validation"},
        ),
    )

    recovery = context.technical_recoveries[issue_id]
    assert recovery["blocked"] is True
    assert recovery["blockers"] == ["先完成确定性计算范围确认"]
    assert recovery["exhausted"] is True
    assert "先完成确定性计算范围确认" in recovery["stoppedSummary"]
    assert context.recovery_waiting is True
    assert required == []
    assert [item[0] for item in saved] == [issue_id]

    # 模型在同一轮里再撞一次同样的边界，也不能把这份判定和未满足项重置掉。
    retry = json.loads(await tool.invoke({"operation_key": "demo_operation"}))
    assert retry["issueStatus"] == "awaiting_user"
    assert context.technical_recoveries[issue_id]["blocked"] is True
    assert context.technical_recoveries[issue_id]["blockers"] == ["先完成确定性计算范围确认"]


async def test_transition_error_without_a_blocked_issue_changes_nothing() -> None:
    context, _required, _events = _context("evaluation")

    await PhaseTransitionRail(context).after_tool_call(  # type: ignore[arg-type]
        _TransitionCallback(
            {"error": "还不能进入“确定性验证”", "blockers": ["先完成确定性计算范围确认"]},
            {"to_phase": "validation"},
        ),
    )

    assert context.phase == "evaluation"
    assert context.technical_recoveries == {}


async def test_any_host_phase_closes_the_issue() -> None:
    # 一条链路可能有多个承载阶段，模型转到其中任何一个都算这条 issue 解决了；
    # 只认 requiredPhase 会把已经能落盘的写入继续挂在未完成恢复上。
    context, required, _events = _context("evaluation")
    resolved: list[list[str]] = []
    context.resolve_issues = resolved.append
    tool = PhaseUnavailableTool(_spec("stage_script_patch"), "validation", context)
    await tool.invoke({"operation_key": "demo_operation"})
    issue_id = next(iter(context.technical_recoveries))

    await PhaseTransitionRail(context).after_tool_call(  # type: ignore[arg-type]
        _TransitionCallback({"phase": "increment", "revision": 7}, {"to_phase": "increment"}),
    )

    assert context.phase == "increment"
    assert context.technical_recoveries == {}
    assert resolved == [[issue_id]]
    assert required == []


async def test_evaluation_tool_surface_answers_every_repair_write() -> None:
    context, _required, _events = _context("evaluation")

    names = {tool.card.name for tool in build_tools(context)}

    assert {"stage_script_patch", "get_script_edit_context", "run_script_test"} <= names
    assert {"create_change_plan", "stage_change_set", "validate_change_set"} <= names
    assert {"list_domain_resources", "get_domain_resource"} <= names
    assert len(names) == len(build_tools(context))


def _write_call(name: str, arguments: dict) -> SimpleNamespace:
    return SimpleNamespace(
        inputs=SimpleNamespace(
            tool_name=name,
            tool_args=arguments,
            tool_call=SimpleNamespace(id=f"call_{name}"),
            tool_result=None,
            tool_msg=None,
        ),
        extra={},
    )


async def _rail_context(phase: str) -> tuple[ToolContext, list[str]]:
    context, required, _events = _context(phase)

    async def state_action(_name: str, _arguments: dict) -> dict:
        return {"phase": phase, "revision": 3}

    context.state_action = state_action
    return context, required


async def test_write_boundary_redirects_the_repair_write_instead_of_waiting() -> None:
    # 工具注册后写入边界门禁会先接住这次调用，它必须给出同一份可自愈的出口，
    # 否则模型仍会收到「先完成当前阶段」这种需要用户动手的死结。
    context, required = await _rail_context("evaluation")
    call = _write_call("stage_script_patch", {"operation_key": "demo_operation"})

    await WriteSafetyRail(context).before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert call.extra["_skip_tool"] is True
    assert result["error"] == "TOOL_PHASE_UNAVAILABLE"
    assert result["requiredPhase"] == "validation"
    assert result["issueStatus"] == "repairing"
    assert required == ["transition_project_state"]
    assert "transition_project_state" in result["instruction"]


async def test_write_boundary_leaves_in_phase_repair_writes_alone() -> None:
    context, required = await _rail_context("validation")
    call = _write_call("stage_script_patch", {"operation_key": "demo_operation"})

    await WriteSafetyRail(context).before_tool_call(call)  # type: ignore[arg-type]

    assert call.inputs.tool_result is None
    assert required == []


async def test_write_boundary_keeps_the_phase_gate_for_plain_domain_writes() -> None:
    context, required = await _rail_context("evaluation")
    call = _write_call("create_object", {"key": "demo_object", "title": "示例对象"})

    await WriteSafetyRail(context).before_tool_call(call)  # type: ignore[arg-type]

    result = json.loads(call.inputs.tool_result)
    assert result["error"] == "WRITE_PHASE_MISMATCH"
    assert required == []
