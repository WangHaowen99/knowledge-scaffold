"""启动自动恢复的韧性：就绪门、瞬时错误分类、失败留因与进程内对账重试。

对应 cs 实测问题：agent 容器早于建模核心就绪，一整批 run 的自动恢复在一秒内
撞上「停止栅栏暂时不可用」(CORE_CANCELLATION_UNAVAILABLE / 503) 后被一次性标记
resume_failed，且此后不再重试，导致项目卡死到下一次服务重启。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from modeling_agent.errors import AgentRuntimeError
from modeling_agent.schemas import RunRecord
from modeling_agent.service import (
    _is_transient_core_error,
    RECOVERY_RESUME_FAILED,
)


def test_transient_classification_covers_cancellation_fence_and_5xx() -> None:
    # 停止栅栏这类发布分支新增的 503 码必须被当作瞬时错误，否则退避重试对它失明。
    assert _is_transient_core_error(
        AgentRuntimeError("CORE_CANCELLATION_UNAVAILABLE", "停止栅栏暂时不可用", status=503)
    )
    assert _is_transient_core_error(AgentRuntimeError("CORE_UNAVAILABLE", "核心不可用", status=503))
    assert _is_transient_core_error(AgentRuntimeError("SOME_NEW_CODE", "网关错误", status=502))
    # 业务型 400 不是瞬时错误，不应触发对账重试。
    assert not _is_transient_core_error(
        AgentRuntimeError("GOAL_SECTION_UNKNOWN_FIELD", "未知字段", status=400)
    )
    assert not _is_transient_core_error(RuntimeError("普通异常"))


@pytest.mark.asyncio
async def test_resume_failure_records_detail_and_reconciles(tmp_path: Path, monkeypatch) -> None:
    """瞬时失败后：留因入库 + 进程内对账重试，重试成功则清除 resume_failed。"""
    import modeling_agent.service as service_module
    from tests.conftest import FakeGateway, ScriptedModel, service as build_service

    # 把两层重试延迟都压到近乎立即，避免测试真的等待。
    monkeypatch.setattr(service_module, "RECOVERY_RETRY_DELAYS", (0.0, 0.0, 0.0, 0.0))
    monkeypatch.setattr(service_module, "RECOVERY_RECONCILE_DELAYS", (0.01, 0.02, 0.03))

    settings = service_module.Settings(
        data_dir=tmp_path / "agent-data",
        core_base_url="http://127.0.0.1:5100",
        service_token="s" * 32,
        llm_base_url="http://127.0.0.1:9/unused",
        llm_api_key="unused",
        llm_model="scripted",
        llm_timeout_seconds=5,
        llm_max_tokens=4096,
        max_iterations=20,
        host="127.0.0.1",
        port=5110,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
    )
    runtime = build_service(settings, ScriptedModel([]), FakeGateway())

    project_id = "project_recovery"
    run_id = "run_recovery"
    runtime.store.create_run(
        RunRecord(
            id=run_id,
            project_id=project_id,
            session_id=f"project:{project_id}",
            status=service_module.RUN_RUNNING,
            prompt="建模",
            token_fingerprint="0" * 12,
            run_credential="cred-1",
        )
    )
    runtime.store.update_run(run_id, status=service_module.RUN_RUNNING)

    run_record = runtime.store.get_run(run_id)
    assert run_record is not None

    # 首轮的内层重试会尝试 len(RECOVERY_RETRY_DELAYS)+1 = 5 次，全部瞬时失败后
    # 才标记 resume_failed；随后进程内对账层重试并成功。
    calls = {"n": 0}
    INNER_ATTEMPTS = len(service_module.RECOVERY_RETRY_DELAYS) + 1

    async def fake_resume(run, prompt, answer, *, preserve_token_fingerprint=False):  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] <= INNER_ATTEMPTS:
            raise AgentRuntimeError(
                "CORE_CANCELLATION_UNAVAILABLE", "建模核心停止栅栏暂时不可用", status=503
            )
        runtime.store.mark_recovery_status(
            project_id, service_module.RECOVERY_NONE, error="", cause="recovery_resumed"
        )

    monkeypatch.setattr(runtime, "_checkpoint_available", lambda: True)
    monkeypatch.setattr(runtime, "_resume", fake_resume)

    await runtime._auto_resume_recovered(run_record)

    # 首轮失败后立即可见：留因写进状态 error。
    state = runtime.store.project_state(project_id)
    assert state.recovery_status == RECOVERY_RESUME_FAILED
    assert "CORE_CANCELLATION_UNAVAILABLE" in (state.error or "")

    # 等待进程内对账任务重试成功。
    for task in list(runtime._recovery_tasks):
        await task

    state = runtime.store.project_state(project_id)
    assert state.recovery_status == service_module.RECOVERY_NONE
    assert calls["n"] > INNER_ATTEMPTS

    await runtime.shutdown()


@pytest.mark.asyncio
async def test_non_transient_failure_does_not_reconcile(tmp_path: Path, monkeypatch) -> None:
    """业务型失败只留因、不进对账队列，避免无意义重试。"""
    import modeling_agent.service as service_module
    from tests.conftest import FakeGateway, ScriptedModel, service as build_service

    settings = service_module.Settings(
        data_dir=tmp_path / "agent-data",
        core_base_url="http://127.0.0.1:5100",
        service_token="s" * 32,
        llm_base_url="http://127.0.0.1:9/unused",
        llm_api_key="unused",
        llm_model="scripted",
        llm_timeout_seconds=5,
        llm_max_tokens=4096,
        max_iterations=20,
        host="127.0.0.1",
        port=5110,
        skills_dir=Path(__file__).resolve().parents[1] / "skills",
    )
    runtime = build_service(settings, ScriptedModel([]), FakeGateway())

    project_id = "project_recovery_hard"
    run_id = "run_recovery_hard"
    runtime.store.create_run(
        RunRecord(
            id=run_id,
            project_id=project_id,
            session_id=f"project:{project_id}",
            status=service_module.RUN_RUNNING,
            prompt="建模",
            token_fingerprint="0" * 12,
            run_credential="cred-1",
        )
    )
    runtime.store.update_run(run_id, status=service_module.RUN_RUNNING)
    run_record = runtime.store.get_run(run_id)
    assert run_record is not None

    async def always_fail(run, prompt, answer, *, preserve_token_fingerprint=False):  # noqa: ANN001
        raise AgentRuntimeError("KNOWLEDGE_PACKAGE_INVALID", "知识包说明不正确", status=422)

    monkeypatch.setattr(runtime, "_checkpoint_available", lambda: True)
    monkeypatch.setattr(runtime, "_resume", always_fail)

    await runtime._auto_resume_recovered(run_record)

    state = runtime.store.project_state(project_id)
    assert state.recovery_status == RECOVERY_RESUME_FAILED
    assert "KNOWLEDGE_PACKAGE_INVALID" in (state.error or "")
    # 非瞬时错误不排对账任务。
    assert not runtime._recovery_tasks

    await runtime.shutdown()
