"""Errors raised by the sandbox integration."""

from __future__ import annotations


class SandboxError(Exception):
    """Base class for sandbox integration errors."""


class SandboxUnavailableError(SandboxError):
    """jiuwenbox is not reachable or unhealthy."""


class SandboxCreateError(SandboxError):
    """Failed to create a sandbox instance."""


class SandboxExecError(SandboxError):
    """Failed to execute a command inside the sandbox."""

