"""Single application-service path for every solver invocation."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import inspect
import logging
import threading
import time
import uuid
from typing import Any

from .canonical import json_schema_digest, sha256_digest
from .contracts import (
    AuthoritativeResult,
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
    VerificationResult,
)
from .errors import (
    ERRORS,
    ArtifactPersistError,
    KnowledgePackageSolverContractIncompatibleError,
    SolverResultRejectedError,
    VerificationDivergenceError,
    VerificationRuntimeFailure,
)
from .job_orchestrator import JobOrchestrationError
from .job_store import (
    JobStoreConflict,
    JobStoreError,
    LeaseConflict,
    OptimisticLockError,
    SolverJobStore,
    TERMINAL_ATTEMPT_STATUSES,
    TERMINAL_JOB_STATUSES,
    TerminalStateError,
)
from .result_projection import validate_authoritative_verification
from .result_schema import SolverAssignmentSchemaValidator
from .connectors.endpoint_policy import EndpointPolicy
from .ports import IndependentVerifier, ProblemCompiler

logger = logging.getLogger(__name__)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SolverServiceError(RuntimeError):
    """Fail-closed application-service error with stable phase and code."""

    def __init__(self, code: str, phase: str, detail: str = "") -> None:
        super().__init__(detail or code)
        self.code = code
        self.phase = phase
        self.detail = detail
        definition = ERRORS.get(code)
        self.retryable = bool(definition and definition.retryable)


def _require_problem_ir(value: object) -> ProblemIR:
    if not isinstance(value, ProblemIR):
        raise SolverServiceError(
            "model_compile_failed",
            "compile",
            "solver compiler returned an invalid problem",
        )
    return value


def _require_verification_result(value: object) -> VerificationResult:
    if not isinstance(value, VerificationResult):
        raise VerificationRuntimeFailure(
            "solver verifier returned an invalid verification result"
        )
    return value


def _should_poll_job(
    status: object,
    *,
    poll_number: int,
    max_polls: int | None,
) -> bool:
    if status in TERMINAL_JOB_STATUSES or status == "solver_completed":
        return False
    return max_polls is None or poll_number < max_polls


@dataclass
class SolverCoordinationService:
    """Coordinate one immutable, durable, independently verified solve path."""

    capability_resolver: Any
    binding_resolver: Any
    fact_resolver: Any
    input_binder: Any
    snapshot_builder: Any
    compiler: ProblemCompiler
    job_orchestrator: Any
    verifier: IndependentVerifier
    artifact_store: Any
    result_projector: Any
    clock: Callable[[], datetime] = _utc_now
    deadline_seconds: int = 300
    # A retry is always an explicit new attempt on the same frozen run and
    # binding.  Keep the default finite; deployments may lower/raise it through
    # the runtime assembly, but never omit the durable policy from the job.
    max_attempts: int = 2
    # Production calls wait for the durable Job terminal state. A finite budget
    # is only for explicit cooperative-yield callers and deterministic tests.
    max_polls: int | None = None
    poll_interval_seconds: float = 0.05
    sleep: Callable[[float], None] = time.sleep
    presenter: Any | None = None
    input_adapter: Any | None = None
    candidate_results_builder: Callable[..., Any] | None = None
    endpoint_policy: EndpointPolicy | None = None

    def __post_init__(self) -> None:
        if self.endpoint_policy is None:
            self.endpoint_policy = EndpointPolicy()
        elif not isinstance(self.endpoint_policy, EndpointPolicy):
            raise TypeError("endpoint_policy must be an EndpointPolicy")
        if not isinstance(getattr(self.job_orchestrator, "store", None), SolverJobStore):
            raise TypeError("job_orchestrator must expose a SolverJobStore")
        for field_name in (
            "capability_resolver",
            "binding_resolver",
            "fact_resolver",
        ):
            if not callable(getattr(getattr(self, field_name), "resolve", None)):
                raise TypeError("{} must provide resolve".format(field_name))
        if not callable(getattr(self.input_binder, "bind", None)):
            raise TypeError("input_binder must provide bind")
        if not callable(getattr(self.snapshot_builder, "build", None)):
            raise TypeError("snapshot_builder must provide build")
        if not callable(getattr(self.compiler, "compile", None)):
            raise TypeError("compiler must provide compile")
        if not callable(getattr(self.verifier, "verify", None)):
            raise TypeError("verifier must provide verify")
        if not callable(getattr(self.artifact_store, "put", None)):
            raise TypeError("artifact_store must provide put")
        if not callable(getattr(self.result_projector, "project", None)):
            raise TypeError("result_projector must provide project")
        if self.presenter is not None and not callable(
            getattr(self.presenter, "present", None)
        ):
            raise TypeError("presenter must provide present")
        if self.input_adapter is not None and not callable(
            getattr(self.input_adapter, "adapt", None)
        ):
            raise TypeError("input_adapter must provide adapt")
        if self.candidate_results_builder is not None and not callable(
            self.candidate_results_builder
        ):
            raise TypeError("candidate_results_builder must be callable")
        if (
            isinstance(self.deadline_seconds, bool)
            or not isinstance(self.deadline_seconds, int)
            or self.deadline_seconds < 1
        ):
            raise ValueError("deadline_seconds must be a positive integer")
        if isinstance(self.max_attempts, bool) or not isinstance(
            self.max_attempts, int
        ):
            raise ValueError("max_attempts must be an integer between 1 and 16")
        if self.max_attempts < 1 or self.max_attempts > 16:
            raise ValueError("max_attempts must be an integer between 1 and 16")
        if self.max_polls is not None:
            if isinstance(self.max_polls, bool) or not isinstance(
                self.max_polls, int
            ):
                raise ValueError("max_polls must be a positive integer or None")
            if self.max_polls < 1:
                raise ValueError("max_polls must be a positive integer or None")
        if isinstance(self.poll_interval_seconds, bool) or not isinstance(
            self.poll_interval_seconds, (int, float)
        ) or self.poll_interval_seconds < 0:
            raise ValueError("poll_interval_seconds must be non-negative")

    @property
    def store(self) -> SolverJobStore:
        return self.job_orchestrator.store

    def retry_job(
        self,
        *,
        job_id: str,
        workspace_id: str,
        owner_id: str,
        attempt_id: str | None = None,
    ) -> dict[str, Any]:
        """Start one explicit retry attempt for a durable terminal failure.

        Retrying never re-posts the original attempt.  The orchestrator creates
        a new attempt id, keeps the persisted ProblemIR and binding revision,
        and lets the JobStore enforce the run's ``max_attempts`` policy.
        """

        if not isinstance(job_id, str) or not job_id.strip():
            raise JobStoreConflict("job_id is required")
        if not isinstance(workspace_id, str) or not workspace_id.strip():
            raise JobStoreConflict("workspace_id is required")
        if not isinstance(owner_id, str) or not owner_id.strip():
            raise JobStoreConflict("owner_id is required")
        if attempt_id is not None and (
            not isinstance(attempt_id, str) or not attempt_id.strip()
        ):
            raise JobStoreConflict("attempt_id must be a non-empty string")
        outcome = self.job_orchestrator.retry(
            job_id.strip(),
            workspace_id=workspace_id.strip(),
            owner_id=owner_id.strip(),
            attempt_id=attempt_id.strip() if isinstance(attempt_id, str) else None,
        )
        if not isinstance(outcome, Mapping):
            raise JobStoreError("solver retry returned an invalid outcome")
        # A retry is deliberately non-authoritative until the normal poll /
        # verify path completes.  Keep the backend's durable identifiers so a
        # caller can subscribe to history and recover after a process restart.
        return {
            "status": "running",
            "code": "retry_started",
            "phase": "retry",
            "job_id": str(outcome.get("job_id") or job_id),
            "attempt_id": str(outcome.get("attempt_id") or ""),
            "attempt_number": int(outcome.get("attempt_number") or 0),
            "authoritative": False,
            "retryable": False,
        }

    # Keep the short spelling available to internal adapters and integrations;
    # the HTTP route uses retry_job to make the mutating operation explicit.
    def retry(self, **kwargs: Any) -> dict[str, Any]:
        return self.retry_job(**kwargs)

    def start_or_resume(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str | int,
        tool_call_id: str,
        card_name: str,
        package_id: str,
        package_revision: str,
        operation_key: str,
        contract_digest: str,
        schema_digest: str,
        arguments: Mapping[str, Any],
        progress_callback: Callable[[str, Mapping[str, Any]], None],
        traceparent: str | None = None,
        cancel_requested: Callable[[], bool] | None = None,
        recovery: bool = False,
    ) -> dict[str, Any]:
        if cancel_requested is not None and not callable(cancel_requested):
            raise TypeError("cancel_requested must be callable or None")
        turn = str(turn_id)
        existing_link = self.store.get_run_link(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn,
            tool_call_id=tool_call_id,
        )
        if existing_link is not None and (
            existing_link["card_name"] != card_name
            or existing_link["contract_digest"] != contract_digest
        ):
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "solver ToolCard contract changed during recovery",
            )
        if existing_link is not None and existing_link.get("tool_result") is not None:
            return dict(existing_link["tool_result"])
        if existing_link is not None:
            linked_job = self.store.get_job(
                existing_link["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if linked_job is not None and linked_job["status"] in TERMINAL_JOB_STATUSES:
                return self._replay_terminal_job_result(
                    job=linked_job,
                    links=self.store.list_job_run_links(
                        linked_job["job_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    progress_callback=progress_callback,
                    target_link_identity=(run_id, turn, tool_call_id),
                )
            if linked_job is not None and linked_job.get("phase") == "freeze_inputs":
                waiting = self._await_input_preparation(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    job_id=str(linked_job["job_id"]),
                    logical_job_key=str(linked_job["logical_job_key"]),
                    attempt_id=existing_link.get("attempt_id"),
                    subscription_id=str(existing_link.get("subscription_id") or ""),
                    cancel_requested=cancel_requested,
                    progress_callback=progress_callback,
                )
                if waiting is not None:
                    return waiting
                existing_link = self.store.get_run_link(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                )
                if existing_link is None:
                    raise SolverServiceError(
                        "solver_unavailable", "resume", "solver run link disappeared"
                    )

        capability = self._resolve_capability(
            workspace_id=workspace_id,
            owner_id=owner_id,
            package_id=package_id,
            package_revision=package_revision,
            operation_key=operation_key,
            contract_drift_on_missing=recovery or existing_link is not None,
        )
        self._validate_capability_lock(
            capability=capability,
            package_id=package_id,
            package_revision=package_revision,
            operation_key=operation_key,
            contract_digest=contract_digest,
            schema_digest=schema_digest,
            phase="resume" if existing_link is not None else "select",
        )
        retry_link = None
        if existing_link is None:
            input_schema = self._capability_value(capability, "input_schema")
            binding_result = self.input_binder.bind(
                operation_key=operation_key,
                schema=input_schema,
                values=arguments,
                input_adapter=self.input_adapter,
                capability=capability,
            )
            if binding_result.status != "bound":
                payload = binding_result.to_dict()
                if binding_result.status == "awaiting_input":
                    awaiting = payload.get("awaiting_input") or {}
                    return {
                        "status": "awaiting_input",
                        "code": "awaiting_input",
                        "phase": "bind",
                        "missing": list(awaiting.get("missing") or ()),
                        "ambiguous": dict(awaiting.get("ambiguous") or {}),
                        "errors": list(payload.get("errors") or ()),
                        "authoritative": False,
                        "retryable": False,
                    }
                return {
                    "status": "error",
                    "code": "invalid_solver_input",
                    "phase": "bind",
                    "errors": list(payload.get("errors") or ()),
                    "authoritative": False,
                    "retryable": False,
                }
            normalized_input = binding_result.normalized_input
            retry_link = self._matching_retryable_verification_link(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                card_name=card_name,
                contract_digest=contract_digest,
                normalized_input=normalized_input,
            )

        resume_link = existing_link or retry_link
        if resume_link is not None:
            existing_job, frozen_input, fact_snapshot, problem = (
                self._resume_frozen_context(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    package_id=package_id,
                    package_revision=package_revision,
                    operation_key=operation_key,
                    contract_digest=contract_digest,
                    schema_digest=schema_digest,
                    existing_link=resume_link,
                )
            )
            if existing_link is not None and existing_link.get("tool_result") is not None:
                return dict(existing_link["tool_result"])
            normalized_input = frozen_input
            binding_revision_id = str(existing_job["binding_revision_id"])
            run_policy = dict(existing_job["run_policy"])
            deadline_at = self._deadline(existing_job["deadline_at"])
            resolved_capability, _required_facts = self._resolve_model(capability)
        else:
            resolved_capability, required_facts = self._resolve_model(capability)
            # Dynamic binding selection must happen before profile facts are
            # frozen: profile providers include the selected revision in the
            # immutable snapshot.  Legacy/fixed resolvers retain the original
            # ordering for compatibility with migration fixtures.
            preselected_binding: tuple[str, Mapping[str, Any]] | None = None
            if bool(getattr(self.binding_resolver, "_dynamic_selection", False)):
                preselected_payload = self.binding_resolver.resolve(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    package_id=package_id,
                    package_revision=package_revision,
                    operation_key=operation_key,
                    capability=resolved_capability,
                    fact_snapshot=None,
                )
                selected_id, selected_configuration = self._binding(
                    preselected_payload,
                    endpoint_policy=self.endpoint_policy,
                )
                preselected_binding = (selected_id, selected_configuration)
            fact_identity = {
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "run_id": run_id,
                "package_id": package_id,
                "package_revision": package_revision,
                "operation_key": operation_key,
                "capability": resolved_capability,
                "normalized_input": normalized_input,
                "required_facts": required_facts,
            }
            if preselected_binding is not None:
                fact_identity["binding_revision_id"] = preselected_binding[0]
            try:
                fact_payload = self.fact_resolver.resolve(**fact_identity)
            except KnowledgePackageSolverContractIncompatibleError as exc:
                raise SolverServiceError(
                    "knowledge_package_solver_contract_incompatible",
                    "snapshot",
                    "selected knowledge package does not implement the required solver contract",
                ) from exc
            except Exception as exc:
                raise SolverServiceError(
                    "fact_snapshot_invalid",
                    "snapshot",
                    "required platform facts could not be resolved",
                ) from exc
            if not isinstance(fact_payload, Mapping):
                raise SolverServiceError(
                    "fact_snapshot_invalid",
                    "snapshot",
                    "fact resolver returned invalid data",
                )
            try:
                fact_snapshot = self.snapshot_builder.build(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    package_id=package_id,
                    package_revision=package_revision,
                    normalized_input=normalized_input,
                    facts=fact_payload.get("facts", ()),
                    bindings=fact_payload.get("bindings", {}),
                    as_of=fact_payload.get("as_of"),
                    source_evidence=fact_payload.get("source_evidence", ()),
                    required_facts=required_facts,
                )
            except Exception as exc:
                raise SolverServiceError(
                    "fact_snapshot_invalid",
                    "snapshot",
                    "fact snapshot could not be frozen",
                ) from exc
            fact_snapshot = self._job_scoped_fact_snapshot(fact_snapshot)

            if preselected_binding is None:
                binding_payload = self.binding_resolver.resolve(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    package_id=package_id,
                    package_revision=package_revision,
                    operation_key=operation_key,
                    capability=resolved_capability,
                    fact_snapshot=fact_snapshot,
                )
                binding_revision_id, binding_configuration = self._binding(
                    binding_payload,
                    endpoint_policy=self.endpoint_policy,
                )
            else:
                binding_revision_id, binding_configuration = preselected_binding
            capability_check = getattr(
                self.job_orchestrator.backend,
                "assert_binding_capabilities",
                None,
            )
            if callable(capability_check):
                try:
                    required_capabilities = self._capability_value(
                        resolved_capability, "engine_capabilities"
                    ) or ()
                    self._assert_backend_capabilities(
                        capability_check,
                        binding_configuration,
                        required_capabilities=required_capabilities,
                    )
                except Exception as exc:
                    raise SolverServiceError(
                        "solver_capability_drift",
                        "select",
                        "solver Connector capabilities do not match the binding revision",
                    ) from exc
            snapshot_binding = fact_snapshot.bindings.get("solver_binding_revision")
            if snapshot_binding is not None and snapshot_binding != binding_revision_id:
                raise SolverServiceError(
                    "fact_snapshot_invalid",
                    "snapshot",
                    "fact snapshot solver binding does not match the selected revision",
                )
            try:
                register_binding = getattr(
                    self.store, "register_connector_binding_revision", None
                )
                if not callable(register_binding):
                    raise SolverServiceError(
                        "solver_unavailable",
                        "select",
                        "federated solver binding registry is unavailable",
                    )
                register_binding(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    binding_revision_id=binding_revision_id,
                    configuration=binding_configuration,
                    endpoint_policy=self.endpoint_policy,
                )
                problem = _require_problem_ir(
                    self.compiler.compile(
                        capability=resolved_capability,
                        normalized_input=normalized_input,
                        fact_snapshot=fact_snapshot,
                    )
                )
            except Exception as exc:
                if isinstance(exc, SolverServiceError):
                    raise
                raise SolverServiceError(
                    "model_compile_failed",
                    "compile",
                    "solver problem could not be compiled",
                ) from exc

            now = self._now()
            orchestrator_now = getattr(self.job_orchestrator, "_now", None)
            if callable(orchestrator_now):
                now = max(now, orchestrator_now())
            run_policy = {
                "max_attempts": self.max_attempts,
                "result_quality": self._result_quality(resolved_capability),
                "contract_digest": contract_digest,
                "schema_digest": schema_digest,
                "package_id": package_id,
                "package_revision": package_revision,
                "operation_key": operation_key,
            }
            if traceparent is not None:
                try:
                    from .connectors.helpers import validate_traceparent

                    run_policy["traceparent"] = validate_traceparent(traceparent)
                except ValueError as exc:
                    raise SolverServiceError(
                        "solver_contract_drift",
                        "submit",
                        "solver trace context is invalid",
                    ) from exc
            deadline_at = now + timedelta(seconds=self.deadline_seconds)
        try:
            outcome = self.job_orchestrator.start_or_resume(
                workspace_id=workspace_id,
                owner_id=owner_id,
                binding_revision_id=binding_revision_id,
                problem=problem,
                run_policy=run_policy,
                deadline_at=deadline_at,
                run_id=run_id,
                turn_id=turn,
                tool_call_id=tool_call_id,
                card_name=card_name,
                contract_digest=contract_digest,
                defer_submit=existing_link is None,
            )
        except JobStoreConflict as exc:
            current_link = self.store.get_run_link(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn,
                tool_call_id=tool_call_id,
            )
            if current_link is not None and current_link.get("tool_result") is not None:
                return dict(current_link["tool_result"])
            if current_link is not None and not bool(
                current_link.get("subscription_active", 1)
            ):
                return self._write_cancelled_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    job_id=current_link["job_id"],
                    attempt_id=current_link.get("attempt_id"),
                )
            if current_link is not None:
                current_job = self.store.get_job(
                    current_link["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                if current_job is not None and current_job["status"] in TERMINAL_JOB_STATUSES:
                    return self._replay_terminal_job_result(
                        job=current_job,
                        links=self.store.list_job_run_links(
                            current_link["job_id"],
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                        ),
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        progress_callback=progress_callback,
                        target_link_identity=(run_id, turn, tool_call_id),
                    )
            raise SolverServiceError(
                "solver_contract_drift", "resume", "solver invocation cannot be resumed"
            ) from exc
        subscription_id = str(outcome.get("subscription_id") or "").strip()
        if outcome["status"] in TERMINAL_JOB_STATUSES:
            terminal_job = self.store.get_job(
                outcome["job_id"],
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if terminal_job is None:
                raise SolverServiceError(
                    "solver_unavailable", "resume", "terminal solver job disappeared"
                )
            return self._replay_terminal_job_result(
                job=terminal_job,
                links=self.store.list_job_run_links(
                    outcome["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                ),
                workspace_id=workspace_id,
                owner_id=owner_id,
                progress_callback=progress_callback,
                target_link_identity=(run_id, turn, tool_call_id),
            )

        if existing_link is None:
            persisted_job = self.store.get_job(
                outcome["job_id"], workspace_id=workspace_id, owner_id=owner_id
            )
            if persisted_job is None:
                raise SolverServiceError(
                    "solver_unavailable",
                    "submit",
                    "solver job disappeared before frozen inputs were persisted",
                )
            deadline_at = self._deadline(persisted_job["deadline_at"])
            preparation_lease = outcome.get("input_preparation_lease")
            if isinstance(preparation_lease, Mapping):
                preparation_owner = str(
                    preparation_lease.get("lease_owner") or ""
                ).strip()
                preparation_fence = preparation_lease.get("fencing_token")
                preparation_lease_seconds = int(
                    preparation_lease.get("lease_seconds")
                    or getattr(self.job_orchestrator, "lease_seconds", 30)
                )
                try:
                    self._persist_frozen_inputs(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        job_id=outcome["job_id"],
                        attempt_id=outcome.get("attempt_id"),
                        normalized_input=normalized_input.to_dict(),
                        fact_snapshot=fact_snapshot.to_dict(),
                        problem=problem.to_dict(),
                        lease_owner=preparation_owner,
                        fencing_token=preparation_fence,
                        lease_seconds=preparation_lease_seconds,
                    )
                    persisted_job = self.store.complete_input_preparation(
                        outcome["job_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=preparation_owner,
                        fencing_token=preparation_fence,
                    )
                except (LeaseConflict, OptimisticLockError, TerminalStateError, JobStoreConflict):
                    current_link = self.store.get_run_link(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        run_id=run_id,
                        turn_id=turn,
                        tool_call_id=tool_call_id,
                    )
                    current_job = self.store.get_job(
                        outcome["job_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                    if current_link is not None and current_link.get("tool_result") is not None:
                        return dict(current_link["tool_result"])
                    if current_link is not None and not bool(
                        current_link.get("subscription_active", 1)
                    ):
                        return self._write_cancelled_tool_result(
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            run_id=run_id,
                            turn_id=turn,
                            tool_call_id=tool_call_id,
                            contract_digest=contract_digest,
                            job_id=outcome["job_id"],
                            attempt_id=outcome.get("attempt_id"),
                        )
                    if current_job is not None and current_job["status"] in TERMINAL_JOB_STATUSES:
                        return self._replay_terminal_job_result(
                            job=current_job,
                            links=self.store.list_job_run_links(
                                outcome["job_id"],
                                workspace_id=workspace_id,
                                owner_id=owner_id,
                            ),
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            progress_callback=progress_callback,
                            target_link_identity=(run_id, turn, tool_call_id),
                        )
                    if current_job is None or current_job.get("phase") == "freeze_inputs":
                        return {
                            "status": "running",
                            "phase": "freeze_inputs",
                            "job_id": outcome["job_id"],
                            "logical_job_key": outcome.get("logical_job_key"),
                            "attempt_id": outcome.get("attempt_id"),
                            "subscription_id": subscription_id,
                            "authoritative": False,
                        }
                    persisted_job = current_job
                except SolverServiceError as exc:
                    attempt_id = str(outcome.get("attempt_id") or "")
                    self._best_effort_stop_attempt(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        attempt_id=attempt_id,
                        lease_owner=preparation_owner,
                        fencing_token=preparation_fence,
                    )
                    return self._fail_job(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        run_id=run_id,
                        turn_id=turn,
                        tool_call_id=tool_call_id,
                        contract_digest=contract_digest,
                        job_id=outcome["job_id"],
                        attempt_id=attempt_id,
                        code=exc.code,
                        phase=exc.phase,
                        retryable=False,
                        fencing_token=preparation_fence,
                        progress_callback=progress_callback,
                    )
            elif persisted_job.get("phase") == "freeze_inputs":
                waiting = self._await_input_preparation(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    job_id=str(outcome["job_id"]),
                    logical_job_key=str(outcome.get("logical_job_key") or ""),
                    attempt_id=outcome.get("attempt_id"),
                    subscription_id=subscription_id,
                    cancel_requested=cancel_requested,
                    progress_callback=progress_callback,
                )
                if waiting is not None:
                    return waiting
                persisted_job = self.store.get_job(
                    outcome["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                if persisted_job is None:
                    raise SolverServiceError(
                        "solver_unavailable", "resume", "solver job disappeared"
                    )
            try:
                outcome = self.job_orchestrator.start_or_resume(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    binding_revision_id=binding_revision_id,
                    problem=problem,
                    run_policy=run_policy,
                    deadline_at=deadline_at,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                    card_name=card_name,
                    contract_digest=contract_digest,
                )
            except JobStoreConflict as exc:
                current_link = self.store.get_run_link(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                )
                if current_link is not None and current_link.get("tool_result") is not None:
                    return dict(current_link["tool_result"])
                if current_link is not None and not bool(
                    current_link.get("subscription_active", 1)
                ):
                    return self._write_cancelled_tool_result(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        run_id=run_id,
                        turn_id=turn,
                        tool_call_id=tool_call_id,
                        contract_digest=contract_digest,
                        job_id=current_link["job_id"],
                        attempt_id=current_link.get("attempt_id"),
                    )
                if current_link is not None:
                    current_job = self.store.get_job(
                        current_link["job_id"],
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                    if current_job is not None and current_job["status"] in TERMINAL_JOB_STATUSES:
                        return self._replay_terminal_job_result(
                            job=current_job,
                            links=self.store.list_job_run_links(
                                current_link["job_id"],
                                workspace_id=workspace_id,
                                owner_id=owner_id,
                            ),
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            progress_callback=progress_callback,
                            target_link_identity=(run_id, turn, tool_call_id),
                        )
                raise SolverServiceError(
                    "solver_contract_drift",
                    "resume",
                    "solver invocation cannot be resumed",
                ) from exc
            subscription_id = str(outcome.get("subscription_id") or "").strip()
        self._drain_events(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn,
            tool_call_id=tool_call_id,
            fallback_attempt_id=outcome.get("attempt_id"),
            progress_callback=progress_callback,
        )

        if existing_link is not None and not bool(
            existing_link.get("subscription_active", 1)
        ):
            return self._write_cancelled_tool_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=outcome["job_id"],
                attempt_id=outcome.get("attempt_id"),
            )

        cancelled = self._cancel_current_subscription_if_requested(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn,
            tool_call_id=tool_call_id,
            contract_digest=contract_digest,
            outcome=outcome,
            subscription_id=subscription_id,
            cancel_requested=cancel_requested,
            progress_callback=progress_callback,
        )
        if cancelled is not None:
            return cancelled

        poll_number = 0
        while _should_poll_job(
            outcome["status"],
            poll_number=poll_number,
            max_polls=self.max_polls,
        ):
            try:
                outcome = self.job_orchestrator.poll_once(
                    outcome["job_id"], workspace_id=workspace_id, owner_id=owner_id
                )
            except (LeaseConflict, OptimisticLockError) as exc:
                current_job = self.store.get_job(
                    outcome["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                if current_job is None:
                    raise SolverServiceError(
                        "solver_unavailable", "solve", "solver job disappeared"
                    ) from exc
                current_attempt = self.store.latest_attempt(
                    outcome["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                outcome = {
                    "job_id": current_job["job_id"],
                    "status": current_job["status"],
                    "phase": current_job["phase"],
                    "error_code": current_job.get("error_code"),
                    "attempt_id": (current_attempt or {}).get("attempt_id"),
                    "backend_job_id": (current_attempt or {}).get("backend_job_id"),
                    "subscription_id": subscription_id,
                }
            poll_number += 1
            self._drain_events(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn,
                tool_call_id=tool_call_id,
                fallback_attempt_id=outcome.get("attempt_id"),
                progress_callback=progress_callback,
            )
            cancelled = self._cancel_current_subscription_if_requested(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                outcome=outcome,
                subscription_id=subscription_id,
                cancel_requested=cancel_requested,
                progress_callback=progress_callback,
            )
            if cancelled is not None:
                return cancelled
            if _should_poll_job(
                outcome["status"],
                poll_number=poll_number,
                max_polls=self.max_polls,
            ):
                self.sleep(float(self.poll_interval_seconds))

        if outcome["status"] == "completed":
            recovered = self._existing_authoritative_event(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                job_id=outcome["job_id"],
                package_revision=package_revision,
                fact_snapshot=fact_snapshot,
                problem=problem,
            )
            if recovered is not None:
                self._write_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    result=recovered,
                )
                return recovered
            return self._verify_and_publish(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                package_revision=package_revision,
                capability=resolved_capability,
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
                problem=problem,
                outcome=outcome,
                progress_callback=progress_callback,
            )

        if outcome["status"] != "solver_completed":
            if outcome["status"] in TERMINAL_JOB_STATUSES:
                result = self._backend_terminal_result(outcome)
                self._write_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    result=result,
                )
                return result
            return {
                "status": "running",
                "phase": outcome.get("phase", "solve"),
                "job_id": outcome["job_id"],
                "attempt_id": outcome.get("attempt_id"),
                "authoritative": False,
            }

        return self._verify_and_publish(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn,
            tool_call_id=tool_call_id,
            contract_digest=contract_digest,
            package_revision=package_revision,
            capability=resolved_capability,
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            outcome=outcome,
            progress_callback=progress_callback,
        )

    def recover_job(
        self,
        job_id: str,
        *,
        workspace_id: str,
        owner_id: str,
        progress_callback: Callable[[str, Mapping[str, Any]], None] | None = None,
    ) -> dict[str, Any]:
        """Resume one persisted Job through verification and every Tool inbox."""

        callback = progress_callback or (lambda _event_type, _payload: None)
        job = self.store.get_job(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if job is None:
            raise SolverServiceError("solver_unavailable", "resume", "solver job is missing")
        links = self.store.list_job_run_links(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if links and job["status"] in TERMINAL_JOB_STATUSES:
            return self._replay_terminal_job_result(
                job=job,
                links=links,
                workspace_id=workspace_id,
                owner_id=owner_id,
                progress_callback=callback,
            )
        current_binding_revision = str(
            getattr(self.job_orchestrator, "binding_revision_id", "") or ""
        ).strip()
        if (
            current_binding_revision
            and job.get("phase") != "freeze_inputs"
            and str(job.get("binding_revision_id") or "").strip()
            != current_binding_revision
        ):
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
            )
        frozen_payloads: dict[str, Mapping[str, Any]] | None = None
        if job.get("phase") == "freeze_inputs":
            preparation_owner = "solver_input_recovery_{}".format(uuid.uuid4().hex)
            try:
                claimed = self.store.claim_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=preparation_owner,
                    lease_seconds=max(self.job_orchestrator.lease_seconds, 60),
                    expected_version=job["version"],
                )
            except (LeaseConflict, OptimisticLockError):
                attempt = self.store.latest_attempt(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                return {
                    "job_id": job_id,
                    "status": job["status"],
                    "phase": "freeze_inputs",
                    "attempt_id": (attempt or {}).get("attempt_id"),
                    "authoritative": False,
                }
            job = self.store.get_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            ) or claimed
            links = self.store.list_job_run_links(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if job["status"] in TERMINAL_JOB_STATUSES:
                self.store.release_job_lease(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=preparation_owner,
                    fencing_token=claimed["fencing_token"],
                )
                return self._replay_terminal_job_result(
                    job=job,
                    links=links,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    progress_callback=callback,
                )
            if not links and self._now() < self._deadline(job["deadline_at"]):
                try:
                    self.store.release_job_lease(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=preparation_owner,
                        fencing_token=claimed["fencing_token"],
                    )
                except LeaseConflict:
                    logger.debug(
                        "Input-recovery lease was already released or transferred"
                    )
                return self._persisted_job_outcome(
                    job_id=job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            try:
                frozen_payloads = {}
                for artifact_type in (
                    "normalized_input",
                    "fact_snapshot",
                    "problem_ir",
                ):
                    self.store.renew_job_lease(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=preparation_owner,
                        fencing_token=claimed["fencing_token"],
                        lease_seconds=max(self.job_orchestrator.lease_seconds, 60),
                    )
                    frozen_payloads[artifact_type] = self._load_frozen_artifact(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        job_id=job_id,
                        artifact_type=artifact_type,
                    )
            except (LeaseConflict, OptimisticLockError, TerminalStateError):
                current = self.store.get_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                links = self.store.list_job_run_links(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                if current is not None and current["status"] in TERMINAL_JOB_STATUSES:
                    return self._replay_terminal_job_result(
                        job=current,
                        links=links,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        progress_callback=callback,
                    )
                return self._persisted_job_outcome(
                    job_id=job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            except SolverServiceError as exc:
                if exc.code != "artifact_persist_failed":
                    raise
                try:
                    self._terminalize(
                        job_id=job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        status="failed",
                        phase="resume",
                        error_code="artifact_persist_failed",
                        fencing_token=claimed["fencing_token"],
                    )
                    self.store.release_job_lease(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=preparation_owner,
                        fencing_token=claimed["fencing_token"],
                    )
                except (LeaseConflict, OptimisticLockError, TerminalStateError):
                    current = self.store.get_job(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                    links = self.store.list_job_run_links(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                    if current is not None and current["status"] in TERMINAL_JOB_STATUSES:
                        return self._replay_terminal_job_result(
                            job=current,
                            links=links,
                            workspace_id=workspace_id,
                            owner_id=owner_id,
                            progress_callback=callback,
                        )
                    return self._persisted_job_outcome(
                        job_id=job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                return self._write_recovery_contract_drift(
                    job_id=job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    links=links,
                    progress_callback=callback,
                    error_code="artifact_persist_failed",
                    phase="resume",
                    retryable=False,
                )
            try:
                job = self.store.complete_input_preparation(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=preparation_owner,
                    fencing_token=claimed["fencing_token"],
                )
            except (LeaseConflict, OptimisticLockError, TerminalStateError, JobStoreConflict):
                current = self.store.get_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                links = self.store.list_job_run_links(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                if current is not None and current["status"] in TERMINAL_JOB_STATUSES:
                    return self._replay_terminal_job_result(
                        job=current,
                        links=links,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        progress_callback=callback,
                    )
                if current is None or current.get("phase") == "freeze_inputs":
                    return self._persisted_job_outcome(
                        job_id=job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                job = current
            links = self.store.list_job_run_links(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
        if (
            current_binding_revision
            and str(job.get("binding_revision_id") or "").strip()
            != current_binding_revision
        ):
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
            )
        if not links:
            # A Job without a durable run link is still in the creation window.
            # Keep it recoverable, but never submit an orphan backend request.
            if self._now() >= self._deadline(job["deadline_at"]):
                return self._write_recovery_contract_drift(
                    job_id=job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    links=(),
                    progress_callback=callback,
                    error_code="solver_unavailable",
                    phase="submit",
                    retryable=False,
                )
            return self._persisted_job_outcome(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
        try:
            if frozen_payloads is None:
                frozen_payloads = {}
                for artifact_type in (
                    "normalized_input",
                    "fact_snapshot",
                    "problem_ir",
                ):
                    frozen_payloads[artifact_type] = self._load_frozen_artifact(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        job_id=job_id,
                        artifact_type=artifact_type,
                    )
            fact_payload = frozen_payloads.get("fact_snapshot")
            if not isinstance(fact_payload, Mapping):
                raise SolverServiceError(
                    "artifact_persist_failed",
                    "resume",
                    "frozen fact snapshot is unavailable",
                )
        except SolverServiceError as exc:
            if exc.code != "artifact_persist_failed":
                raise
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
                error_code="artifact_persist_failed",
                phase="resume",
                retryable=False,
            )
        if job["status"] not in TERMINAL_JOB_STATUSES and job["status"] != "solver_completed":
            try:
                advanced = self.job_orchestrator.poll_once(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            except (LeaseConflict, OptimisticLockError):
                return self._persisted_job_outcome(
                    job_id=job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            if (
                advanced["status"] not in TERMINAL_JOB_STATUSES
                and advanced["status"] != "solver_completed"
            ):
                return advanced
            job = self.store.get_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if job is None:
                raise SolverServiceError(
                    "solver_unavailable", "resume", "solver job disappeared"
                )

        primary = links[0]
        run_policy = job.get("run_policy")
        if not isinstance(run_policy, Mapping):
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
            )
        required_policy: dict[str, str] = {}
        for name in (
            "package_id",
            "package_revision",
            "operation_key",
            "contract_digest",
            "schema_digest",
        ):
            required_policy[name] = str(run_policy.get(name) or "").strip()
        missing_required_policy = False
        for value in required_policy.values():
            if not value:
                missing_required_policy = True
                break
        if missing_required_policy:
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
            )
        package_id = str(fact_payload.get("package_id") or "").strip()
        if package_id != required_policy["package_id"]:
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
            )

        try:
            result = self.start_or_resume(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=str(primary["run_id"]),
                turn_id=str(primary["turn_id"]),
                tool_call_id=str(primary["tool_call_id"]),
                card_name=str(primary["card_name"]),
                package_id=package_id,
                package_revision=required_policy["package_revision"],
                operation_key=required_policy["operation_key"],
                contract_digest=required_policy["contract_digest"],
                schema_digest=required_policy["schema_digest"],
                arguments={},
                progress_callback=callback,
                traceparent=(
                    str(run_policy.get("traceparent") or "").strip() or None
                ),
                recovery=True,
            )
        except SolverServiceError as exc:
            if exc.code not in {"solver_contract_drift", "artifact_persist_failed"}:
                raise
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
                error_code=exc.code,
                phase=exc.phase,
                retryable=False,
            )
        if result.get("status") == "running":
            return result

        refreshed_primary = self.store.get_run_link(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=str(primary["run_id"]),
            turn_id=str(primary["turn_id"]),
            tool_call_id=str(primary["tool_call_id"]),
        )
        if refreshed_primary is None:
            raise SolverServiceError(
                "solver_unavailable", "resume", "solver run link disappeared"
            )
        try:
            _existing_job, _normalized_input, fact_snapshot, problem = (
                self._resume_frozen_context(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    package_id=package_id,
                    package_revision=required_policy["package_revision"],
                    operation_key=required_policy["operation_key"],
                    contract_digest=required_policy["contract_digest"],
                    schema_digest=required_policy["schema_digest"],
                    existing_link=refreshed_primary,
                )
            )
        except SolverServiceError as exc:
            if exc.code not in {"solver_contract_drift", "artifact_persist_failed"}:
                raise
            return self._write_recovery_contract_drift(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                links=links,
                progress_callback=callback,
                error_code=exc.code,
                phase=exc.phase,
                retryable=False,
            )
        for link in links:
            if link.get("tool_result") is not None:
                continue
            # A cancellation and the durable ToolResult write are separate
            # operations.  If the process dies in that window, recovery must
            # preserve the subscription's cancellation semantics even when a
            # sibling subscription later makes the shared Job authoritative.
            if not bool(link.get("subscription_active", 1)):
                self._write_cancelled_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(link["run_id"]),
                    turn_id=str(link["turn_id"]),
                    tool_call_id=str(link["tool_call_id"]),
                    contract_digest=str(link["contract_digest"]),
                    job_id=job_id,
                    attempt_id=link.get("attempt_id"),
                )
                continue
            projected = dict(result)
            if result.get("authoritative") is True:
                projected = self._existing_authoritative_event(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(link["run_id"]),
                    job_id=job_id,
                    package_revision=required_policy["package_revision"],
                    fact_snapshot=fact_snapshot,
                    problem=problem,
                )
                if projected is None:
                    raise SolverServiceError(
                        "artifact_persist_failed",
                        "resume",
                        "completed solver job has no authoritative event",
                    )
            self._write_tool_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=str(link["run_id"]),
                turn_id=str(link["turn_id"]),
                tool_call_id=str(link["tool_call_id"]),
                contract_digest=str(link["contract_digest"]),
                result=projected,
            )
        return result

    def _cancel_current_subscription_if_requested(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        outcome: Mapping[str, Any],
        subscription_id: str,
        cancel_requested: Callable[[], bool] | None,
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any] | None:
        if cancel_requested is None or outcome.get("status") in TERMINAL_JOB_STATUSES:
            return None
        try:
            requested = bool(cancel_requested())
        except Exception as exc:
            raise SolverServiceError(
                "solver_unavailable",
                "cancel",
                "solver cancellation state could not be read",
            ) from exc
        if not requested:
            return None

        if not subscription_id:
            raise SolverServiceError(
                "solver_unavailable",
                "cancel",
                "solver subscription identity is missing",
            )
        try:
            cancellation = self.job_orchestrator.cancel_subscription(
                subscription_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
        except Exception as exc:
            raise SolverServiceError(
                "solver_unavailable",
                "cancel",
                "solver subscription could not be cancelled",
            ) from exc

        attempt_id = cancellation.get("attempt_id") or outcome.get("attempt_id")
        self._drain_events(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            fallback_attempt_id=attempt_id,
            progress_callback=progress_callback,
        )
        return self._write_cancelled_tool_result(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            contract_digest=contract_digest,
            job_id=outcome["job_id"],
            attempt_id=attempt_id,
        )

    def _write_cancelled_tool_result(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        job_id: str,
        attempt_id: Any,
    ) -> dict[str, Any]:
        result = {
            "status": "cancelled",
            "code": "cancelled",
            "phase": "cancel",
            "retryable": False,
            "authoritative": False,
            "job_id": job_id,
            "attempt_id": attempt_id,
        }
        self._write_tool_result(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            contract_digest=contract_digest,
            result=result,
        )
        return result

    def _write_recovery_contract_drift(
        self,
        *,
        job_id: str,
        workspace_id: str,
        owner_id: str,
        links: tuple[Mapping[str, Any], ...],
        progress_callback: Callable[[str, Mapping[str, Any]], None],
        error_code: str = "solver_contract_drift",
        phase: str = "resume",
        retryable: bool = False,
    ) -> dict[str, Any]:
        current = self.store.get_job(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if current is None:
            raise SolverServiceError("solver_unavailable", phase, "solver job is missing")
        links = self.store.list_job_run_links(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if current["status"] in TERMINAL_JOB_STATUSES:
            return self._replay_terminal_job_result(
                job=current,
                links=links,
                workspace_id=workspace_id,
                owner_id=owner_id,
                progress_callback=progress_callback,
            )
        if current["status"] == "cancelling":
            try:
                self.job_orchestrator.finish_cancellation(
                    current,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
            except (JobStoreError, JobOrchestrationError):
                logger.debug(
                    "Recovery cancellation finalization remained incomplete"
                )
            refreshed = self.store.get_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if refreshed is not None and refreshed["status"] in TERMINAL_JOB_STATUSES:
                return self._replay_terminal_job_result(
                    job=refreshed,
                    links=self.store.list_job_run_links(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    progress_callback=progress_callback,
                )
            return self._persisted_job_outcome(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )

        recovery_owner = "solver_recovery_drift_{}".format(uuid.uuid4().hex)
        try:
            claimed = self.store.claim_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                lease_owner=recovery_owner,
                lease_seconds=max(self.job_orchestrator.lease_seconds, 60),
                expected_version=current["version"],
            )
        except (LeaseConflict, OptimisticLockError, TerminalStateError):
            refreshed = self.store.get_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if refreshed is not None and refreshed["status"] in TERMINAL_JOB_STATUSES:
                return self._replay_terminal_job_result(
                    job=refreshed,
                    links=self.store.list_job_run_links(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    progress_callback=progress_callback,
                )
            return self._persisted_job_outcome(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )

        attempt = self.store.latest_attempt(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        attempt_id = str((attempt or {}).get("attempt_id") or "")
        try:
            if attempt_id:
                self._best_effort_stop_attempt(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    attempt_id=attempt_id,
                    error_code=error_code,
                    lease_owner=recovery_owner,
                    fencing_token=claimed["fencing_token"],
                )
            self._terminalize(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                status="failed",
                phase=phase,
                error_code=error_code,
                fencing_token=claimed["fencing_token"],
            )
            self.store.release_job_lease(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                lease_owner=recovery_owner,
                fencing_token=claimed["fencing_token"],
            )
        except (LeaseConflict, OptimisticLockError, TerminalStateError):
            refreshed = self.store.get_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if refreshed is not None and refreshed["status"] in TERMINAL_JOB_STATUSES:
                return self._replay_terminal_job_result(
                    job=refreshed,
                    links=self.store.list_job_run_links(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    progress_callback=progress_callback,
                )
            return self._persisted_job_outcome(
                job_id=job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
        claimed_links = self.store.list_job_run_links(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        result = {
            "status": "error",
            "code": error_code,
            "phase": phase,
            "retryable": retryable,
            "authoritative": False,
            "job_id": job_id,
            "attempt_id": attempt_id or None,
        }
        for link in claimed_links:
            fresh_link = self.store.get_run_link(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=str(link["run_id"]),
                turn_id=str(link["turn_id"]),
                tool_call_id=str(link["tool_call_id"]),
            )
            if fresh_link is None or fresh_link.get("tool_result") is not None:
                continue
            try:
                if not bool(fresh_link.get("subscription_active", 1)):
                    self._write_cancelled_tool_result(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        run_id=str(fresh_link["run_id"]),
                        turn_id=str(fresh_link["turn_id"]),
                        tool_call_id=str(fresh_link["tool_call_id"]),
                        contract_digest=str(fresh_link["contract_digest"]),
                        job_id=job_id,
                        attempt_id=fresh_link.get("attempt_id") or attempt_id or None,
                    )
                    continue
                self._write_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(fresh_link["run_id"]),
                    turn_id=str(fresh_link["turn_id"]),
                    tool_call_id=str(fresh_link["tool_call_id"]),
                    contract_digest=str(fresh_link["contract_digest"]),
                    result=result,
                )
            except JobStoreConflict:
                latest_link = self.store.get_run_link(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(link["run_id"]),
                    turn_id=str(link["turn_id"]),
                    tool_call_id=str(link["tool_call_id"]),
                )
                if latest_link is not None and not bool(
                    latest_link.get("subscription_active", 1)
                ) and latest_link.get("tool_result") is None:
                    self._write_cancelled_tool_result(
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        run_id=str(latest_link["run_id"]),
                        turn_id=str(latest_link["turn_id"]),
                        tool_call_id=str(latest_link["tool_call_id"]),
                        contract_digest=str(latest_link["contract_digest"]),
                        job_id=job_id,
                        attempt_id=latest_link.get("attempt_id") or attempt_id or None,
                    )
                continue
        try:
            progress_callback(
                "solver.job.failed",
                {
                    "job_id": job_id,
                    "attempt_id": attempt_id or None,
                    "phase": phase,
                    "status": "failed",
                    "code": error_code,
                },
            )
        except Exception:
            logger.debug("Solver failure progress callback failed")
        return result

    def _replay_terminal_job_result(
        self,
        *,
        job: Mapping[str, Any],
        links: tuple[Mapping[str, Any], ...],
        workspace_id: str,
        owner_id: str,
        progress_callback: Callable[[str, Mapping[str, Any]], None],
        target_link_identity: tuple[str, str, str] | None = None,
    ) -> dict[str, Any]:
        job_id = str(job["job_id"])
        attempt = self.store.latest_attempt(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        attempt_id = (attempt or {}).get("attempt_id")
        if job["status"] == "completed":
            base_result = self._durable_authoritative_event_result(
                job=job,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
        else:
            base_result = self._backend_terminal_result(
                {**dict(job), "attempt_id": attempt_id}
            )

        primary_result: dict[str, Any] | None = None
        for index, link in enumerate(links):
            link_identity = (
                str(link["run_id"]),
                str(link["turn_id"]),
                str(link["tool_call_id"]),
            )
            is_target = (
                index == 0
                if target_link_identity is None
                else link_identity == target_link_identity
            )
            if is_target:
                self._drain_events(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(link["run_id"]),
                    turn_id=str(link["turn_id"]),
                    tool_call_id=str(link["tool_call_id"]),
                    fallback_attempt_id=attempt_id,
                    progress_callback=progress_callback,
                )
            existing = link.get("tool_result")
            if isinstance(existing, Mapping):
                delivered = dict(existing)
            elif not bool(link.get("subscription_active", 1)):
                delivered = self._write_cancelled_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(link["run_id"]),
                    turn_id=str(link["turn_id"]),
                    tool_call_id=str(link["tool_call_id"]),
                    contract_digest=str(link["contract_digest"]),
                    job_id=job_id,
                    attempt_id=link.get("attempt_id") or attempt_id,
                )
            else:
                delivered = dict(base_result)
                if delivered.get("authoritative") is True:
                    delivered["run_id"] = str(link["run_id"])
                    delivered = AuthoritativeResult(**delivered).to_dict()
                self._write_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=str(link["run_id"]),
                    turn_id=str(link["turn_id"]),
                    tool_call_id=str(link["tool_call_id"]),
                    contract_digest=str(link["contract_digest"]),
                    result=delivered,
                )
            if is_target:
                primary_result = delivered
        if primary_result is None:
            raise SolverServiceError(
                "solver_unavailable", "resume", "solver job has no recoverable run link"
            )
        return primary_result

    def _persisted_job_outcome(
        self,
        *,
        job_id: str,
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        job = self.store.get_job(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if job is None:
            raise SolverServiceError("solver_unavailable", "resume", "solver job is missing")
        attempt = self.store.latest_attempt(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        return {
            "job_id": job_id,
            "status": job["status"],
            "phase": job["phase"],
            "error_code": job.get("error_code"),
            "attempt_id": (attempt or {}).get("attempt_id"),
            "backend_job_id": (attempt or {}).get("backend_job_id"),
        }

    def _await_input_preparation(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        job_id: str,
        logical_job_key: str,
        attempt_id: str | None,
        subscription_id: str,
        cancel_requested: Callable[[], bool] | None,
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any] | None:
        poll_number = 0
        while True:
            link = self.store.get_run_link(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            if link is None:
                raise SolverServiceError(
                    "solver_unavailable", "resume", "solver run link disappeared"
                )
            if link.get("tool_result") is not None:
                return dict(link["tool_result"])
            job = self.store.get_job(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            if job is None:
                raise SolverServiceError(
                    "solver_unavailable", "resume", "solver job disappeared"
                )
            if not bool(link.get("subscription_active", 1)):
                return self._write_cancelled_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    job_id=job_id,
                    attempt_id=attempt_id,
                )
            if job["status"] in TERMINAL_JOB_STATUSES:
                return self._replay_terminal_job_result(
                    job=job,
                    links=self.store.list_job_run_links(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    ),
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    progress_callback=progress_callback,
                    target_link_identity=(run_id, turn_id, tool_call_id),
                )
            if job.get("phase") != "freeze_inputs":
                return None
            outcome = {
                "status": "running",
                "phase": "freeze_inputs",
                "job_id": job_id,
                "logical_job_key": logical_job_key,
                "attempt_id": attempt_id,
                "subscription_id": subscription_id,
                "authoritative": False,
            }
            cancelled = self._cancel_current_subscription_if_requested(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                outcome=outcome,
                subscription_id=subscription_id,
                cancel_requested=cancel_requested,
                progress_callback=progress_callback,
            )
            if cancelled is not None:
                return cancelled
            if self.max_polls is not None and poll_number >= self.max_polls:
                return outcome
            poll_number += 1
            self.sleep(max(float(self.poll_interval_seconds), 0.01))

    def _durable_authoritative_event_result(
        self,
        *,
        job: Mapping[str, Any],
        workspace_id: str,
        owner_id: str,
    ) -> dict[str, Any]:
        job_id = str(job["job_id"])
        run_policy = job.get("run_policy")
        if not isinstance(run_policy, Mapping):
            raise SolverServiceError(
                "solver_unavailable", "resume", "completed solver run policy is invalid"
            )
        package_revision = str(run_policy.get("package_revision") or "").strip()
        contract_digest = str(run_policy.get("contract_digest") or "").strip()
        if not package_revision or not contract_digest:
            raise SolverServiceError(
                "solver_unavailable", "resume", "completed solver contract is incomplete"
            )
        events = self.store.read_events(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        for event in reversed(events):
            if event.get("event_type") != "solver.result.authoritative":
                continue
            payload = event.get("payload")
            try:
                if not isinstance(payload, Mapping):
                    raise ValueError("authoritative event payload is invalid")
                authoritative = AuthoritativeResult(**dict(payload))
            except (TypeError, ValueError) as exc:
                raise SolverServiceError(
                    "solver_unavailable",
                    "resume",
                    "completed authoritative event is invalid",
                ) from exc
            if (
                authoritative.job_id != job_id
                or authoritative.knowledge_revision_id != package_revision
                or authoritative.problem_digest != job.get("problem_digest")
            ):
                raise SolverServiceError(
                    "solver_unavailable",
                    "resume",
                    "completed authoritative event does not match its job",
                )
            artifacts = self.store.list_artifacts(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
            )
            references = [
                ("solver_result", authoritative.result_artifact_ref),
                ("verification_evidence", authoritative.verification_evidence_ref),
            ]
            if authoritative.candidate_results_artifact_ref is not None:
                references.append(
                    (
                        "candidate_results",
                        authoritative.candidate_results_artifact_ref,
                    )
                )
            for artifact_type, artifact_ref in references:
                matches: list[Mapping[str, Any]] = []
                for artifact in artifacts:
                    if artifact.get("artifact_type") != artifact_type:
                        continue
                    if artifact.get("artifact_ref") == artifact_ref:
                        matches.append(artifact)
                if len(matches) != 1:
                    raise SolverServiceError(
                        "solver_unavailable",
                        "resume",
                        "completed authoritative artifact metadata is unavailable",
                    )
            return authoritative.to_dict()
        raise SolverServiceError(
            "solver_unavailable",
            "resume",
            "completed solver job has no authoritative event",
        )

    def _verify_and_publish(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        package_revision: str,
        capability: Any,
        normalized_input: Any,
        fact_snapshot: Any,
        problem: Any,
        outcome: Mapping[str, Any],
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        job_id = str(outcome["job_id"])
        attempt_id = str(outcome["attempt_id"])
        verification_job, verification_owner = self._acquire_verification_lease(
            job_id=job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if verification_owner is None:
            if verification_job["status"] == "completed":
                recovered = self._existing_authoritative_event(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    job_id=job_id,
                    package_revision=package_revision,
                    fact_snapshot=fact_snapshot,
                    problem=problem,
                )
                if recovered is None:
                    raise SolverServiceError(
                        "artifact_persist_failed",
                        "resume",
                        "completed solver job has no authoritative event",
                    )
                self._drain_events(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    fallback_attempt_id=attempt_id,
                    progress_callback=progress_callback,
                )
                self._write_tool_result(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    turn_id=turn_id,
                    tool_call_id=tool_call_id,
                    contract_digest=contract_digest,
                    result=recovered,
                )
                return recovered
            result = self._backend_terminal_result(
                {**verification_job, "attempt_id": attempt_id}
            )
            self._drain_events(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                fallback_attempt_id=attempt_id,
                progress_callback=progress_callback,
            )
            self._write_tool_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                result=result,
            )
            return result

        heartbeat_stop = threading.Event()
        heartbeat = threading.Thread(
            target=self._verification_lease_heartbeat,
            kwargs={
                "job_id": job_id,
                "workspace_id": workspace_id,
                "owner_id": owner_id,
                "lease_owner": verification_owner,
                "fencing_token": verification_job["fencing_token"],
                "lease_seconds": int(
                    getattr(self.job_orchestrator, "lease_seconds", 30)
                ),
                "stop_event": heartbeat_stop,
            },
            name="solver-verification-lease-heartbeat",
            daemon=True,
        )
        heartbeat.start()
        try:
            return self._verify_and_publish_claimed(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                package_revision=package_revision,
                capability=capability,
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
                problem=problem,
                outcome=outcome,
                verification_job=verification_job,
                progress_callback=progress_callback,
            )
        finally:
            heartbeat_stop.set()
            heartbeat.join(timeout=1.0)
            try:
                self.store.release_job_lease(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=verification_owner,
                    fencing_token=verification_job["fencing_token"],
                )
            except JobStoreError:
                logger.debug(
                    "Verification lease was already released or transferred"
                )

    def _verification_lease_heartbeat(
        self,
        *,
        job_id: str,
        workspace_id: str,
        owner_id: str,
        lease_owner: str,
        fencing_token: int,
        lease_seconds: int,
        stop_event: threading.Event,
    ) -> None:
        interval = max(0.1, float(lease_seconds) / 3.0)
        while not stop_event.wait(interval):
            try:
                self.store.renew_job_lease(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    lease_seconds=lease_seconds,
                )
            except LeaseConflict:
                return
            except JobStoreError:
                # A transient SQLite interruption should not immediately
                # surrender the fence; the next heartbeat can retry.
                continue

    def _acquire_verification_lease(
        self,
        *,
        job_id: str,
        workspace_id: str,
        owner_id: str,
    ) -> tuple[Mapping[str, Any], str | None]:
        lease_owner = "verification_{}".format(uuid.uuid4().hex)
        lease_seconds = int(getattr(self.job_orchestrator, "lease_seconds", 30))
        while True:
            current = self.store.get_job(
                job_id, workspace_id=workspace_id, owner_id=owner_id
            )
            if current is None:
                raise SolverServiceError(
                    "solver_unavailable", "verify", "solver job is missing"
                )
            if current["status"] in TERMINAL_JOB_STATUSES:
                return current, None
            if current["status"] != "solver_completed":
                raise SolverServiceError(
                    "solver_unavailable",
                    "verify",
                    "solver job is not ready for verification",
                )
            try:
                claimed = self.store.claim_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=lease_owner,
                    lease_seconds=lease_seconds,
                    expected_version=current["version"],
                )
                return claimed, lease_owner
            except (LeaseConflict, OptimisticLockError, TerminalStateError):
                self.sleep(float(self.poll_interval_seconds))

    def _verify_and_publish_claimed(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        package_revision: str,
        capability: Any,
        normalized_input: Any,
        fact_snapshot: Any,
        problem: Any,
        outcome: Mapping[str, Any],
        verification_job: Mapping[str, Any],
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        job_id = str(outcome["job_id"])
        attempt_id = str(outcome["attempt_id"])
        verification_fence = verification_job["fencing_token"]

        def cancelled_projection() -> dict[str, Any] | None:
            current = self.store.get_job(
                job_id, workspace_id=workspace_id, owner_id=owner_id
            )
            link = self.store.get_run_link(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            if current is None or link is None:
                return None
            cancellation_is_active = current["status"] in {
                "cancelling",
                "cancelled",
            }
            subscription_is_active = bool(link.get("subscription_active", 1))
            if not cancellation_is_active and subscription_is_active:
                return None
            self._drain_events(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                fallback_attempt_id=attempt_id,
                progress_callback=progress_callback,
            )
            return self._write_cancelled_tool_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
            )

        def fenced_fail_job(**kwargs: Any) -> dict[str, Any]:
            cancelled = cancelled_projection()
            if cancelled is not None:
                return cancelled
            return self._fail_job(
                fencing_token=verification_fence,
                **kwargs,
            )

        def fenced_fail_verification(**kwargs: Any) -> dict[str, Any]:
            cancelled = cancelled_projection()
            if cancelled is not None:
                return cancelled
            return self._fail_verification_subscription(
                fencing_token=verification_fence,
                **kwargs,
            )

        attempt = self.store.get_attempt(
            attempt_id, workspace_id=workspace_id, owner_id=owner_id
        )
        if attempt is None or not isinstance(attempt.get("result"), Mapping):
            return fenced_fail_job(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                code="solver_result_rejected",
                phase="verify",
                progress_callback=progress_callback,
            )
        try:
            solver_result = self._solver_result(attempt["result"])
            self._validate_solver_result_assignment(
                capability=capability,
                solver_result=solver_result,
                result_quality=self._result_quality(verification_job.get("run_policy")),
            )
            result_artifact = self._persist_artifact(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
                attempt_id=attempt_id,
                artifact_type="solver_result",
                payload=solver_result.to_dict(),
                lease_owner=str(verification_job.get("lease_owner") or ""),
                fencing_token=verification_fence,
            )

            verification = self._existing_verification(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
            )
            if verification is None:
                verification = _require_verification_result(
                    self.verifier.verify(
                        problem=problem,
                        capability=capability,
                        normalized_input=normalized_input,
                        fact_snapshot=fact_snapshot,
                        solver_result=solver_result,
                    )
                )
            evidence_artifact = self._persist_artifact(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
                attempt_id=attempt_id,
                artifact_type="verification_evidence",
                payload=verification.to_dict(),
                lease_owner=str(verification_job.get("lease_owner") or ""),
                fencing_token=verification_fence,
            )
            verification_id = "verification_{}".format(
                sha256_digest(
                    {
                        "job_id": job_id,
                        "attempt_id": attempt_id,
                        "verification": verification.to_dict(),
                    }
                ).rsplit(":", 1)[-1]
            )
            self.store.add_verification_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
                attempt_id=attempt_id,
                verification_id=verification_id,
                status=verification.status,
                result=verification.to_dict(),
                lease_owner=str(verification_job.get("lease_owner") or ""),
                fencing_token=verification_fence,
            )
            validate_authoritative_verification(
                fact_snapshot=fact_snapshot,
                problem=problem,
                solver_result=solver_result,
                verification=verification,
            )
            candidate_results_artifact = None
            if self.candidate_results_builder is not None:
                candidate_results = self.candidate_results_builder(
                    problem=problem,
                    capability=capability,
                    solver_result=solver_result,
                    verification=verification,
                )
                candidate_payload = (
                    candidate_results.to_dict()
                    if callable(getattr(candidate_results, "to_dict", None))
                    else candidate_results
                )
                if not isinstance(candidate_payload, Mapping):
                    raise SolverResultRejectedError(
                        "candidate results builder returned invalid data"
                    )
                candidate_results_artifact = self._persist_artifact(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    artifact_type="candidate_results",
                    payload=candidate_payload,
                    lease_owner=str(verification_job.get("lease_owner") or ""),
                    fencing_token=verification_fence,
                )
            summary = None
            if self.presenter is not None and verification.feasibility == "feasible":
                summary = self.presenter.present(
                    capability=capability,
                    normalized_input=normalized_input,
                    solver_result=solver_result,
                    verification=verification,
                )
            authoritative = self.result_projector.project(
                run_id=run_id,
                job_id=job_id,
                package_revision=package_revision,
                fact_snapshot=fact_snapshot,
                problem=problem,
                solver_result=solver_result,
                verification=verification,
                result_artifact_ref=result_artifact["artifact_ref"],
                verification_evidence_ref=evidence_artifact["artifact_ref"],
                candidate_results_artifact_ref=(
                    candidate_results_artifact["artifact_ref"]
                    if candidate_results_artifact is not None
                    else None
                ),
                candidate_results_artifact_digest=(
                    candidate_results_artifact["artifact_digest"]
                    if candidate_results_artifact is not None
                    else None
                ),
                evidence_persisted=True,
                summary=summary,
            )
        except ArtifactPersistError:
            return fenced_fail_verification(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                code="artifact_persist_failed",
                phase="publish",
                progress_callback=progress_callback,
            )
        except OSError:
            return fenced_fail_verification(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                code="artifact_persist_failed",
                phase="publish",
                progress_callback=progress_callback,
            )
        except VerificationDivergenceError:
            return fenced_fail_job(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                code="solver_result_rejected",
                phase="verify",
                progress_callback=progress_callback,
            )
        except SolverResultRejectedError as exc:
            return fenced_fail_job(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                # Keep quality-policy failures distinct, while normalizing the
                # projector's non-solution diagnostic to the tool-facing
                # rejection state used by the orchestration contract.
                code=(
                    "quality_requirement_not_met"
                    if getattr(exc, "code", "") == "quality_requirement_not_met"
                    else "solver_result_rejected"
                ),
                phase="verify",
                progress_callback=progress_callback,
            )
        except VerificationRuntimeFailure:
            return fenced_fail_verification(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                code="verification_runtime_failed",
                phase="verify",
                progress_callback=progress_callback,
            )
        except (LeaseConflict, OptimisticLockError, TerminalStateError) as exc:
            cancelled = cancelled_projection()
            if cancelled is not None:
                return cancelled
            raise SolverServiceError(
                "solver_unavailable",
                "verify",
                "solver verification lost its lease",
            ) from exc
        except (JobStoreError, ValueError, TypeError):
            return fenced_fail_job(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
                code="solver_result_rejected",
                phase="verify",
                progress_callback=progress_callback,
            )

        public_result = authoritative.to_dict()
        verification_event_payload = {
            "phase": "verify",
            "status": verification.status,
            "feasibility": verification.feasibility,
            "optimality": verification.optimality,
            "proof_kind": verification.proof_kind,
        }
        try:
            self.store.complete_job_with_authoritative_events(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                expected_version=verification_job["version"],
                fencing_token=verification_job["fencing_token"],
                attempt_id=attempt_id,
                verification_payload=verification_event_payload,
                authoritative_payload=public_result,
            )
        except (JobStoreConflict, OptimisticLockError, LeaseConflict, TerminalStateError) as exc:
            current = self.store.get_job(
                job_id, workspace_id=workspace_id, owner_id=owner_id
            )
            if current is None or current["status"] not in TERMINAL_JOB_STATUSES:
                raise SolverServiceError(
                    "solver_unavailable",
                    "publish",
                    "solver completion lost its verification fence",
                ) from exc
            if current["status"] == "completed":
                recovered = self._existing_authoritative_event(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    run_id=run_id,
                    job_id=job_id,
                    package_revision=package_revision,
                    fact_snapshot=fact_snapshot,
                    problem=problem,
                )
                if recovered is None:
                    raise SolverServiceError(
                        "artifact_persist_failed",
                        "resume",
                        "completed solver job has no authoritative event",
                    ) from exc
                public_result = recovered
            else:
                public_result = self._backend_terminal_result(
                    {**current, "attempt_id": attempt_id}
                )
        self._drain_events(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            fallback_attempt_id=attempt_id,
            progress_callback=progress_callback,
        )
        try:
            self._write_tool_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                fencing_token=verification_fence,
                result=public_result,
            )
        except (LeaseConflict, JobStoreConflict):
            link = self.store.get_run_link(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
            )
            if link is None or bool(link.get("subscription_active", 1)):
                raise
            return self._write_cancelled_tool_result(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                contract_digest=contract_digest,
                job_id=job_id,
                attempt_id=attempt_id,
            )
        return public_result

    def _fail_job(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        job_id: str,
        attempt_id: str,
        code: str,
        phase: str,
        retryable: bool | None = None,
        fencing_token: int | None = None,
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        self._terminalize(
            job_id=job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            status="failed",
            phase=phase,
            error_code=code,
            fencing_token=fencing_token,
        )
        result = {
            "status": "error",
            "code": code,
            "phase": phase,
            "retryable": self._is_retryable(code) if retryable is None else retryable,
            "authoritative": False,
            "job_id": job_id,
            "attempt_id": attempt_id,
        }
        self.store.append_event(
            job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            attempt_id=attempt_id,
            event_type=(
                "solver.verification.rejected"
                if phase == "verify"
                else "solver.job.failed"
            ),
            payload={"phase": phase, "status": "failed", "code": code},
        )
        self._drain_events(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            fallback_attempt_id=attempt_id,
            progress_callback=progress_callback,
        )
        self._write_tool_result(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            contract_digest=contract_digest,
            fencing_token=fencing_token,
            result=result,
        )
        return result

    def _fail_verification_subscription(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        contract_digest: str,
        job_id: str,
        attempt_id: str,
        code: str,
        phase: str,
        fencing_token: int | None = None,
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> dict[str, Any]:
        current = self.store.get_job(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        if current is None:
            raise SolverServiceError("solver_unavailable", phase, "solver job is missing")
        if current["status"] in TERMINAL_JOB_STATUSES:
            result = self._backend_terminal_result(
                {**current, "attempt_id": attempt_id}
            )
        elif current["status"] == "solver_completed":
            result = {
                "status": "error",
                "code": code,
                "phase": phase,
                "retryable": True,
                "authoritative": False,
                "job_id": job_id,
                "attempt_id": attempt_id,
            }
            self.store.append_event(
                job_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                attempt_id=attempt_id,
                event_type="solver.verification.rejected",
                payload={"phase": phase, "status": "failed", "code": code},
            )
        else:
            raise SolverServiceError(
                "solver_unavailable",
                phase,
                "solver job left verification before retryable failure was recorded",
            )
        self._drain_events(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            fallback_attempt_id=attempt_id,
            progress_callback=progress_callback,
        )
        self._write_tool_result(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
            contract_digest=contract_digest,
            fencing_token=fencing_token,
            result=result,
        )
        return result

    def _resume_frozen_context(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        package_id: str,
        package_revision: str,
        operation_key: str,
        contract_digest: str,
        schema_digest: str,
        existing_link: Mapping[str, Any],
    ) -> tuple[Mapping[str, Any], NormalizedInput, FactSnapshot, ProblemIR]:
        job = self.store.get_job(
            existing_link["job_id"],
            workspace_id=workspace_id,
            owner_id=owner_id,
        )
        if job is None:
            raise SolverServiceError(
                "solver_contract_drift", "resume", "linked solver job is missing"
            )
        run_policy = job.get("run_policy")
        expected_policy = {
            "contract_digest": contract_digest,
            "schema_digest": schema_digest,
            "package_id": package_id,
            "package_revision": package_revision,
            "operation_key": operation_key,
        }
        if not isinstance(run_policy, Mapping) or any(
            run_policy.get(name) != expected
            for name, expected in expected_policy.items()
        ):
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "persisted solver run policy does not match the Tool contract",
            )

        normalized_payload = self._load_frozen_artifact(
            workspace_id=workspace_id,
            owner_id=owner_id,
            job_id=job["job_id"],
            artifact_type="normalized_input",
        )
        fact_payload = self._load_frozen_artifact(
            workspace_id=workspace_id,
            owner_id=owner_id,
            job_id=job["job_id"],
            artifact_type="fact_snapshot",
        )
        problem_payload = self._load_frozen_artifact(
            workspace_id=workspace_id,
            owner_id=owner_id,
            job_id=job["job_id"],
            artifact_type="problem_ir",
        )
        try:
            normalized_input = self._normalized_input(normalized_payload)
            fact_snapshot = self._fact_snapshot(fact_payload)
            problem = self._problem_ir(problem_payload)
        except (ValueError, TypeError) as exc:
            raise SolverServiceError(
                "artifact_persist_failed",
                "resume",
                "frozen solver input is invalid",
            ) from exc

        if normalized_input.operation_key != operation_key:
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "frozen fact snapshot identity does not match the Tool invocation",
            )
        if normalized_input.to_dict() != normalized_payload:
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "frozen fact snapshot identity does not match the Tool invocation",
            )
        if (
            fact_snapshot.workspace_id != workspace_id
            or fact_snapshot.owner_id != owner_id
            or fact_snapshot.package_id != package_id
        ):
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "frozen fact snapshot identity does not match the Tool invocation",
            )
        if (
            fact_snapshot.package_revision != package_revision
            or fact_snapshot.normalized_input_digest != normalized_input.digest
        ):
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "frozen fact snapshot identity does not match the Tool invocation",
            )
        if fact_snapshot.to_dict() != fact_payload:
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "frozen solver problem does not match the linked job",
            )
        if (
            problem.to_dict() != problem_payload
            or problem.to_dict() != job.get("problem")
            or problem.problem_id != job.get("problem_digest")
        ):
            raise SolverServiceError(
                "solver_contract_drift",
                "resume",
                "frozen solver problem does not match the linked job",
            )
        return job, normalized_input, fact_snapshot, problem

    def _matching_retryable_verification_link(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        card_name: str,
        contract_digest: str,
        normalized_input: NormalizedInput,
    ) -> Mapping[str, Any] | None:
        seen_job_ids: set[str] = set()
        for link in self.store.list_retryable_verification_links(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            card_name=card_name,
            contract_digest=contract_digest,
        ):
            job_id = str(link["job_id"])
            if job_id in seen_job_ids:
                continue
            seen_job_ids.add(job_id)
            payload = self._load_frozen_artifact(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
                artifact_type="normalized_input",
            )
            try:
                candidate = self._normalized_input(payload)
            except (ValueError, TypeError) as exc:
                raise SolverServiceError(
                    "artifact_persist_failed",
                    "resume",
                    "frozen solver input is invalid",
                ) from exc
            if candidate.to_dict() != payload:
                raise SolverServiceError(
                    "artifact_persist_failed",
                    "resume",
                    "frozen solver input is not canonical",
                )
            if candidate.to_dict() == normalized_input.to_dict():
                return link
        return None

    def _load_frozen_artifact(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        artifact_type: str,
    ) -> Mapping[str, Any]:
        try:
            records: list[Mapping[str, Any]] = []
            for record in self.store.list_artifacts(
                job_id, workspace_id=workspace_id, owner_id=owner_id
            ):
                if record.get("artifact_type") == artifact_type:
                    records.append(record)
            if len(records) != 1:
                raise ArtifactPersistError(
                    "frozen artifact must have exactly one persisted record"
                )
            record = records[0]
            getter = getattr(self.artifact_store, "get", None)
            if not callable(getter):
                raise ArtifactPersistError("artifact store cannot restore frozen input")
            payload = getter(record.get("artifact_ref"))
            if (
                not isinstance(payload, Mapping)
                or record.get("artifact_digest") != sha256_digest(payload)
            ):
                raise ArtifactPersistError("frozen artifact metadata is inconsistent")
            return dict(payload)
        except Exception as exc:
            if isinstance(exc, SolverServiceError):
                raise
            raise SolverServiceError(
                "artifact_persist_failed",
                "resume",
                "frozen solver input could not be restored",
            ) from exc

    @staticmethod
    def _normalized_input(payload: Mapping[str, Any]) -> NormalizedInput:
        expected = {
            "schema_version",
            "operation_key",
            "values",
            "source_evidence",
            "normalization",
        }
        if set(payload) != expected:
            raise ValueError("persisted normalized input fields are invalid")
        return NormalizedInput(
            schema_version=payload["schema_version"],
            operation_key=payload["operation_key"],
            values=payload["values"],
            source_evidence=tuple(payload["source_evidence"]),
            normalization=payload["normalization"],
        )

    @staticmethod
    def _fact_snapshot(payload: Mapping[str, Any]) -> FactSnapshot:
        expected = {
            "schema_version",
            "workspace_id",
            "owner_id",
            "run_id",
            "package_id",
            "package_revision",
            "normalized_input_digest",
            "facts",
            "bindings",
            "as_of",
            "source_evidence",
            "checks",
            "snapshot_digest",
        }
        if set(payload) != expected:
            raise ValueError("persisted fact snapshot fields are invalid")
        return FactSnapshot(
            schema_version=payload["schema_version"],
            workspace_id=payload["workspace_id"],
            owner_id=payload["owner_id"],
            run_id=payload["run_id"],
            package_id=payload["package_id"],
            package_revision=payload["package_revision"],
            normalized_input_digest=payload["normalized_input_digest"],
            facts=tuple(payload["facts"]),
            bindings=payload["bindings"],
            as_of=payload["as_of"],
            source_evidence=tuple(payload["source_evidence"]),
            checks=payload["checks"],
            snapshot_digest=payload["snapshot_digest"],
        )

    @staticmethod
    def _job_scoped_fact_snapshot(fact_snapshot: FactSnapshot) -> FactSnapshot:
        """Drop caller-local provenance before persisting one shareable Job snapshot."""

        if not isinstance(fact_snapshot, FactSnapshot):
            raise TypeError("fact_snapshot must be a FactSnapshot")
        return FactSnapshot(
            schema_version=fact_snapshot.schema_version,
            workspace_id=fact_snapshot.workspace_id,
            owner_id=fact_snapshot.owner_id,
            run_id="",
            package_id=fact_snapshot.package_id,
            package_revision=fact_snapshot.package_revision,
            normalized_input_digest=fact_snapshot.normalized_input_digest,
            facts=fact_snapshot.facts,
            bindings=fact_snapshot.bindings,
            as_of=fact_snapshot.as_of,
            source_evidence=fact_snapshot.source_evidence,
            checks=fact_snapshot.checks,
            snapshot_digest=fact_snapshot.snapshot_digest,
        )

    @staticmethod
    def _problem_ir(payload: Mapping[str, Any]) -> ProblemIR:
        expected = {
            "schema_version",
            "problem_id",
            "problem_family",
            "decision_space",
            "parameters",
            "constraints",
            "objectives",
            "limits",
        }
        if set(payload) != expected:
            raise ValueError("persisted problem IR fields are invalid")
        return ProblemIR(
            schema_version=payload["schema_version"],
            problem_id=payload["problem_id"],
            problem_family=payload["problem_family"],
            decision_space=payload["decision_space"],
            parameters=payload["parameters"],
            constraints=tuple(payload["constraints"]),
            objectives=tuple(payload["objectives"]),
            limits=payload["limits"],
        )

    def _persist_frozen_inputs(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        attempt_id: str | None,
        normalized_input: Mapping[str, Any],
        fact_snapshot: Mapping[str, Any],
        problem: Mapping[str, Any],
        lease_owner: str | None = None,
        fencing_token: int | None = None,
        lease_seconds: int | None = None,
    ) -> None:
        for artifact_type, payload in (
            ("normalized_input", normalized_input),
            ("fact_snapshot", fact_snapshot),
            ("problem_ir", problem),
        ):
            try:
                if lease_owner is not None and fencing_token is not None:
                    self.store.renew_job_lease(
                        job_id,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                        lease_owner=lease_owner,
                        fencing_token=fencing_token,
                        lease_seconds=max(
                            30,
                            int(
                                lease_seconds
                                or getattr(self.job_orchestrator, "lease_seconds", 30)
                            ),
                        ),
                    )
                self._persist_artifact(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    artifact_type=artifact_type,
                    payload=payload,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                )
            except (
                LeaseConflict,
                OptimisticLockError,
                TerminalStateError,
                ArtifactPersistError,
                OSError,
                JobStoreError,
            ) as exc:
                if isinstance(
                    exc,
                    (LeaseConflict, OptimisticLockError, TerminalStateError),
                ):
                    raise
                raise SolverServiceError(
                    "artifact_persist_failed",
                    "snapshot",
                    "frozen solver input could not be persisted",
                ) from exc

    def _persist_artifact(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        attempt_id: str | None,
        artifact_type: str,
        payload: Mapping[str, Any],
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> Mapping[str, str]:
        record = self.artifact_store.put(
            artifact_type=artifact_type, payload=payload
        )
        if not isinstance(record, Mapping) or set(record) != {
            "artifact_ref",
            "artifact_digest",
            "artifact_type",
        }:
            raise ArtifactPersistError("artifact store returned invalid metadata")
        if record["artifact_type"] != artifact_type:
            raise ArtifactPersistError("artifact store changed artifact type")
        self.store.add_artifact(
            workspace_id=workspace_id,
            owner_id=owner_id,
            job_id=job_id,
            attempt_id=attempt_id,
            artifact_type=artifact_type,
            artifact_digest=record["artifact_digest"],
            artifact_ref=record["artifact_ref"],
            lease_owner=lease_owner,
            fencing_token=fencing_token,
        )
        return record

    def _existing_verification(
        self, *, workspace_id: str, owner_id: str, job_id: str
    ) -> VerificationResult | None:
        records = self.store.list_verification_results(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        if not records:
            return None
        return self._verification_result(records[-1]["result"])

    def _existing_authoritative_event(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        job_id: str,
        package_revision: str,
        fact_snapshot: FactSnapshot,
        problem: ProblemIR,
    ) -> dict[str, Any] | None:
        events = self.store.read_events(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        for event in reversed(events):
            if event.get("event_type") != "solver.result.authoritative":
                continue
            payload = event.get("payload")
            try:
                if not isinstance(payload, Mapping):
                    raise ValueError("authoritative event payload is invalid")
                authoritative = AuthoritativeResult(**dict(payload))
            except (TypeError, ValueError) as exc:
                raise SolverServiceError(
                    "artifact_persist_failed",
                    "resume",
                    "persisted authoritative result is invalid",
                ) from exc
            if (
                authoritative.job_id != job_id
                or authoritative.knowledge_revision_id != package_revision
            ):
                raise SolverServiceError(
                    "solver_contract_drift",
                    "resume",
                    "persisted authoritative result does not match the linked job",
                )
            if (
                authoritative.fact_snapshot_digest != fact_snapshot.digest
                or authoritative.problem_digest != problem.problem_id
            ):
                raise SolverServiceError(
                    "solver_contract_drift",
                    "resume",
                    "persisted authoritative result does not match the linked job",
                )
            self._verify_artifact_reference(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
                artifact_type="solver_result",
                artifact_ref=authoritative.result_artifact_ref,
            )
            self._verify_artifact_reference(
                workspace_id=workspace_id,
                owner_id=owner_id,
                job_id=job_id,
                artifact_type="verification_evidence",
                artifact_ref=authoritative.verification_evidence_ref,
            )
            if authoritative.candidate_results_artifact_ref is not None:
                self._verify_artifact_reference(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    job_id=job_id,
                    artifact_type="candidate_results",
                    artifact_ref=authoritative.candidate_results_artifact_ref,
                    expected_digest=authoritative.candidate_results_artifact_digest,
                )
            projected = authoritative.to_dict()
            projected["run_id"] = run_id
            return AuthoritativeResult(**projected).to_dict()
        return None

    def _verify_artifact_reference(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        job_id: str,
        artifact_type: str,
        artifact_ref: str,
        expected_digest: str | None = None,
    ) -> None:
        try:
            records: list[Mapping[str, Any]] = []
            for record in self.store.list_artifacts(
                job_id, workspace_id=workspace_id, owner_id=owner_id
            ):
                if record.get("artifact_type") != artifact_type:
                    continue
                if record.get("artifact_ref") == artifact_ref:
                    records.append(record)
            if len(records) != 1:
                raise ArtifactPersistError("referenced solver artifact is missing")
            if (
                expected_digest is not None
                and records[0].get("artifact_digest") != expected_digest
            ):
                raise ArtifactPersistError("referenced solver artifact digest diverged")
            getter = getattr(self.artifact_store, "get", None)
            if not callable(getter):
                raise ArtifactPersistError("artifact store cannot verify result evidence")
            payload = getter(artifact_ref)
            if (
                not isinstance(payload, Mapping)
                or sha256_digest(payload) != records[0].get("artifact_digest")
            ):
                raise ArtifactPersistError("referenced solver artifact is inconsistent")
        except Exception as exc:
            if isinstance(exc, SolverServiceError):
                raise
            raise SolverServiceError(
                "artifact_persist_failed",
                "resume",
                "persisted authoritative evidence could not be restored",
            ) from exc

    def _drain_events(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        run_id: str,
        turn_id: str,
        tool_call_id: str,
        fallback_attempt_id: str | None,
        progress_callback: Callable[[str, Mapping[str, Any]], None],
    ) -> None:
        link = self.store.get_run_link(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id=run_id,
            turn_id=turn_id,
            tool_call_id=tool_call_id,
        )
        if link is None:
            return
        events = self.store.read_events(
            link["job_id"],
            workspace_id=workspace_id,
            owner_id=owner_id,
            after_event_seq=link["last_event_seq"],
        )
        for event in events:
            payload = dict(event["payload"])
            payload.update(
                {
                    "run_id": run_id,
                    "job_id": event["job_id"],
                    "attempt_id": event.get("attempt_id") or fallback_attempt_id,
                    "event_seq": event["event_seq"],
                }
            )
            if event["event_type"] != "solver.tool_result.ready":
                try:
                    progress_callback(event["event_type"], payload)
                except Exception:
                    logger.debug("Solver progress callback failed")
            self.store.advance_run_link_event_seq(
                workspace_id=workspace_id,
                owner_id=owner_id,
                run_id=run_id,
                turn_id=turn_id,
                tool_call_id=tool_call_id,
                event_seq=event["event_seq"],
            )

    def _terminalize(
        self,
        *,
        job_id: str,
        workspace_id: str,
        owner_id: str,
        status: str,
        phase: str,
        error_code: str | None,
        fencing_token: int | None = None,
    ) -> dict[str, Any]:
        for _ in range(8):
            current = self.store.get_job(
                job_id, workspace_id=workspace_id, owner_id=owner_id
            )
            if current is None:
                raise SolverServiceError("solver_unavailable", phase, "solver job is missing")
            if current["status"] in TERMINAL_JOB_STATUSES:
                return current
            try:
                return self.store.update_job(
                    job_id,
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    expected_version=current["version"],
                    status=status,
                    phase=phase,
                    error_code=error_code,
                    fencing_token=fencing_token,
                )
            except OptimisticLockError:
                continue
        raise SolverServiceError(
            "solver_unavailable", phase, "solver job changed too frequently"
        )

    def _write_tool_result(self, *, result: Mapping[str, Any], **identity: Any) -> None:
        self.store.write_tool_result(result=result, **identity)

    def _best_effort_stop_attempt(
        self,
        *,
        workspace_id: str,
        owner_id: str,
        attempt_id: str,
        error_code: str = "artifact_persist_failed",
        lease_owner: str | None = None,
        fencing_token: int | None = None,
    ) -> None:
        if not attempt_id:
            return
        attempt = self.store.get_attempt(
            attempt_id, workspace_id=workspace_id, owner_id=owner_id
        )
        if attempt is None:
            return
        if attempt.get("status") in TERMINAL_ATTEMPT_STATUSES:
            return
        if lease_owner is not None and fencing_token is not None:
            try:
                self.store.renew_job_lease(
                    attempt["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    lease_owner=lease_owner,
                    fencing_token=fencing_token,
                    lease_seconds=max(self.job_orchestrator.lease_seconds, 60),
                )
            except (JobStoreError, TerminalStateError):
                return
        backend_job_id = attempt.get("backend_job_id")
        if isinstance(backend_job_id, str) and backend_job_id:
            try:
                # Production connector backends require the immutable binding
                # on every remote call.  Recovery used to call ``cancel``
                # directly, which silently failed for remote connectors
                # and could leave a remote Connector job running after the
                # platform had terminalized its attempt.  Go through the
                # orchestrator's neutral adapter so the persisted Binding is
                # loaded and narrow fixture backends (without ``binding`` in
                # their signature) remain supported.
                job = self.store.get_job(
                    attempt["job_id"],
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                )
                backend_call = getattr(self.job_orchestrator, "_backend_call", None)
                binding_loader = getattr(
                    self.job_orchestrator, "_binding_configuration", None
                )
                if callable(backend_call) and callable(binding_loader) and job is not None:
                    binding = binding_loader(
                        job=job,
                        workspace_id=workspace_id,
                        owner_id=owner_id,
                    )
                    backend_call(
                        "cancel",
                        binding=binding,
                        job_id=backend_job_id,
                    )
                else:
                    # Narrow migration fallback for old in-memory test doubles.
                    self.job_orchestrator.backend.cancel(job_id=backend_job_id)
            except Exception:
                logger.warning(
                    "Best-effort solver backend cancellation failed",
                )
        try:
            self.store.update_attempt_status(
                attempt_id,
                workspace_id=workspace_id,
                owner_id=owner_id,
                status="failed",
                error_code=error_code,
                fencing_token=fencing_token,
            )
        except JobStoreError:
            logger.debug("Failed solver attempt status was already finalized")

    def _resolve_capability(
        self,
        *,
        contract_drift_on_missing: bool = False,
        **identity: str,
    ) -> Any:
        try:
            capability = self.capability_resolver.resolve(**identity)
        except Exception as exc:
            if contract_drift_on_missing:
                raise SolverServiceError(
                    "solver_contract_drift",
                    "resume",
                    "persisted solver capability is no longer available",
                ) from exc
            raise SolverServiceError(
                "solver_unavailable", "select", "solver capability could not be resolved"
            ) from exc
        if capability is None:
            if contract_drift_on_missing:
                raise SolverServiceError(
                    "solver_contract_drift",
                    "resume",
                    "persisted solver capability is no longer available",
                )
            raise SolverServiceError(
                "solver_unavailable", "select", "solver capability was not found"
            )
        return capability

    def _validate_capability_lock(
        self,
        *,
        capability: Any,
        package_id: str,
        package_revision: str,
        operation_key: str,
        contract_digest: str,
        schema_digest: str,
        phase: str,
    ) -> None:
        locked = {
            "package_id": package_id,
            "package_revision": package_revision,
            "operation_key": operation_key,
            "contract_digest": contract_digest,
        }
        for field_name, expected in locked.items():
            if self._capability_value(capability, field_name) != expected:
                raise SolverServiceError(
                    "solver_contract_drift", phase, "solver capability identity changed"
                )
        schema = self._capability_value(capability, "input_schema")
        if not isinstance(schema, Mapping):
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver input schema is missing"
            )
        try:
            actual_schema_digest = json_schema_digest(schema)
        except ValueError as exc:
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver input schema is invalid"
            ) from exc
        if schema_digest != actual_schema_digest:
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver input schema changed"
            )
        result_schema = self._capability_value(capability, "result_schema")
        if not isinstance(result_schema, Mapping):
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver result schema is missing"
            )
        try:
            actual_result_schema_digest = json_schema_digest(result_schema)
        except ValueError as exc:
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver result schema is invalid"
            ) from exc
        if self._capability_value(
            capability, "result_schema_digest"
        ) != actual_result_schema_digest:
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver result schema changed"
            )
        try:
            SolverAssignmentSchemaValidator(schema=result_schema)
        except Exception as exc:
            raise SolverServiceError(
                "solver_contract_drift", phase, "solver result schema is invalid"
            ) from exc

    def _validate_solver_result_assignment(
        self,
        *,
        capability: Any,
        solver_result: SolverResult,
        result_quality: str | None = None,
    ) -> None:
        if solver_result.status == "infeasible":
            if solver_result.assignment or solver_result.objective_values:
                raise SolverResultRejectedError(
                    "infeasible solver result contains an assignment"
                )
            return
        if solver_result.status not in {"optimal", "feasible"}:
            raise SolverResultRejectedError("solver result status is unsupported")
        requested_quality = result_quality
        if requested_quality is None:
            requested_quality = self._result_quality(capability)
        if requested_quality not in {"optimal", "feasible", "any"}:
            raise SolverResultRejectedError(
                "locked solver result quality is invalid",
                code="quality_requirement_not_met",
            )
        if requested_quality == "optimal" and solver_result.status != "optimal":
            raise SolverResultRejectedError(
                "solver result does not satisfy optimal quality requirement",
                code="quality_requirement_not_met",
            )
        result_schema = self._capability_value(capability, "result_schema")
        if not isinstance(result_schema, Mapping):
            raise SolverResultRejectedError("locked solver result schema is invalid")
        SolverAssignmentSchemaValidator(schema=result_schema).validate(
            solver_result.assignment
        )

    def _resolve_model(self, capability: Any) -> tuple[Any, tuple[str, ...]]:
        try:
            resolved, required = self.compiler.resolve_capability_model(capability)
        except Exception as exc:
            raise SolverServiceError(
                "model_compile_failed", "compile", "locked decision model is invalid"
            ) from exc
        return resolved, required

    @staticmethod
    def _binding(
        payload: Any,
        *,
        endpoint_policy: EndpointPolicy | None = None,
    ) -> tuple[str, Mapping[str, Any]]:
        if not isinstance(payload, Mapping):
            raise SolverServiceError(
                "solver_unavailable", "select", "solver binding is invalid"
            )
        revision = payload.get("binding_revision_id")
        configuration = payload.get("configuration")
        if not isinstance(revision, str) or not revision.strip() or not isinstance(
            configuration, Mapping
        ):
            raise SolverServiceError(
                "solver_unavailable", "select", "solver binding is invalid"
            )
        try:
            from .bindings import SolverBindingRevision

            binding = SolverBindingRevision.from_configuration(
                revision_id=revision.strip(),
                workspace_id="runtime-selection-scope",
                configuration=configuration,
                endpoint_policy=endpoint_policy,
            )
        except Exception as exc:
            raise SolverServiceError(
                "solver_unavailable", "select", "solver binding is invalid"
            ) from exc
        return binding.revision_id, binding.to_configuration()

    @staticmethod
    def _assert_backend_capabilities(
        checker: Callable[..., Any],
        binding: Mapping[str, Any],
        *,
        required_capabilities: Any,
    ) -> None:
        """Run the live capability handshake without breaking old fixtures."""

        try:
            parameters = inspect.signature(checker).parameters
            accepts_kwargs = any(
                parameter.kind is inspect.Parameter.VAR_KEYWORD
                for parameter in parameters.values()
            )
        except (TypeError, ValueError):
            accepts_kwargs = True
            parameters = {}
        if accepts_kwargs or "required_capabilities" in parameters:
            checker(
                binding,
                required_capabilities=required_capabilities,
            )
        else:
            checker(binding)

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime) or value.tzinfo is None:
            raise SolverServiceError(
                "solver_unavailable", "submit_job", "solver clock is invalid"
            )
        return value.astimezone(timezone.utc)

    @staticmethod
    def _deadline(value: Any) -> datetime:
        try:
            parsed = (
                value
                if isinstance(value, datetime)
                else datetime.fromisoformat(str(value).replace("Z", "+00:00"))
            )
        except (TypeError, ValueError) as exc:
            raise SolverServiceError(
                "solver_contract_drift", "resume", "persisted solver deadline is invalid"
            ) from exc
        if parsed.tzinfo is None:
            raise SolverServiceError(
                "solver_contract_drift", "resume", "persisted solver deadline is invalid"
            )
        return parsed.astimezone(timezone.utc)

    @staticmethod
    def _backend_terminal_result(outcome: Mapping[str, Any]) -> dict[str, Any]:
        status = str(outcome["status"])
        code = str(outcome.get("error_code") or "solver_unavailable")
        if status == "failed" and code == "attempt_lost":
            code = "attempt_lost"
        return {
            "status": status if status in {"timed_out", "cancelled"} else "error",
            "code": code,
            "phase": outcome.get("phase") or "solve",
            "retryable": SolverCoordinationService._is_retryable(code),
            "authoritative": False,
            "job_id": outcome["job_id"],
            "attempt_id": outcome.get("attempt_id"),
        }

    @staticmethod
    def _solver_result(payload: Mapping[str, Any]) -> SolverResult:
        if set(payload) != {
            "schema_version",
            "problem_id",
            "status",
            "assignment",
            "objective_values",
            "diagnostics",
            "engine",
        }:
            raise ValueError("persisted solver result fields are invalid")
        return SolverResult(
            schema_version=payload["schema_version"],
            problem_id=payload["problem_id"],
            status=payload["status"],
            assignment=payload["assignment"],
            objective_values=payload["objective_values"],
            diagnostics=tuple(payload["diagnostics"]),
            engine=payload["engine"],
        )

    @staticmethod
    def _verification_result(payload: Mapping[str, Any]) -> VerificationResult:
        if set(payload) != {
            "schema_version",
            "problem_id",
            "status",
            "feasibility",
            "optimality",
            "proof_kind",
            "diagnostics",
            "evidence",
        }:
            raise ValueError("persisted verification fields are invalid")
        return VerificationResult(
            schema_version=payload["schema_version"],
            problem_id=payload["problem_id"],
            status=payload["status"],
            feasibility=payload["feasibility"],
            optimality=payload["optimality"],
            proof_kind=payload["proof_kind"],
            diagnostics=tuple(payload["diagnostics"]),
            evidence=tuple(payload["evidence"]),
        )

    @staticmethod
    def _capability_value(capability: Any, field_name: str) -> Any:
        if isinstance(capability, Mapping):
            return capability.get(field_name)
        return getattr(capability, field_name, None)

    @classmethod
    def _result_quality(cls, source: Any) -> str:
        """Read the neutral quality requirement and fail closed on drift."""

        if isinstance(source, Mapping):
            value = source.get("result_quality")
            if value is None and isinstance(source.get("requirements"), Mapping):
                value = source["requirements"].get("result_quality")
            if value is None and isinstance(source.get("solver_requirements"), Mapping):
                value = source["solver_requirements"].get("result_quality")
        else:
            value = getattr(source, "result_quality", None)
        value = value or "optimal"
        if value not in {"optimal", "feasible", "any"}:
            raise SolverServiceError(
                "solver_contract_drift",
                "select",
                "solver result quality requirement is invalid",
            )
        return str(value)

    @staticmethod
    def _is_retryable(code: str) -> bool:
        definition = ERRORS.get(str(code or ""))
        return bool(definition and definition.retryable)


UnifiedSolverCoordinationService = SolverCoordinationService
