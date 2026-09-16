"""阶段方法由服务端按阶段注入（C2）。

该读哪本手册是系统事实，由代码根据阶段选，不靠模型自觉调 list_skill；
注入 system 段后享受防驱逐待遇，长阶段中途方法不再被工具返回值挤出窗口。
情形形方法（package-merge、failure-repair）由事件而非阶段触发，保留
list_skill 按需加载，不自动注入。
"""

from __future__ import annotations

import asyncio

import pytest

from modeling_agent.agent import PHASE_SKILLS, _system_prompt

from conftest import FakeGateway, ScriptedModel, ToolTurn, service


METHOD_HEADING = "本阶段方法（服务端按当前阶段提供）"


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


def test_phase_skill_map_covers_phase_shaped_skills_only() -> None:
    assert PHASE_SKILLS == {
        "intake": "material-analysis",
        "goal": "material-analysis",
        "process": "process-modeling",
        "knowledge": "knowledge-extraction",
        "increment": "incremental-intake",
    }
    # 情形形方法由事件触发，不映射到任何阶段
    assert "package-merge" not in PHASE_SKILLS.values()
    assert "failure-repair" not in PHASE_SKILLS.values()
    # 评测/发布阶段没有阶段方法
    assert "evaluation" not in PHASE_SKILLS
    assert "release" not in PHASE_SKILLS


def test_system_prompt_renders_phase_method_slot() -> None:
    prompt = _system_prompt("技能正文", {"validatorVersion": "v1"}, phase_method="阶段方法正文")
    assert METHOD_HEADING in prompt
    assert "阶段方法正文" in prompt

    without = _system_prompt("技能正文", {"validatorVersion": "v1"})
    assert METHOD_HEADING not in without


@pytest.mark.asyncio
async def test_intake_run_gets_material_analysis_injected(settings) -> None:
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        started = await runtime.start_run(
            project_id="project_phase_skill_intake", token="token_1", message="开始建模",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert METHOD_HEADING in text
            assert "# 资料分析" in text
            # 情形形方法不自动注入
            assert "# 知识包融合" not in text
            assert "# 评测失败修复" not in text
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
async def test_increment_run_gets_incremental_intake_injected(settings) -> None:
    model = ScriptedModel([[ToolTurn("update_work_notes", {"markdown": "# 进展"})], []], final="完成。")
    runtime = service(settings, model, FakeGateway({"gate_status": {"pass": True, "blockers": []}}))
    await runtime.startup()
    try:
        enter_increment(runtime, "project_phase_skill_increment")
        started = await runtime.start_run(
            project_id="project_phase_skill_increment", token="token_1", message="继续",
        )
        await wait_for_status(runtime, started["id"], {"completed", "failed", "waiting", "suspended"})
        texts = system_texts(model)
        assert texts
        for text in texts:
            assert METHOD_HEADING in text
            assert "# 增量资料" in text
            assert "# 资料分析" not in text
    finally:
        await runtime.shutdown()
