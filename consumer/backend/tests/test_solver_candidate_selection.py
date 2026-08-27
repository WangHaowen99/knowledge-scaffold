from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Callable

import pytest

from solver_coordination.contracts import ProblemIR, SolverContractError
from solver_coordination.problem_families.candidate_selection import (
    CandidateSelectionAdapter,
    SelectionObjective,
    parse_candidate_selection,
    validate_candidate_selection_components,
)


def _candidate(
    candidate_id: str,
    *,
    eligible: bool = True,
    score: int = 10,
    rank: int = 0,
    assignment: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "candidate_id": candidate_id,
        "eligible": eligible,
        "assignment": assignment or {"item_0001": "OPTION_0"},
        "metrics": {
            "primary_score": score,
            "candidate_rank": rank,
        },
    }


def _components() -> dict[str, Any]:
    candidates = [
        _candidate("CANDIDATE_SYNTHETIC_0000", score=10, rank=0),
        _candidate("CANDIDATE_SYNTHETIC_0001", score=20, rank=1),
    ]
    return {
        "decision_space": {"variables": ["selected_candidate"]},
        "parameters": {
            "candidate_selection": {
                "candidate_count": len(candidates),
                "candidates": candidates,
            }
        },
        "constraints": [{"kind": "select_exactly_one_candidate"}],
        "objectives": [
            {"metric": "primary_score", "sense": "maximize"},
            {"metric": "candidate_rank", "sense": "minimize"},
        ],
        "limits": {"max_candidates": 2},
    }


def _problem_payload() -> dict[str, Any]:
    return {
        "schema_version": "solver.problem.v1",
        "problem_id": "sha256:solver-coordination.v1:" + "0" * 64,
        "problem_family": "candidate_selection.v1",
        **_components(),
    }


def _problem() -> ProblemIR:
    payload = _problem_payload()
    return ProblemIR(
        problem_id=payload["problem_id"],
        problem_family=payload["problem_family"],
        decision_space=payload["decision_space"],
        parameters=payload["parameters"],
        constraints=tuple(payload["constraints"]),
        objectives=tuple(payload["objectives"]),
        limits=payload["limits"],
        schema_version=payload["schema_version"],
    )


def test_candidate_selection_contract_parses_canonical_problem_ir():
    parsed = parse_candidate_selection(_problem())

    assert parsed.candidate_ids == (
        "CANDIDATE_SYNTHETIC_0000",
        "CANDIDATE_SYNTHETIC_0001",
    )
    assert parsed.objectives == (
        SelectionObjective("primary_score", "maximize"),
        SelectionObjective("candidate_rank", "minimize"),
    )
    assert parsed.max_candidates == 2
    assert parsed.candidates[1].eligible is True
    assert dict(parsed.candidates[1].assignment) == {"item_0001": "OPTION_0"}
    assert dict(parsed.candidates[1].metrics) == {
        "primary_score": 20,
        "candidate_rank": 1,
    }


def test_candidate_selection_contract_parses_raw_mapping_envelope():
    parsed = parse_candidate_selection(_problem_payload())

    assert parsed.candidate_ids == (
        "CANDIDATE_SYNTHETIC_0000",
        "CANDIDATE_SYNTHETIC_0001",
    )


def test_missing_max_candidates_defaults_to_the_validated_candidate_count():
    payload = _problem_payload()
    payload["limits"] = {}

    parsed = parse_candidate_selection(payload)

    assert parsed.max_candidates == 2


Mutation = Callable[[dict[str, Any]], None]


def _duplicate_candidate_id(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][1][
        "candidate_id"
    ] = "CANDIDATE_SYNTHETIC_0000"


def _blank_candidate_id(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][0][
        "candidate_id"
    ] = "   "


def _missing_objective_metric(payload: dict[str, Any]) -> None:
    del payload["parameters"]["candidate_selection"]["candidates"][0][
        "metrics"
    ]["primary_score"]


def _boolean_metric(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][0]["metrics"][
        "primary_score"
    ] = True


def _overflow_metric(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][0]["metrics"][
        "primary_score"
    ] = 2**63


def _rank_not_equal_to_position(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][1]["metrics"][
        "candidate_rank"
    ] = 0


def _different_assignment_keys(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][1][
        "assignment"
    ] = {"item_0002": "OPTION_1"}


def _reserved_assignment_key(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][0][
        "assignment"
    ]["candidate_id"] = "COLLISION"


def _non_boolean_eligible(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][0][
        "eligible"
    ] = 1


def _unknown_candidate_field(payload: dict[str, Any]) -> None:
    payload["parameters"]["candidate_selection"]["candidates"][0][
        "unexpected"
    ] = "unsupported"


def _unknown_objective_field(payload: dict[str, Any]) -> None:
    payload["objectives"][0]["unexpected"] = "unsupported"


def _unsupported_objective_sense(payload: dict[str, Any]) -> None:
    payload["objectives"][0]["sense"] = "ascending"


def _missing_final_rank_objective(payload: dict[str, Any]) -> None:
    payload["objectives"].pop()


def _empty_candidates(payload: dict[str, Any]) -> None:
    selection = payload["parameters"]["candidate_selection"]
    selection["candidates"] = []
    selection["candidate_count"] = 0


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (_duplicate_candidate_id, "candidate_id"),
        (_blank_candidate_id, "candidate_id"),
        (_missing_objective_metric, "metric"),
        (_boolean_metric, "integer"),
        (_overflow_metric, "int64"),
        (_rank_not_equal_to_position, "candidate_rank"),
        (_different_assignment_keys, "assignment variables"),
        (_reserved_assignment_key, "candidate_id"),
        (_non_boolean_eligible, "eligible"),
        (_unknown_candidate_field, "fields"),
        (_unknown_objective_field, "fields"),
        (_unsupported_objective_sense, "sense"),
        (_missing_final_rank_objective, "candidate_rank"),
        (_empty_candidates, "candidate"),
    ],
    ids=lambda value: getattr(value, "__name__", str(value)),
)
def test_candidate_selection_contract_rejects_noncanonical_payload(
    mutation: Mutation,
    message: str,
):
    payload = deepcopy(_problem_payload())
    mutation(payload)

    with pytest.raises(SolverContractError, match=message):
        parse_candidate_selection(payload)


@pytest.mark.parametrize("value", [False, 0, -1, 2**63])
def test_candidate_selection_contract_rejects_invalid_max_candidates(value: Any):
    payload = _problem_payload()
    payload["limits"]["max_candidates"] = value

    with pytest.raises(SolverContractError, match="max_candidates"):
        parse_candidate_selection(payload)


def test_candidate_selection_contract_rejects_count_above_max_candidates():
    payload = _problem_payload()
    payload["limits"]["max_candidates"] = 1

    with pytest.raises(SolverContractError, match="max_candidates"):
        parse_candidate_selection(payload)


@pytest.mark.parametrize("candidate_count", [True, 1, 3, 2**63])
def test_candidate_selection_contract_rejects_invalid_candidate_count(
    candidate_count: Any,
):
    payload = _problem_payload()
    payload["parameters"]["candidate_selection"][
        "candidate_count"
    ] = candidate_count

    with pytest.raises(SolverContractError, match="candidate_count"):
        parse_candidate_selection(payload)


def test_candidate_selection_contract_rejects_the_wrong_problem_family():
    payload = _problem_payload()
    payload["problem_family"] = "unsupported_family.v1"

    with pytest.raises(SolverContractError, match="problem_family"):
        parse_candidate_selection(payload)


def test_candidate_selection_contract_rejects_non_mapping_problem_input():
    with pytest.raises(SolverContractError, match="ProblemIR or an object"):
        parse_candidate_selection(object())


def test_candidate_selection_contract_rejects_missing_candidate_selection_section():
    payload = _problem_payload()
    payload["parameters"] = {}

    with pytest.raises(SolverContractError, match="candidate_selection"):
        parse_candidate_selection(payload)


def test_candidate_selection_contract_rejects_non_mapping_assignment_and_metrics():
    for field_name in ("assignment", "metrics"):
        payload = _problem_payload()
        payload["parameters"]["candidate_selection"]["candidates"][0][
            field_name
        ] = []

        with pytest.raises(SolverContractError, match=field_name):
            parse_candidate_selection(payload)


def test_candidate_selection_contract_rejects_non_array_candidates_and_objectives():
    payload = _problem_payload()
    payload["parameters"]["candidate_selection"]["candidates"] = {}

    with pytest.raises(SolverContractError, match="candidates"):
        parse_candidate_selection(payload)

    payload = _problem_payload()
    payload["objectives"] = {}

    with pytest.raises(SolverContractError, match="objectives"):
        parse_candidate_selection(payload)


def test_candidate_selection_components_preserve_platform_neutral_payload_rules():
    components = _components()
    components["parameters"]["candidate_selection"]["candidates"][0][
        "assignment"
    ] = {"auth_token": "not-a-credential"}
    components["parameters"]["candidate_selection"]["candidates"][1][
        "assignment"
    ] = {"auth_token": "not-a-credential"}

    with pytest.raises(SolverContractError, match="credential"):
        validate_candidate_selection_components(components)


@dataclass
class _Registration:
    compiler: Any


class _ProfileCompiler:
    def __init__(self, compiled: dict[str, Any]) -> None:
        self.compiled = compiled
        self.calls: list[dict[str, Any]] = []

    def compile_problem(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self.compiled


class _ProfileRegistry:
    def __init__(self, registration: _Registration) -> None:
        self.registration = registration
        self.requested_ids: list[str] = []

    def get(self, profile_id: str) -> _Registration:
        self.requested_ids.append(profile_id)
        return self.registration


def test_candidate_selection_adapter_delegates_to_the_injected_profile_compiler():
    compiled = _components()
    compiler = _ProfileCompiler(compiled)
    registry = _ProfileRegistry(_Registration(compiler=compiler))
    adapter = CandidateSelectionAdapter(registry)
    capability = {
        "decision_model": {"profile_id": "profile_synthetic.v1"},
    }
    normalized_input = object()
    fact_snapshot = object()

    result = adapter.compile_problem(
        capability=capability,
        normalized_input=normalized_input,
        fact_snapshot=fact_snapshot,
    )

    assert result is compiled
    assert registry.requested_ids == ["profile_synthetic.v1"]
    assert compiler.calls == [
        {
            "capability": capability,
            "normalized_input": normalized_input,
            "fact_snapshot": fact_snapshot,
        }
    ]


def test_candidate_selection_adapter_validates_compiled_components_before_returning():
    compiled = _components()
    compiled["parameters"]["candidate_selection"]["candidates"][0]["metrics"][
        "candidate_rank"
    ] = 1
    registry = _ProfileRegistry(
        _Registration(compiler=_ProfileCompiler(compiled))
    )

    with pytest.raises(SolverContractError, match="candidate_rank"):
        CandidateSelectionAdapter(registry).compile_problem(
            capability={
                "decision_model": {"profile_id": "profile_synthetic.v1"}
            },
            normalized_input=object(),
            fact_snapshot=object(),
        )
