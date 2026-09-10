from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from typing import Any, Mapping

import pytest
from solver_coordination.canonical import sha256_digest
from solver_coordination.contracts import (
    FactSnapshot,
    NormalizedInput,
    ProblemIR,
    SolverResult,
)
from solver_coordination.errors import (
    VerificationDivergenceError,
    VerificationRuntimeFailure,
)
from solver_coordination.profiles import (
    CandidateEvaluation,
    OrderedBinaryRouteCompiler,
    OrderedBinaryRouteEvaluator,
)
from solver_coordination.verification.ordered_binary_route import (
    OrderedBinaryRouteIndependentVerifier,
)
from solver_coordination.verification.profile_dispatch import ProfileDispatchVerifier

OBJECTIVES = [
    {"metric": "primary_score", "sense": "maximize"},
    {"metric": "transition_count", "sense": "minimize"},
    {"metric": "safety_margin", "sense": "maximize"},
    {"metric": "candidate_rank", "sense": "minimize"},
]

ROUTE_FACT = {
    "metric_scale": 100,
    "rounding": "ROUND_HALF_EVEN",
    "capacity_limit": "10.00",
    "items": {
        "ITEM_SYNTHETIC_0001": {
            "OPTION_0": {
                "primary_score": "3.00",
                "load": "1.00",
                "safety": "4.00",
            },
            "OPTION_1": {
                "primary_score": "1.00",
                "load": "1.20",
                "safety": "3.00",
            },
        },
        "ITEM_SYNTHETIC_0002": {
            "OPTION_0": {
                "primary_score": "3.00",
                "load": "1.00",
                "safety": "4.00",
            },
            "OPTION_1": {
                "primary_score": "1.00",
                "load": "1.20",
                "safety": "3.00",
            },
        },
        "ITEM_SYNTHETIC_0003": {
            "OPTION_0": {
                "primary_score": "1.00",
                "load": "1.20",
                "safety": "3.00",
            },
            "OPTION_1": {
                "primary_score": "4.00",
                "load": "1.00",
                "safety": "4.00",
            },
        },
        "ITEM_SYNTHETIC_0004": {
            "OPTION_0": {
                "primary_score": "1.00",
                "load": "1.20",
                "safety": "3.00",
            },
            "OPTION_1": {
                "primary_score": "4.00",
                "load": "1.00",
                "safety": "4.00",
            },
        },
    },
}


def _candidate_route_fact() -> dict[str, Any]:
    candidate_ids = ["ALL_0", "ALL_1"]
    candidate_ids.extend("OPTION_0_TO_OPTION_1_K{:04d}".format(index) for index in range(1, 4))
    candidate_ids.extend("OPTION_1_TO_OPTION_0_K{:04d}".format(index) for index in range(1, 4))
    candidates = {
        candidate_id: {
            "eligible": True,
            "primary_score": "1.00",
            "safety_margin": "2.00",
        }
        for candidate_id in candidate_ids
    }
    candidates["OPTION_1_TO_OPTION_0_K0002"]["primary_score"] = "100.00"
    return {
        "metric_scale": 100,
        "rounding": "ROUND_HALF_EVEN",
        "candidates": candidates,
    }


def _normalized_input() -> NormalizedInput:
    return NormalizedInput(
        operation_key="solve_synthetic_route",
        values={
            "items": [
                {
                    "item_order": item_order,
                    "item_id": "ITEM_SYNTHETIC_{:04d}".format(item_order),
                }
                for item_order in range(1, 5)
            ]
        },
    )


def _snapshot(
    normalized_input: NormalizedInput,
    *,
    fact_value: Mapping[str, Any] | None = None,
    as_of: str = "2026-08-18T00:00:00+00:00",
) -> FactSnapshot:
    return FactSnapshot(
        workspace_id="workspace-synthetic",
        owner_id="owner-synthetic",
        package_id="package-synthetic",
        package_revision="revision-synthetic",
        normalized_input_digest=normalized_input.digest,
        facts=(
            {
                "name": "route_metrics",
                "value": deepcopy(fact_value or ROUTE_FACT),
            },
        ),
        bindings={"solver_binding_revision": "binding-synthetic"},
        as_of=as_of,
        checks={
            "required": ("route_metrics",),
            "missing": (),
            "duplicates": (),
            "conflicts": (),
        },
    )


def _model() -> dict[str, Any]:
    return {
        "profile_id": "ordered_binary_route.v1",
        "required_facts": ["route_metrics"],
        "objectives": deepcopy(OBJECTIVES),
    }


def _capability() -> dict[str, Any]:
    model = _model()
    return {
        "problem_family": "candidate_selection.v1",
        "decision_model": model,
        "decision_model_digest": sha256_digest(model),
    }


def _problem_from_compiled(
    compiled: Mapping[str, Any],
    *,
    normalized_input: NormalizedInput,
    fact_snapshot: FactSnapshot,
) -> ProblemIR:
    model = _model()
    components = {
        "decision_space": deepcopy(compiled["decision_space"]),
        "parameters": deepcopy(compiled["parameters"]),
        "constraints": deepcopy(compiled["constraints"]),
        "objectives": deepcopy(compiled["objectives"]),
        "limits": deepcopy(compiled["limits"]),
    }
    problem_id = sha256_digest(
        {
            "schema_version": "solver.problem.v1",
            "compiler_version": "solver.compiler.v1",
            "problem_family": "candidate_selection.v1",
            "model": {
                "decision_model_digest": sha256_digest(model),
                "document": model,
            },
            "normalized_input_digest": normalized_input.digest,
            "fact_snapshot_digest": fact_snapshot.digest,
            "compiled_problem": components,
        }
    )
    return ProblemIR(
        problem_id=problem_id,
        problem_family="candidate_selection.v1",
        decision_space=components["decision_space"],
        parameters=components["parameters"],
        constraints=tuple(components["constraints"]),
        objectives=tuple(components["objectives"]),
        limits=components["limits"],
    )


def _compile_problem(
    *,
    normalized_input: NormalizedInput,
    fact_snapshot: FactSnapshot,
    compiler: OrderedBinaryRouteCompiler | None = None,
) -> ProblemIR:
    compiled = (compiler or OrderedBinaryRouteCompiler()).compile_problem(
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    return _problem_from_compiled(
        compiled,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )


def _candidate_payloads(problem: ProblemIR) -> tuple[Mapping[str, Any], ...]:
    return tuple(problem.parameters["candidate_selection"]["candidates"])


def _service_candidate_digest(problem: ProblemIR) -> str:
    return sha256_digest(
        [
            {
                "candidate_id": candidate["candidate_id"],
                "eligible": candidate["eligible"],
                "assignment": dict(candidate["assignment"]),
                "metrics": {
                    objective["metric"]: candidate["metrics"][objective["metric"]] for objective in problem.objectives
                },
            }
            for candidate in _candidate_payloads(problem)
        ]
    )


def _objective_key(candidate: Mapping[str, Any]) -> tuple[int, int, int, int]:
    metrics = candidate["metrics"]
    return (
        -metrics["primary_score"],
        metrics["transition_count"],
        -metrics["safety_margin"],
        metrics["candidate_rank"],
    )


def _result_for_candidate(
    problem: ProblemIR,
    candidate: Mapping[str, Any],
    *,
    status: str = "optimal",
) -> SolverResult:
    return SolverResult(
        problem_id=problem.problem_id,
        status=status,
        assignment={
            "candidate_id": candidate["candidate_id"],
            **dict(candidate["assignment"]),
        },
        objective_values={
            objective["metric"]: candidate["metrics"][objective["metric"]] for objective in problem.objectives
        },
        diagnostics=(),
        engine={"name": "ortools-cp-sat", "version": "synthetic"},
    )


def _optimal_result(problem: ProblemIR) -> SolverResult:
    eligible = [candidate for candidate in _candidate_payloads(problem) if candidate["eligible"]]
    return _result_for_candidate(problem, min(eligible, key=_objective_key))


def _fixture() -> tuple[NormalizedInput, FactSnapshot, ProblemIR, SolverResult]:
    normalized_input = _normalized_input()
    fact_snapshot = _snapshot(normalized_input)
    problem = _compile_problem(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    return normalized_input, fact_snapshot, problem, _optimal_result(problem)


def _domain_certificate_fixture():
    normalized_input, fact_snapshot, base_problem, _solver_result = _fixture()
    compiled = base_problem.to_dict()
    compiled["parameters"]["domain_evaluation"] = {"schema_version": "refinery_route_context.v1"}
    problem = _problem_from_compiled(
        compiled,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    candidate_digest = _service_candidate_digest(problem)
    result = replace(
        _optimal_result(problem),
        diagnostics=(
            {
                "kind": "connector_assurance",
                "best_bound": None,
                "gap": "0",
                "certificate": {
                    "schema_version": "refinery_route_evaluation.v1",
                    "evaluation_source": "refinery_route.v1",
                    "candidate_count": len(_candidate_payloads(problem)),
                    "candidate_digest": candidate_digest,
                },
            },
        ),
    )
    return normalized_input, fact_snapshot, problem, result


def _verify(
    *,
    normalized_input: NormalizedInput,
    fact_snapshot: FactSnapshot,
    problem: ProblemIR,
    solver_result: SolverResult,
):
    return OrderedBinaryRouteIndependentVerifier().verify(
        problem=problem,
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        solver_result=solver_result,
    )


def test_independent_verifier_proves_a_genuine_result_optimal():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()

    verification = _verify(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        problem=problem,
        solver_result=solver_result,
    )

    assert verification.status == "verified"
    assert verification.feasibility == "feasible"
    assert verification.optimality == "proven_optimal"
    assert verification.proof_kind == "verifier_exhaustive_check"


def test_independent_verifier_records_matching_domain_candidate_certificate():
    normalized_input, fact_snapshot, problem, solver_result = _domain_certificate_fixture()

    verification = _verify(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        problem=problem,
        solver_result=solver_result,
    )

    evidence = verification.evidence[0]
    assert evidence["service_candidate_digest"] == _service_candidate_digest(problem)


def test_independent_verifier_rejects_divergent_domain_candidate_certificate():
    normalized_input, fact_snapshot, problem, solver_result = _domain_certificate_fixture()
    assurance = solver_result.diagnostics[0]
    bad_assurance = {
        **dict(assurance),
        "certificate": {
            **dict(assurance["certificate"]),
            "candidate_digest": "sha256:solver-coordination.v1:" + "f" * 64,
        },
    }
    solver_result = replace(solver_result, diagnostics=(bad_assurance,))

    with pytest.raises(VerificationDivergenceError, match="certificate"):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=solver_result,
        )


def test_independent_verifier_recomputes_complete_candidate_snapshot():
    normalized_input = _normalized_input()
    fact_snapshot = _snapshot(
        normalized_input,
        fact_value=_candidate_route_fact(),
    )
    problem = _compile_problem(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    solver_result = _optimal_result(problem)

    verification = _verify(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        problem=problem,
        solver_result=solver_result,
    )

    assert solver_result.assignment["candidate_id"] == "OPTION_1_TO_OPTION_0_K0002"
    assert verification.status == "verified"
    assert verification.optimality == "proven_optimal"


def test_independent_verifier_rejects_a_forged_candidate_id():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    forged = replace(
        solver_result,
        assignment={**dict(solver_result.assignment), "candidate_id": "FORGED_CANDIDATE"},
    )

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=forged,
        )


def test_independent_verifier_rejects_an_altered_item_assignment():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    assignment = dict(solver_result.assignment)
    assignment["ITEM_SYNTHETIC_0001"] = "OPTION_1"
    forged = replace(solver_result, assignment=assignment)

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=forged,
        )


def test_independent_verifier_rejects_an_unknown_item_assignment_key():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    assignment = {
        ("UNKNOWN_ITEM" if key == "ITEM_SYNTHETIC_0001" else key): value
        for key, value in solver_result.assignment.items()
    }
    forged = replace(solver_result, assignment=assignment)

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=forged,
        )


def test_independent_verifier_rejects_assignment_with_noncanonical_item_key_order():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    canonical_items = tuple(key for key in solver_result.assignment if key != "candidate_id")
    canonical_values = tuple(solver_result.assignment[key] for key in canonical_items)
    assignment = {"candidate_id": solver_result.assignment["candidate_id"]}
    assignment.update(zip(reversed(canonical_items), canonical_values))
    forged = replace(solver_result, assignment=assignment)

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=forged,
        )


def test_independent_verifier_rejects_altered_objective_values():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    objectives = dict(solver_result.objective_values)
    objectives["primary_score"] += 1
    forged = replace(solver_result, objective_values=objectives)

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=forged,
        )


def test_independent_verifier_rejects_a_changed_candidate_order():
    normalized_input, fact_snapshot, problem, _solver_result = _fixture()
    compiled = problem.to_dict()
    selection = compiled["parameters"]["candidate_selection"]
    selection["candidates"] = list(reversed(selection["candidates"]))
    selection["candidate_evaluation_digest"] = sha256_digest(selection["candidates"])
    reordered_problem = _problem_from_compiled(
        compiled,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=reordered_problem,
            solver_result=_optimal_result(reordered_problem),
        )


def test_independent_verifier_rejects_a_changed_fact_snapshot_digest():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    changed_snapshot = _snapshot(
        normalized_input,
        as_of="2026-08-18T00:00:01+00:00",
    )
    assert changed_snapshot.digest != fact_snapshot.digest

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=changed_snapshot,
            problem=problem,
            solver_result=solver_result,
        )


def test_independent_verifier_rejects_a_nonoptimal_optimal_claim():
    normalized_input, fact_snapshot, problem, _solver_result = _fixture()
    nonoptimal = next(candidate for candidate in _candidate_payloads(problem) if candidate["candidate_id"] == "ALL_0")

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=_result_for_candidate(problem, nonoptimal),
        )


class _TamperedProductionEvaluator:
    def evaluate_candidates(self, **kwargs: Any) -> tuple[CandidateEvaluation, ...]:
        evaluations = OrderedBinaryRouteEvaluator().evaluate_candidates(**kwargs)
        tampered = []
        for evaluation in evaluations:
            metrics = dict(evaluation.objective_metrics)
            if evaluation.candidate_id == "ALL_0":
                metrics["primary_score"] += 50000
            tampered.append(replace(evaluation, objective_metrics=metrics))
        return tuple(tampered)


def test_independent_verifier_rejects_a_consistently_tampered_production_evaluator():
    normalized_input = _normalized_input()
    fact_snapshot = _snapshot(normalized_input)
    problem = _compile_problem(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        compiler=OrderedBinaryRouteCompiler(evaluator=_TamperedProductionEvaluator()),
    )
    tampered_result = _optimal_result(problem)
    assert tampered_result.assignment["candidate_id"] == "ALL_0"

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=tampered_result,
        )


def test_independent_verifier_proves_infeasibility_only_when_no_candidate_is_eligible():
    normalized_input = _normalized_input()
    facts = deepcopy(ROUTE_FACT)
    facts["capacity_limit"] = "0.00"
    fact_snapshot = _snapshot(normalized_input, fact_value=facts)
    problem = _compile_problem(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    solver_result = SolverResult(
        problem_id=problem.problem_id,
        status="infeasible",
        assignment={},
        objective_values={},
        diagnostics=(),
        engine={"name": "ortools-cp-sat", "version": "synthetic"},
    )

    verification = _verify(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        problem=problem,
        solver_result=solver_result,
    )

    assert verification.status == "verified"
    assert verification.feasibility == "infeasible"
    assert verification.optimality == "not_applicable"
    assert verification.proof_kind == "verifier_exhaustive_check"


def test_independent_verifier_rejects_infeasible_payload_fields():
    normalized_input = _normalized_input()
    facts = deepcopy(ROUTE_FACT)
    facts["capacity_limit"] = "0.00"
    fact_snapshot = _snapshot(normalized_input, fact_value=facts)
    problem = _compile_problem(
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )

    for assignment, objective_values in (
        ({"candidate_id": "FORGED_CANDIDATE"}, {}),
        ({}, {"primary_score": 0}),
    ):
        solver_result = SolverResult(
            problem_id=problem.problem_id,
            status="infeasible",
            assignment=assignment,
            objective_values=objective_values,
            diagnostics=(),
            engine={"name": "ortools-cp-sat", "version": "synthetic"},
        )
        with pytest.raises(VerificationDivergenceError):
            _verify(
                normalized_input=normalized_input,
                fact_snapshot=fact_snapshot,
                problem=problem,
                solver_result=solver_result,
            )


def test_independent_verifier_rejects_an_unsupported_solver_status():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()

    with pytest.raises(VerificationDivergenceError):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=problem,
            solver_result=replace(solver_result, status="completed"),
        )


def test_independent_verifier_refuses_work_above_the_verification_limit():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    payload = problem.to_dict()
    payload["limits"]["max_verification_candidates"] = 7
    limited_problem = _problem_from_compiled(
        payload,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )
    limited_result = replace(solver_result, problem_id=limited_problem.problem_id)

    with pytest.raises(VerificationRuntimeFailure):
        _verify(
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            problem=limited_problem,
            solver_result=limited_result,
        )


class _Registration:
    def __init__(self, verifier: Any) -> None:
        self.verifier = verifier


class _DispatchRegistry:
    def __init__(self, registration: Any | None = None) -> None:
        self.registration = registration
        self.requested: list[str] = []

    def get(self, profile_id: str):
        self.requested.append(profile_id)
        if self.registration is None:
            raise LookupError(profile_id)
        return self.registration


class _VerifierSpy:
    def __init__(self) -> None:
        self.kwargs: dict[str, Any] | None = None

    def verify(self, **kwargs: Any):
        self.kwargs = kwargs
        return "verified-synthetic"


class _RuntimeFailureVerifier:
    def verify(self, **_kwargs: Any):
        raise VerificationRuntimeFailure("synthetic verifier runtime failure")


def test_profile_dispatch_uses_locked_profile_and_passes_frozen_problem():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    verifier = _VerifierSpy()
    registry = _DispatchRegistry(_Registration(verifier))

    result = ProfileDispatchVerifier(registry).verify(
        problem=problem,
        capability=_capability(),
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
        solver_result=solver_result,
    )

    assert result == "verified-synthetic"
    assert registry.requested == ["ordered_binary_route.v1"]
    assert verifier.kwargs == {
        "problem": problem,
        "capability": _capability(),
        "normalized_input": normalized_input,
        "fact_snapshot": fact_snapshot,
        "solver_result": solver_result,
    }


def test_profile_dispatch_preserves_verifier_runtime_failures():
    normalized_input, fact_snapshot, problem, solver_result = _fixture()

    with pytest.raises(VerificationRuntimeFailure, match="runtime failure"):
        ProfileDispatchVerifier(_DispatchRegistry(_Registration(_RuntimeFailureVerifier()))).verify(
            problem=problem,
            capability=_capability(),
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            solver_result=solver_result,
        )


@pytest.mark.parametrize("profile_id", ["missing-profile", " ordered_binary_route.v1 ", ""])
def test_profile_dispatch_fails_closed_for_unknown_or_noncanonical_profiles(profile_id: str):
    normalized_input, fact_snapshot, problem, solver_result = _fixture()
    capability = _capability()
    capability["decision_model"]["profile_id"] = profile_id

    with pytest.raises(VerificationDivergenceError):
        ProfileDispatchVerifier(_DispatchRegistry()).verify(
            problem=problem,
            capability=capability,
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
            solver_result=solver_result,
        )
