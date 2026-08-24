from __future__ import annotations

from typing import Any


class AgentRuntimeError(Exception):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        status: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        return {"error": self.message, "code": self.code, **self.details}
