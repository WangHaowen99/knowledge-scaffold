"""openJiuwen provider contract checks against a live DA-compatible engine.

E2E_ENGINE selects the provider: ``deepanalyze`` (default, DA_BASE_URL) or
``loopforest`` (LF_BASE_URL; the user-input check is skipped, Loop Forest never asks).

Uses agent-core's provider-neutral suite
(tests/system_tests/harness_providers/_contract.py at the openJiuwen version
the host pins), exported next to this package by run_contract.sh.
"""

from __future__ import annotations

import os

import pytest
from openjiuwen.harness_protocol import HostCapability
from tests.system_tests.harness_providers._contract import (
    RecordingUserInputHandler,
    make_context,
    run_abort_turn,
    run_follow_up_turns,
    run_resume_turns,
    run_steer_turn,
    run_text_turn,
    run_user_input_turn,
)

from deepanalyze_harness import DeepAnalyzeConfig, DeepAnalyzeHarness

ENGINE = os.getenv("E2E_ENGINE", "deepanalyze")
PREFIX = "LF_" if ENGINE == "loopforest" else "DA_"

pytestmark = pytest.mark.skipif(not os.getenv(PREFIX + "BASE_URL"), reason=f"needs a running {ENGINE} engine")


def build() -> DeepAnalyzeHarness:
    if ENGINE == "loopforest":
        from loopforest_harness import LoopForestHarness as cls
    else:
        cls = DeepAnalyzeHarness
    return cls(DeepAnalyzeConfig.from_mapping({
        "base_url": os.environ[PREFIX + "BASE_URL"],
        "tenant_id": os.getenv(PREFIX + "TENANT_ID", "default"),
        "session_prefix": "contract-",
    }, env_prefix=PREFIX))


async def test_text_turn() -> None:
    await run_text_turn(build(), make_context())


async def test_follow_up_turns() -> None:
    await run_follow_up_turns(build(), make_context())


async def test_abort_turn() -> None:
    await run_abort_turn(build(), make_context())


@pytest.mark.skipif(ENGINE == "loopforest", reason="Loop Forest never asks the user")
async def test_user_input_turn() -> None:
    handler = RecordingUserInputHandler("teal")
    context = make_context(host_capabilities=frozenset({HostCapability.USER_INPUT}), interactions=handler)
    await run_user_input_turn(build(), context, handler, tool_hint="ask_user")


async def test_resume_turns() -> None:
    await run_resume_turns(build, {})


async def test_steer_turn() -> None:
    await run_steer_turn(build(), make_context())
