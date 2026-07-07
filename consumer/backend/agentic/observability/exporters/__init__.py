"""Exporter registry for observability providers."""

from __future__ import annotations

from typing import Dict

from .base import ExporterFactory, RuntimeExporter
from .langfuse import create_langfuse_exporter
from .local import create_local_perf_exporter

EXPORTER_REGISTRY: Dict[str, ExporterFactory] = {
    "local": create_local_perf_exporter,
    "langfuse": create_langfuse_exporter,
}

__all__ = ["EXPORTER_REGISTRY", "ExporterFactory", "RuntimeExporter"]
