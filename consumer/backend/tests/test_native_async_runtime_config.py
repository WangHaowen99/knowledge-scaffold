from types import SimpleNamespace

import pytest

from agent.runtime.config import NativeAsyncRuntimeConfig


def test_native_async_runtime_config_has_bounded_pool_defaults(monkeypatch):
    for name in (
        "AGENTIC_SHUTDOWN_TOTAL_DEADLINE_SECONDS",
        "AGENTIC_SHORT_IO_WORKERS",
        "AGENTIC_SHORT_IO_MAX_PENDING",
        "AGENTIC_EXTERNAL_IO_WORKERS",
        "AGENTIC_EXTERNAL_IO_MAX_PENDING",
        "AGENTIC_TOOL_IO_WORKERS",
        "AGENTIC_TOOL_IO_MAX_PENDING",
    ):
        monkeypatch.delenv(name, raising=False)

    config = NativeAsyncRuntimeConfig.from_env()

    assert config.shutdown_total_deadline_seconds == 30
    assert (config.short_io_workers, config.short_io_max_pending) == (8, 64)
    assert (config.external_io_workers, config.external_io_max_pending) == (16, 16)
    assert (config.tool_io_workers, config.tool_io_max_pending) == (8, 16)
    assert config.tool_io_operation_timeout_seconds == 600


def test_native_async_runtime_config_uses_agent_tool_timeout(monkeypatch):
    monkeypatch.delenv("AGENTIC_TOOL_IO_OPERATION_TIMEOUT_SECONDS", raising=False)

    config = NativeAsyncRuntimeConfig.from_agent_config(
        SimpleNamespace(tool_timeout_seconds=45)
    )

    assert config.tool_io_operation_timeout_seconds == 45


def test_native_async_runtime_config_rejects_invalid_limits(monkeypatch):
    monkeypatch.setenv("AGENTIC_TOOL_IO_MAX_PENDING", "0")

    with pytest.raises(ValueError, match="positive integer"):
        NativeAsyncRuntimeConfig.from_env()


def test_native_async_runtime_config_rejects_process_limit_below_run_limit():
    with pytest.raises(ValueError, match="greater than or equal"):
        NativeAsyncRuntimeConfig(
            max_subscribers_per_run=4,
            max_subscribers_total=3,
        )
