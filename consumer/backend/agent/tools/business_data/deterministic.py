"""Revision-pinned business data bindings for deterministic handlers."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional
from agent.tools.business_data.resolver import (
    BusinessDataConnectionError,
    resolve_business_data_source,
)
from agent.tools.business_data.endpoint_security import (
    endpoint_has_forbidden_auth_material,
)
from integration.sandbox.data_proxy import sandbox_reachable_url
from integration.sandbox.errors import SandboxUnavailableError
from ontology.operation_execution import canonical_json_bytes, sha256_bytes
from shared_capabilities.business_data import AuthSpec, EffectiveConnection


class DeterministicProviderError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


MIN_DETERMINISTIC_PROVIDER_SECRET_LENGTH = 8


@dataclass(frozen=True)
class DeterministicProviderBinding:
    provider: str
    source_ref: str
    data_revision: str
    scope_digest: str
    network_endpoint: str
    effective_connection: EffectiveConnection


def resolve_deterministic_provider_binding(
    *,
    workspace_id: Optional[str],
    ontology_id: str,
    ontology_version: str,
) -> DeterministicProviderBinding:
    """Resolve one physically scoped, read-only graph snapshot.

    Existing model-backed providers are intentionally ineligible.  For graph
    providers the configured selector must be the actual database/graph used by
    the effective connection, so ``data_revision`` is not merely an audit tag.
    """
    if not str(workspace_id or "").strip():
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Deterministic workspace provider requires a workspace.",
        )
    try:
        source = resolve_business_data_source(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
        )
    except BusinessDataConnectionError as exc:
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Deterministic workspace provider is not configured.",
        ) from exc

    provider = str(source.provider or "").strip().casefold()
    if provider not in {"neo4j", "hugegraph"}:
        raise DeterministicProviderError(
            "provider_not_deterministic",
            "Provider '{}' does not support deterministic snapshots.".format(provider),
        )
    config = dict(source.config or {})
    revision = str(config.get("data_revision") or "").strip()
    if not revision:
        raise DeterministicProviderError(
            "dependency_revision_missing",
            "Deterministic provider data_revision is required.",
        )
    access_scope_revision = str(
        config.get("access_scope_revision") or ""
    ).strip()
    if not access_scope_revision:
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Deterministic provider access_scope_revision is required.",
        )
    if not bool(config.get("deterministic_read_only", False)):
        raise DeterministicProviderError(
            "provider_not_deterministic",
            "Deterministic provider must use a read-only snapshot.",
        )
    if not bool(config.get("deterministic_snapshot_immutable", False)):
        raise DeterministicProviderError(
            "provider_snapshot_unverified",
            "Deterministic provider must use an operator-attested immutable snapshot.",
        )
    if not bool(config.get("deterministic_scope_isolated", False)):
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Deterministic provider must use a workspace and ontology isolated snapshot.",
        )
    selector = str(config.get("snapshot_selector") or "").strip()
    if provider == "neo4j":
        physical_target = str(config.get("database") or "neo4j").strip()
    else:
        graph = str(config.get("graph") or "hugegraph").strip()
        graph_space = str(config.get("graph_space") or "").strip()
        physical_target = (
            "{}/{}".format(graph_space, graph) if graph_space else graph
        )
    if not selector or selector != physical_target:
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Provider snapshot_selector must match the complete effective database or graph target.",
        )

    workspace = str(workspace_id or "").strip()
    scope = {
        "workspace_id": workspace,
        "ontology_id": str(ontology_id or "").strip(),
        "ontology_version": str(ontology_version or "").strip(),
        "provider": provider,
        "snapshot_selector": selector,
        "physical_target": physical_target,
        "data_revision": revision,
        "access_scope_revision": access_scope_revision,
    }
    connection = _build_deterministic_effective_connection(
        provider=provider,
        config=config,
        scope=scope,
    )
    try:
        network_endpoint = sandbox_reachable_url(connection.endpoint)
    except (OSError, SandboxUnavailableError, ValueError) as exc:
        raise DeterministicProviderError(
            "provider_endpoint_unresolved",
            "Deterministic provider endpoint could not be resolved safely.",
        ) from exc
    for candidate in (connection.auth.token, connection.auth.password):
        if candidate and len(candidate) < MIN_DETERMINISTIC_PROVIDER_SECRET_LENGTH:
            raise DeterministicProviderError(
                "provider_credentials_unsafe",
                "Deterministic provider credentials do not meet the minimum length.",
            )
    # All non-secret effective connection options are part of the locked scope.
    # A resume must not switch graph-space, database, endpoint, or another
    # behavior-affecting option that happens to reuse the same revision label.
    connection_identity = {
        "version": connection.version,
        "provider": connection.provider,
        "endpoint": connection.endpoint,
        "network_endpoint": network_endpoint,
        "auth_type": connection.auth.type,
        # The principal name is not a credential and must be bound directly.
        # ``access_scope_revision`` remains the operator-owned permission
        # revision (and the only available principal marker for bearer tokens),
        # while password/token rotations intentionally leave this digest stable.
        "auth_username": connection.auth.username,
        "options": connection.options,
    }
    return DeterministicProviderBinding(
        provider=provider,
        source_ref="business_data",
        data_revision=revision,
        scope_digest=sha256_bytes(canonical_json_bytes(connection_identity)),
        network_endpoint=network_endpoint,
        effective_connection=connection,
    )


def _build_deterministic_effective_connection(
    *,
    provider: str,
    config: dict,
    scope: dict,
) -> EffectiveConnection:
    """Build an exact, revision-pinned graph connection for the handler.

    The generic effective-connection converter is optimized for HTTP model
    providers.  Graph providers use ``uri``/basic credentials, so converting
    them explicitly avoids an empty Neo4j endpoint and keeps credentials out of
    the free-form options mapping.
    """
    options = {
        key: value
        for key, value in config.items()
        if key not in {"url", "uri", "token", "username", "password"}
    }
    options.update(scope)
    options["deterministic_read_only"] = True
    options["deterministic_snapshot_immutable"] = True
    options["deterministic_scope_isolated"] = True
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    token = str(config.get("token") or "").strip()
    if token:
        auth = AuthSpec(type="bearer", token=token)
    elif username or password:
        auth = AuthSpec(
            type="mindsdb_password",
            username=username,
            password=password,
        )
    else:
        auth = AuthSpec(type="none")
    endpoint = str(
        config.get("uri") if provider == "neo4j" else config.get("url")
    ).strip().rstrip("/")
    if not endpoint:
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Deterministic provider endpoint is required.",
        )
    if endpoint_has_forbidden_auth_material(endpoint):
        raise DeterministicProviderError(
            "provider_scope_invalid",
            "Deterministic provider credentials must use dedicated auth fields and fragments are forbidden.",
        )
    return EffectiveConnection(
        version=1,
        provider=provider,
        endpoint=endpoint,
        auth=auth,
        options=options,
    )
