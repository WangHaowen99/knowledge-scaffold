# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Shared ToolContext construction."""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Optional

from integration.observability import NOOP_OBSERVER
from agent.runtime.preflight_cache import materialize_preflight_product
from agent.tools.base import ToolContext
from .interaction_policy import user_interaction_supported_from_context
from .modes import resolve_agent_mode
from .ontology_context import (
    active_ontology_id_from_state,
    active_ontology_version_from_state,
    selected_ontology_id_from_state,
)


@dataclass(frozen=True, slots=True)
class _OntologyPreflightToolProducts:
    workspace_id: str
    ontology_id: str
    ontology_version: str
    manifest: Any
    source_signature: Any
    content_revision: Any
    script_bindings: Any
    mutation_revision: Any
    execute_script_tool_schema: Any
    solver_capabilities: Any


_ONTOLOGY_PREFLIGHT_SNAPSHOT: ContextVar[
    _OntologyPreflightToolProducts | None
] = ContextVar(
    "ontology_preflight_tool_snapshot",
    default=None,
)


@contextmanager
def ontology_preflight_tool_context(snapshot: Any) -> Iterator[None]:
    """Bind admitted package products to tools without copying them into state."""

    products = (
        _OntologyPreflightToolProducts(
            workspace_id=str(getattr(snapshot, "workspace_id", "") or "").strip(),
            ontology_id=str(getattr(snapshot, "ontology_id", "") or "").strip(),
            ontology_version=str(
                getattr(snapshot, "ontology_version", "") or ""
            ).strip(),
            manifest=materialize_preflight_product(
                getattr(snapshot, "ontology_manifest", None)
            ),
            source_signature=materialize_preflight_product(
                getattr(snapshot, "ontology_source_signature", None)
            ),
            content_revision=getattr(
                snapshot,
                "ontology_content_revision",
                None,
            ),
            script_bindings=materialize_preflight_product(
                getattr(snapshot, "script_bindings", None)
            ),
            mutation_revision=getattr(snapshot, "mutation_revision", None),
            execute_script_tool_schema=materialize_preflight_product(
                getattr(snapshot, "execute_script_tool_schema", None)
            ),
            solver_capabilities=materialize_preflight_product(
                getattr(snapshot, "solver_capabilities", None)
            ),
        )
        if snapshot is not None
        else None
    )
    token = _ONTOLOGY_PREFLIGHT_SNAPSHOT.set(products)
    try:
        yield
    finally:
        _ONTOLOGY_PREFLIGHT_SNAPSHOT.reset(token)


def current_ontology_preflight_tool_fields() -> dict[str, Any]:
    """Return private ToolContext fields for the current async task."""

    return _ontology_preflight_tool_fields(_ONTOLOGY_PREFLIGHT_SNAPSHOT.get())


def _ontology_preflight_tool_fields(
    products: _OntologyPreflightToolProducts | None,
) -> dict[str, Any]:
    if products is None:
        return {
            "preflight_ontology_manifest": None,
            "preflight_ontology_source_signature": None,
            "preflight_ontology_content_revision": None,
            "preflight_ontology_script_bindings": None,
            "preflight_ontology_mutation_revision": None,
            "preflight_execute_script_tool_schema": None,
            "preflight_solver_capabilities": None,
        }
    return {
        "preflight_ontology_manifest": products.manifest,
        "preflight_ontology_source_signature": products.source_signature,
        "preflight_ontology_content_revision": products.content_revision,
        "preflight_ontology_script_bindings": products.script_bindings,
        "preflight_ontology_mutation_revision": products.mutation_revision,
        "preflight_execute_script_tool_schema": products.execute_script_tool_schema,
        "preflight_solver_capabilities": products.solver_capabilities,
    }


def current_ontology_preflight_tool_products_for_context(
    *,
    workspace_id: Optional[str],
    ontology_id: Optional[str],
    ontology_version: Optional[str],
) -> _OntologyPreflightToolProducts | None:
    """Return admitted products only for the exact selected package identity."""

    products = _ONTOLOGY_PREFLIGHT_SNAPSHOT.get()
    expected = (
        str(workspace_id or "").strip(),
        str(ontology_id or "").strip(),
        str(ontology_version or "").strip(),
    )
    if (
        products is None
        or not all(expected)
        or expected
        != (
            products.workspace_id,
            products.ontology_id,
            products.ontology_version,
        )
    ):
        return None
    manifest = products.manifest
    if (
        not isinstance(manifest, Mapping)
        or str(manifest.get("id") or "").strip() != products.ontology_id
        or str(manifest.get("version") or "").strip()
        != products.ontology_version
    ):
        return None
    return products


def ontology_preflight_tool_fields_for_context(
    *,
    workspace_id: Optional[str],
    ontology_id: Optional[str],
    ontology_version: Optional[str],
) -> dict[str, Any]:
    """Project private fields only for the exact ToolContext identity."""

    return _ontology_preflight_tool_fields(
        current_ontology_preflight_tool_products_for_context(
            workspace_id=workspace_id,
            ontology_id=ontology_id,
            ontology_version=ontology_version,
        )
    )


def build_tool_context(
    state: Mapping[str, Any],
    *,
    memory_manager: Any,
    agent_config: Any,
    caller: str,
    agent_iteration: int,
    workspace_id: Optional[str],
    project_root: Optional[str],
    tool_event_emitter: Any = None,
    protocol_event_observer: Any = None,
    subagent_manager: Any = None,
    subagent_lineage: Any = None,
    parent_tool_call_id: Any = None,
    observer: Any = None,
    traceparent: Optional[str] = None,
) -> ToolContext:
    user_context = dict(state.get("user_context") or {}) if isinstance(state, Mapping) else {}
    context_kwargs = dict(
        memory_manager=memory_manager,
        messages=list(state.get("messages") or []),
        run_id=state.get("run_id"),
        session_id=state.get("session_id"),
        turn_id=int(state.get("current_turn_id") or 0),
        user_id=str(state.get("user_id") or "").strip() or None,
        agent_config=agent_config,
        caller=caller,
        traceparent=traceparent or (str(state.get("traceparent") or "").strip() or None),
        agent_mode=resolve_agent_mode(user_context),
        agent_iteration=agent_iteration,
        workspace_id=workspace_id,
        project_root=project_root,
        event_emitter=tool_event_emitter,
        parent_tool_call_id=parent_tool_call_id,
        subagent_manager=subagent_manager,
        subagent_lineage=subagent_lineage,
        protocol_event_observer=protocol_event_observer,
        read_documents=state.get("read_documents"),
        selected_ontology_id=active_ontology_id_from_state(state),
        selected_ontology_version=active_ontology_version_from_state(state),
        selected_ontology_is_explicit=bool(selected_ontology_id_from_state(state)),
        model_profile_id=str(user_context.get("model_profile_id") or "").strip() or None,
        observer=observer or NOOP_OBSERVER,
        supports_user_interaction=user_interaction_supported_from_context(user_context),
    )
    context_kwargs.update(
        ontology_preflight_tool_fields_for_context(
            workspace_id=workspace_id,
            ontology_id=context_kwargs["selected_ontology_id"],
            ontology_version=context_kwargs["selected_ontology_version"],
        )
    )
    return ToolContext(**context_kwargs)
