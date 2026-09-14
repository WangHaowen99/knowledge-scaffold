# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Stable exceptions for shared business-data provider consumers."""


class BusinessDataProviderError(RuntimeError):
    """Base class for public provider errors."""


class ConnectionEnvelopeError(BusinessDataProviderError):
    """Raised when an effective connection envelope is invalid."""


class UnsupportedProviderError(BusinessDataProviderError):
    """Raised when no implementation exists for a provider."""


class UnsupportedCapabilityError(BusinessDataProviderError):
    """Raised when a provider lacks an operation."""


class ProviderTransportError(BusinessDataProviderError):
    """Raised for transport failures that cannot be represented as a result."""


class ProviderProtocolError(BusinessDataProviderError):
    """Raised for invalid provider protocol data."""


class ProviderInternalError(BusinessDataProviderError):
    """Raised for unexpected internal failures."""
