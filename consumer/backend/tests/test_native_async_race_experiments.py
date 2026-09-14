# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Fast CI gate for the reusable native-async quantitative experiments."""

from __future__ import annotations

import asyncio

from scripts.native_async_race_experiments import run_experiments


def test_native_async_race_experiment_runner_keeps_all_resources_at_zero():
    result = asyncio.run(
        run_experiments(
            cancel_iterations=12,
            shutdown_iterations=12,
            late_event_attempts=120,
            sse_iterations=30,
        )
    )

    assert result["passed"] is True
    assert result["cancel_complete"]["complete_overwrites"] == 0
    assert result["cancel_complete"]["terminal_events"] == 12
    assert result["shutdown_complete"]["complete_overwrites"] == 0
    assert result["shutdown_complete"]["terminal_events"] == 12
    assert result["late_events"]["attempts"] == 120
    assert result["late_events"]["rejected"] == 120
    assert result["late_events"]["writes"] == 0
    assert result["sse_lifecycle"]["total"] == 30
    assert result["sse_lifecycle"]["active_subscribers"] == 0
    assert result["sse_lifecycle"]["reserved_subscribers"] == 0
    assert set(result["resources"].values()) == {0}
