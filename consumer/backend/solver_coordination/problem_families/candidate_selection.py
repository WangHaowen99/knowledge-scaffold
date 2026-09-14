# Copyright (c) Huawei Technologies Co., Ltd. 2026-2026. All rights reserved.
"""Immutable contract for selecting one pre-evaluated candidate."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from solver_coordination.contracts import (
    ProblemIR,
    SolverContractError,
    _assert_model_payload_is_platform_neutral,
    _freeze_array,
    _freeze_object,
    _required_text,
)


PROBLEM_FAMILY = "candidate_selection.v1"
INT64_MIN = -(2**63)
INT64_MAX = 2**63 - 1

_CANDIDATE_FIELDS = {"candidate_id", "eligible", "assignment", "metrics"}
_OBJECTIVE_FIELDS = {"metric", "sense"}
_ALLOWED_SENSES = {"maximize", "minimize"}
_RESERVED_ASSIGNMENT_FIELD = "candidate_id"


@dataclass(frozen=True)
class SelectionObjective:
    metric: str
    sense: str


@dataclass(frozen=True)
class SelectionCandidate:
    candidate_id: str
    eligible: bool
    assignment: Mapping[str, Any]
    metrics: Mapping[str, int]


@dataclass(frozen=True)
class CandidateSelectionProblem:
    candidates: tuple[SelectionCandidate, ...]
    objectives: tuple[SelectionObjective, ...]
    max_candidates: int

    @property
    def candidate_ids(self) -> tuple[str, ...]:
        return tuple(candidate.candidate_id for candidate in self.candidates)


def _int64(value: Any, field_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise SolverContractError(f"{field_name} must be an integer")
    if not INT64_MIN <= value <= INT64_MAX:
        raise SolverContractError(f"{field_name} exceeds int64")
    return value


def _exact_fields(
    value: Mapping[str, Any], expected: set[str], *, field_name: str
) -> None:
    if set(value) != expected:
        raise SolverContractError(f"{field_name} fields are invalid")


def _canonical_text(value: Any, field_name: str) -> str:
    normalized = _required_text(value, field_name)
    if value != normalized:
        raise SolverContractError(f"{field_name} must be canonical text")
    return normalized


def _selection_candidates(
    selection: Mapping[str, Any],
) -> tuple[SelectionCandidate, ...]:
    raw_candidates = _freeze_array(
        selection.get("candidates"), "candidate_selection candidates"
    )
    if not raw_candidates:
        raise SolverContractError("candidate_selection requires at least one candidate")

    candidates: list[SelectionCandidate] = []
    candidate_ids: set[str] = set()
    assignment_variables: frozenset[str] | None = None
    for index, raw_candidate in enumerate(raw_candidates):
        if not isinstance(raw_candidate, Mapping):
            raise SolverContractError("candidate must be an object")
        field_name = f"candidate[{index}]"
        _exact_fields(raw_candidate, _CANDIDATE_FIELDS, field_name=field_name)

        candidate_id = _canonical_text(
            raw_candidate.get("candidate_id"), f"{field_name}.candidate_id"
        )
        if candidate_id in candidate_ids:
            raise SolverContractError("candidate_id values must be unique")
        candidate_ids.add(candidate_id)

        eligible = raw_candidate.get("eligible")
        if not isinstance(eligible, bool):
            raise SolverContractError(f"{field_name}.eligible must be a boolean")

        assignment = _freeze_object(
            raw_candidate.get("assignment"), f"{field_name}.assignment"
        )
        if _RESERVED_ASSIGNMENT_FIELD in assignment:
            raise SolverContractError("candidate assignment cannot contain candidate_id")
        variables = frozenset(assignment)
        if assignment_variables is None:
            assignment_variables = variables
        elif variables != assignment_variables:
            raise SolverContractError(
                "candidate assignment variables must match for every candidate"
            )

        raw_metrics = _freeze_object(
            raw_candidate.get("metrics"), f"{field_name}.metrics"
        )
        metrics: dict[str, int] = {}
        for metric_name, value in raw_metrics.items():
            canonical_name = _canonical_text(
                metric_name, f"{field_name} metric name"
            )
            metrics[canonical_name] = _int64(
                value, f"{field_name}.metrics.{canonical_name}"
            )

        candidates.append(
            SelectionCandidate(
                candidate_id=candidate_id,
                eligible=eligible,
                assignment=assignment,
                metrics=_freeze_object(metrics, f"{field_name}.metrics"),
            )
        )

    return tuple(candidates)


def _selection_objectives(raw: Any) -> tuple[SelectionObjective, ...]:
    raw_objectives = _freeze_array(raw, "objectives")
    if not raw_objectives:
        raise SolverContractError(
            "objectives must end with candidate_rank/minimize"
        )

    objectives: list[SelectionObjective] = []
    metrics: set[str] = set()
    for index, raw_objective in enumerate(raw_objectives):
        if not isinstance(raw_objective, Mapping):
            raise SolverContractError("objective must be an object")
        field_name = f"objective[{index}]"
        _exact_fields(raw_objective, _OBJECTIVE_FIELDS, field_name=field_name)
        metric = _canonical_text(
            raw_objective.get("metric"), f"{field_name}.metric"
        )
        if metric in metrics:
            raise SolverContractError("objective metric values must be unique")
        metrics.add(metric)
        sense = raw_objective.get("sense")
        if sense not in _ALLOWED_SENSES:
            raise SolverContractError(
                f"{field_name}.sense must be maximize or minimize"
            )
        objectives.append(SelectionObjective(metric=metric, sense=sense))

    if objectives[-1] != SelectionObjective("candidate_rank", "minimize"):
        raise SolverContractError(
            "final objective must be candidate_rank/minimize"
        )
    return tuple(objectives)


def _max_candidates(
    limits: Mapping[str, Any],
    selection: Mapping[str, Any],
    candidate_count: int,
) -> int:
    if "candidate_count" in selection:
        declared_count = _int64(
            selection["candidate_count"], "candidate_count"
        )
        if declared_count != candidate_count:
            raise SolverContractError(
                "candidate_count does not match candidates"
            )

    maximum = _int64(
        limits.get("max_candidates", candidate_count), "max_candidates"
    )
    if maximum < 1:
        raise SolverContractError("max_candidates must be positive")
    if candidate_count > maximum:
        raise SolverContractError("candidate count exceeds max_candidates")
    return maximum


def validate_candidate_selection_components(
    compiled: Mapping[str, Any],
) -> CandidateSelectionProblem:
    """Validate compiler components before the generic ProblemIR is created."""

    if not isinstance(compiled, Mapping):
        raise SolverContractError("compiled problem must be an object")
    parameters = _freeze_object(compiled.get("parameters"), "parameters")
    objectives_payload = _freeze_array(compiled.get("objectives"), "objectives")
    limits = _freeze_object(compiled.get("limits"), "limits")
    for value, field_name in (
        (parameters, "parameters"),
        (objectives_payload, "objectives"),
        (limits, "limits"),
    ):
        _assert_model_payload_is_platform_neutral(value, field_name)

    selection = parameters.get("candidate_selection")
    if not isinstance(selection, Mapping):
        raise SolverContractError("parameters.candidate_selection must be an object")
    candidates = _selection_candidates(selection)
    objectives = _selection_objectives(objectives_payload)

    for candidate_index, candidate in enumerate(candidates):
        for objective in objectives:
            if objective.metric not in candidate.metrics:
                raise SolverContractError(
                    "candidate metric is missing for objective {}".format(
                        objective.metric
                    )
                )
        if candidate.metrics["candidate_rank"] != candidate_index:
            raise SolverContractError(
                "candidate_rank must equal the canonical candidate position"
            )

    return CandidateSelectionProblem(
        candidates=candidates,
        objectives=objectives,
        max_candidates=_max_candidates(limits, selection, len(candidates)),
    )


def parse_candidate_selection(
    problem: ProblemIR | Mapping[str, Any],
) -> CandidateSelectionProblem:
    """Parse a candidate-selection ProblemIR object or its wire envelope."""

    if isinstance(problem, ProblemIR):
        problem_family = problem.problem_family
        components: Mapping[str, Any] = {
            "parameters": problem.parameters,
            "objectives": problem.objectives,
            "limits": problem.limits,
        }
    elif isinstance(problem, Mapping):
        problem_family = problem.get("problem_family")
        components = problem
    else:
        raise SolverContractError("problem must be ProblemIR or an object")

    if problem_family != PROBLEM_FAMILY:
        raise SolverContractError(
            "problem_family must be {}".format(PROBLEM_FAMILY)
        )
    return validate_candidate_selection_components(components)


class CandidateSelectionAdapter:
    """Delegate domain compilation through an injected profile registry."""

    family_key = PROBLEM_FAMILY
    problem_family = PROBLEM_FAMILY

    def __init__(self, profile_registry: Any) -> None:
        self.profile_registry = profile_registry

    def compile_problem(
        self,
        *,
        capability: Any,
        normalized_input: Any,
        fact_snapshot: Any,
    ) -> Any:
        if isinstance(capability, Mapping):
            model = capability.get("decision_model")
        else:
            model = getattr(capability, "decision_model", None)
        if not isinstance(model, Mapping):
            raise SolverContractError("decision_model must be an object")
        registration = self.profile_registry.get(str(model.get("profile_id") or ""))
        compiled = registration.compiler.compile_problem(
            capability=capability,
            normalized_input=normalized_input,
            fact_snapshot=fact_snapshot,
        )
        validate_candidate_selection_components(compiled)
        return compiled


__all__ = [
    "CandidateSelectionAdapter",
    "CandidateSelectionProblem",
    "INT64_MAX",
    "INT64_MIN",
    "PROBLEM_FAMILY",
    "SelectionCandidate",
    "SelectionObjective",
    "parse_candidate_selection",
    "validate_candidate_selection_components",
]
