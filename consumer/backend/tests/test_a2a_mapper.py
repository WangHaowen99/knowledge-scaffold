# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Tests for A2A mapper utilities."""

from agent.runtime.contracts import RunStatus
from api.protocols.a2a.mapper import map_run_status_to_a2a


def test_map_run_status_running_to_in_progress():
    assert map_run_status_to_a2a(RunStatus.RUNNING).value == "in_progress"


def test_map_run_status_completed_to_completed():
    assert map_run_status_to_a2a(RunStatus.COMPLETED).value == "completed"


def test_map_run_status_none_to_not_found():
    assert map_run_status_to_a2a(None).value == "not_found"


def test_map_unexpected_awaiting_to_failed():
    assert map_run_status_to_a2a(RunStatus.AWAITING_INPUT).value == "failed"
