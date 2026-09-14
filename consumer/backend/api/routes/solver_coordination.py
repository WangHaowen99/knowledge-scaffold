"""Typed candidate-set generation endpoint for solver coordination."""

from __future__ import annotations

import json
from pathlib import Path
import uuid
from collections.abc import Mapping

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from api.auth.auth import get_current_user
from api.dependencies import require_workspace_id, run_in_runtime_pool
from api.routes.errors import workspace_not_found_response
from utils.logging import get_logger
from solver_coordination.canonical import sha256_digest
from solver_coordination.candidate_service import create_candidate_set
from solver_coordination.candidate_results import CandidateResultsArtifact
from solver_coordination.candidate_store import (
    CandidateSetArtifactIntegrityError,
    CandidateSetNotFoundError,
    CandidateSetStore,
    CandidateSetStoreError,
)
from solver_coordination.contracts import SolverContractError
from solver_coordination.errors import (
    ArtifactPersistError,
    CandidateDigestMismatchError,
    RunNotFoundError,
    error_payload,
)
from solver_coordination.problem_families.binary_ordered_route import assert_candidate_set_shape
from solver_coordination.job_orchestrator import (
    BackendProtocolError,
    BackendTransientError,
    JobOrchestrationError,
)
from solver_coordination.job_store import JobStoreConflict, JobStoreError
from solver_coordination.run_ledger import RunLedgerError, SolverRunLedger
from solver_coordination.validation_store import (
    ValidationRunConflict,
    ValidationRunStore,
    ValidationRunStoreError,
)
from workspace.workspaces import WorkspaceNotFoundError, workspace_scope


logger = get_logger("api.routes.solver_coordination")

solver_coordination_router = APIRouter(
    prefix="/agentic/solver-coordination/v1",
)

# Stable public contract.  The older agentic path remains registered for
# compatibility with clients of the first slice.
solver_coordination_public_router = APIRouter(
    prefix="/api/solver",
)

# Temporary import aliases for focused integrations that have not yet renamed
# the router symbol. Both values are native APIRouter instances.
solver_coordination_bp = solver_coordination_router
solver_coordination_public_bp = solver_coordination_public_router


def _response(payload: Mapping[str, object] | list[object], status_code: int = 200):
    return JSONResponse(content=payload, status_code=status_code)


def response_for_test(
    payload: Mapping[str, object] | list[object],
    status_code: int = 200,
):
    """Build an HTTP response through the production serialization path."""
    return _response(payload, status_code)


def _service(request: Request, key: str):
    services = getattr(request.app.state, "services", {})
    return services.get(key) if isinstance(services, Mapping) else None


async def _optional_json_object(request: Request) -> dict | None:
    content_type = str(request.headers.get("content-type") or "")
    media_type = content_type.partition(";")[0].strip().casefold()
    is_json = media_type == "application/json" or (
        media_type.startswith("application/") and media_type.endswith("+json")
    )
    if not is_json:
        return None
    try:
        payload = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


async def _retry_json_object(request: Request) -> dict | None:
    raw_body = await request.body()
    if not raw_body:
        return {}
    return await _optional_json_object(request)


async def _run_long_solver_task(
    request: Request,
    function,
    *args,
    timeout_code: str,
    **kwargs,
):
    try:
        return await run_in_runtime_pool(
            request,
            "tool_io",
            function,
            *args,
            **kwargs,
        )
    except TimeoutError:
        logger.warning("solver route task timed out: %s", timeout_code)
        return _response(error_payload(timeout_code), 503)


@solver_coordination_router.post("/candidate-sets")
async def create_candidate_set_api(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    payload = await _optional_json_object(request)
    user = get_current_user()
    owner_id = str(getattr(user, "username", "") or "").strip()
    return await run_in_runtime_pool(
        request,
        "short_io",
        _create_candidate_set_response,
        workspace_id=workspace_id,
        owner_id=owner_id,
        payload=payload,
    )


def _create_candidate_set_response(
    *,
    workspace_id: str,
    owner_id: str,
    payload: dict | None,
):
    ledger = None
    solve_run_id = None
    try:
        if not isinstance(payload, dict):
            raise SolverContractError("request body must be a JSON object")
        if set(payload) - {"knowledge_package_id", "knowledge_package_revision", "plan"}:
            raise SolverContractError("request contains unsupported fields")
        package_id = payload.get("knowledge_package_id")
        package_revision = payload.get("knowledge_package_revision")
        if not isinstance(package_id, str) or not package_id.strip():
            raise SolverContractError(
                "knowledge_package_id is required and must be a string"
            )
        if not isinstance(package_revision, str) or not package_revision.strip():
            raise SolverContractError(
                "knowledge_package_revision is required and must be a string"
            )
        if not owner_id:
            return _response({"error": "user is not resolved"}, 401)
        with workspace_scope(workspace_id):
            candidate_set = create_candidate_set(
                plan_payload=payload.get("plan"),
                knowledge_package_id=package_id,
                knowledge_package_revision=package_revision,
            )
            assert_candidate_set_shape(candidate_set)
            solve_run_id = candidate_set.source_run_id
            ledger = SolverRunLedger(workspace_id)
            ledger.create_run(
                run_id=solve_run_id,
                owner_id=owner_id,
                run_type="solve",
                phase="persisting",
                status="running",
                problem_digest=candidate_set.problem_digest,
                candidate_set_digest=candidate_set.candidate_set_digest,
            )
            _ref, artifact = CandidateSetStore(workspace_id, ledger=ledger).put(
                candidate_set,
                owner_id=owner_id,
            )
            ledger.update_run(
                solve_run_id,
                owner_id=owner_id,
                phase="candidate_ready",
                status="completed",
            )
    except SolverContractError:
        _mark_run_failed(ledger, solve_run_id, owner_id, "invalid_solver_input")
        return _response(error_payload("invalid_solver_input"), 400)
    except CandidateSetStoreError:
        _mark_run_failed(ledger, solve_run_id, owner_id, "candidate_store_unavailable")
        return _response(error_payload("candidate_store_unavailable"), 503)
    except RunLedgerError:
        return _response(error_payload("candidate_store_unavailable"), 503)
    except ValueError:
        _mark_run_failed(ledger, solve_run_id, owner_id, "invalid_solver_input")
        return _response(error_payload("invalid_solver_input"), 400)

    public_artifact = {
        key: value for key, value in artifact.items() if key not in {"owner_id", "created_at"}
    }
    return _response(public_artifact, 201)


def _validation_service(request: Request):
    return _service(request, "SOLVER_COORDINATION_VALIDATION_SERVICE")


def _coordination_service(request: Request):
    return _service(request, "SOLVER_COORDINATION_SERVICE")


def _mark_run_failed(
    ledger: SolverRunLedger | None,
    run_id: str | None,
    owner_id: str | None,
    error_code: str,
) -> None:
    """Best-effort failure accounting that never masks the public error."""

    if ledger is None or not run_id or not owner_id:
        return
    try:
        ledger.update_run(
            run_id,
            owner_id=owner_id,
            phase="failed",
            status="failed",
            error_code=error_code,
        )
    except RunLedgerError:
        logger.exception("failed to update solver run ledger")


@solver_coordination_router.post("/validation-runs")
async def create_validation_run_api(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    """Validate one immutable candidate set through the injected service."""
    payload = await _optional_json_object(request)
    user = get_current_user()
    owner_id = str(getattr(user, "username", "") or "").strip()
    return await _run_long_solver_task(
        request,
        _create_validation_run_response,
        workspace_id=workspace_id,
        owner_id=owner_id,
        payload=payload,
        idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
        service=_validation_service(request),
        timeout_code="validation_service_unavailable",
    )


def _create_validation_run_response(
    *,
    workspace_id: str,
    owner_id: str,
    payload: dict | None,
    idempotency_key: str,
    service,
):
    response_status = 201
    ledger = None
    validation_run_id = None
    try:
        if not isinstance(payload, dict):
            raise SolverContractError("request body must be a JSON object")
        allowed = {
            "source_run_id",
            "parent_solve_run_id",
            "candidate_set_ref",
            "candidate_set_digest",
            "candidate_digest",
            "validator_revision",
        }
        unknown = set(payload) - allowed
        if unknown:
            raise SolverContractError("request contains unsupported fields")
        source_run_id = payload.get("source_run_id")
        parent_solve_run_id = payload.get("parent_solve_run_id")
        if source_run_id is None:
            source_run_id = parent_solve_run_id
        elif parent_solve_run_id is not None and source_run_id != parent_solve_run_id:
            raise SolverContractError("source run identifiers do not match")
        candidate_set_ref = payload.get("candidate_set_ref")
        candidate_set_digest = payload.get("candidate_set_digest")
        candidate_digest = payload.get("candidate_digest")
        validator_revision = payload.get("validator_revision")
        for name, value in (
            ("source_run_id", source_run_id),
            ("candidate_set_ref", candidate_set_ref),
            ("candidate_set_digest", candidate_set_digest),
        ):
            if not isinstance(value, str) or not value.strip():
                raise SolverContractError("{} is required and must be a string".format(name))
        if candidate_digest is not None and (
            not isinstance(candidate_digest, str) or not candidate_digest.strip()
        ):
            raise SolverContractError("candidate_digest must be a non-empty string")
        if not isinstance(validator_revision, str) or not validator_revision.strip():
            raise SolverContractError("validator_revision is required and must be a string")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise SolverContractError("Idempotency-Key header is required")
        if not owner_id:
            return _response({"error": "user is not resolved"}, 401)
        if service is None or not callable(getattr(service, "run", None)):
            return _response(error_payload("validation_service_unavailable"), 503)
        configured_revision = getattr(service, "validator_revision", None)
        if not isinstance(configured_revision, str) or not configured_revision.strip():
            return _response(error_payload("validation_service_unavailable"), 503)
        if validator_revision.strip() != configured_revision.strip():
            raise SolverContractError("validator_revision does not match configured validator")

        request_fingerprint = sha256_digest(
            {
                "source_run_id": source_run_id.strip(),
                "parent_solve_run_id": (parent_solve_run_id or source_run_id).strip(),
                "candidate_set_ref": candidate_set_ref.strip(),
                "candidate_set_digest": candidate_set_digest.strip(),
                "candidate_digest": (candidate_digest or "").strip(),
                "validator_revision": (validator_revision or "").strip(),
            }
        )
        store = ValidationRunStore(workspace_id)
        with store.idempotency_lock(
            workspace_id=workspace_id,
            owner_id=owner_id,
            idempotency_key=idempotency_key,
        ):
            existing = store.get_idempotent(
                idempotency_key=idempotency_key,
                owner_id=owner_id,
                request_fingerprint=request_fingerprint,
            )
            if existing is not None:
                private_fields = {
                    "owner_id",
                    "created_at",
                    "request_fingerprint",
                    "payload_digest",
                }
                public_existing = {}
                for key, value in existing.items():
                    if key not in private_fields:
                        public_existing[key] = value
                return _response(public_existing, 200)

            validation_run_id = "validation_run_{}".format(uuid.uuid4().hex)
            ledger = SolverRunLedger(workspace_id)
            candidate_for_lineage = CandidateSetStore(
                workspace_id, ledger=ledger
            ).get(candidate_set_ref.strip(), owner_id=owner_id)
            # The artifact's original source run is authoritative.  A caller
            # may not substitute an arbitrary parent id merely because the
            # candidate-set content digest is valid.
            if candidate_for_lineage.source_run_id != (parent_solve_run_id or source_run_id).strip():
                raise CandidateSetNotFoundError("candidate parent run is not bound to artifact")
            ledger.create_run(
                run_id=validation_run_id,
                owner_id=owner_id,
                run_type="validation",
                phase="validating",
                status="running",
                parent_run_id=(parent_solve_run_id or source_run_id).strip(),
                problem_digest=candidate_for_lineage.problem_digest,
                candidate_set_digest=candidate_set_digest.strip(),
                candidate_digest=candidate_digest.strip() if candidate_digest else None,
            )
            with workspace_scope(workspace_id):
                result = service.run(
                    workspace_id=workspace_id,
                    owner_id=owner_id,
                    candidate_set_ref=candidate_set_ref.strip(),
                    source_run_id=source_run_id.strip(),
                    candidate_set_digest=candidate_set_digest.strip(),
                    validation_run_id=validation_run_id,
                    validator_revision=validator_revision.strip() if validator_revision else None,
                    candidate_digest=candidate_digest.strip() if candidate_digest else None,
                    parent_solve_run_id=parent_solve_run_id.strip() if parent_solve_run_id else None,
                )
            response_payload = result.to_dict()
            response_payload.update(
                {
                    "candidate_set_ref": candidate_set_ref.strip(),
                    "problem_digest": candidate_for_lineage.problem_digest,
                    "validator_revision": configured_revision.strip(),
                }
            )
            stored = store.put(
                idempotency_key=idempotency_key,
                owner_id=owner_id,
                request_fingerprint=request_fingerprint,
                payload=response_payload,
            )
            ledger.update_run(
                validation_run_id,
                owner_id=owner_id,
                phase="completed",
                status="completed" if result.batch_status == "validation_completed" else "failed",
                error_code=("validation_failed" if result.batch_status != "validation_completed" else None),
            )
    except ValidationRunConflict:
        _mark_run_failed(ledger, validation_run_id, owner_id, "idempotency_conflict")
        return _response(error_payload("idempotency_conflict"), 409)
    except CandidateDigestMismatchError:
        _mark_run_failed(ledger, validation_run_id, owner_id, "candidate_digest_mismatch")
        return _response(error_payload("candidate_digest_mismatch"), 409)
    except CandidateSetNotFoundError:
        _mark_run_failed(ledger, validation_run_id, owner_id, "candidate_not_found")
        return _response(error_payload("candidate_not_found"), 404)
    except CandidateSetArtifactIntegrityError:
        _mark_run_failed(ledger, validation_run_id, owner_id, "candidate_digest_mismatch")
        return _response(error_payload("candidate_digest_mismatch"), 409)
    except CandidateSetStoreError:
        # Do not turn storage outages into a misleading 404.
        _mark_run_failed(ledger, validation_run_id, owner_id, "candidate_store_unavailable")
        return _response(error_payload("candidate_store_unavailable"), 503)
    except ValidationRunStoreError:
        _mark_run_failed(ledger, validation_run_id, owner_id, "validation_store_unavailable")
        return _response(error_payload("validation_store_unavailable"), 503)
    except RunLedgerError:
        return _response(error_payload("validation_store_unavailable"), 503)
    except SolverContractError:
        _mark_run_failed(ledger, validation_run_id, owner_id, "validation_input_invalid")
        return _response(error_payload("validation_input_invalid"), 400)
    except ValueError:
        _mark_run_failed(ledger, validation_run_id, owner_id, "validation_input_invalid")
        return _response(error_payload("validation_input_invalid"), 400)

    private_fields = {
        "owner_id",
        "created_at",
        "request_fingerprint",
        "payload_digest",
    }
    public_payload = {}
    for key, value in stored.items():
        if key not in private_fields:
            public_payload[key] = value
    return _response(public_payload, response_status)


def _public_run_response(*, workspace_id: str, owner_id: str, run_id: str):
    try:
        run_id = str(run_id or "").strip()
        if not owner_id:
            return _response({"error": "Unauthorized"}, 401)
        if not run_id:
            raise RunNotFoundError()
        run = SolverRunLedger(workspace_id).get_run(run_id, owner_id=owner_id)
        if run is None:
            raise RunNotFoundError()
        public_run = {
            key: value for key, value in run.items()
            if key not in {"owner_id", "created_at", "updated_at", "workspace_id"}
        }
        return _response(public_run, 200)
    except RunNotFoundError:
        return _response(error_payload("run_not_found"), 404)
    except (ValueError, RunLedgerError):
        return _response(error_payload("run_not_found"), 404)


async def _get_solver_run_response(
    request: Request,
    *,
    workspace_id: str,
    run_id: str,
):
    user = get_current_user()
    return await run_in_runtime_pool(
        request,
        "short_io",
        _public_run_response,
        workspace_id=workspace_id,
        owner_id=str(getattr(user, "username", "") or "").strip(),
        run_id=run_id,
    )


@solver_coordination_router.get("/runs/{run_id}")
async def get_solver_run_api(
    request: Request,
    run_id: str,
    workspace_id: str = Depends(require_workspace_id),
):
    return await _get_solver_run_response(
        request,
        workspace_id=workspace_id,
        run_id=run_id,
    )


@solver_coordination_public_router.get("/runs/{run_id}")
async def get_solver_run_public_api(
    request: Request,
    run_id: str,
    workspace_id: str = Depends(require_workspace_id),
):
    return await _get_solver_run_response(
        request,
        workspace_id=workspace_id,
        run_id=run_id,
    )


@solver_coordination_public_router.post("/runs")
async def create_solver_run_alias(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    payload = await _optional_json_object(request)
    user = get_current_user()
    owner_id = str(getattr(user, "username", "") or "").strip()
    service = _coordination_service(request)
    if service is not None:
        return await _run_long_solver_task(
            request,
            _create_solver_run_through_coordination_service,
            service,
            workspace_id=workspace_id,
            owner_id=owner_id,
            payload=payload,
            idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
            traceparent=str(request.headers.get("traceparent") or ""),
            timeout_code="solver_unavailable",
        )
    return await run_in_runtime_pool(
        request,
        "short_io",
        _create_candidate_set_response,
        workspace_id=workspace_id,
        owner_id=owner_id,
        payload=payload,
    )


@solver_coordination_public_router.post("/validations")
async def create_solver_validation_alias(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    if _coordination_service(request) is not None:
        return _response(
            {
                "status": "error",
                "code": "standalone_validation_retired",
                "phase": "verify",
                "retryable": False,
            },
            410,
        )
    payload = await _optional_json_object(request)
    user = get_current_user()
    return await _run_long_solver_task(
        request,
        _create_validation_run_response,
        workspace_id=workspace_id,
        owner_id=str(getattr(user, "username", "") or "").strip(),
        payload=payload,
        idempotency_key=str(request.headers.get("Idempotency-Key") or ""),
        service=_validation_service(request),
        timeout_code="validation_service_unavailable",
    )


def _create_solver_run_through_coordination_service(
    service,
    *,
    workspace_id: str,
    owner_id: str,
    payload: dict | None,
    idempotency_key: str,
    traceparent: str,
):
    """Adapt the retired manual endpoint to the same Agent Tool service path."""

    from agent.tools.solver import build_solver_tool_specs
    from ontology.ontology_catalog import get_ontology_manifest
    from solver_coordination.capability_catalog import (
        SolverContractResolver,
    )

    if not isinstance(payload, dict):
        return _response(error_payload("invalid_solver_input"), 400)
    allowed = {
        "knowledge_package_id",
        "knowledge_package_revision",
        "operation_key",
        "arguments",
        "plan",
    }
    if set(payload) - allowed:
        return _response(error_payload("invalid_solver_input"), 400)
    package_id = str(payload.get("knowledge_package_id") or "").strip()
    package_revision = str(
        payload.get("knowledge_package_revision") or ""
    ).strip()
    requested_operation = str(payload.get("operation_key") or "").strip()
    idempotency_key = str(idempotency_key or "").strip()
    if not package_id or not package_revision or not idempotency_key:
        return _response(error_payload("invalid_solver_input"), 400)
    if not owner_id:
        return _response({"error": "Unauthorized"}, 401)
    ontology, error = get_ontology_manifest(
        package_id,
        version=package_revision,
        workspace_id=workspace_id,
    )
    if error or ontology is None or ontology.version != package_revision:
        return _response(error_payload("solver_unavailable"), 503)
    manifest_path = Path(ontology.manifest_path).resolve()
    if not manifest_path.is_file() or manifest_path.is_symlink():
        return _response(error_payload("solver_unavailable"), 503)
    try:
        import yaml

        manifest = yaml.safe_load(manifest_path.read_text(encoding="utf-8")) or {}
    except (OSError, UnicodeError, yaml.YAMLError):
        manifest = {}
    if not isinstance(manifest, Mapping):
        return _response(error_payload("solver_unavailable"), 503)
    capabilities = SolverContractResolver(
        Path(ontology.ontology_dir).resolve(),
        manifest,
        package_revision=package_revision,
    ).load_public()
    if not capabilities:
        return _response(error_payload("solver_unavailable"), 503)
    specs = list(build_solver_tool_specs(capabilities))
    if requested_operation:
        specs = [
            spec
            for spec in specs
            if spec.binding.operation_key == requested_operation
        ]
    if len(specs) != 1:
        return _response(error_payload("solver_capability_drift"), 409)
    binding = specs[0].binding
    arguments = payload.get("arguments")
    if arguments is None:
        legacy_plan = payload.get("plan")
        properties = dict(binding.input_schema).get("properties") or {}
        arguments = (
            {"plan": legacy_plan}
            if isinstance(properties, dict) and "plan" in properties
            else legacy_plan
        )
    if not isinstance(arguments, dict):
        return _response(error_payload("invalid_solver_input"), 400)
    invocation_digest = sha256_digest(
        {
            "workspace_id": workspace_id,
            "owner_id": owner_id,
            "idempotency_key": idempotency_key,
        }
    ).rsplit(":", 1)[-1]
    with workspace_scope(workspace_id):
        result = service.start_or_resume(
            workspace_id=workspace_id,
            owner_id=owner_id,
            run_id="compat_run_{}".format(invocation_digest[:32]),
            turn_id=1,
            tool_call_id="compat_tool_{}".format(invocation_digest[32:]),
            card_name=binding.card_name,
            package_id=binding.package_id,
            package_revision=binding.package_revision,
            operation_key=binding.operation_key,
            contract_digest=binding.contract_digest,
            schema_digest=binding.schema_digest,
            arguments=arguments,
            progress_callback=lambda _event_type, _event_payload: None,
            traceparent=str(traceparent or "").strip() or None,
        )
    return _response(result, 200)


@solver_coordination_public_router.post("/history/{job_id}/retry")
async def retry_solver_history_api(
    request: Request,
    job_id: str,
    workspace_id: str = Depends(require_workspace_id),
):
    """Create an explicit new attempt for one retryable failed Job.

    The endpoint deliberately does not accept a new problem, endpoint, or
    binding.  The durable orchestrator reloads those values from the original
    Job, so a retry cannot silently switch Connector or mutate frozen input.
    ``attempt_id`` is optional for backwards-compatible callers; supplying it
    makes a repeated HTTP request idempotent at the attempt boundary.
    """

    owner_id, service, failure = _solver_history_scope(request)
    if failure is not None:
        return failure
    payload = await _retry_json_object(request)
    return await run_in_runtime_pool(
        request,
        "short_io",
        _retry_solver_history_response,
        service=service,
        workspace_id=workspace_id,
        owner_id=owner_id,
        job_id=job_id,
        payload=payload,
    )


def _retry_solver_history_response(
    *,
    service,
    workspace_id: str,
    owner_id: str,
    job_id: str,
    payload: dict | None,
):
    if not isinstance(payload, dict) or set(payload) - {"attempt_id"}:
        return _response(error_payload("invalid_solver_input"), 400)
    attempt_id = payload.get("attempt_id")
    if attempt_id is not None and (
        not isinstance(attempt_id, str) or not attempt_id.strip()
    ):
        return _response(error_payload("invalid_solver_input"), 400)
    retry = getattr(service, "retry_job", None)
    if not callable(retry):
        return _response(error_payload("solver_unavailable"), 503)
    try:
        result = retry(
            job_id=job_id,
            workspace_id=workspace_id,
            owner_id=owner_id,
            attempt_id=attempt_id.strip() if isinstance(attempt_id, str) else None,
        )
        if not isinstance(result, dict):
            raise JobStoreError("solver retry returned an invalid response")
        return _response(result, 202)
    except JobStoreConflict:
        return _response(error_payload("solver_retry_not_allowed"), 409)
    except BackendTransientError:
        return _response(error_payload("solver_unavailable"), 503)
    except BackendProtocolError:
        return _response(error_payload("solver_retry_not_allowed"), 409)
    except JobOrchestrationError as exc:
        # Keep orchestration internals out of the public API.  A missing or
        # out-of-scope Job is indistinguishable from a history miss; all other
        # durable/backend failures are temporarily unavailable and may be
        # retried by the caller after inspecting the job history.
        if "not found" in str(exc).casefold():
            return _response(error_payload("run_not_found"), 404)
        return _response(error_payload("solver_unavailable"), 503)
    except JobStoreError:
        return _response(error_payload("solver_unavailable"), 503)
    except (TypeError, ValueError):
        return _response(error_payload("invalid_solver_input"), 400)
    except WorkspaceNotFoundError as exc:
        return workspace_not_found_response(exc)
    except Exception as exc:
        code = str(getattr(exc, "code", "solver_unavailable") or "solver_unavailable")
        phase = str(getattr(exc, "phase", "solve") or "solve")
        status = 409 if code in {"solver_contract_drift", "solver_capability_drift"} else 503
        return _response(
            {
                "status": "error",
                "code": code,
                "phase": phase,
                "retryable": status == 503,
            },
            status,
        )


_PUBLIC_AUTHORITATIVE_FIELDS = frozenset(
    {
        "schema_version",
        "run_id",
        "job_id",
        "status",
        "feasibility",
        "optimality",
        "proof_kind",
        "authoritative",
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
_PUBLIC_EVIDENCE_EVENT_FIELDS = _PUBLIC_AUTHORITATIVE_FIELDS | frozenset(
    {"phase", "code", "percent", "elapsed_ms"}
)


def _solver_history_scope(request: Request):
    user = get_current_user()
    owner_id = str(getattr(user, "username", "") or "").strip()
    if not owner_id:
        return None, None, _response({"error": "Unauthorized"}, 401)
    service = _coordination_service(request)
    if service is None or getattr(service, "store", None) is None:
        return None, None, _response(error_payload("solver_unavailable"), 503)
    return owner_id, service, None


def _public_history_item(store, *, workspace_id: str, owner_id: str, job: dict):
    detail = store.get_job(
        job["job_id"], workspace_id=workspace_id, owner_id=owner_id
    )
    if detail is None:
        return None
    policy = detail.get("run_policy")
    policy = policy if isinstance(policy, dict) else {}
    public = {
        "job_id": job["job_id"],
        "status": job["status"],
        "phase": job["phase"],
        "error_code": job.get("error_code"),
        "operation_key": policy.get("operation_key"),
        "package_revision": policy.get("package_revision"),
        "problem_digest": job["problem_digest"],
        "created_at": job["created_at"],
        "updated_at": job["updated_at"],
        "terminal_at": job.get("terminal_at"),
    }
    events = store.read_events(
        job["job_id"],
        workspace_id=workspace_id,
        owner_id=owner_id,
    )
    authoritative = None
    for event in reversed(events):
        if event.get("event_type") != "solver.result.authoritative":
            continue
        event_payload = event.get("payload")
        if isinstance(event_payload, dict):
            authoritative = event_payload
            break
    if authoritative is not None:
        for key, value in authoritative.items():
            if key in _PUBLIC_AUTHORITATIVE_FIELDS:
                public[key] = value
        public["status"] = job["status"]
    return public


@solver_coordination_public_router.get("/history")
async def list_solver_history_api(
    request: Request,
    workspace_id: str = Depends(require_workspace_id),
):
    owner_id, service, failure = _solver_history_scope(request)
    if failure is not None:
        return failure
    return await run_in_runtime_pool(
        request,
        "short_io",
        _list_solver_history_response,
        service=service,
        workspace_id=workspace_id,
        owner_id=owner_id,
        raw_limit=str(request.query_params.get("limit", "50")),
        raw_cursor=str(request.query_params.get("cursor") or ""),
    )


def _list_solver_history_response(
    *,
    service,
    workspace_id: str,
    owner_id: str,
    raw_limit: str,
    raw_cursor: str,
):
    try:
        limit = int(raw_limit)
        cursor = str(raw_cursor or "").strip() or None
        page = service.store.list_jobs(
            workspace_id=workspace_id,
            owner_id=owner_id,
            limit=limit,
            cursor=cursor,
        )
        items = []
        for job in page["items"]:
            public = _public_history_item(
                service.store,
                workspace_id=workspace_id,
                owner_id=owner_id,
                job=job,
            )
            if public is not None:
                items.append(public)
        return _response(
            {"items": items, "next_cursor": page.get("next_cursor")},
            200,
        )
    except (TypeError, ValueError, JobStoreError):
        return _response(error_payload("invalid_solver_input"), 400)


@solver_coordination_public_router.get(
    "/history/{job_id}/candidate-results"
)
async def get_solver_history_candidate_results_api(
    request: Request,
    job_id: str,
    workspace_id: str = Depends(require_workspace_id),
):
    """Return one verifier-backed candidate presentation artifact."""
    owner_id, service, failure = _solver_history_scope(request)
    if failure is not None:
        return failure
    return await run_in_runtime_pool(
        request,
        "short_io",
        _solver_history_candidate_results_response,
        service=service,
        workspace_id=workspace_id,
        owner_id=owner_id,
        job_id=job_id,
    )


def _solver_history_candidate_results_response(
    *,
    service,
    workspace_id: str,
    owner_id: str,
    job_id: str,
):
    try:
        job = service.store.get_job(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        if job is None:
            return _response(error_payload("run_not_found"), 404)
        if job.get("status") != "completed":
            return _response(error_payload("solver_result_rejected"), 409)

        events = service.store.read_events(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        authoritative = None
        for event in reversed(events):
            if event.get("event_type") != "solver.result.authoritative":
                continue
            event_payload = event.get("payload")
            if not isinstance(event_payload, Mapping):
                continue
            if event_payload.get("authoritative") is not True:
                continue
            if event_payload.get("job_id") == job_id:
                authoritative = event_payload
                break
        if authoritative is None:
            return _response(error_payload("solver_result_rejected"), 409)

        artifact_ref = authoritative.get("candidate_results_artifact_ref")
        artifact_digest = authoritative.get("candidate_results_artifact_digest")
        if artifact_ref is None and artifact_digest is None:
            return _response(error_payload("candidate_not_found"), 404)
        if not isinstance(artifact_ref, str) or not artifact_ref.strip():
            return _response(error_payload("candidate_digest_mismatch"), 409)
        if not isinstance(artifact_digest, str) or not artifact_digest.strip():
            return _response(error_payload("candidate_digest_mismatch"), 409)

        matching_artifacts = []
        artifacts = service.store.list_artifacts(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        for artifact in artifacts:
            if artifact.get("artifact_type") != "candidate_results":
                continue
            if artifact.get("artifact_ref") == artifact_ref:
                matching_artifacts.append(artifact)
        if (
            len(matching_artifacts) != 1
            or matching_artifacts[0].get("artifact_digest") != artifact_digest
        ):
            return _response(error_payload("candidate_digest_mismatch"), 409)

        artifact_reader = getattr(service.artifact_store, "get", None)
        if not callable(artifact_reader):
            return _response(error_payload("artifact_persist_failed"), 503)
        payload = artifact_reader(artifact_ref)
        if not isinstance(payload, Mapping) or sha256_digest(payload) != artifact_digest:
            return _response(error_payload("candidate_digest_mismatch"), 409)
        artifact = CandidateResultsArtifact.from_dict(payload)
        if (
            artifact.digest != artifact_digest
            or artifact.problem_digest != authoritative.get("problem_digest")
            or artifact.problem_digest != job.get("problem_digest")
        ):
            return _response(error_payload("candidate_digest_mismatch"), 409)
        return _response(artifact.to_dict(), 200)
    except SolverContractError:
        return _response(error_payload("candidate_digest_mismatch"), 409)
    except (ArtifactPersistError, OSError):
        return _response(error_payload("artifact_persist_failed"), 503)
    except JobStoreError:
        return _response(error_payload("artifact_persist_failed"), 503)
    except (TypeError, ValueError):
        return _response(error_payload("candidate_digest_mismatch"), 409)


@solver_coordination_public_router.get("/history/{job_id}/evidence")
async def get_solver_history_evidence_api(
    request: Request,
    job_id: str,
    workspace_id: str = Depends(require_workspace_id),
):
    owner_id, service, failure = _solver_history_scope(request)
    if failure is not None:
        return failure
    return await run_in_runtime_pool(
        request,
        "short_io",
        _solver_history_evidence_response,
        service=service,
        workspace_id=workspace_id,
        owner_id=owner_id,
        job_id=job_id,
    )


def _solver_history_evidence_response(
    *,
    service,
    workspace_id: str,
    owner_id: str,
    job_id: str,
):
    try:
        job = service.store.get_job(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        if job is None:
            return _response(error_payload("run_not_found"), 404)
        artifacts = service.store.list_artifacts(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        artifact_reader = getattr(service.artifact_store, "get", None)
        public_artifacts = []
        for artifact in artifacts:
            if callable(artifact_reader):
                artifact_reader(artifact["artifact_ref"])
            public_artifacts.append(
                {
                    "artifact_type": artifact["artifact_type"],
                    "artifact_digest": artifact["artifact_digest"],
                    "artifact_ref": artifact["artifact_ref"],
                    "created_at": artifact["created_at"],
                    "integrity_verified": callable(artifact_reader),
                }
            )
        verifications = service.store.list_verification_results(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        public_verifications = [
            {
                "verification_id": record["verification_id"],
                "status": record["status"],
                "result_digest": record["result_digest"],
                "result": record["result"],
                "created_at": record["created_at"],
            }
            for record in verifications
        ]
        events = service.store.read_events(
            job_id, workspace_id=workspace_id, owner_id=owner_id
        )
        public_events = [
            {
                "event_seq": event["event_seq"],
                "event_type": event["event_type"],
                "created_at": event["created_at"],
                "payload": {
                    key: value
                    for key, value in event["payload"].items()
                    if key in _PUBLIC_EVIDENCE_EVENT_FIELDS
                },
            }
            for event in events
        ]
        history_item = _public_history_item(
            service.store,
            workspace_id=workspace_id,
            owner_id=owner_id,
            job=job,
        )
        return _response(
            {
                "job": history_item,
                "artifacts": public_artifacts,
                "verification_results": public_verifications,
                "events": public_events,
            },
            200,
        )
    except JobStoreError:
        return _response(error_payload("run_not_found"), 404)
    except (ArtifactPersistError, OSError, ValueError):
        return _response(error_payload("artifact_persist_failed"), 409)
