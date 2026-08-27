"""Immutable component contracts for locked solver profiles."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Protocol

from solver_coordination.contracts import (
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
    VerificationResult,
)


class ProfileFactProvider(Protocol):
    def resolve_facts(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        **identity: Any,
    ) -> Mapping[str, Any]: ...


class ProfileCompiler(Protocol):
    def compile_problem(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
    ) -> Mapping[str, Any]: ...


class ProfileVerifier(Protocol):
    def verify(
        self,
        *,
        problem: ProblemIR,
        capability: Any,
        normalized_input: NormalizedInput,
        fact_snapshot: FactSnapshot,
        solver_result: SolverResult,
    ) -> VerificationResult: ...


class ProfilePresentationPort(Protocol):
    def present(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        solver_result: SolverResult,
        verification: VerificationResult,
    ) -> Mapping[str, Any]: ...


class ProfileInputAdapter(Protocol):
    def adapt(
        self,
        *,
        operation_key: str,
        values: Mapping[str, Any],
        normalization: Mapping[str, Any],
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class SolverProfileRegistration:
    profile_id: str
    fact_provider: ProfileFactProvider
    compiler: ProfileCompiler
    verifier: ProfileVerifier
    presenter: ProfilePresentationPort
    input_adapter: ProfileInputAdapter | None = None


__all__ = [
    "ProfileCompiler",
    "ProfileFactProvider",
    "ProfileInputAdapter",
    "ProfilePresentationPort",
    "ProfileVerifier",
    "SolverProfileRegistration",
]
