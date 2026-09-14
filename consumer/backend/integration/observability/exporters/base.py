# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Exporter contracts for observability providers."""

from __future__ import annotations

from typing import Protocol

from integration.observability.events import RuntimeEvent


class RuntimeExporter(Protocol):
    """Provider boundary consumed by the non-blocking observability bus.

    Exporters receive already-built ``RuntimeEvent`` instances and translate
    them to provider records. They do not construct runtime events.
    """

    @property
    def available(self) -> bool:
        """Whether this exporter can send events with its current configuration."""

    def export(self, event: RuntimeEvent) -> None:
        """Send one runtime event to the provider."""

    def flush(self) -> None:
        """Flush buffered provider data."""

    def close(self) -> None:
        """Release provider resources."""
