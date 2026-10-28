"""Service-key authentication and tenant / knowledge-base scope checks.

The capability and allowlist model follows WeKnora's tenant API keys
(internal/types/tenant_api_key.go): a key carries explicit capabilities and an
optional knowledge-base allowlist; routes deny by default.
"""

from __future__ import annotations

import dataclasses

import hashlib
import secrets
from dataclasses import dataclass
from typing import Iterable, Mapping

from . import db

CAP_MANAGE = "manage"
CAP_INGEST = "ingest"
CAP_RETRIEVE = "retrieve"
CAP_REPROCESS = "reprocess"
ALL_CAPS = frozenset({CAP_MANAGE, CAP_INGEST, CAP_RETRIEVE, CAP_REPROCESS})

KEY_PREFIX = "kbs_"


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Principal:
    key_id: str
    name: str
    tenant_id: str | None
    capabilities: frozenset[str]
    kb_allowlist: tuple[str, ...]
    can_assert_tenant: bool

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise AuthError(403, f"service key lacks capability: {capability}")


def new_key() -> str:
    return KEY_PREFIX + secrets.token_urlsafe(32)


def hash_key(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def extract_token(headers: Mapping[str, str]) -> str | None:
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        token = auth[7:].strip()
        return token or None
    key = headers.get("x-api-key") or headers.get("X-API-Key")
    return key.strip() if key else None


async def principal_from_token(token: str | None) -> Principal:
    if not token:
        raise AuthError(401, "missing service key")
    row = await db.pool().fetchrow(
        "SELECT id, name, tenant_id, capabilities, kb_allowlist, can_assert_tenant, disabled"
        " FROM service_keys WHERE key_hash = $1",
        hash_key(token),
    )
    if row is None or row["disabled"]:
        raise AuthError(401, "invalid service key")
    return Principal(
        key_id=row["id"],
        name=row["name"],
        tenant_id=row["tenant_id"],
        capabilities=frozenset(row["capabilities"]),
        kb_allowlist=tuple(row["kb_allowlist"]),
        can_assert_tenant=row["can_assert_tenant"],
    )


def resolve_tenant(principal: Principal, header_tenant: str | None) -> str:
    """Tenant comes from the key unless the key may assert it via header."""

    header_tenant = (header_tenant or "").strip() or None
    if principal.can_assert_tenant:
        tenant = header_tenant or principal.tenant_id
        if not tenant:
            raise AuthError(400, "X-Tenant-Id is required for this service key")
        return tenant
    if principal.tenant_id is None:
        raise AuthError(403, "service key is not bound to a tenant")
    if header_tenant and header_tenant != principal.tenant_id:
        raise AuthError(403, "X-Tenant-Id does not match the service key tenant")
    return principal.tenant_id


def parse_scope_header(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def narrow_to_scope(principal: Principal, requested: Iterable[str]) -> Principal:
    """Restrict a principal to the knowledge bases a caller declares (``X-KB-Scope``).

    An empty allowlist means "every knowledge base of the tenant", so a narrowing that
    leaves nothing must fail rather than fall back to an empty allowlist.
    """
    wanted = tuple(dict.fromkeys(kb for kb in requested if kb))
    if not wanted:
        return principal
    if principal.kb_allowlist:
        wanted = tuple(kb for kb in wanted if kb in principal.kb_allowlist)
        if not wanted:
            raise AuthError(403, "X-KB-Scope is outside the service key scope")
    return dataclasses.replace(principal, kb_allowlist=wanted)


async def resolve_kb_scope(
    principal: Principal,
    tenant_id: str,
    requested: Iterable[str] | None,
    *,
    allow_all_when_empty: bool,
) -> list[str]:
    """Return the knowledge bases this call may touch.

    Every requested id must exist in the tenant and pass the key allowlist.
    An empty request means "all visible" only when ``allow_all_when_empty``;
    the DA tool surface passes False so an empty scope never reads anything.
    """

    wanted = [kb for kb in dict.fromkeys(requested or []) if kb]
    rows = await db.pool().fetch(
        "SELECT id FROM knowledge_bases WHERE tenant_id = $1 AND deleted_at IS NULL",
        tenant_id,
    )
    tenant_kbs = {r["id"] for r in rows}
    allow = set(principal.kb_allowlist)
    visible = tenant_kbs & allow if allow else tenant_kbs
    if not wanted:
        if not allow_all_when_empty:
            raise AuthError(403, "knowledge base scope is empty")
        return sorted(visible)
    missing = [kb for kb in wanted if kb not in tenant_kbs]
    if missing:
        raise AuthError(404, f"knowledge base not found: {', '.join(missing)}")
    denied = [kb for kb in wanted if kb not in visible]
    if denied:
        raise AuthError(403, f"knowledge base outside key scope: {', '.join(denied)}")
    return wanted
