from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from .. import auth
from ..auth import AuthError, Principal


@dataclass(frozen=True)
class Caller:
    principal: Principal
    tenant_id: str

    async def kb_scope(self, kb_ids: list[str] | None, *, allow_all_when_empty: bool = True) -> list[str]:
        return await auth.resolve_kb_scope(
            self.principal, self.tenant_id, kb_ids, allow_all_when_empty=allow_all_when_empty
        )

    async def check_kb(self, kb_id: str) -> None:
        await auth.resolve_kb_scope(self.principal, self.tenant_id, [kb_id], allow_all_when_empty=False)


async def caller_from_request(request: Request, capability: str) -> Caller:
    principal = await auth.principal_from_token(auth.extract_token(request.headers))
    principal.require(capability)
    tenant = auth.resolve_tenant(principal, request.headers.get("x-tenant-id"))
    return Caller(principal, tenant)


def need(capability: str):
    async def dependency(request: Request) -> Caller:
        return await caller_from_request(request, capability)

    return dependency


def error_body(code: str, message: str, **extra) -> dict:
    return {"error": {"code": code, "message": message, **extra}}


__all__ = ["AuthError", "Caller", "caller_from_request", "error_body", "need"]
