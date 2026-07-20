"""Custom exceptions for the agentic system."""

from typing import Any, Dict, Optional


class AgenticError(Exception):
    """Base exception for agentic system errors."""

    def __init__(
        self,
        message: str,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def __str__(self) -> str:
        if self.details:
            return f"{self.message} - {self.details}"
        return self.message

class DAGValidationError(AgenticError):
    """Raised when DAG has cycles or invalid dependencies."""

    pass


class WorkflowCancelledError(AgenticError):
    """Raised when the workflow is cancelled by user rejection."""

    pass


class AgentLoopStalledError(AgenticError):
    """Raised when the agent repeats an identical failed tool call."""

    PUBLIC_MESSAGE = "Tool execution stopped after a repeated failure."

    def __init__(
        self,
        message: str = "",
        details: Optional[Dict[str, Any]] = None,
        *,
        public_message: Optional[str] = None,
    ) -> None:
        # Tool output can contain credentials or internal paths. Keep the
        # original text available to controlled diagnostics, but never expose
        # it through the normal exception boundary.
        self.internal_message = str(message or "")
        self.public_message = str(public_message or self.PUBLIC_MESSAGE)
        super().__init__(self.public_message, details)

    def __str__(self) -> str:
        return self.public_message
