# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Technical capability checks for shared business-data providers."""

from __future__ import annotations

from .contracts import ProviderCapabilities
from .errors import UnsupportedCapabilityError


def require_capability(
    capabilities: ProviderCapabilities, operation: str
) -> None:
    attribute = "query" if operation == "iter_query_events" else operation
    if attribute not in {"query", "query_sql", "probe"} or not getattr(
        capabilities, attribute, False
    ):
        raise UnsupportedCapabilityError(
            "provider does not support '{}'".format(operation)
        )
