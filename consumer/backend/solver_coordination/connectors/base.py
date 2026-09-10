"""Connector port implemented by concrete solver-service adapters."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from solver_coordination.contracts import ProblemIR, SolverResult


@runtime_checkable
class SolverConnector(Protocol):
    def capabilities(self, *, binding: Mapping[str, Any] | None = None) -> Mapping[str, Any]: ...

    def assert_binding_capabilities(
        self,
        binding: Mapping[str, Any],
        *,
        required_capabilities: Sequence[str] = (),
    ) -> None: ...

    def submit(
        self,
        *,
        binding: Mapping[str, Any],
        problem: ProblemIR,
        idempotency_key: str,
        deadline: datetime,
        result_quality: str = "optimal",
        traceparent: str | None = None,
    ) -> Mapping[str, Any]: ...

    def poll(self, *, binding: Mapping[str, Any], job_id: str) -> Mapping[str, Any]: ...

    def cancel(self, *, binding: Mapping[str, Any], job_id: str) -> Mapping[str, Any]: ...

    def result(self, *, binding: Mapping[str, Any], job_id: str) -> SolverResult: ...


__all__ = ["SolverConnector"]
