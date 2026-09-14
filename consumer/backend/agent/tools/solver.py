"""Request-scoped Solver ToolCards backed by one stable dispatcher."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from hashlib import sha256
import json
import logging
from pathlib import Path
import re
from types import MappingProxyType
import unicodedata
from typing import Any, Dict, Optional, Set

from agent.tools.base import ToolCaller, ToolContext, ToolOutcome
from agent.tools.events import emit_protocol_event
from agent.tools.solver_terminal import SolverTerminalOutcomeProjector
from solver_coordination.canonical import json_schema_digest, sha256_digest


logger = logging.getLogger(__name__)

_SOLVER_COORDINATION_SERVICE: Any = None
_TERMINAL_OUTCOME_PROJECTOR = SolverTerminalOutcomeProjector()


_CARD_NAME_PATTERN = re.compile(r"^[A-Za-z0-9_]+$")
_FORBIDDEN_KEY_PARTS = frozenset(
    {
        "endpoint",
        "token",
        "secret",
        "password",
        "credential",
        "platform",
        "runtimebinding",
        "internalpath",
        "problemir",
    }
)
_PUBLIC_RESULT_FIELDS = frozenset(
    {
        "schema_version",
        "status",
        "code",
        "phase",
        "retryable",
        "run_id",
        "job_id",
        "attempt_id",
        "event_seq",
        "authoritative",
        "feasibility",
        "optimality",
        "proof_kind",
        "summary",
        "result_artifact_ref",
        "verification_evidence_ref",
        "candidate_results_artifact_ref",
        "candidate_results_artifact_digest",
        "knowledge_revision_id",
        "fact_snapshot_digest",
        "problem_digest",
        "missing",
        "ambiguous",
        "errors",
        "questions",
    }
)
_PUBLIC_PROGRESS_FIELDS = frozenset(
    {
        "run_id",
        "job_id",
        "attempt_id",
        "event_seq",
        "phase",
        "status",
        "message",
        "elapsed_ms",
        "percent",
        "code",
        "authoritative",
        "feasibility",
        "optimality",
        "proof_kind",
        "summary",
        "result_artifact_ref",
        "verification_evidence_ref",
        "candidate_results_artifact_ref",
        "candidate_results_artifact_digest",
        "knowledge_revision_id",
        "fact_snapshot_digest",
        "problem_digest",
    }
)


class SolverToolCatalogError(ValueError):
    """Raised when virtual ToolCards cannot be exposed without ambiguity."""


def configure_solver_tool_service(service: Any) -> None:
    """Install the app-scoped coordination service used by virtual cards."""

    global _SOLVER_COORDINATION_SERVICE
    _SOLVER_COORDINATION_SERVICE = service


def recover_solver_tool_result(
    *,
    workspace_id: str,
    owner_id: str,
    run_id: str,
    turn_id: int,
    tool_call_id: str,
    card_name: str,
    progress_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> Mapping[str, Any] | None:
    """Complete one exact persisted Solver ToolCall without rebinding input."""

    service = _SOLVER_COORDINATION_SERVICE
    if service is None:
        return {"state": "missing", "link_exists": False}
    link = service.store.get_run_link(
        workspace_id=workspace_id,
        owner_id=owner_id,
        run_id=run_id,
        turn_id=str(turn_id),
        tool_call_id=tool_call_id,
    )
    if link is None:
        return {"state": "missing", "link_exists": False}
    if str(link.get("card_name") or "") != card_name:
        return {
            "state": "conflict",
            "link_exists": True,
            "job_id": str(link.get("job_id") or ""),
        }
    if link.get("tool_result") is None:
        service.recover_job(
            str(link["job_id"]),
            workspace_id=workspace_id,
            owner_id=owner_id,
            progress_callback=progress_callback,
        )
        link = service.store.get_run_link(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=str(turn_id),
            tool_call_id=tool_call_id,
        )
    result = link.get("tool_result") if isinstance(link, Mapping) else None
    result_digest = str(link.get("tool_result_digest") or "") if isinstance(link, Mapping) else ""
    if not isinstance(result, Mapping) or not result_digest:
        return {
            "state": "pending",
            "link_exists": True,
            "job_id": str(link.get("job_id") or ""),
        }
    public = {
        str(key): _thaw_json(value)
        for key, value in result.items()
        if str(key) in _PUBLIC_RESULT_FIELDS
    }
    return {
        "state": "ready",
        "link_exists": True,
        "result": public,
        "result_digest": result_digest,
        "job_id": str(link.get("job_id") or ""),
    }


def dispatch_solver_checkpoint_tool(
    *,
    workspace_id: str,
    owner_id: str,
    session_id: str,
    run_id: str,
    turn_id: int,
    checkpoint: Mapping[str, Any],
    progress_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
) -> Mapping[str, Any] | None:
    """Re-dispatch a checkpoint whose durable run link was never created.

    The pre-dispatch checkpoint is the only state available in this crash
    window.  Reusing its ToolCall id and arguments is safe because the service
    binds the same logical ``run_id + tool_call_id`` idempotency identity.
    """

    call = checkpoint.get("tool_call") if isinstance(checkpoint, Mapping) else None
    state = checkpoint.get("state") if isinstance(checkpoint, Mapping) else None
    if not isinstance(call, Mapping) or not isinstance(state, Mapping):
        return None
    tool_call_id = str(call.get("id") or "").strip()
    tool_name = str(call.get("name") or "").strip()
    arguments = call.get("arguments")
    if not tool_call_id or not tool_name or not isinstance(arguments, Mapping):
        return None
    if _SOLVER_COORDINATION_SERVICE is None:
        payload = _error_outcome(
            "solver_unavailable",
            "submit",
            True,
            run_id=run_id,
        )
        public = _public_result_from_content(payload.content)
        return {
            "result": public,
            "result_digest": sha256_digest(public),
            "job_id": "",
        }

    # Resolve the package identity the same way a live turn does: a session
    # that auto-selected its package stores neither id nor revision, and the
    # checkpoint state is the only identity this recovery path has.
    from agent.core.shared.ontology_context import (
        active_ontology_id_from_state,
        active_ontology_version_from_state,
    )

    identity_state = {
        "user_context": state.get("user_context"),
        "workspace_id": workspace_id,
    }
    selected_id = active_ontology_id_from_state(identity_state) or ""
    selected_revision = active_ontology_version_from_state(identity_state) or ""
    specs = get_solver_tool_specs_for_context(
        selected_ontology_id=selected_id,
        selected_ontology_version=selected_revision,
        workspace_id=workspace_id,
    )
    spec = next((candidate for candidate in specs if candidate.name == tool_name), None)
    if spec is None:
        payload = _error_outcome(
            "solver_contract_drift",
            "resume",
            False,
            run_id=run_id,
        )
        public = _public_result_from_content(payload.content)
        return {
            "result": public,
            "result_digest": sha256_digest(public),
            "job_id": "",
        }
    try:
        from config import AgentConfig

        context = ToolContext(
            memory_manager=None,
            messages=[
                dict(message)
                for message in list(state.get("messages") or [])
                if isinstance(message, Mapping)
            ],
            run_id=run_id,
            session_id=session_id,
            turn_id=turn_id,
            agent_config=AgentConfig.from_env(),
            caller="main",
            user_id=owner_id,
            workspace_id=workspace_id,
            selected_ontology_id=selected_id or None,
            selected_ontology_version=selected_revision or None,
        )
        outcome = spec.execute(
            args=dict(arguments),
            context=context,
            tool_call_id=tool_call_id,
        )
        public = _public_result_from_content(getattr(outcome, "content", None))
        if public is None:
            return None
        return {
            "result": public,
            "result_digest": sha256_digest(public),
            "job_id": str(public.get("job_id") or ""),
        }
    except Exception:
        payload = _error_outcome(
            "solver_unavailable",
            "submit",
            True,
            run_id=run_id,
        )
        public = _public_result_from_content(payload.content)
        return {
            "result": public,
            "result_digest": sha256_digest(public),
            "job_id": "",
        }


def _public_result_from_content(content: Any) -> dict[str, Any] | None:
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, Mapping):
        return None
    return {
        str(key): _thaw_json(value)
        for key, value in payload.items()
        if str(key) in _PUBLIC_RESULT_FIELDS
    }


def project_solver_tool_result(result: Mapping[str, Any]) -> ToolOutcome:
    """Project a persisted or freshly returned Solver result identically."""

    public = {
        str(key): _thaw_json(value)
        for key, value in result.items()
        if str(key) in _PUBLIC_RESULT_FIELDS
    }
    return _TERMINAL_OUTCOME_PROJECTOR.project(public)


def _run_cancel_probe(run_id: str) -> Callable[[], bool] | None:
    """Return the shared Agent Run cancellation probe when the router is active."""

    from llm import get_llm_router

    try:
        router = get_llm_router()
    except RuntimeError:
        return None
    get_cancel_event = getattr(router, "get_cancel_event", None)
    if not callable(get_cancel_event):
        return None
    cancel_event = get_cancel_event(run_id)
    is_set = getattr(cancel_event, "is_set", None)
    return is_set if callable(is_set) else None


def get_solver_tool_specs_for_context(
    *,
    selected_ontology_id: Optional[str],
    selected_ontology_version: Optional[str],
    workspace_id: Optional[str],
) -> tuple["SolverToolSpec", ...]:
    package_id = str(selected_ontology_id or "").strip()
    revision = str(selected_ontology_version or "").strip()
    workspace = str(workspace_id or "").strip()
    # Cards are revision-bound, so an incomplete identity yields no tools at
    # all. Name the missing part instead of letting the cards vanish without a
    # trace, because the symptom points nowhere near the cause.
    if not workspace or not package_id:
        logger.debug(
            "Solver tool cards unavailable: workspace=%r package=%r",
            workspace,
            package_id,
        )
        return ()
    if not revision:
        logger.warning(
            "Solver tool cards unavailable: package %s in workspace %s resolved no revision",
            package_id,
            workspace,
        )
        return ()
    if _SOLVER_COORDINATION_SERVICE is None:
        logger.debug("Solver tool cards unavailable: coordination service not configured")
        return ()
    admitted_capabilities = _admitted_public_capabilities(
        package_id=package_id,
        package_revision=revision,
        workspace_id=workspace,
    )
    capabilities = (
        admitted_capabilities
        if admitted_capabilities is not None
        else _load_public_capabilities(
            package_id=package_id,
            package_revision=revision,
            workspace_id=workspace,
        )
    )
    if not capabilities:
        logger.debug(
            "Solver tool cards unavailable: package %s@%s declares no public capabilities",
            package_id,
            revision,
        )
        return ()
    dispatcher = SolverToolDispatcher(
        service=_SOLVER_COORDINATION_SERVICE,
        binding_resolver=resolve_solver_tool_binding,
    )
    try:
        return build_solver_tool_specs(capabilities, dispatcher=dispatcher)
    except SolverToolCatalogError:
        logger.warning(
            "Solver tool cards unavailable: package %s@%s has an invalid capability catalog",
            package_id,
            revision,
            exc_info=True,
        )
        return ()


def resolve_solver_tool_binding(
    *,
    workspace_id: str,
    package_id: str,
    package_revision: str,
    card_name: str,
) -> Optional["SolverToolBinding"]:
    admitted_capabilities = _admitted_public_capabilities(
        package_id=package_id,
        package_revision=package_revision,
        workspace_id=workspace_id,
    )
    capabilities = (
        admitted_capabilities
        if admitted_capabilities is not None
        else _load_public_capabilities(
            package_id=package_id,
            package_revision=package_revision,
            workspace_id=workspace_id,
        )
    )
    try:
        specs = build_solver_tool_specs(capabilities)
    except SolverToolCatalogError:
        return None
    matches = [spec.binding for spec in specs if spec.name == card_name]
    return matches[0] if len(matches) == 1 else None


def _admitted_public_capabilities(
    *,
    package_id: str,
    package_revision: str,
    workspace_id: str,
) -> tuple[Mapping[str, Any], ...] | None:
    from agent.core.shared.tool_context import (
        current_ontology_preflight_tool_products_for_context,
    )

    admitted = current_ontology_preflight_tool_products_for_context(
        workspace_id=workspace_id,
        ontology_id=package_id,
        ontology_version=package_revision,
    )
    if admitted is None:
        return None
    capabilities = admitted.solver_capabilities
    if not isinstance(capabilities, (list, tuple)):
        return ()
    public = tuple(
        capability
        for capability in capabilities
        if isinstance(capability, Mapping)
    )
    if len(public) != len(capabilities) or any(
        str(capability.get("package_id") or "").strip() != package_id
        or str(capability.get("package_revision") or "").strip()
        != package_revision
        for capability in public
    ):
        return ()
    # A preflight product is the private execution surface.  If a caller
    # accidentally binds the lightweight ``solver_summary`` here, fail closed
    # rather than attempting a live package read or exposing an underspecified
    # ToolCard.
    if any(
        not isinstance(capability.get("input_schema"), Mapping)
        for capability in public
    ):
        return ()
    return public


def _load_public_capabilities(
    *, package_id: str, package_revision: str, workspace_id: str
) -> list[Mapping[str, Any]]:
    """Load full ToolCard capabilities from the locked package revision.

    Root ontology context is intentionally progressive and may contain only
    ``solver_summary``.  Solver ToolCards still require the strict input
    schema, therefore this fallback path resolves the exact manifest and uses
    the package-bound full catalog loader instead of reading a model-facing
    root payload.
    """
    try:
        from ontology.ontology_catalog import get_ontology_manifest
        from solver_coordination.capability_catalog import (
            SolverContractResolver,
        )

        ontology, error = get_ontology_manifest(
            package_id,
            version=package_revision,
            workspace_id=workspace_id,
        )
        if error or ontology is None or ontology.version != package_revision:
            return []
        manifest_path = Path(ontology.manifest_path).resolve()
        if not manifest_path.is_file() or manifest_path.is_symlink():
            return []
        import yaml

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
        if not isinstance(manifest, Mapping):
            return []
        catalog = SolverContractResolver(
            Path(ontology.ontology_dir).resolve(),
            manifest,
            package_revision=package_revision,
        ).load_public()
        return [item for item in catalog if isinstance(item, Mapping)]
    except Exception:
        return []


@dataclass(frozen=True)
class SolverToolBinding:
    card_name: str
    package_id: str
    package_revision: str
    operation_key: str
    contract_digest: str
    schema_digest: str
    input_schema: Mapping[str, Any]


@dataclass(frozen=True)
class SolverToolSpec:
    name: str
    schema: Dict[str, Any]
    binding: SolverToolBinding
    bindings: Mapping[str, SolverToolBinding]
    dispatcher: Optional["SolverToolDispatcher"] = None
    allowed_callers: Set[ToolCaller] = None  # type: ignore[assignment]
    concurrency_safe: bool = False
    max_parallelism: Optional[int] = 1
    category: str = "solver"

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_callers", {"main", "subagent", "acp"})

    def execute(
        self,
        *,
        args: Dict[str, Any],
        context: ToolContext,
        tool_call_id: Optional[str] = None,
        event_emitter: Optional[Callable[..., Any]] = None,
    ) -> ToolOutcome:
        del event_emitter
        if self.dispatcher is None:
            return ToolOutcome(
                content=json.dumps(
                    {
                        "status": "error",
                        "code": "solver_unavailable",
                        "phase": "submit",
                        "retryable": True,
                    },
                    sort_keys=True,
                )
            )
        binding = self.binding
        arguments: Mapping[str, Any] = args
        if self.name == "solve":
            operation_key = str(args.get("operation_key") or "").strip()
            binding = self.bindings.get(operation_key)  # type: ignore[assignment]
            arguments = args.get("arguments") if isinstance(args.get("arguments"), Mapping) else {}
            if binding is None:
                return _error_outcome("solver_contract_drift", "catalog", False)
        return self.dispatcher.dispatch(
            binding=binding,
            arguments=arguments,
            context=context,
            tool_call_id=tool_call_id,
        )


def build_solver_tool_specs(
    capabilities: Sequence[Any],
    *,
    exact_cards: bool = True,
    provider_name_limit: int = 64,
    dispatcher: Optional["SolverToolDispatcher"] = None,
) -> tuple[SolverToolSpec, ...]:
    if isinstance(provider_name_limit, bool) or provider_name_limit < 24:
        raise SolverToolCatalogError("provider tool-name limit is too small")
    bindings = tuple(
        _binding_from_capability(item, provider_name_limit=provider_name_limit)
        for item in capabilities
        if _capability_value(item, "enabled", True) is not False
    )
    names: dict[str, SolverToolBinding] = {}
    operations: dict[str, SolverToolBinding] = {}
    for binding in bindings:
        if binding.card_name in names:
            raise SolverToolCatalogError(
                "solver ToolCard name collision: {}".format(binding.card_name)
            )
        if binding.operation_key in operations:
            raise SolverToolCatalogError(
                "duplicate solver operation: {}".format(binding.operation_key)
            )
        names[binding.card_name] = binding
        operations[binding.operation_key] = binding
    if exact_cards:
        return tuple(
            SolverToolSpec(
                name=binding.card_name,
                schema=_exact_schema(binding),
                binding=binding,
                bindings=MappingProxyType({binding.operation_key: binding}),
                dispatcher=dispatcher,
            )
            for binding in bindings
        )
    if not bindings:
        return ()
    first = bindings[0]
    return (
        SolverToolSpec(
            name="solve",
            schema=_compatibility_schema(bindings),
            binding=first,
            bindings=MappingProxyType(dict(operations)),
            dispatcher=dispatcher,
        ),
    )


def _binding_from_capability(
    capability: Any, *, provider_name_limit: int
) -> SolverToolBinding:
    package_id = _required_capability_text(capability, "package_id")
    package_revision = _required_capability_text(capability, "package_revision")
    operation_key = _required_capability_text(capability, "operation_key")
    contract_digest = _required_capability_text(capability, "contract_digest")
    digest_value = contract_digest.rsplit(":", 1)[-1].lower()
    if not re.fullmatch(r"[0-9a-f]{8,128}", digest_value):
        raise SolverToolCatalogError("contract_digest must contain a hexadecimal digest")
    raw_schema = _capability_value(capability, "input_schema")
    schema = _thaw_json(raw_schema)
    if not isinstance(schema, dict) or schema.get("type") != "object":
        raise SolverToolCatalogError("solver input schema must be an object")
    if schema.get("additionalProperties") is not False:
        raise SolverToolCatalogError(
            "solver input schema must set additionalProperties=false"
        )
    forbidden = _find_forbidden_key(schema)
    if forbidden:
        raise SolverToolCatalogError(
            "solver input schema contains forbidden runtime field: {}".format(forbidden)
        )
    card_name = _card_name(
        operation_key,
        digest_value[:8],
        provider_name_limit=provider_name_limit,
    )
    return SolverToolBinding(
        card_name=card_name,
        package_id=package_id,
        package_revision=package_revision,
        operation_key=operation_key,
        contract_digest=contract_digest,
        schema_digest=json_schema_digest(schema),
        input_schema=_freeze_json(schema),
    )


def _card_name(operation_key: str, digest8: str, *, provider_name_limit: int) -> str:
    normalized = unicodedata.normalize("NFKD", operation_key)
    ascii_key = normalized.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^A-Za-z0-9]+", "_", ascii_key).strip("_").lower()
    if not slug:
        slug = "operation_{}".format(sha256(operation_key.encode("utf-8")).hexdigest()[:8])
    suffix = "_{}".format(digest8)
    available = provider_name_limit - len("solve_") - len(suffix)
    if available < 1:
        raise SolverToolCatalogError("provider tool-name limit is too small")
    slug = slug[:available].rstrip("_") or "operation"[:available]
    name = "solve_{}{}".format(slug, suffix)
    if len(name) > provider_name_limit or not _CARD_NAME_PATTERN.fullmatch(name):
        raise SolverToolCatalogError("solver ToolCard name is invalid")
    return name


def _exact_schema(binding: SolverToolBinding) -> Dict[str, Any]:
    return {
        "type": "function",
        "function": {
            "name": binding.card_name,
            "description": (
                "Solve the selected ontology operation using the platform-bound solver "
                "and return only independently verified results. Only properties listed "
                "in the schema's required array can block execution. If an optional "
                "property was not provided, omit optional properties and call this tool "
                "without asking the user for them."
            ),
            "parameters": _thaw_json(binding.input_schema),
        },
    }


def _compatibility_schema(bindings: Sequence[SolverToolBinding]) -> Dict[str, Any]:
    branches = []
    for binding in bindings:
        branches.append(
            {
                "type": "object",
                "properties": {
                    "operation_key": {"type": "string", "const": binding.operation_key},
                    "arguments": _thaw_json(binding.input_schema),
                },
                "required": ["operation_key", "arguments"],
                "additionalProperties": False,
            }
        )
    return {
        "type": "function",
        "function": {
            "name": "solve",
            "description": "Dispatch one selected ontology solver operation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "operation_key": {
                        "type": "string",
                        "enum": [binding.operation_key for binding in bindings],
                    },
                    "arguments": {"type": "object"},
                },
                "required": ["operation_key", "arguments"],
                "additionalProperties": False,
                "oneOf": branches,
            },
        },
    }


@dataclass(frozen=True)
class SolverToolDispatcher:
    service: Any
    binding_resolver: Callable[..., Any]

    def dispatch(
        self,
        *,
        binding: SolverToolBinding,
        arguments: Mapping[str, Any],
        context: Any,
        tool_call_id: Optional[str],
    ) -> ToolOutcome:
        workspace_id = str(getattr(context, "workspace_id", "") or "").strip()
        owner_id = str(getattr(context, "user_id", "") or "").strip()
        run_id = str(getattr(context, "run_id", "") or "").strip()
        selected_id = str(
            getattr(context, "selected_ontology_id", "") or ""
        ).strip()
        selected_revision = str(
            getattr(context, "selected_ontology_version", "") or ""
        ).strip()
        if not workspace_id or not owner_id or not run_id:
            return _error_outcome(
                "solver_contract_drift",
                "catalog",
                False,
                run_id=run_id,
            )
        if (
            not tool_call_id
            or selected_id != binding.package_id
            or selected_revision != binding.package_revision
        ):
            return _error_outcome(
                "solver_contract_drift",
                "catalog",
                False,
                run_id=run_id,
            )
        try:
            current = self.binding_resolver(
                workspace_id=workspace_id,
                package_id=selected_id,
                package_revision=selected_revision,
                card_name=binding.card_name,
            )
        except Exception:
            current = None
        if not _same_binding(binding, current):
            return _error_outcome(
                "solver_contract_drift",
                "resume",
                False,
                run_id=run_id,
            )

        def progress_callback(event_type: str, payload: Mapping[str, Any]) -> None:
            try:
                _emit_solver_progress(
                    context=context,
                    event_type=event_type,
                    payload=payload,
                    tool_call_id=tool_call_id,
                    tool_name=binding.card_name,
                )
            except Exception:
                # A detached stream subscriber must never change Job outcome.
                return

        try:
            result = self.service.start_or_resume(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=int(getattr(context, "turn_id", 0) or 0),
                tool_call_id=tool_call_id,
                card_name=binding.card_name,
                package_id=binding.package_id,
                package_revision=binding.package_revision,
                operation_key=binding.operation_key,
                contract_digest=binding.contract_digest,
                schema_digest=binding.schema_digest,
                arguments=_thaw_json(arguments),
                traceparent=(
                    str(getattr(context, "traceparent", "") or "").strip() or None
                ),
                progress_callback=progress_callback,
                cancel_requested=_run_cancel_probe(run_id),
            )
        except Exception as exc:
            return _error_outcome(
                str(getattr(exc, "code", "solver_unavailable") or "solver_unavailable"),
                str(getattr(exc, "phase", "solve") or "solve"),
                bool(getattr(exc, "retryable", True)),
                run_id=run_id,
                job_id=str(getattr(exc, "job_id", "") or ""),
            )
        payload = _object_payload(result)
        if not isinstance(payload, dict) or _find_forbidden_key(payload):
            job_id = str(payload.get("job_id") or "") if isinstance(payload, dict) else ""
            return _error_outcome(
                "solver_result_rejected",
                "publish",
                False,
                run_id=run_id,
                job_id=job_id,
            )
        public = {
            str(key): _thaw_json(value)
            for key, value in payload.items()
            if str(key) in _PUBLIC_RESULT_FIELDS
        }
        return project_solver_tool_result(public)


def _same_binding(expected: SolverToolBinding, actual: Any) -> bool:
    if actual is None:
        return False
    fields = (
        "card_name",
        "package_id",
        "package_revision",
        "operation_key",
        "contract_digest",
        "schema_digest",
    )
    return all(
        str(getattr(actual, field, "") or "") == str(getattr(expected, field) or "")
        for field in fields
    )


def _emit_solver_progress(
    *,
    context: Any,
    event_type: str,
    payload: Mapping[str, Any],
    tool_call_id: str,
    tool_name: str,
) -> None:
    if event_type not in {
        "solver.input.awaiting_confirmation",
        "solver.progress",
        "solver.verification.completed",
        "solver.verification.rejected",
        "solver.result.authoritative",
    }:
        event_type = "solver.progress"
    public = {
        str(key): _thaw_json(value)
        for key, value in payload.items()
        if str(key) in _PUBLIC_PROGRESS_FIELDS
    }
    required = ("run_id", "job_id", "attempt_id", "event_seq")
    if any(key not in public for key in required):
        return
    public.update({"tool_call_id": tool_call_id, "tool_name": tool_name})
    emit_protocol_event(
        context=context,
        event_type=event_type,
        source={"node": "agent_loop", "component": "solver"},
        payload=public,
    )
    emit_protocol_event(
        context=context,
        event_type="tool.progress",
        source={"node": "agent_loop", "component": "tool"},
        payload=public,
    )


def _error_outcome(
    code: str,
    phase: str,
    retryable: bool,
    *,
    run_id: str = "",
    job_id: str = "",
) -> ToolOutcome:
    return _TERMINAL_OUTCOME_PROJECTOR.error(
        code=code,
        phase=phase,
        retryable=retryable,
        run_id=run_id,
        job_id=job_id,
    )


def _capability_value(capability: Any, field_name: str, default: Any = None) -> Any:
    if isinstance(capability, Mapping):
        return capability.get(field_name, default)
    return getattr(capability, field_name, default)


def _required_capability_text(capability: Any, field_name: str) -> str:
    value = _capability_value(capability, field_name)
    if not isinstance(value, str) or not value.strip():
        raise SolverToolCatalogError("{} is required".format(field_name))
    return value.strip()


def _object_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return _thaw_json(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        return _thaw_json(to_dict())
    if is_dataclass(value):
        return _thaw_json(asdict(value))
    return None


def _find_forbidden_key(value: Any) -> str:
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = re.sub(r"[^a-z0-9]", "", str(key).lower())
            if normalized in {"factsnapshot", "factsnapshotpayload"} or any(
                part in normalized for part in _FORBIDDEN_KEY_PARTS
            ):
                return str(key)
            nested = _find_forbidden_key(item)
            if nested:
                return nested
    elif isinstance(value, (list, tuple)):
        for item in value:
            nested = _find_forbidden_key(item)
            if nested:
                return nested
    return ""


def _thaw_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _thaw_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_thaw_json(item) for item in value]
    return value


def _freeze_json(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType(
            {str(key): _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_json(item) for item in value)
    return value
