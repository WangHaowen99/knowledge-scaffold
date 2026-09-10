"""Framework-neutral ports for the solver coordination core."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Iterable, Protocol

from .connectors.base import SolverConnector
from .contracts import (
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
    VerificationResult,
)


class FactSnapshotProducer(Protocol):
    def build(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        package_id: str,
        package_revision: str,
        normalized_input: NormalizedInput,
        required_facts: Iterable[str],
        **kwargs: Any,
    ) -> FactSnapshot:
        return ...


class DecisionModelLoader(Protocol):
    def load(self, *, capability: Any) -> Mapping[str, Any]:
        return ...


class ProblemCompiler(Protocol):
    def compile(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> ProblemIR:
        return ...


class IndependentVerifier(Protocol):
    def verify(
        self,
        *,
        problem: ProblemIR,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
        solver_result: SolverResult,
    ) -> VerificationResult:
        return ...


class ProblemFamilyAdapter(Protocol):
    family_key: str

    def compile_problem(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> Mapping[str, Any]:
        return ...


class ProblemFamilyRegistryPort(Protocol):
    def get(self, family_key: str) -> ProblemFamilyAdapter:
        return ...

    def compile_problem(self, family_key: str, **kwargs: Any) -> Any:
        return ...


__all__ = [
    "DecisionModelLoader",
    "FactSnapshotProducer",
    "IndependentVerifier",
    "ProblemCompiler",
    "ProblemFamilyAdapter",
    "ProblemFamilyRegistryPort",
    "SolverConnector",
]
