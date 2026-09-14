from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest
from solver_coordination.profiles import (
    ProfileFactResolver,
    ProfileNotFound,
    ProfileRegistryError,
    SolverProfileRegistration,
    SolverProfileRegistry,
)


@dataclass(frozen=True)
class _Component:
    method_name: str

    def __getattr__(self, name: str) -> Any:
        if name == self.method_name:
            return lambda **_kwargs: {}
        raise AttributeError(name)


def _registration(profile_id: str = "ordered_binary_route.v1") -> SolverProfileRegistration:
    return SolverProfileRegistration(
        profile_id=profile_id,
        fact_provider=_Component("resolve_facts"),
        compiler=_Component("compile_problem"),
        verifier=_Component("verify"),
        presenter=_Component("present"),
    )


def test_profile_registry_resolves_one_stable_profile():
    registration = _registration()
    registry = SolverProfileRegistry([registration])

    assert registry.get("ordered_binary_route.v1") is registration


def test_profile_registry_rejects_duplicate_or_missing_profiles():
    registration = _registration()
    with pytest.raises(ProfileRegistryError, match="duplicate"):
        SolverProfileRegistry([registration, registration])

    registry = SolverProfileRegistry([registration])
    with pytest.raises(ProfileNotFound, match="missing-profile"):
        registry.get("missing-profile")


def test_profile_registry_fails_closed_for_a_noncanonical_lookup_id():
    registry = SolverProfileRegistry([_registration()])

    with pytest.raises(ProfileNotFound, match="profile"):
        registry.get(" ordered_binary_route.v1 ")


@pytest.mark.parametrize("profile_id", ["", "   "])
def test_profile_registry_rejects_blank_profile_ids(profile_id: str):
    with pytest.raises(ProfileRegistryError, match="profile_id"):
        SolverProfileRegistry([_registration(profile_id)])


@pytest.mark.parametrize(
    ("field_name", "method_name"),
    [
        ("fact_provider", "resolve_facts"),
        ("compiler", "compile_problem"),
        ("verifier", "verify"),
        ("presenter", "present"),
    ],
)
def test_profile_registry_rejects_a_missing_required_component_callable(
    field_name: str,
    method_name: str,
):
    values = {
        "profile_id": "ordered_binary_route.v1",
        "fact_provider": _Component("resolve_facts"),
        "compiler": _Component("compile_problem"),
        "verifier": _Component("verify"),
        "presenter": _Component("present"),
    }
    values[field_name] = object()
    registration = SolverProfileRegistration(**values)

    with pytest.raises(ProfileRegistryError, match=method_name):
        SolverProfileRegistry([registration])


class _FactProviderSpy:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def resolve_facts(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"facts": [], "bindings": {}, "as_of": "synthetic"}


def test_profile_fact_resolver_delegates_using_the_locked_profile_id():
    provider = _FactProviderSpy()
    registration = SolverProfileRegistration(
        profile_id="ordered_binary_route.v1",
        fact_provider=provider,
        compiler=_Component("compile_problem"),
        verifier=_Component("verify"),
        presenter=_Component("present"),
    )
    resolver = ProfileFactResolver(
        profile_registry=SolverProfileRegistry([registration]),
        binding_revision_id="binding-synthetic",
    )
    capability = {
        "decision_model": {"profile_id": "ordered_binary_route.v1"},
    }
    normalized_input = object()

    result = resolver.resolve(
        capability=capability,
        normalized_input=normalized_input,
        required_facts=("route_metrics",),
        workspace_id="workspace-synthetic",
    )

    assert result == {"facts": [], "bindings": {}, "as_of": "synthetic"}
    assert provider.calls == [
        {
            "capability": capability,
            "normalized_input": normalized_input,
            "required_facts": ("route_metrics",),
            "workspace_id": "workspace-synthetic",
            "binding_revision_id": "binding-synthetic",
        }
    ]


def test_profile_fact_resolver_fails_closed_without_a_locked_model():
    resolver = ProfileFactResolver(
        profile_registry=SolverProfileRegistry([_registration()]),
        binding_revision_id="binding-synthetic",
    )

    with pytest.raises(ProfileRegistryError, match="decision_model"):
        resolver.resolve(
            capability={},
            normalized_input=object(),
            required_facts=(),
        )
