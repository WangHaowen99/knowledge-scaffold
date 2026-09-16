# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from pathlib import Path

from scripts.native_async_restart_experiments import run_experiments


def test_independent_process_restart_and_turn_allocation_smoke(tmp_path: Path):
    result = run_experiments(
        tmp_path,
        running_rounds=1,
        awaiting_rounds=1,
        turn_allocations=40,
        allocation_sessions=4,
    )

    assert result["passed"] is True
    assert result["http_layer_exercised"] is False
    assert result["running_loss"]["sigkill_confirmed"] == 1
    assert result["running_loss"]["verified"] == 1
    assert result["awaiting_survival"]["sigkill_confirmed"] == 1
    assert result["awaiting_survival"]["verified"] == 1
    assert result["awaiting_survival"]["idempotent_retries"] == 1
    assert result["awaiting_survival"]["cross_key_retries"] == 1
    assert result["awaiting_survival"]["hitl_cards_restored"] == 1
    assert result["awaiting_survival"]["original_runs_reopened"] == 0
    assert result["turn_allocations"]["allocations"] == 40
    assert result["turn_allocations"]["sessions_starting_at_one"] == 4
    assert result["turn_allocations"]["duplicate_turn_ids"] == 0
    assert result["turn_allocations"]["turn_id_regressions"] == 0
    assert result["turn_allocations"]["duplicate_run_ids"] == 0
    assert result["turn_allocations"]["fresh_store_sessions"] == 4
    assert result["persistence"]["parse_errors"] == 0
    assert result["persistence"]["temporary_files"] == 0
    assert result["runtime_manager_exercised"] is False
    assert result["real_processes"] is True
    assert result["real_sigkill"] is True
    assert result["lingering_children"] == 0
