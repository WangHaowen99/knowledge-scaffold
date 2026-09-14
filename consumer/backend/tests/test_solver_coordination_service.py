# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
from __future__ import annotations

from internal_access import internal
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from backend_synthetic_credentials import synthetic_secret
from solver_coordination.canonical import json_schema_digest, sha256_digest
from solver_coordination.compiler import ProblemIRCompiler
from solver_coordination.contracts import SolverResult, VerificationResult
from solver_coordination.errors import (
    KnowledgePackageSolverContractIncompatibleError,
)
from solver_coordination.fact_snapshot import FactSnapshotBuilder
from solver_coordination.input_binding import SolverInputBinder
from solver_coordination.job_orchestrator import DurableJobOrchestrator
from solver_coordination.job_store import SolverJobStore
from solver_coordination.problem_families.registry import ProblemFamilyRegistry
from solver_coordination.result_projection import AuthoritativeResultProjector
from solver_coordination.service import SolverCoordinationService, SolverServiceError
from solver_coordination.verification.service import (
    ContentAddressedSolverArtifactStore,
    IndependentVerificationService,
)

WORKSPACE = "workspace-synthetic"
OWNER = "owner-synthetic"
RUN_ID = "run-synthetic"
TOOL_CALL_ID = "tool-call-synthetic"
CONTRACT_DIGEST = "sha256:" + "a" * 64


def _fixed_clock():
    return datetime(2026, 8, 17, tzinfo=timezone.utc)


def _capability():
    capability = {
        "package_id": "package-synthetic",
        "package_revision": "revision-synthetic-1",
        "operation_key": "solve_synthetic",
        "decision_model_id": "model-synthetic",
        "problem_family": "synthetic_family.v1",
        "contract_digest": CONTRACT_DIGEST,
        "validator_revision": "verifier-synthetic-1",
        "input_schema": {
            "type": "object",
            "properties": {"item_count": {"type": "integer", "minimum": 1}},
            "required": ["item_count"],
            "additionalProperties": False,
        },
        "result_schema": {
            "type": "object",
            "properties": {
                "choice": {
                    "type": "string",
                    "enum": ["OPTION_0", "OPTION_1"],
                }
            },
            "required": ["choice"],
            "additionalProperties": False,
        },
        "decision_model": {
            "id": "model-synthetic",
            "required_facts": ["limit"],
            "decision_space": {"variables": ["choice"], "options": ["OPTION_0", "OPTION_1"]},
            "constraints": [{"kind": "limit", "fact": "limit"}],
            "objectives": [{"name": "score", "direction": "min"}],
            "limits": {"max_candidates": 2},
        },
    }
    capability["result_schema_digest"] = json_schema_digest(capability["result_schema"])
    return capability


def _dynamic_assignment_schema():
    return {
        "type": "object",
        "properties": {
            "candidate_id": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            }
        },
        "required": ["candidate_id"],
        "additionalProperties": {
            "type": "string",
            "enum": ["OPTION_0", "OPTION_1"],
        },
        "minProperties": 2,
        "maxProperties": 65,
    }


class _CapabilityResolver:
    def __init__(self):
        self.capability = _capability()

    def resolve(self, **kwargs):
        assert kwargs["workspace_id"] == WORKSPACE
        assert kwargs["owner_id"] == OWNER
        return self.capability


class _FactResolver:
    def __init__(self):
        self.calls = []

    def resolve(self, **kwargs):
        assert kwargs["required_facts"] == ("limit",)
        assert kwargs["capability"]["decision_model"]["id"] == "model-synthetic"
        self.calls.append(kwargs)
        return {
            "facts": [{"name": "limit", "value": 10, "source_evidence": []}],
            "bindings": {"solver_binding_revision": "binding-synthetic-1"},
            "as_of": "2026-08-17T00:00:00Z",
            "source_evidence": [{"kind": "synthetic_fixture", "ref": "fact-source-synthetic"}],
        }


class _FailIfCalledFactResolver:
    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def resolve(self, **kwargs):
        raise AssertionError("replay must not resolve live facts")


class _BindingResolver:
    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def resolve(self, **kwargs):
        return {
            "binding_revision_id": "binding-synthetic-1",
            "configuration": {
                "endpoint": "https://solver.example.net",
                "connector_type": "ortools",
                "service_api_version": "ortools.solver.v1",
                "service_credential_ref": "secret://solver/synthetic",
                "capability_fingerprint": "sha256:" + "a" * 64,
                "allowed_families": {"binary_ordered_route": ["1.0"]},
                "neutral_limits": {
                    "max_payload_bytes": 1048576,
                    "max_solve_time_ms": 60000,
                    "max_workers": 1,
                },
                "execution_profile_ref": None,
                "enabled": True,
            },
        }


class _FamilyAdapter:
    family_key = "synthetic_family.v1"

    # pylint: disable-next=add-staticmethod-or-classmethod-decorator
    def compile_problem(self, *, capability, normalized_input, fact_snapshot):
        model = capability["decision_model"]
        return {
            "decision_space": model["decision_space"],
            "parameters": {
                "item_count": normalized_input.values["item_count"],
                "limit": next(item["value"] for item in fact_snapshot.facts if item["name"] == "limit"),
            },
            "constraints": model["constraints"],
            "objectives": model["objectives"],
            "limits": model["limits"],
        }


class _Backend:
    def __init__(self, *, status="completed_optimal"):
        self.status = status
        self.problem = None
        self.submit_calls = 0
        self.cancel_calls = 0

    def submit(self, *, problem, idempotency_key, deadline):
        self.problem = problem
        self.submit_calls += 1
        return {"job_id": "backend-job-synthetic", "status": "running"}

    def poll(self, *, job_id):
        return {"job_id": job_id, "status": self.status, "progress": {"percent": 100}}

    def cancel(self, *, job_id):
        self.cancel_calls += 1

    def result(self, *, job_id):
        return SolverResult(
            problem_id=self.problem.problem_id,
            status="optimal",
            assignment={"choice": "OPTION_0"},
            objective_values={"score": "1"},
            diagnostics=(),
            engine={"name": "synthetic-backend", "version": "1"},
        )


class _Verifier:
    def __init__(
        self,
        *,
        status="verified",
        feasibility="feasible",
        optimality="proven_optimal",
        proof_kind="verifier_exhaustive_check",
    ):
        self.calls = 0
        self.problems = []
        self.status = status
        self.feasibility = feasibility
        self.optimality = optimality
        self.proof_kind = proof_kind

    def verify(self, **kwargs):
        self.calls += 1
        self.problems.append(kwargs["problem"])
        result = kwargs["solver_result"]
        return VerificationResult(
            problem_id=result.problem_id,
            status=self.status,
            feasibility=self.feasibility,
            optimality=self.optimality,
            proof_kind=self.proof_kind,
            evidence=({"kind": "synthetic_proof"},),
        )


class _PresenterSpy:
    def __init__(self):
        self.calls = 0

    def present(self, **kwargs):
        self.calls += 1
        return {"synthetic": "summary"}


def _service(
    tmp_path,
    *,
    backend=None,
    compiler=None,
    verifier=None,
    verification_service=None,
    store=None,
    artifact_store=None,
    presenter=None,
    candidate_results_builder=None,
):
    selected_store = store or SolverJobStore(tmp_path / "jobs.sqlite3")
    selected_backend = backend or _Backend()
    selected_verifier = verifier or _Verifier()
    selected_compiler = compiler or ProblemIRCompiler(registry=ProblemFamilyRegistry([_FamilyAdapter()]))
    orchestrator = DurableJobOrchestrator(
        store=selected_store,
        backend=selected_backend,
        idempotency_secret=synthetic_secret("solver_coordination_service-credential-01").encode("utf-8"),
        lease_owner="worker-synthetic",
    )
    service = SolverCoordinationService(
        capability_resolver=_CapabilityResolver(),
        binding_resolver=_BindingResolver(),
        fact_resolver=_FactResolver(),
        input_binder=SolverInputBinder(),
        snapshot_builder=FactSnapshotBuilder(),
        compiler=selected_compiler,
        job_orchestrator=orchestrator,
        verifier=verification_service or IndependentVerificationService(verifier=selected_verifier),
        artifact_store=artifact_store or ContentAddressedSolverArtifactStore(tmp_path / "artifacts"),
        result_projector=AuthoritativeResultProjector(),
        clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
        deadline_seconds=30,
        max_polls=3,
        sleep=lambda _seconds: None,
        presenter=presenter,
        candidate_results_builder=candidate_results_builder,
    )
    return service, selected_store, selected_backend, selected_verifier


def test_service_rejects_an_invalid_compiler_return_contract(tmp_path):
    class _InvalidCompiler:
        def __init__(self):
            self.delegate = ProblemIRCompiler(registry=ProblemFamilyRegistry([_FamilyAdapter()]))

        def __getattr__(self, name):
            return getattr(self.delegate, name)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def compile(self, **_kwargs):
            return {"invalid": "problem"}

    service, _store, _backend, _verifier = _service(
        tmp_path,
        compiler=_InvalidCompiler(),
    )

    with pytest.raises(SolverServiceError) as error:
        _start(service)

    assert error.value.code == "model_compile_failed"
    assert error.value.phase == "compile"


def test_service_preserves_an_incompatible_knowledge_package_contract(tmp_path):
    class _IncompatiblePackageFactResolver:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def resolve(self, **_kwargs):
            raise KnowledgePackageSolverContractIncompatibleError("internal package contract detail")

    service, _store, _backend, _verifier = _service(tmp_path)
    service.fact_resolver = _IncompatiblePackageFactResolver()

    with pytest.raises(SolverServiceError) as error:
        _start(service)

    assert error.value.code == "knowledge_package_solver_contract_incompatible"
    assert error.value.phase == "snapshot"
    assert error.value.retryable is False


def test_service_rejects_an_invalid_verifier_return_contract(tmp_path):
    class _InvalidVerificationService:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def verify(self, **_kwargs):
            return {"invalid": "verification"}

    service, _store, _backend, _verifier = _service(
        tmp_path,
        verification_service=_InvalidVerificationService(),
    )

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "verification_runtime_failed"
    assert outcome["phase"] == "verify"
    assert outcome["authoritative"] is False


def test_service_does_not_present_an_unverified_result(tmp_path):
    presenter = _PresenterSpy()
    service, _store, _backend, _verifier = _service(
        tmp_path,
        verifier=_Verifier(
            status="unproven",
            feasibility="not_found",
            optimality="not_proven",
            proof_kind="none",
        ),
        presenter=presenter,
    )

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "solver_result_rejected"
    assert outcome["authoritative"] is False
    assert presenter.calls == 0


def _start(service, **overrides):
    kwargs = {
        "workspace_id": WORKSPACE,
        "owner_id": OWNER,
        "run_id": RUN_ID,
        "turn_id": 1,
        "tool_call_id": TOOL_CALL_ID,
        "card_name": "solve_solve_synthetic_aaaaaaaa",
        "package_id": "package-synthetic",
        "package_revision": "revision-synthetic-1",
        "operation_key": "solve_synthetic",
        "contract_digest": CONTRACT_DIGEST,
        "schema_digest": sha256_digest(_capability()["input_schema"]),
        "arguments": {"item_count": 2},
        "progress_callback": lambda _type, _payload: None,
    }
    kwargs.update(overrides)
    return service.start_or_resume(**kwargs)


def test_service_returns_awaiting_input_without_creating_a_job(tmp_path):
    service, store, backend, _verifier = _service(tmp_path)

    outcome = _start(service, arguments={})

    assert outcome["status"] == "awaiting_input"
    assert outcome["code"] == "awaiting_input"
    assert outcome["phase"] == "bind"
    assert outcome["missing"] == ["item_count"]
    assert backend.submit_calls == 0
    assert (
        store.list_recoverable_jobs(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            binding_revision_id="binding-synthetic-1",
        )
        == ()
    )


def test_service_publishes_only_persisted_independently_verified_result(tmp_path):
    events = []
    service, store, backend, verifier = _service(tmp_path)

    outcome = _start(service, progress_callback=lambda kind, payload: events.append((kind, payload)))

    assert outcome["status"] == "completed"
    assert outcome["authoritative"] is True
    assert outcome["feasibility"] == "feasible"
    assert outcome["optimality"] == "proven_optimal"
    assert outcome["result_artifact_ref"].startswith("artifact://")
    assert outcome["verification_evidence_ref"].startswith("artifact://")
    assert backend.submit_calls == 1
    assert verifier.calls == 1
    assert verifier.problems == [backend.problem]
    (fact_call,) = service.fact_resolver.calls
    assert set(fact_call) == {
        "workspace_id",
        "owner_id",
        "run_id",
        "package_id",
        "package_revision",
        "operation_key",
        "capability",
        "normalized_input",
        "required_facts",
    }
    assert fact_call["capability"]["problem_family"] == "synthetic_family.v1"
    assert fact_call["normalized_input"].operation_key == "solve_synthetic"
    assert [kind for kind, _payload in events][-2:] == [
        "solver.verification.completed",
        "solver.result.authoritative",
    ]
    assert all({"run_id", "job_id", "attempt_id", "event_seq"}.issubset(payload) for _kind, payload in events)
    persisted_job = store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert persisted_job["run_policy"]["max_attempts"] == 2
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    assert link["tool_result"]["authoritative"] is True
    job = store.get_job(link["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["run_policy"]["result_quality"] == "optimal"


def test_service_persists_candidate_results_before_publishing_authority(tmp_path):
    calls = []

    def build_candidates(**kwargs):
        calls.append(kwargs)
        return {
            "schema_version": "solver.candidate-results.v1",
            "candidate_count": 2,
            "selected_candidate_id": "ALL_0",
        }

    service, store, _backend, _verifier = _service(
        tmp_path,
        candidate_results_builder=build_candidates,
    )

    outcome = _start(service)

    assert len(calls) == 1
    assert outcome["candidate_results_artifact_ref"].startswith("artifact://candidate_results/")
    assert outcome["candidate_results_artifact_digest"].startswith("sha256:")
    artifacts = store.list_artifacts(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    candidate_record = next(artifact for artifact in artifacts if artifact["artifact_type"] == "candidate_results")
    assert candidate_record["artifact_ref"] == outcome["candidate_results_artifact_ref"]
    assert service.artifact_store.get(candidate_record["artifact_ref"]) == {
        "schema_version": "solver.candidate-results.v1",
        "candidate_count": 2,
        "selected_candidate_id": "ALL_0",
    }

    replay = _start(service)
    assert replay == outcome
    assert len(calls) == 1


def test_service_persists_traceparent_in_frozen_run_policy(tmp_path):
    service, store, _backend, _verifier = _service(tmp_path)
    traceparent = "00-" + "a" * 32 + "-" + "b" * 16 + "-01"

    outcome = _start(service, traceparent=traceparent)

    job = store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["run_policy"]["traceparent"] == traceparent


def test_service_rejects_feasible_connector_result_when_run_requires_optimal(tmp_path):
    class _FeasibleBackend(_Backend):
        def result(self, *, job_id):
            del job_id
            return SolverResult(
                problem_id=self.problem.problem_id,
                status="feasible",
                assignment={"choice": "OPTION_0"},
                objective_values={"score": "1"},
                diagnostics=(),
                engine={"name": "synthetic-backend", "version": "1"},
            )

    service, _store, _backend, verifier = _service(tmp_path, backend=_FeasibleBackend(status="completed_feasible"))

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "quality_requirement_not_met"
    assert outcome["authoritative"] is False
    assert verifier.calls == 0


def test_service_rejects_assignment_that_violates_locked_result_schema(tmp_path):
    class _InvalidAssignmentBackend(_Backend):
        def result(self, *, job_id):
            del job_id
            return SolverResult(
                problem_id=self.problem.problem_id,
                status="optimal",
                assignment={"choice": "OUTSIDE_DECLARED_ENUM"},
                objective_values={"score": "1"},
                diagnostics=(),
                engine={"name": "synthetic-backend", "version": "1"},
            )

    backend = _InvalidAssignmentBackend()
    service, _store, _backend, verifier = _service(tmp_path, backend=backend)

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "solver_result_rejected"
    assert outcome["phase"] == "verify"
    assert outcome["authoritative"] is False
    assert verifier.calls == 0


def test_service_accepts_dynamic_assignment_keys_from_locked_result_schema(tmp_path):
    class _DynamicAssignmentBackend(_Backend):
        def result(self, *, job_id):
            del job_id
            return SolverResult(
                problem_id=self.problem.problem_id,
                status="optimal",
                assignment={
                    "candidate_id": "OPTION_0_TO_OPTION_1_K0001",
                    "item_0001": "OPTION_0",
                    "item_0002": "OPTION_1",
                },
                objective_values={"score": "1"},
                diagnostics=(),
                engine={"name": "synthetic-backend", "version": "1"},
            )

    service, _store, _backend, verifier = _service(tmp_path, backend=_DynamicAssignmentBackend())
    dynamic_schema = _dynamic_assignment_schema()
    service.capability_resolver.capability["result_schema"] = dynamic_schema
    service.capability_resolver.capability["result_schema_digest"] = json_schema_digest(dynamic_schema)

    outcome = _start(service)

    assert outcome["status"] == "completed"
    assert outcome["authoritative"] is True
    assert verifier.calls == 1


def test_service_rejects_invalid_dynamic_assignment_option(tmp_path):
    class _InvalidDynamicAssignmentBackend(_Backend):
        def result(self, *, job_id):
            del job_id
            return SolverResult(
                problem_id=self.problem.problem_id,
                status="optimal",
                assignment={
                    "candidate_id": "OPTION_0_TO_OPTION_1_K0001",
                    "item_0001": "UNDECLARED_OPTION",
                },
                objective_values={"score": "1"},
                diagnostics=(),
                engine={"name": "synthetic-backend", "version": "1"},
            )

    service, _store, _backend, verifier = _service(tmp_path, backend=_InvalidDynamicAssignmentBackend())
    dynamic_schema = _dynamic_assignment_schema()
    service.capability_resolver.capability["result_schema"] = dynamic_schema
    service.capability_resolver.capability["result_schema_digest"] = json_schema_digest(dynamic_schema)

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "solver_result_rejected"
    assert outcome["phase"] == "verify"
    assert outcome["authoritative"] is False
    assert verifier.calls == 0


def test_service_allows_empty_infeasible_payload_to_reach_independent_verification(tmp_path):
    class _InfeasibleBackend(_Backend):
        def result(self, *, job_id):
            del job_id
            return SolverResult(
                problem_id=self.problem.problem_id,
                status="infeasible",
                assignment={},
                objective_values={},
                diagnostics=(),
                engine={"name": "synthetic-backend", "version": "1"},
            )

    class _InfeasibleVerifier(_Verifier):
        def verify(self, **kwargs):
            self.calls += 1
            self.problems.append(kwargs["problem"])
            return VerificationResult(
                problem_id=kwargs["problem"].problem_id,
                status="verified",
                feasibility="infeasible",
                optimality="not_applicable",
                proof_kind="verifier_exhaustive_check",
                evidence=({"kind": "synthetic_proof"},),
            )

    service, _store, _backend, verifier = _service(
        tmp_path,
        backend=_InfeasibleBackend(),
        verifier=_InfeasibleVerifier(),
    )

    outcome = _start(service)

    assert outcome["status"] == "completed"
    assert outcome["feasibility"] == "infeasible"
    assert outcome["proof_kind"] == "verifier_exhaustive_check"
    assert verifier.calls == 1


@pytest.mark.parametrize(
    ("status", "assignment", "objective_values"),
    [
        ("infeasible", {"choice": "OPTION_0"}, {}),
        ("infeasible", {}, {"score": "1"}),
        ("completed", {"choice": "OPTION_0"}, {"score": "1"}),
    ],
)
def test_service_rejects_inconsistent_or_unsupported_solver_status_payloads(
    tmp_path,
    status,
    assignment,
    objective_values,
):
    class _StatusBackend(_Backend):
        def result(self, *, job_id):
            del job_id
            return SolverResult(
                problem_id=self.problem.problem_id,
                status=status,
                assignment=assignment,
                objective_values=objective_values,
                diagnostics=(),
                engine={"name": "synthetic-backend", "version": "1"},
            )

    service, _store, _backend, verifier = _service(
        tmp_path,
        backend=_StatusBackend(),
    )

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "solver_result_rejected"
    assert verifier.calls == 0


def test_service_stops_when_result_schema_digest_does_not_match_loaded_schema(tmp_path):
    service, _store, backend, _verifier = _service(tmp_path)
    service.capability_resolver.capability["result_schema_digest"] = sha256_digest({"different": "synthetic-schema"})

    with pytest.raises(SolverServiceError) as exc_info:
        _start(service)

    assert exc_info.value.code == "solver_contract_drift"
    assert exc_info.value.phase == "select"
    assert backend.submit_calls == 0


def test_service_replays_same_tool_result_without_backend_or_verifier_reexecution(tmp_path):
    service, store, backend, verifier = _service(tmp_path)
    first = _start(service)

    reconstructed, _same_store, _backend, _verifier = _service(
        tmp_path,
        backend=backend,
        verifier=verifier,
        store=SolverJobStore(store.database_path),
    )
    reconstructed.fact_resolver = _FailIfCalledFactResolver()
    replay = _start(reconstructed)

    assert replay == first
    assert backend.submit_calls == 1
    assert verifier.calls == 1


def test_service_resumes_running_job_from_frozen_inputs_without_live_facts(tmp_path):
    backend = _Backend(status="running")
    service, _store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1

    first = _start(service)
    assert first["status"] == "running"

    service.fact_resolver = _FailIfCalledFactResolver()
    backend.status = "completed_optimal"
    resumed = _start(service)

    assert resumed["status"] == "completed"
    assert resumed["authoritative"] is True
    assert backend.submit_calls == 1
    assert verifier.calls == 1


def test_service_cancels_current_subscription_from_agent_cancel_probe(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    cancellation_checks = iter((False, True))

    outcome = _start(
        service,
        cancel_requested=lambda: next(cancellation_checks),
    )

    assert outcome == {
        "status": "cancelled",
        "code": "cancelled",
        "phase": "cancel",
        "retryable": False,
        "authoritative": False,
        "job_id": outcome["job_id"],
        "attempt_id": outcome["attempt_id"],
    }
    assert backend.submit_calls == 1
    assert backend.cancel_calls == 1
    assert verifier.calls == 0
    job = store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["status"] == "cancelled"
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    assert link["tool_result"] == outcome


def test_service_persists_frozen_inputs_before_backend_submission(tmp_path):
    service, store, backend, _verifier = _service(tmp_path)

    def _interrupt_frozen_persist(**kwargs):
        raise SystemExit("synthetic process interruption")

    internal(service).persist_frozen_inputs = _interrupt_frozen_persist

    with pytest.raises(SystemExit, match="synthetic process interruption"):
        _start(service)

    assert backend.submit_calls == 0
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    attempt = store.get_attempt(link["attempt_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert attempt["backend_job_id"] is None


def test_recovery_waits_while_foreground_is_freezing_inputs(tmp_path):
    service, store, backend, _verifier = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_persist = internal(service).persist_frozen_inputs

    def _blocked_frozen_persist(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_persist(**kwargs)

    internal(service).persist_frozen_inputs = _blocked_frozen_persist
    with ThreadPoolExecutor(max_workers=1) as executor:
        foreground = executor.submit(_start, service)
        assert entered.wait(5)
        link = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )
        job_id = link["job_id"]
        try:
            recovered = service.recover_job(
                job_id,
                workspace_id=WORKSPACE,
                owner_id=OWNER,
            )
            job = store.get_job(job_id, workspace_id=WORKSPACE, owner_id=OWNER)
            assert recovered["status"] == "created"
            assert recovered["phase"] == "freeze_inputs"
            assert job["status"] == "created"
            assert job["phase"] == "freeze_inputs"
            assert job["lease_owner"] is not None
            assert backend.submit_calls == 0
        finally:
            release.set()
        outcome = foreground.result(timeout=5)

    assert outcome["status"] == "completed"
    assert backend.submit_calls == 1
    completed = store.get_job(job_id, workspace_id=WORKSPACE, owner_id=OWNER)
    assert completed["status"] == "completed"
    assert completed["lease_owner"] is None


def test_shared_subscription_can_cancel_while_inputs_are_freezing(tmp_path):
    service, store, backend, _verifier = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_persist = internal(service).persist_frozen_inputs

    def _blocked_frozen_persist(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_persist(**kwargs)

    internal(service).persist_frozen_inputs = _blocked_frozen_persist
    with ThreadPoolExecutor(max_workers=1) as executor:
        foreground = executor.submit(
            _start,
            service,
            run_id="run-synthetic-freeze-owner",
            tool_call_id="tool-call-synthetic-freeze-owner",
        )
        assert entered.wait(5)
        cancelled = _start(
            service,
            run_id="run-synthetic-freeze-owner",
            tool_call_id="tool-call-synthetic-freeze-sibling",
            cancel_requested=lambda: True,
        )
        assert cancelled["status"] == "cancelled"
        sibling = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id="run-synthetic-freeze-owner",
            turn_id="1",
            tool_call_id="tool-call-synthetic-freeze-sibling",
        )
        assert sibling["tool_result"] == cancelled
        assert not bool(sibling["subscription_active"])
        assert backend.cancel_calls == 0
        release.set()
        foreground_outcome = foreground.result(timeout=5)

    assert foreground_outcome["status"] == "completed"
    assert backend.submit_calls == 1


def test_last_freeze_subscription_cancels_without_submitting_backend(tmp_path):
    service, store, backend, _verifier = _service(tmp_path)
    entered = threading.Event()
    release = threading.Event()
    original_persist = internal(service).persist_frozen_inputs

    def _blocked_frozen_persist(**kwargs):
        entered.set()
        assert release.wait(5)
        return original_persist(**kwargs)

    internal(service).persist_frozen_inputs = _blocked_frozen_persist
    with ThreadPoolExecutor(max_workers=1) as executor:
        foreground = executor.submit(_start, service)
        assert entered.wait(5)
        link = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )
        assert link is not None
        cancellation = service.job_orchestrator.cancel_subscription(
            link["subscription_id"], workspace_id=WORKSPACE, owner_id=OWNER
        )
        assert cancellation["job_status"] == "cancelled"
        assert backend.submit_calls == 0
        assert backend.cancel_calls == 0
        release.set()
        outcome = foreground.result(timeout=5)

    assert outcome["status"] == "cancelled"
    assert backend.submit_calls == 0
    assert backend.cancel_calls == 0


def test_recovery_does_not_steal_a_foreground_poll_fence(tmp_path):
    service, store, backend, _verifier = _service(tmp_path)
    service.max_polls = 1
    backend.status = "running"
    entered = threading.Event()
    release = threading.Event()
    original_poll = backend.poll

    def _blocked_poll(*, job_id):
        entered.set()
        assert release.wait(5)
        return original_poll(job_id=job_id)

    backend.poll = _blocked_poll
    with ThreadPoolExecutor(max_workers=1) as executor:
        foreground = executor.submit(_start, service)
        assert entered.wait(5)
        link = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )
        recovered = service.recover_job(
            link["job_id"],
            workspace_id=WORKSPACE,
            owner_id=OWNER,
        )
        assert recovered["status"] == "waiting_solver"
        release.set()
        foreground_outcome = foreground.result(timeout=5)

    assert foreground_outcome["status"] == "running"
    assert backend.submit_calls == 1


def test_service_cancels_shared_job_only_after_last_subscription_leaves(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    internal(service.job_orchestrator).clock = _fixed_clock

    first = _start(service, tool_call_id="tool-call-synthetic-first")
    assert first["status"] == "running"

    second = _start(
        service,
        tool_call_id="tool-call-synthetic-second",
        cancel_requested=lambda: True,
    )

    assert second["status"] == "cancelled"
    assert second["job_id"] == first["job_id"]
    assert backend.cancel_calls == 0
    assert verifier.calls == 0
    shared_job = store.get_job(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert shared_job["status"] == "waiting_solver"
    second_link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-second",
    )
    assert second_link["tool_result"] == second

    last = _start(
        service,
        tool_call_id="tool-call-synthetic-first",
        cancel_requested=lambda: True,
    )

    assert last["status"] == "cancelled"
    assert last["job_id"] == first["job_id"]
    assert backend.cancel_calls == 1
    terminal_job = store.get_job(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert terminal_job["status"] == "cancelled"


def test_service_recovers_subscriber_cancel_after_tool_result_write_interruption(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    internal(service.job_orchestrator).clock = _fixed_clock
    first = _start(service, tool_call_id="tool-call-synthetic-active")
    assert first["status"] == "running"

    write_tool_result = internal(service).write_tool_result

    def _interrupt_tool_result(**kwargs):
        raise RuntimeError("synthetic process interruption")

    internal(service).write_tool_result = _interrupt_tool_result
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        _start(
            service,
            tool_call_id="tool-call-synthetic-cancelled",
            cancel_requested=lambda: True,
        )

    assert backend.cancel_calls == 0
    shared_job = store.get_job(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert shared_job["status"] == "waiting_solver"

    internal(service).write_tool_result = write_tool_result
    service.fact_resolver = _FailIfCalledFactResolver()
    recovered = _start(
        service,
        tool_call_id="tool-call-synthetic-cancelled",
        cancel_requested=lambda: False,
    )

    assert recovered["status"] == "cancelled"
    assert recovered["job_id"] == first["job_id"]
    assert backend.cancel_calls == 0
    assert verifier.calls == 0


def test_service_job_recovery_preserves_cancelled_subscriber_after_crash(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    internal(service.job_orchestrator).clock = _fixed_clock
    active = _start(service, tool_call_id="tool-call-synthetic-active-recovery")
    assert active["status"] == "running"

    write_tool_result = internal(service).write_tool_result

    def _interrupt_tool_result(**kwargs):
        raise RuntimeError("synthetic process interruption")

    internal(service).write_tool_result = _interrupt_tool_result
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        _start(
            service,
            tool_call_id="tool-call-synthetic-cancelled-recovery",
            cancel_requested=lambda: True,
        )

    internal(service).write_tool_result = write_tool_result
    service.fact_resolver = _FailIfCalledFactResolver()
    backend.status = "completed_optimal"
    recovered = service.recover_job(
        active["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered["status"] == "completed"
    assert recovered["authoritative"] is True
    assert verifier.calls == 1
    active_link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-active-recovery",
    )
    cancelled_link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-cancelled-recovery",
    )
    assert active_link["tool_result"]["authoritative"] is True
    assert cancelled_link["subscription_active"] == 0
    assert cancelled_link["tool_result"]["status"] == "cancelled"
    assert cancelled_link["tool_result"]["authoritative"] is False


def test_service_isolates_jobs_across_runs_and_projects_run_local_results(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1

    first = _start(
        service,
        run_id="run-synthetic-a",
        tool_call_id="tool-call-synthetic-a",
    )
    second = _start(
        service,
        run_id="run-synthetic-b",
        tool_call_id="tool-call-synthetic-b",
    )

    assert first["status"] == "running"
    assert second["status"] == "running"
    assert first["job_id"] != second["job_id"]
    assert backend.submit_calls == 2
    frozen = [
        artifact
        for artifact in store.list_artifacts(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
        if artifact["artifact_type"] in {"normalized_input", "fact_snapshot", "problem_ir"}
    ]
    assert [artifact["artifact_type"] for artifact in frozen] == [
        "normalized_input",
        "fact_snapshot",
        "problem_ir",
    ]

    service.fact_resolver = _FailIfCalledFactResolver()
    first_replay = _start(
        service,
        run_id="run-synthetic-a",
        tool_call_id="tool-call-synthetic-a",
        arguments={},
    )
    assert first_replay["status"] == "running"

    backend.status = "completed_optimal"
    completed_second = _start(
        service,
        run_id="run-synthetic-b",
        tool_call_id="tool-call-synthetic-b",
        arguments={},
    )
    completed_first = _start(
        service,
        run_id="run-synthetic-a",
        tool_call_id="tool-call-synthetic-a",
        arguments={},
    )

    assert completed_second["status"] == "completed"
    assert completed_second["run_id"] == "run-synthetic-b"
    assert completed_first["status"] == "completed"
    assert completed_first["run_id"] == "run-synthetic-a"
    assert completed_first["job_id"] != completed_second["job_id"]
    assert backend.submit_calls == 2
    assert verifier.calls == 2


def test_service_never_publishes_authoritative_result_when_cancel_wins_terminal_race(
    tmp_path,
):
    service, store, backend, verifier = _service(tmp_path)
    original_complete = store.complete_job_with_authoritative_events
    events = []

    def _cancel_before_authoritative_commit(*args, **kwargs):
        link = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )
        service.job_orchestrator.cancel_subscription(
            link["subscription_id"],
            workspace_id=WORKSPACE,
            owner_id=OWNER,
        )
        return original_complete(*args, **kwargs)

    store.complete_job_with_authoritative_events = _cancel_before_authoritative_commit
    outcome = _start(
        service,
        progress_callback=lambda kind, payload: events.append((kind, payload)),
    )

    assert outcome["status"] == "cancelled"
    assert outcome["authoritative"] is False
    assert backend.cancel_calls == 0
    assert verifier.calls == 1
    assert all(kind != "solver.result.authoritative" for kind, _payload in events)
    job = store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["status"] == "cancelled"


def test_service_returns_cancelled_when_cancel_wins_artifact_persist_race(tmp_path):
    entered = threading.Event()
    release = threading.Event()
    delegate = ContentAddressedSolverArtifactStore(tmp_path / "artifacts")

    class _BlockingArtifactStore:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def put(self, *, artifact_type, payload):
            if artifact_type == "solver_result":
                entered.set()
                assert release.wait(5)
            return delegate.put(artifact_type=artifact_type, payload=payload)

        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def get(self, artifact_ref):
            return delegate.get(artifact_ref)

    service, store, backend, verifier = _service(tmp_path, artifact_store=_BlockingArtifactStore())
    events = []
    with ThreadPoolExecutor(max_workers=1) as executor:
        foreground = executor.submit(
            _start,
            service,
            progress_callback=lambda kind, payload: events.append((kind, payload)),
        )
        assert entered.wait(5)
        link = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )
        cancellation = service.job_orchestrator.cancel_subscription(
            link["subscription_id"], workspace_id=WORKSPACE, owner_id=OWNER
        )
        assert cancellation["job_status"] == "cancelled"
        release.set()
        outcome = foreground.result(timeout=5)

    assert outcome["status"] == "cancelled"
    assert outcome["authoritative"] is False
    assert backend.cancel_calls == 0
    assert verifier.calls == 0
    assert all(kind != "solver.verification.rejected" for kind, _payload in events)
    event_types = [
        event["event_type"] for event in store.read_events(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    ]
    assert "solver.job.cancelled" in event_types
    assert "solver.verification.rejected" not in event_types


def test_service_returns_cancelled_when_subscription_cancels_after_job_completion(
    tmp_path,
):
    service, store, backend, verifier = _service(tmp_path)
    original_complete = store.complete_job_with_authoritative_events

    def _complete_then_cancel(*args, **kwargs):
        completed = original_complete(*args, **kwargs)
        link = store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )
        service.job_orchestrator.cancel_subscription(
            link["subscription_id"],
            workspace_id=WORKSPACE,
            owner_id=OWNER,
        )
        return completed

    store.complete_job_with_authoritative_events = _complete_then_cancel
    outcome = _start(service)

    assert outcome["status"] == "cancelled"
    assert outcome["authoritative"] is False
    assert backend.cancel_calls == 0
    assert verifier.calls == 1
    job = store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["status"] == "completed"
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    assert not bool(link["subscription_active"])
    assert link["tool_result"] == outcome


def test_service_verifies_distinct_run_jobs_independently(tmp_path):
    class _BlockingVerifier(_Verifier):
        def __init__(self):
            super().__init__()
            self._lock = threading.Lock()
            self.first_entered = threading.Event()
            self.second_entered = threading.Event()
            self.release = threading.Event()

        def verify(self, **kwargs):
            with self._lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                self.first_entered.set()
            else:
                self.second_entered.set()
            assert self.release.wait(5)
            result = kwargs["solver_result"]
            return VerificationResult(
                problem_id=result.problem_id,
                status="verified",
                feasibility="feasible",
                optimality="proven_optimal",
                proof_kind="verifier_exhaustive_check",
                evidence=({"kind": "synthetic_proof"},),
            )

    backend = _Backend(status="running")
    verifier = _BlockingVerifier()
    seed, store, _backend, _verifier = _service(tmp_path, backend=backend, verifier=verifier)
    seed.max_polls = 1
    first = _start(
        seed,
        run_id="run-synthetic-verify-a",
        tool_call_id="tool-call-synthetic-verify-a",
    )
    second = _start(
        seed,
        run_id="run-synthetic-verify-b",
        tool_call_id="tool-call-synthetic-verify-b",
    )
    assert first["job_id"] != second["job_id"]
    backend.status = "completed_optimal"

    first_service, _store_a, _backend_a, _verifier_a = _service(
        tmp_path,
        backend=backend,
        verifier=verifier,
        store=SolverJobStore(store.database_path),
        artifact_store=seed.artifact_store,
    )
    second_service, _store_b, _backend_b, _verifier_b = _service(
        tmp_path,
        backend=backend,
        verifier=verifier,
        store=SolverJobStore(store.database_path),
        artifact_store=seed.artifact_store,
    )

    # Distinct runs intentionally have independent verification lifecycles;
    # keep this regression deterministic instead of asserting the old shared
    # job's cross-run verifier serialization.
    verifier.release.set()
    first_result = _start(
        first_service,
        run_id="run-synthetic-verify-a",
        tool_call_id="tool-call-synthetic-verify-a",
        arguments={},
    )
    second_result = _start(
        second_service,
        run_id="run-synthetic-verify-b",
        tool_call_id="tool-call-synthetic-verify-b",
        arguments={},
    )

    assert verifier.calls == 2
    assert first_result["authoritative"] is True
    assert second_result["authoritative"] is True
    for job_id in (first["job_id"], second["job_id"]):
        events = store.read_events(job_id, workspace_id=WORKSPACE, owner_id=OWNER)
        assert [event["event_type"] for event in events].count("solver.verification.completed") == 1
        assert [event["event_type"] for event in events].count("solver.result.authoritative") == 1


def test_service_recovers_completed_job_when_tool_result_write_was_interrupted(tmp_path):
    service, store, backend, verifier = _service(tmp_path)
    write_tool_result = internal(service).write_tool_result
    events = []

    def _interrupt_tool_result(**kwargs):
        raise RuntimeError("synthetic process interruption")

    internal(service).write_tool_result = _interrupt_tool_result
    with pytest.raises(RuntimeError, match="synthetic process interruption"):
        _start(service, progress_callback=lambda kind, payload: events.append((kind, payload)))

    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    job = store.get_job(link["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["status"] == "completed"
    assert link["tool_result"] is None

    internal(service).write_tool_result = write_tool_result
    service.fact_resolver = _FailIfCalledFactResolver()
    recovered = _start(service, progress_callback=lambda kind, payload: events.append((kind, payload)))

    assert recovered["status"] == "completed"
    assert recovered["authoritative"] is True
    assert backend.submit_calls == 1
    assert verifier.calls == 1
    assert [kind for kind, _payload in events].count("solver.verification.completed") == 1
    assert [kind for kind, _payload in events].count("solver.result.authoritative") == 1


def test_service_fails_closed_when_replay_frozen_artifact_is_missing(tmp_path):
    service, store, backend, verifier = _service(tmp_path)
    service.max_polls = 1
    backend.status = "running"
    first = _start(service)
    assert first["status"] == "running"
    artifacts = store.list_artifacts(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    normalized_record = next(item for item in artifacts if item["artifact_type"] == "normalized_input")
    artifact_path = (
        service.artifact_store.root
        / "normalized_input"
        / "{}.json".format(normalized_record["artifact_digest"].rsplit(":", 1)[-1])
    )
    artifact_path.unlink()
    service.fact_resolver = _FailIfCalledFactResolver()

    with pytest.raises(SolverServiceError) as exc_info:
        _start(service)

    assert exc_info.value.code == "artifact_persist_failed"
    assert exc_info.value.phase == "resume"
    assert backend.submit_calls == 1
    assert verifier.calls == 0


def test_service_stops_resume_when_tool_contract_drifted(tmp_path):
    service, _store, backend, verifier = _service(tmp_path)
    _start(service)

    with pytest.raises(SolverServiceError) as exc_info:
        _start(service, contract_digest="sha256:" + "b" * 64)

    assert exc_info.value.code == "solver_contract_drift"
    assert exc_info.value.phase == "resume"
    assert backend.submit_calls == 1
    assert verifier.calls == 1


def test_service_replays_same_tool_call_without_rebinding_changed_arguments(tmp_path):
    service, _store, backend, verifier = _service(tmp_path)
    first = _start(service)
    service.fact_resolver = _FailIfCalledFactResolver()

    replay = _start(service, arguments={"item_count": 3})

    assert replay == first
    assert backend.submit_calls == 1
    assert verifier.calls == 1


def test_service_never_publishes_when_evidence_persistence_fails(tmp_path):
    class _FailingArtifactStore:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def put(self, *args, **kwargs):
            raise OSError("synthetic disk failure")

    service, store, _backend, _verifier = _service(tmp_path, artifact_store=_FailingArtifactStore())

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "artifact_persist_failed"
    assert outcome["retryable"] is False
    assert outcome["authoritative"] is False
    job = store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["status"] == "failed"


def test_service_maps_backend_timeout_without_fabricating_no_solution(tmp_path):
    backend = _Backend(status="timed_out")
    service, _store, _backend, verifier = _service(tmp_path, backend=backend)

    outcome = _start(service)

    assert outcome["status"] == "timed_out"
    assert outcome["code"] == "timed_out_no_solution"
    assert outcome["authoritative"] is False
    assert verifier.calls == 0


def test_service_maps_verifier_divergence_to_non_retryable_result_rejection(tmp_path):
    class _DivergingVerifier:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def verify(self, **kwargs):
            return VerificationResult(
                problem_id="sha256:" + "f" * 64,
                status="verified",
                feasibility="feasible",
                optimality="not_proven",
                proof_kind="synthetic_divergence",
            )

    service, store, _backend, _verifier = _service(tmp_path, verifier=_DivergingVerifier())

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "solver_result_rejected"
    assert outcome["phase"] == "verify"
    assert outcome["retryable"] is False
    assert outcome["authoritative"] is False
    assert {
        artifact["artifact_type"]
        for artifact in store.list_artifacts(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    } >= {"solver_result"}


def test_service_marks_verifier_runtime_failure_retryable_without_authority(tmp_path):
    class _FailingVerifier:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def verify(self, **kwargs):
            raise RuntimeError("synthetic verifier outage")

    service, store, _backend, _verifier = _service(tmp_path, verifier=_FailingVerifier())

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "verification_runtime_failed"
    assert outcome["retryable"] is True
    assert outcome["authoritative"] is False
    assert (
        store.get_run_link(
            workspace_id=WORKSPACE,
            owner_id=OWNER,
            run_id=RUN_ID,
            turn_id="1",
            tool_call_id=TOOL_CALL_ID,
        )["tool_result"]
        == outcome
    )
    assert store.get_job(outcome["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)["status"] == "solver_completed"


def test_service_retries_verification_on_new_tool_call_without_resolving_facts(tmp_path):
    class _FlakyVerifier(_Verifier):
        def verify(self, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("synthetic verifier outage")
            result = kwargs["solver_result"]
            return VerificationResult(
                problem_id=result.problem_id,
                status="verified",
                feasibility="feasible",
                optimality="proven_optimal",
                proof_kind="verifier_exhaustive_check",
                evidence=({"kind": "synthetic_proof"},),
            )

    verifier = _FlakyVerifier()
    service, store, backend, _selected_verifier = _service(tmp_path, verifier=verifier)

    first = _start(service, tool_call_id="tool-call-synthetic-verify-first")
    assert first["code"] == "verification_runtime_failed"
    assert first["retryable"] is True
    assert store.get_job(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)["status"] == "solver_completed"

    service.fact_resolver = _FailIfCalledFactResolver()
    retried = _start(
        service,
        tool_call_id="tool-call-synthetic-verify-retry",
    )

    assert retried["status"] == "completed"
    assert retried["authoritative"] is True
    assert retried["job_id"] == first["job_id"]
    assert backend.submit_calls == 1
    assert verifier.calls == 2
    replay = _start(
        service,
        tool_call_id="tool-call-synthetic-verify-first",
        arguments={},
    )
    assert replay == first


def test_service_retries_evidence_persistence_on_new_tool_call(tmp_path):
    class _FlakyEvidenceArtifactStore:
        def __init__(self, root):
            self.delegate = ContentAddressedSolverArtifactStore(root)
            self.failed_once = False

        def put(self, *, artifact_type, payload):
            if artifact_type == "verification_evidence" and not self.failed_once:
                self.failed_once = True
                raise OSError("synthetic evidence storage outage")
            return self.delegate.put(artifact_type=artifact_type, payload=payload)

        def get(self, artifact_ref):
            return self.delegate.get(artifact_ref)

    artifact_store = _FlakyEvidenceArtifactStore(tmp_path / "artifacts")
    service, store, backend, verifier = _service(
        tmp_path,
        artifact_store=artifact_store,
    )

    first = _start(service, tool_call_id="tool-call-synthetic-evidence-first")

    assert first["status"] == "error"
    assert first["code"] == "artifact_persist_failed"
    assert first["phase"] == "publish"
    assert first["retryable"] is True
    assert store.get_job(first["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)["status"] == "solver_completed"

    service.fact_resolver = _FailIfCalledFactResolver()
    retried = _start(
        service,
        tool_call_id="tool-call-synthetic-evidence-retry",
    )

    assert retried["status"] == "completed"
    assert retried["authoritative"] is True
    assert retried["job_id"] == first["job_id"]
    assert backend.submit_calls == 1
    assert verifier.calls == 2


def test_service_recovery_finishes_isolated_jobs_and_tool_inboxes(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1

    first = _start(
        service,
        run_id="run-synthetic-recovery-a",
        tool_call_id="tool-call-synthetic-recovery-a",
    )
    second = _start(
        service,
        run_id="run-synthetic-recovery-b",
        tool_call_id="tool-call-synthetic-recovery-b",
    )

    assert first["status"] == "running"
    assert second["status"] == "running"
    assert first["job_id"] != second["job_id"]
    assert backend.submit_calls == 2

    backend.status = "completed_optimal"
    recovered_service, reopened_store, _backend, _verifier = _service(
        tmp_path,
        backend=backend,
        verifier=verifier,
        store=SolverJobStore(store.database_path),
    )
    recovered_service.fact_resolver = _FailIfCalledFactResolver()

    recovered = recovered_service.recover_job(
        first["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )
    recovered_second = recovered_service.recover_job(
        second["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered["status"] == "completed"
    assert recovered["authoritative"] is True
    assert recovered_second["status"] == "completed"
    assert recovered_second["authoritative"] is True
    assert backend.submit_calls == 2
    assert verifier.calls == 2
    first_link = reopened_store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id="run-synthetic-recovery-a",
        turn_id="1",
        tool_call_id="tool-call-synthetic-recovery-a",
    )
    second_link = reopened_store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id="run-synthetic-recovery-b",
        turn_id="1",
        tool_call_id="tool-call-synthetic-recovery-b",
    )
    assert first_link["tool_result"]["run_id"] == "run-synthetic-recovery-a"
    assert second_link["tool_result"]["run_id"] == "run-synthetic-recovery-b"
    assert first_link["tool_result"]["job_id"] != second_link["tool_result"]["job_id"]


def test_service_recovery_persists_binding_contract_drift_tool_result(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    service.job_orchestrator.binding_revision_id = "binding-synthetic-1"
    started = _start(service, tool_call_id="tool-call-synthetic-binding-drift")
    assert started["status"] == "running"

    service.job_orchestrator.binding_revision_id = "binding-synthetic-2"
    service.fact_resolver = _FailIfCalledFactResolver()
    recovered = service.recover_job(
        started["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered == {
        "status": "error",
        "code": "solver_contract_drift",
        "phase": "resume",
        "retryable": False,
        "authoritative": False,
        "job_id": started["job_id"],
        "attempt_id": started["attempt_id"],
    }
    job = store.get_job(started["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-binding-drift",
    )
    assert job["status"] == "failed"
    assert job["error_code"] == "solver_contract_drift"
    assert link["tool_result"] == recovered
    assert backend.cancel_calls == 1
    assert verifier.calls == 0


def test_service_recovery_persists_toolcard_contract_drift_tool_result(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    started = _start(service, tool_call_id="tool-call-synthetic-card-drift")
    assert started["status"] == "running"

    service.capability_resolver.capability["contract_digest"] = "sha256:" + "b" * 64
    service.fact_resolver = _FailIfCalledFactResolver()
    backend.status = "completed_optimal"
    recovered = service.recover_job(
        started["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered["status"] == "error"
    assert recovered["code"] == "solver_contract_drift"
    assert recovered["retryable"] is False
    job = store.get_job(started["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-card-drift",
    )
    assert job["status"] == "failed"
    assert link["tool_result"] == recovered
    assert backend.cancel_calls == 0
    assert verifier.calls == 0


def test_service_recovery_converts_missing_capability_to_contract_drift(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    started = _start(service, tool_call_id="tool-call-synthetic-capability-missing")
    assert started["status"] == "running"
    backend.status = "completed_optimal"

    class _MissingCapabilityResolver:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def resolve(self, **_kwargs):
            raise KeyError("synthetic capability removed")

    service.capability_resolver = _MissingCapabilityResolver()
    recovered = service.recover_job(
        started["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered["code"] == "solver_contract_drift"
    assert recovered["retryable"] is False
    job = store.get_job(started["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-capability-missing",
    )
    assert job["status"] == "failed"
    assert link["tool_result"] == recovered
    assert verifier.calls == 0


def test_service_recovery_terminalizes_missing_frozen_artifact(tmp_path):
    backend = _Backend(status="running")
    service, store, _backend, verifier = _service(tmp_path, backend=backend)
    service.max_polls = 1
    started = _start(service, tool_call_id="tool-call-synthetic-artifact-missing")
    assert started["status"] == "running"

    def missing_artifact(**kwargs):
        raise SolverServiceError(
            "artifact_persist_failed",
            "resume",
            "synthetic frozen artifact is missing",
        )

    internal(service).load_frozen_artifact = missing_artifact
    recovered = service.recover_job(
        started["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered["code"] == "artifact_persist_failed"
    assert recovered["retryable"] is False
    job = store.get_job(started["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id="tool-call-synthetic-artifact-missing",
    )
    assert job["status"] == "failed"
    assert link["tool_result"] == recovered
    assert verifier.calls == 0


def test_recovery_replays_completed_authoritative_event_without_rereading_artifacts(
    tmp_path,
    monkeypatch,
):
    service, store, backend, verifier = _service(tmp_path)
    original_write = store.write_tool_result

    def interrupt_authoritative_delivery(**kwargs):
        if kwargs["result"].get("authoritative") is True:
            raise SystemExit("synthetic interruption after authoritative commit")
        return original_write(**kwargs)

    store.write_tool_result = interrupt_authoritative_delivery
    with pytest.raises(SystemExit, match="after authoritative commit"):
        _start(service)
    link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    job = store.get_job(link["job_id"], workspace_id=WORKSPACE, owner_id=OWNER)
    assert job["status"] == "completed"
    assert link["tool_result"] is None

    store.write_tool_result = original_write

    def unavailable_artifact(_store, _artifact_ref):
        raise OSError("synthetic transient artifact outage")

    monkeypatch.setattr(type(service.artifact_store), "get", unavailable_artifact)
    recovered = service.recover_job(
        job["job_id"],
        workspace_id=WORKSPACE,
        owner_id=OWNER,
    )

    assert recovered["status"] == "completed"
    assert recovered["authoritative"] is True
    assert backend.submit_calls == 1
    assert verifier.calls == 1
    completed_link = store.get_run_link(
        workspace_id=WORKSPACE,
        owner_id=OWNER,
        run_id=RUN_ID,
        turn_id="1",
        tool_call_id=TOOL_CALL_ID,
    )
    assert completed_link["tool_result"] == recovered


def test_same_tool_replays_completed_event_before_live_capability_resolution(tmp_path):
    service, store, backend, verifier = _service(tmp_path)
    original_write = store.write_tool_result

    def interrupt_authoritative_delivery(**kwargs):
        if kwargs["result"].get("authoritative") is True:
            raise SystemExit("synthetic interruption after authoritative commit")
        return original_write(**kwargs)

    store.write_tool_result = interrupt_authoritative_delivery
    with pytest.raises(SystemExit, match="after authoritative commit"):
        _start(service)
    store.write_tool_result = original_write

    class _MissingCapabilityResolver:
        # pylint: disable-next=add-staticmethod-or-classmethod-decorator
        def resolve(self, **_kwargs):
            raise KeyError("synthetic capability removed")

    service.capability_resolver = _MissingCapabilityResolver()
    replayed = _start(service)

    assert replayed["status"] == "completed"
    assert replayed["authoritative"] is True
    assert backend.submit_calls == 1
    assert verifier.calls == 1


def test_new_run_gets_a_distinct_completed_job(tmp_path):
    service, _store, backend, verifier = _service(tmp_path)
    first = _start(
        service,
        run_id="run-synthetic-terminal-a",
        tool_call_id="tool-call-synthetic-terminal-a",
    )
    second = _start(
        service,
        run_id="run-synthetic-terminal-b",
        tool_call_id="tool-call-synthetic-terminal-b",
    )

    assert first["status"] == "completed"
    assert second["status"] == "completed"
    assert second["run_id"] == "run-synthetic-terminal-b"
    assert second["job_id"] != first["job_id"]
    assert backend.submit_calls == 2
    assert verifier.calls == 2


def test_service_does_not_present_a_result_with_insufficient_proof(tmp_path):
    presenter = _PresenterSpy()
    service, _store, _backend, _verifier = _service(
        tmp_path,
        verifier=_Verifier(proof_kind="constraint_recalculation"),
        presenter=presenter,
    )

    outcome = _start(service)

    assert outcome["status"] == "error"
    assert outcome["code"] == "solver_result_rejected"
    assert presenter.calls == 0


def test_service_rejects_verifier_feasibility_divergence_before_presentation(tmp_path):
    presenter = _PresenterSpy()
    service, _store, _backend, _verifier = _service(
        tmp_path,
        verifier=_Verifier(
            feasibility="infeasible",
            optimality="not_applicable",
            proof_kind="verifier_exhaustive_check",
        ),
        presenter=presenter,
    )

    outcome = _start(service)

    assert outcome["status"] == "error"
