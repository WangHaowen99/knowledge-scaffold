"""Dispatch independent verification through a locked profile registry."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import VerificationDivergenceError, VerificationRuntimeFailure


class ProfileDispatchVerifier:
    def __init__(self, profile_registry: Any) -> None:
        if not callable(getattr(profile_registry, "get", None)):
            raise VerificationDivergenceError("profile registry is unavailable")
        self.profile_registry = profile_registry

    def verify(
        self,
        *,
        problem: Any,
        capability: Any,
        normalized_input: Any,
        fact_snapshot: Any,
        solver_result: Any,
    ) -> Any:
        model = capability.get("decision_model") if isinstance(capability, Mapping) else getattr(capability, "decision_model", None)
        if not isinstance(model, Mapping):
            raise VerificationDivergenceError("locked decision model is missing")
        profile_id = model.get("profile_id")
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise VerificationDivergenceError("profile_id is noncanonical")
        try:
            registration = self.profile_registry.get(profile_id)
        except Exception as exc:
            raise VerificationDivergenceError("solver profile is unavailable") from exc
        verifier = getattr(registration, "verifier", None)
        if not callable(getattr(verifier, "verify", None)):
            raise VerificationDivergenceError("solver profile verifier is unavailable")
        try:
            return verifier.verify(
                problem=problem,
                capability=capability,
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
                solver_result=solver_result,
            )
        except (VerificationDivergenceError, VerificationRuntimeFailure):
            raise
        except Exception as exc:
            raise VerificationDivergenceError("solver profile verification failed") from exc


__all__ = ["ProfileDispatchVerifier"]
