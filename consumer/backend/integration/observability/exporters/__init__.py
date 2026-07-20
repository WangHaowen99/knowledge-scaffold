"""Exporter registry for observability providers."""

from __future__ import annotations

from typing import Dict

from integration.observability.exporters.base import ExporterFactory, RuntimeExporter
from integration.observability.exporters.langfuse import create_langfuse_exporter
from integration.observability.exporters.local import create_local_perf_exporter
from integration.observability.exporters.speedscope import create_speedscope_exporter

EXPORTER_REGISTRY: Dict[str, ExporterFactory] = {
    "local": create_local_perf_exporter,
    "langfuse": create_langfuse_exporter,
}

__all__ = ["EXPORTER_REGISTRY", "ExporterFactory", "RuntimeExporter", "create_speedscope_exporter"]
