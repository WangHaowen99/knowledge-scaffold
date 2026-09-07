"""Shared protocol admission hook for request-scoped run preflight."""

from __future__ import annotations

from typing import Any


def _resolve_preflight_builder(runtime: Any):
    explicit = getattr(runtime, "protocol_preflight_builder", None)
    if explicit is not None:
        if not callable(explicit):
            raise TypeError("protocol_preflight_builder must be callable")
        return explicit

    from agent.runtime.runtime import RuntimeContainer

    if not isinstance(runtime, RuntimeContainer):
        return None

    from api.routes import get_orchestrator

    builder = getattr(get_orchestrator(), "build_run_preflight_snapshot", None)
    if not callable(builder):
        raise RuntimeError("run preflight builder is unavailable")
    return builder


async def build_protocol_preflight_snapshot(
    runtime: Any,
    *,
    user_request: str,
    user_context: dict[str, Any],
    arguments: dict[str, Any] | None,
    session_id: str,
    model_profile_id: str | None,
    user_id: str,
    workspace_id: str,
):
    """Build once for production while allowing deliberately minimal test fakes."""

    builder = _resolve_preflight_builder(runtime)
    if builder is None:
        return None
    return await runtime.short_io.run(
        builder,
        user_request=user_request,
        user_context=user_context,
        arguments=arguments,
        session_id=session_id,
        model_profile_id=model_profile_id,
        user_id=user_id,
        workspace_id=workspace_id,
    )
