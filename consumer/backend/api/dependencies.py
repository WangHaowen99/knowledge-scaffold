"""Native FastAPI dependencies shared by Consumer API routes."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

from fastapi import Depends, HTTPException, Request

from agent.runtime.offload import OffloadPoolClosed, OffloadPoolSaturated
from api.auth.auth import (
    AuthUser,
    require_admin_user,
    require_authenticated_user,
)
from api.exceptions import APIError
from workspace.workspaces import (
    WorkspaceNotFoundError,
    enforce_workspace_access,
    enforce_workspace_owner_or_admin,
    resolve_workspace_id,
    resolve_workspace_owner_or_admin_id,
    workspace_scope,
)


def _is_isolated_route_test(request: Request) -> bool:
    app_type = type(request.app)
    return (
        app_type.__module__ == "api.testing"
        and app_type.__name__ == "TestApplication"
    )


async def run_in_runtime_pool(
    request: Request,
    pool_name: str,
    function: Callable[..., Any],
    *args: Any,
    **kwargs: Any,
) -> Any:
    """Run blocking route work in a lifespan-owned bounded pool.

    Focused router tests intentionally assemble a tiny app without a lifespan;
    they may execute the synthetic/local callable inline. Production requests
    fail closed when the process runtime is unavailable.
    """
    runtime = getattr(request.app.state, "runtime_container", None)
    if runtime is None and _is_isolated_route_test(request):
        runtime = getattr(
            request.app.state,
            "agent_execution_runtime",
            None,
        )
    pool = getattr(runtime, pool_name, None) if runtime is not None else None
    if pool is None:
        if _is_isolated_route_test(request):
            return function(*args, **kwargs)
        raise HTTPException(status_code=503, detail="Runtime is not available")
    try:
        return await pool.run(function, *args, **kwargs)
    except OffloadPoolSaturated as exc:
        raise HTTPException(
            status_code=429,
            detail="Runtime offload capacity is exhausted",
        ) from exc
    except OffloadPoolClosed as exc:
        raise HTTPException(status_code=503, detail="Runtime is draining") from exc


def get_runtime_container(request: Request):
    """Resolve the lifespan-owned runtime, with an isolated-test fallback."""
    runtime = getattr(request.app.state, "runtime_container", None)
    if runtime is None and _is_isolated_route_test(request):
        runtime = getattr(
            request.app.state,
            "agent_execution_runtime",
            None,
        )
    if runtime is None:
        raise HTTPException(status_code=503, detail="Runtime is not available")
    return runtime


async def require_workspace_id(
    request: Request,
    _user: AuthUser = Depends(require_authenticated_user),
) -> AsyncIterator[str]:
    """Authorize, resolve, and bind the request workspace for one endpoint."""
    try:
        workspace_id = await run_in_runtime_pool(
            request,
            "short_io",
            resolve_workspace_id,
            request,
        )
    except WorkspaceNotFoundError as exc:
        raise APIError(404, exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with workspace_scope(workspace_id):
        yield workspace_id


async def require_workspace_path_access(
    workspace_id: str,
    request: Request,
    _user: AuthUser = Depends(require_authenticated_user),
) -> AsyncIterator[str]:
    """Authorize and bind a workspace selected by a route path parameter."""
    try:
        resolved_id = await run_in_runtime_pool(
            request,
            "short_io",
            enforce_workspace_access,
            workspace_id,
        )
    except WorkspaceNotFoundError as exc:
        raise APIError(404, exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with workspace_scope(resolved_id):
        yield resolved_id


async def require_workspace_path_owner_or_admin(
    workspace_id: str,
    request: Request,
    _user: AuthUser = Depends(require_authenticated_user),
) -> AsyncIterator[str]:
    """Bind a path workspace after owner-or-global-admin authorization."""
    try:
        resolved_id = await run_in_runtime_pool(
            request,
            "short_io",
            enforce_workspace_owner_or_admin,
            workspace_id,
        )
    except WorkspaceNotFoundError as exc:
        raise APIError(404, exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with workspace_scope(resolved_id):
        yield resolved_id


async def require_workspace_owner_or_admin_id(
    request: Request,
    _user: AuthUser = Depends(require_authenticated_user),
) -> AsyncIterator[str]:
    """Bind a workspace after enforcing owner-or-global-admin authorization."""
    try:
        workspace_id = await run_in_runtime_pool(
            request,
            "short_io",
            resolve_workspace_owner_or_admin_id,
            request,
        )
    except WorkspaceNotFoundError as exc:
        raise APIError(404, exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with workspace_scope(workspace_id):
        yield workspace_id


async def require_admin_workspace_id(
    request: Request,
    _user: AuthUser = Depends(require_admin_user),
) -> AsyncIterator[str]:
    """Resolve and bind a request workspace for a global-admin endpoint."""
    try:
        workspace_id = await run_in_runtime_pool(
            request,
            "short_io",
            resolve_workspace_id,
            request,
        )
    except WorkspaceNotFoundError as exc:
        raise APIError(404, exc.to_dict()) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    with workspace_scope(workspace_id):
        yield workspace_id
