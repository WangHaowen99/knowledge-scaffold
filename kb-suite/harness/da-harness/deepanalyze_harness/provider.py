"""Provider factory (entry point ``openjiuwen.harness_providers: deepanalyze``)."""

from __future__ import annotations

from typing import Any

from openjiuwen.harness_protocol import HarnessCard, JsonObject

from .config import DeepAnalyzeConfig
from .harness import DeepAnalyzeHarness


class DeepAnalyzeHarnessProvider:
    """Validate configuration and create an unstarted DeepAnalyze harness."""

    @property
    def card(self) -> HarnessCard:
        return DeepAnalyzeHarness.card

    @staticmethod
    def create(config: JsonObject) -> DeepAnalyzeHarness:
        return DeepAnalyzeHarness(DeepAnalyzeConfig.from_mapping(config))

    def model_config(self, choice: Any, *, fallback: Any = None) -> JsonObject:
        """``ModelConfigProvider`` hook: DA runs the model configured on the engine.

        A team may still assign a built-in model to the member; it is accepted
        and ignored. Endpoint models never arrive (the card does not declare
        ``ENDPOINT_MODELS``), nor does a fallback (no ``AUTH_FALLBACK``).
        """
        del choice, fallback
        return {}


__all__ = ["DeepAnalyzeHarnessProvider"]
