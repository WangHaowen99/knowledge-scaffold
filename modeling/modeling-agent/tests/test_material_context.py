"""资料档案查询工具的规格与暴露面（方案 E：资料档案与检索优先）。

行为测试在 TS 侧（tests/material-context.test.ts，core-service 持有三层落库数据）；
这里只守规格：工具存在、全部阶段可用（读取类不受阶段收敛限制）、描述里带
锚点核对纪律、子代理分析员也拿得到它——它们才是重读原文的大户。
"""

from __future__ import annotations

from modeling_agent.tools import (
    TOOL_WORK_DETAILS,
    core_tool_specs_for_phase,
    tool_spec,
)


ALL_PHASES = ("intake", "goal", "process", "knowledge", "increment", "validation", "evaluation", "release")


def test_material_context_spec_exists_with_anchor_discipline() -> None:
    spec = tool_spec("get_material_context")
    assert spec is not None
    # 档案是缓存不是事实源：锚点核对纪律必须写进工具描述本身
    assert "核对原文" in spec.description
    assert "stale" in spec.description
    assert set(spec.properties) == {"document", "topic", "kind"}
    assert spec.required == ()
    assert "get_material_context" in TOOL_WORK_DETAILS


def test_material_context_exposed_in_every_phase() -> None:
    """读取类工具不进任何阶段收敛集：后期阶段正是重复读原文的重灾区，
    档案查询必须在所有阶段都可用。"""
    for phase in ALL_PHASES:
        names = {spec.name for spec in core_tool_specs_for_phase(phase)}
        assert "get_material_context" in names, phase
    assert "get_material_context" in {spec.name for spec in core_tool_specs_for_phase("")}
