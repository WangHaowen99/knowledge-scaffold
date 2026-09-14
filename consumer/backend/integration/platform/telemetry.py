# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from typing import Any, Mapping

from integration.observability.events import RuntimeEvent
from integration.platform.config import PlatformConfig
from integration.platform.events import to_platform_event


RunContext = Mapping[str, Any] | None


def to_platform_telemetry_event(
    event: RuntimeEvent,
    config: PlatformConfig,
    *,
    run_context: RunContext = None,
) -> dict[str, Any] | None:
    return to_platform_event(event, config, run_context=run_context)
