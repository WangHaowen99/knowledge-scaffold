# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Helpers for reporting runtime asset snapshots to onto-platform."""

from __future__ import annotations

from utils.logging import get_logger
from integration.platform.config import PlatformConfig
from integration.platform.exporter import PlatformExporter

logger = get_logger("platform.assets")


def export_ontology_snapshot_safely(*, workspace_id: str | None = None) -> None:
    config = PlatformConfig.from_env()
    if not config.enabled:
        return
    try:
        exporter = PlatformExporter(config)
        if workspace_id is None:
            exporter.export_ontology_snapshots()
        else:
            exporter.export_ontology_snapshot(workspace_id=workspace_id)
    except Exception:
        logger.warning("Failed to schedule onto-platform ontology snapshot", exc_info=True)
