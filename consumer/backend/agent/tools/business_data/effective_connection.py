"""Control-plane conversion into caller-neutral effective connections."""

from __future__ import annotations

from shared_capabilities.business_data import AuthSpec, EffectiveConnection

from agent.tools.business_data.resolver import ResolvedBusinessDataSource


def build_effective_connection(
    source: ResolvedBusinessDataSource,
    *,
    provider_override: str | None = None,
) -> EffectiveConnection:
    config = dict(source.config or {})
    provider = str(provider_override or source.provider or "").strip().lower()
    endpoint = str(config.pop("url", "") or "").strip().rstrip("/")
    token = str(config.pop("token", "") or "").strip()
    if provider == "mindsdb":
        requires_auth = bool(config.pop("requires_auth", False))
        username = str(config.pop("login", "") or "mindsdb").strip() or "mindsdb"
        password = str(config.pop("password", "") or "")
        auth = (
            AuthSpec(type="mindsdb_password", username=username, password=password)
            if requires_auth
            else AuthSpec(type="none")
        )
    else:
        auth = AuthSpec(type="bearer", token=token) if token else AuthSpec(type="none")
    return EffectiveConnection(
        version=1,
        provider=provider,
        endpoint=endpoint,
        auth=auth,
        options=config,
    )
