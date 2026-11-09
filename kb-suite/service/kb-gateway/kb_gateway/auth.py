"""Service keys for gateway callers.

Keys are configured as ``KBGW_KEYS="plugin=KBS_PLUGIN_KEY:manage,ingest,retrieve;mcp=..."``:
each entry names the env var holding the key and the capabilities it grants, so
no key value lives in the gateway config. The gateway has a single tenant.
"""

from __future__ import annotations

import hashlib
import hmac
import os
from dataclasses import dataclass, field
from typing import Iterable, Mapping

CAP_MANAGE = "manage"
CAP_INGEST = "ingest"
CAP_RETRIEVE = "retrieve"
CAP_REPROCESS = "reprocess"
CAPABILITIES = frozenset({CAP_MANAGE, CAP_INGEST, CAP_RETRIEVE, CAP_REPROCESS})


class AuthError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


@dataclass(frozen=True)
class Principal:
    name: str
    capabilities: frozenset[str]
    # empty = every knowledge base; set by X-KB-Scope on MCP connections
    kb_allowlist: tuple[str, ...] = field(default=())

    def require(self, capability: str) -> None:
        if capability not in self.capabilities:
            raise AuthError(403, f"key {self.name!r} lacks capability {capability!r}")


@dataclass(frozen=True)
class _Key:
    name: str
    digest: bytes
    capabilities: frozenset[str]


def _digest(value: str) -> bytes:
    return hashlib.sha256(value.encode("utf-8")).digest()


def parse_keys(spec: str, env: Mapping[str, str] | None = None) -> list[_Key]:
    env = os.environ if env is None else env
    keys = []
    for entry in (part.strip() for part in spec.split(";")):
        if not entry:
            continue
        name, _, rest = entry.partition("=")
        var, _, caps = rest.partition(":")
        name, var = name.strip(), var.strip()
        capabilities = frozenset(c.strip() for c in caps.split(",") if c.strip())
        unknown = capabilities - CAPABILITIES
        if not all((name, var, capabilities)) or unknown:
            raise ValueError(f"bad KBGW_KEYS entry {entry!r} (unknown capabilities: {sorted(unknown)})")
        value = env.get(var, "")
        if not value:
            raise ValueError(f"KBGW_KEYS entry {name!r}: env var {var} is empty")
        keys.append(_Key(name, _digest(value), capabilities))
    return keys


class KeyRing:
    def __init__(self, keys: Iterable[_Key]) -> None:
        self._keys = list(keys)

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthError(401, "missing bearer token")
        digest = _digest(token)
        for key in self._keys:
            if hmac.compare_digest(key.digest, digest):
                return Principal(key.name, key.capabilities)
        raise AuthError(401, "invalid key")


def extract_token(headers: Mapping[str, str]) -> str | None:
    auth = headers.get("authorization") or headers.get("Authorization") or ""
    if auth.lower().startswith("bearer "):
        return auth[7:].strip() or None
    return None


def parse_scope_header(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.split(",") if part.strip()]


def narrow(principal: Principal, requested: Iterable[str]) -> Principal:
    wanted = tuple(dict.fromkeys(kb for kb in requested if kb))
    if not wanted:
        return principal
    if principal.kb_allowlist:
        wanted = tuple(kb for kb in wanted if kb in principal.kb_allowlist)
        if not wanted:
            raise AuthError(403, "X-KB-Scope is outside the key's knowledge bases")
    return Principal(principal.name, principal.capabilities, wanted)


def check_scope(principal: Principal, kb_ids: Iterable[str]) -> list[str]:
    """Drop nothing silently: every requested kb must be allowed."""

    kb_ids = list(dict.fromkeys(kb_ids))
    if principal.kb_allowlist:
        outside = [kb for kb in kb_ids if kb not in principal.kb_allowlist]
        if outside:
            raise AuthError(403, f"knowledge bases outside this connection's scope: {', '.join(outside)}")
    return kb_ids
