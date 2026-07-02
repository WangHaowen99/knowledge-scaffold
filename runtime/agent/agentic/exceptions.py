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


class InputTokenLimitError(AgenticError):
    """Raised when an LLM request exceeds the platform input token limit."""

    def __init__(self, estimated_tokens: int, limit: int) -> None:
        super().__init__(
            "输入 Token 长度超过上限: {} / {}".format(estimated_tokens, limit),
            {
                "estimated_tokens": estimated_tokens,
                "limit": limit,
            },
        )


class WorkflowCancelledError(AgenticError):
    """Raised when the workflow is cancelled by user rejection."""

    pass
