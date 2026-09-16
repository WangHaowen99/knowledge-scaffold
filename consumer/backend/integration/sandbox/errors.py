# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Errors raised by the sandbox integration."""

from __future__ import annotations


class SandboxError(Exception):
    """Base class for sandbox integration errors."""


class SandboxUnavailableError(SandboxError):
    """jiuwenbox is not reachable or unhealthy."""


class SandboxUnsupportedProviderError(SandboxUnavailableError):
    """The bound business-data provider has no sandbox proxy support yet."""

    def __init__(self, provider: str) -> None:
        self.provider = str(provider or "").strip().lower() or "unknown"
        super().__init__(
            "unsupported sandbox business data provider '{}'".format(self.provider)
        )


class SandboxCreateError(SandboxError):
    """Failed to create a sandbox instance."""


class SandboxExecError(SandboxError):
    """Failed to execute a command inside the sandbox."""
