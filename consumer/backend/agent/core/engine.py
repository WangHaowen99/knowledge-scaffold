# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Stable agent-loop interface backed exclusively by openJiuwen."""

from __future__ import annotations

from .jiuwen_engine import JiuwenReactLoopEngine


class AgentLoopEngine(JiuwenReactLoopEngine):
    """Compatibility name for the repository's only agent-loop runtime."""
