# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Fail-closed lookup and fact dispatch for solver profiles."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from solver_coordination.contracts import NormalizedInput, SolverContractError

from .contracts import SolverProfileRegistration


class ProfileRegistryError(SolverContractError):
    """Raised when the immutable profile registry is malformed."""


class ProfileNotFound(ProfileRegistryError):
    """Raised when a locked decision model names no registered profile."""


class SolverProfileRegistry:
    _COMPONENT_METHODS = {
        "fact_provider": "resolve_facts",
        "compiler": "compile_problem",
        "verifier": "verify",
        "presenter": "present",
    }

    def __init__(
        self, registrations: Iterable[SolverProfileRegistration] = ()
    ) -> None:
        profiles: dict[str, SolverProfileRegistration] = {}
        for registration in registrations:
            if not isinstance(registration, SolverProfileRegistration):
                raise ProfileRegistryError(
                    "registrations must contain SolverProfileRegistration values"
                )
            profile_id = registration.profile_id
            if (
                not isinstance(profile_id, str)
                or not profile_id.strip()
                or profile_id != profile_id.strip()
            ):
                raise ProfileRegistryError("profile_id must be a nonblank canonical string")
            if profile_id in profiles:
                raise ProfileRegistryError("duplicate profile_id: {}".format(profile_id))
            for field_name, method_name in self._COMPONENT_METHODS.items():
                component = getattr(registration, field_name)
                if not callable(getattr(component, method_name, None)):
                    raise ProfileRegistryError(
                        "profile {} must provide callable {}".format(
                            profile_id, method_name
                        )
                    )
            input_adapter = registration.input_adapter
            if input_adapter is not None and not callable(
                getattr(input_adapter, "adapt", None)
            ):
                raise ProfileRegistryError(
                    "profile {} must provide callable input adapter".format(profile_id)
                )
            profiles[profile_id] = registration
        self._profiles = MappingProxyType(profiles)

    def get(self, profile_id: str) -> SolverProfileRegistration:
        if (
            not isinstance(profile_id, str)
            or not profile_id
            or profile_id != profile_id.strip()
        ):
            raise ProfileNotFound("solver profile not found: noncanonical profile_id")
        key = profile_id
        try:
            return self._profiles[key]
        except KeyError as exc:
            raise ProfileNotFound("solver profile not found: {}".format(key)) from exc

    def profile_ids(self) -> tuple[str, ...]:
        return tuple(self._profiles)


@dataclass(frozen=True)
class ProfileFactResolver:
    profile_registry: SolverProfileRegistry
    binding_revision_id: str | None

    def __post_init__(self) -> None:
        if not callable(getattr(self.profile_registry, "get", None)):
            raise ProfileRegistryError("profile_registry must provide get")
        if self.binding_revision_id is not None:
            if (
                not isinstance(self.binding_revision_id, str)
                or not self.binding_revision_id.strip()
            ):
                raise ProfileRegistryError("binding_revision_id is invalid")
            object.__setattr__(self, "binding_revision_id", self.binding_revision_id.strip())

    def resolve(
        self,
        *,
        capability: Any,
        normalized_input: NormalizedInput,
        **identity: Any,
    ) -> Mapping[str, Any]:
        model = _capability_value(capability, "decision_model")
        if not isinstance(model, Mapping):
            raise ProfileRegistryError("locked decision_model must be an object")
        profile_id = model.get("profile_id")
        if not isinstance(profile_id, str) or not profile_id.strip():
            raise ProfileRegistryError("locked decision_model profile_id is required")
        registration = self.profile_registry.get(profile_id)
        selected_binding = identity.pop("binding_revision_id", self.binding_revision_id)
        if not isinstance(selected_binding, str) or not selected_binding.strip():
            raise ProfileRegistryError("binding_revision_id is required")
        return registration.fact_provider.resolve_facts(
            capability=capability,
            normalized_input=normalized_input,
            binding_revision_id=selected_binding.strip(),
            **identity,
        )


def _capability_value(capability: Any, name: str) -> Any:
    if isinstance(capability, Mapping):
        return capability.get(name)
    return getattr(capability, name, None)


__all__ = [
    "ProfileFactResolver",
    "ProfileNotFound",
    "ProfileRegistryError",
    "SolverProfileRegistry",
]
