"""Provider factory (entry point ``openjiuwen.harness_providers: loopforest``)."""

from __future__ import annotations

from typing import Any

from openjiuwen.harness_protocol import HarnessCard, JsonObject

from deepanalyze_harness.config import DeepAnalyzeConfig

from .harness import LoopForestHarness

ENV_PREFIX = "LF_"  # LF_BASE_URL, LF_ENGINE_SECRET, LF_TENANT_ID, LF_KB_CATALOG_URL ...


class LoopForestHarnessProvider:
    """Validate configuration and create an unstarted Loop Forest harness."""

    @property
    def card(self) -> HarnessCard:
        return LoopForestHarness.card

    @staticmethod
    def create(config: JsonObject) -> LoopForestHarness:
        return LoopForestHarness(DeepAnalyzeConfig.from_mapping(config, env_prefix=ENV_PREFIX))

    def model_config(self, choice: Any, *, fallback: Any = None) -> JsonObject:
        """``ModelConfigProvider`` hook: the service runs the model it is configured with."""
        del choice, fallback
        return {}


__all__ = ["LoopForestHarnessProvider"]
