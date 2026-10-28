"""LoopForestHarness: the kb-suite Loop Forest service speaks DA-java's /api/agents subset.

Everything protocol-side (SSE mapping, reconnect, abort, steer, checkpoints, team
MCP bridge, knowledge-base catalog) is DeepAnalyzeHarness; only the card differs:
Loop Forest never asks the user, so no USER_INPUT / TOOL_APPROVAL host capability.
"""

from __future__ import annotations

from openjiuwen.harness_protocol import HarnessCapability, HarnessCard, HostCapability

from deepanalyze_harness.harness import DeepAnalyzeHarness

VERSION = "0.1.0"

LOOPFOREST_CARD = HarnessCard(
    name="loopforest",
    implementation_version=VERSION,
    capabilities=frozenset({
        HarnessCapability.GRACEFUL_ABORT,
        HarnessCapability.STEER,
        HarnessCapability.CHECKPOINT,
        HarnessCapability.MCP_TOOLS,
    }),
    optional_host_capabilities=frozenset({
        HostCapability.CHECKPOINT_SINK,
        HostCapability.MCP_SERVERS,
    }),
)


class LoopForestHarness(DeepAnalyzeHarness):
    card = LOOPFOREST_CARD
    engine_name = "Loop Forest"
