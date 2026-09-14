# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Internal graph detail tool adapter."""

from __future__ import annotations

from ..backends.base import GraphBackend
from ..models import GraphScope


def get_element_info(
    *,
    backend: GraphBackend,
    element_instance_id: str,
    scope: GraphScope,
    element_class: str = "",
    element_type: str = "",
) -> dict:
    return backend.get_element_info(
        element_instance_id=element_instance_id,
        element_class=element_class,
        element_type=element_type,
        scope=scope,
    )
