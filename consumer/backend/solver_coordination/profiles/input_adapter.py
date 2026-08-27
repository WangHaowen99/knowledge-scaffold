"""Profile-scoped canonicalization of schema-bound solver input."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from solver_coordination.contracts import SolverContractError

from .registry import ProfileNotFound


class InputAliasConflict(SolverContractError):
    """One or more accepted aliases disagree on their canonical value."""

    def __init__(self, errors: tuple[Mapping[str, str], ...]) -> None:
        super().__init__("input aliases disagree")
        self.errors = errors


@dataclass(frozen=True)
class ProfileInputAdapterService:
    profile_registry: Any

    def __post_init__(self) -> None:
        if not callable(getattr(self.profile_registry, "get", None)):
            raise SolverContractError("profile registry must provide get")

    def adapt(
        self,
        *,
        capability: Any,
        operation_key: str,
        values: Mapping[str, Any],
        normalization: Mapping[str, Any],
    ) -> Mapping[str, Any]:
        model = (
            capability.get("decision_model")
            if isinstance(capability, Mapping)
            else getattr(capability, "decision_model", None)
        )
        if not isinstance(model, Mapping):
            raise SolverContractError("locked decision model must be an object")
        profile_id = model.get("profile_id")
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise SolverContractError("locked decision model profile_id is required")
        try:
            registration = self.profile_registry.get(profile_id)
        except Exception as exc:
            if isinstance(exc, ProfileNotFound):
                raise
            raise SolverContractError("solver input profile lookup failed") from exc
        adapter = getattr(registration, "input_adapter", None)
        if adapter is None:
            return {
                "values": dict(values),
                "normalization": dict(normalization),
            }
        if not callable(getattr(adapter, "adapt", None)):
            raise SolverContractError("solver profile input adapter is invalid")
        adapted = adapter.adapt(
            operation_key=operation_key,
            values=values,
            normalization=normalization,
        )
        if not isinstance(adapted, Mapping):
            raise SolverContractError("solver profile input adapter returned invalid data")
        if set(adapted) != {"values", "normalization"}:
            raise SolverContractError("solver profile input adapter returned invalid data")
        if not isinstance(adapted.get("values"), Mapping):
            raise SolverContractError("solver profile input adapter returned invalid data")
        if not isinstance(adapted.get("normalization"), Mapping):
            raise SolverContractError("solver profile input adapter returned invalid data")
        return {
            "values": dict(adapted["values"]),
            "normalization": dict(adapted["normalization"]),
        }


__all__ = ["InputAliasConflict", "ProfileInputAdapterService"]
