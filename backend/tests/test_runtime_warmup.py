"""Tests for the one-shot runtime warm-up launcher."""

import threading
import time

import pytest

import agentic.warmup as warmup
from agentic.config.settings import AgentConfig


SECRET_TEST_KEY = "MDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDAwMDA="


def _reset_warmup_state():
    """Reset module-level warm-up state for test isolation."""
    with warmup._lock:
        warmup._status = warmup.RuntimeWarmupStatus(
            enabled=False,
            state=warmup._WARMUP_IDLE,
        )


def _wait_for_terminal_state(timeout=5.0):
    """Poll until warm-up reaches a terminal state or timeout."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        status = warmup.get_runtime_warmup_status()
        if status.state in warmup._TERMINAL_STATES:
            return status
        time.sleep(0.01)
    return warmup.get_runtime_warmup_status()


@pytest.fixture(autouse=True)
def reset_warmup():
    _reset_warmup_state()
    yield
    _reset_warmup_state()


def _agent_config(enabled: bool) -> AgentConfig:
    return AgentConfig(
        llm_connection_max_retries=3,
        main_agent_max_iterations=50,
        sub_agent_max_iterations=5,
        sub_agent_max_concurrency=4,
        sub_agent_max_depth=2,
        parallel_default_max_workers=10,
        context_max_rounds=10,
        context_prune_tools=set(),
        runtime_warmup_enabled=enabled,
    )


def test_warmup_disabled_when_config_is_false():
    status = warmup.start_runtime_warmup(
        agent_config=_agent_config(False)
    )

    assert status.enabled is False
    assert status.state == "disabled"


def test_warmup_starts_once_when_config_is_true(monkeypatch):
    call_count = {"value": 0}
    started = threading.Event()
    release = threading.Event()

    def fake_warm_up(*, agent_config=None):
        call_count["value"] += 1
        started.set()
        release.wait(timeout=2.0)
        return {"duration_ms": 10, "mode": "jiuwen-runtime"}

    monkeypatch.setattr(
        "agentic.loop.jiuwen_adapter.warm_up_jiuwen_runtime",
        fake_warm_up,
    )

    config = _agent_config(True)
    status1 = warmup.start_runtime_warmup(agent_config=config)
    assert status1.enabled is True
    assert status1.state == "running"
    assert started.wait(timeout=1.0)

    try:
        # Second call should not restart
        status2 = warmup.start_runtime_warmup(agent_config=config)
        assert status2.state == "running"
    finally:
        release.set()

    final = _wait_for_terminal_state()
    assert final.state == "success"
    assert call_count["value"] == 1


def test_warmup_success_records_duration_and_is_one_shot(monkeypatch):
    def fake_warm_up(*, agent_config=None):
        return {"duration_ms": 42, "mode": "jiuwen-runtime"}

    monkeypatch.setattr(
        "agentic.loop.jiuwen_adapter.warm_up_jiuwen_runtime",
        fake_warm_up,
    )

    config = _agent_config(True)
    warmup.start_runtime_warmup(agent_config=config)

    status = _wait_for_terminal_state()
    assert status.state == "success"
    assert status.duration_ms == 42
    assert status.mode == "jiuwen-runtime"
    assert status.error_type is None
    assert status.error_message is None
    assert status.started_at is not None
    assert status.finished_at is not None

    # Subsequent call does not restart
    status2 = warmup.start_runtime_warmup(agent_config=config)
    assert status2.state == "success"


def test_warmup_failed_does_not_throw_and_is_one_shot(monkeypatch):
    def fake_warm_up(*, agent_config=None):
        raise RuntimeError("warm-up exploded")

    monkeypatch.setattr(
        "agentic.loop.jiuwen_adapter.warm_up_jiuwen_runtime",
        fake_warm_up,
    )

    config = _agent_config(True)

    # Should not raise
    warmup.start_runtime_warmup(agent_config=config)

    status = _wait_for_terminal_state()
    assert status.state == "failed"
    assert status.error_type is None
    assert status.error_message is None
    assert status.duration_ms is None

    # Subsequent call does not retry
    status2 = warmup.start_runtime_warmup(agent_config=config)
    assert status2.state == "failed"


def test_warmup_enabled_by_default(monkeypatch):

    def fake_warm_up(*, agent_config=None):
        return {"duration_ms": 5, "mode": "jiuwen-runtime"}

    monkeypatch.setattr(
        "agentic.loop.jiuwen_adapter.warm_up_jiuwen_runtime",
        fake_warm_up,
    )

    config = AgentConfig.from_env()
    assert config.runtime_warmup_enabled is True

    warmup.start_runtime_warmup(agent_config=config)
    status = _wait_for_terminal_state()
    assert status.state == "success"


def test_warmup_env_fallback_can_disable_warmup(monkeypatch):
    monkeypatch.setenv("AGENT_RUNTIME_WARMUP_ENABLED", "false")

    config = AgentConfig.from_env()
    assert config.runtime_warmup_enabled is False

    status = warmup.start_runtime_warmup(agent_config=config)
    assert status.enabled is False
    assert status.state == "disabled"


def test_warmup_failed_logs_only_failure_result(monkeypatch, caplog):
    def fake_warm_up(*, agent_config=None):
        raise RuntimeError("api_key=sk-test token=secret password=pw")

    monkeypatch.setattr(
        "agentic.loop.jiuwen_adapter.warm_up_jiuwen_runtime",
        fake_warm_up,
    )

    caplog.set_level("WARNING")
    config = _agent_config(True)
    warmup.start_runtime_warmup(agent_config=config)

    status = _wait_for_terminal_state()
    assert status.state == "failed"
    assert status.error_type is None
    assert status.error_message is None
    assert "Runtime warm-up finished state=failed" in caplog.text
    assert "api_key" not in caplog.text
    assert "sk-test" not in caplog.text
    assert "token" not in caplog.text
    assert "password" not in caplog.text
