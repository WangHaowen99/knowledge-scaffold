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
