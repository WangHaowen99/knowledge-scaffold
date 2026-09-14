# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Stable platform-side error categories for the OR-Tools service connector."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ConnectorError(Exception):
    """A sanitized error at the Consumer/Connector boundary."""

    message: str
    category: str = "internal"
    code: str = "CONNECTOR_ERROR"
    retryable: bool = False
    status_code: int | None = None
    diagnostic_ref: str | None = None
    payload: Any = None

    def __post_init__(self) -> None:
        # Do not expose response bodies or exception reprs through Exception's
        # default args.  They may contain vendor data or credentials.
        Exception.__init__(self, self.message)


class ConnectorContractError(ConnectorError):
    def __init__(self, message: str = "connector contract validation failed", **kwargs: Any):
        super().__init__(
            message,
            category="validation",
            code="CONNECTOR_CONTRACT_INVALID",
            retryable=kwargs.pop("retryable", False),
            **kwargs,
        )


class ConnectorAuthenticationError(ConnectorError):
    def __init__(self, message: str = "connector authentication failed", **kwargs: Any):
        super().__init__(
            message,
            category="authentication",
            code="CONNECTOR_AUTHENTICATION_FAILED",
            retryable=kwargs.pop("retryable", False),
            **kwargs,
        )


class ConnectorCapacityError(ConnectorError):
    def __init__(self, message: str = "connector capacity is unavailable", **kwargs: Any):
        super().__init__(
            message,
            category="capacity",
            code="CONNECTOR_CAPACITY_UNAVAILABLE",
            retryable=kwargs.pop("retryable", True),
            **kwargs,
        )


class ConnectorTransportError(ConnectorError):
    def __init__(self, message: str = "connector transport failed", **kwargs: Any):
        super().__init__(
            message,
            category="internal",
            code="CONNECTOR_TRANSPORT_FAILED",
            retryable=kwargs.pop("retryable", True),
            **kwargs,
        )


class ConnectorExecutionError(ConnectorError):
    def __init__(self, message: str = "connector execution failed", **kwargs: Any):
        super().__init__(
            message,
            category="execution",
            code="CONNECTOR_EXECUTION_FAILED",
            retryable=kwargs.pop("retryable", False),
            **kwargs,
        )


class ConnectorResultError(ConnectorError):
    def __init__(self, message: str = "connector result is invalid", **kwargs: Any):
        super().__init__(
            message,
            category="result",
            code="CONNECTOR_RESULT_INVALID",
            retryable=kwargs.pop("retryable", False),
            **kwargs,
        )


__all__ = [
    "ConnectorAuthenticationError",
    "ConnectorCapacityError",
    "ConnectorContractError",
    "ConnectorError",
    "ConnectorExecutionError",
    "ConnectorResultError",
    "ConnectorTransportError",
]
