# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""A2A adapter layer for exposing agentic runtime as external agent APIs."""

from api.protocols.a2a.models import A2ATaskSnapshot, A2ATaskState
from api.protocols.a2a.service import A2ATaskService

__all__ = [
    "A2ATaskSnapshot",
    "A2ATaskState",
    "A2ATaskService",
]
