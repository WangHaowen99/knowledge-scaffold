# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Immutable Solver Service binding revisions."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import hashlib
import re
from types import MappingProxyType
from typing import Any

from .canonical import canonical_json_bytes
from .connectors.endpoint_policy import EndpointPolicy, EndpointPolicyError


_CONFIGURATION_FIELDS = {
    "endpoint",
    "connector_type",
    "service_api_version",
    "service_credential_ref",
    "capability_fingerprint",
    "allowed_families",
    "neutral_limits",
    "execution_profile_ref",
    "enabled",
}
_LIMIT_FIELDS = {
    "max_payload_bytes",
    "max_solve_time_ms",
    "max_workers",
}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_OPAQUE_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://[^\s]+$")
_FAMILY_VERSION_SUFFIX_RE = re.compile(r"^(?P<family>.+)\.v(?P<major>[0-9]+)$")

# Consumer's first internal family names are intentionally accepted here as
# aliases. They all map to the one public problem type exposed by the
# ``ortools.solver.v1`` Solver Service API.
_FAMILY_ALIASES = {
    "candidate_selection": "binary_ordered_route",
    "candidate_selection.v1": "binary_ordered_route",
    "binary_ordered_route.v1": "binary_ordered_route",
}


class BindingContractError(ValueError):
    """Raised when a binding revision crosses the Solver Service boundary."""


def _identity_value(identity: Mapping[str, Any], name: str) -> Any:
    """Read a resolver identity value from kwargs or a capability object."""

    if name in identity and identity[name] is not None:
        return identity[name]
    capability = identity.get("capability")
    if isinstance(capability, Mapping):
        return capability.get(name)
    if capability is not None:
        return getattr(capability, name, None)
    return None


def _family_and_version(value: Any, version: Any = None) -> tuple[str | None, str | None]:
    if not isinstance(value, str) or not value.strip():
        return None, None
    family = value.strip()
    inferred_version: str | None = None
    suffix = _FAMILY_VERSION_SUFFIX_RE.fullmatch(family)
    if suffix is not None:
        family = suffix.group("family")
        inferred_version = "{}.0".format(suffix.group("major"))
    family = _FAMILY_ALIASES.get(family, family)
    if version is None:
        version = inferred_version
    if version is not None:
        if not isinstance(version, str) or not version.strip():
            return family, None
        version = version.strip()
    return family, version


def _required_capabilities(identity: Mapping[str, Any]) -> tuple[str, ...]:
    raw = identity.get("required_capabilities")
    if raw is None:
        raw = identity.get("capabilities")
    if raw is None:
        raw = identity.get("engine_capabilities")
    if raw is None:
        capability = identity.get("capability")
        if isinstance(capability, Mapping):
            raw = capability.get("engine_capabilities") or capability.get("capabilities")
        elif capability is not None:
            raw = getattr(capability, "engine_capabilities", None)
    if raw is None:
        return ()
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        raise BindingContractError("capability requirements are invalid")
    values = tuple(sorted({item.strip() for item in raw if isinstance(item, str) and item.strip()}))
    if len(values) != len(tuple(raw)):
        raise BindingContractError("capability requirements are invalid")
    return values


def _declared_supported_capabilities(identity: Mapping[str, Any]) -> tuple[str, ...] | None:
    raw = identity.get("supported_capabilities")
    if raw is None:
        descriptor = identity.get("connector_capabilities")
        if isinstance(descriptor, Mapping):
            raw = descriptor.get("features")
    if raw is None:
        return None
    if isinstance(raw, str):
        raw = (raw,)
    if not isinstance(raw, Sequence) or isinstance(raw, (bytes, bytearray)):
        raise BindingContractError("supported capabilities are invalid")
    values = tuple(sorted({item.strip() for item in raw if isinstance(item, str) and item.strip()}))
    if len(values) != len(tuple(raw)):
        raise BindingContractError("supported capabilities are invalid")
    return values


def _requested_limits(identity: Mapping[str, Any]) -> Mapping[str, Any] | None:
    raw = identity.get("limits")
    if raw is None:
        capability = identity.get("capability")
        if isinstance(capability, Mapping):
            raw = capability.get("limits") or capability.get("neutral_limits")
        elif capability is not None:
            raw = getattr(capability, "limits", None)
    return raw if isinstance(raw, Mapping) else None


def _binding_versions_for_family(
    binding: "SolverBindingRevision", requested_family: str
) -> tuple[str, ...] | None:
    """Return versions for a public family, accepting internal aliases.

    Persisted revisions created by the federated API use the public family
    name.  During migration a few pre-federation records may still contain
    ``candidate_selection.v1``/``binary_ordered_route.v1``; treating those as
    aliases is safe because they normalize to the same public family and do
    not introduce a second selection preference.
    """

    direct = binding.allowed_families.get(requested_family)
    if direct is not None:
        return direct
    matches: list[tuple[str, ...]] = []
    for key, versions in binding.allowed_families.items():
        normalized, inferred = _family_and_version(key)
        if normalized != requested_family:
            continue
        if inferred is not None:
            matches.append(tuple(item for item in versions if item == inferred))
        else:
            matches.append(tuple(versions))
    if not matches:
        return None
    return tuple(sorted({version for values in matches for version in values}))


def _text(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise BindingContractError("{} is required".format(field_name))
    return value


def _positive_int(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BindingContractError("{} must be a positive integer".format(field_name))
    return value


@dataclass(frozen=True)
class SolverBindingRevision:
    revision_id: str
    workspace_id: str
    endpoint: str
    connector_type: str
    service_api_version: str
    service_credential_ref: str
    capability_fingerprint: str
    allowed_families: Mapping[str, tuple[str, ...]]
    neutral_limits: Mapping[str, int]
    execution_profile_ref: str | None
    enabled: bool

    @classmethod
    def from_configuration(
        cls,
        *,
        revision_id: str,
        workspace_id: str,
        configuration: Mapping[str, Any],
        endpoint_policy: EndpointPolicy | None = None,
    ) -> "SolverBindingRevision":
        if not isinstance(configuration, Mapping) or set(configuration) != _CONFIGURATION_FIELDS:
            raise BindingContractError("binding configuration fields are invalid")
        connector_type = _text(
            configuration.get("connector_type"), "connector type"
        )
        if connector_type != "ortools":
            raise BindingContractError("connector type is unsupported")
        service_api_version = _text(
            configuration.get("service_api_version"), "service API version"
        )
        if service_api_version != "ortools.solver.v1":
            raise BindingContractError("service API version is unsupported")
        credential_ref = _text(
            configuration.get("service_credential_ref"),
            "service credential reference",
        )
        if not _OPAQUE_REF_RE.fullmatch(credential_ref):
            raise BindingContractError("service credential reference is invalid")
        fingerprint = _text(
            configuration.get("capability_fingerprint"),
            "capability_fingerprint",
        )
        if not _DIGEST_RE.fullmatch(fingerprint):
            raise BindingContractError("capability_fingerprint is invalid")

        raw_families = configuration.get("allowed_families")
        if not isinstance(raw_families, Mapping) or not raw_families:
            raise BindingContractError("allowed_families must be a non-empty object")
        families: dict[str, tuple[str, ...]] = {}
        for family, raw_versions in raw_families.items():
            name = _text(family, "problem family")
            if (
                not isinstance(raw_versions, (list, tuple))
                or not raw_versions
                or any(not isinstance(item, str) or not item or item != item.strip() for item in raw_versions)
            ):
                raise BindingContractError("allowed family versions are invalid")
            versions = tuple(raw_versions)
            if len(set(versions)) != len(versions):
                raise BindingContractError("allowed family versions contain duplicates")
            families[name] = versions

        raw_limits = configuration.get("neutral_limits")
        if not isinstance(raw_limits, Mapping) or set(raw_limits) != _LIMIT_FIELDS:
            raise BindingContractError("neutral_limits fields are invalid")
        limits = {
            name: _positive_int(raw_limits.get(name), "neutral_limits.{}".format(name))
            for name in sorted(_LIMIT_FIELDS)
        }
        profile = configuration.get("execution_profile_ref")
        if profile is not None:
            profile = _text(profile, "execution_profile_ref")
        enabled = configuration.get("enabled")
        if not isinstance(enabled, bool):
            raise BindingContractError("enabled must be a boolean")
        try:
            validated_endpoint = (endpoint_policy or EndpointPolicy()).validate(
                configuration.get("endpoint")
            )
        except EndpointPolicyError as exc:
            raise BindingContractError(str(exc)) from exc
        return cls(
            revision_id=_text(revision_id, "revision_id"),
            workspace_id=_text(workspace_id, "workspace_id"),
            endpoint=validated_endpoint,
            connector_type=connector_type,
            service_api_version=service_api_version,
            service_credential_ref=credential_ref,
            capability_fingerprint=fingerprint,
            allowed_families=MappingProxyType(families),
            neutral_limits=MappingProxyType(limits),
            execution_profile_ref=profile,
            enabled=enabled,
        )

    def to_configuration(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "connector_type": self.connector_type,
            "service_api_version": self.service_api_version,
            "service_credential_ref": self.service_credential_ref,
            "capability_fingerprint": self.capability_fingerprint,
            "allowed_families": {
                name: list(versions)
                for name, versions in self.allowed_families.items()
            },
            "neutral_limits": dict(self.neutral_limits),
            "execution_profile_ref": self.execution_profile_ref,
            "enabled": self.enabled,
        }

    def assert_capabilities(self, payload: Mapping[str, Any]) -> None:
        fingerprint = "sha256:" + hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        if fingerprint != self.capability_fingerprint:
            raise BindingContractError("solver capability drift detected")


__all__ = ["BindingContractError", "SolverBindingRevision"]


class DurableSolverBindingResolver:
    """Resolve one enabled Solver Service binding without exposing it to packages.

    An administrator may pin ``revision_id`` for backwards compatibility.  If
    it is omitted, all revisions in the workspace/owner scope are evaluated
    against the requested family/version and capability requirements.  The
    resolver never ranks or silently prefers a service: exactly one
    compatible enabled revision is required.
    """

    def __init__(
        self,
        *,
        store: Any,
        revision_id: str | None = None,
        endpoint_policy: EndpointPolicy | None = None,
    ) -> None:
        self.store = store
        self.revision_id = (
            _text(revision_id, "revision_id") if revision_id is not None else None
        )
        # Runtime assembly historically used this sentinel before dynamic
        # selection was introduced.  Keep it observable for compatibility,
        # but treat it exactly like an omitted administrator pin.
        self._dynamic_selection = self.revision_id in {
            None,
            "connector-binding-unconfigured",
        }
        self.endpoint_policy = endpoint_policy or EndpointPolicy()

    def resolve(self, *, workspace_id: str, owner_id: str, **identity: Any) -> dict[str, Any]:
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise BindingContractError("workspace scope is invalid")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise BindingContractError("owner scope is invalid")

        if not self._dynamic_selection:
            records = [
                self.store.get_binding_revision(
                    self.revision_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            ]
        else:
            list_revisions = getattr(self.store, "list_binding_revisions", None)
            if not callable(list_revisions):
                raise BindingContractError("solver binding revisions cannot be listed")
            records = list_revisions(workspace_id=workspace_id, owner_id=owner_id)

        requested_family, requested_version = _family_and_version(
            _identity_value(identity, "problem_family"),
            _identity_value(identity, "family_version"),
        )
        required_capabilities = _required_capabilities(identity)
        supported_capabilities = _declared_supported_capabilities(identity)
        requested_limits = _requested_limits(identity)
        requested_fingerprint = _identity_value(identity, "capability_fingerprint")
        if requested_fingerprint is not None and (
            not isinstance(requested_fingerprint, str)
            or _DIGEST_RE.fullmatch(requested_fingerprint) is None
        ):
            raise BindingContractError("capability fingerprint requirement is invalid")
        candidates: list[SolverBindingRevision] = []
        saw_disabled = False
        saw_incompatible = False
        for record in records:
            if not isinstance(record, Mapping):
                continue
            revision = record.get("binding_revision_id")
            if not isinstance(revision, str) or not revision.strip():
                continue
            try:
                binding = SolverBindingRevision.from_configuration(
                    revision_id=revision,
                    workspace_id=workspace_id,
                    configuration=record.get("configuration"),
                    endpoint_policy=self.endpoint_policy,
                )
            except BindingContractError:
                # A malformed persisted revision cannot become a compatible
                # candidate.  Explicitly pinned revisions retain a precise
                # contract error below; dynamic resolution treats it as no
                # compatible administrator configuration.
                if not self._dynamic_selection:
                    raise
                continue
            if not binding.enabled:
                saw_disabled = True
                continue
            if requested_family is not None:
                versions = _binding_versions_for_family(binding, requested_family)
                if not isinstance(versions, tuple) or (
                    requested_version is not None and requested_version not in versions
                ):
                    saw_incompatible = True
                    continue
            if (
                requested_fingerprint is not None
                and binding.capability_fingerprint != requested_fingerprint
            ):
                saw_incompatible = True
                continue
            if supported_capabilities is not None and not set(required_capabilities).issubset(
                supported_capabilities
            ):
                saw_incompatible = True
                continue
            if requested_limits is not None:
                for name, value in requested_limits.items():
                    ceiling = binding.neutral_limits.get(name)
                    if isinstance(value, bool) or not isinstance(value, int):
                        saw_incompatible = True
                        break
                    if not isinstance(ceiling, int) or value > ceiling:
                        saw_incompatible = True
                        break
                else:
                    candidates.append(binding)
                continue
            # Binding revisions deliberately store no engine capability list.
            # Required capabilities are checked against the live Solver Service
            # descriptor/fingerprint handshake, not guessed from an opaque
            # execution profile here.
            candidates.append(binding)

        if not self._dynamic_selection:
            if not candidates:
                if saw_disabled:
                    raise BindingContractError("solver binding revision is disabled")
                if saw_incompatible:
                    raise BindingContractError("solver binding revision is incompatible")
                raise BindingContractError("solver binding revision is not configured")
        elif not candidates:
            raise BindingContractError("no compatible solver binding is configured")
        elif len(candidates) > 1:
            raise BindingContractError("solver binding selection is ambiguous")

        binding = candidates[0]
        return {
            "binding_revision_id": binding.revision_id,
            "configuration": binding.to_configuration(),
        }


__all__.append("DurableSolverBindingResolver")
